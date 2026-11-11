"""Player configuration for low-latency Station streaming."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.config_entries import PlayerConfig

from music_assistant.constants import CONF_ENTRY_OUTPUT_CODEC
from music_assistant.providers.yandex_station.player import YandexStationPlayer

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


def _make_player() -> YandexStationPlayer:
    """Build a player with no available intercept targets."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._player_id = "test_station"
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(players=SimpleNamespace(all_players=lambda **_kwargs: [])),
    )
    return player


async def test_new_station_uses_wav_for_queue_and_live_sources() -> None:
    """An unconfigured Station uses the measured low-latency streaming profile."""
    entries = await _make_player().get_config_entries()
    config = PlayerConfig.parse(
        entries,
        {"provider": "yandex_station", "player_id": "test_station", "values": {}},
    )

    assert config.get_value("output_codec") == "wav"
    assert config.get_value("prefer_wav_for_live_sources") is True
    assert config.get_value("http_profile") == "forced_content_length"
    assert CONF_ENTRY_OUTPUT_CODEC.default_value == "flac"


@pytest.mark.parametrize("codec", ["flac", "mp3", "aac", "wav"])
async def test_saved_stream_preferences_override_station_defaults(codec: str) -> None:
    """Existing explicit choices remain effective after updating the provider."""
    entries = await _make_player().get_config_entries()
    config = PlayerConfig.parse(
        entries,
        {
            "provider": "yandex_station",
            "player_id": "test_station",
            "values": {"output_codec": codec, "prefer_wav_for_live_sources": False},
        },
    )

    assert config.get_value("output_codec") == codec
    assert config.get_value("prefer_wav_for_live_sources") is False
