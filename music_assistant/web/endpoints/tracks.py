"""Radio's API endpoints."""

from aiohttp.web import Request, Response, RouteTableDef
from aiohttp_jwt import login_required
from music_assistant.helpers.util import json_serializer
from music_assistant.helpers.web import async_stream_json

routes = RouteTableDef()


@routes.get("/api/tracks")
@login_required
async def async_tracks(request: Request):
    """Get all tracks known in the database."""
    generator = request.app["mass"].database.async_get_tracks()
    return await async_stream_json(request, generator)


@routes.get("/api/tracks/{item_id}/versions")
@login_required
async def async_track_versions(request: Request):
    """Get all versions of an track."""
    item_id = request.match_info.get("item_id")
    provider = request.rel_url.query.get("provider")
    if item_id is None or provider is None:
        return Response(text="invalid item_id or provider", status=501)
    generator = request.app["mass"].music.async_get_track_versions(item_id, provider)
    return await async_stream_json(request, generator)


@routes.get("/api/tracks/{item_id}")
@login_required
async def async_track(request: Request):
    """Get full track details."""
    item_id = request.match_info.get("item_id")
    provider = request.rel_url.query.get("provider")
    lazy = request.rel_url.query.get("lazy", "true") != "false"
    if item_id is None or provider is None:
        return Response(text="invalid item or provider", status=501)
    result = await request.app["mass"].music.async_get_track(
        item_id, provider, lazy=lazy
    )
    return Response(body=json_serializer(result), content_type="application/json")
