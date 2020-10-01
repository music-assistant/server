"""Tracks API endpoints."""

from aiohttp import web
from aiohttp_jwt import login_required
from music_assistant.helpers.util import json_serializer
from music_assistant.helpers.web import async_stream_json

routes = web.RouteTableDef()


@routes.get("/api/radios")
@login_required
async def async_radios(request: web.Request):
    """Get all radios known in the database."""
    generator = request.app["mass"].database.async_get_radios()
    return await async_stream_json(request, generator)


@routes.get("/api/radios/{item_id}")
@login_required
async def async_radio(request: web.Request):
    """Get full radio details."""
    item_id = request.match_info.get("item_id")
    provider = request.rel_url.query.get("provider")
    if item_id is None or provider is None:
        return web.Response(text="invalid item_id or provider", status=501)
    result = await request.app["mass"].music.async_get_radio(item_id, provider)
    return web.Response(body=json_serializer(result), content_type="application/json")
