"""Players API endpoints."""

from aiohttp.web import Request, Response, RouteTableDef, StreamResponse
from music_assistant.helpers.web import require_local_subnet
from music_assistant.models.media_types import MediaType

routes = RouteTableDef()


@routes.get("/stream/media/{media_type}/{item_id}")
async def stream_media(request: Request):
    """Stream a single audio track."""
    media_type = MediaType.from_string(request.match_info["media_type"])
    if media_type not in [MediaType.Track, MediaType.Radio]:
        return Response(status=404, reason="Media item is not playable!")
    item_id = request.match_info["item_id"]
    provider = request.rel_url.query.get("provider", "database")
    media_item = await request.app["mass"].music.async_get_item(
        item_id, provider, media_type
    )
    streamdetails = await request.app["mass"].music.async_get_stream_details(media_item)

    # prepare request
    content_type = streamdetails.content_type.value
    resp = StreamResponse(
        status=200, reason="OK", headers={"Content-Type": f"audio/{content_type}"}
    )
    await resp.prepare(request)

    # stream track
    async for audio_chunk in request.app["mass"].streams.async_get_stream(
        streamdetails
    ):
        await resp.write(audio_chunk)
    return resp


@routes.get("/stream/queue/{player_id}")
@require_local_subnet
async def stream_queue(request: Request):
    """Stream a player's queue."""
    player_id = request.match_info["player_id"]
    if not request.app["mass"].players.get_player_queue(player_id):
        return Response(text="invalid queue", status=404)

    # prepare request
    resp = StreamResponse(
        status=200, reason="OK", headers={"Content-Type": "audio/flac"}
    )
    resp.enable_chunked_encoding()
    await resp.prepare(request)

    # stream queue
    async for audio_chunk in request.app["mass"].streams.async_queue_stream_flac(
        player_id
    ):
        await resp.write(audio_chunk)
    return resp


@routes.get("/stream/queue/{player_id}/{queue_item_id}")
@require_local_subnet
async def stream_queue_item(request: Request):
    """Stream a single queue item."""
    player_id = request.match_info["player_id"]
    queue_item_id = request.match_info["queue_item_id"]

    # prepare request
    resp = StreamResponse(
        status=200, reason="OK", headers={"Content-Type": "audio/flac"}
    )
    await resp.prepare(request)

    async for audio_chunk in request.app["mass"].streams.async_stream_queue_item(
        player_id, queue_item_id
    ):
        await resp.write(audio_chunk)
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
    player_state = request.app["mass"].players.get_player(group_player_id)
    async for audio_chunk in player_state.subscribe_stream_client(child_player_id):
        await resp.write(audio_chunk)
    return resp
