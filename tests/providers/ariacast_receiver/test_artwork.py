"""Tests for the artwork a sender may make the AriaCast receiver fetch."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.streamdetails import StreamMetadata
from yarl import URL

from music_assistant.providers.ariacast_receiver import MAX_ARTWORK_BYTES, AriaCastReceiver

SENDER = "192.168.1.10"


class _FakeContent:
    """Response body that hands out at most the asked-for number of bytes, like aiohttp does."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self, n: int = -1) -> bytes:
        """Return up to n bytes of the payload."""
        return self._payload if n < 0 else self._payload[:n]


class _FakeResponse:
    """Async context manager standing in for an aiohttp response."""

    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self.content = _FakeContent(payload)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False


def _fetcher(payload: bytes = b"artwork-bytes", status: int = 200) -> SimpleNamespace:
    """Build a bare receiver namespace whose HTTP session serves the given response."""
    mass = MagicMock()
    mass.http_session.get = MagicMock(return_value=_FakeResponse(status, payload))
    mass.metadata.get_image_url.return_value = "http://music-assistant/image"
    return SimpleNamespace(
        mass=mass,
        logger=MagicMock(),
        instance_id="ariacast_receiver",
        _artwork_bytes=None,
        _stream_meta=StreamMetadata(title="Test Track"),
        _broadcast_meta=AsyncMock(),
    )


def _receiver(last_artwork_url: str | None = None) -> SimpleNamespace:
    """Build a bare receiver namespace for driving _apply_meta."""
    return SimpleNamespace(
        mass=MagicMock(),
        logger=MagicMock(),
        instance_id="ariacast_receiver",
        _stream_meta=StreamMetadata(title="Test Track", image_url="http://music-assistant/old"),
        _artwork_bytes=b"previous-artwork",
        _last_artwork_url=last_artwork_url,
        # a plain mock, as _apply_meta hands the call straight to create_task
        _fetch_artwork=MagicMock(),
        _broadcast_meta=AsyncMock(),
    )


async def _apply(receiver: SimpleNamespace, artwork: Any, sender: str | None = SENDER) -> None:
    """Merge an artwork-carrying update from the given peer."""
    data: dict[str, Any] = {"artworkUrl": artwork}
    await AriaCastReceiver._apply_meta(cast("AriaCastReceiver", receiver), data, sender)


async def _fetch(fetcher: SimpleNamespace, url: URL) -> None:
    """Run the artwork fetch on the bare receiver."""
    await AriaCastReceiver._fetch_artwork(cast("AriaCastReceiver", fetcher), url)


async def test_artwork_on_the_senders_own_host_is_fetched() -> None:
    """The sender's own HTTP server is the one place artwork is allowed to live."""
    receiver = _receiver()

    await _apply(receiver, f"http://{SENDER}:8080/artwork.jpg")

    assert receiver._fetch_artwork.call_args.args == (URL(f"http://{SENDER}:8080/artwork.jpg"),)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/artwork.jpg",
        "http://127.0.0.1:8095/api/",
        "http://169.254.169.254/latest/meta-data/",
        # a host that merely starts with the sender address must not pass either
        "http://192.168.1.100/artwork.jpg",
        # the sender address as userinfo does not make the host the sender
        f"http://{SENDER}@evil.example.com/artwork.jpg",
        # a hostname is refused outright rather than resolved
        "http://localhost/artwork.jpg",
        "http://sender.local:8080/artwork.jpg",
    ],
)
async def test_artwork_on_a_foreign_host_is_not_fetched(url: str) -> None:
    """Any host other than the sender is refused, so the LAN cannot aim us at it."""
    receiver = _receiver()

    await _apply(receiver, url)

    receiver._fetch_artwork.assert_not_called()


@pytest.mark.parametrize("url", ["file:///etc/passwd", f"ftp://{SENDER}/artwork.jpg", "/artwork"])
async def test_artwork_outside_http_is_not_fetched(url: str) -> None:
    """Only http(s) is fetched, so a URL cannot reach the local filesystem."""
    receiver = _receiver()

    await _apply(receiver, url)

    receiver._fetch_artwork.assert_not_called()


async def test_artwork_without_a_known_sender_is_not_fetched() -> None:
    """A peer address we could not determine fails closed."""
    receiver = _receiver()

    await _apply(receiver, f"http://{SENDER}/artwork.jpg", sender=None)

    receiver._fetch_artwork.assert_not_called()


@pytest.mark.parametrize("artwork", [1234, {"url": "http://192.168.1.10/a.jpg"}, None])
async def test_artwork_that_is_not_a_string_is_ignored(artwork: Any) -> None:
    """A non-string artwork value is dropped rather than raised on in the background."""
    receiver = _receiver()

    await _apply(receiver, artwork)

    receiver._fetch_artwork.assert_not_called()


async def test_a_refused_url_leaves_the_current_artwork_alone() -> None:
    """
    A refused URL changes nothing, so a hostile peer cannot blank the artwork.

    Recording it as the last seen URL would also let that peer suppress the very
    same URL when the real sender pushes it afterwards.
    """
    receiver = _receiver()

    await _apply(receiver, "http://10.0.0.5/artwork.jpg")

    assert receiver._artwork_bytes == b"previous-artwork"
    assert receiver._stream_meta.image_url == "http://music-assistant/old"
    assert receiver._last_artwork_url is None


async def test_artwork_url_with_smuggled_userinfo_never_reaches_a_foreign_host() -> None:
    """
    Userinfo tricks cannot smuggle a foreign host past the check.

    yarl versions disagree on this URL: newer ones refuse the backslash outright,
    older ones read the host as the sender. Either way the foreign host stays out,
    and that is the property pinned here.
    """
    receiver = _receiver()

    await _apply(receiver, f"http://evil.example.com\\@{SENDER}/artwork.jpg")

    calls = receiver._fetch_artwork.call_args_list
    assert all(call.args[0].host == SENDER for call in calls)


async def test_artwork_path_parameters_survive_validation() -> None:
    """A ;params suffix belongs to the path, so the accepted URL keeps it."""
    receiver = _receiver()

    await _apply(receiver, f"http://{SENDER}/artwork;version=2.jpg?size=large")

    assert receiver._fetch_artwork.call_args.args == (
        URL(f"http://{SENDER}/artwork;version=2.jpg?size=large"),
    )


async def test_artwork_on_an_ipv6_sender_is_fetched() -> None:
    """An IPv6 peer is matched against the URL host, compression differences included."""
    receiver = _receiver()

    await _apply(receiver, "http://[fd00:0:0::1]:8080/artwork.jpg", sender="fd00::1")

    assert receiver._fetch_artwork.call_args.args == (URL("http://[fd00::1]:8080/artwork.jpg"),)


async def test_a_new_artwork_url_supersedes_the_one_in_flight() -> None:
    """
    Only the latest artwork fetch stays alive, since the artwork is single-valued.

    A peer feeding a fresh URL per update would otherwise stack up fetches, and the
    4 MB cap only bounds them one at a time.
    """
    receiver = _receiver()

    await _apply(receiver, f"http://{SENDER}/one.jpg")
    await _apply(receiver, f"http://{SENDER}/two.jpg")

    calls = receiver.mass.create_task.call_args_list
    assert len(calls) == 2
    assert len({call.kwargs["task_id"] for call in calls}) == 1
    assert all(call.kwargs["abort_existing"] for call in calls)


async def test_fetched_artwork_is_cached_and_published() -> None:
    """A fetched image is cached and its MA image URL published on the metadata."""
    fetcher = _fetcher()

    await _fetch(fetcher, URL(f"http://{SENDER}/artwork.jpg"))

    assert fetcher._artwork_bytes == b"artwork-bytes"
    assert fetcher._stream_meta.image_url == "http://music-assistant/image"


async def test_artwork_fetch_does_not_follow_redirects() -> None:
    """A permitted host must not be able to bounce the request to another one."""
    fetcher = _fetcher()

    await _fetch(fetcher, URL(f"http://{SENDER}/artwork.jpg"))

    assert fetcher.mass.http_session.get.call_args.kwargs["allow_redirects"] is False


async def test_oversized_artwork_is_discarded() -> None:
    """An artwork URL cannot be used to feed us an unbounded body."""
    fetcher = _fetcher(payload=b"x" * (MAX_ARTWORK_BYTES + 1))

    await _fetch(fetcher, URL(f"http://{SENDER}/artwork.jpg"))

    assert fetcher._artwork_bytes is None
