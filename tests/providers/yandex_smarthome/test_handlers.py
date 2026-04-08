"""Tests for provider/handlers.py — Yandex Smart Home API request handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Use the PlaybackState from conftest's mock enums
from music_assistant_models.enums import PlaybackState

from provider.handlers import (
    build_response,
    handle_device_list,
    handle_devices_action,
    handle_devices_query,
    handle_user_unlink,
    parse_action_payload,
)


@dataclass
class MockPlayer:
    player_id: str = "p1"
    name: str = "Speaker"
    available: bool = True
    enabled: bool = True
    powered: bool | None = True
    playback_state: Any = PlaybackState.PLAYING
    volume_level: int | None = 50
    volume_muted: bool | None = False
    synced_to: str | None = None
    device_info: Any = None
    supported_features: set[str] = field(default_factory=set)
    source_list: list = field(default_factory=list)
    active_source: str | None = None


def _make_mass(players: list[MockPlayer]) -> MagicMock:
    """Create a mock MusicAssistant with given players."""
    mass = MagicMock()
    mass.players.__iter__ = MagicMock(return_value=iter(players))

    player_map = {p.player_id: p for p in players}
    mass.players.get_player = MagicMock(side_effect=player_map.get)

    mass.players.cmd_play = AsyncMock()
    mass.players.cmd_stop = AsyncMock()
    mass.players.cmd_pause = AsyncMock()
    mass.players.cmd_power = AsyncMock()
    mass.players.cmd_volume_set = AsyncMock()
    mass.players.cmd_volume_mute = AsyncMock()
    return mass


# ---------------------------------------------------------------------------
# Tests: handle_device_list
# ---------------------------------------------------------------------------


class TestHandleDeviceList:
    @pytest.mark.asyncio
    async def test_empty(self):
        mass = _make_mass([])
        result = await handle_device_list(mass, "user1")
        assert result.user_id == "user1"
        assert result.devices == []

    @pytest.mark.asyncio
    async def test_exposes_available_players(self):
        players = [
            MockPlayer(player_id="p1", name="Speaker 1"),
            MockPlayer(player_id="p2", name="Speaker 2"),
        ]
        mass = _make_mass(players)
        result = await handle_device_list(mass, "user1")
        assert len(result.devices) == 2
        ids = {d.id for d in result.devices}
        assert ids == {"p1", "p2"}

    @pytest.mark.asyncio
    async def test_filters_unavailable(self):
        players = [
            MockPlayer(player_id="p1", available=True),
            MockPlayer(player_id="p2", available=False),
        ]
        mass = _make_mass(players)
        result = await handle_device_list(mass, "user1")
        assert len(result.devices) == 1
        assert result.devices[0].id == "p1"

    @pytest.mark.asyncio
    async def test_filters_synced(self):
        players = [
            MockPlayer(player_id="leader"),
            MockPlayer(player_id="follower", synced_to="leader"),
        ]
        mass = _make_mass(players)
        result = await handle_device_list(mass, "user1")
        assert len(result.devices) == 1
        assert result.devices[0].id == "leader"

    @pytest.mark.asyncio
    async def test_filters_by_exposed_ids(self):
        players = [
            MockPlayer(player_id="p1", name="Speaker 1"),
            MockPlayer(player_id="p2", name="Speaker 2"),
            MockPlayer(player_id="p3", name="Speaker 3"),
        ]
        mass = _make_mass(players)
        result = await handle_device_list(mass, "user1", exposed_ids={"p1", "p3"})
        assert len(result.devices) == 2
        ids = {d.id for d in result.devices}
        assert ids == {"p1", "p3"}


# ---------------------------------------------------------------------------
# Tests: handle_devices_query
# ---------------------------------------------------------------------------


class TestHandleDevicesQuery:
    @pytest.mark.asyncio
    async def test_returns_states(self):
        mass = _make_mass([MockPlayer(player_id="p1", volume_level=75)])
        result = await handle_devices_query(mass, ["p1"])
        assert len(result.devices) == 1
        assert result.devices[0].id == "p1"
        assert result.devices[0].error_code is None

    @pytest.mark.asyncio
    async def test_unknown_device_returns_error(self):
        mass = _make_mass([])
        result = await handle_devices_query(mass, ["missing"])
        assert len(result.devices) == 1
        assert result.devices[0].error_code == "DEVICE_UNREACHABLE"

    @pytest.mark.asyncio
    async def test_unavailable_device_returns_error(self):
        mass = _make_mass([MockPlayer(player_id="p1", available=False)])
        result = await handle_devices_query(mass, ["p1"])
        assert result.devices[0].error_code == "DEVICE_UNREACHABLE"


# ---------------------------------------------------------------------------
# Tests: handle_devices_action
# ---------------------------------------------------------------------------


class TestHandleDevicesAction:
    @pytest.mark.asyncio
    async def test_executes_on_off(self):
        mass = _make_mass([MockPlayer(player_id="p1")])
        payload = parse_action_payload(
            {
                "payload": {
                    "devices": [
                        {
                            "id": "p1",
                            "capabilities": [
                                {
                                    "type": "devices.capabilities.on_off",
                                    "state": {"instance": "on", "value": True},
                                }
                            ],
                        }
                    ],
                },
            }
        )
        result = await handle_devices_action(mass, payload)
        assert len(result.devices) == 1
        mass.players.cmd_play.assert_awaited_once_with("p1")

    @pytest.mark.asyncio
    async def test_missing_device_returns_error(self):
        mass = _make_mass([])
        payload = parse_action_payload(
            {
                "payload": {
                    "devices": [
                        {
                            "id": "missing",
                            "capabilities": [
                                {
                                    "type": "devices.capabilities.on_off",
                                    "state": {"instance": "on", "value": True},
                                }
                            ],
                        }
                    ],
                },
            }
        )
        result = await handle_devices_action(mass, payload)
        assert (
            result.devices[0].capabilities[0].state.action_result.error_code == "DEVICE_UNREACHABLE"
        )


# ---------------------------------------------------------------------------
# Tests: handle_user_unlink
# ---------------------------------------------------------------------------


class TestHandleUserUnlink:
    @pytest.mark.asyncio
    async def test_returns_empty(self):
        result = await handle_user_unlink()
        assert result == {}


# ---------------------------------------------------------------------------
# Tests: parse_action_payload
# ---------------------------------------------------------------------------


class TestParseActionPayload:
    def test_basic_payload(self):
        raw = {
            "payload": {
                "devices": [
                    {
                        "id": "p1",
                        "capabilities": [
                            {
                                "type": "devices.capabilities.on_off",
                                "state": {"instance": "on", "value": True},
                            }
                        ],
                    }
                ],
            },
        }
        payload = parse_action_payload(raw)
        assert len(payload.devices) == 1
        assert payload.devices[0].id == "p1"
        assert len(payload.devices[0].capabilities) == 1
        cap = payload.devices[0].capabilities[0]
        assert cap.type == "devices.capabilities.on_off"
        assert cap.state.instance == "on"
        assert cap.state.value is True
        assert cap.state.relative is False

    def test_relative_volume(self):
        raw = {
            "payload": {
                "devices": [
                    {
                        "id": "p1",
                        "capabilities": [
                            {
                                "type": "devices.capabilities.range",
                                "state": {"instance": "volume", "value": 10, "relative": True},
                            }
                        ],
                    }
                ],
            },
        }
        payload = parse_action_payload(raw)
        cap = payload.devices[0].capabilities[0]
        assert cap.state.relative is True
        assert cap.state.value == 10

    def test_multiple_devices(self):
        raw = {
            "payload": {
                "devices": [
                    {"id": "p1", "capabilities": []},
                    {"id": "p2", "capabilities": []},
                ],
            },
        }
        payload = parse_action_payload(raw)
        assert len(payload.devices) == 2

    def test_unwrapped_payload(self):
        """parse_action_payload should also handle messages without outer 'payload' key."""
        raw = {
            "devices": [
                {
                    "id": "p1",
                    "capabilities": [
                        {
                            "type": "devices.capabilities.toggle",
                            "state": {"instance": "pause", "value": True},
                        }
                    ],
                }
            ],
        }
        payload = parse_action_payload(raw)
        assert len(payload.devices) == 1


# ---------------------------------------------------------------------------
# Tests: build_response
# ---------------------------------------------------------------------------


class TestBuildResponse:
    def test_dict_payload(self):
        resp = build_response("req-1", {"key": "val"})
        assert resp == {"request_id": "req-1", "payload": {"key": "val"}}

    def test_none_payload(self):
        resp = build_response("req-1", None)
        assert resp == {"request_id": "req-1", "payload": {}}

    def test_dataclass_payload(self):
        from provider.schema import DeviceListPayload

        payload = DeviceListPayload(user_id="u1", devices=[])
        resp = build_response("req-1", payload)
        assert resp["request_id"] == "req-1"
        assert resp["payload"]["user_id"] == "u1"
