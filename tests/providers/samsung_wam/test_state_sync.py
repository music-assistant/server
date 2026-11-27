"""Tests for mapping the state of a Samsung WAM speaker onto its player."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from music_assistant.providers.samsung_wam.features.state_sync.mapper import StateSyncMapper
from music_assistant.providers.samsung_wam.features.state_sync.models import (
    WamSpeakerAttributes,
)
from music_assistant.providers.samsung_wam.player import WamPlayer


def test_aux_and_optical_are_marked_as_inputs() -> None:
    """AUX and Optical are inputs; Wi-Fi, Bluetooth and TV SoundConnect are not."""
    player = MagicMock()
    attributes = WamSpeakerAttributes(
        mac="AA:BB:CC:DD:EE:FF",
        name="Speaker",
        model=None,
        software_version=None,
        source="Wi-Fi",
        source_list=["Wi-Fi", "Bluetooth", "AUX", "Optical", "TV SoundConnect"],
        group_leader_mac=None,
        is_group_child=False,
        volume=None,
        muted=None,
        play_status=None,
    )

    StateSyncMapper.apply_attributes_to_player(
        cast("WamPlayer", player), attributes, group_children=set(), stream_active=False
    )

    assert {x.id: x.is_input for x in player._attr_source_list} == {
        "Wi-Fi": False,
        "Bluetooth": False,
        "AUX": True,
        "Optical": True,
        "TV SoundConnect": False,
    }
