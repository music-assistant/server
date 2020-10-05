"""Playlists API endpoints."""

from aiohttp.web import Request, Response, RouteTableDef
from aiohttp_jwt import login_required
from music_assistant.helpers.util import json_serializer
from music_assistant.helpers.web import async_media_items_from_body, async_stream_json

routes = RouteTableDef()


@routes.get("/api/playlists/{item_id}")
@login_required
async def async_playlist(request: Request):
    """Get full playlist details."""
    item_id = request.match_info.get("item_id")
    provider = request.rel_url.query.get("provider")
    if item_id is None or provider is None:
        return Response(text="invalid item or provider", status=501)
    result = await request.app["mass"].music.async_get_playlist(item_id, provider)
    return Response(body=json_serializer(result), content_type="application/json")


@routes.get("/api/playlists/{item_id}/tracks")
@login_required
async def async_playlist_tracks(request: Request):
    """Get playlist tracks from provider."""
    item_id = request.match_info.get("item_id")
    provider = request.rel_url.query.get("provider")
    if item_id is None or provider is None:
        return Response(text="invalid item_id or provider", status=501)
    generator = request.app["mass"].music.async_get_playlist_tracks(item_id, provider)
    return await async_stream_json(request, generator)


@routes.put("/api/playlists/{item_id}/tracks")
@login_required
async def async_add_playlist_tracks(request: Request):
    """Add tracks to (editable) playlist."""
    item_id = request.match_info.get("item_id")
    body = await request.json()
    tracks = await async_media_items_from_body(request.app["mass"], body)
    result = await request.app["mass"].music.async_add_playlist_tracks(item_id, tracks)
    return Response(body=json_serializer(result), content_type="application/json")


@routes.delete("/api/playlists/{item_id}/tracks")
@login_required
async def async_remove_playlist_tracks(request: Request):
    """Remove tracks from (editable) playlist."""
    item_id = request.match_info.get("item_id")
    body = await request.json()
    tracks = await async_media_items_from_body(request.app["mass"], body)
    result = await request.app["mass"].music.async_remove_playlist_tracks(
        item_id, tracks
    )
    return Response(body=json_serializer(result), content_type="application/json")
