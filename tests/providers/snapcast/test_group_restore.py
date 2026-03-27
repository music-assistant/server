"""Tests for plugin-only Snapcast group restore."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature

from music_assistant.providers.snapcast.group_restore import SnapcastGroupRestorer
from music_assistant.providers.snapcast.stream_registry import SnapcastStreamRegistry
from music_assistant.providers.sync_group.constants import SGP_PREFIX


def test_collect_restore_targets_matches_live_snapcast_group_to_sync_group_name() -> None:
    """A live Snapcast group should resolve to the matching MA sync group by stream name."""
    sync_group_player = SimpleNamespace(
        player_id=f"{SGP_PREFIX}abc12345",
        config=SimpleNamespace(name="broadcast"),
        state=SimpleNamespace(supported_features={PlayerFeature.SET_MEMBERS}),
    )
    mass = SimpleNamespace(
        closing=False,
        players=SimpleNamespace(
            get_player=MagicMock(
                side_effect=lambda ref, raise_unavailable=False: (  # noqa: ARG005
                    sync_group_player if ref == sync_group_player.player_id else None
                )
            ),
            __iter__=MagicMock(return_value=iter([sync_group_player])),
        ),
    )
    snap_stream = SimpleNamespace(
        identifier="snap-stream-123",
        friendly_name="broadcast",
        _stream={"uri": {"raw": "tcp://0.0.0.0:4978?name=broadcast"}},
    )
    snap_group = SimpleNamespace(
        identifier="group-1",
        name="PC_VAIO_2",
        clients=["PC_VAIO_2", "thor-speakers"],
        stream="snap-stream-123",
    )
    provider: Any = SimpleNamespace(
        logger=MagicMock(),
        mass=mass,
        _snapserver=SimpleNamespace(
            groups=[snap_group],
            streams=[snap_stream],
            stream=MagicMock(return_value=snap_stream),
        ),
        _get_stream_registry=MagicMock(return_value=SnapcastStreamRegistry()),
        _get_ma_id=MagicMock(
            side_effect=lambda snap_id: {
                "PC_VAIO_2": "PC_VAIO_2",
                "thor-speakers": "thor-speakers",
            }[snap_id]
        ),
        resolve_sync_group_player=MagicMock(return_value=sync_group_player),
        dedicated_fallback_group_name=None,
    )

    targets = SnapcastGroupRestorer(provider)._collect_restore_targets()

    assert len(targets) == 1
    assert targets[0].sync_group_player_id == sync_group_player.player_id
    assert targets[0].sync_group_name == "broadcast"
    assert targets[0].stream_display_name == "broadcast"
    assert targets[0].sync_leader_id == "PC_VAIO_2"
    assert targets[0].member_player_ids == ["PC_VAIO_2", "thor-speakers"]


def test_collect_restore_targets_skips_configured_fallback_group() -> None:
    """The external dedicated fallback group must never be restored as an MA sync group."""
    sync_group_player = SimpleNamespace(
        player_id=f"{SGP_PREFIX}abc12345",
        config=SimpleNamespace(name="broadcast"),
        state=SimpleNamespace(supported_features={PlayerFeature.SET_MEMBERS}),
    )
    mass = SimpleNamespace(
        closing=False,
        players=SimpleNamespace(
            get_player=MagicMock(
                side_effect=lambda ref, raise_unavailable=False: (  # noqa: ARG005
                    sync_group_player if ref == sync_group_player.player_id else None
                )
            ),
            __iter__=MagicMock(return_value=iter([sync_group_player])),
        ),
    )
    snap_stream = SimpleNamespace(
        identifier="snap-stream-123",
        friendly_name="broadcast",
        _stream={"uri": {"raw": "tcp://0.0.0.0:4978?name=broadcast"}},
    )
    fallback_group = SimpleNamespace(
        identifier="group-media",
        name="Media",
        clients=["PC_VAIO_2", "thor-speakers"],
        stream="default",
    )
    broadcast_group = SimpleNamespace(
        identifier="group-1",
        name="PC_VAIO_2",
        clients=["PC_VAIO_2", "thor-speakers"],
        stream="snap-stream-123",
    )
    provider: Any = SimpleNamespace(
        logger=MagicMock(),
        mass=mass,
        dedicated_fallback_group_name="Media",
        _snapserver=SimpleNamespace(
            groups=[fallback_group, broadcast_group],
            streams=[snap_stream],
            stream=MagicMock(return_value=snap_stream),
        ),
        _get_stream_registry=MagicMock(return_value=SnapcastStreamRegistry()),
        _get_ma_id=MagicMock(
            side_effect=lambda snap_id: {
                "PC_VAIO_2": "PC_VAIO_2",
                "thor-speakers": "thor-speakers",
            }[snap_id]
        ),
        resolve_sync_group_player=MagicMock(return_value=sync_group_player),
    )

    targets = SnapcastGroupRestorer(provider)._collect_restore_targets()

    assert len(targets) == 1
    assert targets[0].sync_group_name == "broadcast"


@pytest.mark.asyncio
async def test_restore_updates_runtime_members_and_sync_leader() -> None:
    """Restoring a live group should use public set-members flow and set the runtime leader."""
    sync_group_player = SimpleNamespace(
        player_id=f"{SGP_PREFIX}abc12345",
        config=SimpleNamespace(name="broadcast"),
        state=SimpleNamespace(supported_features={PlayerFeature.SET_MEMBERS}),
        group_members=[],
        static_group_members=[],
        sync_leader=None,
    )
    leader_player = SimpleNamespace(
        player_id="PC_VAIO_2",
        state=SimpleNamespace(group_members=["PC_VAIO_2", "thor-speakers"]),
    )
    member_player = SimpleNamespace(player_id="thor-speakers")
    players = SimpleNamespace(
        cmd_set_members=AsyncMock(),
        trigger_player_update=MagicMock(),
    )
    players.get_player = MagicMock(
        side_effect=lambda ref, raise_unavailable=False: {  # noqa: ARG005
            sync_group_player.player_id: sync_group_player,
            "PC_VAIO_2": leader_player,
            "thor-speakers": member_player,
        }.get(ref)
    )
    mass = SimpleNamespace(closing=False, players=players)
    provider: Any = SimpleNamespace(logger=MagicMock(), mass=mass)
    target: Any = SimpleNamespace(
        sync_group_player_id=sync_group_player.player_id,
        sync_group_name="broadcast",
        stream_display_name="broadcast",
        sync_leader_id="PC_VAIO_2",
        member_player_ids=["PC_VAIO_2", "thor-speakers"],
    )

    await SnapcastGroupRestorer(provider)._restore_target(target)

    players.cmd_set_members.assert_awaited_once_with(
        sync_group_player.player_id,
        player_ids_to_add=["PC_VAIO_2", "thor-speakers"],
        player_ids_to_remove=None,
    )
    assert sync_group_player.sync_leader is leader_player
    players.trigger_player_update.assert_called_once_with(
        sync_group_player.player_id, force_update=True, debounce_delay=0
    )
