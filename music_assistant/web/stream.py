"""
StreamManager: handles all audio streaming to players.

Either by sending tracks one by one or send one continuous stream
of music with crossfade/gapless support (queue stream).

All audio is processed by SoX and/or ffmpeg, using various subprocess streams.
"""

import asyncio
import logging
from typing import AsyncGenerator, Optional, Tuple

from aiohttp.web import Request, Response, RouteTableDef, StreamResponse
from aiohttp.web_exceptions import HTTPNotFound
from music_assistant.constants import (
    CONF_MAX_SAMPLE_RATE,
    EVENT_STREAM_ENDED,
    EVENT_STREAM_STARTED,
)
from music_assistant.helpers.audio import (
    analyze_audio,
    crossfade_pcm_parts,
    get_stream_details,
    strip_silence,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task
from music_assistant.helpers.web import require_local_subnet
from music_assistant.models.player_queue import PlayerQueue
from music_assistant.models.streamdetails import ContentType, StreamDetails, StreamType

routes = RouteTableDef()

LOGGER = logging.getLogger("stream")


@routes.get("/stream/queue/{player_id}")
@require_local_subnet
async def stream_queue(request: Request):
    """Stream all items in player's queue as continuous stream in FLAC audio format."""
    mass: MusicAssistant = request.app["mass"]
    player_id = request.match_info["player_id"]
    player_queue = mass.players.get_player_queue(player_id)
    if not player_queue:
        raise HTTPNotFound(reason="invalid player_id")
    LOGGER.info("Start Queue Stream for player %s ", player_queue.player.name)

    # prepare request
    resp = StreamResponse(
        status=200, reason="OK", headers={"Content-Type": "audio/flac"}
    )
    await resp.prepare(request)

    player_conf = player_queue.player.config
    sample_rate = min(player_conf.get(CONF_MAX_SAMPLE_RATE, 96000), 96000)

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
            async for audio_chunk in get_queue_stream(
                mass, player_queue, sample_rate, 32
            ):
                await sox_proc.write(audio_chunk)
                del audio_chunk

        fill_buffer_task = create_task(fill_buffer())

        # start delivering audio chunks
        try:
            async for audio_chunk in sox_proc.iterate_chunks(None):
                await resp.write(audio_chunk)
        except (asyncio.CancelledError, GeneratorExit) as err:
            LOGGER.debug(
                "Queue stream aborted for: %s",
                player_queue.player.name,
            )
            fill_buffer_task.cancel()
            raise err
        else:
            LOGGER.debug(
                "Queue stream finished for: %s",
                player_queue.player.name,
            )
            return resp


@routes.get("/stream/queue/{player_id}/{queue_item_id}")
@require_local_subnet
async def stream_single_queue_item(request: Request):
    """Stream a single queue item."""
    mass: MusicAssistant = request.app["mass"]
    player_id = request.match_info["player_id"]
    queue_item_id = request.match_info["queue_item_id"]
    player_queue = mass.players.get_player_queue(player_id)
    if not player_queue:
        raise HTTPNotFound(reason="invalid player_id")
    if player_queue.use_queue_stream and not request.query.get("alert"):
        # redirect request if player switched to queue streaming
        return await stream_queue(request)
    LOGGER.debug("Stream request for %s", player_queue.player.name)

    queue_item = player_queue.by_item_id(queue_item_id)
    if not queue_item:
        raise HTTPNotFound(reason="invalid queue_item_id")

    streamdetails = await get_stream_details(mass, queue_item, player_id)

    # prepare request
    resp = StreamResponse(
        status=200,
        reason="OK",
        headers={"Content-Type": "audio/flac"},
    )
    await resp.prepare(request)

    # start streaming
    LOGGER.debug(
        "Start streaming %s (%s) on player %s",
        queue_item_id,
        queue_item.name,
        player_queue.player.name,
    )

    async for _, audio_chunk in get_media_stream(mass, streamdetails):
        await resp.write(audio_chunk)
        del audio_chunk
    LOGGER.debug(
        "Finished streaming %s (%s) on player %s",
        queue_item_id,
        queue_item.name,
        player_queue.player.name,
    )

    return resp


@routes.get("/stream/group/{group_player_id}")
@require_local_subnet
async def stream_group(request: Request):
    """Handle streaming to all players of a group. Highly experimental."""
    group_player_id = request.match_info["group_player_id"]
    if not request.app["mass"].players.get_player_queue(group_player_id):
        return Response(text="invalid player id", status=404)
    child_player_id = request.rel_url.query.get("player_id", request.remote)

    # prepare request
    resp = StreamResponse(
        status=200, reason="OK", headers={"Content-Type": "audio/flac"}
    )
    await resp.prepare(request)

    # stream queue
    player = request.app["mass"].players.get_player(group_player_id)
    async for audio_chunk in player.subscribe_stream_client(child_player_id):
        await resp.write(audio_chunk)
    return resp


async def get_media_stream(
    mass: MusicAssistant,
    streamdetails: StreamDetails,
    output_format: Optional[ContentType] = None,
    resample: Optional[int] = None,
    chunk_size: Optional[int] = None,
) -> AsyncGenerator[Tuple[bool, bytes], None]:
    """Get the audio stream for the given streamdetails."""
    input_format = streamdetails.content_type.value
    stream_path = streamdetails.path
    stream_type = StreamType(streamdetails.type)
    if output_format is None:
        output_format = ContentType.FLAC

    # collect all args for sox/ffmpeg
    if output_format in [
        ContentType.S24,
        ContentType.S32,
        ContentType.S64,
    ]:
        output_args = ["-t", output_format.value, "-c", "2", "-"]
    elif output_format == ContentType.FLAC:
        output_args = ["-t", output_format.value] + ["-C", "0", "-"]
    else:
        output_args = ["-t", output_format.value, "-"]

    # stream from URL or file
    if stream_type in [StreamType.URL, StreamType.FILE]:
        # input_args = ["sox", "-t", input_format, stream_path]
        input_args = ["sox", stream_path]
    # stream from executable
    else:
        input_args = [stream_path, "|", "sox", "-t", input_format, "-"]

    filter_args = []
    if streamdetails.gain_correct:
        filter_args += ["vol", str(streamdetails.gain_correct), "dB"]
    if resample:
        filter_args += ["rate", "-v", str(resample)]

    if streamdetails.content_type in [ContentType.AAC, ContentType.MPEG]:
        # use ffmpeg for processing radio streams
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            stream_path,
            "-filter:a",
            "volume=%sdB" % streamdetails.gain_correct,
            "-f",
            "flac",
            "-",
        ]
    else:
        # regular sox processing
        args = input_args + output_args + filter_args

    # signal start of stream event
    mass.eventbus.signal(EVENT_STREAM_STARTED, streamdetails)
    LOGGER.debug(
        "start media stream for: %s/%s (%s)",
        streamdetails.provider,
        streamdetails.item_id,
        streamdetails.type,
    )

    async with AsyncProcess(args) as sox_proc:

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
                "media stream aborted for: %s/%s",
                streamdetails.provider,
                streamdetails.item_id,
            )
            raise err
        else:
            LOGGER.debug(
                "finished media stream for: %s/%s",
                streamdetails.provider,
                streamdetails.item_id,
            )
            await mass.database.mark_item_played(
                streamdetails.item_id, streamdetails.provider
            )
        finally:
            mass.eventbus.signal(EVENT_STREAM_ENDED, streamdetails)
            # send analyze job to background worker
            if streamdetails.loudness is None:
                uri = f"{streamdetails.provider}://{streamdetails.media_type.value}/{streamdetails.item_id}"
                mass.tasks.add(
                    f"Analyze audio for {uri}", analyze_audio(mass, streamdetails)
                )


async def get_queue_stream(
    mass: MusicAssistant, player_queue: PlayerQueue, sample_rate=96000, bit_depth=32
) -> AsyncGenerator[bytes, None]:
    """Stream the PlayerQueue's tracks as constant feed in PCM raw audio."""
    last_fadeout_data = b""
    queue_index = None
    # get crossfade details
    fade_length = player_queue.crossfade_duration
    pcm_args = ["s32", "-c", "2", "-r", str(sample_rate)]
    sample_size = int(sample_rate * (bit_depth / 8) * 2)  # 1 second
    buffer_size = sample_size * fade_length if fade_length else sample_size * 10
    # stream queue tracks one by one
    while True:
        # get the (next) track in queue
        if queue_index is None:
            # report start of queue playback so we can calculate current track/duration etc.
            queue_index = await player_queue.queue_stream_start()
        else:
            queue_index = await player_queue.queue_stream_next(queue_index)
        queue_track = player_queue.get_item(queue_index)
        if not queue_track:
            LOGGER.debug("no (more) tracks in queue")
            break

        # get streamdetails
        streamdetails = await get_stream_details(
            mass, queue_track, player_queue.queue_id
        )

        LOGGER.debug(
            "Start Streaming queue track: %s (%s) for player %s",
            queue_track.item_id,
            queue_track.name,
            player_queue.player.name,
        )
        fade_in_part = b""
        cur_chunk = 0
        prev_chunk = None
        bytes_written = 0
        # handle incoming audio chunks
        async for is_last_chunk, chunk in get_media_stream(
            mass,
            streamdetails,
            ContentType.S32,
            resample=sample_rate,
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
                if not player_queue.crossfade_enabled or len(last_part) < buffer_size:
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
            player_queue.player.name,
        )
    # end of queue reached, pass last fadeout bits to final output
    if last_fadeout_data:
        yield last_fadeout_data
    del last_fadeout_data
    # END OF QUEUE STREAM
