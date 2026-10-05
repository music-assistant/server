"""Tests for provider/handlers.py — Yandex Smart Home API request handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Use the PlaybackState from conftest's mock enums
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.yandex_smarthome.handlers import (
    build_response,
    handle_device_list,
    handle_devices_action,
    handle_devices_query,
    handle_user_unlink,
    parse_action_payload,
)
from music_assistant.providers.yandex_smarthome.schema import (
    DeviceListPayload,
    YandexCapabilityType,
)


@dataclass
class MockPlayer:
    """Mock player for handler tests."""

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
    source_list: list[str] = field(default_factory=list)
    active_source: str | None = None

    @property
    def state(self) -> MockPlayer:
        """Return self as state (mirrors real Player.state)."""
        return self


def _make_mass(players: list[MockPlayer]) -> MagicMock:
    """Create a mock MusicAssistant with given players."""
    mass = MagicMock()
    mass.players.__iter__ = MagicMock(return_value=iter(players))
    mass.players.all_players = MagicMock(return_value=players)

    player_map = {p.player_id: p for p in players}
    mass.players.get_player = MagicMock(side_effect=player_map.get)

    mass.players.cmd_play = AsyncMock()
    mass.players.cmd_stop = AsyncMock()
    mass.players.cmd_pause = AsyncMock()
    mass.players.cmd_power = AsyncMock()
    mass.players.cmd_volume_set = AsyncMock()
    mass.players.cmd_volume_mute = AsyncMock()
    mass.player_queues.play_media = AsyncMock()
    return mass


# ---------------------------------------------------------------------------
# Tests: handle_device_list
# ---------------------------------------------------------------------------


class TestHandleDeviceList:
    """Tests for handle_device_list."""

    @pytest.mark.asyncio
    async def test_empty(self) -> None:
        """Test empty."""
        mass = _make_mass([])
        result = await handle_device_list(mass, "user1")
        assert result.user_id == "user1"
        assert result.devices == []

    @pytest.mark.asyncio
    async def test_exposes_available_players(self) -> None:
        """Test exposes available players."""
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
    async def test_filters_unavailable(self) -> None:
        """Test filters unavailable."""
        players = [
            MockPlayer(player_id="p1", available=True),
            MockPlayer(player_id="p2", available=False),
        ]
        mass = _make_mass(players)
        result = await handle_device_list(mass, "user1")
        assert len(result.devices) == 1
        assert result.devices[0].id == "p1"

    @pytest.mark.asyncio
    async def test_filters_synced(self) -> None:
        """Test filters synced."""
        players = [
            MockPlayer(player_id="leader"),
            MockPlayer(player_id="follower", synced_to="leader"),
        ]
        mass = _make_mass(players)
        result = await handle_device_list(mass, "user1")
        assert len(result.devices) == 1
        assert result.devices[0].id == "leader"

    @pytest.mark.asyncio
    async def test_playlist_uris_register_mode_capability(self) -> None:
        """Players without native sources get mode(input_source) when playlists are configured."""
        players = [MockPlayer(player_id="p1")]
        mass = _make_mass(players)
        result = await handle_device_list(
            mass,
            "user1",
            playlist_uris=("library://playlist/a", "library://playlist/b"),
        )
        assert len(result.devices) == 1
        mode_caps = [
            c for c in result.devices[0].capabilities if c.type == YandexCapabilityType.MODE
        ]
        assert len(mode_caps) == 1
        assert mode_caps[0].parameters is not None
        modes = mode_caps[0].parameters.modes
        assert modes is not None
        assert [m.value for m in modes] == ["one", "two"]

    @pytest.mark.asyncio
    async def test_filters_by_exposed_ids(self) -> None:
        """Test filters by exposed ids."""
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
    """Tests for handle_devices_query."""

    @pytest.mark.asyncio
    async def test_returns_states(self) -> None:
        """Test returns states."""
        mass = _make_mass([MockPlayer(player_id="p1", volume_level=75)])
        result = await handle_devices_query(mass, ["p1"])
        assert len(result.devices) == 1
        assert result.devices[0].id == "p1"
        assert result.devices[0].error_code is None

    @pytest.mark.asyncio
    async def test_unknown_device_returns_error(self) -> None:
        """Test unknown device returns error."""
        mass = _make_mass([])
        result = await handle_devices_query(mass, ["missing"])
        assert len(result.devices) == 1
        assert result.devices[0].error_code == "DEVICE_UNREACHABLE"

    @pytest.mark.asyncio
    async def test_unavailable_device_returns_error(self) -> None:
        """Test unavailable device returns error."""
        mass = _make_mass([MockPlayer(player_id="p1", available=False)])
        result = await handle_devices_query(mass, ["p1"])
        assert result.devices[0].error_code == "DEVICE_UNREACHABLE"


# ---------------------------------------------------------------------------
# Tests: handle_devices_action
# ---------------------------------------------------------------------------


class TestHandleDevicesAction:
    """Tests for handle_devices_action."""

    @pytest.mark.asyncio
    async def test_executes_on_off(self) -> None:
        """Test executes on off."""
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
    async def test_missing_device_returns_error(self) -> None:
        """Test missing device returns error."""
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
    """Tests for handle_user_unlink."""

    @pytest.mark.asyncio
    async def test_returns_empty(self) -> None:
        """Test returns empty."""
        result = await handle_user_unlink()
        assert result == {}


# ---------------------------------------------------------------------------
# Tests: parse_action_payload
# ---------------------------------------------------------------------------


class TestParseActionPayload:
    """Tests for parse_action_payload."""

    def test_basic_payload(self) -> None:
        """Test basic payload."""
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

    def test_relative_volume(self) -> None:
        """Test relative volume."""
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

    def test_multiple_devices(self) -> None:
        """Test multiple devices."""
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

    def test_unwrapped_payload(self) -> None:
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

    def test_skips_missing_or_empty_instance(self) -> None:
        """Capability with missing/empty/non-string instance should be skipped."""
        raw = {
            "payload": {
                "devices": [
                    {
                        "id": "p1",
                        "capabilities": [
                            {"type": "devices.capabilities.on_off", "state": {"value": True}},
                            {
                                "type": "devices.capabilities.on_off",
                                "state": {"instance": "   ", "value": True},
                            },
                            {
                                "type": "devices.capabilities.on_off",
                                "state": {"instance": 42, "value": True},
                            },
                        ],
                    }
                ],
            },
        }
        payload = parse_action_payload(raw)
        assert len(payload.devices) == 1
        assert payload.devices[0].capabilities == []

    def test_skips_non_bool_relative(self) -> None:
        """Capability with non-bool `relative` field should be skipped."""
        raw = {
            "payload": {
                "devices": [
                    {
                        "id": "p1",
                        "capabilities": [
                            {
                                "type": "devices.capabilities.range",
                                "state": {"instance": "volume", "value": 10, "relative": "yes"},
                            },
                        ],
                    }
                ],
            },
        }
        payload = parse_action_payload(raw)
        assert payload.devices[0].capabilities == []


# ---------------------------------------------------------------------------
# Tests: build_response
# ---------------------------------------------------------------------------


class TestBuildResponse:
    """Tests for build_response."""

    def test_dict_payload(self) -> None:
        """Test dict payload."""
        resp = build_response("req-1", {"key": "val"})
        assert resp == {"request_id": "req-1", "payload": {"key": "val"}}

    def test_none_payload(self) -> None:
        """Test none payload."""
        resp = build_response("req-1", None)
        assert resp == {"request_id": "req-1", "payload": {}}

    def test_dataclass_payload(self) -> None:
        """Test dataclass payload."""
        payload = DeviceListPayload(user_id="u1", devices=[])
        resp = build_response("req-1", payload)
        assert resp["request_id"] == "req-1"
        assert resp["payload"]["user_id"] == "u1"
