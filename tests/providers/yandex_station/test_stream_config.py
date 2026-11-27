"""Player configuration for low-latency Station streaming."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.enums import ConfigEntryType, ContentType

from music_assistant.constants import CONF_PLAYBACK_QUALITY
from music_assistant.providers.yandex_station.player import YandexStationPlayer

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

# the playback quality entry the config controller adds for the Station
PLAYBACK_QUALITY_ENTRY = ConfigEntry(key=CONF_PLAYBACK_QUALITY, type=ConfigEntryType.STRING)


def _make_player() -> YandexStationPlayer:
    """Build a player with no available intercept targets."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._protocol_id = "test_station"
    player._player = None
    player.mass = cast(
        "MusicAssistant",
        SimpleNamespace(players=SimpleNamespace(all_players=lambda **_kwargs: [])),
    )
    return player


async def test_new_station_uses_wav_for_queue_and_live_sources() -> None:
    """An unconfigured Station uses the measured low-latency streaming profile."""
    player = _make_player()
    entries = [*await player.get_config_entries(), PLAYBACK_QUALITY_ENTRY]
    player._config = cast(
        "PlayerConfig",
        PlayerConfig.parse(
            entries,
            {"provider": "yandex_station", "player_id": "test_station", "values": {}},
        ),
    )

    assert player.output_codec == ContentType.WAV
    assert player.config.get_value("prefer_wav_for_live_sources") is True


def test_a_station_requires_a_content_length() -> None:
    """The Station gets a content length on every stream."""
    assert _make_player().requires_content_length is True


@pytest.mark.parametrize(
    ("playback_quality", "codec"),
    [
        ("lossless_compressed", ContentType.FLAC),
        ("compressed", ContentType.MP3),
        ("lossless_uncompressed", ContentType.WAV),
    ],
)
async def test_saved_stream_preferences_override_station_defaults(
    playback_quality: str, codec: ContentType
) -> None:
    """Existing explicit choices remain effective after updating the provider."""
    player = _make_player()
    entries = [*await player.get_config_entries(), PLAYBACK_QUALITY_ENTRY]
    player._config = cast(
        "PlayerConfig",
        PlayerConfig.parse(
            entries,
            {
                "provider": "yandex_station",
                "player_id": "test_station",
                "values": {
                    CONF_PLAYBACK_QUALITY: playback_quality,
                    "prefer_wav_for_live_sources": False,
                },
            },
        ),
    )

    assert player.output_codec == codec
    assert player.config.get_value("prefer_wav_for_live_sources") is False
