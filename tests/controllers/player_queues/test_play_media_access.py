"""
Tests that enqueuing rejects what the requesting user has no music source for.

The check runs while the requested media is resolved, so a track on a source the user may not
use is refused right there instead of failing later, per queue item, when the player asks for
its audio. Items that carry no access record of their own (a plugin's playlists) stay playable.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import (
    MediaType,
    ProviderSharing,
    ProviderType,
    QueueOption,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from tests.common import set_music_source_access

GET_CURRENT_USER = "music_assistant.controllers.player_queues.queue_loader.get_current_user"
USER_ID = "listener"
OTHER_USER_ID = "housemate"

OWN_INSTANCE = "tidal--mine"
BLOCKED_INSTANCE = "spotify--theirs"
EVERYONE_INSTANCE = "qobuz--house"
PLUGIN_INSTANCE = "smart_playlist"


def _track(item_id: str, provider: str, *mapping_instances: str) -> Track:
    """Build a playable track carrying a mapping on each of the given instances."""
    return Track(
        item_id=item_id,
        provider=provider,
        name=f"Track {item_id}",
        duration=180,
        artists=UniqueList(
            [ItemMapping(item_id="a", provider=provider, name="A", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=instance.split("--", maxsplit=1)[0],
                provider_instance=instance,
            )
            for instance in mapping_instances
        },
    )


def _controller() -> Any:
    """
    Build a bare controller driving ``play_media`` on a single queue "q1".

    The requested item is handed straight back by the stubbed media resolver, so a play that
    is not refused ends up as exactly that one item on the queue.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(return_value=Mock(extra_data={}))
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_player_lock = Mock(return_value=lock_cm)
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_next_item = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.get_config_value = Mock(return_value=QueueOption.REPLACE.value)  # type: ignore[method-assign]
    ctrl._managed_pool = Mock()
    ctrl._smart_shuffle = Mock()
    ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
    ctrl._media_resolver = Mock()
    ctrl._media_resolver._resolve_media_items = AsyncMock(
        side_effect=lambda media_item, *_args, **_kwargs: [media_item]
    )
    # the real access check, over the sources configured by each test
    music = MusicController.__new__(MusicController)
    music.mass = ctrl.mass
    ctrl.mass.music.check_item_playable_for_user = music.check_item_playable_for_user
    ctrl.mass.providers = []
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return ctrl


def _set_sources(ctrl: Any) -> None:
    """Give the server one source of the listener and one another member keeps private."""
    set_music_source_access(
        ctrl.mass,
        {
            OWN_INSTANCE: ProviderAccess(owner=USER_ID, sharing=ProviderSharing.PRIVATE),
            BLOCKED_INSTANCE: ProviderAccess(owner=OTHER_USER_ID, sharing=ProviderSharing.PRIVATE),
        },
    )


def _queued_item_ids(ctrl: Any) -> list[str]:
    """Return the item ids of the tracks currently loaded in the queue."""
    return [
        item.media_item.item_id
        for item in ctrl._queue_data["q1"].items
        if item.media_item is not None
    ]


@patch(GET_CURRENT_USER)
async def test_a_provider_item_on_a_blocked_source_is_refused(mock_get_user: Mock) -> None:
    """A track of another member's account is refused instead of enqueued."""
    mock_get_user.return_value = User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
    ctrl = _controller()
    _set_sources(ctrl)

    with pytest.raises(MediaNotFoundError) as err:
        await ctrl.play_media(
            "q1", _track("t1", BLOCKED_INSTANCE, BLOCKED_INSTANCE), QueueOption.REPLACE
        )

    assert err.value.translation_key == "media_not_available_for_user"
    assert _queued_item_ids(ctrl) == []


@patch(GET_CURRENT_USER)
async def test_a_library_item_without_an_allowed_mapping_is_refused(mock_get_user: Mock) -> None:
    """A library track that only exists on a blocked source is refused as well."""
    mock_get_user.return_value = User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
    ctrl = _controller()
    _set_sources(ctrl)

    with pytest.raises(MediaNotFoundError) as err:
        await ctrl.play_media("q1", _track("42", "library", BLOCKED_INSTANCE), QueueOption.REPLACE)

    assert err.value.translation_key == "media_not_available_for_user"
    assert _queued_item_ids(ctrl) == []


@patch(GET_CURRENT_USER)
async def test_a_library_item_mapped_on_a_plugin_instance_still_plays(
    mock_get_user: Mock,
) -> None:
    """A plugin's item carries no access record of its own, so it stays playable."""
    mock_get_user.return_value = User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
    ctrl = _controller()
    _set_sources(ctrl)
    plugin = MagicMock()
    plugin.instance_id = PLUGIN_INSTANCE
    plugin.type = ProviderType.PLUGIN
    ctrl.mass.providers = [plugin]

    await ctrl.play_media("q1", _track("7", "library", PLUGIN_INSTANCE), QueueOption.REPLACE)

    assert _queued_item_ids(ctrl) == ["7"]


@patch(GET_CURRENT_USER)
async def test_an_allowed_item_is_enqueued(mock_get_user: Mock) -> None:
    """The listener's own source plays as before."""
    mock_get_user.return_value = User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
    ctrl = _controller()
    _set_sources(ctrl)

    await ctrl.play_media("q1", _track("t2", OWN_INSTANCE, OWN_INSTANCE), QueueOption.REPLACE)

    assert _queued_item_ids(ctrl) == ["t2"]
    assert cast("PlayerQueueData", ctrl._queue_data["q1"]).userid == USER_ID


@patch(GET_CURRENT_USER)
async def test_a_blocked_item_does_not_take_the_batch_down(mock_get_user: Mock) -> None:
    """One refused track is skipped, the rest of the request still plays."""
    mock_get_user.return_value = User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
    ctrl = _controller()
    _set_sources(ctrl)

    await ctrl.play_media(
        "q1",
        [
            _track("t1", BLOCKED_INSTANCE, BLOCKED_INSTANCE),
            _track("t2", OWN_INSTANCE, OWN_INSTANCE),
        ],
        QueueOption.REPLACE,
    )

    assert _queued_item_ids(ctrl) == ["t2"]


@patch(GET_CURRENT_USER)
async def test_a_fully_refused_request_leaves_the_queue_with_its_user(
    mock_get_user: Mock,
) -> None:
    """A queue keeps playing for its listener until another member's request survives."""
    housemate = User(user_id=OTHER_USER_ID, username=OTHER_USER_ID, role=UserRole.USER)
    mock_get_user.return_value = User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
    ctrl = _controller()
    _set_sources(ctrl)
    await ctrl.play_media("q1", _track("t1", OWN_INSTANCE, OWN_INSTANCE), QueueOption.REPLACE)
    assert cast("PlayerQueueData", ctrl._queue_data["q1"]).userid == USER_ID

    # the housemate asks for the listener's own source, which they may not use
    mock_get_user.return_value = housemate
    with pytest.raises(MediaNotFoundError):
        await ctrl.play_media("q1", _track("t2", OWN_INSTANCE, OWN_INSTANCE), QueueOption.REPLACE)

    assert cast("PlayerQueueData", ctrl._queue_data["q1"]).userid == USER_ID

    # BLOCKED_INSTANCE is the housemate's own source, so this request does play
    await ctrl.play_media(
        "q1", _track("t3", BLOCKED_INSTANCE, BLOCKED_INSTANCE), QueueOption.REPLACE
    )

    assert cast("PlayerQueueData", ctrl._queue_data["q1"]).userid == OTHER_USER_ID


@patch(GET_CURRENT_USER)
async def test_anonymous_playback_reaches_the_household_sources_only(mock_get_user: Mock) -> None:
    """A queue without a user plays what is shared with everyone, and nothing private."""
    mock_get_user.return_value = None
    ctrl = _controller()
    set_music_source_access(
        ctrl.mass,
        {
            EVERYONE_INSTANCE: ProviderAccess(owner=USER_ID, sharing=ProviderSharing.EVERYONE),
            BLOCKED_INSTANCE: ProviderAccess(owner=OTHER_USER_ID, sharing=ProviderSharing.PRIVATE),
        },
    )

    await ctrl.play_media(
        "q1", _track("t3", EVERYONE_INSTANCE, EVERYONE_INSTANCE), QueueOption.REPLACE
    )
    assert _queued_item_ids(ctrl) == ["t3"]
    assert cast("PlayerQueueData", ctrl._queue_data["q1"]).userid is None

    with pytest.raises(MediaNotFoundError):
        await ctrl.play_media(
            "q1", _track("t4", BLOCKED_INSTANCE, BLOCKED_INSTANCE), QueueOption.REPLACE
        )
