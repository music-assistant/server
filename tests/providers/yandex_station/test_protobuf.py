"""Tests for protobuf encoding/decoding."""

from __future__ import annotations

import base64
import importlib
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol, cast

from music_assistant.providers.yandex_station.protobuf import dumps, loads

if TYPE_CHECKING:
    from music_assistant.models.player import PlayerMedia


class _StreamCommand(Protocol):
    """Typed contract for the private stream-command test target."""

    def __call__(
        self,
        url: str,
        media: PlayerMedia | None,
        *,
        audio_client: bool,
    ) -> dict[str, object]: ...


_player_module = importlib.import_module("music_assistant.providers.yandex_station.player")
_stream_command = cast("_StreamCommand", _player_module._stream_command)


def _decode_external(command: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Decode the real externalCommandBypass wire payload."""
    assert command["command"] == "externalCommandBypass"
    encoded = command["data"]
    assert isinstance(encoded, str)
    decoded = loads(base64.b64decode(encoded))
    name = decoded[1]
    payload = decoded[2]
    assert isinstance(name, bytes)
    assert isinstance(payload, bytes)
    return name.decode(), json.loads(payload)


def test_roundtrip_simple() -> None:
    """Test that encoding and decoding a simple dict is identity."""
    data = {1: "radio_play", 2: '{"streamUrl": "http://example.com/stream.flac"}'}
    encoded = dumps(data)
    decoded = loads(encoded)
    assert decoded[1] == b"radio_play"
    val = decoded[2]
    assert isinstance(val, (bytes, bytearray, memoryview))
    assert b"streamUrl" in bytes(val)


def test_dumps_produces_bytes() -> None:
    """Test that dumps returns bytes."""
    result = dumps({1: "test"})
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_audio_play_track_payload() -> None:
    """Current firmware must receive the required audio_client stream fields."""
    media = cast(
        "PlayerMedia",
        SimpleNamespace(
            title="Track title",
            artist="Track artist",
            image_url="https://images.example/cover.jpg",
        ),
    )

    name, payload = _decode_external(
        _stream_command("http://192.168.1.10:8097/item.flac", media, audio_client=True)
    )

    assert name == "audio_play"
    assert payload == {
        "stream": {
            "url": "http://192.168.1.10:8097/item.flac",
            "format": "MP3",
            "type": "Track",
            "offset_ms": 0,
        },
        "metadata": {
            "title": "Track title",
            "subtitle": "Track artist",
            "art_image_url": "images.example/cover.jpg",
        },
        "set_pause": False,
    }


def test_audio_play_hls_uses_url_path_extension() -> None:
    """HLS detection must ignore query parameters and select the radio UI."""
    name, payload = _decode_external(
        _stream_command(
            "http://192.168.1.10:8097/live/playlist.m3u8?token=abc",
            None,
            audio_client=True,
        )
    )

    assert name == "audio_play"
    assert payload == {
        "stream": {
            "url": "http://192.168.1.10:8097/live/playlist.m3u8?token=abc",
            "format": "HLS",
            "type": "FmRadio",
            "offset_ms": 0,
        },
        "set_pause": False,
    }


def test_audio_play_keeps_non_https_artwork_unchanged() -> None:
    """Only the HTTPS scheme is removed from artwork returned as coverURI."""
    media = cast(
        "PlayerMedia",
        SimpleNamespace(title="Track", artist="", image_url="http://images.example/a.jpg"),
    )

    _, payload = _decode_external(
        _stream_command("http://192.168.1.10/item.mp3", media, audio_client=True)
    )

    assert payload["metadata"] == {
        "title": "Track",
        "art_image_url": "http://images.example/a.jpg",
    }


def test_legacy_stream_payload() -> None:
    """Stations without audio_client retain the compatible radio_play payload."""
    name, payload = _decode_external(
        _stream_command("http://192.168.1.10:8097/item.flac", None, audio_client=False)
    )

    assert name == "radio_play"
    assert payload == {
        "streamUrl": "http://192.168.1.10:8097/item.flac",
        "force_restart_player": True,
    }
