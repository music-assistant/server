"""Utilities for image manipulation and retrieval."""
from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Optional

from PIL import Image
from tinytag import TinyTag

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def create_thumbnail(
    mass: MusicAssistant, path: str, size: Optional[int]
) -> bytes:
    """Create thumbnail from image url."""
    if not size:
        size = 200
    img_data = None
    if path.startswith("http"):
        async with mass.http_session.get(path, verify_ssl=False) as response:
            assert response.status == 200
            img_data = await response.read()
    else:
        # assume file from file provider, we need to fetch it here...
        for prov in mass.music.providers:
            if not prov.type.is_file():
                continue
            if not prov.exists(path):
                continue
            if TinyTag.is_supported(path):
                # embedded image in music file
                def get_embedded_image():
                    tags = TinyTag.get(path, image=True)
                    return tags.get_image()

                img_data = await mass.loop.run_in_executor(None, get_embedded_image)
            else:
                # regular image file on disk
                async with prov.open_file(path) as _file:
                    img_data = await _file.read()
            break
    if not img_data:
        raise FileNotFoundError(f"Image not found: {path}")

    def _create_image():
        data = BytesIO(img_data)
        img = Image.open(data)
        img.thumbnail((size, size), Image.ANTIALIAS)
        img.save(data, format="png")
        return data.getvalue()

    return await mass.loop.run_in_executor(None, _create_image)
