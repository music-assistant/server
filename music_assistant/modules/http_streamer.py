#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import LOGGER, try_parse_int, get_ip, run_async_background_task, run_periodic, get_folder_size
from models import TrackQuality, MediaType
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

AUDIO_TEMP_DIR = "/tmp/audio_tmp"
AUDIO_CACHE_DIR = "/tmp/audio_cache"

class HTTPStreamer():
    ''' Built-in streamer using sox and webserver '''
    
    def __init__(self, mass):
        self.mass = mass
        self.create_config_entries()
        self.local_ip = get_ip()
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
            queue = asyncio.Queue()
            cancelled = threading.Event()
            task = run_async_background_task(
                self.mass.bg_executor, 
                self.__get_audio_stream, queue, track_id, provider, player_id, cancelled)
            try:
                while True:
                    chunk = await queue.get()
                    if not chunk:
                        queue.task_done()
                        break
                    await resp.write(chunk)
                    queue.task_done()
                LOGGER.info("stream_track fininished for %s" % track_id)
            except asyncio.CancelledError:
                cancelled.set()
                LOGGER.info("stream_track interrupted for %s" % track_id)
                raise asyncio.CancelledError()
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
            streamm all tracks in queue from player with http
            loads audiodata in memory so only recommended for high performance servers
            use case is enable crossfade support for chromecast devices 
        '''
        player_id = http_request.query.get('player_id')
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
                self.__stream_queue, player_id, queue, cancelled)
            try:
                while True:
                    chunk = await queue.get()
                    await resp.write(chunk)
                    queue.task_done()
                    if not chunk:
                        break
                LOGGER.info("stream_queue fininished for %s" % player_id)
            except asyncio.CancelledError:
                cancelled.set()
                LOGGER.info("stream_queue interrupted for %s" % player_id)
                raise asyncio.CancelledError()
        return resp

    async def __stream_queue(self, player_id, buffer, cancelled):
        ''' start streaming all queue tracks '''
        # TODO: get correct queue index and implement reporting of position
        sample_rate = self.mass.config['player_settings'][player_id]['max_sample_rate']
        fade_length = self.mass.config['player_settings'][player_id]["crossfade_duration"]
        pcm_args = 'raw -b 32 -c 2 -e signed-integer -r %s' % sample_rate
        args = 'sox -t %s - -t flac -C 2 -' % pcm_args
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

        last_fadeout_data = None
        while True:
            # get current track in queue
            queue_tracks = await self.mass.player.player_queue(player_id, 0, 10000)
            player = self.mass.player._players[player_id]
            queue_index = player.cur_queue_index
            try:
                queue_track = queue_tracks[queue_index]
            except IndexError:
                LOGGER.info("queue index out of range or end reached")
                break

            params = urllib.parse.parse_qs(queue_track.uri.split('?')[1])
            track_id = params['track_id'][0]
            provider = params['provider'][0]
            LOGGER.info("Stream queue track: %s - %s" % (track_id, queue_track.name))
            audiodata = await self.__get_raw_audio(track_id, provider, sample_rate)
            fade_bytes = int(sample_rate * 4 * 2 * fade_length)
            LOGGER.debug("total bytes in audio_data: %s - fade_bytes: %s" % (len(audiodata),fade_bytes))
            
            # get fade in part
            args = 'sox --ignore-length -t %s - -t %s - fade t %s' % (pcm_args, pcm_args, fade_length)
            process = await asyncio.create_subprocess_shell(args,
                    stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
            fade_in_part, stderr = await process.communicate(audiodata[:fade_bytes])
            LOGGER.debug("Got %s bytes in memory for fadein_part after sox" % len(fade_in_part))
            if last_fadeout_data:
                # perform crossfade with previous fadeout samples
                fadeinfile = MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
                fadeinfile.write(fade_in_part)
                fadeoutfile = MemoryTempfile(fallback=True).NamedTemporaryFile(buffering=0)
                fadeoutfile.write(last_fadeout_data)
                args = 'sox -m -v 1.0 -t %s %s -v 1.0 -t %s %s -t %s -' % (pcm_args, fadeoutfile.name, pcm_args, fadeinfile.name, pcm_args)
                process = await asyncio.create_subprocess_shell(args,
                        stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
                crossfade_part, stderr = await process.communicate(fade_in_part)
                LOGGER.debug("Got %s bytes in memory for crossfade_part after sox" % len(crossfade_part))
                sox_proc.stdin.write(crossfade_part)
                await sox_proc.stdin.drain()
                fadeinfile.close()
                fadeoutfile.close()
                del crossfade_part
                del fade_in_part
                last_fadeout_data = None
            else:
                # simply put the fadein part in the final file
                sox_proc.stdin.write(fade_in_part)
                await sox_proc.stdin.drain()
                del fade_in_part

            # feed the middle part into the main sox
            sox_proc.stdin.write(audiodata[fade_bytes:-fade_bytes])
            await sox_proc.stdin.drain()

            # get fade out part
            args = 'sox --ignore-length -t %s - -t %s - reverse fade t %s reverse' % (pcm_args, pcm_args, fade_length)
            process = await asyncio.create_subprocess_shell(args,
                    stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
            last_fadeout_data, stderr = await process.communicate(audiodata[-fade_bytes:])
            LOGGER.debug("Got %s bytes in memory for fade_out_part after sox" % len(last_fadeout_data))
            # cleanup audio data
            del audiodata

            # wait for the queue to consume the data
            while buffer.qsize() > 5 and not cancelled.is_set():
                await asyncio.sleep(1)
            if cancelled.is_set():
                break
            # assume end of track and increase queue_index
            player.cur_queue_index += 1
            await self.mass.player.trigger_update(player_id)
        
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_data:
            sox_proc.stdin.write(last_fadeout_data)
            await sox_proc.stdin.drain()
        sox_proc.stdin.close()
        await sox_proc.wait()
        LOGGER.info("streaming of queue for player %s completed" % player_id)

    async def __get_raw_audio(self, track_id, provider, sample_rate=96000):
        ''' get raw pcm data for a track upsampled to given sample_rate packed as wav '''
        audiodata = b''
        cachefile = self.__get_track_cache_filename(track_id, provider)
        pcm_args = 'raw -b 32 -c 2 -e signed-integer'
        if self.mass.config['base']['http_streamer']['volume_normalisation']:
            gain_correct = await self.__get_track_gain_correct(track_id, provider)
        else:
            gain_correct = -6 # always need some headroom for upsampling and crossfades
        if os.path.isfile(cachefile):
            # we have a cache file for this track which we can use
            args = 'sox -t flac "%s" -t %s - vol %s dB rate -v %s' % (cachefile, pcm_args, gain_correct, sample_rate)
            process = await asyncio.create_subprocess_shell(args, stdout=asyncio.subprocess.PIPE)
        else:
            # stream from provider
            input_content_type = await self.mass.music.providers[provider].get_stream_content_type(track_id)
            assert(input_content_type)
            args = 'sox -t %s - -t %s - vol %s dB rate -v %s' % (input_content_type, pcm_args, gain_correct, sample_rate)
            process = await asyncio.create_subprocess_shell(args,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            asyncio.get_event_loop().create_task(
                     self.__fill_audio_buffer(process.stdin, track_id, provider, input_content_type))
        #await process.wait()
        audiodata, stderr = await process.communicate()
        LOGGER.debug("__get_raw_audio for track_id %s completed" % (track_id))
        return audiodata
    
    async def __get_audio_stream(self, audioqueue, track_id, provider, player_id=None, cancelled=None):
        ''' get audio stream from provider and apply additional effects/processing where/if needed'''
        cachefile = self.__get_track_cache_filename(track_id, provider)
        sox_effects = await self.__get_player_sox_options(track_id, provider, player_id, False)
        if self.mass.config['base']['http_streamer']['volume_normalisation']:
            gain_correct = await self.__get_track_gain_correct(track_id, provider)
            sox_effects += ' vol %s dB ' % gain_correct
        if os.path.isfile(cachefile):
            # we have a cache file for this track which we can use
            if sox_effects.strip():
                args = 'sox -t flac "%s" -t flac -C 0 - %s' % (cachefile, sox_effects)
            else:
                args = 'sox -t flac "%s" -t flac -C 0 - %s' % cachefile
            LOGGER.debug("Running sox with args: %s" % args)
            process = await asyncio.create_subprocess_shell(args, 
                    stdout=asyncio.subprocess.PIPE)
            buffer_task = None
        else:
            # stream from provider
            input_content_type = await self.mass.music.providers[provider].get_stream_content_type(track_id)
            assert(input_content_type)
            if sox_effects.strip():
                args = 'sox -t %s - -t flac -C 0 - %s' % (input_content_type, sox_effects)
            else:
                args = 'sox -t %s - -t flac -C 0 -' % (input_content_type)
            LOGGER.debug("Running sox with args: %s" % args)
            process = await asyncio.create_subprocess_shell(args,
                    stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
            buffer_task = asyncio.get_event_loop().create_task(
                     self.__fill_audio_buffer(process.stdin, track_id, provider, input_content_type))
        # put chunks from stdout into queue
        while not process.stdout.at_eof():
            chunk = await process.stdout.read(705600)
            if not chunk:
                break
            if not cancelled.is_set():
                await audioqueue.put(chunk)
                if audioqueue.qsize() > 10:
                    await asyncio.sleep(0.1) # cooldown a bit
        await process.wait()
        await audioqueue.put('') # indicate EOF
        if cancelled.is_set():
            LOGGER.warning("__get_audio_stream for track_id %s interrupted" % track_id)
        else:
            LOGGER.debug("__get_audio_stream for track_id %s completed" % track_id)

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
        
    async def __analyze_audio(self, tmpfile, track_id, provider, content_type):
        ''' analyze track audio, for now we only calculate EBU R128 loudness '''
        LOGGER.info('Start analyzing file %s' % tmpfile)
        cachefile = self.__get_track_cache_filename(track_id, provider)
        # not needed to do processing if there already is a cachedfile
        bs1770_binary = self.__get_bs1770_binary()
        if bs1770_binary:
            # calculate integrated r128 loudness with bs1770
            analyse_dir = os.path.join(self.mass.datapath, 'analyse_info')
            analysis_file = os.path.join(analyse_dir, "%s_%s.xml" %(provider, track_id.split(os.sep)[-1]))
            if not os.path.isfile(analysis_file):
                if not os.path.isdir(analyse_dir):
                    os.makedirs(analyse_dir)
                cmd = '%s %s --xml --ebu -f %s' % (bs1770_binary, tmpfile, analysis_file)
                process = await asyncio.create_subprocess_shell(cmd)
                await process.wait()
            if self.mass.config['base']['http_streamer']['enable_cache'] and not os.path.isfile(cachefile):
                # use sox to store cache file (strip silence from start and end for better transitions)
                cmd = 'sox -t %s %s -t flac -C5 %s silence 1 0.1 1%% reverse silence 1 0.1 1%% reverse' %(content_type, tmpfile, cachefile)
                process = await asyncio.create_subprocess_shell(cmd)
                await process.wait()
        # always clean up temp file
        while os.path.isfile(tmpfile):
            os.remove(tmpfile)
            await asyncio.sleep(0.5)
        LOGGER.info('Fininished analyzing file %s' % tmpfile)
    
    async def __get_track_gain_correct(self, track_id, provider):
        ''' get the gain correction that should be applied to a track '''
        target_gain = int(self.mass.config['base']['http_streamer']['target_volume'])
        fallback_gain = int(self.mass.config['base']['http_streamer']['fallback_gain_correct'])
        analysis_file = os.path.join(self.mass.datapath, 'analyse_info', "%s_%s.xml" %(provider, track_id.split(os.sep)[-1]))
        if not os.path.isfile(analysis_file):
            return fallback_gain
        try: # read audio analysis if available
            tree = ET.parse(analysis_file)
            trackinfo = tree.getroot().find("album").find("track")
            track_lufs = trackinfo.find('integrated').get('lufs')
            gain_correct = target_gain - float(track_lufs)
        except Exception as exc:
            LOGGER.error('could not retrieve track gain - %s' % exc)
            gain_correct = fallback_gain # fallback value
            if os.path.isfile(analysis_file):
                os.remove(analysis_file)
                # reschedule analyze task to try again
                cachefile = self.__get_track_cache_filename(track_id, provider)
                self.mass.event_loop.create_task(self.__analyze_audio(cachefile, track_id, provider, 'flac'))
        return round(gain_correct,2)

    async def __fill_audio_buffer(self, buf, track_id, provider, content_type):
        ''' get audio data from provider and write to buffer'''
        # fill the buffer with audio data
        # a tempfile is created so we can do audio analysis
        tmpfile = os.path.join(AUDIO_TEMP_DIR, '%s%s%s.tmp' % (random.randint(0, 999), track_id, random.randint(0, 999)))
        fd = open(tmpfile, 'wb')
        async for chunk in self.mass.music.providers[provider].get_audio_stream(track_id):
            buf.write(chunk)
            await buf.drain()
            fd.write(chunk)
        await buf.drain()
        buf.write_eof()
        fd.close()
        LOGGER.info("fill_audio_buffer complete for track %s" % track_id)
        # successfull completion, process temp file for analysis
        self.mass.event_loop.create_task(
                self.__analyze_audio(tmpfile, track_id, provider, content_type))
        return

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