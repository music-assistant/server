"""Tests for plugin-only Snapcast group materialization."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.snapcast.group_materialize import SnapcastGroupMaterializer
from music_assistant.providers.sync_group.constants import SGP_PREFIX


@pytest.mark.asyncio
async def test_materialize_creates_native_idle_group_for_first_snapcast_member() -> None:
    """The first Snapcast member in a dynamic MA sync group should materialize native state."""
    sync_group_player = SimpleNamespace(
        player_id=f"{SGP_PREFIX}abc12345",
        config=SimpleNamespace(name="broadcast"),
        group_members=["thor-speakers"],
        sync_leader=None,
        playback_state=PlaybackState.IDLE,
    )
    leader_player = SimpleNamespace(player_id="thor-speakers")
    idle_stream = SimpleNamespace(stream_id="broadcast", stream_display_name="broadcast")
    leader_group = SimpleNamespace(
        clients=["snap-thor"],
        stream="default",
        set_callback=MagicMock(),
        add_client=AsyncMock(),
        set_stream=AsyncMock(),
    )
    players = SimpleNamespace(trigger_player_update=MagicMock())
    players.get_player = MagicMock(
        side_effect=lambda ref, raise_unavailable=False: {  # noqa: ARG005
            sync_group_player.player_id: sync_group_player,
            "thor-speakers": leader_player,
        }.get(ref)
    )
    provider = SimpleNamespace(
        logger=MagicMock(),
        mass=SimpleNamespace(closing=False, players=players),
        get_snap_client=MagicMock(return_value=SimpleNamespace(identifier="snap-thor")),
        ensure_sync_group_idle_stream=AsyncMock(return_value=idle_stream),
        ensure_player_owned_group=AsyncMock(return_value=leader_group),
        move_player_to_fallback_group=AsyncMock(return_value=False),
        isolate_player_to_dedicated_group=AsyncMock(),
        _get_stable_stream_reference=MagicMock(return_value="broadcast"),
        _get_ma_id=MagicMock(return_value="thor-speakers"),
        _get_snapclient_id=MagicMock(return_value="snap-thor"),
        _update_group_callbacks=MagicMock(),
    )

    await SnapcastGroupMaterializer(provider).materialize(sync_group_player.player_id)  # type: ignore[arg-type]

    provider.ensure_sync_group_idle_stream.assert_awaited_once_with(sync_group_player)
    provider.ensure_player_owned_group.assert_awaited_once_with(
        "thor-speakers", set_stream_id="broadcast"
    )
    leader_group.set_stream.assert_awaited_once_with("broadcast")
    assert sync_group_player.sync_leader is leader_player
    players.trigger_player_update.assert_called_once_with(
        sync_group_player.player_id, force_update=True, debounce_delay=0
    )
    provider._update_group_callbacks.assert_called_once_with(poke=True)


@pytest.mark.asyncio
async def test_materialize_skips_when_sync_group_is_playing() -> None:
    """Active queue-backed playback should not be overridden by idle pre-materialization."""
    sync_group_player = SimpleNamespace(
        player_id=f"{SGP_PREFIX}abc12345",
        config=SimpleNamespace(name="broadcast"),
        group_members=["thor-speakers"],
        sync_leader=SimpleNamespace(player_id="thor-speakers"),
        playback_state=PlaybackState.PLAYING,
    )
    players = SimpleNamespace()
    players.get_player = MagicMock(return_value=sync_group_player)
    provider = SimpleNamespace(
        logger=MagicMock(),
        mass=SimpleNamespace(closing=False, players=players),
        ensure_sync_group_idle_stream=AsyncMock(),
    )

    await SnapcastGroupMaterializer(provider).materialize(sync_group_player.player_id)  # type: ignore[arg-type]

    provider.ensure_sync_group_idle_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_materialize_is_noop_when_native_state_already_matches() -> None:
    """Repeated materialization should not log or re-trigger updates when nothing changed."""
    leader_player = SimpleNamespace(player_id="thor-speakers")
    sync_group_player = SimpleNamespace(
        player_id=f"{SGP_PREFIX}abc12345",
        config=SimpleNamespace(name="broadcast"),
        group_members=["thor-speakers"],
        sync_leader=leader_player,
        playback_state=PlaybackState.IDLE,
    )
    idle_stream = SimpleNamespace(stream_id="broadcast", stream_display_name="broadcast")
    leader_group = SimpleNamespace(
        clients=["snap-thor"],
        stream="broadcast",
        set_callback=MagicMock(),
        add_client=AsyncMock(),
        set_stream=AsyncMock(),
    )
    logger = MagicMock()
    players = SimpleNamespace(trigger_player_update=MagicMock())
    players.get_player = MagicMock(
        side_effect=lambda ref, raise_unavailable=False: {  # noqa: ARG005
            sync_group_player.player_id: sync_group_player,
            "thor-speakers": leader_player,
        }.get(ref)
    )
    provider = SimpleNamespace(
        logger=logger,
        mass=SimpleNamespace(closing=False, players=players),
        get_snap_client=MagicMock(return_value=SimpleNamespace(identifier="snap-thor")),
        ensure_sync_group_idle_stream=AsyncMock(return_value=idle_stream),
        ensure_player_owned_group=AsyncMock(return_value=leader_group),
        move_player_to_fallback_group=AsyncMock(return_value=False),
        isolate_player_to_dedicated_group=AsyncMock(),
        _get_stable_stream_reference=MagicMock(return_value="broadcast"),
        _get_ma_id=MagicMock(return_value="thor-speakers"),
        _get_snapclient_id=MagicMock(return_value="snap-thor"),
        _update_group_callbacks=MagicMock(),
    )

    await SnapcastGroupMaterializer(provider).materialize(sync_group_player.player_id)  # type: ignore[arg-type]

    leader_group.add_client.assert_not_awaited()
    leader_group.set_stream.assert_not_awaited()
    players.trigger_player_update.assert_not_called()
    provider._update_group_callbacks.assert_not_called()
    logger.getChild.return_value.info.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_removes_idle_stream_when_sync_group_becomes_empty() -> None:
    """An empty sync group should clean up its previously materialized idle Snapcast stream."""
    sync_group_player = SimpleNamespace(
        player_id=f"{SGP_PREFIX}abc12345",
        config=SimpleNamespace(name="broadcast"),
        group_members=[],
        sync_leader=None,
        playback_state=PlaybackState.IDLE,
    )
    idle_stream = SimpleNamespace(
        stream_name="Music Assistant - idle_syncgroupabc12345",
        stream_display_name="broadcast",
        stream_id="broadcast",
        queue_id=None,
    )
    snap_group = SimpleNamespace(
        stream="broadcast",
        clients=["PC_VAIO_2"],
    )
    logger = MagicMock()
    players = SimpleNamespace()
    players.get_player = MagicMock(return_value=sync_group_player)
    provider = SimpleNamespace(
        logger=logger,
        mass=SimpleNamespace(closing=False, players=players),
        _snapserver=SimpleNamespace(groups=[snap_group]),
        _get_ma_id=MagicMock(return_value="PC_VAIO_2"),
        get_snap_ma_stream=MagicMock(return_value=idle_stream),
        move_player_to_fallback_group=AsyncMock(return_value=True),
        delete_ma_stream=AsyncMock(),
        _update_group_callbacks=MagicMock(),
    )

    await SnapcastGroupMaterializer(provider).materialize(sync_group_player.player_id)  # type: ignore[arg-type]

    provider.move_player_to_fallback_group.assert_awaited_once_with("PC_VAIO_2")
    provider.delete_ma_stream.assert_awaited_once_with(idle_stream.stream_name)
    provider._update_group_callbacks.assert_called_once_with(poke=True)
    logger.getChild.return_value.info.assert_called_once()


@pytest.mark.asyncio
async def test_materialize_moves_stale_old_leader_off_shared_stream() -> None:
    """Leader handoff should evict stale clients that still linger on the old broadcast group."""
    sync_group_player = SimpleNamespace(
        player_id=f"{SGP_PREFIX}abc12345",
        config=SimpleNamespace(name="broadcast"),
        group_members=["PC_VAIO_2"],
        sync_leader=None,
        playback_state=PlaybackState.IDLE,
    )
    leader_player = SimpleNamespace(player_id="PC_VAIO_2")
    idle_stream = SimpleNamespace(stream_id="broadcast", stream_display_name="broadcast")
    leader_group = SimpleNamespace(
        identifier="group-new-leader",
        clients=["snap-pc"],
        stream="broadcast",
        set_callback=MagicMock(),
        add_client=AsyncMock(),
        set_stream=AsyncMock(),
    )
    stale_group = SimpleNamespace(
        identifier="group-old-leader",
        clients=["snap-thor"],
        stream="broadcast",
    )
    players = SimpleNamespace(trigger_player_update=MagicMock())
    players.get_player = MagicMock(
        side_effect=lambda ref, raise_unavailable=False: {  # noqa: ARG005
            sync_group_player.player_id: sync_group_player,
            "PC_VAIO_2": leader_player,
        }.get(ref)
    )
    provider = SimpleNamespace(
        logger=MagicMock(),
        mass=SimpleNamespace(closing=False, players=players),
        _snapserver=SimpleNamespace(groups=[leader_group, stale_group]),
        get_snap_client=MagicMock(
            side_effect=lambda player_id=None, **kwargs: (  # noqa: ARG005
                SimpleNamespace(identifier="snap-pc") if player_id == "PC_VAIO_2" else None
            )
        ),
        ensure_sync_group_idle_stream=AsyncMock(return_value=idle_stream),
        ensure_player_owned_group=AsyncMock(return_value=leader_group),
        move_player_to_fallback_group=AsyncMock(return_value=True),
        isolate_player_to_dedicated_group=AsyncMock(),
        _get_stable_stream_reference=MagicMock(return_value="broadcast"),
        _get_ma_id=MagicMock(
            side_effect=lambda client_id: {
                "snap-pc": "PC_VAIO_2",
                "snap-thor": "thor-speakers",
            }[client_id]
        ),
        _get_snapclient_id=MagicMock(return_value="snap-pc"),
        _update_group_callbacks=MagicMock(),
    )

    await SnapcastGroupMaterializer(provider).materialize(sync_group_player.player_id)  # type: ignore[arg-type]

    provider.move_player_to_fallback_group.assert_awaited_once_with("thor-speakers")
