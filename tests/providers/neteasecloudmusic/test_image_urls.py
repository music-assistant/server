"""Tests for image URL normalization."""

from __future__ import annotations

from typing import cast

import pytest

from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider


def _normalize(url: str) -> str:
    # _normalize_image_url does not touch instance state
    return NeteaseCloudMusicProvider._normalize_image_url(
        cast("NeteaseCloudMusicProvider", None), url
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # http links on the NCM image CDN are upgraded to https
        ("http://p1.music.126.net/abc.jpg", "https://p1.music.126.net/abc.jpg"),
        ("http://music.126.net/abc.jpg", "https://music.126.net/abc.jpg"),
        # the CDN hostname must be the actual host, not just a substring anywhere
        ("http://evil.com/music.126.net/abc.jpg", "http://evil.com/music.126.net/abc.jpg"),
        ("http://evil-music.126.net/abc.jpg", "http://evil-music.126.net/abc.jpg"),
        ("http://music.126.net.evil.com/abc.jpg", "http://music.126.net.evil.com/abc.jpg"),
        # https and non-NCM urls stay untouched
        ("https://p1.music.126.net/abc.jpg", "https://p1.music.126.net/abc.jpg"),
        ("http://example.com/abc.jpg", "http://example.com/abc.jpg"),
    ],
)
def test_normalize_image_url(url: str, expected: str) -> None:
    """Only real *.music.126.net hosts are upgraded to https."""
    assert _normalize(url) == expected
