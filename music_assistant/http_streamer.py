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

from .constants import EVENT_STREAM_STARTED, EVENT_STREAM_ENDED
from .utils import LOGGER, try_parse_int, get_ip, run_async_background_task, run_periodic, get_folder_size
from .models.media_types import TrackQuality, MediaType
from .models.playerstate import PlayerState

class HTTPStreamer():
    ''' Built-in streamer using sox and webserver '''
    
    def __init__(self, mass):
        self.mass = mass
        self.local_ip = get_ip()
        self.analyze_jobs = {}
        self.stream_clients = []

    async def setup(self):
        ''' async initialize of module '''
        pass
        # self.mass.event_loop.create_task(
        #         asyncio.start_server(self.sockets_streamer, '0.0.0.0', 8093))
        
    async def stream(self, http_request):
        ''' 
            start stream for a player
        '''
        # make sure we have a valid player
        player_id = http_request.match_info.get('player_id','')
        player = await self.mass.players.get_player(player_id)
        assert(player)
        # prepare headers as audio/flac content
        resp = web.StreamResponse(status=200, reason='OK', 
                headers={
                    'Content-Type': 'audio/flac'
                    })
        await resp.prepare(http_request)
        # send content only on GET request
        if http_request.method.upper() != 'GET':
            return resp
        # stream audio
        cancelled = threading.Event()
        if player.queue.use_queue_stream:
            # use queue stream
            bg_task = run_async_background_task(
                None, 
                self.__stream_queue, player, resp, cancelled)
        else:
            # single track stream
            queue_item_id = http_request.match_info.get('queue_item_id')
            queue_item = await player.queue.by_item_id(queue_item_id)
            assert(queue_item)
            bg_task = run_async_background_task(
                None, 
                self.__stream_single, player, queue_item, resp, cancelled)
        # let the streaming begin!
        try:
            await asyncio.gather(bg_task)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            LOGGER.debug("stream request cancelled")
            cancelled.set()
            # wait for bg_task to finish
            await asyncio.gather(bg_task)
            raise asyncio.CancelledError()
        return resp
    
    async def __stream_single(self, player, queue_item, buffer, cancelled):
        ''' start streaming single queue track '''
        LOGGER.debug("stream single queue track started for track %s on player %s" % (queue_item.name, player.name))
        audio_stream = self.__get_audio_stream(player, queue_item, cancelled)
        async for is_last_chunk, audio_chunk in audio_stream:
            if cancelled.is_set():
                # http session ended
                # we must consume the data to prevent hanging subprocess instances
                continue
            # put chunk in buffer
            asyncio.run_coroutine_threadsafe(
                    buffer.write(audio_chunk), 
                    self.mass.event_loop)
            # this should be garbage collected but just in case...
            del audio_chunk
            # wait for the queue to consume the data
            if not cancelled.is_set():
                await asyncio.sleep(0.5)
        # all chunks received: streaming finished
        if cancelled.is_set():
            LOGGER.debug("stream single track interrupted for track %s on player %s" % (queue_item.name, player.name))
        else:
            # indicate EOF if no more data
            asyncio.run_coroutine_threadsafe(
                    buffer.write_eof(), 
                    self.mass.event_loop)
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
            fade_length = 1
            fade_bytes = int(sample_rate * 4 * 2)
        pcm_args = 'raw -b 32 -c 2 -e signed-integer -r %s' % sample_rate
        args = 'sox -t %s - -t flac -C 0 -' % pcm_args
        # start sox process
        # we use normal subprocess instead of asyncio because of bug with executor
        # this should be fixed with python 3.8
        sox_proc = subprocess.Popen(args, shell=True, 
            stdout=subprocess.PIPE, stdin=subprocess.PIPE)

        def fill_buffer():
            chunk_size = int(sample_rate * 4 * 2)
            while True:
                chunk = sox_proc.stdout.read(chunk_size)
                if not chunk:
                    break
                if chunk and not cancelled.is_set():
                    asyncio.run_coroutine_threadsafe(
                        buffer.write(chunk), self.mass.event_loop)
                del chunk
            # indicate EOF if no more data
            if not cancelled.is_set():
                asyncio.run_coroutine_threadsafe(
                        buffer.write_eof(),  self.mass.event_loop)
            LOGGER.debug("stream queue player %s: fill buffer completed" % player.name)
        # start fill buffer task in background
        fill_buffer_thread = threading.Thread(target=fill_buffer)
        fill_buffer_thread.start()
        
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
                    player, queue_track, cancelled, chunksize=fade_bytes, 
                    resample=sample_rate):
                cur_chunk += 1

                ### HANDLE FIRST PART OF TRACK
                if cur_chunk <= 2 and not last_fadeout_data:
                    # no fadeout_part available so just pass it to the output directly
                    sox_proc.stdin.write(chunk)
                    bytes_written += len(chunk)
                    del chunk
                elif cur_chunk == 1 and last_fadeout_data:
                    prev_chunk = chunk
                    del chunk
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
                    del chunk
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
                        del chunk
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
                            del chunk
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
                    del chunk
                    ### throttle to prevent entire track sitting in memory
                    if not cancelled.is_set():
                        await asyncio.sleep(fade_length)
            # end of the track reached
            if cancelled.is_set():
                # break out the loop if the http session is cancelled
                break
            else:
                # update actual duration to the queue for more accurate now playing info
                accurate_duration = bytes_written / int(sample_rate * 4 * 2)
                queue_track.duration = accurate_duration
                LOGGER.debug("Finished Streaming queue track: %s (%s) on player %s" % (queue_track.item_id, queue_track.name, player.name))
                LOGGER.debug("bytes written: %s - duration: %s" % (bytes_written, accurate_duration))
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_data and not cancelled.is_set():
            sox_proc.stdin.write(last_fadeout_data)
            del last_fadeout_data
        ### END OF QUEUE STREAM
        sox_proc.stdin.close()
        sox_proc.terminate()
        fill_buffer_thread.join()
        del sox_proc
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
                streamdetails['player_id'] = player.player_id
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
        sox_options = await self.__get_player_sox_options(player, queue_item)
        outputfmt = 'flac -C 0'
        if resample:
            outputfmt = 'raw -b 32 -c 2 -e signed-integer'
            sox_options += ' rate -v %s' % resample
        streamdetails['sox_options'] = sox_options
        # determine how to proceed based on input file ype
        if streamdetails["content_type"] == 'aac':
            # support for AAC created with ffmpeg in between
            args = 'ffmpeg -v quiet -i "%s" -f flac - | sox -t flac - -t %s - %s' % (streamdetails["path"], outputfmt, sox_options)
        elif streamdetails['type'] == 'url':
            args = 'sox -t %s "%s" -t %s - %s' % (streamdetails["content_type"], 
                    streamdetails["path"], outputfmt, sox_options)
        elif streamdetails['type'] == 'executable':
            args = '%s | sox -t %s - -t %s - %s' % (streamdetails["path"], 
                    streamdetails["content_type"], outputfmt, sox_options)
        # start sox process
        # we use normal subprocess instead of asyncio because of bug with executor
        # this should be fixed with python 3.8
        process = subprocess.Popen(args, shell=True, stdout=subprocess.PIPE)
        # fire event that streaming has started for this track
        asyncio.run_coroutine_threadsafe(
                self.mass.signal_event(EVENT_STREAM_STARTED, queue_item), self.mass.event_loop)
        # yield chunks from stdout
        # we keep 1 chunk behind to detect end of stream properly
        bytes_sent = 0
        while True:
            if cancelled.is_set():
                # http session ended
                # send terminate and pick up left over bytes
                process.terminate()
            # read exactly chunksize of data
            chunk = process.stdout.read(chunksize)
            if len(chunk) < chunksize:
                # last chunk
                LOGGER.debug("last chunk received")
                bytes_sent += len(chunk)
                yield (True, chunk)
                break
            else:
                bytes_sent += len(chunk)
                yield (False, chunk)

        # fire event that streaming has ended
        asyncio.run_coroutine_threadsafe(
                self.mass.signal_event(EVENT_STREAM_ENDED, queue_item), self.mass.event_loop)
        # send task to main event loop to analyse the audio
        self.mass.event_loop.call_soon_threadsafe(
                asyncio.ensure_future, self.__analyze_audio(queue_item))

    async def __get_player_sox_options(self, player, queue_item):
        ''' get player specific sox effect options '''
        sox_options = []
        # volume normalisation
        gain_correct = asyncio.run_coroutine_threadsafe(
                self.mass.players.get_gain_correct(
                    player.player_id, queue_item.item_id, queue_item.provider), 
                self.mass.event_loop).result()
        if gain_correct != 0:
            sox_options.append('vol %s dB ' % gain_correct)
        # downsample if needed
        if player.settings['max_sample_rate']:
            max_sample_rate = try_parse_int(player.settings['max_sample_rate'])
            if max_sample_rate:
                quality = queue_item.quality
                if quality > TrackQuality.FLAC_LOSSLESS_HI_RES_3 and max_sample_rate == 192000:
                    sox_options.append('rate -v 192000')
                elif quality > TrackQuality.FLAC_LOSSLESS_HI_RES_2 and max_sample_rate == 96000:
                    sox_options.append('rate -v 96000')
                elif quality > TrackQuality.FLAC_LOSSLESS_HI_RES_1 and max_sample_rate == 48000:
                    sox_options.append('rate -v 48000')
        if player.settings.get('sox_options'):
            sox_options.append(player.settings['sox_options'])
        return " ".join(sox_options)
        
    async def __analyze_audio(self, queue_item):
        ''' analyze track audio, for now we only calculate EBU R128 loudness '''
        if queue_item.media_type != MediaType.Track:
            # TODO: calculate loudness average for web radio ?
            LOGGER.debug("analyze is only supported for tracks")
            return
        item_key = '%s%s' %(queue_item.item_id, queue_item.provider)
        if item_key in self.analyze_jobs:
            return # prevent multiple analyze jobs for same track
        self.analyze_jobs[item_key] = True
        streamdetails = queue_item.streamdetails
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
                audio_data = subprocess.check_output(streamdetails["path"], shell=True)
            # calculate BS.1770 R128 integrated loudness
            with io.BytesIO(audio_data) as tmpfile:
                data, rate = soundfile.read(tmpfile)
            meter = pyloudnorm.Meter(rate) # create BS.1770 meter
            loudness = meter.integrated_loudness(data) # measure loudness
            del data
            await self.mass.db.set_track_loudness(queue_item.item_id, queue_item.provider, loudness)
            del audio_data
            LOGGER.debug("Integrated loudness of track %s is: %s" %(item_key, loudness))
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
        fadeinfile.close()
        fadeoutfile.close()
        del fadeinfile
        del fadeoutfile
        return crossfade_part

    async def start_stream(self, clients_needed):
        # wait for clients
        print("wait for clients...")
        track = asyncio.run_coroutine_threadsafe(
                self.mass.music.track('2741', provider='database'), 
                self.mass.event_loop).result()
        player_id = '1523403a-4cc4-f151-29d1-758822807128'
        player = self.mass.players._players[player_id]
        cancelled = threading.Event()
        # wait for clients
        while len(self.stream_clients) < clients_needed:
            await asyncio.sleep(0.1)
        # start streaming
        while self.stream_clients:
            audio_stream = self.__get_audio_stream(player, track, cancelled)
            async for is_last_chunk, audio_chunk in audio_stream:
                for client in self.stream_clients:
                    try:
                        client.write(audio_chunk)
                        await client.drain()
                    except ConnectionResetError:
                        print('client disconnected')
                        client.close()
                        self.stream_clients.remove(client)
            await asyncio.sleep(1)
        print("all clients disconnected")
        return        

    async def add_client(self, client_writer, client_msg):
        print("new client connected!")
        for line in client_msg.decode().split('\r\n'):
            print(line)
        msg = 'HTTP/1.0 200 OK\r\n'
        msg += "Content-Type: audio/flac\r\n"
        msg += "Transfer-Encoding: chunked\r\n\r\n"
        client_writer.write(msg.encode())
        await client_writer.drain()
        self.stream_clients.append(client_writer)
        if len(self.stream_clients) == 1:
            bg_task = run_async_background_task(
                    None, 
                    self.start_stream, 2)

    async def sockets_streamer(self, reader, writer):
        while True:
            request = await reader.read(1024)
            if request:
                await self.add_client(writer, request)
            else:
                print("client lost")
                break

