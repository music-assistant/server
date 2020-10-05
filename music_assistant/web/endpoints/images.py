"""Images API endpoints."""


import os

from aiohttp.web import FileResponse, Request, Response, RouteTableDef
from music_assistant.models.media_types import MediaType

routes = RouteTableDef()


@routes.get("/api/providers/{provider_id}/icon")
async def async_get_provider_icon(request: Request):
    """Get Provider icon."""
    provider_id = request.match_info.get("provider_id")
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(base_dir, "..", "providers", provider_id, "icon.png")
    if os.path.isfile(icon_path):
        headers = {"Cache-Control": "max-age=86400, public", "Pragma": "public"}
        return FileResponse(icon_path, headers=headers)
    return Response(status=404)


@routes.get("/api/{media_type}/{media_id}/thumb")
async def async_get_image(request: Request):
    """Get (resized) thumb image."""
    media_type_str = request.match_info.get("media_type")
    media_type = MediaType.from_string(media_type_str)
    media_id = request.match_info.get("media_id")
    provider = request.rel_url.query.get("provider")
    if media_id is None or provider is None:
        return Response(text="invalid media_id or provider", status=501)
    size = int(request.rel_url.query.get("size", 0))
    img_file = await request.app["mass"].music.async_get_image_thumb(
        media_id, provider, media_type, size
    )
    if not img_file or not os.path.isfile(img_file):
        return Response(status=404)
    headers = {"Cache-Control": "max-age=86400, public", "Pragma": "public"}
    return FileResponse(img_file, headers=headers)
