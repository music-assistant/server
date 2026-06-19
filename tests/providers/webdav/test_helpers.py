"""Tests for the WebDAV helper functions."""

from __future__ import annotations

import pytest

from music_assistant.providers.webdav.helpers import build_webdav_url

BASE_URL = "https://host.example/remote.php/dav/files/user/Music"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Artist/Album", f"{BASE_URL}/Artist/Album"),
        ("/Artist/Album", f"{BASE_URL}/Artist/Album"),
        # reserved characters must be percent-encoded, not interpreted as
        # params/query/fragment/scheme by the URL machinery
        ("Live; Unplugged", f"{BASE_URL}/Live%3B%20Unplugged"),
        ("Die drei ???", f"{BASE_URL}/Die%20drei%20%3F%3F%3F"),
        ("Rock #1", f"{BASE_URL}/Rock%20%231"),
        ("Live: In Concert", f"{BASE_URL}/Live%3A%20In%20Concert"),
        # the path separator and umlauts are handled as expected
        ("Sigur Rós/Ágætis", f"{BASE_URL}/Sigur%20R%C3%B3s/%C3%81g%C3%A6tis"),
    ],
)
def test_build_webdav_url_encodes_reserved_characters(path: str, expected: str) -> None:
    """Reserved characters in resource paths must be percent-encoded."""
    assert build_webdav_url(BASE_URL, path) == expected


def test_build_webdav_url_passes_through_absolute_urls() -> None:
    """An absolute URL (e.g. from a playlist line) must be returned unchanged."""
    absolute = "http://other.example/song.mp3"
    assert build_webdav_url(BASE_URL, absolute) == absolute
