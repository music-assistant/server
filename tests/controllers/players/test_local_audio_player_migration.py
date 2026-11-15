"""Tests for the config migration that promotes local_audio stubs to regular players."""

from __future__ import annotations

from typing import Any

from music_assistant.constants import (
    CONF_EXPOSE_PLAYER_TO_HA,
    CONF_HIDE_IN_UI,
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
)
from music_assistant.controllers.config.migrations import migrate

DEVICE_UUID = "aabbccdd-1122-3344-5566-77889900aabb"
UNIVERSAL_ID = "upaabbccdd11223344556677889900aabb"


def _make_data() -> dict[str, Any]:
    """Build a settings dict as persisted by the previous local_audio setup."""
    return {
        CONF_PLAYERS: {
            DEVICE_UUID: {
                "player_id": DEVICE_UUID,
                "provider": "local_audio",
                "player_type": "protocol",
                "enabled": True,
                "values": {"protocol_parent_id": UNIVERSAL_ID},
            },
            UNIVERSAL_ID: {
                "player_id": UNIVERSAL_ID,
                "provider": "universal_player",
                "player_type": "group",
                "enabled": False,
                "name": "Kitchen Output",
                "values": {
                    # ghost_spb has no config anymore (device gone) - must not crash
                    "linked_protocol_ids": [DEVICE_UUID, "spb_1", "ghost_spb"],
                    "device_identifiers": {"uuid": DEVICE_UUID},
                    "device_info": {"model": "Dummy Output", "manufacturer": "Local Audio"},
                    CONF_EXPOSE_PLAYER_TO_HA: False,
                    CONF_HIDE_IN_UI: True,
                },
            },
            "spb_1": {
                "player_id": "spb_1",
                "provider": "sendspin",
                "player_type": "protocol",
                "enabled": True,
                "values": {"protocol_parent_id": UNIVERSAL_ID},
            },
            "syncgroup_1": {
                "player_id": "syncgroup_1",
                "provider": "sync_group",
                "player_type": "group",
                "enabled": True,
                "values": {
                    "group_members": [UNIVERSAL_ID, "other_player"],
                    "allowed_members": [UNIVERSAL_ID],
                },
            },
        },
        CONF_PLAYER_QUEUES: {
            UNIVERSAL_ID: {
                "queue_id": UNIVERSAL_ID,
                "values": {"autoplay_mode": "smart"},
            },
        },
        CONF_PLAYER_DSP: {
            UNIVERSAL_ID: {"enabled": True},
        },
    }


async def test_stub_is_promoted_and_its_wrapper_folded_in() -> None:
    """The stub becomes a regular player carrying the universal player's settings."""
    data = _make_data()

    assert await migrate(data) is True

    stub_cfg = data[CONF_PLAYERS][DEVICE_UUID]
    assert stub_cfg["player_type"] == "player"
    # the universal player was the device toggle the user operated
    assert stub_cfg["enabled"] is False
    assert stub_cfg["name"] == "Kitchen Output"
    values = stub_cfg["values"]
    assert "protocol_parent_id" not in values
    assert values[CONF_EXPOSE_PLAYER_TO_HA] is False
    assert values[CONF_HIDE_IN_UI] is True
    assert "linked_protocol_ids" not in values
    assert "device_identifiers" not in values
    assert "device_info" not in values
    # the stub's own protocol belongs to it, as does the bridge protocol of the wrapper
    protocols = data[CONF_PLAYER_PROTOCOLS]
    assert (protocols[DEVICE_UUID]["player_id"], protocols[DEVICE_UUID]["role"]) == (
        DEVICE_UUID,
        "native",
    )
    assert (protocols["spb_1"]["player_id"], protocols["spb_1"]["role"]) == (DEVICE_UUID, "audio")
    assert "ghost_spb" not in protocols

    # the wrapper is gone
    assert UNIVERSAL_ID not in data[CONF_PLAYERS]
    assert UNIVERSAL_ID not in protocols
    assert "spb_1" not in data[CONF_PLAYERS]

    # queue settings, DSP config and group memberships follow the new player_id
    queue_cfg = data[CONF_PLAYER_QUEUES][DEVICE_UUID]
    assert queue_cfg["queue_id"] == DEVICE_UUID
    assert queue_cfg["values"]["autoplay_mode"] == "smart"
    assert UNIVERSAL_ID not in data[CONF_PLAYER_QUEUES]
    assert data[CONF_PLAYER_DSP] == {DEVICE_UUID: {"enabled": True}}
    group_values = protocols["syncgroup_1"]["values"]
    assert group_values["group_members"] == [DEVICE_UUID, "other_player"]
    assert group_values["allowed_members"] == [DEVICE_UUID]


async def test_stub_without_a_wrapper_is_promoted() -> None:
    """A stub without a universal player wrapper is still promoted."""
    data = _make_data()
    del data[CONF_PLAYERS][UNIVERSAL_ID]

    assert await migrate(data) is True

    stub_cfg = data[CONF_PLAYERS][DEVICE_UUID]
    assert stub_cfg["player_type"] == "player"
    assert stub_cfg["enabled"] is True
    assert "protocol_parent_id" not in stub_cfg["values"]
    stub_protocol = data[CONF_PLAYER_PROTOCOLS][DEVICE_UUID]
    assert (stub_protocol["player_id"], stub_protocol["role"]) == (DEVICE_UUID, "native")


async def test_migration_is_idempotent() -> None:
    """A second run finds nothing left to migrate."""
    data = _make_data()

    assert await migrate(data) is True
    assert await migrate(data) is False
