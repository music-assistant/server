"""
Unit tests for AirPlay protocol crash-handler teardown behaviour.

When a CLI stream process dies unexpectedly the protocol must hand off to the
player controller (so it can drop just this member or transfer leadership) and
must NOT pre-mark a sync leader idle — doing so makes the active queue look
stopped and forces the controller to dissolve the whole group instead of
transferring leadership to a healthy member.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.airplay.constants import StreamingProtocol
from music_assistant.providers.airplay.protocols.airplay2 import AirPlay2Stream
from music_assistant.providers.airplay.protocols.raop import RaopStream


def _make_stream(
    stream_cls: type[AirPlay2Stream | RaopStream],
    group_members: list[str],
) -> tuple[AirPlay2Stream | RaopStream, MagicMock, MagicMock]:
    """
    Build a protocol stream wired to a mock player whose CLI process has died.

    :param stream_cls: The protocol stream class to instantiate.
    :param group_members: The player's group_members (non-empty marks a sync leader).
    """
    player = MagicMock()
    player.player_id = "apaabbccddeeff"
    player.display_name = "Test Player"
    player.protocol = StreamingProtocol.AIRPLAY2
    player.group_members = group_members
    player.device_info.mac_address = "AA:BB:CC:DD:EE:FF"
    player.logger = logging.getLogger("test.airplay.player")
    player.set_state_from_stream = MagicMock()

    mass = MagicMock()
    mass.create_task = MagicMock()
    mass.players.cmd_ungroup = MagicMock(return_value="ungroup-coro")
    player.provider.mass = mass
    player.provider.logger = logging.getLogger("test.airplay.prov")

    stream = stream_cls(player)

    # Mock a CLI process whose stderr is already exhausted: the reader loop
    # exits immediately without seeing an end-of-stream marker, i.e. an
    # unexpected (crash) stop.
    async def _empty_stderr() -> AsyncIterator[str]:
        return
        yield  # pragma: no cover - makes this an async generator

    cli_proc = MagicMock()
    cli_proc.iter_stderr = MagicMock(side_effect=lambda: _empty_stderr())
    stream._cli_proc = cli_proc

    return stream, player, mass


@pytest.mark.parametrize("stream_cls", [AirPlay2Stream, RaopStream])
@pytest.mark.asyncio
async def test_crash_on_sync_leader_defers_idle_and_ungroups(
    stream_cls: type[AirPlay2Stream | RaopStream],
) -> None:
    """A crashed sync leader is ungrouped but NOT pre-marked idle (lets controller transfer)."""
    stream, player, mass = _make_stream(stream_cls, group_members=["apaabbccddeeff", "member"])

    await stream._stderr_reader()

    mass.players.cmd_ungroup.assert_called_once_with(player.player_id)
    mass.create_task.assert_called_once()
    # Leader must NOT be forced idle here — that would dissolve the group.
    player.set_state_from_stream.assert_not_called()


@pytest.mark.parametrize("stream_cls", [AirPlay2Stream, RaopStream])
@pytest.mark.asyncio
async def test_crash_on_member_idles_and_ungroups(
    stream_cls: type[AirPlay2Stream | RaopStream],
) -> None:
    """A crashed non-leader member (no group_members) is marked idle and ungrouped."""
    stream, player, mass = _make_stream(stream_cls, group_members=[])

    await stream._stderr_reader()

    mass.players.cmd_ungroup.assert_called_once_with(player.player_id)
    mass.create_task.assert_called_once()
    # Members / standalone players are reflected idle right away.
    player.set_state_from_stream.assert_called_once()
    _, kwargs = player.set_state_from_stream.call_args
    assert kwargs.get("elapsed_time") == 0
    assert kwargs.get("stream") is stream
    state_arg: Any = kwargs.get("state")
    assert state_arg is not None
    assert state_arg.value == "idle"
