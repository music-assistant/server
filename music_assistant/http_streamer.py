#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
import operator
from aiohttp import web
import threading
import urllib
from memory_tempfile import MemoryTempfile
import soundfile
import pyloudnorm
import io
import aiohttp
import subprocess
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
        queue_item_id = http_request.match_info.get('queue_item_id')
        queue_item = await player.queue.by_item_id(queue_item_id)
        # prepare headers as audio/flac content
        resp = web.StreamResponse(status=200, reason='OK', headers={'Content-Type': 'audio/flac'})
        await resp.prepare(http_request)
        # send content only on GET request
        if http_request.method.upper() != 'HEAD':
            # stream audio
            buf_queue = asyncio.Queue()
            cancelled = threading.Event()
            if queue_item:
                # single stream requested, run stream in executor
                bg_task = run_async_background_task(
                    self.mass.bg_executor, 
                    self.__stream_single, player, queue_item, buf_queue, cancelled)
            else:
                # no item is given, start queue stream, run stream in executor
                bg_task = run_async_background_task(
                    self.mass.bg_executor, 
                    self.__stream_queue, player, buf_queue, cancelled)
            try:
                while True:
                    chunk = await buf_queue.get()
                    if not chunk:
                        buf_queue.task_done()
                        break
                    await resp.write(chunk)
                    buf_queue.task_done()
            except (asyncio.CancelledError, asyncio.TimeoutError):
                cancelled.set()
                # wait for bg_task
                await asyncio.sleep(1)
                del buf_queue
                raise asyncio.CancelledError()
        if not cancelled.is_set():
            return resp
    
    async def __stream_single(self, player, queue_item, buffer, cancelled):
        ''' start streaming single track from provider '''
        LOGGER.debug("stream single track started for track %s on player %s" % (queue_item.name, player.name))
        audio_stream = self.__get_audio_stream(player, queue_item, cancelled)
        async for is_last_chunk, audio_chunk in audio_stream:
            asyncio.run_coroutine_threadsafe(
                    buffer.put(audio_chunk), 
                    self.mass.event_loop)
        # indicate EOF if no more data
        asyncio.run_coroutine_threadsafe(
                buffer.put(b''), 
                self.mass.event_loop)
        if cancelled.is_set():
            LOGGER.debug("stream single track interrupted for track %s on player %s" % (queue_item.name, player.name))
        else:
            LOGGER.debug("stream single track finished for track %s on player %s" % (queue_item.name, player.name))

    async def __stream_queue(self, player, buffer, cancelled):
        ''' start streaming all queue tracks '''
        sample_rate = try_parse_int(player.settings['max_sample_rate'])
        fade_length = try_parse_int(player.settings["crossfade_duration"])
        if not sample_rate or sample_rate < 44100 or sample_rate > 384000:
            sample_rate = 96000
        if fade_length:
            fade_bytes = int(sample_rate * 4 * 2 * fade_length)
        else:
            fade_bytes = int(sample_rate * 4 * 2)
        pcm_args = 'raw -b 32 -c 2 -e signed-integer -r %s' % sample_rate
        args = 'sox -t %s - -t flac -C 0 -' % pcm_args
        # start sox process
        # we use normal subprocess instead of asyncio because of bug with executor
        # this should be fixed with python 3.8
        sox_proc = subprocess.Popen(args, shell=True, 
            stdout=subprocess.PIPE, stdin=subprocess.PIPE)

        def fill_buffer():
            sample_size = int(sample_rate * 4 * 2 * 2)
            while sox_proc.returncode == None:
                chunk = sox_proc.stdout.read(sample_size)
                if not chunk:
                    # no more data
                    break
                if not cancelled.is_set():
                    asyncio.run_coroutine_threadsafe(
                        buffer.put(chunk), 
                        self.mass.event_loop)
            # indicate EOF if no more data
            if not cancelled.is_set():
                asyncio.run_coroutine_threadsafe(
                        buffer.put(b''), 
                        self.mass.event_loop)
        threading.Thread(target=fill_buffer).start()
        

        LOGGER.info("Start Queue Stream for player %s " %(player.name))
        is_start = True
        last_fadeout_data = b''
        while True:
            if cancelled.is_set():
                break
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
                LOGGER.debug("no (more) tracks left in queue")
                break
            LOGGER.debug("Start Streaming queue track: %s (%s) on player %s" % (queue_track.item_id, queue_track.name, player.name))
            fade_in_part = b''
            cur_chunk = 0
            prev_chunk = None
            bytes_written = 0
            # handle incoming audio chunks
            async for is_last_chunk, chunk in self.__get_audio_stream(
                    player, queue_track, cancelled, chunksize=fade_bytes, resample=sample_rate):
                cur_chunk += 1

                ### HANDLE FIRST PART OF TRACK
                if cur_chunk <= 2 and not last_fadeout_data:
                    # no fadeout_part available so just pass it to the output directly
                    sox_proc.stdin.write(chunk)
                    bytes_written += len(chunk)
                elif cur_chunk == 1 and last_fadeout_data:
                    prev_chunk = chunk
                ### HANDLE CROSSFADE OF PREVIOUS TRACK FADE_OUT AND THIS TRACK FADE_IN
                elif cur_chunk == 2 and last_fadeout_data:
                    # combine the first 2 chunks and strip off silence
                    args = 'sox --ignore-length -t %s - -t %s - silence 1 0.1 1%%' % (pcm_args, pcm_args)
                    first_part, std_err = subprocess.Popen(args, shell=True,
                            stdout=subprocess.PIPE, 
                            stdin=subprocess.PIPE).communicate(prev_chunk + chunk)
                    fade_in_part = first_part[:fade_bytes]
                    remaining_bytes = first_part[fade_bytes:]
                    del first_part
                    # do crossfade
                    crossfade_part = self.__crossfade_pcm_parts(fade_in_part, 
                            last_fadeout_data, pcm_args, fade_length)
                    sox_proc.stdin.write(crossfade_part)
                    bytes_written += len(crossfade_part)
                    del crossfade_part
                    del fade_in_part
                    last_fadeout_data = b''
                    # also write the leftover bytes from the strip action
                    sox_proc.stdin.write(remaining_bytes)
                    bytes_written += len(remaining_bytes)
                    del remaining_bytes
                    prev_chunk = None # needed to prevent this chunk being sent again
                ### HANDLE LAST PART OF TRACK
                elif prev_chunk and is_last_chunk:
                    # last chunk received so create the fadeout_part with the previous chunk and this chunk
                    # and strip off silence
                    args = 'sox --ignore-length -t %s - -t %s - reverse silence 1 0.1 1%% reverse' % (pcm_args, pcm_args)
                    last_part, stderr = subprocess.Popen(args, shell=True,
                            stdout=subprocess.PIPE, 
                            stdin=subprocess.PIPE).communicate(prev_chunk + chunk)
                    if not player.queue.crossfade_enabled:
                        # crossfading is not enabled so just pass the (stripped) audio data
                        sox_proc.stdin.write(last_part)
                        bytes_written += len(last_part)
                        del last_part
                    else:
                        # handle crossfading support
                        if len(last_part) < fade_bytes:
                            # not enough data for crossfade duration after the strip action...
                            last_part = prev_chunk + chunk
                        if len(last_part) < fade_bytes:
                            # still not enough data so we'll skip the crossfading
                            LOGGER.debug("not enough data for fadeout so skip crossfade... %s" % len(last_part))
                            sox_proc.stdin.write(last_part)
                            bytes_written += len(last_part)
                            del last_part
                        else:
                            # store fade section to be picked up for next track
                            last_fadeout_data = last_part[-fade_bytes:]
                            remaining_bytes = last_part[:-fade_bytes]
                            # write remaining bytes
                            sox_proc.stdin.write(remaining_bytes)
                            bytes_written += len(remaining_bytes)
                            del last_part
                            del remaining_bytes
                ### MIDDLE PARTS OF TRACK
                else:
                    # middle part of the track
                    # keep previous chunk in memory so we have enough samples to perform the crossfade
                    if prev_chunk:
                        sox_proc.stdin.write(prev_chunk)
                        bytes_written += len(prev_chunk)
                        prev_chunk = chunk
                    else:
                        prev_chunk = chunk
            # end of the track reached
            if cancelled.is_set():
                # break out the loop if the http session is cancelled
                break
            else:
                # WIP: update actual duration to the queue for more accurate now playing info
                accurate_duration = bytes_written / int(sample_rate * 4 * 2)
                queue_track.duration = accurate_duration
                LOGGER.debug("Finished Streaming queue track: %s (%s) on player %s" % (queue_track.item_id, queue_track.name, player.name))
                LOGGER.debug("bytes written: %s - duration: %s" % (bytes_written, accurate_duration))
            # wait for the queue to consume the data
            while buffer.qsize() > 10 and not cancelled.is_set():
                await asyncio.sleep(1)
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_data and not cancelled.is_set():
            sox_proc.stdin.write(last_fadeout_data)
        sox_proc.stdin.close()
        sox_proc.terminate()
        LOGGER.info("streaming of queue for player %s completed" % player.name)

    async def __get_audio_stream(self, player, queue_item, cancelled,
                chunksize=128000, resample=None):
        ''' get audio stream from provider and apply additional effects/processing where/if needed'''
        # get stream details from provider
        # sort by quality and check track availability
        for prov_media in sorted(queue_item.provider_ids, 
                key=operator.itemgetter('quality'), reverse=True):
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
            LOGGER.warning(f"no stream details for {queue_item.name}")
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
        # we use normal subprocess instead of asyncio because of bug with executor
        # this should be fixed with python 3.8
        process = subprocess.Popen(args, shell=True, stdout=subprocess.PIPE)
        
        # fire event that streaming has started for this track (needed by some streaming providers)
        streamdetails["provider"] = queue_item.provider
        streamdetails["track_id"] = queue_item.item_id
        streamdetails["player_id"] = player.player_id
        asyncio.run_coroutine_threadsafe(
                self.mass.signal_event('streaming_started', 
                streamdetails), self.mass.event_loop)
        # yield chunks from stdout
        # we keep 1 chunk behind to detect end of stream properly
        bytes_sent = 0
        buf = b''
        while True:
            # read exactly buffersize of data
            if cancelled.is_set():
                process.terminate()
            data = process.stdout.read(chunksize)
            if not data:
                # last bytes received
                yield (True, buf)
                bytes_sent += len(buf)
                break
            elif len(buf) + len(data) >= chunksize:
                new_data = buf + data
                chunk = new_data[:chunksize]
                yield (False, chunk)
                bytes_sent += len(chunk)
                buf = new_data[chunksize:]
            else:
                buf += data
        if cancelled.is_set():
            return
        # fire event that streaming has ended for this track (needed by some streaming providers)
        if resample:
            bytes_per_second = resample * (32/8) * 2
            bytes_per_second = (resample * 32 * 2) / 8
            seconds_streamed = int(bytes_sent/bytes_per_second)
        else:
            seconds_streamed = queue_item.duration
        streamdetails["seconds"] = seconds_streamed
        asyncio.run_coroutine_threadsafe(
                self.mass.signal_event('streaming_ended', streamdetails), self.mass.event_loop)
        # send task to background to analyse the audio
        asyncio.run_coroutine_threadsafe(
            self.__analyze_audio(queue_item), self.mass.event_loop)

    async def __get_player_sox_options(self, player, queue_item):
        ''' get player specific sox effect options '''
        sox_effects = []
        # volume normalisation enabled but not natively handled by player so handle with sox
        if not player.supports_replay_gain and player.settings['volume_normalisation']:
            target_gain = int(player.settings['target_volume'])
            fallback_gain = int(player.settings['fallback_gain_correct'])
            track_loudness = asyncio.run_coroutine_threadsafe(
                    self.mass.db.get_track_loudness(queue_item.item_id, queue_item.provider), 
                    self.mass.event_loop).result()
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
        
    async def __analyze_audio(self, queue_item):
        ''' analyze track audio, for now we only calculate EBU R128 loudness '''
        if queue_item.media_type != MediaType.Track:
            # TODO: calculate loudness average for web radio ?
            return
        item_key = '%s%s' %(queue_item.item_id, queue_item.provider)
        if item_key in self.analyze_jobs:
            return # prevent multiple analyze jobs for same track
        self.analyze_jobs[item_key] = True
        streamdetails = queue_item.stream_details
        track_loudness = await self.mass.db.get_track_loudness(
                queue_item.item_id, queue_item.provider)
        if track_loudness == None:
            # only when needed we do the analyze stuff
            LOGGER.debug('Start analyzing track %s' % item_key)
            if streamdetails['type'] == 'url':
                async with aiohttp.ClientSession() as session:
                    async with session.get(streamdetails["path"], verify_ssl=False) as resp:
                        audio_data = await resp.read()
            elif streamdetails['type'] == 'executable':
                process = await asyncio.create_subprocess_shell(streamdetails["path"],
                    stdout=asyncio.subprocess.PIPE)
                audio_data, stderr = await process.communicate()
            # calculate BS.1770 R128 integrated loudness
            if track_loudness == None:
                with io.BytesIO(audio_data) as tmpfile:
                    data, rate = soundfile.read(tmpfile)
                meter = pyloudnorm.Meter(rate) # create BS.1770 meter
                loudness = meter.integrated_loudness(data) # measure loudness
                del data
                LOGGER.debug("Integrated loudness of track %s is: %s" %(item_key, loudness))
                await self.mass.db.set_track_loudness(queue_item.item_id, queue_item.provider, loudness)
            del audio_data
            LOGGER.debug('Finished analyzing track %s' % item_key)
        self.analyze_jobs.pop(item_key, None)
    
    def __crossfade_pcm_parts(self, fade_in_part, fade_out_part, pcm_args, fade_length):
        ''' crossfade two chunks of audio using sox '''
        # create fade-in part
        fadeinfile = MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
        args = 'sox --ignore-length -t %s - -t %s %s fade t %s' % (pcm_args, pcm_args, fadeinfile.name, fade_length)
        process = subprocess.Popen(args, shell=True, stdin=subprocess.PIPE)
        process.communicate(fade_in_part)
        # create fade-out part
        fadeoutfile = MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
        args = 'sox --ignore-length -t %s - -t %s %s reverse fade t %s reverse' % (pcm_args, pcm_args, fadeoutfile.name, fade_length)
        process = subprocess.Popen(args, shell=True,
                stdout=subprocess.PIPE, stdin=subprocess.PIPE)
        process.communicate(fade_out_part)
        # create crossfade using sox and some temp files
        # TODO: figure out how to make this less complex and without the tempfiles
        args = 'sox -m -v 1.0 -t %s %s -v 1.0 -t %s %s -t %s -' % (pcm_args, fadeoutfile.name, pcm_args, fadeinfile.name, pcm_args)
        process = subprocess.Popen(args, shell=True,
                stdout=subprocess.PIPE, stdin=subprocess.PIPE)
        crossfade_part, stderr = process.communicate()
        LOGGER.debug("Got %s bytes in memory for crossfade_part after sox" % len(crossfade_part))
        return crossfade_part

    # def readexactly(streamobj, chunksize):
    #     ''' read exactly n bytes from the stream object '''
    #     buf = b''
    #     while len(buf) < chunksize:
    #         new_data = streamobj.read(chunksize)
