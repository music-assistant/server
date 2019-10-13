#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
import operator
from aiohttp import web
import threading
import urllib
from memory_tempfile import MemoryTempfile
import io
import soundfile as sf
import pyloudnorm as pyln
import aiohttp
from .utils import LOGGER, try_parse_int, get_ip, run_async_background_task, run_periodic, get_folder_size
from .models.media_types import TrackQuality, MediaType
from .models.playerstate import PlayerState


class HTTPStreamer():
    ''' Built-in streamer using sox and webserver '''
    
    def __init__(self, mass):
        self.mass = mass
        self.local_ip = get_ip()
        self.analyze_jobs = {}
    
    async def stream(self, http_request):
        ''' 
            start stream for a player
        '''
        # make sure we have a valid player
        player_id = http_request.match_info.get('player_id','')
        player = await self.mass.player.get_player(player_id)
        if not player:
            LOGGER.error("Received stream request for non-existing player %s" %(player_id))
            return
        queue_item_id = http_request.query.get('queue_item_id')
        queue_item = await player.queue.by_item_id(queue_item_id)
        # prepare headers as audio/flac content
        resp = web.StreamResponse(status=200, reason='OK', headers={'Content-Type': 'audio/flac'})
        await resp.prepare(http_request)
        # send content only on GET request
        if http_request.method.upper() != 'HEAD':
            # stream audio
            queue = asyncio.Queue()
            cancelled = threading.Event()
            if queue_item:
                # single stream requested
                run_async_background_task(
                    self.mass.bg_executor, 
                    self.__stream_single, player, queue_item,  queue, cancelled)
            else:
                # no item is given, start queue stream
                run_async_background_task(
                    self.mass.bg_executor, 
                    self.__stream_queue, player, queue, cancelled)
            try:
                while True:
                    chunk = await queue.get()
                    if not chunk:
                        queue.task_done()
                        break
                    await resp.write(chunk)
                    queue.task_done()
                LOGGER.info("stream fininished for player %s" % player.name)
            except asyncio.CancelledError:
                cancelled.set()
                LOGGER.warning("stream interrupted for player %s" % player.name)
                raise asyncio.CancelledError()
        return resp
        
    async def __stream_single(self, player, queue_item, buffer, cancelled):
        ''' start streaming single track from provider '''
        try:
            audio_stream = self.__get_audio_stream(player, queue_item, cancelled)
            async for is_last_chunk, audio_chunk in audio_stream:
                asyncio.run_coroutine_threadsafe(
                        buffer.put(audio_chunk), 
                        self.mass.event_loop)
                # wait for the queue to consume the data
                # this prevents that the entire track is sitting in memory
                while buffer.qsize() > 2 and not cancelled.is_set():
                    await asyncio.sleep(1)
            # indicate EOF if no more data
            asyncio.run_coroutine_threadsafe(
                    buffer.put(b''), 
                    self.mass.event_loop)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            cancelled.set()
            LOGGER.info("stream_track interrupted for %s" % queue_item.name)
            raise asyncio.CancelledError()
        else:
            LOGGER.info("stream_track fininished for %s" % queue_item.name)

    async def __stream_queue(self, player, buffer, cancelled):
        ''' start streaming all queue tracks '''
        sample_rate = player.settings['max_sample_rate']
        fade_length = player.settings["crossfade_duration"]
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
                asyncio.run_coroutine_threadsafe(
                    buffer.put(chunk), 
                    self.mass.event_loop)
            # indicate EOF if no more data
            asyncio.run_coroutine_threadsafe(
                    buffer.put(b''), 
                    self.mass.event_loop)
        asyncio.create_task(fill_buffer())

        LOGGER.info("Start Queue Stream for player %s " %(player.name))
        is_start = True
        last_fadeout_data = b''
        while True:
            # get the (next) track in queue
            if is_start:
                # report start of queue playback so we can calculate current track/duration etc.
                queue_track = asyncio.run_coroutine_threadsafe(
                    player.queue.start_queue_stream(), 
                    self.mass.event_loop).result()
                is_start = False
            else:
                queue_track = player.queue.next_item
            if not queue_track:
                LOGGER.warning("no (more) tracks left in queue")
                break
            LOGGER.info("Start Streaming queue track: %s (%s) on player %s" % (queue_track.item_id, queue_track.name, player.name))
            fade_in_part = b''
            cur_chunk = 0
            prev_chunk = None
            bytes_written = 0
            async for is_last_chunk, chunk in self.__get_audio_stream(
                    player, queue_track, cancelled, chunksize=fade_bytes, resample=sample_rate):
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
                while buffer.qsize() > 2 and not cancelled.is_set():
                    await asyncio.sleep(1)
            # end of the track reached
            if cancelled.is_set():
                # break out the loop if the http session is cancelled
                LOGGER.debug("session cancelled")
                break
            else:
                # WIP: update actual duration to the queue for more accurate now playing info
                accurate_duration = bytes_written / int(sample_rate * 4 * 2)
                queue_track.duration = accurate_duration
                LOGGER.info("Finished Streaming queue track: %s (%s) on player %s" % (queue_track.item_id, queue_track.name, player.name))
                LOGGER.debug("bytes written: %s - duration: %s" % (bytes_written, accurate_duration))
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_data and not cancelled.is_set():
            sox_proc.stdin.write(last_fadeout_data)
            await sox_proc.stdin.drain()
        sox_proc.stdin.close()
        await sox_proc.wait()
        LOGGER.info("streaming of queue for player %s completed" % player.name)

    async def __get_audio_stream(self, player, queue_item, cancelled,
                chunksize=512000, resample=None):
        ''' get audio stream from provider and apply additional effects/processing where/if needed'''
        # get stream details from provider
        # sort by quality and check track availability
        for prov_media in sorted(queue_item.provider_ids, key=operator.itemgetter('quality'), reverse=True):
            streamdetails = asyncio.run_coroutine_threadsafe(
                    self.mass.music.providers[prov_media['provider']].get_stream_details(prov_media['item_id']), 
                    self.mass.event_loop).result()
            if streamdetails:
                queue_item.streamdetails = streamdetails
                queue_item.item_id = prov_media['item_id']
                queue_item.provider = prov_media['provider']
                queue_item.quality = prov_media['quality']
                break
        if not streamdetails:
            LOGGER.warning("no stream details!")
            yield (True, b'')
            return
        # get sox effects and resample options
        sox_effects = await self.__get_player_sox_options(player, queue_item)
        outputfmt = 'flac -C 0'
        if resample:
            outputfmt = 'raw -b 32 -c 2 -e signed-integer'
            sox_effects += ' rate -v %s' % resample
        # determine how to proceed based on input file ype
        if streamdetails["content_type"] == 'aac':
            # support for AAC created with ffmpeg in between
            args = 'ffmpeg -i "%s" -f flac - | sox -t flac - -t %s - %s' % (streamdetails["path"], outputfmt, sox_effects)
        elif streamdetails['type'] == 'url':
            args = 'sox -t %s "%s" -t %s - %s' % (streamdetails["content_type"], 
                    streamdetails["path"], outputfmt, sox_effects)
        elif streamdetails['type'] == 'executable':
            args = '%s | sox -t %s - -t %s - %s' % (streamdetails["path"], 
                    streamdetails["content_type"], outputfmt, sox_effects)
        # start sox process
        process = await asyncio.create_subprocess_shell(args,
                stdout=asyncio.subprocess.PIPE)
        # fire event that streaming has started for this track (needed by some streaming providers)
        streamdetails["provider"] = queue_item.provider
        streamdetails["track_id"] = queue_item.item_id
        streamdetails["player_id"] = player.player_id
        asyncio.run_coroutine_threadsafe(
                self.mass.signal_event('streaming_started', streamdetails), self.mass.event_loop)
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
            if prev_chunk:
                yield (False, prev_chunk)
                bytes_sent += len(prev_chunk)
            prev_chunk = chunk
        # yield last chunk
        if not cancelled.is_set():
            yield (True, prev_chunk)
            bytes_sent += len(prev_chunk)
        await process.wait()
        if cancelled.is_set():
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            LOGGER.debug("__get_audio_stream for track_id %s interrupted - bytes_sent: %s" % (queue_item.item_id, bytes_sent))
        else:
            LOGGER.debug("__get_audio_stream for track_id %s completed- bytes_sent: %s" % (queue_item.item_id, bytes_sent))
        # fire event that streaming has ended for this track (needed by some streaming providers)
        if resample:
            bytes_per_second = resample * (32/8) * 2
        else:
            bytes_per_second = streamdetails["sample_rate"] * (streamdetails["bit_depth"]/8) * 2
        seconds_streamed = int(bytes_sent/bytes_per_second)
        streamdetails["seconds"] = seconds_streamed
        asyncio.run_coroutine_threadsafe(
                self.mass.signal_event('streaming_ended', streamdetails), 
                self.mass.event_loop)
        # send task to background to analyse the audio
        # TODO: send audio data completely
        if not queue_item.media_type == MediaType.Radio:
            asyncio.run_coroutine_threadsafe(
                self.__analyze_audio(queue_item.item_id, queue_item.provider), 
                self.mass.event_loop)

    async def __get_player_sox_options(self, player, queue_item):
        ''' get player specific sox effect options '''
        sox_effects = []
        # volume normalisation enabled but not natively handled by player so handle with sox
        if not player.supports_replay_gain and player.settings['volume_normalisation']:
            target_gain = int(player.settings['target_volume'])
            fallback_gain = int(player.settings['fallback_gain_correct'])
            track_loudness = await self.mass.db.get_track_loudness(
                    queue_item.item_id, queue_item.provider)
            if track_loudness == None:
                gain_correct = fallback_gain
            else:
                gain_correct = target_gain - track_loudness
            gain_correct = round(gain_correct,2)
            sox_effects.append('vol %s dB ' % gain_correct)
        else:
            gain_correct = ''
        # downsample if needed
        if player.settings['max_sample_rate']:
            max_sample_rate = try_parse_int(player.settings['max_sample_rate'])
            if max_sample_rate:
                quality = queue_item.quality
                if quality > TrackQuality.FLAC_LOSSLESS_HI_RES_3 and max_sample_rate == 192000:
                    sox_effects.append('rate -v 192000')
                elif quality > TrackQuality.FLAC_LOSSLESS_HI_RES_2 and max_sample_rate == 96000:
                    sox_effects.append('rate -v 96000')
                elif quality > TrackQuality.FLAC_LOSSLESS_HI_RES_1 and max_sample_rate == 48000:
                    sox_effects.append('rate -v 48000')
        if player.settings.get('sox_effects'):
            sox_effects.append(player.settings['sox_effects'])
        return " ".join(sox_effects)
        
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
