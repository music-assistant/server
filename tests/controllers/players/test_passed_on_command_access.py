"""
Tests for the access a player command needs when it is passed on to the owner of the playback.

Player "a" is synced to player "b", or a dynamic or static member of group player "g"; that
owner plays its queue or a line-in. A playback command on "a" is passed on to the owner, so it
needs access to the owner as well. Taking "a" out of its group needs access to "a" only. The
commands are reached through the registered API handlers with the real players controller;
the queue controller is reduced to its permission check, the devices record what they are told.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import (
    PlaybackState,
    PlayerFeature,
    PlayerType,
    RepeatMode,
    SleepTimerMode,
)
from music_assistant_models.errors import InsufficientPermissions
from music_assistant_models.player import PlayerSource
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.models.player import Player, PlayerMedia
from tests.common import MockPlayer, MockProvider, as_user, grouping_player_controller

KINDS = ["synced", "dynamic", "static"]
OWNER = {"synced": "b", "dynamic": "g", "static": "g"}
OWNER_NAME = {"b": "Player B", "g": "Group G"}
SOURCES = ["queue", "line_in"]
# the callers that may use the owner: none at all, an unlimited user, a user of both players
ALLOWED_USERS = ["no-user", "unlimited", "member-and-owner"]
SLEEP_TIMER = 1234.0
MEDIA = PlayerMedia(uri="http://media.test/stream.mp3", title="Stream")
# command -> (API command, its arguments besides the player id)
COMMANDS: dict[str, tuple[str, dict[str, Any]]] = {
    "play": ("players/cmd/play", {}),
    "pause": ("players/cmd/pause", {}),
    "play_pause": ("players/cmd/play_pause", {}),
    "stop": ("players/cmd/stop", {}),
    "next": ("players/cmd/next", {}),
    "previous": ("players/cmd/previous", {}),
    "seek": ("players/cmd/seek", {"position": 30}),
    "shuffle": ("players/cmd/shuffle", {"shuffle_enabled": True}),
    "repeat": ("players/cmd/repeat", {"repeat_mode": RepeatMode.ALL}),
    "resume": ("players/cmd/resume", {}),
    # a source the player does not have, so the supplied media is played
    "resume-with-media": ("players/cmd/resume", {"source": "other", "media": MEDIA}),
    "sleep_timer-get": ("players/sleep_timer/get", {}),
    "sleep_timer-set": ("players/sleep_timer/set", {"seconds": 600}),
    "sleep_timer-clear": ("players/sleep_timer/clear", {}),
}
RELEASE_COMMANDS: dict[str, tuple[str, dict[str, Any]]] = {
    "ungroup": ("players/cmd/ungroup", {}),
    "power-off": ("players/cmd/power", {"powered": False}),
    "select_source": ("players/cmd/select_source", {"source": "aux"}),
}
DEVICE_METHODS = [
    "play",
    "pause",
    "stop",
    "seek",
    "next_track",
    "previous_track",
    "set_shuffle",
    "set_repeat",
    "play_media",
    "select_source",
    "power",
]
QUEUE_METHODS = ["stop", "pause", "resume", "next", "previous", "seek", "set_shuffle", "set_repeat"]


@contextlib.asynccontextmanager
async def _no_wait(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
    """Stand in for waiting on a player update: the mocked server signals no events."""
    yield


def _caller(user: str, kind: str) -> contextlib.AbstractContextManager[None]:
    """Run the command as the given caller; "member-only" may only use a."""
    if user == "no-user":
        return contextlib.nullcontext()
    return as_user(
        {"unlimited": [], "member-and-owner": ["a", OWNER[kind]], "member-only": ["a"]}[user]
    )


class _Setup:
    """The players of one test, with what the queue controller and devices were told."""

    def __init__(self, kind: str, source: str) -> None:
        """
        Build players a and b and group player g, with a captured by its owner.

        :param kind: How a is captured: synced (to b), dynamic or static (member of g).
        :param source: What the owner plays: its queue, or a line-in of its own.
        """
        self.calls: list[tuple[Any, ...]] = []
        players = grouping_player_controller("a", "b")
        self.players = players
        self.mass = cast("MagicMock", players.mass)
        group = MockPlayer(
            MockProvider("group", instance_id="group", mass=self.mass),
            "g",
            "Group G",
            player_type=PlayerType.GROUP,
        )
        group._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        players._players["g"] = group
        group.set_initialized()
        owner = players._players[OWNER[kind]]
        if kind == "synced":
            owner._attr_group_members = ["b", "a"]
        else:
            group._attr_group_members = ["a", "b"]
            group._attr_static_group_members = ["a", "b"] if kind == "static" else []
        for player in players._players.values():
            player._attr_supported_features |= {
                PlayerFeature.PAUSE,
                PlayerFeature.SEEK,
                PlayerFeature.NEXT_PREVIOUS,
                PlayerFeature.SELECT_SOURCE,
                PlayerFeature.POWER,
            }
            player._attr_source_list = [
                PlayerSource(
                    id=source_id,
                    name=source_id,
                    passive=False,
                    can_play_pause=True,
                    can_seek=True,
                    can_next_previous=True,
                    can_shuffle=True,
                    can_repeat=True,
                )
                for source_id in ("line_in", "aux")
            ]
            for method in DEVICE_METHODS:
                setattr(player, method, AsyncMock(side_effect=self._recorder(player, method)))
        owner._attr_playback_state = PlaybackState.PAUSED
        owner._attr_active_source = OWNER[kind] if source == "queue" else "line_in"
        owner.set_sleep_timer_mode(SleepTimerMode.DURATION)
        owner.set_sleep_timer_expires_at(SLEEP_TIMER)
        self.mass.player_queues = self._queue_controller(OWNER[kind] if source == "queue" else None)
        players.get_player_audio_source = Mock(return_value=None)  # type: ignore[method-assign]
        players.wait_for_player_update = _no_wait  # type: ignore[method-assign]
        players._handle_set_members = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda parent, *args: self.calls.append(
                ("members", parent.player_id, *args)
            )
        )
        # the owner first, so its members derive their state from its final one
        for _ in range(2):
            for player in sorted(players._players.values(), key=lambda p: p.player_id != "g"):
                player._cache.clear()
                player.update_state(force_update=True, signal_event=False)
        member = players._players["a"].state
        assert (member.synced_to or member.active_group) == OWNER[kind]

    async def run(self, command: str, player_id: str, commands: dict[str, Any] = COMMANDS) -> Any:
        """Run the given command through its registered handler, naming the given player."""
        api_command, kwargs = commands[command]
        handler = self.mass.command_handlers[api_command].target
        result = handler(player_id=player_id, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def outcome(self) -> tuple[Any, ...]:
        """Return everything a command may have changed or reached."""
        return (
            self.calls,
            {
                pid: p.sleep_timer_expires_at != SLEEP_TIMER
                for pid, p in self.players._players.items()
            },
            self.mass.call_later.call_count,
            {
                pid: p.state.synced_to or p.state.active_group
                for pid, p in self.players._players.items()
            },
        )

    def _recorder(self, player: Player, method: str) -> Any:
        """Return a side effect that records a device call; a stop or power reports its state."""

        def _record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((player.player_id, method, *args, *kwargs.values()))
            if method == "stop":
                player._attr_playback_state = PlaybackState.IDLE
            elif method == "power":
                player._attr_powered = args[0]
            player.update_state(force_update=True, signal_event=False)

        return _record

    def _queue_controller(self, queue_id: str | None) -> MagicMock:
        """Return a queue controller that serves the given queue and checks access like it."""
        real = PlayerQueuesController.__new__(PlayerQueuesController)
        real.mass = self.mass
        queues = MagicMock()
        queue = (
            PlayerQueue(
                queue_id=queue_id, active=True, display_name=queue_id, available=True, items=1
            )
            if queue_id
            else None
        )
        queues.get = Mock(side_effect=lambda qid: queue if queue and qid == queue_id else None)
        queues.has_lost_paused_stream = Mock(return_value=False)
        queues.set_sleep_timer_target = Mock(return_value=None)
        # the stop a group's power off runs for its queue, without a permission check
        queues._handle_stop = AsyncMock(
            side_effect=lambda qid: self.calls.append(("queue", "_handle_stop", qid))
        )
        for method in QUEUE_METHODS:

            async def _call(qid: str, *args: Any, _method: str = method) -> None:
                real._check_player_permission(qid)
                self.calls.append(("queue", _method, qid, *args))

            setattr(queues, method, AsyncMock(side_effect=_call))
        return queues


async def _reference(kind: str, source: str, command: str, commands: dict[str, Any]) -> Any:
    """Return what the given command does when a caller without a user names the owner itself."""
    setup = _Setup(kind, source)
    await setup.run(command, OWNER[kind], commands)
    return setup.outcome()


@pytest.mark.parametrize("command", list(COMMANDS))
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_user_of_the_member_only_is_refused(kind: str, source: str, command: str) -> None:
    """A command passed on to the owner needs access to the owner; nothing is changed."""
    setup = _Setup(kind, source)
    before = setup.outcome()

    with (
        _caller("member-only", kind),
        pytest.raises(
            InsufficientPermissions,
            match=f"limited does not have access to player {OWNER_NAME[OWNER[kind]]}$",
        ),
    ):
        await setup.run(command, "a")

    assert setup.outcome() == before
    assert setup.calls == []


@pytest.mark.parametrize("command", list(COMMANDS))
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_user_of_the_owner_only_is_refused_on_the_member(
    kind: str, source: str, command: str
) -> None:
    """A command needs access to the player it names, also when it is passed on."""
    setup = _Setup(kind, source)
    before = setup.outcome()

    with (
        as_user([OWNER[kind]]),
        pytest.raises(
            InsufficientPermissions, match="limited does not have access to player Player A"
        ),
    ):
        await setup.run(command, "a")

    assert setup.outcome() == before
    assert setup.calls == []


@pytest.mark.parametrize("user", ALLOWED_USERS)
@pytest.mark.parametrize("command", list(COMMANDS))
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("kind", KINDS)
async def test_a_user_of_the_owner_reaches_the_owner(
    kind: str, source: str, command: str, user: str
) -> None:
    """A caller that may use the owner gets the same as naming the owner itself."""
    setup = _Setup(kind, source)

    with _caller(user, kind):
        result = await setup.run(command, "a")

    assert setup.outcome() == await _reference(kind, source, command, COMMANDS)
    if command == "sleep_timer-get":
        assert result == SLEEP_TIMER
    else:
        # the command reached the owner (its device, its queue or its sleep timer)
        assert setup.outcome() != _Setup(kind, source).outcome()
        assert all(call[0] != "a" for call in setup.calls)


@pytest.mark.parametrize("user", ["member-only", *ALLOWED_USERS])
@pytest.mark.parametrize("command", list(RELEASE_COMMANDS))
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("kind", KINDS)
async def test_taking_the_member_out_of_its_group_needs_access_to_the_member_only(
    kind: str, source: str, command: str, user: str
) -> None:
    """Ungroup, power off and select source on the member act the same for every caller."""
    setup = _Setup(kind, source)
    reference = _Setup(kind, source)
    await reference.run(command, "a", RELEASE_COMMANDS)

    with _caller(user, kind):
        await setup.run(command, "a", RELEASE_COMMANDS)

    assert setup.outcome() == reference.outcome()
    assert setup.outcome() != _Setup(kind, source).outcome()
