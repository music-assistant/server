"""
Tests for a start on a grouped player whose first item cannot be loaded.

A play request on a player loads its first item before the player is taken out of its group or
sync. While the group plays a source that allows one stream, that load fails while the player
still plays the group's stream. The failed start must not stop its device, which would silence
the whole group.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from functools import partial
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlayerType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.models.music_provider import MusicProvider, ProviderStreamLimitError

QUEUE_ID = "member"
FAILURES = ["stream-limit", "no-more-tracks", "no-item"]


class _Harness(NamedTuple):
    """The controller under test and the mocks it reports to."""

    ctrl: PlayerQueuesController
    queue_data: PlayerQueueData
    mass: MagicMock


def _player(
    player_id: str, synced_to: str | None = None, active_group: str | None = None
) -> SimpleNamespace:
    """
    Build a player with the given sync leader or group.

    :param player_id: The id of the player.
    :param synced_to: The player it is synced to, if any.
    :param active_group: The group player it plays along with, if any.
    """
    return SimpleNamespace(
        player_id=player_id,
        type=PlayerType.PLAYER,
        protocol_parent_id=None,
        state=SimpleNamespace(synced_to=synced_to, active_group=active_group),
    )


def _controller(synced_to: str | None = None, active_group: str | None = None) -> _Harness:
    """
    Build a bare controller with the queue of one player, synced or in a group as given.

    :param synced_to: The player the queue's player is synced to, if any.
    :param active_group: The group player the queue's player plays along with, if any.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Member", available=True, items=1
    )
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = [QueueItem(queue_id=QUEUE_ID, queue_item_id="i1", name="i1", duration=200)]
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.logger = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl._get_resume_position = AsyncMock(return_value=0)  # type: ignore[method-assign]
    ctrl._get_next_index = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl._cleanup_queue_audio_data = Mock()  # type: ignore[method-assign]

    mass = MagicMock()
    players = {
        player.player_id: player
        for player in (
            _player(QUEUE_ID, synced_to=synced_to, active_group=active_group),
            _player("leader"),
            _player("group"),
        )
    }
    mass.players.get_player = Mock(side_effect=lambda player_id, *_args: players.get(player_id))
    # the real resolution of whose stream a player plays
    mass.players._audio_source_owner = partial(PlayerController._audio_source_owner, mass.players)
    mass.players._handle_cmd_stop = AsyncMock()
    mass.players.play_media = AsyncMock()

    @contextlib.asynccontextmanager
    async def _no_lock(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        """Stand in for the playback lock the play and stop actions take."""
        yield

    mass.players.get_group_and_player_lock = _no_lock
    ctrl.mass = mass
    return _Harness(ctrl, queue_data, mass)


async def _fail_start(harness: _Harness, failure: str) -> str:
    """
    Start the queue so that it fails at the given point, expect its error and return its session.

    :param harness: The controller to start the queue on.
    :param failure: stream-limit (the provider has no stream left), no-more-tracks (the item
        cannot be played and none follows) or no-item (the index has no item).
    """
    ctrl = harness.ctrl
    if failure == "stream-limit":
        provider = MagicMock(spec=MusicProvider)
        provider.max_concurrent_streams = 1
        provider.name = "Spotify"
        provider.instance_id = "spotify--1"
        ctrl._load_item = AsyncMock(  # type: ignore[method-assign]
            side_effect=ProviderStreamLimitError(provider, 15)
        )
        with pytest.raises(ProviderStreamLimitError):
            await ctrl.play_index(QUEUE_ID, 0)
    elif failure == "no-more-tracks":
        ctrl._load_item = AsyncMock(side_effect=MediaNotFoundError("unplayable"))  # type: ignore[method-assign]
        with pytest.raises(MediaNotFoundError, match="no more tracks"):
            await ctrl.play_index(QUEUE_ID, 0)
    else:
        ctrl._load_item = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(MediaNotFoundError, match="No playable item"):
            await ctrl.play_index(QUEUE_ID, 5)
    session_id: str = harness.mass.streams.audio_processing.start_session.call_args.args[1]
    return session_id


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize(
    ("synced_to", "active_group"),
    [("leader", None), (None, "group")],
    ids=["synced", "group-member"],
)
async def test_failed_start_keeps_the_group_playing(
    failure: str, synced_to: str | None, active_group: str | None
) -> None:
    """A player still playing its group's stream is not stopped when its own start fails."""
    harness = _controller(synced_to=synced_to, active_group=active_group)

    session_id = await _fail_start(harness, failure)

    harness.mass.players._handle_cmd_stop.assert_not_awaited()
    harness.mass.players.play_media.assert_not_awaited()
    # the session the failed start opened still ends, with whatever audio it attached
    assert harness.queue_data.session_id is None
    harness.mass.streams.audio_processing.clear.assert_called_once_with(QUEUE_ID, session_id)
    harness.ctrl._cleanup_queue_audio_data.assert_called_once_with(QUEUE_ID, session_id)  # type: ignore[attr-defined]


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("synced_to", [None, QUEUE_ID], ids=["ungrouped", "synced-to-itself"])
async def test_failed_start_stops_a_player_on_its_own(failure: str, synced_to: str | None) -> None:
    """A player that plays on its own is stopped when its start fails."""
    harness = _controller(synced_to=synced_to)

    session_id = await _fail_start(harness, failure)

    harness.mass.players._handle_cmd_stop.assert_awaited_once_with(QUEUE_ID)
    assert harness.queue_data.session_id is None
    harness.mass.streams.audio_processing.clear.assert_called_once_with(QUEUE_ID, session_id)
    harness.ctrl._cleanup_queue_audio_data.assert_called_once_with(QUEUE_ID, session_id)  # type: ignore[attr-defined]
