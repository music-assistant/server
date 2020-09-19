"""
StreamManager: handles all audio streaming to players.

Either by sending tracks one by one or send one continuous stream
of music with crossfade/gapless support (queue stream).
"""
import asyncio
import gc
import io
import logging
import shlex
import subprocess
import threading
import urllib
from contextlib import suppress

import aiohttp
import pyloudnorm
import soundfile
from aiofile import AIOFile, Reader
from aiohttp import web
from music_assistant.constants import EVENT_STREAM_ENDED, EVENT_STREAM_STARTED
from music_assistant.models.media_types import MediaType
from music_assistant.models.player_queue import QueueItem
from music_assistant.models.streamdetails import ContentType, StreamDetails, StreamType
from music_assistant.utils import create_tempfile, decrypt_string, get_ip, try_parse_int
from music_assistant.web import require_local_subnet

LOGGER = logging.getLogger("mass")

MusicAssistantType = "MusicAssistant"


class StreamManager:
    """Built-in streamer utilizing SoX."""

    def __init__(self, mass: MusicAssistantType):
        """Initialize class."""
        self.mass = mass
        self.local_ip = get_ip()
        self.analyze_jobs = {}
        self.stream_clients = []

    async def async_get_audio_stream(self, streamdetails: StreamDetails):
        """Get the (original) audio data for the given streamdetails. Generator."""
        stream_path = decrypt_string(streamdetails.path)
        stream_type = StreamType(streamdetails.type)

        if streamdetails.content_type == ContentType.AAC:
            # support for AAC created with ffmpeg in between
            stream_type = StreamType.EXECUTABLE
            streamdetails.content_type = ContentType.FLAC
            stream_path = f'ffmpeg -v quiet -i "{stream_path}" -f flac -'

        if stream_type == StreamType.URL:
            async with self.mass.http_session.get(stream_path) as response:
                async for chunk in response.content.iter_any():
                    yield chunk
        elif stream_type == StreamType.FILE:
            async with AIOFile(stream_path) as afp:
                async for chunk in Reader(afp):
                    yield chunk
        elif stream_type == StreamType.EXECUTABLE:
            args = shlex.split(stream_path)
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE
            )
            try:
                async for chunk in process.stdout:
                    yield chunk
            except (asyncio.CancelledError, StopAsyncIteration, GeneratorExit) as exc:
                LOGGER.error("process aborted")
                raise exc
            finally:
                process.terminate()
                while True:
                    data = await process.stdout.read()
                    if not data:
                        break
                LOGGER.error("process ended")

    async def async_stream_media_item(self, http_request):
        """Start stream for a single media item, player independent."""
        # make sure we have valid params
        media_type = MediaType.from_string(http_request.match_info["media_type"])
        if media_type not in [MediaType.Track, MediaType.Radio]:
            return web.Response(status=404, reason="Media item is not playable!")
        provider = http_request.match_info["provider"]
        item_id = http_request.match_info["item_id"]
        player_id = http_request.remote  # fake player id
        # prepare headers as audio/flac content
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "audio/flac"}
        )
        await resp.prepare(http_request)
        # collect tracks to play
        media_item = await self.mass.music_manager.async_get_item(
            item_id, provider, media_type
        )
        queue_item = QueueItem(media_item)
        # run the streamer in executor to prevent the subprocess locking up our eventloop
        cancelled = threading.Event()
        bg_task = self.mass.loop.run_in_executor(
            None,
            self.__get_queue_item_stream,
            player_id,
            queue_item,
            resp,
            cancelled,
        )
        # let the streaming begin!
        try:
            await asyncio.gather(bg_task)
        except (
            asyncio.CancelledError,
            aiohttp.ClientConnectionError,
            asyncio.TimeoutError,
        ) as exc:
            cancelled.set()
            raise exc  # re-raise
        return resp

    @require_local_subnet
    async def async_stream(self, http_request):
        """Start stream for a player."""
        # make sure we have valid params
        player_id = http_request.match_info.get("player_id", "")
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        if not player_queue:
            return web.Response(status=404, reason="Player(queue) not found!")
        if not player_queue.use_queue_stream:
            queue_item_id = http_request.match_info.get("queue_item_id")
            queue_item = player_queue.by_item_id(queue_item_id)
            if not queue_item:
                return web.Response(status=404, reason="Invalid Queue item Id")
        # prepare headers as audio/flac content
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": "audio/flac"}
        )
        await resp.prepare(http_request)
        # run the streamer in executor to prevent the subprocess locking up our eventloop
        cancelled = threading.Event()
        if player_queue.use_queue_stream:
            bg_task = self.mass.loop.run_in_executor(
                None, self.__get_queue_stream, player_id, resp, cancelled
            )
        else:
            bg_task = self.mass.loop.run_in_executor(
                None,
                self.__get_queue_item_stream,
                player_id,
                queue_item,
                resp,
                cancelled,
            )
        # let the streaming begin!
        try:
            await asyncio.gather(bg_task)
        except (
            asyncio.CancelledError,
            aiohttp.ClientConnectionError,
            asyncio.TimeoutError,
        ) as exc:
            cancelled.set()
            raise exc  # re-raise
        return resp

    def __get_queue_item_stream(self, player_id, queue_item, buffer, cancelled):
        """Start streaming single queue track."""
        # pylint: disable=unused-variable
        LOGGER.debug(
            "stream single queue track started for track %s on player %s",
            queue_item.name,
            player_id,
        )
        for is_last_chunk, audio_chunk in self.__get_audio_stream(
            player_id, queue_item, cancelled
        ):
            if cancelled.is_set():
                # http session ended
                # we must consume the data to prevent hanging subprocess instances
                continue
            # put chunk in buffer
            with suppress((BrokenPipeError, ConnectionResetError)):
                asyncio.run_coroutine_threadsafe(
                    buffer.write(audio_chunk), self.mass.loop
                ).result()
        # all chunks received: streaming finished
        if cancelled.is_set():
            LOGGER.debug(
                "stream single track interrupted for track %s on player %s",
                queue_item.name,
                player_id,
            )
        else:
            # indicate EOF if no more data
            with suppress((BrokenPipeError, ConnectionResetError)):
                asyncio.run_coroutine_threadsafe(
                    buffer.write_eof(), self.mass.loop
                ).result()

            LOGGER.debug(
                "stream single track finished for track %s on player %s",
                queue_item.name,
                player_id,
            )

    def __get_queue_stream(self, player_id, buffer, cancelled):
        """Start streaming all queue tracks."""
        player_conf = self.mass.config.get_player_config(player_id)
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        sample_rate = try_parse_int(player_conf["max_sample_rate"])
        fade_length = try_parse_int(player_conf["crossfade_duration"])
        if not sample_rate or sample_rate < 44100:
            sample_rate = 96000
        if fade_length:
            fade_bytes = int(sample_rate * 4 * 2 * fade_length)
        else:
            fade_bytes = int(sample_rate * 4 * 2 * 6)
        pcm_args = "raw -b 32 -c 2 -e signed-integer -r %s" % sample_rate
        args = "sox -t %s - -t flac -C 0 -" % pcm_args
        # start sox process
        args = shlex.split(args)
        sox_proc = subprocess.Popen(
            args, shell=False, stdout=subprocess.PIPE, stdin=subprocess.PIPE
        )

        def fill_buffer():
            while True:
                chunk = sox_proc.stdout.read(128000)  # noqa
                if not chunk:
                    break
                if chunk and not cancelled.is_set():
                    with suppress((BrokenPipeError, ConnectionResetError)):
                        asyncio.run_coroutine_threadsafe(
                            buffer.write(chunk), self.mass.loop
                        ).result()
                del chunk
            # indicate EOF if no more data
            if not cancelled.is_set():
                with suppress((BrokenPipeError, ConnectionResetError)):
                    asyncio.run_coroutine_threadsafe(
                        buffer.write_eof(), self.mass.loop
                    ).result()

        # start fill buffer task in background
        fill_buffer_thread = threading.Thread(target=fill_buffer)
        fill_buffer_thread.start()

        LOGGER.info("Start Queue Stream for player %s ", player_id)
        is_start = True
        last_fadeout_data = b""
        while True:
            if cancelled.is_set():
                break
            # get the (next) track in queue
            if is_start:
                # report start of queue playback so we can calculate current track/duration etc.
                queue_track = self.mass.add_job(
                    player_queue.async_start_queue_stream()
                ).result()
                is_start = False
            else:
                queue_track = player_queue.next_item
            if not queue_track:
                LOGGER.debug("no (more) tracks left in queue")
                break
            LOGGER.debug(
                "Start Streaming queue track: %s (%s) on player %s",
                queue_track.item_id,
                queue_track.name,
                player_id,
            )
            fade_in_part = b""
            cur_chunk = 0
            prev_chunk = None
            bytes_written = 0
            # handle incoming audio chunks
            for is_last_chunk, chunk in self.__get_audio_stream(
                player_id,
                queue_track,
                cancelled,
                chunksize=fade_bytes,
                resample=sample_rate,
            ):
                cur_chunk += 1

                # HANDLE FIRST PART OF TRACK
                if not chunk and cur_chunk == 1 and is_last_chunk:
                    LOGGER.warning("Stream error, skip track %s", queue_track.item_id)
                    break
                if cur_chunk <= 2 and not last_fadeout_data:
                    # no fadeout_part available so just pass it to the output directly
                    sox_proc.stdin.write(chunk)
                    bytes_written += len(chunk)
                    del chunk
                elif cur_chunk == 1 and last_fadeout_data:
                    prev_chunk = chunk
                    del chunk
                # HANDLE CROSSFADE OF PREVIOUS TRACK FADE_OUT AND THIS TRACK FADE_IN
                elif cur_chunk == 2 and last_fadeout_data:
                    # combine the first 2 chunks and strip off silence
                    args = "sox --ignore-length -t %s - -t %s - silence 1 0.1 1%%" % (
                        pcm_args,
                        pcm_args,
                    )
                    first_part, _ = subprocess.Popen(
                        args, shell=True, stdout=subprocess.PIPE, stdin=subprocess.PIPE
                    ).communicate(prev_chunk + chunk)
                    if len(first_part) < fade_bytes:
                        # part is too short after the strip action?!
                        # so we just use the full first part
                        first_part = prev_chunk + chunk
                    fade_in_part = first_part[:fade_bytes]
                    remaining_bytes = first_part[fade_bytes:]
                    del first_part
                    # do crossfade
                    crossfade_part = self.__crossfade_pcm_parts(
                        fade_in_part, last_fadeout_data, pcm_args, fade_length
                    )
                    sox_proc.stdin.write(crossfade_part)
                    bytes_written += len(crossfade_part)
                    del crossfade_part
                    del fade_in_part
                    last_fadeout_data = b""
                    # also write the leftover bytes from the strip action
                    sox_proc.stdin.write(remaining_bytes)
                    bytes_written += len(remaining_bytes)
                    del remaining_bytes
                    del chunk
                    prev_chunk = None  # needed to prevent this chunk being sent again
                # HANDLE LAST PART OF TRACK
                elif prev_chunk and is_last_chunk:
                    # last chunk received so create the last_part
                    # with the previous chunk and this chunk
                    # and strip off silence
                    args = (
                        "sox --ignore-length -t %s - -t %s - reverse silence 1 0.1 1%% reverse"
                        % (pcm_args, pcm_args)
                    )
                    last_part, _ = subprocess.Popen(
                        args, shell=True, stdout=subprocess.PIPE, stdin=subprocess.PIPE
                    ).communicate(prev_chunk + chunk)
                    if len(last_part) < fade_bytes:
                        # part is too short after the strip action
                        # so we just use the entire original data
                        last_part = prev_chunk + chunk
                        if len(last_part) < fade_bytes:
                            LOGGER.warning(
                                "Not enough data for crossfade: %s", len(last_part)
                            )
                    if (
                        not player_queue.crossfade_enabled
                        or len(last_part) < fade_bytes
                    ):
                        # crossfading is not enabled so just pass the (stripped) audio data
                        sox_proc.stdin.write(last_part)
                        bytes_written += len(last_part)
                        del last_part
                        del chunk
                    else:
                        # handle crossfading support
                        # store fade section to be picked up for next track
                        last_fadeout_data = last_part[-fade_bytes:]
                        remaining_bytes = last_part[:-fade_bytes]
                        # write remaining bytes
                        sox_proc.stdin.write(remaining_bytes)
                        bytes_written += len(remaining_bytes)
                        del last_part
                        del remaining_bytes
                        del chunk
                # MIDDLE PARTS OF TRACK
                else:
                    # middle part of the track
                    # keep previous chunk in memory so we have enough
                    # samples to perform the crossfade
                    if prev_chunk:
                        sox_proc.stdin.write(prev_chunk)
                        bytes_written += len(prev_chunk)
                        prev_chunk = chunk
                    else:
                        prev_chunk = chunk
                    del chunk
            # end of the track reached
            if cancelled.is_set():
                # break out the loop if the http session is cancelled
                break
            # update actual duration to the queue for more accurate now playing info
            accurate_duration = bytes_written / int(sample_rate * 4 * 2)
            queue_track.duration = accurate_duration
            LOGGER.debug(
                "Finished Streaming queue track: %s (%s) on player %s",
                queue_track.item_id,
                queue_track.name,
                player_id,
            )
            # run garbage collect manually to avoid too much memory fragmentation
            gc.collect()
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_data and not cancelled.is_set():
            sox_proc.stdin.write(last_fadeout_data)
            del last_fadeout_data
        # END OF QUEUE STREAM
        sox_proc.stdin.close()
        sox_proc.terminate()
        sox_proc.communicate()
        fill_buffer_thread.join()
        # run garbage collect manually to avoid too much memory fragmentation
        gc.collect()
        if cancelled.is_set():
            LOGGER.info("streaming of queue for player %s interrupted", player_id)
        else:
            LOGGER.info("streaming of queue for player %s completed", player_id)

    def __get_audio_stream(
        self, player_id, queue_item, cancelled, chunksize=128000, resample=None
    ):
        """Get audio stream from provider and apply additional effects/processing if needed."""
        streamdetails = self.mass.add_job(
            self.mass.music_manager.async_get_stream_details(queue_item, player_id)
        ).result()
        if not streamdetails:
            LOGGER.warning("no stream details for %s", queue_item.name)
            yield (True, b"")
            return
        # get sox effects and resample options
        sox_options = self.__get_player_sox_options(player_id, streamdetails)
        outputfmt = "flac -C 0"
        if resample:
            outputfmt = "raw -b 32 -c 2 -e signed-integer"
            sox_options += " rate -v %s" % resample
        streamdetails.sox_options = sox_options
        # determine how to proceed based on input file type
        if streamdetails.content_type == ContentType.AAC:
            # support for AAC created with ffmpeg in between
            args = 'ffmpeg -v quiet -i "%s" -f flac - | sox -t flac - -t %s - %s' % (
                decrypt_string(streamdetails.path),
                outputfmt,
                sox_options,
            )
            process = subprocess.Popen(
                args, shell=True, stdout=subprocess.PIPE, bufsize=chunksize
            )
        elif streamdetails.type in [StreamType.URL, StreamType.FILE]:
            args = 'sox -t %s "%s" -t %s - %s' % (
                streamdetails.content_type.name,
                decrypt_string(streamdetails.path),
                outputfmt,
                sox_options,
            )
            args = shlex.split(args)
            process = subprocess.Popen(
                args, shell=False, stdout=subprocess.PIPE, bufsize=chunksize
            )
        elif streamdetails.type == StreamType.EXECUTABLE:
            args = "%s | sox -t %s - -t %s - %s" % (
                decrypt_string(streamdetails.path),
                streamdetails.content_type.name,
                outputfmt,
                sox_options,
            )
            process = subprocess.Popen(
                args, shell=True, stdout=subprocess.PIPE, bufsize=chunksize
            )
        else:
            LOGGER.warning("no streaming options for %s", queue_item.name)
            yield (True, b"")
            return
        # fire event that streaming has started for this track
        self.mass.signal_event(EVENT_STREAM_STARTED, streamdetails)
        # yield chunks from stdout
        # we keep 1 chunk behind to detect end of stream properly
        prev_chunk = b""
        while True:
            if cancelled.is_set():
                # http session ended
                # send terminate and pick up left over bytes
                process.terminate()
                chunk, _ = process.communicate()
            else:
                # read exactly chunksize of data
                chunk = process.stdout.read(chunksize)
            if len(chunk) < chunksize:
                # last chunk
                yield (True, prev_chunk + chunk)
                break
            if prev_chunk:
                yield (False, prev_chunk)
            prev_chunk = chunk
        # fire event that streaming has ended
        if not cancelled.is_set():
            streamdetails.seconds_played = queue_item.duration
            self.mass.signal_event(EVENT_STREAM_ENDED, streamdetails)
            # send task to background to analyse the audio
            if queue_item.media_type == MediaType.Track:
                self.mass.add_job(self.__analyze_audio, streamdetails)

    def __get_player_sox_options(
        self, player_id: str, streamdetails: StreamDetails
    ) -> str:
        """Get player specific sox effect options."""
        sox_options = []
        player_conf = self.mass.config.get_player_config(player_id)
        # volume normalisation
        gain_correct = self.mass.add_job(
            self.mass.player_manager.async_get_gain_correct(
                player_id, streamdetails.item_id, streamdetails.provider
            )
        ).result()
        if gain_correct != 0:
            sox_options.append("vol %s dB " % gain_correct)
        # downsample if needed
        if player_conf["max_sample_rate"]:
            max_sample_rate = try_parse_int(player_conf["max_sample_rate"])
            if max_sample_rate < streamdetails.sample_rate:
                sox_options.append(f"rate -v {max_sample_rate}")
        if player_conf.get("sox_options"):
            sox_options.append(player_conf["sox_options"])
        return " ".join(sox_options)

    def __analyze_audio(self, streamdetails):
        """Analyze track audio, for now we only calculate EBU R128 loudness."""
        item_key = "%s%s" % (streamdetails.item_id, streamdetails.provider)
        if item_key in self.analyze_jobs:
            return  # prevent multiple analyze jobs for same track
        self.analyze_jobs[item_key] = True
        track_loudness = self.mass.add_job(
            self.mass.database.async_get_track_loudness(
                streamdetails.item_id, streamdetails.provider
            )
        ).result()
        if track_loudness is None:
            # only when needed we do the analyze stuff
            LOGGER.debug("Start analyzing track %s", item_key)
            if streamdetails.type == StreamType.URL:
                audio_data = urllib.request.urlopen(
                    decrypt_string(streamdetails.path)
                ).read()
            elif streamdetails.type == StreamType.EXECUTABLE:
                audio_data = subprocess.check_output(
                    decrypt_string(streamdetails.path), shell=True
                )
            elif streamdetails.type == StreamType.FILE:
                with open(decrypt_string(streamdetails.path), "rb") as _file:
                    audio_data = _file.read()
            # calculate BS.1770 R128 integrated loudness
            with io.BytesIO(audio_data) as tmpfile:
                data, rate = soundfile.read(tmpfile)
            meter = pyloudnorm.Meter(rate)  # create BS.1770 meter
            loudness = meter.integrated_loudness(data)  # measure loudness
            del data
            self.mass.add_job(
                self.mass.database.async_set_track_loudness(
                    streamdetails.item_id, streamdetails.provider, loudness
                )
            )
            del audio_data
            LOGGER.debug("Integrated loudness of track %s is: %s", item_key, loudness)
        self.analyze_jobs.pop(item_key, None)

    @staticmethod
    def __crossfade_pcm_parts(fade_in_part, fade_out_part, pcm_args, fade_length):
        """Crossfade two chunks of audio using sox."""
        # create fade-in part
        fadeinfile = create_tempfile()
        args = "sox --ignore-length -t %s - -t %s %s fade t %s" % (
            pcm_args,
            pcm_args,
            fadeinfile.name,
            fade_length,
        )
        args = shlex.split(args)
        process = subprocess.Popen(args, shell=False, stdin=subprocess.PIPE)
        process.communicate(fade_in_part)
        # create fade-out part
        fadeoutfile = create_tempfile()
        args = "sox --ignore-length -t %s - -t %s %s reverse fade t %s reverse" % (
            pcm_args,
            pcm_args,
            fadeoutfile.name,
            fade_length,
        )
        args = shlex.split(args)
        process = subprocess.Popen(
            args, shell=False, stdout=subprocess.PIPE, stdin=subprocess.PIPE
        )
        process.communicate(fade_out_part)
        # create crossfade using sox and some temp files
        # TODO: figure out how to make this less complex and without the tempfiles
        args = "sox -m -v 1.0 -t %s %s -v 1.0 -t %s %s -t %s -" % (
            pcm_args,
            fadeoutfile.name,
            pcm_args,
            fadeinfile.name,
            pcm_args,
        )
        args = shlex.split(args)
        process = subprocess.Popen(
            args, shell=False, stdout=subprocess.PIPE, stdin=subprocess.PIPE
        )
        crossfade_part, _ = process.communicate()
        fadeinfile.close()
        fadeoutfile.close()
        del fadeinfile
        del fadeoutfile
        return crossfade_part
