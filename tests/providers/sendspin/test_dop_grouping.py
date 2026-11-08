"""Tests for restarting a DoP stream as PCM when a Sendspin player gets grouped."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from aiosendspin.server.events import GroupMemberAddedEvent

from music_assistant.providers.sendspin.player import SendspinPlayer

LEADER_ID = "leader"
MEMBER_ID = "member"


def _leader(*, streaming_dop: bool) -> tuple[SendspinPlayer, MagicMock]:
    """Build an un-initialized SendspinPlayer leading a group, with its mocked mass."""
    player = SendspinPlayer.__new__(SendspinPlayer)
    player._player_id = LEADER_ID
    player._attr_group_members = []
    player.logger = MagicMock()
    player._config = MagicMock(name="config")
    mass = MagicMock()
    mass.players.get_active_queue.return_value = MagicMock(queue_id="queue-1")
    mass.streams.audio.is_streaming_dop.return_value = streaming_dop
    player.mass = mass
    return player, mass


def _group() -> MagicMock:
    group = MagicMock()
    group.clients = [MagicMock(client_id=LEADER_ID), MagicMock(client_id=MEMBER_ID)]
    return group


def test_member_joining_a_dop_stream_restarts_it() -> None:
    """A member joining while DoP plays replaces the membership sync with a restart."""
    player, mass = _leader(streaming_dop=True)
    group = _group()
    with (
        patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(player, "update_state", MagicMock()),
        patch.object(player, "_restart_dop_stream_for_group", MagicMock()) as restart,
        patch.object(player, "_schedule_membership_sync", MagicMock()) as sync,
    ):
        player.group_event_cb(group, GroupMemberAddedEvent(client_id=MEMBER_ID))

    restart.assert_called_once_with(group, "queue-1")
    sync.assert_not_called()
    assert mass.create_task.call_args.kwargs == {
        "task_id": f"sendspin_membership_sync_{LEADER_ID}",
        "abort_existing": True,
    }
    assert player._attr_group_members == [LEADER_ID, MEMBER_ID]


def test_member_joining_a_pcm_stream_only_syncs_membership() -> None:
    """Without DoP the running stream is kept and the member is simply attached."""
    player, mass = _leader(streaming_dop=False)
    group = _group()
    with (
        patch.object(SendspinPlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(player, "update_state", MagicMock()),
        patch.object(player, "_schedule_membership_sync", MagicMock()) as sync,
    ):
        player.group_event_cb(group, GroupMemberAddedEvent(client_id=MEMBER_ID))

    sync.assert_called_once_with(group)
    mass.create_task.assert_not_called()


async def test_restart_stops_before_attaching_the_member_and_resumes_in_place() -> None:
    """The DoP session stops before the member is attached, then the queue resumes there."""
    player, mass = _leader(streaming_dop=True)
    group = _group()
    calls: list[str] = []
    player.playback_session = MagicMock()
    player.playback_session.cancel = AsyncMock(side_effect=lambda *_a, **_k: calls.append("stop"))
    mass.player_queues.get.return_value = MagicMock(corrected_elapsed_time=42.7)
    mass.player_queues.seek = AsyncMock(side_effect=lambda *_a: calls.append("seek"))

    async def _sync(_group: object) -> None:
        calls.append("attach")

    with (
        patch.object(player, "_sync_membership_from_group", _sync),
        patch.object(SendspinPlayer, "display_name", new_callable=PropertyMock, return_value="L"),
    ):
        await player._restart_dop_stream_for_group(group, "queue-1")

    assert calls == ["stop", "attach", "seek"]
    mass.player_queues.seek.assert_awaited_once_with("queue-1", 42)
