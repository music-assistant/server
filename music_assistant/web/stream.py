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
    get_sox_args,
    get_stream_details,
    strip_silence,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task
from music_assistant.controllers.player_queue import PlayerQueue
from music_assistant.models.streamdetails import ContentType, StreamDetails

routes = RouteTableDef()

LOGGER = logging.getLogger("stream")


@routes.get("/stream/queue/{player_id}")
async def stream_queue(request: Request):
    """Stream all items in player's queue as continuous stream in FLAC audio format."""
    mass: MusicAssistant = request.app["mass"]
    player_id = request.match_info["player_id"]
    player_queue = mass.players.get_player_queue(player_id)
    if not player_queue:
        raise HTTPNotFound(reason="invalid player_id")

    # prepare request
    resp = StreamResponse(
        status=200, reason="OK", headers={"Content-Type": "audio/flac"}
    )
    await resp.prepare(request)

    player_conf = player_queue.player.config
    # determine sample rate and pcm format for the queue stream, depending on player capabilities
    player_max_sample_rate = player_conf.get(CONF_MAX_SAMPLE_RATE, 48000)
    sample_rate = min(player_max_sample_rate, 96000)
    if player_max_sample_rate > 96000:
        # assume that highest possible quality is needed
        # if player supports sample rates > 96000
        # we use float64 PCM format internally which is heavy on CPU
        pcm_format = ContentType.PCM_F64LE
    elif sample_rate > 48000:
        # prefer internal PCM_S32LE format
        pcm_format = ContentType.PCM_S32LE
    else:
        # fallback to 24 bits
        pcm_format = ContentType.PCM_S24LE

    args = [
        "sox",
        "-t",
        pcm_format.sox_format(),
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

        LOGGER.info(
            "Start Queue Stream for player %s",
            player_queue.player.name,
        )

        # feed stdin with pcm samples
        async def fill_buffer():
            """Feed audio data into sox stdin for processing."""
            async for audio_chunk in get_pcm_queue_stream(
                mass, player_queue, sample_rate, pcm_format
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
async def stream_single_queue_item(request: Request):
    """Stream a single queue item."""
    mass: MusicAssistant = request.app["mass"]
    player_id = request.match_info["player_id"]
    queue_item_id = request.match_info["queue_item_id"]
    player_queue = mass.players.get_player_queue(player_id)
    if not player_queue:
        raise HTTPNotFound(reason="invalid player_id")
    if player_queue.use_queue_stream:
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

    async for _, audio_chunk in get_media_stream(mass, streamdetails, ContentType.FLAC):
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

