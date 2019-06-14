#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import LOGGER, try_parse_int, get_ip, run_async_background_task, run_periodic, get_folder_size
from models import TrackQuality, MediaType, PlayerState
import shutil
import xml.etree.ElementTree as ET
import random
import base64
import operator
from aiohttp import web
import threading
import urllib
import math
from memory_tempfile import MemoryTempfile
import tempfile
import io
import soundfile as sf
import pyloudnorm as pyln
import aiohttp

AUDIO_TEMP_DIR = "/tmp/audio_tmp"
AUDIO_CACHE_DIR = "/tmp/audio_cache"

class HTTPStreamer():
    ''' Built-in streamer using sox and webserver '''
    
    def __init__(self, mass):
        self.mass = mass
        self.create_config_entries()
        self.local_ip = get_ip()
        self.analyze_jobs = {}
        self._audio_cache_dir = self.mass.config['base']['http_streamer']['audio_cache_folder']
        # create needed temp/cache dirs
        if self.mass.config['base']['http_streamer']['enable_cache'] and not os.path.isdir(self._audio_cache_dir):
            self._audio_cache_dir = self.mass.config['base']['http_streamer']['audio_cache_folder']
            os.makedirs(self._audio_cache_dir)
        if not os.path.isdir(AUDIO_TEMP_DIR):
            os.makedirs(AUDIO_TEMP_DIR)
        mass.event_loop.create_task(self.__cache_cleanup())

    def create_config_entries(self):
        ''' sets the config entries for this module (list with key/value pairs)'''
        config_entries = [
            ('volume_normalisation', True, 'enable_r128_volume_normalisation'), 
            ('target_volume', '-23', 'target_volume_lufs'),
            ('fallback_gain_correct', '-12', 'fallback_gain_correct'),
            ('enable_cache', True, 'enable_audio_cache'),
            ('trim_silence', True, 'trim_silence'),
            ('audio_cache_folder', '/tmp/audio_cache', 'audio_cache_folder'),
            ('audio_cache_max_size_gb', 20, 'audio_cache_max_size_gb')
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
                audio_stream = self.__get_audio_stream(track_id, provider, player_id)
                async for is_last_chunk, audio_chunk in audio_stream:
                    if not cancelled.is_set():
                        await queue.put(audio_chunk)
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
                args = 'ffmpeg -i "%s" -f flac - | sox -t flac - -t flac -C 0 - %s' % (stream_url, sox_effects)
            else:
                args = 'sox -t %s "%s" -t flac -C 0 - %s' % (input_content_type, stream_url, sox_effects)
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
        LOGGER.info("Start Queue Stream for player %s at index %s" %(player_id, queue_index))
        last_fadeout_data = b''
        # report start of queue playback so we can calculate current track/duration etc.
        self.mass.event_loop.create_task(self.mass.player.player_queue_stream_update(player_id, queue_index, True))
        while True:
            # get the (next) track in queue
            try:
                queue_tracks = await self.mass.player.player_queue(player_id, queue_index, queue_index+1)
                queue_track = queue_tracks[0]
            except IndexError:
                LOGGER.info("queue index out of range or end reached")
                break

            params = urllib.parse.parse_qs(queue_track.uri.split('?')[1])
            track_id = params['track_id'][0]
            provider = params['provider'][0]
            LOGGER.info("Start Streaming queue track: %s - %s" % (track_id, queue_track.name))
            fade_in_part = b''
            cur_chunk = 0
            prev_chunk = None
            bytes_written = 0
            async for is_last_chunk, chunk in self.__get_audio_stream(
                    track_id, provider, player_id, chunksize=fade_bytes, outputfmt=pcm_args, 
                    sox_effects='rate -v %s' % sample_rate ):
                cur_chunk += 1
                if cur_chunk == 1 and not last_fadeout_data:
                    # fade-in part but this is the first track so just pass it to the final file
                    sox_proc.stdin.write(chunk)
                    await sox_proc.stdin.drain()
                    bytes_written += len(chunk)
                elif cur_chunk == 1 and last_fadeout_data:
                    # create fade-out part
                    args = 'sox --ignore-length -t %s - -t %s - reverse fade t %s reverse' % (pcm_args, pcm_args, fade_length)
                    process = await asyncio.create_subprocess_shell(args,
                            stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
                    last_fadeout_data, stderr = await process.communicate(last_fadeout_data)
                    LOGGER.debug("Got %s bytes in memory for fade_out_part after sox" % len(last_fadeout_data))
                    # create fade-in part
                    args = 'sox --ignore-length -t %s - -t %s - fade t %s' % (pcm_args, pcm_args, fade_length)
                    process = await asyncio.create_subprocess_shell(args,
                            stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
                    fade_in_part, stderr = await process.communicate(chunk)
                    LOGGER.debug("Got %s bytes in memory for fadein_part after sox" % len(fade_in_part))
                    # create crossfade using sox and some temp files
                    # TODO: figure out how to make this less complex and without the tempfiles
                    fadeinfile = MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
                    fadeinfile.write(fade_in_part)
                    fadeoutfile = MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
                    fadeoutfile.write(last_fadeout_data)
                    args = 'sox -m -v 1.0 -t %s %s -v 1.0 -t %s %s -t %s -' % (pcm_args, fadeoutfile.name, pcm_args, fadeinfile.name, pcm_args)
                    process = await asyncio.create_subprocess_shell(args,
                            stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
                    crossfade_part, stderr = await process.communicate(fade_in_part)
                    LOGGER.debug("Got %s bytes in memory for crossfade_part after sox" % len(crossfade_part))
                    # write the crossfade part to the sox player
                    sox_proc.stdin.write(crossfade_part)
                    await sox_proc.stdin.drain()
                    bytes_written += len(crossfade_part)
                    fadeinfile.close()
                    fadeoutfile.close()
                    del crossfade_part
                    fade_in_part = None
                    last_fadeout_data = b''
                elif prev_chunk and is_last_chunk:
                    # last chunk received so create the fadeout with the previous chunk and this chunk
                    last_part = prev_chunk + chunk
                    last_fadeout_data = last_part[-fade_bytes:]
                    bytes_remaining = last_part[:-fade_bytes]
                    sox_proc.stdin.write(bytes_remaining)
                    bytes_written += len(bytes_remaining)
                    await sox_proc.stdin.drain()
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
                # pre-analyse the next track - to ensure smooth transitions
                try:
                    queue_tracks = await self.mass.player.player_queue(player_id, queue_index+1, queue_index+2)
                    queue_track = queue_tracks[0]
                    params = urllib.parse.parse_qs(queue_track.uri.split('?')[1])
                    track_id = params['track_id'][0]
                    provider = params['provider'][0]
                    self.mass.event_loop.create_task(self.__analyze_audio(track_id, provider))
                except:
                    pass
                # wait for the queue to consume the data
                # this prevents that the entire track is sitting in memory
                # and it helps a bit in the quest to follow where we are in the queue
                while buffer.qsize() > 2 and not cancelled.is_set():
                    await asyncio.sleep(1)
            # end of the track reached
            # WIP: update actual duration to the queue for more accurate now playing info
            accurate_duration = bytes_written / int(sample_rate * 4 * 2)
            queue_track.duration = accurate_duration
            self.mass.player.providers[player.player_provider]._player_queue[player_id][queue_index] = queue_track
            # move to next queue index
            queue_index += 1
            self.mass.event_loop.create_task(self.mass.player.player_queue_stream_update(player_id, queue_index, False))
            LOGGER.info("Finished Streaming queue track: %s - %s" % (track_id, queue_track.name))
            # break out the loop if the http session is cancelled
            if cancelled.is_set():
                break
        
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_data:
            sox_proc.stdin.write(last_fadeout_data)
            await sox_proc.stdin.drain()
        sox_proc.stdin.close()
        await sox_proc.wait()
        LOGGER.info("streaming of queue for player %s completed" % player_id)

    async def __get_audio_stream(self, track_id, provider, player_id,
                chunksize=512000, outputfmt='flac -C 0', sox_effects=''):
        ''' get audio stream from provider and apply additional effects/processing where/if needed'''
        if self.mass.config['base']['http_streamer']['volume_normalisation']:
            gain_correct = await self.__get_track_gain_correct(track_id, provider)
            gain_correct = 'vol %s dB ' % gain_correct
        else:
            gain_correct = ''
        sox_effects += await self.__get_player_sox_options(track_id, provider, player_id, False)

        cachefile = self.__get_track_cache_filename(track_id, provider)
        if os.path.isfile(cachefile):
            # stream from cachefile
            args = 'sox -t sox "%s" -t %s - %s %s' % (cachefile, outputfmt, gain_correct, sox_effects)
        else:
            # stream directly from provider
            streamdetails = asyncio.run_coroutine_threadsafe(
                    self.mass.music.providers[provider].get_stream_details(track_id), self.mass.event_loop).result()
            if not streamdetails:
                yield b''
                return
            if streamdetails['type'] == 'url':
                args = 'sox -t %s "%s" -t %s - %s %s' % (streamdetails["content_type"], streamdetails["path"], outputfmt, gain_correct, sox_effects)
            elif streamdetails['type'] == 'executable':
                args = '%s | sox -t %s - -t %s - %s %s' % (streamdetails["path"], streamdetails["content_type"], outputfmt, gain_correct, sox_effects)
        LOGGER.debug("Running sox with args: %s" % args)
        process = await asyncio.create_subprocess_shell(args,
                stdout=asyncio.subprocess.PIPE)
        # yield chunks from stdout
        # we keep 1 chunk behind to detect end of stream properly
        prev_chunk = b''
        while not process.stdout.at_eof():
            try:
                chunk = await process.stdout.readexactly(chunksize)
            except asyncio.streams.IncompleteReadError:
                chunk = await process.stdout.read(chunksize)
            if prev_chunk:
                yield (False, prev_chunk)
            prev_chunk = chunk
        # yield last chunk
        yield (True, prev_chunk)
        await process.wait()
        LOGGER.info("__get_audio_stream for track_id %s completed" % track_id)
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
            return # prevent multiple analyze jobs for same tracks
        self.analyze_jobs[track_key] = True
        streamdetails = await self.mass.music.providers[provider].get_stream_details(track_id)
        cachefile = self.__get_track_cache_filename(track_id, provider)
        enable_cache = self.mass.config['base']['http_streamer']['enable_cache']
        needs_cachefile = enable_cache and not os.path.isfile(cachefile)
        track_loudness = await self.mass.db.get_track_loudness(track_id, provider)
        if needs_cachefile or track_loudness == None:
            # only when needed we do the analyze stuff
            LOGGER.info('Start analyzing track %s' % track_id)
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
            if needs_cachefile:
                # use sox to store cache file (strip silence from start and end for better transitions)
                cmd = 'sox -t %s - -t sox %s silence 1 0.1 1%% reverse silence 1 0.1 1%% reverse' %(streamdetails['content_type'], cachefile)
                process = await asyncio.create_subprocess_shell(cmd, stdin=asyncio.subprocess.PIPE)
                await process.communicate(audio_data)
            del audio_data
            LOGGER.info('Finished analyzing track %s' % track_id)
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

    async def __fill_audio_buffer(self, buf, track_id, provider, content_type):
        ''' get audio data from provider and write to buffer'''
        # fill the buffer with audio data
        # a tempfile is created so we can do audio analysis
        try:
            tmpfile = os.path.join(AUDIO_TEMP_DIR, '%s%s%s.tmp' % (random.randint(0, 999), track_id, random.randint(0, 999)))
            fd = open(tmpfile, 'wb')
            async for chunk in self.mass.music.providers[provider].get_audio_stream(track_id):
                if not chunk:
                    break
                buf.write(chunk)
                await buf.drain()
                fd.write(chunk)
            LOGGER.info("fill_audio_buffer complete for track %s" % track_id)
            # successfull completion, process temp file for analysis
            self.mass.event_loop.create_task(
                    self.__analyze_audio(tmpfile, track_id, provider, content_type))
        except Exception as exc:
            LOGGER.exception("fill_audio_buffer failed for track %s" % track_id)
        finally:
            buf.write_eof()
            await buf.drain()
            fd.close()

    def __get_track_cache_filename(self, track_id, provider):
        ''' get filename for a track to use as cache file '''
        filename = '%s%s' %(provider, track_id.split(os.sep)[-1])
        filename = base64.b64encode(filename.encode()).decode()
        return os.path.join(self._audio_cache_dir, filename)

    @run_periodic(3600)
    async def __cache_cleanup(self):
        ''' calculate size of cache folder and cleanup if needed '''
        def cleanup():
            size_limit = self.mass.config['base']['http_streamer']['audio_cache_max_size_gb']
            total_size_gb = get_folder_size(self._audio_cache_dir)
            LOGGER.info("current size of cache folder is %s GB" % total_size_gb)
            if size_limit and total_size_gb > size_limit:
                LOGGER.info("Cache folder size exceeds threshold, start cleanup...")
                from pathlib import Path
                import time
                days = 14
                while total_size_gb > size_limit:
                    time_in_secs = time.time() - (days * 24 * 60 * 60)
                    for i in Path(self._audio_cache_dir).iterdir():
                        if i.is_file():
                            if i.stat().st_atime <= time_in_secs:
                                total_size_gb -= i.stat().st_size/float(1<<30)
                                i.unlink()
                        if total_size_gb < size_limit:
                            break
                    days -= 1
                LOGGER.info("Cache folder size cleanup completed")
        await self.mass.event_loop.run_in_executor(None, cleanup)

    @staticmethod
    def __get_bs1770_binary():
        ''' get the path to the bs1770 binary for the current OS '''
        import platform
        bs1770_binary = None
        if platform.system() == "Windows":
            bs1770_binary = os.path.join(os.path.dirname(__file__), "bs1770gain", "win64", "bs1770gain")
        elif platform.system() == "Darwin":
            # macos binary is x86_64 intel
            bs1770_binary = os.path.join(os.path.dirname(__file__), "bs1770gain", "osx", "bs1770gain")
        elif platform.system() == "Linux":
            architecture = platform.machine()
            if architecture.startswith('AMD64') or architecture.startswith('x86_64'):
                bs1770_binary = os.path.join(os.path.dirname(__file__), "bs1770gain", "linux64", "bs1770gain")
            # TODO: build armhf binary
        return bs1770_binary