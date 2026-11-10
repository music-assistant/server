"""
Tests for ending a group session through a static member, with the real group providers.

A user who may use a static member of a group player may end the session of that group
(ungroup or power off of the member, or a play request that takes the member out of its
group), with the same effect as for any other caller, but nothing of that release runs with
more permissions than the caller has.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, QueueOption
from music_assistant_models.media_items import ItemMapping, ProviderMapping, Track
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    ATTR_FAKE_POWER,
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_GROUP_MEMBERS,
    CONF_PLAY_MEDIA_OVERRIDES_GROUP,
    CONF_POWER_CONTROL,
)
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.media_resolver import ResolvedItem
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.providers.sync_group.player import SyncGroupPlayer
from music_assistant.providers.universal_group.player import UniversalGroupPlayer
from tests.common import (
    MockPlayer,
    MockProvider,
    as_user,
    use_api_command_registry,
    use_real_create_task,
)

USERS = [
    pytest.param(lambda: as_user(["a"]), id="limited-to-member"),
    pytest.param(lambda: as_user([]), id="unlimited"),
    pytest.param(nullcontext, id="no-user"),
]
COMMANDS = ["ungroup", "power"]


class _Device(MockPlayer):
    """A member that records its device calls in ``mass.calls``."""

    stop_reports_idle = True

    @property
    def _calls(self) -> list[tuple[Any, ...]]:
        return cast("list[tuple[Any, ...]]", cast("MagicMock", self.mass).calls)

    async def stop(self) -> None:
        """Record the stop; a device may acknowledge it before it reports idle."""
        self._calls.append((self.player_id, "stop"))
        if self.stop_reports_idle:
            self._attr_playback_state = PlaybackState.IDLE
            self.update_state(force_update=True)

    async def power(self, powered: bool) -> None:
        """Record the power command and report the new state."""
        self._calls.append((self.player_id, "power", powered))
        self._attr_powered = powered
        if not powered:
            self._attr_playback_state = PlaybackState.IDLE
        self.update_state(force_update=True)

    async def play_media(self, media: PlayerMedia) -> None:
        """Record what the device is asked to play."""
        self._calls.append((self.player_id, "play_media", media.source_id))

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Record the member change and report it on all players."""
        self._calls.append(
            (self.player_id, "members", sorted(player_ids_to_add or []), player_ids_to_remove)
        )
        await super().set_members(player_ids_to_add, player_ids_to_remove)
        for player in self.mass.players._players.values():
            player.update_state(force_update=True)


def _config(name: str, values: dict[str, Any]) -> MagicMock:
    """Return a player config with the given values."""
    config = MagicMock(default_name=name, enabled=True)
    config.name = None
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    return config


@pytest.fixture
def mass() -> Iterator[MagicMock]:
    """Return a mocked server with real tasks and timers, cancelling what is left afterwards."""
    mass = MagicMock()
    mass.closing = False
    mass.calls = []
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.config.get_player_dsp_config.return_value.enabled = False
    mass.player_queues.get.return_value = None
    mass.streams.register_dynamic_route.return_value = lambda: None
    mass.cache.set = AsyncMock()
    mass._tracked_timers = {}
    use_real_create_task(mass)
    mass.call_later.side_effect = lambda *a, **kw: MusicAssistant.call_later(mass, *a, **kw)
    mass.cancel_timer.side_effect = lambda *a, **kw: MusicAssistant.cancel_timer(mass, *a, **kw)
    mass.cancel_task.side_effect = lambda *a, **kw: MusicAssistant.cancel_task(mass, *a, **kw)
    yield mass
    for handle in mass._tracked_timers.values():
        handle.cancel()
    for task in mass._tracked_tasks.values():
        task.cancel()


async def _build(
    mass: MagicMock, group_cls: type[Player], power_control: bool, playing: bool = True
) -> tuple[PlayerController, Player]:
    """
    Register a group player "g" with static members "a" and "b", and a player "c".

    The members have a native power control; "b" leads a sync group.

    :param mass: The mocked server.
    :param group_cls: The group player class.
    :param power_control: Whether the group player has a (fake) power control.
    :param playing: Whether the group (and so its members) is playing.
    """
    mass.loop = asyncio.get_running_loop()
    controller = PlayerController(mass)
    mass.players = controller
    values: dict[str, dict[str, Any]] = {
        "a": {CONF_POWER_CONTROL: "native", CONF_PLAY_MEDIA_OVERRIDES_GROUP: True},
        "b": {CONF_POWER_CONTROL: "native"},
        "c": {CONF_POWER_CONTROL: PLAYER_CONTROL_NONE},
        "g": {
            CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE if power_control else PLAYER_CONTROL_NONE,
            CONF_GROUP_MEMBERS: ["a", "b"],
            CONF_DYNAMIC_GROUP_MEMBERS: False,
        },
    }
    mass.config.get_raw_player_config_value.side_effect = lambda pid, key, default=None: values[
        pid
    ].get(key, default)
    for pid in ("a", "b", "c"):
        name = f"Player {pid.upper()}"
        device = _Device(MockProvider("test", mass=mass), pid, name)
        device._config = _config(name, values[pid])
        device._attr_supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.SET_MEMBERS}
        if pid != "c":
            device._attr_supported_features.add(PlayerFeature.POWER)
            if playing:
                device._attr_playback_state = PlaybackState.PLAYING
        device._attr_can_group_with = {"a", "b", "c"} - {pid}
        controller._players[pid] = device
        device.set_initialized()
    mass.config.get_base_player_config.return_value = _config("Group G", values["g"])
    domain = "sync_group" if group_cls is SyncGroupPlayer else "universal_group"
    group = group_cls(MockProvider(domain, instance_id=domain, mass=mass), "g")  # type: ignore[arg-type]
    controller._players["g"] = group
    await group.on_config_updated()
    group._attr_powered = True if power_control else None
    if power_control:
        group.extra_data[ATTR_FAKE_POWER] = True
    if isinstance(group, SyncGroupPlayer):
        leader = controller._players["b"]
        leader._attr_group_members = ["b", "a"]
        group.sync_leader = leader
        group._update_attributes()
    else:
        assert isinstance(group, UniversalGroupPlayer)
        if playing:
            group.stream = MagicMock(done=False, stop=AsyncMock())
        group._set_attributes()
    group.set_initialized()
    if playing:
        group._attr_playback_state = PlaybackState.PLAYING
    for player in (*controller._players.values(), controller._players["a"]):
        player._cache.clear()
        player.update_state(force_update=True, signal_event=False)
    use_api_command_registry(mass, controller)
    assert controller._players["a"].state.active_group == "g"
    return controller, group


async def _send(controller: PlayerController, command: str, player_id: str = "a") -> None:
    """Send the given players/cmd API command for a member (power: switch it off)."""
    target = cast(
        "Callable[..., Awaitable[None]]",
        controller.mass.command_handlers[f"players/cmd/{command}"].target,
    )
    if command == "power":
        await target(player_id, False)
    else:
        await target(player_id)


async def _settle() -> None:
    """Run what the command scheduled without a delay."""
    for _ in range(10):
        await asyncio.sleep(0)


def _end_state(controller: PlayerController) -> dict[str, tuple[Any, ...]]:
    """Return the power, playback and group state of the group player and its members."""
    players = [controller._players[pid] for pid in ("a", "b", "g")]
    for player in players:
        player.update_state(force_update=True, signal_event=False)
    return {
        player.player_id: (
            player.state.powered,
            player.state.playback_state,
            player.state.active_group,
            player.state.synced_to,
            sorted(player.state.group_members),
        )
        for player in players
    }


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("user", USERS)
async def test_universal_group_static_member_ends_the_session(
    mass: MagicMock, user: Callable[[], AbstractContextManager[object]], command: str
) -> None:
    """A powered universal group switches its members off, also for a user limited to one."""
    controller, group = await _build(mass, UniversalGroupPlayer, power_control=True)

    with user():
        await _send(controller, command)
    await _settle()

    # a powered off member is switched off by its group and by its own command
    member_off = [("a", "power", False)] if command == "power" else []
    assert sorted(mass.calls) == sorted(
        [("a", "stop"), ("b", "stop"), ("a", "power", False), ("b", "power", False), *member_off]
    )
    assert group.powered is False
    off: tuple[Any, ...] = (False, PlaybackState.IDLE, None, None, [])
    assert _end_state(controller) == {
        "a": off,
        "b": off,
        "g": (False, PlaybackState.IDLE, None, None, ["a", "b"]),
    }


# the group iterates its members in hash order, so each member is commanded once to have the
# member the user may not use switched off first under every hash seed
@pytest.mark.parametrize("member", ["a", "b"])
@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    "user",
    [
        pytest.param(lambda member: as_user([member]), id="limited-to-member"),
        pytest.param(lambda _member: as_user([]), id="unlimited"),
        pytest.param(lambda _member: nullcontext(), id="no-user"),
    ],
)
async def test_idle_universal_group_static_member_ends_the_session(
    mass: MagicMock,
    user: Callable[[str], AbstractContextManager[object]],
    command: str,
    member: str,
) -> None:
    """An idle powered universal group switches its members off, also for a user limited to one."""
    controller, group = await _build(mass, UniversalGroupPlayer, power_control=True, playing=False)

    with user(member):
        await _send(controller, command, member)
    await _settle()

    member_off = [(member, "power", False)] if command == "power" else []
    assert sorted(mass.calls) == sorted([("a", "power", False), ("b", "power", False), *member_off])
    assert group.powered is False
    off: tuple[Any, ...] = (False, PlaybackState.IDLE, None, None, [])
    assert _end_state(controller) == {
        "a": off,
        "b": off,
        "g": (False, PlaybackState.IDLE, None, None, ["a", "b"]),
    }


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("user", USERS)
async def test_universal_group_without_power_control_static_member_stops_the_session(
    mass: MagicMock, user: Callable[[], AbstractContextManager[object]], command: str
) -> None:
    """A universal group without power control is stopped; only a powered off member is off."""
    controller, _group = await _build(mass, UniversalGroupPlayer, power_control=False)

    with user():
        await _send(controller, command)
    await _settle()

    member_off = [("a", "power", False)] if command == "power" else []
    assert sorted(mass.calls) == sorted([("a", "stop"), ("b", "stop"), *member_off])
    assert _end_state(controller) == {
        "a": (command != "power", PlaybackState.IDLE, None, None, []),
        "b": (True, PlaybackState.IDLE, None, None, []),
        "g": (None, PlaybackState.IDLE, None, None, ["a", "b"]),
    }


@pytest.mark.parametrize("command", COMMANDS)
async def test_static_member_release_restarts_nothing_beyond_the_user(
    mass: MagicMock, command: str
) -> None:
    """
    A user limited to a static member can not have the group's queue restarted by the release.

    Releasing the member changes the shape of the sync group, which schedules a DSP restart of
    the group's queue while its leader still reports playing; for this user that restart is
    refused on the group player.
    """
    controller, _group = await _build(mass, SyncGroupPlayer, power_control=False)
    queues = PlayerQueuesController(mass)
    mass.player_queues = queues
    mass.streams.base_url = "http://example.invalid"
    item = QueueItem(queue_id="g", queue_item_id="i", name="Test", duration=100)
    queue = PlayerQueue(
        queue_id="g",
        active=True,
        display_name="G",
        available=True,
        items=1,
        state=PlaybackState.PLAYING,
        current_item=item,
    )
    queues._queue_data["g"] = PlayerQueueData(queue=queue, items=[item], session_id="session")
    queues.signal_update = MagicMock()  # type: ignore[method-assign]
    leader = controller._players["b"]
    leader._attr_current_media = PlayerMedia(
        uri="mass:g:i", source_id="g", queue_item_id="i", queue_session_id="session"
    )
    for player in controller._players.values():
        player.stop_reports_idle = False  # type: ignore[attr-defined]
        player.update_state(force_update=True, signal_event=False)
    mass.config.get_player_dsp_config.side_effect = lambda pid: MagicMock(enabled=pid == "b")
    restarts: list[str | None] = []
    resume_after_dsp_change = controller._resume_queue_after_dsp_change

    async def _record_restart(queue_id: str) -> None:
        user = get_current_user()
        restarts.append(user.username if user else None)
        await resume_after_dsp_change(queue_id)

    controller._resume_queue_after_dsp_change = _record_restart  # type: ignore[method-assign]
    queues._play_index = AsyncMock()  # type: ignore[method-assign]

    with as_user(["a"]):
        await _send(controller, command)
    await _settle()

    assert restarts
    assert set(restarts) == {"limited"}
    queues._play_index.assert_not_awaited()
    assert ("b", "members", [], ["a"]) in mass.calls


@pytest.mark.parametrize("option", [QueueOption.PLAY, QueueOption.REPLACE])
@pytest.mark.parametrize("power_control", [False, True])
@pytest.mark.parametrize(
    "user", [*USERS, pytest.param(lambda: as_user(["a", "g"]), id="member-and-group")]
)
async def test_play_media_on_a_static_member_ends_the_session(
    mass: MagicMock,
    user: Callable[[], AbstractContextManager[object]],
    power_control: bool,
    option: QueueOption,
) -> None:
    """
    New content on a static member that overrides its group ends the group's session.

    The member then plays the content alone from its own queue, also for a user limited to it.
    """
    controller, group = await _build(mass, SyncGroupPlayer, power_control=power_control)
    queues = PlayerQueuesController(mass)
    mass.player_queues = queues
    for queue_id in ("a", "b", "g"):
        queue = PlayerQueue(
            queue_id=queue_id, active=True, display_name=queue_id, available=True, items=0
        )
        queues._queue_data[queue_id] = PlayerQueueData(queue=queue)
    queues.signal_update = MagicMock()  # type: ignore[method-assign]
    queues._media_resolver._resolve_media_items = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda media_item, *_args, **_kwargs: [ResolvedItem(media_item)]
    )
    mass.music.check_item_playable_for_user = MagicMock()

    async def _play_index(queue_id: str, *_args: Any, **_kwargs: Any) -> None:
        # what play_index does: check the caller's access, then the queue's player plays it
        queues._check_player_permission(queue_id)
        await controller.play_media(
            queue_id, PlayerMedia(uri=f"queue://{queue_id}", source_id=queue_id)
        )

    queues.play_index = _play_index  # type: ignore[method-assign]
    track = Track(
        item_id="t1",
        provider="test",
        name="Track",
        duration=180,
        artists=UniqueList(
            [ItemMapping(item_id="x", provider="test", name="X", media_type=MediaType.ARTIST)]
        ),
        provider_mappings={
            ProviderMapping(item_id="t1", provider_domain="test", provider_instance="test")
        },
    )

    with user():
        await queues.play_media("a", track, option)
    await _settle()

    assert mass.calls == [("b", "stop"), ("b", "members", [], ["a"]), ("a", "play_media", "a")]
    assert queues._queue_data["g"].items == []
    assert [
        item.media_item.item_id for item in queues._queue_data["a"].items if item.media_item
    ] == ["t1"]
    assert group.powered is (False if power_control else None)
    assert _end_state(controller) == {
        "a": (True, PlaybackState.PLAYING, None, None, []),
        "b": (True, PlaybackState.IDLE, None, None, []),
        "g": (group.powered, PlaybackState.IDLE, None, None, ["a", "b"]),
    }
