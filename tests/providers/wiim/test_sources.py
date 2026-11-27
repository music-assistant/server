"""Tests for the WiiM source resolution (device inputs and external streaming services)."""

from __future__ import annotations

import pytest

from music_assistant.providers.wiim.constants import (
    SOURCE_AIRPLAY,
    SOURCE_EXTERNAL,
    SOURCE_SPOTIFY,
    SOURCE_UNKNOWN,
)
from music_assistant.providers.wiim.sources import (
    external_source_for,
    input_source_for_mode,
    input_source_for_play_mode,
    is_own_stream,
    resolve_active_source,
)

QUEUE_ID = "wiim_test_queue"
OWN_HOSTS = {"192.168.2.62", "127.0.0.1", "localhost"}
MA_STREAM_URI = "http://192.168.2.62:8097/single/wiim_test_queue/abc.flac"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("43", "optical"),
        ("6", "optical"),
        ("40", "line_in"),
        ("60", "line_in"),
        ("41", "bluetooth"),
        ("5", "bluetooth"),
        ("49", "hdmi"),
        ("54", "phono_in"),
        ("44", "aux_in"),
        ("10", None),
        ("3", None),
        ("31", None),
        ("", None),
        (None, None),
    ],
)
def test_input_source_for_mode(mode: str | None, expected: str | None) -> None:
    """A physical input mode resolves to its source, a network mode does not."""
    assert input_source_for_mode(mode) == expected


@pytest.mark.parametrize(
    ("play_mode", "expected"),
    [
        ("Optical In", "optical"),
        ("Line In", "line_in"),
        ("Bluetooth", "bluetooth"),
        ("TV", "hdmi"),
        ("Phono In", "phono_in"),
        ("Network", None),
        ("", None),
        (None, None),
    ],
)
def test_input_source_for_play_mode(play_mode: str | None, expected: str | None) -> None:
    """The SDK play mode resolves to the same source ids as the raw modes."""
    assert input_source_for_play_mode(play_mode) == expected


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (MA_STREAM_URI, True),
        ("http://localhost:8097/stream/1", True),
        ("http://192.168.2.62:8095/other", True),
        ("http://192.168.2.99:8097/single/x", False),
        ("https://sp-pr-fa.audio.tidal.com/mediatracks/1.flac", False),
        ("spotify:track:1234", False),
        ("", False),
        (None, False),
    ],
)
def test_is_own_stream(uri: str | None, expected: bool) -> None:
    """Only URLs on the MA server's own host count as our stream."""
    assert is_own_stream(uri, OWN_HOSTS) is expected


@pytest.mark.parametrize(
    ("vendor", "uri", "expected"),
    [
        ("Tidal", None, "tidal"),
        ("tidal", None, "tidal"),
        ("Qobuz", None, "qobuz"),
        (None, "https://resources.tidal.com/images/1.jpg", "tidal"),
        (None, "https://cdn.qobuz.com/track/1.flac", "qobuz"),
        (None, "https://something.example.com/track.flac", None),
        (None, None, None),
    ],
)
def test_external_source_for(vendor: str | None, uri: str | None, expected: str | None) -> None:
    """An external session is labelled with its service when the device reveals it."""
    assert external_source_for(vendor, uri) == expected


def test_resolve_input_wins_over_stale_queue() -> None:
    """An optical input stays visible while the MA queue still holds the previously played item."""
    assert (
        resolve_active_source(
            play_mode="Network",
            mode="43",
            vendor=None,
            uri="",
            own_hosts=OWN_HOSTS,
            queue_id=QUEUE_ID,
            queue_has_item=True,
        )
        == "optical"
    )


def test_resolve_external_service_is_not_the_queue() -> None:
    """A Tidal Connect stream is reported as Tidal, never as Music Assistant's own queue."""
    assert (
        resolve_active_source(
            play_mode="Network",
            mode="10",
            vendor="Tidal",
            uri="https://sp-pr-fa.audio.tidal.com/mediatracks/1.flac",
            own_hosts=OWN_HOSTS,
            queue_id=QUEUE_ID,
            queue_has_item=True,
        )
        == "tidal"
    )


def test_resolve_unknown_external_session() -> None:
    """An external stream whose service is unknown still never claims the MA queue."""
    assert (
        resolve_active_source(
            play_mode="Network",
            mode="10",
            vendor=None,
            uri="https://media.example.com/live.flac",
            own_hosts=OWN_HOSTS,
            queue_id=QUEUE_ID,
            queue_has_item=True,
        )
        == SOURCE_EXTERNAL
    )


def test_resolve_own_stream_is_the_queue() -> None:
    """A stream URL of the MA server resolves to the queue of that player."""
    assert (
        resolve_active_source(
            play_mode="Network",
            mode="10",
            vendor=None,
            uri=MA_STREAM_URI,
            own_hosts=OWN_HOSTS,
            own_stream_uri=True,
            queue_id=QUEUE_ID,
            queue_has_item=True,
        )
        == QUEUE_ID
    )


def test_resolve_own_stream_by_host() -> None:
    """A stream of our own host is ours even when the start URL is no longer known (restart)."""
    assert (
        resolve_active_source(
            play_mode="Network",
            mode="10",
            vendor=None,
            uri="http://192.168.2.62:8097/single/x/next.flac",
            own_hosts=OWN_HOSTS,
            queue_id=QUEUE_ID,
            queue_has_item=True,
        )
        == QUEUE_ID
    )


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("wiimu_airplay", SOURCE_AIRPLAY),
        ("spotify:track:0VjIjW4GlUZAMYd2vXMi3b", SOURCE_SPOTIFY),
    ],
)
def test_resolve_passive_sentinel_uris(uri: str, expected: str) -> None:
    """The SDK's sentinel URIs for AirPlay and Spotify Connect keep their own source."""
    assert (
        resolve_active_source(
            play_mode="Network",
            mode="10",
            vendor=None,
            uri=uri,
            own_hosts=OWN_HOSTS,
            queue_id=QUEUE_ID,
            queue_has_item=True,
        )
        == expected
    )


def test_resolve_ack_window_keeps_the_queue() -> None:
    """While the device has not loaded our stream yet, the queue item stays the active source."""
    assert (
        resolve_active_source(
            play_mode="Network",
            mode="10",
            vendor=None,
            uri="",
            own_hosts=OWN_HOSTS,
            queue_id=QUEUE_ID,
            queue_has_item=True,
        )
        == QUEUE_ID
    )


def test_resolve_nothing_loaded() -> None:
    """A device with nothing loaded and an empty queue reports the unknown source."""
    assert (
        resolve_active_source(
            play_mode=None,
            mode="0",
            vendor=None,
            uri="",
            own_hosts=OWN_HOSTS,
            queue_id=QUEUE_ID,
            queue_has_item=False,
        )
        == SOURCE_UNKNOWN
    )


def test_resolve_external_service_without_uri() -> None:
    """A Tidal Connect session exposes no URI at all on the device, only its vendor."""
    assert (
        resolve_active_source(
            play_mode="Network",
            mode="10",
            vendor="Tidal",
            uri="",
            own_hosts=OWN_HOSTS,
            queue_id=QUEUE_ID,
            queue_has_item=True,
        )
        == "tidal"
    )


def test_resolve_own_stream_wins_over_the_vendor() -> None:
    """Music Assistant playing Tidal reports vendor Tidal too, but its own stream still wins."""
    assert (
        resolve_active_source(
            play_mode="Network",
            mode="10",
            vendor="Tidal",
            uri=MA_STREAM_URI,
            own_hosts=OWN_HOSTS,
            own_stream_uri=True,
            queue_id=QUEUE_ID,
            queue_has_item=True,
        )
        == QUEUE_ID
    )
