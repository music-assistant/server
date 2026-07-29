"""
Colour palette system for the Hue Lights Sync provider.

A palette is a named, fixed list of colours. When one is selected it replaces the
album-art-derived colours that normally drive the base effects, so the lights
cycle through a deliberate set of colours instead. The strobe overlay keeps its
own independent colour and is unaffected by the palette.

The 100 bundled palettes live in ``palettes.json`` (shipped with the package).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .constants import PALETTE_ALBUM_COLORS

_PALETTES_PATH = Path(__file__).parent / "palettes.json"

# Empty selection = keep the default behaviour (derive colours from the music).
PALETTE_NONE = ""


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    """Parse a '#RRGGBB' (or '#RGB') hex colour into an (r, g, b) 0-1 tuple."""
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        return (
            int(text[0:2], 16) / 255.0,
            int(text[2:4], 16) / 255.0,
            int(text[4:6], 16) / 255.0,
        )
    except ValueError, IndexError:
        return (1.0, 1.0, 1.0)


@lru_cache(maxsize=1)
def _bundled() -> dict[str, list[tuple[float, float, float]]]:
    """Load the bundled palettes once: name -> list of (r, g, b) 0-1 colours."""
    try:
        data = json.loads(_PALETTES_PATH.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    result: dict[str, list[tuple[float, float, float]]] = {}
    for entry in data.get("palettes", []):
        name = entry.get("name")
        colors = [_hex_to_rgb(c) for c in entry.get("colors", []) if c]
        if name and colors:
            result[str(name)] = colors
    return result


def palette_names() -> list[str]:
    """Return all bundled palette names, in file order."""
    return list(_bundled())


def _rgb_to_hex(color: tuple[float, float, float]) -> str:
    """Format an (r, g, b) 0-1 tuple as '#RRGGBB'."""
    return f"#{round(color[0] * 255):02X}{round(color[1] * 255):02X}{round(color[2] * 255):02X}"


def palettes_with_colors() -> list[dict[str, Any]]:
    """Return [{"name", "colors": ["#RRGGBB", ...]}, ...] for the bundled palettes."""
    return [
        {"name": name, "colors": [_rgb_to_hex(c) for c in colors]}
        for name, colors in _bundled().items()
    ]


def resolve_palette(name: object) -> list[tuple[float, float, float]] | None:
    """
    Return the colour list for a palette name, or None for 'use music colours'.

    :param name: The selected palette name (album colours / empty / unknown -> None).
    """
    if not name or name == PALETTE_ALBUM_COLORS:
        return None
    return _bundled().get(str(name))
