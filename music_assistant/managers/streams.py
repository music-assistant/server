"""
StreamManager: handles all audio streaming to players.

Either by sending tracks one by one or send one continuous stream
of music with crossfade/gapless support (queue stream).

All audio is processed by the SoX executable, using various subprocess streams.
"""
import asyncio
import logging
import shlex
import subprocess
from enum import Enum
from typing import AsyncGenerator, List, Optional, Tuple

import aiofiles
from music_assistant.constants import (
    CONF_MAX_SAMPLE_RATE,
    EVENT_STREAM_ENDED,
    EVENT_STREAM_STARTED,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_tempfile, get_ip
from music_assistant.models.streamdetails import ContentType, StreamDetails, StreamType

LOGGER = logging.getLogger("stream_manager")


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

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self.local_ip = get_ip()
        self.analyze_jobs = {}

    async def get_sox_stream(
        self,
        streamdetails: StreamDetails,
        output_format: SoxOutputFormat = SoxOutputFormat.FLAC,
        resample: Optional[int] = None,
        gain_db_adjust: Optional[float] = None,
        chunk_size: int = 512000,
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
        if streamdetails.content_type in [ContentType.AAC, ContentType.MPEG]:
            input_format = "flac"
        else:
            input_format = streamdetails.content_type.value

        args = ["sox", "-t", input_format, "-", "-t"] + output_format + ["-"]
        if gain_db_adjust:
            args += ["vol", str(gain_db_adjust), "dB"]
        if resample:
            args += ["rate", "-v", str(resample)]

        LOGGER.debug(
            "start sox stream for: %s/%s", streamdetails.provider, streamdetails.item_id
        )

        async with AsyncProcess(args, enable_write=True) as sox_proc:

            async def fill_buffer():
                """Forward audio chunks to sox stdin."""
                # feed audio data into sox stdin for processing
                async for chunk in self.get_media_stream(streamdetails):
                    await sox_proc.write(chunk)
                await sox_proc.write_eof()

            fill_buffer_task = self.mass.loop.create_task(fill_buffer())
            # yield chunks from stdout
            # we keep 1 chunk behind to detect end of stream properly
            try:
                prev_chunk = b""
                async for chunk in sox_proc.iterate_chunks(chunk_size):
                    if prev_chunk:
                        yield (False, prev_chunk)
                    prev_chunk = chunk
                # send last chunk
                yield (True, prev_chunk)
            except (asyncio.CancelledError, GeneratorExit) as err:
                LOGGER.debug(
                    "get_sox_stream aborted for: %s/%s",
                    streamdetails.provider,
                    streamdetails.item_id,
                )
                fill_buffer_task.cancel()
                raise err
            else:
                LOGGER.debug(
                    "finished sox stream for: %s/%s",
                    streamdetails.provider,
                    streamdetails.item_id,
                )

    async def queue_stream_flac(self, player_id) -> AsyncGenerator[bytes, None]:
        """Stream the PlayerQueue's tracks as constant feed in flac format."""
        player_conf = self.mass.config.get_player_config(player_id)
        sample_rate = player_conf.get(CONF_MAX_SAMPLE_RATE, 96000)

        args = [
            "sox",
            "-t",
            "s32",
            "-c",
            "2",
            "-r",
            str(sample_rate),
            "-",
            "-t",
            "flac",
            "-",
        ]
        async with AsyncProcess(args, enable_write=True) as sox_proc:

            # feed stdin with pcm samples
            async def fill_buffer():
                """Feed audio data into sox stdin for processing."""
                async for chunk in self.queue_stream_pcm(player_id, sample_rate, 32):
                    await sox_proc.write(chunk)

            fill_buffer_task = self.mass.loop.create_task(fill_buffer())

            # start yielding audio chunks
            try:
                async for chunk in sox_proc.iterate_chunks():
                    yield chunk
            except (asyncio.CancelledError, GeneratorExit) as err:
                LOGGER.debug(
                    "queue_stream_flac aborted for: %s",
                    player_id,
                )
                fill_buffer_task.cancel()
                raise err
            else:
                LOGGER.debug(
                    "finished queue_stream_flac for: %s",
                    player_id,
                )

    async def queue_stream_pcm(
        self, player_id, sample_rate=96000, bit_depth=32
    ) -> AsyncGenerator[bytes, None]:
        """Stream the PlayerQueue's tracks as constant feed in PCM raw audio."""
        player_queue = self.mass.players.get_player_queue(player_id)

        LOGGER.info("Start Queue Stream for player %s ", player_id)

        last_fadeout_data = b""
        queue_index = None
        while True:

            # get the (next) track in queue
            if queue_index is None:
                # report start of queue playback so we can calculate current track/duration etc.
                queue_index = await player_queue.queue_stream_start()
            else:
                queue_index = await player_queue.queue_stream_next(queue_index)
            queue_track = player_queue.get_item(queue_index)
            if not queue_track:
                LOGGER.info("no (more) tracks left in queue")
                break

            # get crossfade details
            fade_length = player_queue.crossfade_duration
            pcm_args = ["s32", "-c", "2", "-r", str(sample_rate)]
            sample_size = int(sample_rate * (bit_depth / 8) * 2)  # 1 second
            buffer_size = sample_size * fade_length if fade_length else sample_size * 10

            # get streamdetails
            streamdetails = await self.mass.music.get_stream_details(
                queue_track, player_id
            )
            # get gain correct / replaygain
            gain_correct = await self.mass.players.get_gain_correct(
                player_id, streamdetails.item_id, streamdetails.provider
            )
            streamdetails.gain_correct = gain_correct

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
            async for is_last_chunk, chunk in self.mass.streams.get_sox_stream(
                streamdetails,
                SoxOutputFormat.S32,
                resample=sample_rate,
                gain_db_adjust=gain_correct,
                chunk_size=buffer_size,
            ):
                cur_chunk += 1

                # HANDLE FIRST PART OF TRACK
                if not chunk and bytes_written == 0:
                    # stream error: got empy first chunk
                    LOGGER.error("Stream error on track %s", queue_track.item_id)
                    # prevent player queue get stuck by just skipping to the next track
                    queue_track.duration = 0
                    continue
                if cur_chunk <= 2 and not last_fadeout_data:
                    # no fadeout_part available so just pass it to the output directly
                    yield chunk
                    bytes_written += len(chunk)
                    del chunk
                elif cur_chunk == 1 and last_fadeout_data:
                    prev_chunk = chunk
                    del chunk
                # HANDLE CROSSFADE OF PREVIOUS TRACK FADE_OUT AND THIS TRACK FADE_IN
                elif cur_chunk == 2 and last_fadeout_data:
                    # combine the first 2 chunks and strip off silence
                    first_part = await strip_silence(prev_chunk + chunk, pcm_args)
                    if len(first_part) < buffer_size:
                        # part is too short after the strip action?!
                        # so we just use the full first part
                        first_part = prev_chunk + chunk
                    fade_in_part = first_part[:buffer_size]
                    remaining_bytes = first_part[buffer_size:]
                    del first_part
                    # do crossfade
                    crossfade_part = await crossfade_pcm_parts(
                        fade_in_part, last_fadeout_data, pcm_args, fade_length
                    )
                    # send crossfade_part
                    yield crossfade_part
                    bytes_written += len(crossfade_part)
                    del crossfade_part
                    del fade_in_part
                    last_fadeout_data = b""
                    # also write the leftover bytes from the strip action
                    yield remaining_bytes
                    bytes_written += len(remaining_bytes)
                    del remaining_bytes
                    del chunk
                    prev_chunk = None  # needed to prevent this chunk being sent again
                # HANDLE LAST PART OF TRACK
                elif prev_chunk and is_last_chunk:
                    # last chunk received so create the last_part
                    # with the previous chunk and this chunk
                    # and strip off silence
                    last_part = await strip_silence(prev_chunk + chunk, pcm_args, True)
                    if len(last_part) < buffer_size:
                        # part is too short after the strip action
                        # so we just use the entire original data
                        last_part = prev_chunk + chunk
                    if (
                        not player_queue.crossfade_enabled
                        or len(last_part) < buffer_size
                    ):
                        # crossfading is not enabled or not enough data,
                        # so just pass the (stripped) audio data
                        if not player_queue.crossfade_enabled:
                            LOGGER.warning(
                                "Not enough data for crossfade: %s", len(last_part)
                            )

                        yield last_part
                        bytes_written += len(last_part)
                        del last_part
                        del chunk
                    else:
                        # handle crossfading support
                        # store fade section to be picked up for next track
                        last_fadeout_data = last_part[-buffer_size:]
                        remaining_bytes = last_part[:-buffer_size]
                        # write remaining bytes
                        if remaining_bytes:
                            yield remaining_bytes
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
                        yield prev_chunk
                        bytes_written += len(prev_chunk)
                        prev_chunk = chunk
                    else:
                        prev_chunk = chunk
                    del chunk
            # end of the track reached
            # update actual duration to the queue for more accurate now playing info
            accurate_duration = bytes_written / sample_size
            queue_track.duration = accurate_duration
            LOGGER.debug(
                "Finished Streaming queue track: %s (%s) on queue %s",
                queue_track.item_id,
                queue_track.name,
                player_id,
            )
        # end of queue reached, pass last fadeout bits to final output
        if last_fadeout_data:
            yield last_fadeout_data
        del last_fadeout_data
        # END OF QUEUE STREAM
        LOGGER.info("streaming of queue for player %s completed", player_id)

    async def stream_queue_item(
        self, player_id: str, queue_item_id: str
    ) -> AsyncGenerator[bytes, None]:
        """Stream a single Queue item."""
        # collect streamdetails
        player_queue = self.mass.players.get_player_queue(player_id)
        if not player_queue:
            raise FileNotFoundError("invalid player_id")
        queue_item = player_queue.by_item_id(queue_item_id)
        if not queue_item:
            raise FileNotFoundError("invalid queue_item_id")
        streamdetails = await self.mass.music.get_stream_details(queue_item, player_id)

        # get gain correct / replaygain
        gain_correct = await self.mass.players.get_gain_correct(
            player_id, streamdetails.item_id, streamdetails.provider
        )
        streamdetails.gain_correct = gain_correct

        # start streaming
        LOGGER.debug("Start streaming %s (%s)", queue_item_id, queue_item.name)
        async for _, audio_chunk in self.get_sox_stream(
            streamdetails, gain_db_adjust=gain_correct, chunk_size=4000000
        ):
            yield audio_chunk
        LOGGER.debug("Finished streaming %s (%s)", queue_item_id, queue_item.name)

    async def get_media_stream(
        self, streamdetails: StreamDetails
    ) -> AsyncGenerator[bytes, None]:
        """Get the (original/untouched) audio data for the given streamdetails. Generator."""
        stream_path = streamdetails.path
        stream_type = StreamType(streamdetails.type)
        audio_data = b""
        track_loudness = await self.mass.database.get_track_loudness(
            streamdetails.item_id, streamdetails.provider
        )
        needs_analyze = track_loudness is None

        # support for AAC/MPEG created with ffmpeg in between
        if streamdetails.content_type in [ContentType.AAC, ContentType.MPEG]:
            stream_type = StreamType.EXECUTABLE
            stream_path = f'ffmpeg -v quiet -i "{stream_path}" -f flac -'

        # signal start of stream event
        self.mass.signal_event(EVENT_STREAM_STARTED, streamdetails)
        LOGGER.debug(
            "start media stream for: %s/%s (%s)",
            streamdetails.provider,
            streamdetails.item_id,
            streamdetails.type,
        )
        # stream from URL
        if stream_type == StreamType.URL:
            async with self.mass.http_session.get(stream_path) as response:
                async for chunk, _ in response.content.iter_chunks():
                    yield chunk
                    if needs_analyze and len(audio_data) < 100000000:
                        audio_data += chunk
        # stream from file
        elif stream_type == StreamType.FILE:
            async with aiofiles.open(stream_path) as afp:
                async for chunk in afp:
                    yield chunk
                    if needs_analyze and len(audio_data) < 100000000:
                        audio_data += chunk
        # stream from executable's stdout
        elif stream_type == StreamType.EXECUTABLE:
            args = shlex.split(stream_path)
            async with AsyncProcess(args) as process:
                async for chunk in process.iterate_chunks():
                    yield chunk
                    if needs_analyze and len(audio_data) < 100000000:
                        audio_data += chunk

        # signal end of stream event
        self.mass.signal_event(EVENT_STREAM_ENDED, streamdetails)
        LOGGER.debug(
            "finished media stream for: %s/%s",
            streamdetails.provider,
            streamdetails.item_id,
        )
        await self.mass.database.mark_item_played(
            streamdetails.item_id, streamdetails.provider
        )

        # send analyze job to background worker
        # TODO: feed audio chunks to analyzer while streaming
        # so we don't have to load this large chunk in memory
        if needs_analyze and audio_data:
            self.mass.add_job(self.__analyze_audio, streamdetails, audio_data)

    def __analyze_audio(self, streamdetails, audio_data) -> None:
        """Analyze track audio, for now we only calculate EBU R128 loudness."""
        item_key = "%s%s" % (streamdetails.item_id, streamdetails.provider)
        if item_key in self.analyze_jobs:
            return  # prevent multiple analyze jobs for same track
        self.analyze_jobs[item_key] = True

        # get track loudness
        track_loudness = self.mass.add_job(
            self.mass.database.get_track_loudness(
                streamdetails.item_id, streamdetails.provider
            )
        ).result()
        if track_loudness is None:
            # only when needed we do the analyze stuff
            LOGGER.debug("Start analyzing track %s", item_key)
            # calculate BS.1770 R128 integrated loudness with ffmpeg
            # we used pyloudnorm here before but the numpy/scipy requirements were too heavy,
            # considered the same feature is also included in ffmpeg
            value = subprocess.check_output(
                "ffmpeg -i pipe: -af ebur128=framelog=verbose -f null - 2>&1 | awk '/I:/{print $2}'",
                shell=True,
                input=audio_data,
            )
            loudness = float(value.decode().strip())
            self.mass.add_job(
                self.mass.database.set_track_loudness(
                    streamdetails.item_id, streamdetails.provider, loudness
                )
            )
            LOGGER.debug("Integrated loudness of track %s is: %s", item_key, loudness)
        del audio_data
        self.analyze_jobs.pop(item_key, None)


async def crossfade_pcm_parts(
    fade_in_part: bytes, fade_out_part: bytes, pcm_args: List[str], fade_length: int
) -> bytes:
    """Crossfade two chunks of pcm/raw audio using sox."""
    # create fade-in part
    fadeinfile = create_tempfile()
    args = ["sox", "--ignore-length", "-t"] + pcm_args
    args += ["-", "-t"] + pcm_args + [fadeinfile.name, "fade", "t", str(fade_length)]
    async with AsyncProcess(args, enable_write=True) as sox_proc:
        await sox_proc.communicate(fade_in_part)
    # create fade-out part
    fadeoutfile = create_tempfile()
    args = ["sox", "--ignore-length", "-t"] + pcm_args + ["-", "-t"] + pcm_args
    args += [fadeoutfile.name, "reverse", "fade", "t", str(fade_length), "reverse"]
    async with AsyncProcess(args, enable_write=True) as sox_proc:
        await sox_proc.communicate(fade_out_part)
    # create crossfade using sox and some temp files
    # TODO: figure out how to make this less complex and without the tempfiles
    args = ["sox", "-m", "-v", "1.0", "-t"] + pcm_args + [fadeoutfile.name, "-v", "1.0"]
    args += ["-t"] + pcm_args + [fadeinfile.name, "-t"] + pcm_args + ["-"]
    async with AsyncProcess(args, enable_write=False) as sox_proc:
        crossfade_part, _ = await sox_proc.communicate()
    fadeinfile.close()
    fadeoutfile.close()
    del fadeinfile
    del fadeoutfile
    return crossfade_part


async def strip_silence(audio_data: bytes, pcm_args: List[str], reverse=False) -> bytes:
    """Strip silence from (a chunk of) pcm audio."""
    args = ["sox", "--ignore-length", "-t"] + pcm_args + ["-", "-t"] + pcm_args + ["-"]
    if reverse:
        args.append("reverse")
    args += ["silence", "1", "0.1", "1%"]
    if reverse:
        args.append("reverse")
    async with AsyncProcess(args, enable_write=True) as sox_proc:
        stripped_data, _ = await sox_proc.communicate(audio_data)
    return stripped_data
