"""
StreamManager: handles all audio streaming to players.

Either by sending tracks one by one or send one continuous stream
of music with crossfade/gapless support (queue stream).
"""
import asyncio
import gc
import gzip
import io
import logging
import os
import shlex
from enum import Enum
from typing import AsyncGenerator, List, Optional, Tuple

import pyloudnorm
import soundfile
from aiofile import AIOFile, Reader
from music_assistant.constants import EVENT_STREAM_ENDED, EVENT_STREAM_STARTED
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.models.streamdetails import ContentType, StreamDetails, StreamType
from music_assistant.utils import (
    create_tempfile,
    decrypt_bytes,
    decrypt_string,
    encrypt_bytes,
    get_ip,
    try_parse_int,
    yield_chunks,
)

LOGGER = logging.getLogger("mass")


class SoxOutputFormat(Enum):
    """Enum representing the various output formats."""

    MP3 = "mp3"  # Lossy mp3
    OGG = "ogg"  # Lossy Ogg Vorbis
    FLAC = "flac"  # Flac (with default compression)
    S24 = "s24"  # Raw PCM 24bits signed
    S32 = "s32"  # Raw PCM 32bits signed
    S64 = "s64"  # Raw PCM 64bits signed


class StreamManager:
    """Built-in streamer utilizing SoX."""

    def __init__(self, mass: MusicAssistantType) -> None:
        """Initialize class."""
        self.mass = mass
        self.local_ip = get_ip()
        self.analyze_jobs = {}

    async def async_get_sox_stream(
        self,
        streamdetails: StreamDetails,
        output_format: SoxOutputFormat = SoxOutputFormat.FLAC,
        resample: Optional[int] = None,
        gain_db_adjust: Optional[float] = None,
        chunk_size: int = 128000,
    ) -> AsyncGenerator[Tuple[bool, bytes], None]:
        """Get the sox manipulated audio data for the given streamdetails."""
        # collect all args for sox
        if output_format in [
            SoxOutputFormat.S24,
            SoxOutputFormat.S32,
            SoxOutputFormat.S64,
        ]:
            output_format = [output_format.value, "-c", "2"]
        else:
            output_format = [output_format.value]
        args = (
            ["sox", "-t", streamdetails.content_type.value, "-", "-t"]
            + output_format
            + ["-"]
        )
        if gain_db_adjust:
            args += ["vol", str(gain_db_adjust), "dB"]
        if resample:
            args += ["rate", "-v", str(resample)]
        LOGGER.debug(
            "[async_get_sox_stream] [%s/%s] started using args: %s",
            streamdetails.provider,
            streamdetails.item_id,
            " ".join(args),
        )
        # init the process with stdin/out pipes
        sox_proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            bufsize=0,
        )

        async def fill_buffer():
            """Forward audio chunks to sox stdin."""
            LOGGER.debug(
                "[async_get_sox_stream] [%s/%s] fill_buffer started",
                streamdetails.provider,
                streamdetails.item_id,
            )
            # feed audio data into sox stdin for processing
            async for chunk in self.async_get_media_stream(streamdetails):
                sox_proc.stdin.write(chunk)
                await sox_proc.stdin.drain()
            sox_proc.stdin.write_eof()
            await sox_proc.stdin.drain()
            LOGGER.debug(
                "[async_get_sox_stream] [%s/%s] fill_buffer finished",
                streamdetails.provider,
                streamdetails.item_id,
            )

        fill_buffer_task = self.mass.loop.create_task(fill_buffer())
        try:
            # yield chunks from stdout
            # we keep 1 chunk behind to detect end of stream properly
            prev_chunk = b""
            while True:
                # read exactly chunksize of data
                try:
                    chunk = await sox_proc.stdout.readexactly(chunk_size)
                except asyncio.IncompleteReadError as exc:
                    chunk = exc.partial
                if len(chunk) < chunk_size:
                    # last chunk
                    yield (True, prev_chunk + chunk)
                    break
                if prev_chunk:
                    yield (False, prev_chunk)
                prev_chunk = chunk

            await asyncio.wait([fill_buffer_task])
            LOGGER.debug(
                "[async_get_sox_stream] [%s/%s] finished",
                streamdetails.provider,
                streamdetails.item_id,
            )
        except (GeneratorExit, Exception):  # pylint: disable=broad-except
            LOGGER.warning(
                "[async_get_sox_stream] [%s/%s] aborted",
                streamdetails.provider,
                streamdetails.item_id,
            )
            if fill_buffer_task and not fill_buffer_task.cancelled():
                fill_buffer_task.cancel()
            sox_proc.terminate()
            await sox_proc.communicate()
            await sox_proc.wait()
            # raise GeneratorExit from exc
        else:
            LOGGER.debug(
                "[async_get_sox_stream] [%s/%s] finished",
                streamdetails.provider,
                streamdetails.item_id,
            )

    async def async_queue_stream_flac(self, player_id) -> AsyncGenerator[bytes, None]:
        """Stream the PlayerQueue's tracks as constant feed in flac format."""

        args = ["sox", "-t", "s32", "-c", "2", "-r", "96000", "-", "-t", "flac", "-"]
        sox_proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        LOGGER.debug(
            "[async_queue_stream_flac] [%s] started using args: %s",
            player_id,
            " ".join(args),
        )
        chunk_size = 571392  # 74,7% of pcm

        # feed stdin with pcm samples
        async def fill_buffer():
            """Feed audio data into sox stdin for processing."""
            LOGGER.debug(
                "[async_queue_stream_flac] [%s] fill buffer started", player_id
            )
            async for chunk in self.async_queue_stream_pcm(player_id, 96000, 32):
                sox_proc.stdin.write(chunk)
                await sox_proc.stdin.drain()
            sox_proc.stdin.write_eof()
            await sox_proc.stdin.drain()
            LOGGER.debug(
                "[async_queue_stream_flac] [%s] fill buffer finished", player_id
            )

        fill_buffer_task = self.mass.loop.create_task(fill_buffer())
        try:
            # yield flac chunks from stdout
            while True:
                try:
                    chunk = await sox_proc.stdout.readexactly(chunk_size)
                    yield chunk
                except asyncio.IncompleteReadError as exc:
                    chunk = exc.partial
                    yield chunk
                    break
        except (GeneratorExit, Exception):  # pylint: disable=broad-except
            LOGGER.debug("[async_queue_stream_flac] [%s] aborted", player_id)
            if fill_buffer_task and not fill_buffer_task.cancelled():
                fill_buffer_task.cancel()
            sox_proc.terminate()
            await sox_proc.communicate()
            await sox_proc.wait()
        else:
            LOGGER.debug(
                "[async_queue_stream_flac] [%s] finished",
                player_id,
            )

    async def async_queue_stream_pcm(
        self, player_id, sample_rate=96000, bit_depth=32
    ) -> AsyncGenerator[bytes, None]:
        """Stream the PlayerQueue's tracks as constant feed in PCM raw audio."""
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        queue_conf = self.mass.config.get_player_config(player_id)
        fade_length = try_parse_int(queue_conf["crossfade_duration"])
        pcm_args = ["s32", "-c", "2", "-r", str(sample_rate)]
        chunk_size = int(sample_rate * (bit_depth / 8) * 2)  # 1 second
        if fade_length:
            buffer_size = chunk_size * fade_length
        else:
            buffer_size = chunk_size * 10

        LOGGER.info("Start Queue Stream for player %s ", player_id)

        is_start = True
        last_fadeout_data = b""
        while True:

            # get the (next) track in queue
            if is_start:
                # report start of queue playback so we can calculate current track/duration etc.
                queue_track = await player_queue.async_start_queue_stream()
                is_start = False
            else:
                queue_track = player_queue.next_item
            if not queue_track:
                LOGGER.debug("no (more) tracks left in queue")
                break
            # get streamdetails
            streamdetails = await self.mass.music_manager.async_get_stream_details(
                queue_track, player_id
            )
            # get gain correct / replaygain
            gain_correct = await self.mass.player_manager.async_get_gain_correct(
                player_id, streamdetails.item_id, streamdetails.provider
            )
            LOGGER.debug(
                "Start Streaming queue track: %s (%s) for player %s",
                queue_track.item_id,
                queue_track.name,
                player_id,
            )
            fade_in_part = b""
            cur_chunk = 0
            prev_chunk = None
            bytes_written = 0
            # handle incoming audio chunks
            async for is_last_chunk, chunk in self.mass.stream_manager.async_get_sox_stream(
                streamdetails,
                SoxOutputFormat.S32,
                resample=sample_rate,
                gain_db_adjust=gain_correct,
                chunk_size=buffer_size,
            ):
                cur_chunk += 1

                # HANDLE FIRST PART OF TRACK
                if not chunk and cur_chunk == 1 and is_last_chunk:
                    LOGGER.warning("Stream error, skip track %s", queue_track.item_id)
                    break
                if cur_chunk <= 2 and not last_fadeout_data:
                    # no fadeout_part available so just pass it to the output directly
                    for small_chunk in yield_chunks(chunk, chunk_size):
                        yield small_chunk
                    bytes_written += len(chunk)
                    del chunk
                elif cur_chunk == 1 and last_fadeout_data:
                    prev_chunk = chunk
                    del chunk
                # HANDLE CROSSFADE OF PREVIOUS TRACK FADE_OUT AND THIS TRACK FADE_IN
                elif cur_chunk == 2 and last_fadeout_data:
                    # combine the first 2 chunks and strip off silence
                    first_part = await async_strip_silence(prev_chunk + chunk, pcm_args)
                    if len(first_part) < buffer_size:
                        # part is too short after the strip action?!
                        # so we just use the full first part
                        first_part = prev_chunk + chunk
                    fade_in_part = first_part[:buffer_size]
                    remaining_bytes = first_part[buffer_size:]
                    del first_part
                    # do crossfade
                    crossfade_part = await async_crossfade_pcm_parts(
                        fade_in_part, last_fadeout_data, pcm_args, fade_length
                    )
                    # send crossfade_part
                    for small_chunk in yield_chunks(crossfade_part, chunk_size):
                        yield small_chunk
                    bytes_written += len(crossfade_part)
                    del crossfade_part
                    del fade_in_part
                    last_fadeout_data = b""
                    # also write the leftover bytes from the strip action
                    for small_chunk in yield_chunks(remaining_bytes, chunk_size):
                        yield small_chunk
                    bytes_written += len(remaining_bytes)
                    del remaining_bytes
                    del chunk
                    prev_chunk = None  # needed to prevent this chunk being sent again
                # HANDLE LAST PART OF TRACK
                elif prev_chunk and is_last_chunk:
                    # last chunk received so create the last_part
                    # with the previous chunk and this chunk
                    # and strip off silence
                    last_part = await async_strip_silence(
                        prev_chunk + chunk, pcm_args, True
                    )
                    if len(last_part) < buffer_size:
                        # part is too short after the strip action
                        # so we just use the entire original data
                        last_part = prev_chunk + chunk
                        if len(last_part) < buffer_size:
                            LOGGER.warning(
                                "Not enough data for crossfade: %s", len(last_part)
                            )
                    if (
                        not player_queue.crossfade_enabled
                        or len(last_part) < buffer_size
                    ):
                        # crossfading is not enabled so just pass the (stripped) audio data
                        for small_chunk in yield_chunks(last_part, chunk_size):
                            yield small_chunk
                        bytes_written += len(last_part)
                        del last_part
                        del chunk
                    else:
                        # handle crossfading support
                        # store fade section to be picked up for next track
                        last_fadeout_data = last_part[-buffer_size:]
                        remaining_bytes = last_part[:-buffer_size]
                        # write remaining bytes
                        for small_chunk in yield_chunks(remaining_bytes, chunk_size):
                            yield small_chunk
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
                        for small_chunk in yield_chunks(prev_chunk, chunk_size):
                            yield small_chunk
                        bytes_written += len(prev_chunk)
                        prev_chunk = chunk
                    else:
                        prev_chunk = chunk
                    del chunk
            # end of the track reached
            # update actual duration to the queue for more accurate now playing info
            accurate_duration = bytes_written / chunk_size
            queue_track.duration = accurate_duration
            LOGGER.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.item_id,
                queue_track.name,
                player_id,
            )
            # run garbage collect manually to avoid too much memory fragmentation
            gc.collect()
        # end of queue reached, pass last fadeout bits to final output
        for small_chunk in yield_chunks(last_fadeout_data, chunk_size):
            yield small_chunk
        del last_fadeout_data
        # END OF QUEUE STREAM
        # run garbage collect manually to avoid too much memory fragmentation
        gc.collect()
        LOGGER.info("streaming of queue for player %s completed", player_id)

    async def async_stream_queue_item(
        self, player_id: str, queue_item_id: str
    ) -> AsyncGenerator[bytes, None]:
        """Stream a single Queue item."""
        # collect streamdetails
        player_queue = self.mass.player_manager.get_player_queue(player_id)
        if not player_queue:
            raise FileNotFoundError("invalid player_id")
        queue_item = player_queue.by_item_id(queue_item_id)
        if not queue_item:
            raise FileNotFoundError("invalid queue_item_id")
        streamdetails = await self.mass.music_manager.async_get_stream_details(
            queue_item, player_id
        )

        # get gain correct / replaygain
        gain_correct = await self.mass.player_manager.async_get_gain_correct(
            player_id, streamdetails.item_id, streamdetails.provider
        )
        # start streaming
        async for _, audio_chunk in self.async_get_sox_stream(
            streamdetails, gain_db_adjust=gain_correct
        ):
            yield audio_chunk

    async def async_get_media_stream(
        self, streamdetails: StreamDetails
    ) -> AsyncGenerator[bytes, None]:
        """Get the (original/untouched) audio data for the given streamdetails. Generator."""
        stream_path = decrypt_string(streamdetails.path)
        stream_type = StreamType(streamdetails.type)
        audio_data = b""

        # Handle (optional) caching of audio data
        cache_file = "/tmp/" + f"{streamdetails.item_id}{streamdetails.provider}"[::-1]
        if os.path.isfile(cache_file):
            with gzip.open(cache_file, "rb") as _file:
                audio_data = decrypt_bytes(_file.read())
            if audio_data:
                stream_type = StreamType.CACHE

        # support for AAC created with ffmpeg in between
        if streamdetails.content_type == ContentType.AAC:
            stream_type = StreamType.EXECUTABLE
            streamdetails.content_type = ContentType.FLAC
            stream_path = f'ffmpeg -v quiet -i "{stream_path}" -f flac -'

        # signal start of stream event
        self.mass.signal_event(EVENT_STREAM_STARTED, streamdetails)
        LOGGER.debug(
            "[async_get_media_stream] [%s/%s] started, using %s",
            streamdetails.provider,
            streamdetails.item_id,
            stream_type,
        )

        if stream_type == StreamType.CACHE:
            yield audio_data
        elif stream_type == StreamType.URL:
            async with self.mass.http_session.get(stream_path) as response:
                async for chunk in response.content.iter_any():
                    audio_data += chunk
                    yield chunk
        elif stream_type == StreamType.FILE:
            async with AIOFile(stream_path) as afp:
                async for chunk in Reader(afp):
                    audio_data += chunk
                    yield chunk
        elif stream_type == StreamType.EXECUTABLE:
            args = shlex.split(stream_path)
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE
            )
            try:
                async for chunk in process.stdout:
                    audio_data += chunk
                    yield chunk
            except (GeneratorExit, Exception) as exc:  # pylint: disable=broad-except
                LOGGER.warning(
                    "[async_get_media_stream] [%s/%s] Aborted: %s",
                    streamdetails.provider,
                    streamdetails.item_id,
                    str(exc),
                )
                # read remaining bytes
                process.terminate()
                await process.communicate()
                await process.wait()

        # signal end of stream event
        self.mass.signal_event(EVENT_STREAM_ENDED, streamdetails)

        # send analyze job to background worker
        self.mass.add_job(self.__analyze_audio, streamdetails, audio_data)
        LOGGER.debug(
            "[async_get_media_stream] [%s/%s] Finished",
            streamdetails.provider,
            streamdetails.item_id,
        )

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

    def __analyze_audio(self, streamdetails, audio_data) -> None:
        """Analyze track audio, for now we only calculate EBU R128 loudness."""
        item_key = "%s%s" % (streamdetails.item_id, streamdetails.provider)
        if item_key in self.analyze_jobs:
            return  # prevent multiple analyze jobs for same track
        self.analyze_jobs[item_key] = True
        # do we need saving to disk ?
        cache_file = "/tmp/" + f"{streamdetails.item_id}{streamdetails.provider}"[::-1]
        if not os.path.isfile(cache_file):
            with gzip.open(cache_file, "wb") as _file:
                _file.write(encrypt_bytes(audio_data))
        # get track loudness
        track_loudness = self.mass.add_job(
            self.mass.database.async_get_track_loudness(
                streamdetails.item_id, streamdetails.provider
            )
        ).result()
        if track_loudness is None:
            # only when needed we do the analyze stuff
            LOGGER.debug("Start analyzing track %s", item_key)
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
            LOGGER.debug("Integrated loudness of track %s is: %s", item_key, loudness)
        del audio_data
        self.analyze_jobs.pop(item_key, None)


async def async_crossfade_pcm_parts(
    fade_in_part: bytes, fade_out_part: bytes, pcm_args: List[str], fade_length: int
) -> bytes:
    """Crossfade two chunks of pcm/raw audio using sox."""
    # create fade-in part
    fadeinfile = create_tempfile()
    args = ["sox", "--ignore-length", "-t"] + pcm_args
    args += ["-", "-t"] + pcm_args + [fadeinfile.name, "fade", "t", str(fade_length)]
    process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE)
    await process.communicate(fade_in_part)
    # create fade-out part
    fadeoutfile = create_tempfile()
    args = ["sox", "--ignore-length", "-t"] + pcm_args + ["-", "-t"] + pcm_args
    args += [fadeoutfile.name, "reverse", "fade", "t", str(fade_length), "reverse"]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE
    )
    await process.communicate(fade_out_part)
    # create crossfade using sox and some temp files
    # TODO: figure out how to make this less complex and without the tempfiles
    args = ["sox", "-m", "-v", "1.0", "-t"] + pcm_args + [fadeoutfile.name, "-v", "1.0"]
    args += ["-t"] + pcm_args + [fadeinfile.name, "-t"] + pcm_args + ["-"]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE
    )
    crossfade_part, _ = await process.communicate()
    fadeinfile.close()
    fadeoutfile.close()
    del fadeinfile
    del fadeoutfile
    return crossfade_part


async def async_strip_silence(
    audio_data: bytes, pcm_args: List[str], reverse=False
) -> bytes:
    """Strip silence from (a chunk of) pcm audio."""
    args = ["sox", "--ignore-length", "-t"] + pcm_args + ["-", "-t"] + pcm_args + ["-"]
    if reverse:
        args.append("reverse")
    args += ["silence", "1", "0.1", "1%"]
    if reverse:
        args.append("reverse")
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    stripped_data, _ = await process.communicate(audio_data)
    return stripped_data
