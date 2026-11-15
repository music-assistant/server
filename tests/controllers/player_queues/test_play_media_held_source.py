"""
Tests for refusing a play whose sources are held elsewhere, before its tracks are fetched.

A large playlist takes a while to expand into tracks. A play of a playlist, album or track
that starts right away is checked against the slots that realtime playback on other players
holds as soon as the requested item itself is known, so a refusal comes at once and leaves the
queue as it was. Anything else is left to the check when its audio is streamed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.media_items import Artist, ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.models.music_provider import MusicProvider, ProviderStreamHeldError

GET_CURRENT_USER = "music_assistant.controllers.player_queues.queue_loader.get_current_user"
LISTENER = User(user_id="listener", username="listener", role=UserRole.USER)
INSTANCE = "spotify--one"


def _track(item_id: str) -> Track:
    """Build a playable track on the single-slot provider."""
    return Track(
        item_id=item_id,
        provider=INSTANCE,
        name=f"Track {item_id}",
        duration=180,
        artists=UniqueList(
            [ItemMapping(item_id="a", provider=INSTANCE, name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="spotify", provider_instance=INSTANCE)
        },
    )


def _held_error() -> ProviderStreamHeldError:
    """Build the refusal for a provider whose only slot the living room's playback holds."""
    provider = MagicMock(spec=MusicProvider)
    provider.max_concurrent_streams = 1
    provider.name = "Spotify"
    provider.instance_id = INSTANCE
    holder = MagicMock(queue_id="living_room")
    holder.display_name = "Living Room"
    return ProviderStreamHeldError(provider, holder)


def _controller(default_option: QueueOption = QueueOption.REPLACE) -> Any:
    """
    Build a bare controller driving ``play_media`` on a queue "q1" that holds one item.

    The requested item is handed straight back by the stubbed media resolver, and the slot
    of every source counts as held unless a test lets it through.

    :param default_option: The configured enqueue option for a play that names none.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(
        return_value=Mock(extra_data={}, offline_by_power_control=False)
    )
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_group_and_player_lock = Mock(return_value=lock_cm)
    ctrl.mass.player_queues.refuse_held_item = Mock(side_effect=_held_error())
    ctrl.mass.providers = []
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.get_config_value = Mock(return_value=default_option.value)  # type: ignore[method-assign]
    ctrl._managed_pool = Mock()
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        side_effect=lambda media_item, *_args, **_kwargs: [ResolvedItem(media_item)]
    )
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=1)
    earlier = _track("earlier")
    ctrl._queue_data = {
        "q1": PlayerQueueData(
            queue=queue,
            items=[QueueItem.from_media_item("q1", earlier)],
            enqueued_media_items=[earlier],
        )
    }
    return ctrl


@pytest.mark.parametrize(
    ("option", "default_option", "refused"),
    [
        (QueueOption.PLAY, QueueOption.REPLACE, True),
        (QueueOption.REPLACE, QueueOption.REPLACE, True),
        (None, QueueOption.PLAY, True),
        (QueueOption.NEXT, QueueOption.REPLACE, False),
        (QueueOption.ADD, QueueOption.REPLACE, False),
        (QueueOption.REPLACE_NEXT, QueueOption.REPLACE, False),
        (None, QueueOption.ADD, False),
    ],
    ids=["play", "replace", "default-play", "next", "add", "replace-next", "default-add"],
)
@patch(GET_CURRENT_USER, Mock(return_value=LISTENER))
async def test_only_a_play_that_starts_now_is_refused_early(
    option: QueueOption | None, default_option: QueueOption, refused: bool
) -> None:
    """A play that starts right away is refused at once; one that only adds items goes on."""
    ctrl = _controller(default_option)
    track = _track("new")

    if refused:
        with pytest.raises(ProviderStreamHeldError) as exc_info:
            await ctrl.play_media("q1", track, option)
        assert exc_info.value.data == {
            "queue_id": "living_room",
            "player_name": "Living Room",
            "provider_instance": INSTANCE,
        }
        ctrl.mass.player_queues.refuse_held_item.assert_called_once_with(track, "q1", LISTENER)
    else:
        await ctrl.play_media("q1", track, option)
        ctrl.mass.player_queues.refuse_held_item.assert_not_called()
        ctrl._media_resolver._resolve_media_items.assert_awaited_once()


@patch(GET_CURRENT_USER, Mock(return_value=LISTENER))
async def test_a_refused_play_leaves_the_queue_as_it_was() -> None:
    """Nothing is fetched, loaded or forgotten for a play that is refused."""
    ctrl = _controller()
    data: PlayerQueueData = ctrl._queue_data["q1"]
    items_before = list(data.items)
    enqueued_before = list(data.enqueued_media_items)

    with pytest.raises(ProviderStreamHeldError):
        await ctrl.play_media("q1", _track("new"), QueueOption.REPLACE)

    ctrl._media_resolver._resolve_media_items.assert_not_awaited()
    ctrl.play_index.assert_not_awaited()
    assert data.items == items_before
    assert data.enqueued_media_items == enqueued_before
    assert data.userid is None


@patch(GET_CURRENT_USER, Mock(return_value=LISTENER))
async def test_only_the_first_item_of_a_batch_is_checked() -> None:
    """Playback starts from the first item, so the items behind it are not checked."""
    ctrl = _controller()
    ctrl.mass.player_queues.refuse_held_item.side_effect = None
    first, second = _track("first"), _track("second")

    await ctrl.play_media("q1", [first, second], QueueOption.REPLACE)

    ctrl.mass.player_queues.refuse_held_item.assert_called_once_with(first, "q1", LISTENER)
    assert [item.media_item for item in ctrl._queue_data["q1"].items] == [first, second]


@pytest.mark.parametrize(
    ("shuffle", "queue_shuffled"),
    [(True, False), (None, True)],
    ids=["shuffle-requested", "queue-shuffled"],
)
@patch(GET_CURRENT_USER, Mock(return_value=LISTENER))
async def test_a_shuffled_batch_is_left_to_the_stream(
    shuffle: bool | None, queue_shuffled: bool
) -> None:
    """A shuffled batch may start on any of its items, so its first item says nothing yet."""
    ctrl = _controller()
    ctrl._queue_data["q1"].queue.shuffle_enabled = queue_shuffled

    await ctrl.play_media(
        "q1", [_track("first"), _track("second")], QueueOption.REPLACE, shuffle=shuffle
    )

    ctrl.mass.player_queues.refuse_held_item.assert_not_called()
    ctrl.play_index.assert_awaited_once()


@patch(GET_CURRENT_USER, Mock(return_value=LISTENER))
async def test_a_shuffled_single_item_is_still_refused_early() -> None:
    """Whatever order its tracks are shuffled in, they all come from the one item."""
    ctrl = _controller()

    with pytest.raises(ProviderStreamHeldError):
        await ctrl.play_media("q1", _track("new"), QueueOption.REPLACE, shuffle=True)

    ctrl._media_resolver._resolve_media_items.assert_not_awaited()


@patch(GET_CURRENT_USER, Mock(return_value=LISTENER))
async def test_an_artist_is_left_to_the_stream() -> None:
    """An artist names no source of its own tracks, so its play is not refused early."""
    ctrl = _controller()
    artist = Artist(
        item_id="a",
        provider=INSTANCE,
        name="A",
        provider_mappings={
            ProviderMapping(item_id="a", provider_domain="spotify", provider_instance=INSTANCE)
        },
    )
    ctrl._media_resolver.resolve_artist_tracks = AsyncMock(return_value=None)
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        return_value=[ResolvedItem(_track("by-a"))]
    )

    await ctrl.play_media("q1", artist, QueueOption.REPLACE)

    ctrl.mass.player_queues.refuse_held_item.assert_not_called()
    ctrl._media_resolver._resolve_media_items.assert_awaited_once()
