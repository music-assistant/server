"""Tests for the HEOS player provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from pyheos import CommandError, ConnectionState, Heos, HeosGroup, HeosPlayer
from pyheos import const as heos_const
from pyheos.connection import ConnectionBase
from pyheos.message import HeosCommand, HeosMessage

from music_assistant.providers.heos.player import HeosPlayer as HeosProtocol
from music_assistant.providers.heos.provider import HeosPlayerProvider

LEADER_ID = 2061239925
MEMBER_ID = -1104512417


class _HeosSystem:
    """Two HEOS speakers answering the CLI commands of a controller connection."""

    def __init__(self) -> None:
        """Start with both speakers in one group, the leader first."""
        self.group = [LEADER_ID, MEMBER_ID]

    async def command(self, command: HeosCommand) -> HeosMessage:
        """Answer a CLI command the way the speakers do."""
        if command.command == "player/get_players":
            payload: Any = [
                self._player(LEADER_ID, "Living Room"),
                self._player(MEMBER_ID, "Kitchen"),
            ]
        elif command.command == "group/get_groups":
            payload = [self._group()] if self.group else []
        elif command.command == "group/get_group_info":
            payload = self._group()
        else:
            raise AssertionError(f"unexpected HEOS command {command.command}")
        return HeosMessage(command.command, payload=payload)

    def _player(self, player_id: int, name: str) -> dict[str, Any]:
        """Return the CLI data of one speaker."""
        data: dict[str, Any] = {"name": name, "pid": player_id, "model": "Denon Home 200"}
        if player_id in self.group:
            data["gid"] = self.group[0]
        return data

    def _group(self) -> dict[str, Any]:
        """Return the CLI data of the group."""
        return {
            "name": "Living Room + Kitchen",
            "gid": self.group[0],
            "players": [
                {
                    "name": str(pid),
                    "pid": pid,
                    "role": "leader" if pid == self.group[0] else "member",
                }
                for pid in self.group
            ],
        }


async def _connect(connection: ConnectionBase) -> None:
    """Connect without a socket, running the callbacks a real connect runs."""
    connection._state = ConnectionState.CONNECTED
    await ConnectionBase._on_connected(connection)


async def test_populate_sources_skips_unresponsive_device() -> None:
    """A device whose inputs cannot be browsed must not fail the source population."""
    healthy_input = MagicMock(source_id=1, media_id="inputs/aux_in_1")
    healthy_device = MagicMock()
    healthy_device.name = "Living Room"
    healthy_device.browse = AsyncMock(return_value=SimpleNamespace(items=[healthy_input]))
    unresponsive_device = MagicMock()
    unresponsive_device.name = "Bedroom"
    unresponsive_device.browse = AsyncMock(
        side_effect=CommandError("browse/browse", "Command timed out")
    )

    heos = MagicMock()
    heos.browse = AsyncMock(
        return_value=SimpleNamespace(items=[unresponsive_device, healthy_device])
    )
    heos.get_music_sources = AsyncMock(return_value={})

    provider = HeosPlayerProvider.__new__(HeosPlayerProvider)
    provider._heos = heos
    provider._music_source_list = []
    provider._input_source_list = []
    provider.logger = MagicMock()

    await provider._populate_sources()

    heos.browse.assert_awaited_once_with(heos_const.MUSIC_SOURCE_AUX_INPUT)
    assert provider.input_source_list == [healthy_input]
    provider.logger.warning.assert_called_once()


async def test_groups_follow_the_speakers_after_a_controller_reconnect() -> None:
    """A group the speakers dropped while the controller was away is gone after the reconnect."""
    system = _HeosSystem()
    provider = HeosPlayerProvider.__new__(HeosPlayerProvider)
    provider.logger = MagicMock()
    provider.config = MagicMock()
    provider.config.get_value.return_value = 10
    provider.mass = MagicMock()
    provider._music_source_list = []
    provider._input_source_list = []
    protocols: list[HeosProtocol] = []
    provider.mass.players.iter_protocols.side_effect = lambda _instance_id: iter(protocols)
    system_info = SimpleNamespace(preferred_hosts=[], get_ip_addresses=list)

    with (
        patch.object(ConnectionBase, "connect", _connect),
        patch.object(ConnectionBase, "command", system.command),
        patch.object(Heos, "check_account", AsyncMock()),
        patch.object(Heos, "register_for_change_events", AsyncMock()),
        patch.object(Heos, "get_system_info", AsyncMock(return_value=system_info)),
        patch.object(HeosPlayer, "refresh", AsyncMock()),
        patch.object(HeosGroup, "refresh", AsyncMock()),
        patch.object(HeosPlayerProvider, "_populate_sources", AsyncMock()),
    ):
        await provider._setup_controllers("192.168.1.81")
        heos = cast("Heos", provider._heos)
        for device in (await heos.get_players()).values():
            protocol = HeosProtocol(provider, device)
            await protocol.build_group_list()
            protocols.append(protocol)
        leader = protocols[0]
        assert leader.group_members == [str(LEADER_ID), str(MEMBER_ID)]

        connection = heos._connection
        connection._state = ConnectionState.DISCONNECTED
        await connection._on_disconnected(due_to_error=True)
        system.group = []
        await connection.connect()

    assert leader.group_members == []
