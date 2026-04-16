"""Tests for My Wave (Моя волна) browse and rotor feedback helpers."""

from __future__ import annotations

from music_assistant.providers.yandex_music.constants import (
    RADIO_TRACK_ID_SEP,
    ROTOR_STATION_MY_WAVE,
)
from music_assistant.providers.yandex_music.provider import _parse_radio_item_id


def test_parse_radio_item_id_plain_track_id() -> None:
    """Plain track_id returns (track_id, None)."""
    assert _parse_radio_item_id("12345") == ("12345", None)
    assert _parse_radio_item_id("0") == ("0", None)


def test_parse_radio_item_id_composite() -> None:
    """Composite track_id@station_id returns (track_id, station_id)."""
    assert _parse_radio_item_id(f"12345{RADIO_TRACK_ID_SEP}{ROTOR_STATION_MY_WAVE}") == (
        "12345",
        ROTOR_STATION_MY_WAVE,
    )
    assert _parse_radio_item_id("99@user:custom") == ("99", "user:custom")
