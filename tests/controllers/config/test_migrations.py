"""Tests for (settings.json) config migrations."""

from typing import Any

from music_assistant.controllers.config.migrations import _migrate_output_limiter


def test_migrate_output_limiter_drops_stored_values() -> None:
    """The removed output limiter setting is dropped, other player values are kept."""
    data: dict[str, Any] = {
        "players": {
            "p1": {"player_id": "p1", "values": {"output_limiter": False, "flow_mode": True}},
            "p2": {"player_id": "p2", "values": {"output_limiter": True}},
            "p3": {"player_id": "p3", "values": {}},
        }
    }
    assert _migrate_output_limiter(data) is True
    assert data["players"]["p1"]["values"] == {"flow_mode": True}
    assert data["players"]["p2"]["values"] == {}
    assert data["players"]["p3"]["values"] == {}


def test_migrate_output_limiter_noop_when_absent() -> None:
    """Migration reports no change when no player stored the setting."""
    data: dict[str, Any] = {"players": {"p1": {"player_id": "p1", "values": {"flow_mode": True}}}}
    assert _migrate_output_limiter(data) is False
