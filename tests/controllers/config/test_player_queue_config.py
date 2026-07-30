"""Tests for the per-queue config layer (migration + parsing)."""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant_models.config_entries import ConfigEntry, PlayerQueueConfig
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import (
    CONF_CORE,
    CONF_CROSSFADE_DURATION,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_PLAYER_QUEUES,
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VOLUME_NORMALIZATION,
)
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.config.migrations import (
    _migrate_global_queue_settings,
    _migrate_player_queue_settings,
)
from music_assistant.controllers.player_queues.constants import (
    CONF_AUTOPLAY_PLAYLIST,
    CONF_SMART_SHUFFLE_ENABLED,
    CONF_SMART_SHUFFLE_SONG_RECENCY,
)


def _migrate(data: dict[str, Any]) -> bool:
    """Run the (settings.json) queue-settings migration against a raw data dict."""
    return _migrate_player_queue_settings(data)


def test_migrate_player_queue_settings_moves_only_queue_keys() -> None:
    """Queue-scoped values move to the per-queue config, player-scoped ones stay put."""
    data: dict[str, Any] = {
        "players": {
            "p1": {
                "player_id": "p1",
                "values": {
                    "crossfade_duration": 12,
                    "volume_normalization": False,
                    "tts_pre_announce": False,  # player-scoped -> stays on the player
                    "smart_fades_mode": "disabled",  # legacy off -> consumed, nothing carried over
                },
            },
            "p2": {"player_id": "p2", "values": {}},  # nothing to move
        }
    }
    assert _migrate(data) is True
    # moved keys (and the consumed legacy smart_fades_mode) are gone from the player config
    assert data["players"]["p1"]["values"] == {"tts_pre_announce": False}
    # ...and now live under the per-queue config (queue_id == player_id)
    assert data["player_queues"]["p1"]["values"] == {
        "crossfade_duration": 12,
        "volume_normalization": False,
    }
    # players without queue-scoped values don't get a queue config entry
    assert "p2" not in data.get("player_queues", {})


def test_migrate_player_queue_settings_noop_when_nothing_to_move() -> None:
    """Migration reports no change when there are no queue-scoped values to move."""
    data: dict[str, Any] = {
        "players": {"p1": {"player_id": "p1", "values": {"tts_pre_announce": False}}}
    }
    assert _migrate(data) is False
    assert "player_queues" not in data


def test_migrate_player_queue_settings_maps_legacy_smart_fades_mode() -> None:
    """smart_fades_mode is consumed and standard/smart carry over to crossfade_mode."""
    data: dict[str, Any] = {
        "players": {
            "p1": {"player_id": "p1", "values": {"smart_fades_mode": "standard_crossfade"}},
            "p2": {"player_id": "p2", "values": {"smart_fades_mode": "smart_crossfade"}},
            "p3": {"player_id": "p3", "values": {"smart_fades_mode": "disabled"}},
        }
    }
    assert _migrate(data) is True
    # the legacy key is removed from every player
    assert all(cfg["values"] == {} for cfg in data["players"].values())
    # standard/smart carry over to the new crossfade_mode select
    assert data["player_queues"]["p1"]["values"] == {"crossfade_mode": "standard_crossfade"}
    assert data["player_queues"]["p2"]["values"] == {"crossfade_mode": "smart_crossfade"}
    # disabled just means crossfade is off -> nothing written
    assert "p3" not in data.get("player_queues", {})


def test_migrate_player_queue_settings_keeps_existing_queue_value() -> None:
    """An already-stored queue value is not clobbered by the migration."""
    data: dict[str, Any] = {
        "players": {"p1": {"player_id": "p1", "values": {"crossfade_duration": 12}}},
        "player_queues": {"p1": {"queue_id": "p1", "values": {"crossfade_duration": 5}}},
    }
    assert _migrate(data) is True
    assert data["player_queues"]["p1"]["values"]["crossfade_duration"] == 5
    assert "crossfade_duration" not in data["players"]["p1"]["values"]


def test_migrate_global_queue_settings_bool_to_select() -> None:
    """The former boolean toggles become their enabled/disabled select strings (idempotently)."""
    data: dict[str, Any] = {
        CONF_PLAYER_QUEUES: {
            "q1": {
                "queue_id": "q1",
                "values": {
                    CONF_VOLUME_NORMALIZATION: False,
                    CONF_SMART_SHUFFLE_ENABLED: True,
                },
            }
        }
    }
    assert _migrate_global_queue_settings(data) is True
    values = data[CONF_PLAYER_QUEUES]["q1"]["values"]
    assert values[CONF_VOLUME_NORMALIZATION] == CONF_VALUE_DISABLED
    assert values[CONF_SMART_SHUFFLE_ENABLED] == CONF_VALUE_ENABLED
    # a second run is a no-op (the values are already select strings)
    assert _migrate_global_queue_settings(data) is False


def test_migrate_global_queue_settings_promotes_consistent_value() -> None:
    """A crossfade duration shared by every queue is promoted to core and dropped per-queue."""
    data: dict[str, Any] = {
        CONF_PLAYER_QUEUES: {
            "q1": {"queue_id": "q1", "values": {CONF_CROSSFADE_DURATION: 12}},
            "q2": {"queue_id": "q2", "values": {CONF_CROSSFADE_DURATION: 12}},
        }
    }
    assert _migrate_global_queue_settings(data) is True
    assert data[CONF_CORE][CONF_PLAYER_QUEUES]["values"][CONF_CROSSFADE_DURATION] == 12
    assert CONF_CROSSFADE_DURATION not in data[CONF_PLAYER_QUEUES]["q1"]["values"]
    assert CONF_CROSSFADE_DURATION not in data[CONF_PLAYER_QUEUES]["q2"]["values"]


def test_migrate_global_queue_settings_mixed_values_not_promoted() -> None:
    """When queues disagree the value is not promoted, but the per-queue copies are still dropped."""
    data: dict[str, Any] = {
        CONF_PLAYER_QUEUES: {
            "q1": {"queue_id": "q1", "values": {CONF_SMART_SHUFFLE_SONG_RECENCY: "3600"}},
            "q2": {"queue_id": "q2", "values": {CONF_SMART_SHUFFLE_SONG_RECENCY: "86400"}},
        }
    }
    assert _migrate_global_queue_settings(data) is True
    core_values = data.get(CONF_CORE, {}).get(CONF_PLAYER_QUEUES, {}).get("values", {})
    assert CONF_SMART_SHUFFLE_SONG_RECENCY not in core_values
    assert CONF_SMART_SHUFFLE_SONG_RECENCY not in data[CONF_PLAYER_QUEUES]["q1"]["values"]
    assert CONF_SMART_SHUFFLE_SONG_RECENCY not in data[CONF_PLAYER_QUEUES]["q2"]["values"]


def test_migrate_global_queue_settings_noop_when_empty() -> None:
    """Nothing to migrate reports no change."""
    assert _migrate_global_queue_settings({}) is False
    assert _migrate_global_queue_settings({CONF_PLAYER_QUEUES: {}}) is False


def test_player_queue_config_parse_roundtrip() -> None:
    """A stored queue config parses its values back via the current entries."""
    config = cast(
        "PlayerQueueConfig",
        PlayerQueueConfig.parse(
            [CONF_ENTRY_CROSSFADE_DURATION],
            {"queue_id": "q1", "values": {"crossfade_duration": 9}},
        ),
    )
    assert config.queue_id == "q1"
    assert config.get_value("crossfade_duration") == 9


async def test_get_player_queue_config_for_api_populates_playlist_options() -> None:
    """The 'get' command resolves the autoplay playlist dropdown from the library playlists."""
    entry = ConfigEntry(
        key=CONF_AUTOPLAY_PLAYLIST, type=ConfigEntryType.STRING, options=[], required=False
    )
    config = cast(
        "PlayerQueueConfig",
        PlayerQueueConfig.parse([entry], {"queue_id": "q1", "values": {}}),
    )

    async def _playlists() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(uri="library://playlist/1", name="Chill")
        yield SimpleNamespace(uri="library://playlist/2", name="Party")

    fake = MagicMock()
    fake.get_player_queue_config = MagicMock(return_value=config)
    fake.mass.music.playlists.iter_library_items = lambda: _playlists()
    # bind the real helper so the command exercises the actual option-building logic
    fake._library_playlist_options = lambda: ConfigController._library_playlist_options(fake)

    result = await ConfigController.get_player_queue_config_for_api(fake, "q1")

    options = result.values[CONF_AUTOPLAY_PLAYLIST].options
    assert [(opt.title, opt.value) for opt in options] == [
        ("Chill", "library://playlist/1"),
        ("Party", "library://playlist/2"),
    ]
