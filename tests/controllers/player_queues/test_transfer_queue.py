"""Tests for PlayerQueuesController.transfer_queue protocol/ungroup and settings handover."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import AlbumType, PlaybackState, PlayerType
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import Album, Track
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


class _DummyACM:
    """Minimal async context manager stand-in for wait_for_player_update."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


def _fake_controller(source_id: str, target_player: MagicMock) -> MagicMock:
    """
    Build a MagicMock standing in for the controller for transfer_queue tests.

    The source queue is idle so the capture/stop/resume path is a no-op; the test
    only exercises the pre-transfer ungroup branch.
    """
    source_queue = MagicMock()
    source_queue.state = PlaybackState.IDLE
    source_queue.resume_pos = 0
    source_queue.elapsed_time = 0
    source_queue.current_index = 0
    source_queue.current_item = None
    target_queue = MagicMock()

    fake = MagicMock()
    fake.get = MagicMock(side_effect=lambda qid: source_queue if qid == source_id else target_queue)
    fake.stop = AsyncMock()
    fake.load = AsyncMock()
    fake.resume = AsyncMock()
    fake.clear = MagicMock()
    fake.update_items = MagicMock()
    fake._notify_audio_source_transferred = AsyncMock()
    fake.mass.players.get_player = MagicMock(return_value=target_player)
    fake.mass.players.cmd_ungroup = AsyncMock()
    fake.mass.players.wait_for_player_update = MagicMock(return_value=_DummyACM())
    fake.mass.streams.is_smart_fades_active = MagicMock(return_value=False)
    return fake


async def test_transfer_queue_ad_hoc_member_ungroups_target_not_leader() -> None:
    """
    Transferring onto an ad-hoc sync member frees the target itself, not its leader.

    Ungrouping the leader would transfer leadership to a remaining member and
    recurse back into transfer_queue, so only the target may be ungrouped.
    """
    target_player = MagicMock()
    target_player.state.type = PlayerType.PLAYER
    target_player.state.synced_to = "leaderA"
    target_player.state.active_group = None
    fake = _fake_controller("src", target_player)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "memberB", auto_play=False
    )

    fake.mass.players.cmd_ungroup.assert_awaited_once_with("memberB")


async def test_transfer_queue_refuses_non_audio_target() -> None:
    """
    A target that can never render audio is refused before anything is touched.

    The source queue must survive intact: no stop, no clear, no item handover.
    """
    target_player = MagicMock()
    target_player.state.type = PlayerType.VISUALIZER
    fake = _fake_controller("src", target_player)

    with pytest.raises(PlayerCommandFailed):
        await PlayerQueuesController.transfer_queue(
            cast("PlayerQueuesController", fake), "src", "viz", auto_play=False
        )

    fake.stop.assert_not_awaited()
    fake._clear.assert_not_called()
    fake.load.assert_not_awaited()
    fake.mass.players.cmd_ungroup.assert_not_awaited()


async def test_transfer_queue_group_member_ungroups_group() -> None:
    """Transferring onto a virtual-group member releases the group player itself."""
    target_player = MagicMock()
    target_player.state.type = PlayerType.PLAYER
    target_player.state.synced_to = None
    target_player.state.active_group = "groupP"
    fake = _fake_controller("src", target_player)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "memberB", auto_play=False
    )

    fake.mass.players.cmd_ungroup.assert_awaited_once_with("groupP")


def _shuffle_controller(
    source_shuffle_enabled: bool,
    target_shuffle_enabled: bool = False,
    source_is_dynamic: bool = False,
) -> MagicMock:
    """
    Build a controller stand-in whose two queues carry real state records.

    The shuffle flag lives on the per-queue state record, so unlike the ungroup tests above these
    need real queues rather than one shared mock.

    :param source_shuffle_enabled: Whether shuffle is on for the queue being handed over.
    :param target_shuffle_enabled: Whether shuffle is on for the queue being handed to.
    :param source_is_dynamic: Whether the source queue is managed by a dynamic source.
    """
    source_queue = PlayerQueue(
        queue_id="src",
        active=True,
        display_name="Src",
        available=True,
        items=0,
        shuffle_enabled=source_shuffle_enabled,
        smart_shuffle_active=source_is_dynamic,
        is_dynamic=source_is_dynamic,
    )
    target_queue = PlayerQueue(
        queue_id="tgt",
        active=True,
        display_name="Tgt",
        available=True,
        items=0,
        shuffle_enabled=target_shuffle_enabled,
    )

    fake = MagicMock()
    fake.get = MagicMock(side_effect=lambda qid: source_queue if qid == "src" else target_queue)
    fake._queue_data = {
        "src": PlayerQueueData(queue=source_queue),
        "tgt": PlayerQueueData(queue=target_queue),
    }
    fake.stop = AsyncMock()
    fake.load = AsyncMock()
    fake.resume = AsyncMock()
    fake._clear = MagicMock()
    fake.update_items = MagicMock()
    fake._notify_audio_source_transferred = AsyncMock()
    fake.is_smart_shuffle_active = MagicMock(side_effect=lambda queue: queue.is_dynamic)
    # bind the real settings-copy helper so the handover under test exercises actual logic
    fake._copy_queue_settings = lambda source_queue_id, target_queue_id: (
        PlayerQueuesController._copy_queue_settings(fake, source_queue_id, target_queue_id)
    )
    target_player = MagicMock()
    target_player.state.type = PlayerType.PLAYER
    target_player.state.synced_to = None
    target_player.state.active_group = None
    fake.mass.players.get_player = MagicMock(return_value=target_player)
    fake.mass.streams.is_smart_fades_active = MagicMock(return_value=False)
    return fake


async def test_transfer_queue_carries_the_album_credit_bookkeeping() -> None:
    """A credited album stays credited on the player the queue is handed to."""
    fake = _shuffle_controller(source_shuffle_enabled=False)
    album = Album(
        item_id="a1",
        provider="library",
        name="A",
        provider_mappings=set(),
        album_type=AlbumType.ALBUM,
    )
    track = Track(item_id="t1", provider="library", name="T1", provider_mappings=set(), album=album)
    source_data = fake._queue_data["src"]
    source_data.enqueued_media_items = [album]
    source_data.credited_albums = {album}

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    target_data = fake._queue_data["tgt"]
    # the target holds its own copy, so the source's set no longer drives it
    assert target_data.credited_albums == {album}
    assert target_data.credited_albums is not source_data.credited_albums
    # and the album is not credited a second time on the new player
    assert (
        PlayerQueuesController._claim_enqueued_album_credit(
            cast("PlayerQueuesController", fake), target_data, track
        )
        is None
    )


async def test_transfer_queue_overwrites_the_targets_own_shuffle() -> None:
    """The queue brings its own shuffle state, so the target's previous one does not survive."""
    fake = _shuffle_controller(source_shuffle_enabled=False, target_shuffle_enabled=True)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake.get("tgt").shuffle_enabled is False


async def test_transfer_queue_carries_the_source_shuffle() -> None:
    """A shuffled queue handed to another player stays shuffled there."""
    fake = _shuffle_controller(source_shuffle_enabled=True)

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake.get("tgt").shuffle_enabled is True


async def test_transfer_queue_drops_dynamic_shuffle_from_source() -> None:
    """The shuffle imposed by a dynamic source follows it to the target queue."""
    fake = _shuffle_controller(source_shuffle_enabled=True, source_is_dynamic=True)
    fake._clear.side_effect = lambda queue_id, skip_stop=False: PlayerQueuesController._clear(
        cast("PlayerQueuesController", fake), queue_id, skip_stop
    )

    await PlayerQueuesController.transfer_queue(
        cast("PlayerQueuesController", fake), "src", "tgt", auto_play=False
    )

    assert fake.get("tgt").is_dynamic is True
    assert fake.get("tgt").shuffle_enabled is True
    assert fake.get("src").is_dynamic is False
    assert fake.get("src").shuffle_enabled is False
    assert fake.get("src").smart_shuffle_active is False
