"""Fonts for the text the Playlist Metadata provider draws on its covers."""

from __future__ import annotations

import contextlib
from pathlib import Path

from PIL import ImageFont

FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/Windows/Fonts/arialbd.ttf",
)


def get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """
    Return a bold TrueType font at the given size, falling back to Pillow's built-in.

    :param size: The font size, in pixels.
    """
    for path in FONT_PATHS:
        if Path(path).exists():
            with contextlib.suppress(OSError):
                return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()
