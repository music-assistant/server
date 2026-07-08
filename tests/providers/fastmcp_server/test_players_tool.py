"""
Tests for the ``players`` sub-server tools.

End-to-end through a FastMCP ``Client`` so the parameter signature and
filtering behaviour exposed to MCP clients are pinned, not just the
in-process helpers.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.tools import build_players_server, build_queue_server


def _player(
    *,
    player_id: str,
    name: str,
    available: bool = True,
    enabled: bool = True,
    state: str = "idle",
    needs_setup: bool = False,
    active_group: str | None = None,
    synced_to: str | None = None,
) -> SimpleNamespace:
    """Build a minimal player stub that satisfies ``to_brief_player``."""
    return SimpleNamespace(
        player_id=player_id,
        name=name,
        playback_state=SimpleNamespace(value=state),
        volume_level=None,
        powered=True,
        current_media=None,
        available=available,
        enabled=enabled,
        needs_setup=needs_setup,
        active_group=active_group,
        synced_to=synced_to,
    )


def _make_all_players_mock(roster: list[SimpleNamespace]) -> Any:
    """
    Side effect mirroring MA's ``all_players(return_unavailable, return_disabled)`` filter.

    Lets the tests pin the contract — "we forward the flag to MA" — without
    coupling to a re-implementation in Python.
    """

    def side_effect(
        *,
        return_unavailable: bool = True,
        return_disabled: bool = False,
        **_kwargs: Any,
    ) -> list[Any]:
        result = list(roster)
        if not return_unavailable:
            result = [p for p in result if p.available]
        if not return_disabled:
            result = [p for p in result if p.enabled]
        return result

    return side_effect


@pytest.fixture
def mounted_players(mock_mass: Any) -> Iterator[FastMCP]:
    """
    Build a root FastMCP with only the players sub-server mounted.

    Yields rather than returns so future FastMCP lifecycle methods (e.g.
    a ``close()`` / ``shutdown()`` once upstream adds one) can be wired
    into the ``finally`` block without per-test churn. ``contextlib.suppress``
    keeps the teardown idempotent against an environment where ``close``
    is later renamed or removed.
    """
    mcp: FastMCP = FastMCP(name="test")
    mcp.mount(build_players_server(mock_mass), namespace="players")
    try:
        yield mcp
    finally:
        close = getattr(mcp, "close", None) or getattr(mcp, "shutdown", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


async def test_list_players_hides_unavailable_by_default(
    mock_mass: Any, mounted_players: FastMCP
) -> None:
    """
    Default ``list_players`` call asks MA to omit unavailable and disabled players.

    Matches the spec: a model asked to pick a speaker should not see
    devices MA can no longer reach or that the admin has disabled.
    The filter lives in MA's controller, so the contract this test
    pins is that the tool forwards both flags to ``all_players``.
    """
    roster = [
        _player(player_id="ok", name="Kitchen", available=True),
        _player(player_id="gone", name="Bedroom", available=False),
    ]
    mock_mass.players.all_players.side_effect = _make_all_players_mock(roster)
    async with Client(mounted_players) as client:
        result = await client.call_tool("players_list_players", {})
    ids = {p.player_id for p in result.data}
    assert ids == {"ok"}, "unavailable players must be filtered out by default"
    mock_mass.players.all_players.assert_called_with(
        return_unavailable=False, return_disabled=False
    )


async def test_list_players_include_unavailable_returns_all(
    mock_mass: Any, mounted_players: FastMCP
) -> None:
    """
    With ``include_unavailable=True`` every player comes through.

    The unavailable one is still tagged ``state="unavailable"`` so the
    caller can act on it.
    """
    roster = [
        _player(player_id="ok", name="Kitchen", available=True),
        _player(player_id="gone", name="Bedroom", available=False),
    ]
    mock_mass.players.all_players.side_effect = _make_all_players_mock(roster)
    async with Client(mounted_players) as client:
        result = await client.call_tool("players_list_players", {"include_unavailable": True})
    by_id = {p.player_id: p for p in result.data}
    assert set(by_id) == {"ok", "gone"}
    assert by_id["gone"].state == "unavailable"
    assert by_id["ok"].state == "idle"
    mock_mass.players.all_players.assert_called_with(return_unavailable=True, return_disabled=False)


async def test_list_players_include_disabled_returns_disabled(
    mock_mass: Any, mounted_players: FastMCP
) -> None:
    """
    With ``include_disabled=True`` admin-disabled players surface as ``state="disabled"``.

    Without the flag MA filters them out before they reach the brief,
    so the ``enabled`` field on the response is always ``True``.
    """
    roster = [
        _player(player_id="ok", name="Kitchen", available=True, enabled=True),
        _player(player_id="off", name="Closet", available=True, enabled=False),
    ]
    mock_mass.players.all_players.side_effect = _make_all_players_mock(roster)
    async with Client(mounted_players) as client:
        result = await client.call_tool("players_list_players", {"include_disabled": True})
    by_id = {p.player_id: p for p in result.data}
    assert set(by_id) == {"ok", "off"}
    assert by_id["off"].enabled is False
    assert by_id["off"].state == "disabled"
    mock_mass.players.all_players.assert_called_with(return_unavailable=False, return_disabled=True)


async def test_list_players_synced_player_reports_synced_state(
    mock_mass: Any, mounted_players: FastMCP
) -> None:
    """
    A player belonging to an active sync group reports ``state="synced"``.

    The cached ``playback_state`` on a sync follower stays ``idle``
    because the device doesn't have its own queue — the group plays
    through it. Without this synthesis an LLM would treat a sync
    follower as a quiet, idle speaker.
    """
    roster = [
        _player(
            player_id="follower",
            name="Lenco LS-500",
            available=True,
            active_group="syncgroup_x",
        ),
    ]
    mock_mass.players.all_players.side_effect = _make_all_players_mock(roster)
    async with Client(mounted_players) as client:
        result = await client.call_tool("players_list_players", {})
    by_id = {p.player_id: p for p in result.data}
    assert by_id["follower"].state == "synced"
    assert by_id["follower"].active_group == "syncgroup_x"


async def test_list_players_synced_reads_from_state_object(
    mock_mass: Any, mounted_players: FastMCP
) -> None:
    """
    ``state.active_group`` (canonical) wins over the raw dataclass attr.

    Mirrors the live MA shape — ``Player.state.active_group`` is set
    by ``__final_active_group`` while the raw ``Player.active_group``
    field stays ``None`` for SyncGroupPlayer followers. The brief must
    read the canonical signal end-to-end through the tool.
    """
    follower = SimpleNamespace(
        player_id="follower",
        name="Kitchen",
        playback_state=SimpleNamespace(value="idle"),
        volume_level=None,
        powered=True,
        current_media=None,
        available=True,
        enabled=True,
        needs_setup=False,
        # Raw attrs are None — the bug condition we shipped in 0.3.32.
        active_group=None,
        synced_to=None,
        # Canonical view carries the resolved group id.
        state=SimpleNamespace(
            powered=True,
            current_media=None,
            active_group="syncgroup_x",
            synced_to=None,
        ),
    )
    mock_mass.players.all_players.side_effect = _make_all_players_mock([follower])
    async with Client(mounted_players) as client:
        result = await client.call_tool("players_list_players", {})
    by_id = {p.player_id: p for p in result.data}
    assert by_id["follower"].state == "synced"
    assert by_id["follower"].active_group == "syncgroup_x"


async def test_list_players_syncgroup_reports_group_volume(
    mock_mass: Any, mounted_players: FastMCP
) -> None:
    """
    A SyncGroupPlayer surfaces its ``group_volume`` round-tripped through MCP.

    Per-player ``volume_level`` is ``None`` on a sync group; the
    canonical volume signal lives on ``state.group_volume``. The brief
    must surface this so a caller can answer "what volume is the
    group at?" without a separate query.
    """
    syncgroup = SimpleNamespace(
        player_id="syncgroup_x",
        name="Test Group",
        playback_state=SimpleNamespace(value="playing"),
        volume_level=None,
        powered=True,
        current_media=None,
        available=True,
        enabled=True,
        needs_setup=False,
        active_group=None,
        synced_to=None,
        state=SimpleNamespace(
            powered=True,
            current_media=None,
            active_group=None,
            synced_to=None,
            volume_muted=False,
            group_volume=60,
            group_volume_muted=False,
        ),
    )
    mock_mass.players.all_players.side_effect = _make_all_players_mock([syncgroup])
    async with Client(mounted_players) as client:
        result = await client.call_tool("players_list_players", {})
    by_id = {p.player_id: p for p in result.data}
    assert by_id["syncgroup_x"].group_volume == 60
    assert by_id["syncgroup_x"].group_volume_muted is False
    assert by_id["syncgroup_x"].volume_muted is False
    assert by_id["syncgroup_x"].volume_level is None


async def test_list_players_needs_setup_reports_needs_setup_state(
    mock_mass: Any, mounted_players: FastMCP
) -> None:
    """A player still awaiting first-run setup reports ``state="needs_setup"``."""
    roster = [
        _player(
            player_id="raw",
            name="Unboxed Speaker",
            available=True,
            needs_setup=True,
        ),
    ]
    mock_mass.players.all_players.side_effect = _make_all_players_mock(roster)
    async with Client(mounted_players) as client:
        result = await client.call_tool("players_list_players", {})
    by_id = {p.player_id: p for p in result.data}
    assert by_id["raw"].state == "needs_setup"
    assert by_id["raw"].needs_setup is True


async def test_get_player_returns_unavailable_player(
    mock_mass: Any, mounted_players: FastMCP
) -> None:
    """Direct id lookup ignores availability — the caller already has the id."""
    mock_mass.players.get_player.return_value = _player(
        player_id="gone", name="Bedroom", available=False
    )
    async with Client(mounted_players) as client:
        result = await client.call_tool("players_get_player", {"player_id": "gone"})
    assert result.data.player_id == "gone"
    assert result.data.available is False
    assert result.data.state == "unavailable"


async def test_group_player_calls_cmd_group(mock_mass: Any, mounted_players: FastMCP) -> None:
    """``players_group_player`` forwards to ``mass.players.cmd_group``."""
    async with Client(mounted_players) as client:
        await client.call_tool(
            "players_group_player",
            {"player_id": "follower", "target_player_id": "leader"},
        )
    mock_mass.players.cmd_group.assert_awaited_once_with("follower", "leader")


async def test_ungroup_player_calls_cmd_ungroup(mock_mass: Any, mounted_players: FastMCP) -> None:
    """``players_ungroup_player`` forwards to ``mass.players.cmd_ungroup``."""
    async with Client(mounted_players) as client:
        await client.call_tool("players_ungroup_player", {"player_id": "follower"})
    mock_mass.players.cmd_ungroup.assert_awaited_once_with("follower")


async def test_get_player_reports_external_source(mock_mass: Any, mounted_players: FastMCP) -> None:
    """An idle player driven by a Connect source reports playing + provider."""
    player = _player(player_id="lenco", name="Lenco LS-500", state="idle")
    mock_mass.players.get_player.return_value = player
    queue = SimpleNamespace(
        state=SimpleNamespace(value="playing"),
        current_item=SimpleNamespace(
            name="Yandex Music Connect (Ynison)",
            streamdetails=SimpleNamespace(
                media_type=SimpleNamespace(value="audio_source"),
                provider="yandex_ynison--PL8BnL7a",
                stream_metadata=SimpleNamespace(title="Behind Your Walls"),
            ),
        ),
    )
    mock_mass.player_queues.get_active_queue.return_value = queue
    async with Client(mounted_players) as client:
        result = await client.call_tool("players_get_player", {"player_id": "lenco"})
    assert result.data.state == "playing"
    assert result.data.external_source == "yandex_ynison--PL8BnL7a"
    assert result.data.current_item == "Behind Your Walls"


def _ns(obj: Any) -> Any:
    """Recursively turn dicts/lists into attribute-accessible namespaces."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


async def test_queue_get_active_queue_external_item_title(mock_mass: Any) -> None:
    """queue_get_active_queue surfaces the real title for an AUDIO_SOURCE item."""
    raw = json.loads(
        Path(__file__).parent.joinpath("fixtures/queue_external_audio_source.json").read_text()
    )
    queue = _ns(raw)
    mock_mass.player_queues.get_active_queue.return_value = queue
    mock_mass.player_queues.items.return_value = [queue.current_item]

    mcp = FastMCP(name="test")
    mcp.mount(build_queue_server(mock_mass), namespace="queue")
    async with Client(mcp) as client:
        result = await client.call_tool("queue_get_active_queue", {"player_id": "lenco"})
    assert result.data.items[0].name == "Behind Your Walls"
    mock_mass.player_queues.items.assert_called_with("lenco", limit=25, offset=0)
