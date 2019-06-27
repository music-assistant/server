#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import LOGGER, try_parse_int, get_ip, run_async_background_task, run_periodic, get_folder_size
from models import TrackQuality, MediaType, PlayerState
import operator
from aiohttp import web
import threading
import urllib
from memory_tempfile import MemoryTempfile
import io
import soundfile as sf
import pyloudnorm as pyln
import aiohttp


class HTTPStreamer():
    ''' Built-in streamer using sox and webserver '''
    
    def __init__(self, mass):
        self.mass = mass
        self.create_config_entries()
        self.local_ip = get_ip()
        self.analyze_jobs = {}

    def create_config_entries(self):
        ''' sets the config entries for this module (list with key/value pairs)'''
        config_entries = [
            ('volume_normalisation', True, 'enable_r128_volume_normalisation'), 
            ('target_volume', '-23', 'target_volume_lufs'),
            ('fallback_gain_correct', '-12', 'fallback_gain_correct')
            ]
        if not self.mass.config['base'].get('http_streamer'):
            self.mass.config['base']['http_streamer'] = {}
        self.mass.config['base']['http_streamer']['__desc__'] = config_entries
        for key, def_value, desc in config_entries:
            if not key in self.mass.config['base']['http_streamer']:
                self.mass.config['base']['http_streamer'][key] = def_value
    
    async def stream_track(self, http_request):
        ''' start streaming track from provider '''
        player_id = http_request.query.get('player_id')
        track_id = http_request.query.get('track_id')
        provider = http_request.query.get('provider')
        resp = web.StreamResponse(status=200,
                                 reason='OK',
                                 headers={'Content-Type': 'audio/flac'})
        await resp.prepare(http_request)
        if http_request.method.upper() != 'HEAD':
            # stream audio
            cancelled = threading.Event()
            queue = asyncio.Queue()

            async def fill_buffer():
                ''' fill buffer runs in background process to prevent deadlocks of the sox executable '''
                audio_stream = self.__get_audio_stream(track_id, provider, player_id, cancelled)
                async for is_last_chunk, audio_chunk in audio_stream:
                    if not cancelled.is_set():
                        await queue.put(audio_chunk)
                    # wait for the queue to consume the data
                    # this prevents that the entire track is sitting in memory
                    while queue.qsize() > 1 and not cancelled.is_set():
                        await asyncio.sleep(1)
                await queue.put(b'') # EOF
            run_async_background_task(self.mass.bg_executor, fill_buffer)
               
            try:
                while True:
                    chunk = await queue.get()
                    if not chunk:
                        queue.task_done()
                        break
                    await resp.write(chunk)
                    queue.task_done()
            except (asyncio.CancelledError, asyncio.TimeoutError):
                cancelled.set()
                LOGGER.info("stream_track interrupted for %s" % track_id)
                raise asyncio.CancelledError()
            else:
                LOGGER.info("stream_track fininished for %s" % track_id)
                return resp        

    async def stream_radio(self, http_request):
        ''' start streaming radio from provider '''
        player_id = http_request.query.get('player_id')
        radio_id = http_request.query.get('radio_id')
        provider = http_request.query.get('provider')
        resp = web.StreamResponse(status=200,
                                 reason='OK',
                                 headers={'Content-Type': 'audio/flac'})
        await resp.prepare(http_request)
        if http_request.method.upper() != 'HEAD':
            # stream audio with sox
            sox_effects = await self.__get_player_sox_options(radio_id, provider, player_id, True)
            if self.mass.config['base']['http_streamer']['volume_normalisation']:
                gain_correct = await self.__get_track_gain_correct(radio_id, provider)
                gain_correct = 'vol %s dB ' % gain_correct
            else:
                gain_correct = ''
            media_item = await self.mass.music.item(radio_id, MediaType.Radio, provider)
            stream = sorted(media_item.provider_ids, key=operator.itemgetter('quality'), reverse=True)[0]
            stream_url = stream["details"]
            if stream["quality"] == TrackQuality.LOSSY_AAC:
                input_content_type = "aac"
            elif stream["quality"] == TrackQuality.LOSSY_OGG:
                input_content_type = "ogg"
            else:
                input_content_type = "mp3"
            if input_content_type == "aac":
                args = 'ffmpeg -i "%s" -f flac - | sox -t flac - -t flac -C 0 - %s %s' % (stream_url, gain_correct, sox_effects)
            else:
                args = 'sox -t %s "%s" -t flac -C 0 - %s %s' % (input_content_type, stream_url, gain_correct, sox_effects)
            LOGGER.info("Running sox with args: %s" % args)
            process = await asyncio.create_subprocess_shell(args, stdout=asyncio.subprocess.PIPE)
            try:
                while not process.stdout.at_eof():
                    chunk = await process.stdout.read(128000)
                    if not chunk:
                        break
                    await resp.write(chunk)
                await process.wait()
                LOGGER.info("streaming of radio_id %s completed" % radio_id)
            except asyncio.CancelledError:
                process.terminate()
                await process.wait()
                LOGGER.info("streaming of radio_id %s interrupted" % radio_id)
                raise asyncio.CancelledError()
        return resp
    
    async def stream_queue(self, http_request):
        ''' 
            stream all tracks in queue from player with http
            loads large part of audiodata in memory so only recommended for high performance servers
            use case is enable crossfade/gapless support for chromecast devices 
        '''
        player_id = http_request.query.get('player_id')
        startindex = int(http_request.query.get('startindex'))
        cancelled = threading.Event()
        resp = web.StreamResponse(status=200,
                                 reason='OK',
                                 headers={'Content-Type': 'audio/flac'})
        await resp.prepare(http_request)
        if http_request.method.upper() != 'HEAD':
            # stream audio
            queue = asyncio.Queue()
            cancelled = threading.Event()
            run_async_background_task(
                self.mass.bg_executor, 
                self.__stream_queue, player_id, startindex, queue, cancelled)
            try:
                while True:
                    chunk = await queue.get()
                    if not chunk:
                        queue.task_done()
                        break
                    await resp.write(chunk)
                    queue.task_done()
                LOGGER.info("stream_queue fininished for %s" % player_id)
            except asyncio.CancelledError:
                cancelled.set()
                LOGGER.info("stream_queue interrupted for %s" % player_id)
                raise asyncio.CancelledError()
        return resp

    async def __stream_queue(self, player_id, startindex, buffer, cancelled):
        ''' start streaming all queue tracks '''
        sample_rate = self.mass.config['player_settings'][player_id]['max_sample_rate']
        fade_length = self.mass.config['player_settings'][player_id]["crossfade_duration"]
        fade_bytes = int(sample_rate * 4 * 2 * fade_length)
        pcm_args = 'raw -b 32 -c 2 -e signed-integer -r %s' % sample_rate
        args = 'sox -t %s - -t flac -C 0 -' % pcm_args
        sox_proc = await asyncio.create_subprocess_shell(args, 
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)

        async def fill_buffer():
            while not sox_proc.stdout.at_eof():
                chunk = await sox_proc.stdout.read(256000)
                if not chunk:
                    break
                await buffer.put(chunk)
            await buffer.put(b'') # indicate EOF
        asyncio.create_task(fill_buffer())

        # retrieve player object
        player = await self.mass.player.player(player_id)
        queue_index = startindex
        LOGGER.info("Start Queue Stream for player %s at index %s" %(player.name, queue_index))
        last_fadeout_data = b''
        # report start of queue playback so we can calculate current track/duration etc.
        self.mass.event_loop.create_task(self.mass.player.player_queue_stream_update(player_id, queue_index, True))
        while True:
            # get the (next) track in queue
            try:
                queue_tracks = await self.mass.player.player_queue(player_id, queue_index, queue_index+1)
                queue_track = queue_tracks[0]
            except IndexError:
                LOGGER.warning("queue index out of range or end reached")
                break

            params = urllib.parse.parse_qs(queue_track.uri.split('?')[1])
            track_id = params['track_id'][0]
            provider = params['provider'][0]
            LOGGER.debug("Start Streaming queue track: %s (%s) on player %s" % (track_id, queue_track.name, player.name))
            fade_in_part = b''
            cur_chunk = 0
            prev_chunk = None
            bytes_written = 0
            async for is_last_chunk, chunk in self.__get_audio_stream(
                    track_id, provider, player_id, cancelled, chunksize=fade_bytes, resample=sample_rate):
                cur_chunk += 1
                if cur_chunk <= 2 and not last_fadeout_data:
                    # fade-in part but no fadeout_part available so just pass it to the output directly
                    sox_proc.stdin.write(chunk)
                    await sox_proc.stdin.drain()
                    bytes_written += len(chunk)
                elif cur_chunk == 1 and last_fadeout_data:
                    prev_chunk = chunk
                elif cur_chunk == 2 and last_fadeout_data:
                    # combine the first 2 chunks and strip off silence
                    args = 'sox --ignore-length -t %s - -t %s - silence 1 0.1 1%%' % (pcm_args, pcm_args)
                    process = await asyncio.create_subprocess_shell(args,
                            stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
                    first_part, stderr = await process.communicate(prev_chunk + chunk)
                    fade_in_part = first_part[:fade_bytes]
                    remaining_bytes = first_part[fade_bytes:]
                    del first_part
                    # do crossfade
                    crossfade_part = await self.__crossfade_pcm_parts(fade_in_part, last_fadeout_data, pcm_args, fade_length) 
                    sox_proc.stdin.write(crossfade_part)
                    await sox_proc.stdin.drain()
                    bytes_written += len(crossfade_part)
                    del crossfade_part
                    del fade_in_part
                    last_fadeout_data = b''
                    # also write the leftover bytes from the strip action
                    sox_proc.stdin.write(remaining_bytes)
                    await sox_proc.stdin.drain()
                    bytes_written += len(remaining_bytes)
                    del remaining_bytes
                    prev_chunk = None # needed to prevent this chunk being sent again
                elif prev_chunk and is_last_chunk:
                    # last chunk received so create the fadeout_part with the previous chunk and this chunk
                    # and strip off silence
                    args = 'sox --ignore-length -t %s - -t %s - reverse silence 1 0.1 1%% reverse' % (pcm_args, pcm_args)
                    process = await asyncio.create_subprocess_shell(args,
                            stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
                    last_part, stderr = await process.communicate(prev_chunk + chunk)
                    if len(last_part) < fade_bytes:
                        # not enough data for crossfade duration after the strip action...
                        last_part = prev_chunk + chunk
                    if len(last_part) < fade_bytes:
                        # still not enough data so we'll skip the crossfading
                        LOGGER.warning("not enough data for fadeout so skip crossfade... %s" % len(last_part))
                        sox_proc.stdin.write(last_part)
                        bytes_written += len(last_part)
                        await sox_proc.stdin.drain()
                        del last_part
                    else:
                        # store fade section to be picked up for next track
                        last_fadeout_data = last_part[-fade_bytes:]
                        remaining_bytes = last_part[:-fade_bytes]
                        # write remaining bytes
                        sox_proc.stdin.write(remaining_bytes)
                        bytes_written += len(remaining_bytes)
                        await sox_proc.stdin.drain()
                        del last_part
                        del remaining_bytes
                else:
                    # middle part of the track
                    # keep previous chunk in memory so we have enough samples to perform the crossfade
                    if prev_chunk:
                        sox_proc.stdin.write(prev_chunk)
                        await sox_proc.stdin.drain()
                        bytes_written += len(prev_chunk)
                        prev_chunk = chunk
                    else:
                        prev_chunk = chunk
                # wait for the queue to consume the data
                # this prevents that the entire track is sitting in memory
                # and it helps a bit in the quest to follow where we are in the queue
                while buffer.qsize() > 1 and not cancelled.is_set():
                    await asyncio.sleep(1)
            # end of the track reached
            if cancelled.is_set():
                # break out the loop if the http session is cancelled
                break
            else:
                # WIP: update actual duration to the queue for more accurate now playing info
                accurate_duration = bytes_written / int(sample_rate * 4 * 2)
                queue_track.duration = accurate_duration
                self.mass.player.providers[player.player_provider]._player_queue[player_id][queue_index] = queue_track
                # move to next queue index
                queue_index += 1
                self.mass.event_loop.create_task(self.mass.player.player_queue_stream_update(player_id, queue_index, False))
                LOGGER.debug("Finished Streaming queue track: %s (%s) on player %s" % (track_id, queue_track.name, player.name))
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_data and not cancelled.is_set():
            sox_proc.stdin.write(last_fadeout_data)
            await sox_proc.stdin.drain()
        sox_proc.stdin.close()
        await sox_proc.wait()
        LOGGER.info("streaming of queue for player %s completed" % player.name)

    async def __get_audio_stream(self, track_id, provider, player_id, cancelled,
                chunksize=512000, resample=None):
        ''' get audio stream from provider and apply additional effects/processing where/if needed'''
        if self.mass.config['base']['http_streamer']['volume_normalisation']:
            gain_correct = await self.__get_track_gain_correct(track_id, provider)
            gain_correct = 'vol %s dB ' % gain_correct
        else:
            gain_correct = ''
        sox_effects = await self.__get_player_sox_options(track_id, provider, player_id, False)
        outputfmt = 'flac -C 0'
        if resample:
            outputfmt = 'raw -b 32 -c 2 -e signed-integer'
            sox_effects += ' rate -v %s' % resample
        # stream audio from provider
        streamdetails = asyncio.run_coroutine_threadsafe(
                self.mass.music.providers[provider].get_stream_details(track_id), 
                self.mass.event_loop).result()
        if not streamdetails:
            yield (True, b'')
            return
        # TODO: add support for AAC streams (which sox doesn't natively support)
        if streamdetails['type'] == 'url':
            args = 'sox -t %s "%s" -t %s - %s %s' % (streamdetails["content_type"], 
                    streamdetails["path"], outputfmt, gain_correct, sox_effects)
        elif streamdetails['type'] == 'executable':
            args = '%s | sox -t %s - -t %s - %s %s' % (streamdetails["path"], 
                    streamdetails["content_type"], outputfmt, gain_correct, sox_effects)
        LOGGER.debug("Running sox with args: %s" % args)
        process = await asyncio.create_subprocess_shell(args,
                stdout=asyncio.subprocess.PIPE)
        # fire event that streaming has started for this track (needed by some streaming providers)
        streamdetails["provider"] = provider
        streamdetails["track_id"] = track_id
        streamdetails["player_id"] = player_id
        self.mass.signal_event('streaming_started', streamdetails)
        # yield chunks from stdout
        # we keep 1 chunk behind to detect end of stream properly
        prev_chunk = b''
        bytes_sent = 0
        while not process.stdout.at_eof():
            try:
                chunk = await process.stdout.readexactly(chunksize)
            except asyncio.streams.IncompleteReadError:
                chunk = await process.stdout.read(chunksize)
            if not chunk:
                break
            if prev_chunk and not cancelled.is_set():
                yield (False, prev_chunk)
                bytes_sent += len(prev_chunk)
            prev_chunk = chunk
        # yield last chunk
        if not cancelled.is_set():
            yield (True, prev_chunk)
            bytes_sent += len(prev_chunk)
        #await process.wait()
        if cancelled.is_set():
            LOGGER.warning("__get_audio_stream for track_id %s interrupted" % track_id)
        else:
            LOGGER.debug("__get_audio_stream for track_id %s completed" % track_id)
        # fire event that streaming has ended for this track (needed by some streaming providers)
        if resample:
            bytes_per_second = resample * (32/8) * 2
        else:
            bytes_per_second = streamdetails["sample_rate"] * (streamdetails["bit_depth"]/8) * 2
        seconds_streamed = int(bytes_sent/bytes_per_second)
        streamdetails["seconds"] = seconds_streamed
        self.mass.signal_event('streaming_ended', streamdetails)
        # send task to background to analyse the audio
        self.mass.event_loop.create_task(self.__analyze_audio(track_id, provider))

    async def __get_player_sox_options(self, track_id, provider, player_id, is_radio):
        ''' get player specific sox options '''
        sox_effects = ''
        if player_id and not is_radio and self.mass.config['player_settings'][player_id]['max_sample_rate']:
            # downsample if needed
            max_sample_rate = try_parse_int(self.mass.config['player_settings'][player_id]['max_sample_rate'])
            if max_sample_rate:
                quality = TrackQuality.LOSSY_MP3
                track_future = asyncio.run_coroutine_threadsafe(
                    self.mass.music.track(track_id, provider),
                    self.mass.event_loop
                )
                track = track_future.result()
                for item in track.provider_ids:
                    if item['provider'] == provider and item['item_id'] == track_id:
                        quality = item['quality']
                        break
                if quality > TrackQuality.FLAC_LOSSLESS_HI_RES_3 and max_sample_rate == 192000:
                    sox_effects += 'rate -v 192000'
                elif quality > TrackQuality.FLAC_LOSSLESS_HI_RES_2 and max_sample_rate == 96000:
                    sox_effects += 'rate -v 96000'
                elif quality > TrackQuality.FLAC_LOSSLESS_HI_RES_1 and max_sample_rate == 48000:
                    sox_effects += 'rate -v 48000'
        if player_id and self.mass.config['player_settings'][player_id]['sox_effects']:
            sox_effects += ' ' + self.mass.config['player_settings'][player_id]['sox_effects']
        return sox_effects
        
    async def __analyze_audio(self, track_id, provider):
        ''' analyze track audio, for now we only calculate EBU R128 loudness '''
        track_key = '%s%s' %(track_id, provider)
        if track_key in self.analyze_jobs:
            return # prevent multiple analyze jobs for same track
        self.analyze_jobs[track_key] = True
        streamdetails = await self.mass.music.providers[provider].get_stream_details(track_id)
        track_loudness = await self.mass.db.get_track_loudness(track_id, provider)
        if track_loudness == None:
            # only when needed we do the analyze stuff
            LOGGER.debug('Start analyzing track %s' % track_id)
            if streamdetails['type'] == 'url':
                async with aiohttp.ClientSession() as session:
                    async with session.get(streamdetails["path"]) as resp:
                        audio_data = await resp.read()
            elif streamdetails['type'] == 'executable':
                process = await asyncio.create_subprocess_shell(streamdetails["path"],
                    stdout=asyncio.subprocess.PIPE)
                audio_data, stderr = await process.communicate()
            # calculate BS.1770 R128 integrated loudness
            if track_loudness == None:
                with io.BytesIO(audio_data) as tmpfile:
                    data, rate = sf.read(tmpfile)
                meter = pyln.Meter(rate) # create BS.1770 meter
                loudness = meter.integrated_loudness(data) # measure loudness
                del data
                LOGGER.debug("Integrated loudness of track %s is: %s" %(track_id, loudness))
                await self.mass.db.set_track_loudness(track_id, provider, loudness)
            del audio_data
            LOGGER.debug('Finished analyzing track %s' % track_id)
        self.analyze_jobs.pop(track_key, None)
    
    async def __get_track_gain_correct(self, track_id, provider):
        ''' get the gain correction that should be applied to a track '''
        target_gain = int(self.mass.config['base']['http_streamer']['target_volume'])
        fallback_gain = int(self.mass.config['base']['http_streamer']['fallback_gain_correct'])
        track_loudness = await self.mass.db.get_track_loudness(track_id, provider)
        if track_loudness == None:
            return fallback_gain
        gain_correct = target_gain - track_loudness
        return round(gain_correct,2)

    async def __crossfade_pcm_parts(self, fade_in_part, fade_out_part, pcm_args, fade_length):
        ''' crossfade two chunks of audio using sox '''
        # create fade-in part
        fadeinfile = MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
        args = 'sox --ignore-length -t %s - -t %s %s fade t %s' % (pcm_args, pcm_args, fadeinfile.name, fade_length)
        process = await asyncio.create_subprocess_shell(args, stdin=asyncio.subprocess.PIPE)
        await process.communicate(fade_in_part)
        # create fade-out part
        fadeoutfile = MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
        args = 'sox --ignore-length -t %s - -t %s %s reverse fade t %s reverse' % (pcm_args, pcm_args, fadeoutfile.name, fade_length)
        process = await asyncio.create_subprocess_shell(args,
                stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
        await process.communicate(fade_out_part)
        # create crossfade using sox and some temp files
        # TODO: figure out how to make this less complex and without the tempfiles
        args = 'sox -m -v 1.0 -t %s %s -v 1.0 -t %s %s -t %s -' % (pcm_args, fadeoutfile.name, pcm_args, fadeinfile.name, pcm_args)
        process = await asyncio.create_subprocess_shell(args,
                stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
        crossfade_part, stderr = await process.communicate()
        LOGGER.debug("Got %s bytes in memory for crossfade_part after sox" % len(crossfade_part))
        return crossfade_part
