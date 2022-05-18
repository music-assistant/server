"""Utilities for image manipulation and retrieval."""
from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Optional

from PIL import Image

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def create_thumbnail(
    mass: MusicAssistant, path: str, size: Optional[int]
) -> bytes:
    """Create thumbnail from image url."""
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
            if not await prov.exists(path):
                continue
            # embedded image in music file
            img_data = await prov.get_embedded_image(path)
            # regular image file on disk
            if not img_data:
                async with prov.open_file(path) as _file:
                    img_data = await _file.read()
            break
    if not img_data:
        raise FileNotFoundError(f"Image not found: {path}")

    def _create_image():
        data = BytesIO(img_data)
        img = Image.open(data)
        if size:
            img.thumbnail((size, size), Image.ANTIALIAS)
        img.save(data, format="png")
        return data.getvalue()

    return await mass.loop.run_in_executor(None, _create_image)
