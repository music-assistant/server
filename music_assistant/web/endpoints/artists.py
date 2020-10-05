"""Artists API endpoints."""

from aiohttp.web import Request, Response, RouteTableDef
from aiohttp_jwt import login_required
from music_assistant.helpers.util import json_serializer
from music_assistant.helpers.web import async_stream_json

routes = RouteTableDef()


@routes.get("/api/artists")
@login_required
async def async_artists(request: Request):
    """Get all artists known in the database."""
    generator = request.app["mass"].database.async_get_artists()
    return await async_stream_json(request, generator)


@routes.get("/api/artists/{item_id}")
@login_required
async def async_artist(request: Request):
    """Get full artist details."""
    item_id = request.match_info.get("item_id")
    provider = request.rel_url.query.get("provider")
    lazy = request.rel_url.query.get("lazy", "true") != "false"
    if item_id is None or provider is None:
        return Response(text="invalid item or provider", status=501)
    result = await request.app["mass"].music.async_get_artist(
        item_id, provider, lazy=lazy
    )
    return Response(body=json_serializer(result), content_type="application/json")


@routes.get("/api/artists/{item_id}/toptracks")
@login_required
async def async_artist_toptracks(request: Request):
    """Get top tracks for given artist."""
    item_id = request.match_info.get("item_id")
    provider = request.rel_url.query.get("provider")
    if item_id is None or provider is None:
        return Response(text="invalid item_id or provider", status=501)
    generator = request.app["mass"].music.async_get_artist_toptracks(item_id, provider)
    return await async_stream_json(request, generator)


@routes.get("/api/artists/{item_id}/albums")
@login_required
async def async_artist_albums(request: Request):
    """Get (all) albums for given artist."""
    item_id = request.match_info.get("item_id")
    provider = request.rel_url.query.get("provider")
    if item_id is None or provider is None:
        return Response(text="invalid item_id or provider", status=501)
    generator = request.app["mass"].music.async_get_artist_albums(item_id, provider)
    return await async_stream_json(request, generator)
