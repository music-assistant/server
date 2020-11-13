"""Library API endpoints."""

from aiohttp.web import Request, RouteTableDef
from aiohttp_jwt import login_required
from music_assistant.helpers.web import async_json_response, async_media_items_from_body

routes = RouteTableDef()


@routes.get("/api/library/artists")
@login_required
async def async_library_artists(request: Request):
    """Get all library artists."""
    orderby = request.query.get("orderby", "name")

    return await async_json_response(
        await request.app["mass"].library.async_get_library_artists(orderby=orderby)
    )


@routes.get("/api/library/albums")
@login_required
async def async_library_albums(request: Request):
    """Get all library albums."""
    orderby = request.query.get("orderby", "name")

    return await async_json_response(
        await request.app["mass"].library.async_get_library_albums(orderby=orderby)
    )


@routes.get("/api/library/tracks")
@login_required
async def async_library_tracks(request: Request):
    """Get all library tracks."""
    orderby = request.query.get("orderby", "name")

    return await async_json_response(
        await request.app["mass"].library.async_get_library_tracks(orderby=orderby)
    )


@routes.get("/api/library/radios")
@login_required
async def async_library_radios(request: Request):
    """Get all library radios."""
    orderby = request.query.get("orderby", "name")

    return await async_json_response(
        await request.app["mass"].library.async_get_library_radios(orderby=orderby)
    )


@routes.get("/api/library/playlists")
@login_required
async def async_library_playlists(request: Request):
    """Get all library playlists."""
    orderby = request.query.get("orderby", "name")

    return await async_json_response(
        await request.app["mass"].library.async_get_library_playlists(orderby=orderby)
    )


@routes.put("/api/library")
@login_required
async def async_library_add(request: Request):
    """Add item(s) to the library."""
    body = await request.json()
    media_items = await async_media_items_from_body(request.app["mass"], body)
    result = await request.app["mass"].library.async_library_add(media_items)
    return await async_json_response(result)


@routes.delete("/api/library")
@login_required
async def async_library_remove(request: Request):
    """Remove item(s) from the library."""
    body = await request.json()
    media_items = await async_media_items_from_body(request.app["mass"], body)
    result = await request.app["mass"].library.async_library_remove(media_items)
    return await async_json_response(result)
