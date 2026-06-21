"""Tests for the per-queue config layer (migration + parsing)."""

from types import SimpleNamespace
from typing import Any, cast

from music_assistant_models.config_entries import PlayerQueueConfig

from music_assistant.constants import CONF_ENTRY_CROSSFADE_DURATION
from music_assistant.controllers.config import ConfigController


def _migrate(data: dict[str, Any]) -> bool:
    """Run the (settings.json) queue-settings migration against a raw data dict."""
    stub = cast("ConfigController", SimpleNamespace(_data=data))
    return ConfigController._migrate_player_queue_settings(stub)


def test_migrate_player_queue_settings_moves_only_queue_keys() -> None:
    """Queue-scoped values move to the per-queue config, player-scoped ones stay put."""
    data: dict[str, Any] = {
        "players": {
            "p1": {
                "player_id": "p1",
                "values": {
                    "crossfade_duration": 12,
                    "volume_normalization": False,
                    "output_limiter": True,  # player/DSP stage -> stays on the player
                    "smart_fades_mode": "disabled",  # legacy off -> consumed, nothing carried over
                },
            },
            "p2": {"player_id": "p2", "values": {}},  # nothing to move
        }
    }
    assert _migrate(data) is True
    # moved keys (and the consumed legacy smart_fades_mode) are gone from the player config
    assert data["players"]["p1"]["values"] == {"output_limiter": True}
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
        "players": {"p1": {"player_id": "p1", "values": {"output_limiter": True}}}
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
