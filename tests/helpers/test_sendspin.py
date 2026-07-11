"""Tests for Sendspin protocol helpers."""

from __future__ import annotations

import json

from music_assistant.helpers.sendspin import (
    filter_audio_only_sendspin_message,
    get_sendspin_client_id,
)


def test_get_sendspin_client_id() -> None:
    """Client IDs are accepted from proxy authentication and protocol hello messages."""
    assert get_sendspin_client_id('{"type":"auth","client_id":"web_player"}') == "web_player"
    assert (
        get_sendspin_client_id('{"type":"client/hello","payload":{"client_id":"web_player"}}')
        == "web_player"
    )
    assert get_sendspin_client_id('{"type":"client/time"}') is None


def test_audio_only_state_keeps_controller_controls() -> None:
    """Metadata and color are removed while controller state remains available."""
    message = json.dumps(
        {
            "type": "server/state",
            "payload": {
                "metadata": {
                    "title": "Answer",
                    "artist": "Artist",
                    "album": "Album",
                    "year": 1999,
                    "artwork_url": "https://example.test/cover.jpg",
                },
                "controller": {"volume": 50, "muted": False},
                "color": {"primary": [1, 2, 3]},
            },
        }
    )

    filtered = filter_audio_only_sendspin_message(message)

    assert isinstance(filtered, str)
    assert json.loads(filtered) == {
        "type": "server/state",
        "payload": {"controller": {"volume": 50, "muted": False}},
    }


def test_audio_only_metadata_only_state_is_dropped() -> None:
    """Metadata catch-up and update messages are not forwarded."""
    message = '{"type":"server/state","payload":{"metadata":{"title":"Answer"}}}'

    assert filter_audio_only_sendspin_message(message) is None


def test_audio_only_stream_start_keeps_player_only() -> None:
    """Artwork and visualizer stream configuration is removed from audio stream setup."""
    message = json.dumps(
        {
            "type": "stream/start",
            "payload": {
                "player": {"codec": "opus", "sample_rate": 48000},
                "artwork": {"channels": [{"source": "album"}]},
                "visualizer": {"types": ["spectrum"]},
            },
        }
    )

    filtered = filter_audio_only_sendspin_message(message)

    assert isinstance(filtered, str)
    assert json.loads(filtered) == {
        "type": "stream/start",
        "payload": {"player": {"codec": "opus", "sample_rate": 48000}},
    }


def test_audio_only_binary_allows_only_audio_chunks() -> None:
    """Only complete audio chunk messages pass the binary filter."""
    audio = bytes([4]) + b"\0" * 8 + b"audio"
    artwork = bytes([8]) + b"\0" * 8 + b"artwork"
    visualizer = bytes([16]) + b"\0" * 8 + b"visualizer"

    assert filter_audio_only_sendspin_message(audio) == audio
    assert filter_audio_only_sendspin_message(artwork) is None
    assert filter_audio_only_sendspin_message(visualizer) is None
    assert filter_audio_only_sendspin_message(bytes([4])) is None
