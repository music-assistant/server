"""Tests for the migration of stored MSX Bridge settings and Sendspin bridge players."""

from copy import deepcopy
from typing import Any

import pytest

from music_assistant.constants import CONF_LINKED_PROTOCOL_IDS
from music_assistant.controllers.config.migrations import _migrate_msx_bridge_settings, migrate


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("legacy_value", [True, False, None])
async def test_msx_bridge_retired_settings_migrate(
    enabled: bool, legacy_value: bool | None
) -> None:
    """Retired keys are dropped whatever their value and shared moves to independent."""
    data: dict[str, Any] = {
        "providers": {
            "msx_bridge_instance": {
                "domain": "msx_bridge",
                "enabled": enabled,
                "values": {
                    "enable_player_grouping": legacy_value,
                    "enable_sendspin_bridge": legacy_value,
                    "group_stream_mode": "shared",
                    "port": 8099,
                },
            },
            "other": {
                "domain": "other",
                "values": {
                    "enable_player_grouping": False,
                    "enable_sendspin_bridge": False,
                    "group_stream_mode": "shared",
                },
            },
        }
    }
    expected = deepcopy(data)
    expected["providers"]["msx_bridge_instance"]["values"] = {
        "group_stream_mode": "independent",
        "port": 8099,
    }
    assert await migrate(data) is True
    assert data == expected
    assert await migrate(data) is False
    assert data == expected


@pytest.mark.parametrize(
    "values", [{}, {"group_stream_mode": "redirect"}, {"group_stream_mode": "independent"}, None]
)
def test_msx_bridge_current_settings_unchanged(values: Any) -> None:
    """Current stream modes and missing values are left alone."""
    data = {"providers": {"msx": {"domain": "msx_bridge", "values": values}}}
    expected = deepcopy(data)
    assert _migrate_msx_bridge_settings(data) is False
    assert data == expected


def test_msx_bridge_sendspin_players_removed() -> None:
    """Sendspin bridge players, their DSP config and their links are removed."""
    data: dict[str, Any] = {
        "players": {
            "msx_livingroom": {
                "player_id": "msx_livingroom",
                "values": {CONF_LINKED_PROTOCOL_IDS: ["spb_msx_livingroom", "ap_tv"]},
            },
            "spb_msx_livingroom": {"player_id": "spb_msx_livingroom", "values": {}},
            "spb_msx_bedroom": {"player_id": "spb_msx_bedroom", "enabled": False},
            "spb_kitchen": {
                "player_id": "spb_kitchen",
                "values": {CONF_LINKED_PROTOCOL_IDS: []},
            },
        },
        "player_dsp": {"spb_msx_livingroom": {"enabled": True}, "spb_kitchen": {"enabled": True}},
    }
    assert _migrate_msx_bridge_settings(data) is True
    assert set(data["players"]) == {"msx_livingroom", "spb_kitchen"}
    assert data["players"]["msx_livingroom"]["values"][CONF_LINKED_PROTOCOL_IDS] == ["ap_tv"]
    assert data["player_dsp"] == {"spb_kitchen": {"enabled": True}}
    assert _migrate_msx_bridge_settings(data) is False


def test_msx_bridge_stale_link_without_player_removed() -> None:
    """A cached link to a bridge player without a stored config is removed as well."""
    data: dict[str, Any] = {
        "players": {
            "msx_tv": {"values": {CONF_LINKED_PROTOCOL_IDS: ["spb_msx_tv"]}},
        }
    }
    assert _migrate_msx_bridge_settings(data) is True
    assert data["players"]["msx_tv"]["values"][CONF_LINKED_PROTOCOL_IDS] == []


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"providers": None, "players": None},
        {"providers": [], "players": []},
        {"providers": {"msx": None}, "players": {"p1": None}},
        {"providers": {"msx": {"domain": "msx_bridge"}}},
        {"providers": {"msx": {"domain": "msx_bridge", "values": ["shared"]}}},
        {"players": {"p1": {"values": None}}, "player_dsp": None},
        {"players": {"p1": {"values": {CONF_LINKED_PROTOCOL_IDS: "spb_msx_tv"}}}},
        {"players": {"p1": {"values": {CONF_LINKED_PROTOCOL_IDS: [None, 1, "ap_tv"]}}}},
    ],
)
def test_msx_bridge_malformed_shapes_do_not_raise(data: dict[str, Any]) -> None:
    """Missing or malformed stored values are tolerated and left unchanged."""
    expected = deepcopy(data)
    assert _migrate_msx_bridge_settings(data) is False
    assert data == expected


def test_msx_bridge_sendspin_player_removed_without_dsp_store() -> None:
    """A bridge player is removed when no DSP configs are stored."""
    data: dict[str, Any] = {"players": {"spb_msx_tv": {}}, "player_dsp": None}
    assert _migrate_msx_bridge_settings(data) is True
    assert data["players"] == {}
