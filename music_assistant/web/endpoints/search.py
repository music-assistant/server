"""Search API endpoints."""

from aiohttp.web import Request, Response, RouteTableDef
from aiohttp_jwt import login_required
from music_assistant.helpers.util import json_serializer
from music_assistant.models.media_types import MediaType

routes = RouteTableDef()


@routes.get("/api/search")
@login_required
async def async_search(request: Request):
    """Search database and/or providers."""
    searchquery = request.rel_url.query.get("query")
    media_types_query = request.rel_url.query.get("media_types")
    limit = request.rel_url.query.get("limit", 5)
    media_types = []
    if not media_types_query or "artists" in media_types_query:
        media_types.append(MediaType.Artist)
    if not media_types_query or "albums" in media_types_query:
        media_types.append(MediaType.Album)
    if not media_types_query or "tracks" in media_types_query:
        media_types.append(MediaType.Track)
    if not media_types_query or "playlists" in media_types_query:
        media_types.append(MediaType.Playlist)
    if not media_types_query or "radios" in media_types_query:
        media_types.append(MediaType.Radio)

    result = await request.app["mass"].music.async_global_search(
        searchquery, media_types, limit=limit
    )
    return Response(body=json_serializer(result), content_type="application/json")
