"""Tests for the builtin provider's resolve_image path handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from music_assistant.constants import CUSTOM_IMAGES_DIRNAME
from music_assistant.providers.builtin import BuiltinProvider


def _provider(tmp_path: Path) -> BuiltinProvider:
    """Build a BuiltinProvider stub with resolve_image bound and a tmp storage path."""
    provider = MagicMock()
    provider.mass.storage_path = str(tmp_path)
    provider.resolve_image = BuiltinProvider.resolve_image.__get__(provider, BuiltinProvider)
    return provider


async def test_resolve_custom_image_returns_bytes(tmp_path: Path) -> None:
    """A stored custom image resolves to its raw file bytes."""
    images_dir = tmp_path / CUSTOM_IMAGES_DIRNAME
    images_dir.mkdir()
    (images_dir / "genre.1.abcd1234.webp").write_bytes(b"webp-bytes")
    provider = _provider(tmp_path)
    result = await provider.resolve_image(f"{CUSTOM_IMAGES_DIRNAME}/genre.1.abcd1234.webp")
    assert result == b"webp-bytes"


async def test_resolve_custom_image_blocks_traversal(tmp_path: Path) -> None:
    """A traversal attempt outside the custom images dir is rejected."""
    images_dir = tmp_path / CUSTOM_IMAGES_DIRNAME
    images_dir.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    provider = _provider(tmp_path)
    with pytest.raises(FileNotFoundError):
        await provider.resolve_image(f"{CUSTOM_IMAGES_DIRNAME}/../secret.txt")


async def test_resolve_missing_custom_image_raises(tmp_path: Path) -> None:
    """A dangling custom image reference raises FileNotFoundError."""
    provider = _provider(tmp_path)
    with pytest.raises(FileNotFoundError):
        await provider.resolve_image(f"{CUSTOM_IMAGES_DIRNAME}/genre.1.gone.png")
