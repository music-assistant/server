"""Images API endpoints."""

import os
from io import BytesIO

from aiohttp.web import FileResponse, Request, Response, RouteTableDef
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.models.media_types import MediaType
from PIL import Image

routes = RouteTableDef()


@routes.get("/api/images/provider-icon/{provider_id}")
async def async_get_provider_icon(request: Request):
    """Get Provider icon."""
    provider_id = request.match_info.get("provider_id")
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(base_dir, "..", "providers", provider_id, "icon.png")
    if os.path.isfile(icon_path):
        headers = {"Cache-Control": "max-age=86400, public", "Pragma": "public"}
        return FileResponse(icon_path, headers=headers)
    return Response(status=404)


@routes.get("/api/images/thumb")
async def async_get_image_thumb(request: Request):
    """Get (resized) thumb image."""
    mass = request.app["mass"]
    size = int(request.rel_url.query.get("size", 0))
    provider = request.rel_url.query.get("provider")
    item_id = request.rel_url.query.get("item_id")

    if provider and item_id:
        media_type = MediaType(request.rel_url.query.get("media_type"))
        url = await async_get_image_url(mass, item_id, provider, media_type)
    else:
        url = request.rel_url.query.get("url")
    if not url:
        return Response(status=404, text="Invalid URL OR media details given")

    img_file = await async_get_image_file(mass, url, size)
    if not img_file or not os.path.isfile(img_file):
        return Response(status=404)
    headers = {"Cache-Control": "max-age=86400, public", "Pragma": "public"}
    return FileResponse(img_file, headers=headers)


async def async_get_image_file(mass: MusicAssistantType, url, size: int = 150):
    """Get path to (resized) thumbnail image for given image url."""
    cache_folder = os.path.join(mass.config.data_path, ".thumbs")
    cache_id = await mass.database.async_get_thumbnail_id(url, size)
    cache_file = os.path.join(cache_folder, f"{cache_id}.png")
    if os.path.isfile(cache_file):
        # return file from cache
        return cache_file
    # no file in cache so we should get it
    os.makedirs(cache_folder, exist_ok=True)
    # download base image
    async with mass.http_session.get(url, verify_ssl=False) as response:
        assert response.status == 200
        img_data = BytesIO(await response.read())

    # save resized image
    if size:
        basewidth = size
        img = Image.open(img_data)
        wpercent = basewidth / float(img.size[0])
        hsize = int((float(img.size[1]) * float(wpercent)))
        img = img.resize((basewidth, hsize), Image.ANTIALIAS)
        img.save(cache_file)
    else:
        with open(cache_file, "wb") as _file:
            _file.write(img_data.getvalue())
    # return file from cache
    return cache_file


async def async_get_image_url(
    mass: MusicAssistantType, item_id: str, provider_id: str, media_type: MediaType
):
    """Get url to image for given media item."""
    item = await mass.music.async_get_item(item_id, provider_id, media_type)
    if not item:
        return None
    if item and item.metadata.get("image"):
        return item.metadata["image"]
    if (
        hasattr(item, "album")
        and hasattr(item.album, "metadata")
        and item.album.metadata.get("image")
    ):
        return item.album.metadata["image"]
    if hasattr(item, "albums"):
        for album in item.albums:
            if hasattr(album, "metadata") and album.metadata.get("image"):
                return album.metadata["image"]
    if (
        hasattr(item, "artist")
        and hasattr(item.artist, "metadata")
        and item.artist.metadata.get("image")
    ):
        return item.album.metadata["image"]
    if media_type == MediaType.Track and item.album:
        # try album instead for tracks
        return await async_get_image_url(
            mass, item.album.item_id, item.album.provider, MediaType.Album
        )
    elif media_type == MediaType.Album and item.artist:
        # try artist instead for albums
        return await async_get_image_url(
            mass, item.artist.item_id, item.artist.provider, MediaType.Artist
        )
    return None
