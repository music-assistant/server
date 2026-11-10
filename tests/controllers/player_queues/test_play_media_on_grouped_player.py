"""
Tests for a play request naming the queue of a player that plays along with another player.

Player "a" is synced to player "b", or a dynamic or static member of group player "g". A play
request on a's own queue that adds to what it plays goes to the queue of that owner; new content
follows a's play_media_overrides_group setting: on, a leaves the group and plays it on its own
queue; off, the owner's queue plays it. The real queue and players controllers handle the request;
only the media lookup, the member change and what a device plays are stubbed.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlayerFeature, PlayerType, QueueOption
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_PLAY_MEDIA_OVERRIDES_GROUP
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import PlayerMedia
from tests.common import MockPlayer, MockProvider, grouping_player_controller

ADDING_OPTIONS = [QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT]
STARTING_OPTIONS = [QueueOption.PLAY, QueueOption.REPLACE]
KINDS = ["synced", "dynamic", "static"]
OWNER = {"synced": "b", "dynamic": "g", "static": "g"}


class _Setup:
    """The controllers of one test, with what they recorded."""

    def __init__(self, kind: str | None, overrides_group: bool, track_default: str) -> None:
        """
        Build players a and b (and group g), each with an empty queue.

        :param kind: How a is captured: synced (to b), dynamic or static (member of g), or
            None for no capture at all.
        :param overrides_group: a's play_media_overrides_group setting.
        :param track_default: The enqueue option a track uses when a request names none.
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
        self.queues = self._queue_controller(mass, track_default)
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

    def _queue_controller(self, mass: MagicMock, track_default: str) -> Any:
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
        ctrl.get_config_value = Mock(
            side_effect=lambda key, *_args, **_kwargs: (
                track_default
                if key == "default_enqueue_option_track"
                else QueueOption.REPLACE.value
            )
        )
        ctrl._managed_pool = Mock()
        ctrl._smart_shuffle = Mock()
        ctrl._smart_shuffle.is_enabled = Mock(return_value=False)
        ctrl._media_resolver = Mock()
        ctrl._media_resolver._resolve_media_items = AsyncMock(
            side_effect=lambda media_item, *_args, **_kwargs: [ResolvedItem(media_item)]
        )
        mass.music.check_item_playable_for_user = Mock()

        async def _play_index(queue_id: str, *_args: Any, **_kwargs: Any) -> None:
            # what play_index ends in: the queue's player plays the queue
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
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="test", provider_instance="test")
        },
    )


@pytest.mark.parametrize("overrides_group", [True, False])
@pytest.mark.parametrize("option", ADDING_OPTIONS)
@pytest.mark.parametrize("kind", KINDS)
async def test_adding_goes_to_the_queue_the_player_plays(
    kind: str, option: QueueOption, overrides_group: bool
) -> None:
    """Media added on a captured player lands in its owner's queue, the player stays grouped."""
    setup = _Setup(kind, overrides_group, QueueOption.PLAY.value)

    await setup.queues.play_media("a", _track(), option)

    assert setup.items(OWNER[kind]) == ["t1"]
    assert setup.items("a") == []
    assert setup.played == []
    assert setup.released == []


@pytest.mark.parametrize("option", STARTING_OPTIONS)
@pytest.mark.parametrize("kind", KINDS)
async def test_new_content_plays_on_the_player_alone_with_the_setting_on(
    kind: str, option: QueueOption
) -> None:
    """With the setting on the player leaves its group and plays its own queue."""
    setup = _Setup(kind, True, QueueOption.PLAY.value)

    await setup.queues.play_media("a", _track(), option)

    assert setup.items("a") == ["t1"]
    assert setup.items(OWNER[kind]) == []
    assert setup.played == [("a", "a")]
    assert setup.released == [
        {
            "synced": "set_members(b,-['a'])",
            "dynamic": "set_members(g,-['a'])",
            "static": "stop(g)",
        }[kind]
    ]


@pytest.mark.parametrize("option", STARTING_OPTIONS)
@pytest.mark.parametrize("kind", KINDS)
async def test_new_content_plays_on_the_owner_with_the_setting_off(
    kind: str, option: QueueOption
) -> None:
    """With the setting off the owner's queue gets the media and the owner plays it."""
    setup = _Setup(kind, False, QueueOption.PLAY.value)

    await setup.queues.play_media("a", _track(), option)

    assert setup.items(OWNER[kind]) == ["t1"]
    assert setup.items("a") == []
    assert setup.played == [(OWNER[kind], OWNER[kind])]
    assert setup.released == []


@pytest.mark.parametrize(
    ("track_default", "lands_in"),
    [(QueueOption.ADD.value, "g"), (QueueOption.PLAY.value, "a")],
)
async def test_a_request_without_option_follows_the_default_for_its_media(
    track_default: str, lands_in: str
) -> None:
    """The enqueue default of the media decides, like an explicit option would."""
    setup = _Setup("dynamic", True, track_default)

    await setup.queues.play_media("a", _track())

    assert setup.items(lands_in) == ["t1"]


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
