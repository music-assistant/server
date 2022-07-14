"""Utilities for image manipulation and retrieval."""
from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Optional

from PIL import Image

from music_assistant.helpers.tags import get_embedded_image

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def create_thumbnail(
    mass: MusicAssistant, path: str, size: Optional[int]
) -> bytes:
    """Create thumbnail from image url."""
    # always try ffmpeg first to get the image because it supports
    # both online and offline image files as well as embedded images in media files
    img_data = await get_embedded_image(path)
    if not img_data:
        # assume file from file provider, we need to fetch it here...
        for prov in mass.music.providers:
            if not prov.type.is_file():
                continue
            if not await prov.exists(path):
                continue
            path = await prov.resolve(path)
            img_data = await get_embedded_image(path)
            break
    if not img_data:
        raise FileNotFoundError(f"Image not found: {path}")

    def _create_image():
        data = BytesIO(img_data)
        img = Image.open(data)
        if size:
            img.thumbnail((size, size), Image.ANTIALIAS)
        img.convert("RGB").save(data, "PNG", optimize=True)
        return data.getvalue()

    return await mass.loop.run_in_executor(None, _create_image)
