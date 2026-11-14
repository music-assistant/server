"""Test the station logos of the ORF Radiothek provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from music_assistant.providers.orf_radiothek import LOCAL_IMG_PREFIX

from .test_catchup import _provider


async def test_resolve_image_returns_the_logo_bytes() -> None:
    """A bundled station logo resolves to its image data."""
    provider = _provider()
    logo = Path(provider._media_dir / "oe1.png").read_bytes()

    assert await provider.resolve_image(f"{LOCAL_IMG_PREFIX}oe1.png") == logo


@pytest.mark.parametrize("path", [f"{LOCAL_IMG_PREFIX}oe1.png", f"{LOCAL_IMG_PREFIX}fm4.png"])
def test_accepts_stored_image_path_station_logos(path: str) -> None:
    """Bundled station logo paths are accepted as stored image paths."""
    assert _provider().accepts_stored_image_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "oe1.png",
        LOCAL_IMG_PREFIX,
        f"{LOCAL_IMG_PREFIX}../oe1.png",
        f"{LOCAL_IMG_PREFIX}x/oe1.png",
        f"{LOCAL_IMG_PREFIX}x\\oe1.png",
        f"{LOCAL_IMG_PREFIX}oe1.png\n",
        f"{LOCAL_IMG_PREFIX}oe1.png?x",
        f"{LOCAL_IMG_PREFIX}/etc/passwd",
        f"{LOCAL_IMG_PREFIX}oe1.jpg",
        "/etc/passwd",
        "@evil.com/x",
        "//evil.com/x",
        "http://evil/x.png",
    ],
)
def test_accepts_stored_image_path_refuses_paths(path: str) -> None:
    """Anything but a bundled station logo path is refused."""
    assert not _provider().accepts_stored_image_path(path)
