"""Tests for the config entries a Cast player offers."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pychromecast.const import CAST_TYPE_AUDIO, CAST_TYPE_CHROMECAST, CAST_TYPE_GROUP

from music_assistant.constants import CONF_FLOW_MODE, PlaybackQuality
from music_assistant.providers.chromecast.helpers import ChromecastInfo
from music_assistant.providers.chromecast.player import ChromecastPlayer

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


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
    return ChromecastPlayer(MagicMock(domain="chromecast"), str(cast_info.uuid), cast_info)


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
    ("model_name", "cast_type", "manufacturer"),
    [
        ("Chromecast Audio", CAST_TYPE_AUDIO, "Google Inc."),
        ("Google Nest Hub", CAST_TYPE_CHROMECAST, "Google Inc."),
        ("Google Nest Mini", CAST_TYPE_AUDIO, "Google Inc."),
        ("LS60D", CAST_TYPE_AUDIO, "Samsung"),
        ("Google Cast Group", CAST_TYPE_GROUP, "Google Inc."),
    ],
    ids=["chromecast-audio", "nest-hub", "nest-mini", "samsung-music-frame", "group"],
)
async def test_playback_quality_default(model_name: str, cast_type: str, manufacturer: str) -> None:
    """Every Cast device defaults to compressed."""
    player = _player(model_name, cast_type, manufacturer)

    assert player.default_playback_quality is PlaybackQuality.COMPRESSED


@pytest.mark.parametrize(
    ("model_name", "cast_type"),
    [("Google Nest Mini", CAST_TYPE_AUDIO), ("Google Cast Group", CAST_TYPE_GROUP)],
    ids=["device", "group"],
)
async def test_flow_mode_is_offered_and_off_by_default(
    mass_minimal: MusicAssistant, model_name: str, cast_type: str
) -> None:
    """Cast devices and groups get the one flow mode setting, off unless the user turns it on."""
    player = _player(model_name, cast_type)

    entries = await mass_minimal.config._get_protocol_config_entries(player)
    flow_mode_entries = [entry for entry in entries if entry.key == CONF_FLOW_MODE]

    assert len(flow_mode_entries) == 1
    assert flow_mode_entries[0].default_value is False
