"""Tests for the WebDAV helper functions."""

from __future__ import annotations

from typing import Any, Self, cast

import aiohttp
import pytest

from music_assistant.providers.webdav.helpers import (
    _parse_propfind_response,
    build_webdav_url,
    webdav_propfind,
)

BASE_URL = "https://host.example/remote.php/dav/files/user/Music"

EMPTY_MULTISTATUS = (
    '<?xml version="1.0" encoding="utf-8"?>\n<d:multistatus xmlns:d="DAV:"></d:multistatus>'
)


class _FakeResponse:
    """Minimal async-context-manager stand-in for an aiohttp response."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def text(self) -> str:
        return self._body


class _FakeSession:
    """Capture the headers passed to session.request for assertion."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_headers: dict[str, str] | None = None

    def request(self, _method: str, _url: str, *, headers: dict[str, str], **_kwargs: Any) -> Any:
        self.last_headers = headers
        return self._response


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


def test_propfind_parses_and_normalizes_etag() -> None:
    """PROPFIND parsing extracts the ETag and strips the weak prefix and surrounding quotes."""
    response = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/dav/Artist/Album/folder.jpg</d:href>
    <d:propstat><d:prop>
      <d:getcontentlength>2048</d:getcontentlength>
      <d:getlastmodified>Wed, 01 Jan 2025 00:00:00 GMT</d:getlastmodified>
      <d:getetag>W/"abc-123"</d:getetag>
    </d:prop></d:propstat>
  </d:response>
</d:multistatus>"""
    items = _parse_propfind_response(response, "/dav")
    assert len(items) == 1
    assert items[0].etag == "abc-123"


def test_propfind_tolerates_missing_etag() -> None:
    """A server omitting getetag yields a None ETag so the HTTP date remains the change token."""
    response = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/dav/Artist/Album/folder.jpg</d:href>
    <d:propstat><d:prop>
      <d:getlastmodified>Wed, 01 Jan 2025 00:00:00 GMT</d:getlastmodified>
    </d:prop></d:propstat>
  </d:response>
</d:multistatus>"""
    items = _parse_propfind_response(response, "/dav")
    assert len(items) == 1
    assert items[0].etag is None
    assert items[0].last_modified == "Wed, 01 Jan 2025 00:00:00 GMT"


async def test_webdav_propfind_sends_authorization_header() -> None:
    """A provided auth_header must be sent as the Authorization request header."""
    session = _FakeSession(_FakeResponse(207, EMPTY_MULTISTATUS))
    auth_header = aiohttp.encode_basic_auth("user", "pass")

    await webdav_propfind(
        cast("aiohttp.ClientSession", session),
        BASE_URL,
        depth=0,
        auth_header=auth_header,
    )

    assert session.last_headers is not None
    assert session.last_headers["Authorization"] == auth_header


async def test_webdav_propfind_omits_authorization_header_when_unset() -> None:
    """Without credentials no Authorization header must be sent."""
    session = _FakeSession(_FakeResponse(207, EMPTY_MULTISTATUS))

    await webdav_propfind(
        cast("aiohttp.ClientSession", session),
        BASE_URL,
        depth=0,
        auth_header=None,
    )

    assert session.last_headers is not None
    assert "Authorization" not in session.last_headers
