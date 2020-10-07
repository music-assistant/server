"""Players API endpoints."""

import orjson
from aiohttp.web import Request, Response, RouteTableDef
from aiohttp_jwt import login_required
from music_assistant.helpers.util import json_serializer
from music_assistant.helpers.web import async_media_items_from_body, async_stream_json
from music_assistant.models.player_queue import QueueOption

routes = RouteTableDef()


@routes.get("/api/players")
@login_required
async def async_players(request: Request):
    # pylint: disable=unused-argument
    """Get all playerstates."""
    player_states = request.app["mass"].players.player_states
    player_states.sort(key=lambda x: str(x.name), reverse=False)
    players = [player_state.to_dict() for player_state in player_states]
    return Response(body=json_serializer(players), content_type="application/json")


@routes.post("/api/players/{player_id}/cmd/{cmd}")
@login_required
async def async_player_command(request: Request):
    """Issue player command."""
    success = False
    player_id = request.match_info.get("player_id")
    cmd = request.match_info.get("cmd")
    try:
        cmd_args = await request.json(loads=orjson.loads)
        if cmd_args in ["", {}, []]:
            cmd_args = None
    except orjson.JSONDecodeError:
        cmd_args = None
    player_cmd = getattr(request.app["mass"].players, f"async_cmd_{cmd}", None)
    if player_cmd and cmd_args is not None:
        success = await player_cmd(player_id, cmd_args)
    elif player_cmd:
        success = await player_cmd(player_id)
    else:
        return Response(text="invalid command", status=501)
    result = {"success": success in [True, None]}
    return Response(body=json_serializer(result), content_type="application/json")


@routes.post("/api/players/{player_id}/play_media/{queue_opt}")
@login_required
async def async_player_play_media(request: Request):
    """Issue player play media command."""
    player_id = request.match_info.get("player_id")
    player_state = request.app["mass"].players.get_player_state(player_id)
    if not player_state:
        return Response(status=404)
    queue_opt = QueueOption(request.match_info.get("queue_opt", "play"))
    body = await request.json()
    media_items = await async_media_items_from_body(request.app["mass"], body)
    success = await request.app["mass"].players.async_play_media(
        player_id, media_items, queue_opt
    )
    result = {"success": success in [True, None]}
    return Response(body=json_serializer(result), content_type="application/json")


@routes.get("/api/players/{player_id}/queue/items/{queue_item}")
@login_required
async def async_player_queue_item(request: Request):
    """Return item (by index or queue item id) from the player's queue."""
    player_id = request.match_info.get("player_id")
    item_id = request.match_info.get("queue_item")
    player_queue = request.app["mass"].players.get_player_queue(player_id)
    if not player_queue:
        return Response(text="invalid player", status=404)
    try:
        item_id = int(item_id)
        queue_item = player_queue.get_item(item_id)
    except ValueError:
        queue_item = player_queue.by_item_id(item_id)
    return Response(body=json_serializer(queue_item), content_type="application/json")


@routes.get("/api/players/{player_id}/queue/items")
@login_required
async def async_player_queue_items(request: Request):
    """Return the items in the player's queue."""
    player_id = request.match_info.get("player_id")
    player_queue = request.app["mass"].players.get_player_queue(player_id)
    if not player_queue:
        return Response(text="invalid player", status=404)

    async def async_queue_tracks_iter():
        for item in player_queue.items:
            yield item

    return await async_stream_json(request, async_queue_tracks_iter())


@routes.get("/api/players/{player_id}/queue")
@login_required
async def async_player_queue(request: Request):
    """Return the player queue details."""
    player_id = request.match_info.get("player_id")
    player_queue = request.app["mass"].players.get_player_queue(player_id)
    if not player_queue:
        return Response(text="invalid player", status=404)
    return Response(
        body=json_serializer(player_queue.to_dict()), content_type="application/json"
    )


@routes.put("/api/players/{player_id}/queue/{cmd}")
@login_required
async def async_player_queue_cmd(request: Request):
    """Change the player queue details."""
    player_id = request.match_info.get("player_id")
    player_queue = request.app["mass"].players.get_player_queue(player_id)
    cmd = request.match_info.get("cmd")
    try:
        cmd_args = await request.json(loads=orjson.loads)
    except orjson.JSONDecodeError:
        cmd_args = None
    if cmd == "repeat_enabled":
        player_queue.repeat_enabled = cmd_args
    elif cmd == "shuffle_enabled":
        player_queue.shuffle_enabled = cmd_args
    elif cmd == "clear":
        await player_queue.async_clear()
    elif cmd == "index":
        await player_queue.async_play_index(cmd_args)
    elif cmd == "move_up":
        await player_queue.async_move_item(cmd_args, -1)
    elif cmd == "move_down":
        await player_queue.async_move_item(cmd_args, 1)
    elif cmd == "next":
        await player_queue.async_move_item(cmd_args, 0)
    return Response(
        body=json_serializer(player_queue.to_dict()), content_type="application/json"
    )


@routes.get("/api/players/{player_id}")
@login_required
async def async_player(request: Request):
    """Get state of single player."""
    player_id = request.match_info.get("player_id")
    player_state = request.app["mass"].players.get_player_state(player_id)
    if not player_state:
        return Response(text="invalid player", status=404)
    return Response(
        body=json_serializer(player_state.to_dict()), content_type="application/json"
    )
