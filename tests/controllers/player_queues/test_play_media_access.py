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
from music_assistant_models.media_items import ItemMapping, Playlist, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.music import MusicController
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.provider_access import playback_user_or_stand_in
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
    ctrl.mass.players.get_player = Mock(
        return_value=Mock(extra_data={}, offline_by_power_control=False)
    )
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=None)
    lock_cm.__aexit__ = AsyncMock(return_value=None)
    ctrl.mass.players.get_group_and_player_lock = Mock(return_value=lock_cm)
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
        side_effect=lambda media_item, *_args, **_kwargs: [ResolvedItem(media_item)]
    )
    # the real access check, over the sources configured by each test
    music = MusicController.__new__(MusicController)
    music.mass = ctrl.mass
    ctrl.mass.music.check_item_playable_for_user = music.check_item_playable_for_user
    ctrl.mass.providers = []
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return ctrl


def _dynamic_playlist(item_id: str, instance: str) -> Playlist:
    """Build a dynamic playlist of the given provider instance."""
    playlist = Playlist(
        item_id=item_id,
        provider=instance,
        name=f"Radio {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=instance.split("--", maxsplit=1)[0],
                provider_instance=instance,
            )
        },
    )
    playlist.is_dynamic = True
    return playlist


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


@pytest.mark.parametrize(
    ("option", "ended", "expected_userids", "expected_owner"),
    [
        (QueueOption.ADD, False, {"t1": USER_ID, "t3": OTHER_USER_ID}, USER_ID),
        (QueueOption.NEXT, False, {"t1": USER_ID, "t3": OTHER_USER_ID}, USER_ID),
        (QueueOption.REPLACE, False, {"t3": OTHER_USER_ID}, OTHER_USER_ID),
        # an ended queue continues on ADD, anything else starts it afresh
        (QueueOption.ADD, True, {"t1": USER_ID, "t3": OTHER_USER_ID}, USER_ID),
        (QueueOption.NEXT, True, {"t3": OTHER_USER_ID}, OTHER_USER_ID),
    ],
)
@patch(GET_CURRENT_USER)
async def test_each_item_plays_for_whom_enqueued_it(
    mock_get_user: Mock,
    option: QueueOption,
    ended: bool,
    expected_userids: dict[str, str],
    expected_owner: str,
) -> None:
    """Adding to someone's queue keeps it theirs; only replacing its content changes the owner."""
    mock_get_user.return_value = User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
    ctrl = _controller()
    _set_sources(ctrl)
    await ctrl.play_media("q1", _track("t1", OWN_INSTANCE, OWN_INSTANCE), QueueOption.REPLACE)
    ctrl._queue_data["q1"].queue.ended = ended

    mock_get_user.return_value = User(
        user_id=OTHER_USER_ID, username=OTHER_USER_ID, role=UserRole.USER
    )
    await ctrl.play_media("q1", _track("t3", BLOCKED_INSTANCE, BLOCKED_INSTANCE), option)

    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    assert {
        item.media_item.item_id: item.userid for item in queue_data.items if item.media_item
    } == expected_userids
    assert queue_data.userid == expected_owner


@pytest.mark.parametrize(
    ("instance", "refused"),
    [(BLOCKED_INSTANCE, True), (EVERYONE_INSTANCE, False)],
    ids=["only on the adding user's source", "on a source of both"],
)
@pytest.mark.parametrize(
    "dynamic_playlist", [False, True], ids=["track onto a dynamic queue", "dynamic playlist"]
)
@patch(GET_CURRENT_USER)
async def test_an_added_source_must_be_playable_for_the_queue_owner(
    mock_get_user: Mock, dynamic_playlist: bool, instance: str, refused: bool
) -> None:
    """The pool builds an added source's tracks for the queue's owner, who must be able to play it."""
    listener = User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
    housemate = User(user_id=OTHER_USER_ID, username=OTHER_USER_ID, role=UserRole.USER)
    ctrl = _controller()
    set_music_source_access(
        ctrl.mass,
        {
            OWN_INSTANCE: ProviderAccess(owner=USER_ID, sharing=ProviderSharing.PRIVATE),
            BLOCKED_INSTANCE: ProviderAccess(owner=OTHER_USER_ID, sharing=ProviderSharing.PRIVATE),
            EVERYONE_INSTANCE: ProviderAccess(owner=USER_ID, sharing=ProviderSharing.EVERYONE),
        },
    )
    ctrl.mass.player_queues = ctrl
    ctrl.mass.webserver.auth.get_user = AsyncMock(
        side_effect={USER_ID: listener, OTHER_USER_ID: housemate}.get
    )
    ctrl.mass.music.get_item = AsyncMock()
    rebuilt_as: list[User | None] = []
    ctrl._enter_dynamic_mode = AsyncMock(
        side_effect=lambda *_args: rebuilt_as.append(get_current_user())
    )
    mock_get_user.return_value = listener
    await ctrl.play_media("q1", _track("t1", OWN_INSTANCE, OWN_INSTANCE), QueueOption.REPLACE)
    queue_data = cast("PlayerQueueData", ctrl._queue_data["q1"])
    added: Track | Playlist
    if dynamic_playlist:
        # a dynamic playlist turns any queue into a dynamic one
        added = _dynamic_playlist("p3", instance)
    else:
        queue_data.source_items = [_dynamic_playlist("radio", OWN_INSTANCE)]
        queue_data.queue.is_dynamic = True
        added = _track("t3", instance, instance)

    mock_get_user.return_value = housemate
    if refused:
        with pytest.raises(MediaNotFoundError) as err:
            await ctrl.play_media("q1", added, QueueOption.ADD)
        assert err.value.translation_key == "media_not_available_for_user"
    else:
        await ctrl.play_media("q1", added, QueueOption.ADD)

    assert (added in queue_data.source_items) is not refused
    assert queue_data.userid == USER_ID
    # the pool is rebuilt for the owner, not for the one adding
    assert rebuilt_as == ([] if refused else [listener])


@pytest.mark.parametrize("owned", [True, False], ids=["owned queue", "anonymous queue"])
@pytest.mark.parametrize(
    "seed_of_the_adding_user", [True, False], ids=["seed only they see", "seed the owner sees"]
)
@patch(GET_CURRENT_USER)
async def test_a_radio_is_fetched_and_built_for_the_queue_owner(
    mock_get_user: Mock, owned: bool, seed_of_the_adding_user: bool
) -> None:
    """A radio on a plugin passes the source check, but rests on a seed the owner may not see."""
    listener = User(user_id=USER_ID, username=USER_ID, role=UserRole.USER)
    housemate = User(user_id=OTHER_USER_ID, username=OTHER_USER_ID, role=UserRole.USER)
    owner = listener if owned else None
    ctrl = _controller()
    set_music_source_access(
        ctrl.mass,
        {EVERYONE_INSTANCE: ProviderAccess(owner=USER_ID, sharing=ProviderSharing.EVERYONE)},
    )
    plugin = MagicMock()
    plugin.instance_id = PLUGIN_INSTANCE
    plugin.type = ProviderType.PLUGIN
    ctrl.mass.providers = [plugin]
    ctrl.mass.player_queues = ctrl
    ctrl.mass.webserver.auth.get_user = AsyncMock(return_value=listener)

    async def fetch(*_args: Any, **_kwargs: Any) -> None:
        # the server, which sees every source, and the adding user can resolve the seed
        if seed_of_the_adding_user and get_current_user() not in (None, housemate):
            raise MediaNotFoundError("Radio playlist seed not found")

    ctrl.mass.music.get_item = AsyncMock(side_effect=fetch)
    rebuilt_as: list[User | None] = []
    ctrl._enter_dynamic_mode = AsyncMock(
        side_effect=lambda *_args: rebuilt_as.append(get_current_user())
    )
    mock_get_user.return_value = owner
    await ctrl.play_media(
        "q1", _track("t1", EVERYONE_INSTANCE, EVERYONE_INSTANCE), QueueOption.REPLACE
    )

    mock_get_user.return_value = housemate
    radio = _dynamic_playlist("seeded", PLUGIN_INSTANCE)
    if seed_of_the_adding_user:
        with pytest.raises(MediaNotFoundError) as err:
            await ctrl.play_media("q1", radio, QueueOption.ADD)
        assert err.value.translation_key == "media_not_available_for_user"
    else:
        await ctrl.play_media("q1", radio, QueueOption.ADD)

    assert (radio in ctrl._queue_data["q1"].source_items) is not seed_of_the_adding_user
    # an anonymous queue is built as the stand-in its streams use, not as the server
    expected = [] if seed_of_the_adding_user else [playback_user_or_stand_in(owner)]
    assert rebuilt_as == expected


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
