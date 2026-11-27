"""Tests for the config entries a Cast player offers."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_CHROMECAST, CAST_TYPE_GROUP

from music_assistant.constants import CONF_OUTPUT_CODEC
from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.player import ChromecastPlayer


def _player(model_name: str, cast_type: str, manufacturer: str = "Google Inc.") -> ChromecastPlayer:
    """
    Build a Cast player for a device of the given model.

    :param model_name: Model name the device reports.
    :param cast_type: Cast type of the device (audio, cast or group).
    :param manufacturer: Manufacturer the device reports.
    """
    cast_info = ChromecastInfo(
        services=set(),
        uuid=uuid4(),
        model_name=model_name,
        friendly_name=model_name,
        host="10.0.0.10",
        port=8009,
        cast_type=cast_type,
        manufacturer=manufacturer,
    )
    return ChromecastPlayer(MagicMock(), str(cast_info.uuid), cast_info)


@pytest.mark.parametrize(
    ("model_name", "cast_type"),
    [("Google Nest Mini", CAST_TYPE_AUDIO), ("Google Cast Group", CAST_TYPE_GROUP)],
    ids=["device", "group"],
)
def test_no_content_length_required(model_name: str, cast_type: str) -> None:
    """Cast receivers play fine without a content length, so their streams declare none."""
    player = _player(model_name, cast_type)

    assert player.requires_content_length is False


@pytest.mark.parametrize(
    ("model_name", "cast_type"),
    [("Google Nest Mini", CAST_TYPE_AUDIO), ("Google Cast Group", CAST_TYPE_GROUP)],
    ids=["device", "group"],
)
def test_a_cast_player_fetches_files(model_name: str, cast_type: str) -> None:
    """Every Cast receiver is built around files, so no setting has to tell."""
    assert _player(model_name, cast_type).fetches_files is True


@pytest.mark.parametrize(
    ("model_name", "cast_type", "manufacturer", "expected_codec"),
    [
        ("Chromecast Audio", CAST_TYPE_AUDIO, "Google Inc.", "flac"),
        ("Google Nest Hub", CAST_TYPE_CHROMECAST, "Google Inc.", "flac"),
        ("Google Nest Mini", CAST_TYPE_AUDIO, "Google Inc.", "mp3"),
        ("LS60D", CAST_TYPE_AUDIO, "Samsung", "mp3"),
        ("Google Cast Group", CAST_TYPE_GROUP, "Google Inc.", "mp3"),
    ],
    ids=["chromecast-audio", "nest-hub", "nest-mini", "samsung-music-frame", "group"],
)
async def test_output_codec_default(
    model_name: str, cast_type: str, manufacturer: str, expected_codec: str
) -> None:
    """Cast devices default to MP3, except the models known to play long lossless streams."""
    player = _player(model_name, cast_type, manufacturer)

    entries = await player.get_config_entries()

    codec_entry = next(entry for entry in entries if entry.key == CONF_OUTPUT_CODEC)
    assert codec_entry.default_value == expected_codec
