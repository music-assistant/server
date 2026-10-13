"""
Tests for the copy an audiobook played on its own is pinned to.

Playing an audiobook by the uri of one provider copy swaps in the library item, which carries
every copy. The queue item keeps the copy the play request named, so the audiobook plays and
resumes from the account it was started from.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.media_items import (
    Audiobook,
    ItemMapping,
    MediaItemType,
    ProviderMapping,
    Track,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem, QueueItemOrigin

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import MediaResolver
from music_assistant.controllers.player_queues.state import PlayerQueueData

FIRST = "spotify--first"
SECOND = "spotify--second"
ITEM_ID = "book"


def _mapping(instance: str) -> ProviderMapping:
    """Build the mapping of the item on the given Spotify account."""
    return ProviderMapping(item_id=ITEM_ID, provider_domain="spotify", provider_instance=instance)


# the same audiobook on two accounts of one service, linked into one library item
LIBRARY_BOOK = Audiobook(
    item_id="7",
    provider="library",
    name="Dune",
    provider_mappings={_mapping(FIRST), _mapping(SECOND)},
)


def _loaded_provider(instance_or_domain: str) -> SimpleNamespace | None:
    """Return the loaded account for an instance id, or the first account of a service."""
    return next(
        (
            SimpleNamespace(instance_id=instance)
            for instance in (FIRST, SECOND)
            if instance_or_domain in (instance, instance.split("--", maxsplit=1)[0])
        ),
        None,
    )


def _controller(resolved: MediaItemType) -> PlayerQueuesController:
    """
    Build a bare controller whose play requests go through the real resolver.

    :param resolved: The item each uri or mapping of a play request resolves to.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = Mock()
    ctrl.mass.get_provider = Mock(side_effect=_loaded_provider)
    ctrl.mass.music.get_item_by_uri = AsyncMock(return_value=resolved)
    ctrl.mass.music.get_resume_position = AsyncMock(return_value=(False, 0))
    ctrl.mass.players.get_player = Mock(return_value=Mock(extra_data={}))
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_group_and_player_lock = Mock(return_value=lock_cm)
    ctrl.get_config_value = Mock(return_value=QueueOption.REPLACE.value)  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.store_sources = Mock()  # type: ignore[method-assign]
    ctrl._apply_shuffle = AsyncMock()  # type: ignore[method-assign]
    ctrl._enqueue_with_option = AsyncMock()  # type: ignore[method-assign]
    resolver = MediaResolver.__new__(MediaResolver)
    resolver.mass = ctrl.mass
    resolver.logger = Mock()
    ctrl._media_resolver = resolver
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl.get = Mock(return_value=queue)  # type: ignore[method-assign]
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return ctrl


async def _enqueued(ctrl: PlayerQueuesController, request: str | ItemMapping) -> QueueItem:
    """Play the request and return the single queue item it enqueued."""
    await ctrl._handle_play_media("q1", request)
    enqueue = cast("AsyncMock", ctrl._enqueue_with_option).await_args
    assert enqueue is not None
    queue_items = cast("list[QueueItem]", enqueue.args[1])
    assert len(queue_items) == 1
    return queue_items[0]


@pytest.mark.parametrize(
    ("request_", "pinned_instance"),
    [
        (f"{SECOND}://audiobook/{ITEM_ID}", SECOND),
        (
            ItemMapping(
                media_type=MediaType.AUDIOBOOK, item_id=ITEM_ID, provider=SECOND, name="Dune"
            ),
            SECOND,
        ),
        # a uri of the service names no account, so the one loaded first is used
        (f"spotify://audiobook/{ITEM_ID}", FIRST),
    ],
    ids=["uri of the second account", "mapping of the second account", "uri of the service"],
)
async def test_an_audiobook_keeps_the_account_it_was_started_from(
    request_: str | ItemMapping, pinned_instance: str
) -> None:
    """The library audiobook a provider copy resolves to is pinned to that copy."""
    ctrl = _controller(LIBRARY_BOOK)

    queue_item = await _enqueued(ctrl, request_)

    assert queue_item.media_item is LIBRARY_BOOK
    assert queue_item.origin == QueueItemOrigin(provider_instance=pinned_instance, item_id=ITEM_ID)
    # the progress of that same account is where the book resumes from
    cast("AsyncMock", ctrl.mass.music.get_resume_position).assert_awaited_once_with(
        LIBRARY_BOOK, userid=None, pinned=(pinned_instance, ITEM_ID)
    )


async def test_a_library_audiobook_is_not_pinned() -> None:
    """An audiobook played from the library names no copy, so the user's own account leads."""
    ctrl = _controller(LIBRARY_BOOK)

    queue_item = await _enqueued(ctrl, "library://audiobook/7")

    assert queue_item.origin is None
    cast("AsyncMock", ctrl.mass.music.get_resume_position).assert_awaited_once_with(
        LIBRARY_BOOK, userid=None, pinned=None
    )


async def test_a_single_track_is_not_pinned() -> None:
    """A track played on its own by a provider uri plays the best copy, as before."""
    track = Track(item_id="9", provider="library", name="Song", provider_mappings={_mapping(FIRST)})

    queue_item = await _enqueued(_controller(track), f"{SECOND}://track/{ITEM_ID}")

    assert queue_item.origin is None
