"""Library API endpoints."""

from aiohttp import web
from aiohttp_jwt import login_required
from music_assistant.helpers.util import json_serializer
from music_assistant.helpers.web import async_media_items_from_body, async_stream_json

routes = web.RouteTableDef()


@routes.get("/api/library/artists")
@login_required
async def async_library_artists(request: web.Request):
    """Get all library artists."""
    orderby = request.query.get("orderby", "name")
    provider_filter = request.rel_url.query.get("provider")
    generator = request.app["mass"].music.async_get_library_artists(
        orderby=orderby, provider_filter=provider_filter
    )
    return await async_stream_json(request, generator)


@routes.get("/api/library/albums")
@login_required
async def async_library_albums(request: web.Request):
    """Get all library albums."""
    orderby = request.query.get("orderby", "name")
    provider_filter = request.rel_url.query.get("provider")
    generator = request.app["mass"].music.async_get_library_albums(
        orderby=orderby, provider_filter=provider_filter
    )
    return await async_stream_json(request, generator)


@routes.get("/api/library/tracks")
@login_required
async def async_library_tracks(request: web.Request):
    """Get all library tracks."""
    orderby = request.query.get("orderby", "name")
    provider_filter = request.rel_url.query.get("provider")
    generator = request.app["mass"].music.async_get_library_tracks(
        orderby=orderby, provider_filter=provider_filter
    )
    return await async_stream_json(request, generator)


@routes.get("/api/library/radios")
@login_required
async def async_library_radios(request: web.Request):
    """Get all library radios."""
    orderby = request.query.get("orderby", "name")
    provider_filter = request.rel_url.query.get("provider")
    generator = request.app["mass"].music.async_get_library_radios(
        orderby=orderby, provider_filter=provider_filter
    )
    return await async_stream_json(request, generator)


@routes.get("/api/library/playlists")
@login_required
async def async_library_playlists(request: web.Request):
    """Get all library playlists."""
    orderby = request.query.get("orderby", "name")
    provider_filter = request.rel_url.query.get("provider")
    generator = request.app["mass"].music.async_get_library_playlists(
        orderby=orderby, provider_filter=provider_filter
    )
    return await async_stream_json(request, generator)


@routes.put("/api/library")
@login_required
async def async_library_add(request: web.Request):
    """Add item(s) to the library."""
    body = await request.json()
    media_items = await async_media_items_from_body(request.app["mass"], body)
    result = await request.app["mass"].music.async_library_add(media_items)
    return web.Response(body=json_serializer(result), content_type="application/json")


@routes.delete("/api/library")
@login_required
async def async_library_remove(request: web.Request):
    """Remove item(s) from the library."""
    body = await request.json()
    media_items = await async_media_items_from_body(request.app["mass"], body)
    result = await request.app["mass"].music.async_library_remove(media_items)
    return web.Response(body=json_serializer(result), content_type="application/json")
