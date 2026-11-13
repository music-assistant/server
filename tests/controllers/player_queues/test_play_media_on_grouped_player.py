"""
Tests for a play request naming the queue of a player that plays along with another player.

Player "a" is synced to player "b", or a dynamic or static member of group player "g". A play
request on a's own queue that adds to what it plays goes to the queue of that owner; new content
follows a's play_media_overrides_group setting: on, a leaves the group and plays it on its own
queue; off, the owner's queue plays it. The real queue and players controllers handle the request;
only the media lookup, the member change and what a device plays are stubbed. A request that
ends up on the owner's queue needs access to the owner as well.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlayerFeature, PlayerType, QueueOption
from music_assistant_models.errors import InsufficientPermissions, MediaNotFoundError
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItemType,
    Playlist,
    ProviderMapping,
    Radio,
    Track,
)
from music_assistant_models.player_control import PlayerControl
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_PLAY_MEDIA_OVERRIDES_GROUP, CONF_POWER_CONTROL
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import PlayerMedia
from tests.common import MockPlayer, MockProvider, as_user, grouping_player_controller

ADDING_OPTIONS = [QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT]
STARTING_OPTIONS = [QueueOption.PLAY, QueueOption.REPLACE]
KINDS = ["synced", "dynamic", "static"]
OWNER = {"synced": "b", "dynamic": "g", "static": "g"}
# the callers that may use the owner: none at all, an unlimited user, a user of both players
USERS = ["no-user", "unlimited", "member-and-owner"]
RELEASE = {
    "synced": "set_members(b,-['a'])",
    "dynamic": "set_members(g,-['a'])",
    "static": "stop(g)",
}


def _caller(user: str, kind: str) -> contextlib.AbstractContextManager[None]:
    """Run the request as the given caller, see USERS; "member-only" may only use a."""
    if user == "no-user":
        return contextlib.nullcontext()
    return as_user(
        {"unlimited": [], "member-and-owner": ["a", OWNER[kind]], "member-only": ["a"]}[user]
    )


class _Setup:
    """The controllers of one test, with what they recorded."""

    def __init__(
        self,
        kind: str | None,
        overrides_group: bool,
        track_default: str,
        radio_default: str = QueueOption.REPLACE.value,
    ) -> None:
        """
        Build players a and b (and group g), each with an empty queue.

        :param kind: How a is captured: synced (to b), dynamic or static (member of g), or
            None for no capture at all.
        :param overrides_group: a's play_media_overrides_group setting.
        :param track_default: The enqueue option a track uses when a request names none.
        :param radio_default: The enqueue option a radio station uses when a request names none;
            any other media type uses REPLACE.
        """
        self.played: list[tuple[str, str | None]] = []
        self.released: list[str] = []
        players = grouping_player_controller("a", "b")
        self.players = players
        mass = cast("MagicMock", players.mass)
        mass.config.get_raw_player_config_value = MagicMock(
            side_effect=lambda player_id, key, default=None: (
                overrides_group
                if key == CONF_PLAY_MEDIA_OVERRIDES_GROUP and player_id == "a"
                else default
            )
        )
        group = MockPlayer(
            MockProvider("group", instance_id="group", mass=mass),
            "g",
            "Group G",
            player_type=PlayerType.GROUP,
        )
        group._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        players._players["g"] = group
        group.set_initialized()
        if kind == "synced":
            players._players["b"]._attr_group_members = ["b", "a"]
            players._players["b"]._attr_active_source = "b"
        elif kind is not None:
            group._attr_powered = True
            group._attr_group_members = ["a", "b"]
            group._attr_static_group_members = ["a", "b"] if kind == "static" else []
            group._attr_active_source = "g"
        self.queues = self._queue_controller(mass, track_default, radio_default)
        mass.player_queues = self.queues
        self._stub_player_side(players)
        # the owner first, so its members derive their state from its final one
        for _ in range(2):
            for player in sorted(players._players.values(), key=lambda p: p.player_id != "g"):
                player._cache.clear()
                player.update_state(force_update=True, signal_event=False)
        if kind is not None:
            member = players._players["a"].state
            assert (member.synced_to or member.active_group) == OWNER[kind]

    def items(self, queue_id: str) -> list[str]:
        """Return the item ids in the given queue."""
        return [
            item.media_item.item_id
            for item in self.queues._queue_data[queue_id].items
            if item.media_item is not None
        ]

    def _queue_controller(self, mass: MagicMock, track_default: str, radio_default: str) -> Any:
        """Build the queue controller over the players, its play_index starting the player."""
        ctrl: Any = PlayerQueuesController.__new__(PlayerQueuesController)
        ctrl.logger = Mock()
        ctrl.mass = mass
        ctrl._queue_data = {
            queue_id: PlayerQueueData(
                queue=PlayerQueue(
                    queue_id=queue_id,
                    active=True,
                    display_name=queue_id.upper(),
                    available=True,
                    items=0,
                )
            )
            for queue_id in ("a", "b", "g")
        }
        ctrl.signal_update = Mock()
        ctrl.on_player_update = Mock()
        ctrl.refuse_held_item = Mock()
        ctrl.get_next_item = Mock(return_value=None)
        defaults = {
            "default_enqueue_option_track": track_default,
            "default_enqueue_option_live_sources": radio_default,
        }
        ctrl.get_config_value = Mock(
            side_effect=lambda key, *_args, **_kwargs: defaults.get(key, QueueOption.REPLACE.value)
        )
        ctrl._managed_pool = Mock()
        ctrl._smart_shuffle = Mock()
        ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
        ctrl._media_resolver = Mock()
        ctrl._media_resolver._resolve_media_items = AsyncMock(
            side_effect=lambda media_item, *_args, **_kwargs: [ResolvedItem(media_item)]
        )
        mass.music.get_item_by_uri = AsyncMock(side_effect=_lookup)

        def _check_playable(item: MediaItemType, _user: Any) -> None:
            if item.item_id == "forbidden":
                raise MediaNotFoundError(f"{item.name} is not available on any music source")

        mass.music.check_item_playable_for_user = Mock(side_effect=_check_playable)

        async def _play_index(queue_id: str, *_args: Any, **_kwargs: Any) -> None:
            # what play_index does: check the caller's access, then the queue's player plays it
            ctrl._check_player_permission(queue_id)
            await self.players.play_media(
                queue_id, PlayerMedia(uri=f"queue://{queue_id}", source_id=queue_id)
            )

        ctrl.play_index = _play_index
        return ctrl

    def _stub_player_side(self, players: PlayerController) -> None:
        """Record the member changes and what is played instead of reaching any device."""

        async def _played(player_id: str, media: PlayerMedia) -> None:
            self.played.append((player_id, media.source_id))

        async def _set_members(
            parent: Any, _to_add: Any = None, player_ids_to_remove: Any = None
        ) -> None:
            self.released.append(f"set_members({parent.player_id},-{player_ids_to_remove})")

        async def _stop(player_id: str) -> None:
            self.released.append(f"stop({player_id})")

        @contextlib.asynccontextmanager
        async def _no_wait(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
            yield

        players._handle_play_media = AsyncMock(side_effect=_played)  # type: ignore[method-assign]
        players._handle_set_members = AsyncMock(side_effect=_set_members)  # type: ignore[method-assign]
        players._handle_cmd_stop = AsyncMock(side_effect=_stop)  # type: ignore[method-assign]
        players.wait_for_player_update = _no_wait  # type: ignore[method-assign]


def _track(item_id: str = "t1") -> Track:
    return Track(
        item_id=item_id,
        provider="test",
        name=f"Track {item_id}",
        duration=180,
        artists=UniqueList(
            [ItemMapping(item_id="x", provider="test", name="X", media_type=MediaType.ARTIST)]
        ),
        provider_mappings=_mappings(item_id),
    )


def _lookup(uri: str) -> MediaItemType:
    """Return the media item a uri names; the "missing" track is not found."""
    if uri == "test://track/missing":
        raise MediaNotFoundError("Track missing not found")
    if uri == "test://radio/r":
        return Radio(item_id="r", provider="test", name="Station", provider_mappings=_mappings("r"))
    if uri == "radio_playlist://playlist/test://track/t1":
        return Playlist(
            item_id="mix",
            provider="radio_playlist",
            name="Radio Mix",
            is_dynamic=True,
            provider_mappings=_mappings("mix"),
        )
    raise AssertionError(f"unexpected lookup of {uri}")


def _mappings(item_id: str) -> set[ProviderMapping]:
    return {ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")}


@pytest.mark.parametrize("user", USERS)
@pytest.mark.parametrize("overrides_group", [True, False])
@pytest.mark.parametrize("option", ADDING_OPTIONS)
@pytest.mark.parametrize("kind", KINDS)
async def test_adding_goes_to_the_queue_the_player_plays(
    kind: str, option: QueueOption, overrides_group: bool, user: str
) -> None:
    """Media added on a captured player lands in its owner's queue, the player stays grouped."""
    setup = _Setup(kind, overrides_group, QueueOption.PLAY.value)

    with _caller(user, kind):
        await setup.queues.play_media("a", _track(), option)

    assert setup.items(OWNER[kind]) == ["t1"]
    assert setup.items("a") == []
    assert setup.played == []
    assert setup.released == []


@pytest.mark.parametrize("user", [*USERS, "member-only"])
@pytest.mark.parametrize("option", STARTING_OPTIONS)
@pytest.mark.parametrize("kind", KINDS)
async def test_new_content_plays_on_the_player_alone_with_the_setting_on(
    kind: str, option: QueueOption, user: str
) -> None:
    """With the setting on the player leaves its group and plays its own queue."""
    setup = _Setup(kind, True, QueueOption.PLAY.value)

    with _caller(user, kind):
        await setup.queues.play_media("a", _track(), option)

    assert setup.items("a") == ["t1"]
    assert setup.items(OWNER[kind]) == []
    assert setup.played == [("a", "a")]
    assert setup.released == [RELEASE[kind]]


@pytest.mark.parametrize("user", USERS)
@pytest.mark.parametrize("option", STARTING_OPTIONS)
@pytest.mark.parametrize("kind", KINDS)
async def test_new_content_plays_on_the_owner_with_the_setting_off(
    kind: str, option: QueueOption, user: str
) -> None:
    """With the setting off the owner's queue gets the media and the owner plays it."""
    setup = _Setup(kind, False, QueueOption.PLAY.value)

    with _caller(user, kind):
        await setup.queues.play_media("a", _track(), option)

    assert setup.items(OWNER[kind]) == ["t1"]
    assert setup.items("a") == []
    assert setup.played == [(OWNER[kind], OWNER[kind])]
    assert setup.released == []


@pytest.mark.parametrize(
    ("option", "overrides_group"),
    [
        *((option, True) for option in ADDING_OPTIONS),
        *((option, False) for option in ADDING_OPTIONS),
    ]
    + [(option, False) for option in STARTING_OPTIONS],
)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_request_for_the_owner_queue_needs_access_to_the_owner(
    kind: str, option: QueueOption, overrides_group: bool
) -> None:
    """A user of the member only is refused before any queue or player is touched."""
    setup = _Setup(kind, overrides_group, QueueOption.PLAY.value)

    with (
        _caller("member-only", kind),
        pytest.raises(
            InsufficientPermissions, match=f"limited does not have access to player {OWNER[kind]}"
        ),
    ):
        await setup.queues.play_media("a", _track(), option)

    assert setup.items(OWNER[kind]) == []
    assert setup.items("a") == []
    assert setup.played == []
    assert setup.released == []


@pytest.mark.parametrize("kind", KINDS)
async def test_a_request_without_option_that_adds_goes_to_the_owner(kind: str) -> None:
    """A track defaulting to ADD is added to the owner's queue, nothing starts playing."""
    setup = _Setup(kind, True, QueueOption.ADD.value)

    await setup.queues.play_media("a", _track())

    assert setup.items(OWNER[kind]) == ["t1"]
    assert setup.items("a") == []
    assert setup.played == []
    assert setup.released == []


@pytest.mark.parametrize("kind", KINDS)
async def test_a_request_without_option_that_plays_follows_the_setting(kind: str) -> None:
    """A track defaulting to PLAY makes the player leave its group with the setting on."""
    setup = _Setup(kind, True, QueueOption.PLAY.value)

    await setup.queues.play_media("a", _track())

    assert setup.items("a") == ["t1"]
    assert setup.items(OWNER[kind]) == []
    assert setup.played == [("a", "a")]
    assert setup.released == [RELEASE[kind]]


@pytest.mark.parametrize("user", USERS)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_request_without_option_that_plays_goes_to_the_owner_with_the_setting_off(
    kind: str, user: str
) -> None:
    """A track defaulting to PLAY is played by the owner with the setting off."""
    setup = _Setup(kind, False, QueueOption.PLAY.value)

    with _caller(user, kind):
        await setup.queues.play_media("a", _track())

    assert setup.items(OWNER[kind]) == ["t1"]
    assert setup.items("a") == []
    assert setup.played == [(OWNER[kind], OWNER[kind])]
    assert setup.released == []


@pytest.mark.parametrize("kind", KINDS)
async def test_a_request_without_option_that_plays_needs_the_owner_with_the_setting_off(
    kind: str,
) -> None:
    """A track defaulting to PLAY needs access to the owner with the setting off."""
    setup = _Setup(kind, False, QueueOption.PLAY.value)

    with (
        _caller("member-only", kind),
        pytest.raises(
            InsufficientPermissions, match=f"limited does not have access to player {OWNER[kind]}"
        ),
    ):
        await setup.queues.play_media("a", _track())

    assert setup.items(OWNER[kind]) == []
    assert setup.items("a") == []
    assert setup.played == []
    assert setup.released == []


async def test_a_request_without_option_that_adds_leaves_a_switched_off_owner_off() -> None:
    """A track defaulting to ADD is added to a sync leader switched off at its power control."""
    setup = _Setup("synced", True, QueueOption.ADD.value)
    mass = cast("MagicMock", setup.players.mass)
    raw_config = mass.config.get_raw_player_config_value.side_effect
    mass.config.get_raw_player_config_value.side_effect = lambda player_id, key, default=None: (
        "plug"
        if key == CONF_POWER_CONTROL and player_id == "b"
        else raw_config(player_id, key, default)
    )
    setup.players._controls["plug"] = PlayerControl(
        id="plug", provider="test", name="Plug", supports_power=True, power_state=False
    )
    leader = setup.players._players["b"]
    leader._attr_available = False
    leader._cache.clear()
    leader.update_state(force_update=True, signal_event=False)
    assert leader.offline_by_power_control
    power_on = AsyncMock()
    setup.players._power_on_switched_off_player = power_on  # type: ignore[method-assign]

    await setup.queues.play_media("a", _track())

    power_on.assert_not_awaited()
    assert setup.items("b") == ["t1"]
    assert setup.played == []
    assert setup.released == []


@pytest.mark.parametrize("kind", KINDS)
async def test_a_radio_mode_request_follows_the_playlist_default(kind: str) -> None:
    """
    A radio mode request plays a radio playlist, so the playlist default decides.

    Tracks default to ADD here and playlists to REPLACE, so with the setting on the player
    leaves its group and plays the radio playlist's tracks on its own queue.
    """
    setup = _Setup(kind, True, QueueOption.ADD.value)
    setup.queues._managed_pool.fill = AsyncMock(return_value=[_track("p1"), _track("p2")])

    await setup.queues.play_media("a", _track(), radio_mode=True)

    assert setup.items("a") == ["p1", "p2"]
    assert setup.items(OWNER[kind]) == []
    assert setup.played == [("a", "a")]
    assert setup.released == [RELEASE[kind]]


# the first entry of these batches is skipped by the loader: a track that is not found, or one
# the user's music sources do not reach; the radio station behind it decides the option
SKIPPED_FIRST: dict[str, MediaItemType | str] = {
    "missing": "test://track/missing",
    "forbidden": _track("forbidden"),
}


@pytest.mark.parametrize("user", [*USERS, "member-only"])
@pytest.mark.parametrize("first", list(SKIPPED_FIRST))
@pytest.mark.parametrize("kind", KINDS)
async def test_the_first_played_entry_decides_a_default_that_plays(
    kind: str, first: str, user: str
) -> None:
    """
    A batch whose first entry is skipped plays by the default of the entry that plays.

    Tracks default to ADD and radio to REPLACE, so with the setting on the player leaves its
    group and plays the radio station alone, which needs access to the player only.
    """
    setup = _Setup(kind, True, QueueOption.ADD.value, QueueOption.REPLACE.value)

    with _caller(user, kind):
        await setup.queues.play_media("a", [SKIPPED_FIRST[first], "test://radio/r"])

    assert setup.items("a") == ["r"]
    assert setup.items(OWNER[kind]) == []
    assert setup.played == [("a", "a")]
    assert setup.released == [RELEASE[kind]]


@pytest.mark.parametrize("user", USERS)
@pytest.mark.parametrize("first", list(SKIPPED_FIRST))
@pytest.mark.parametrize("kind", KINDS)
async def test_the_first_played_entry_decides_a_default_that_adds(
    kind: str, first: str, user: str
) -> None:
    """
    A batch whose first entry is skipped adds by the default of the entry that plays.

    Tracks default to PLAY and radio to ADD, so the radio station is added to the owner's
    queue and the player stays in its group.
    """
    setup = _Setup(kind, True, QueueOption.PLAY.value, QueueOption.ADD.value)

    with _caller(user, kind):
        await setup.queues.play_media("a", [SKIPPED_FIRST[first], "test://radio/r"])

    assert setup.items(OWNER[kind]) == ["r"]
    assert setup.items("a") == []
    assert setup.played == []
    assert setup.released == []


@pytest.mark.parametrize("first", list(SKIPPED_FIRST))
@pytest.mark.parametrize("kind", KINDS)
async def test_the_first_played_entry_decides_the_access_a_default_needs(
    kind: str, first: str
) -> None:
    """A batch that adds to the owner's queue by the default of its played entry needs the owner."""
    setup = _Setup(kind, True, QueueOption.PLAY.value, QueueOption.ADD.value)

    with (
        _caller("member-only", kind),
        pytest.raises(
            InsufficientPermissions, match=f"limited does not have access to player {OWNER[kind]}"
        ),
    ):
        await setup.queues.play_media("a", [SKIPPED_FIRST[first], "test://radio/r"])

    assert setup.items(OWNER[kind]) == []
    assert setup.items("a") == []
    assert setup.played == []
    assert setup.released == []


@pytest.mark.parametrize("kind", KINDS)
async def test_a_request_of_which_nothing_plays_stays_on_the_named_queue(kind: str) -> None:
    """A batch whose entries are all skipped fails on the player's own queue, not on access."""
    setup = _Setup(kind, True, QueueOption.ADD.value)

    with _caller("member-only", kind), pytest.raises(MediaNotFoundError):
        await setup.queues.play_media("a", list(SKIPPED_FIRST.values()))

    assert setup.items(OWNER[kind]) == []
    assert setup.items("a") == []
    assert setup.played == []
    assert setup.released == []


@pytest.mark.parametrize("overrides_group", [True, False])
@pytest.mark.parametrize("option", [*ADDING_OPTIONS, *STARTING_OPTIONS])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_request_naming_the_owner_queue_is_unchanged(
    kind: str, option: QueueOption, overrides_group: bool
) -> None:
    """A request on the owner's own queue is handled there, the member stays grouped."""
    setup = _Setup(kind, overrides_group, QueueOption.PLAY.value)
    owner = OWNER[kind]

    await setup.queues.play_media(owner, _track(), option)

    assert setup.items(owner) == ["t1"]
    assert setup.played == ([(owner, owner)] if option in STARTING_OPTIONS else [])
    assert setup.released == []


@pytest.mark.parametrize("option", [*ADDING_OPTIONS, *STARTING_OPTIONS])
async def test_a_player_that_is_not_captured_is_untouched(option: QueueOption) -> None:
    """A player playing on its own handles the request on its own queue."""
    setup = _Setup(None, False, QueueOption.PLAY.value)

    await setup.queues.play_media("a", _track(), option)

    assert setup.items("a") == ["t1"]
    assert setup.played == ([("a", "a")] if option in STARTING_OPTIONS else [])
    assert setup.released == []
