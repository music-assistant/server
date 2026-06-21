"""Tests for the per-queue config layer (migration + parsing)."""

from types import SimpleNamespace

from music_assistant.controllers.config import (
    PLAYER_QUEUE_CONFIG_ENTRIES,
    ConfigController,
    PlayerQueueConfig,
)


def test_migrate_player_queue_settings_moves_only_queue_keys() -> None:
    """Queue-scoped values move to the per-queue config, player-scoped ones stay put."""
    data = {
        "players": {
            "p1": {
                "player_id": "p1",
                "values": {
                    "crossfade_duration": 12,
                    "volume_normalization": False,
                    "output_limiter": True,  # player/DSP stage -> stays on the player
                    "smart_fades_mode": "standard_crossfade",  # seeded elsewhere -> stays
                },
            },
            "p2": {"player_id": "p2", "values": {}},  # nothing to move
        }
    }
    stub = SimpleNamespace(_data=data)
    changed = ConfigController._migrate_player_queue_settings(stub)
    assert changed is True
    # moved keys are gone from the player config
    assert data["players"]["p1"]["values"] == {
        "output_limiter": True,
        "smart_fades_mode": "standard_crossfade",
    }
    # ...and now live under the per-queue config (queue_id == player_id)
    assert data["player_queues"]["p1"]["values"] == {
        "crossfade_duration": 12,
        "volume_normalization": False,
    }
    # players without queue-scoped values don't get a queue config entry
    assert "p2" not in data.get("player_queues", {})


def test_migrate_player_queue_settings_noop_when_nothing_to_move() -> None:
    """Migration reports no change when there are no queue-scoped values to move."""
    data = {"players": {"p1": {"player_id": "p1", "values": {"output_limiter": True}}}}
    stub = SimpleNamespace(_data=data)
    assert ConfigController._migrate_player_queue_settings(stub) is False
    assert "player_queues" not in data


def test_migrate_player_queue_settings_keeps_existing_queue_value() -> None:
    """An already-stored queue value is not clobbered by the migration."""
    data = {
        "players": {"p1": {"player_id": "p1", "values": {"crossfade_duration": 12}}},
        "player_queues": {"p1": {"queue_id": "p1", "values": {"crossfade_duration": 5}}},
    }
    stub = SimpleNamespace(_data=data)
    assert ConfigController._migrate_player_queue_settings(stub) is True
    assert data["player_queues"]["p1"]["values"]["crossfade_duration"] == 5
    assert "crossfade_duration" not in data["players"]["p1"]["values"]


def test_player_queue_config_parse_roundtrip() -> None:
    """A stored queue config parses its values back via the current entries."""
    config = PlayerQueueConfig.parse(
        PLAYER_QUEUE_CONFIG_ENTRIES,
        {"queue_id": "q1", "values": {"crossfade_duration": 9}},
    )
    assert config.queue_id == "q1"
    assert config.get_value("crossfade_duration") == 9
