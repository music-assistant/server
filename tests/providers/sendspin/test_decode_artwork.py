"""Tests for Sendspin artwork decoding resilience."""

from __future__ import annotations

import logging
from io import BytesIO
from types import SimpleNamespace
from typing import cast

from PIL import Image

from music_assistant.providers.sendspin.player import SendspinPlayer


def _fake_player() -> SendspinPlayer:
    return cast("SendspinPlayer", SimpleNamespace(logger=logging.getLogger("test")))


def _png_bytes(size: tuple[int, int] = (4, 4)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(buf, "PNG")
    return buf.getvalue()


async def test_decode_artwork_valid_image() -> None:
    """A valid raster image decodes to a Pillow image."""
    img = await SendspinPlayer._decode_artwork(_fake_player(), _png_bytes())
    assert img is not None
    assert img.size == (4, 4)


async def test_decode_artwork_returns_none_for_unidentifiable() -> None:
    """SVG / arbitrary / empty bytes return None instead of raising."""
    assert await SendspinPlayer._decode_artwork(_fake_player(), b"<svg></svg>") is None
    assert await SendspinPlayer._decode_artwork(_fake_player(), b"") is None


async def test_decode_artwork_returns_none_for_truncated_image() -> None:
    """Truncated data opens lazily but fails on decode; it must be caught, not raised."""
    full = _png_bytes((64, 64))
    truncated = full[: len(full) // 2]
    assert await SendspinPlayer._decode_artwork(_fake_player(), truncated) is None
