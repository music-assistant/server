"""
Tests for the commands of the players controller and what it does around them.

This module tests:
- grouping commands (set members, group, ungroup) and the state forwarding between players
- the access and availability checks of the commands
- power, the external power control and the idle power off
- the sleep timer, config changes and the player locks

Every player of these tests has a native protocol with an id of its own (``proto_<player id>``),
so the controller has to tell the two kinds of id apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import logging
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ConfigEntry, PlayerConfig
from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
    SleepTimerMode,
)
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    ResourceBusyError,
    UnsupportedFeaturedException,
)
from music_assistant_models.player import PlayerMedia, PlayerSource
from music_assistant_models.player_control import PlayerControl
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    ATTR_FAKE_POWER,
    ATTR_IDLE_SINCE,
    CONF_AUTO_PLAY,
    CONF_ICON,
    CONF_IDLE_POWER_OFF,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
    CONF_OUTPUT_CODEC,
    CONF_POWER_CONTROL,
)
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players import group_commands as players_group_commands
from music_assistant.controllers.players import locks as players_locks
from music_assistant.controllers.players import playback as players_playback
from music_assistant.controllers.players import power as players_power
from music_assistant.controllers.players.constants import (
    PLAYER_LOCK_OWNER_ATTEMPTS,
    PlayerLockPurpose,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    get_current_user,
    sendspin_player_id,
)
from music_assistant.models.player import Player
from tests.common import (
    MockProtocol,
    MockProvider,
    attach_mock,
    record_lock_waits,
    use_real_create_task,
    virtual_loop_clock,
)


def _proto(player_id: str) -> str:
    """Return the id of the native protocol of a player in these tests."""
    return f"proto_{player_id}"


def _attach(
    provider: MockProvider,
    player_id: str,
    name: str | None = None,
    *,
    player_type: PlayerType = PlayerType.PLAYER,
    protocol_class: type[MockProtocol] = MockProtocol,
) -> tuple[Player, MockProtocol]:
    """
    Return a player with the given id and its native protocol, which has an id of its own.

    :param provider: The (mock) provider of the protocol; its mass holds the controller.
    :param player_id: Id of the player.
    :param name: The name the device reports.
    :param player_type: The type of player the protocol reports.
    :param protocol_class: The class of the protocol.
    """
    return attach_mock(
        provider,
        _proto(player_id),
        name,
        # a group is a virtual player
        role=ProtocolRole.VIRTUAL if player_type == PlayerType.GROUP else ProtocolRole.NATIVE,
        player_id=player_id,
        player_type=player_type,
        protocol_class=protocol_class,
    )


def _attach_audio(
    provider: MockProvider, player: Player, protocol_id: str, name: str | None = None
) -> MockProtocol:
    """
    Return an audio protocol attached to a player, next to its native protocol.

    :param provider: The (mock) provider of the audio protocol.
    :param player: The player it belongs to.
    :param protocol_id: Id of the audio protocol.
    :param name: The name the device reports through it.
    """
    _, protocol = attach_mock(provider, protocol_id, name, role=ProtocolRole.AUDIO, player=player)
    return protocol


def _controller(mock_mass: MagicMock) -> PlayerController:
    """Return a players controller that is the one of the mocked mass."""
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    return controller


def _refresh(*players: Player) -> None:
    """Recalculate the state of the given players, in order, without publishing it."""
    for player in players:
        # config-derived values are cached until the config is set again
        player._cache.clear()
        for protocol in player.attached_protocols.values():
            protocol._cache.clear()
        player.update_state(signal_event=False, force_update=True)


def _player_config_stub(
    values: dict[str, object] | None = None,
    *,
    min_volume: int = 0,
    max_volume: int = 100,
) -> Callable[..., object]:
    """
    Build a ``get_player_value`` side effect.

    :param values: Extra config keys to answer, e.g. ``{CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE}``.
    :param min_volume: Value returned for the ``min_volume`` key.
    :param max_volume: Value returned for the ``max_volume`` key.
    """
    config: dict[str, object] = {
        CONF_MIN_VOLUME: min_volume,
        CONF_MAX_VOLUME: max_volume,
        **(values or {}),
    }

    def _conf(_player_id: str, key: str, default: object = None) -> object:
        if key in config:
            return config[key]
        return default

    return _conf


def _set_config_value(player: Player, key: str, value: object) -> None:
    """
    Let the config of a player answer a value for one key.

    :param player: The player.
    :param key: The config key.
    :param value: The value to answer.
    """
    config_get_value = player.config.get_value
    player.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda conf_key, *args, **kwargs: (
            value if conf_key == key else config_get_value(conf_key, *args, **kwargs)
        )
    )


def _stub_queue(queue_id: str) -> PlayerQueue:
    """Build an empty PlayerQueue for the given id."""
    return PlayerQueue(
        queue_id=queue_id,
        active=False,
        display_name=queue_id,
        available=True,
        items=0,
    )


@pytest.fixture
def running_background_tasks(mock_mass: MagicMock) -> Iterator[None]:
    """
    Really run the tasks that ``mass.create_task`` is handed, and raise what they raise.

    The real implementation only logs the exception of a background task, which would
    leave a test that dispatches its work through the TaskManager passing regardless.
    """
    tasks: list[asyncio.Task[Any]] = []
    use_real_create_task(mock_mass)
    real_create_task = mock_mass.create_task

    def _create_task(target: Any, *args: Any, **kwargs: Any) -> Any:
        task = real_create_task(target, *args, **kwargs)
        if isinstance(task, asyncio.Task):
            tasks.append(task)
        return task

    mock_mass.create_task = MagicMock(side_effect=_create_task)
    yield
    errors = [
        err
        for task in tasks
        if task.done() and not task.cancelled() and (err := task.exception()) is not None
    ]
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("background tasks failed", errors)


@pytest.fixture
def mock_mass() -> Iterator[MagicMock]:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    # kept so teardown still sees its calls when a test swaps mass.create_task
    create_task = mass.create_task = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_player_value = MagicMock(side_effect=_player_config_stub())
    # Return "GLOBAL" for log level config (standard default)
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.set = MagicMock()
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    yield mass
    # a mocked create_task never runs what it is handed, so close what no test started
    for scheduled in create_task.call_args_list:
        target = scheduled.args[0] if scheduled.args else None
        if inspect.iscoroutine(target) and inspect.getcoroutinestate(target) == "CORO_CREATED":
            target.close()


@pytest.fixture
def controller(mock_mass: MagicMock) -> PlayerController:
    """Create a PlayerController instance."""
    return _controller(mock_mass)


@contextlib.contextmanager
def _restricted_user(
    visible_player_ids: list[str], own_player_id: str | None = None
) -> Iterator[None]:
    """
    Run the wrapped block as a non-admin user that may only see the given players.

    :param visible_player_ids: The player ids the user is allowed to see.
    :param own_player_id: The client player the user connected on, if any.
    """
    # the contextvar is copied into the task an API command runs in, so it stays
    # live for everything that command reaches, internal bookkeeping included
    token = current_user.set(
        User(
            user_id="user_1",
            username="restricted",
            role=UserRole.USER,
            player_filter=visible_player_ids,
        )
    )
    player_token = sendspin_player_id.set(own_player_id)
    try:
        yield
    finally:
        sendspin_player_id.reset(player_token)
        current_user.reset(token)


def _group_with_member(
    mock_mass: MagicMock,
    *,
    initialize_group: bool = True,
    member_type: PlayerType = PlayerType.PLAYER,
) -> tuple[PlayerController, Player, Player]:
    """
    Build a controller holding one group player with a single member.

    :param mock_mass: The mocked MusicAssistant instance to attach the controller to.
    :param initialize_group: Whether the group player is marked as fully registered.
    :param member_type: The type to register the member as.

    :return: The controller, the group player and its member.
    """
    controller = _controller(mock_mass)
    provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

    group_player, group_protocol = _attach(provider, "group", "Group", player_type=PlayerType.GROUP)
    member, _ = _attach(provider, "member", "Member", player_type=member_type)

    # a group names its members by player id
    group_protocol._attr_group_members = ["member"]
    if not initialize_group:
        group_player.initialized.clear()
    _refresh(group_player, member)
    return controller, group_player, member


async def _yield_to_loop() -> None:
    """Give other pending tasks a chance to run up to their next suspension point."""
    for _ in range(5):
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def _skip_player_update_wait(
    *_args: object,
    **_kwargs: object,
) -> AsyncIterator[None]:
    """Skip provider-driven state propagation in command-routing tests."""
    yield


@contextlib.asynccontextmanager
async def _lock_held_elsewhere(controller: PlayerController, player_id: str) -> AsyncIterator[None]:
    """Hold the player's playback lock from another task for the duration of the block."""
    held = asyncio.Event()
    release = asyncio.Event()

    async def _hold() -> None:
        async with controller.get_player_lock(player_id, PlayerLockPurpose.PLAYBACK):
            held.set()
            await release.wait()

    holder = asyncio.create_task(_hold())
    await held.wait()
    try:
        yield
    finally:
        release.set()
        await holder


def _set_play_media_override(mock_mass: MagicMock, value: bool) -> None:
    """
    Configure get_player_value to return ``value`` for the play-media override key.

    Other keys keep the existing defaults from the shared fixture. Use this in
    tests for ``play_media`` override behavior so the legacy/new branch is
    selected deterministically.
    """
    original = mock_mass.config.get_player_value.side_effect

    def _side_effect(player_id: str, key: str, default: object = None) -> object:
        if key == "play_media_overrides_group":
            return value
        if callable(original):
            return original(player_id, key, default)
        return default

    mock_mass.config.get_player_value = MagicMock(side_effect=_side_effect)


def _scheduled_queue_stops(mock_mass: MagicMock) -> list[Any]:
    """Return the ``call_later`` calls that scheduled an external power off queue stop."""
    return [
        scheduled
        for scheduled in mock_mass.call_later.call_args_list
        if str(scheduled.kwargs.get("task_id", "")).startswith("external_power_off_stop_")
    ]


class TestSetMembersValidation:
    """Test cmd_set_members validation logic."""

    def test_set_members_requires_feature(self, mock_mass: MagicMock) -> None:
        """Test that set_members requires SET_MEMBERS feature."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader, _ = _attach(provider, "leader", "Leader")
        # Note: NOT adding SET_MEMBERS feature
        member, _ = _attach(provider, "member", "Member")
        _refresh(leader, member)

        # Should raise exception because leader doesn't support SET_MEMBERS
        with pytest.raises(UnsupportedFeaturedException):
            asyncio.run(controller.cmd_set_members("leader", player_ids_to_add=["member"]))

    def test_cannot_group_incompatible_players(self, mock_mass: MagicMock) -> None:
        """Test that a player the leader can not group with is not grouped."""
        controller = _controller(mock_mass)
        provider = MockProvider("provider_a", instance_id="provider_a", mass=mock_mass)

        player_a, protocol_a = _attach(provider, "player_a", "Player A")
        protocol_a._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        # only player_c: a model of the same provider that player_b is not
        protocol_a._attr_can_group_with = {_proto("player_c")}
        protocol_a.set_members = AsyncMock()  # type: ignore[method-assign]

        player_b, _ = _attach(provider, "player_b", "Player B")
        player_c, _ = _attach(provider, "player_c", "Player C")
        _refresh(player_a, player_b, player_c)
        assert player_a.state.can_group_with == {"player_c"}

        # player_b is skipped as incompatible, so the provider is never asked to group it
        asyncio.run(controller.cmd_set_members("player_a", player_ids_to_add=["player_b"]))
        protocol_a.set_members.assert_not_awaited()
        assert player_a.state.group_members == []


class TestCacheInvalidationAfterGrouping:
    """Test that the players that can group are recalculated after a grouping change."""

    async def test_member_change_refreshes_the_other_players_of_the_provider(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A changed member list of a leader lets every player of its provider recalculate.

        Regression test for: Stale can_group_with cache after grouping changes.
        """
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader, leader_protocol = _attach(provider, "leader", "Leader")
        leader_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_protocol._attr_can_group_with = {"test"}

        member, _ = _attach(provider, "member", "Member")

        other, other_protocol = _attach(provider, "other", "Other")
        other_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        other_protocol._attr_can_group_with = {"test"}
        mock_mass.get_provider.return_value = provider
        _refresh(leader, member, other)
        assert "member" in other.state.can_group_with
        controller.trigger_player_update = MagicMock()  # type: ignore[method-assign]

        await controller._handle_set_members(leader, player_ids_to_add=["member"])
        # the provider reports the new group
        leader.update_state()

        assert leader.state.group_members == ["leader", "member"]
        refreshed = {c.args[0] for c in controller.trigger_player_update.call_args_list}
        assert {"member", "other"} <= refreshed


def _make_takeover_players(
    mock_mass: MagicMock, token: int
) -> tuple[PlayerController, Player, MockProtocol, MockProtocol]:
    """Create a player rendering through an audio protocol that owns group content."""
    controller = _controller(mock_mass)
    provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
    audio_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)
    parent, native = _attach(provider, "parent", "Parent")
    audio = _attach_audio(audio_provider, parent, "audio_parent", "Parent")
    audio._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
    audio.on_group_content_takeover = AsyncMock(return_value=token)  # type: ignore[method-assign]
    audio.on_group_content_takeover_finished = AsyncMock()  # type: ignore[method-assign]
    parent.set_active_output_protocol("audio_parent")
    _refresh(parent)
    return controller, parent, native, audio


def _protocol_child(audio_provider: MockProvider, audio: MockProtocol) -> Player:
    """
    Attach player "protocol_child", reachable only over the audio protocol of a takeover parent.

    :param audio_provider: The provider of the parent's audio protocol.
    :param audio: The parent's audio protocol, which offers the child.
    """
    child, _ = attach_mock(
        audio_provider,
        "protocol_child",
        "Protocol child",
        role=ProtocolRole.AUDIO,
        player_id="protocol_child",
    )
    audio._attr_can_group_with = {"protocol_child"}
    return child


class TestContentTakeover:
    """Test content ownership of an audio protocol around group membership changes."""

    async def test_native_unsupported_keeps_protocol_takeover_owner(
        self, mock_mass: MagicMock
    ) -> None:
        """An unsupported native leg must not bypass an active protocol transaction."""
        controller, parent, native, audio = _make_takeover_players(mock_mass, 7)
        provider = cast("MockProvider", native.provider)
        native._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        native.set_members = AsyncMock()  # type: ignore[method-assign]
        native_child, native_child_protocol = _attach(provider, "native_child", "Native child")
        native._attr_group_members = [native.protocol_id, native_child_protocol.protocol_id]
        protocol_child = _protocol_child(cast("MockProvider", audio.provider), audio)
        _refresh(parent, native_child, protocol_child)

        # the protocol child joins over the audio protocol, the native child leaves natively
        takeover = await controller._handle_set_members(
            parent, ["protocol_child"], ["native_child"], new_content=True
        )

        assert takeover == (audio, 7)
        native.set_members.assert_not_awaited()
        cast("AsyncMock", audio.on_group_content_takeover_finished).assert_not_awaited()

    async def test_protocol_takeover_failure_finishes_captured_owner(
        self, mock_mass: MagicMock
    ) -> None:
        """A protocol forwarding failure releases the owner that started the token."""
        controller, parent, _native, audio = _make_takeover_players(mock_mass, 11)
        audio.on_group_content_takeover_aborted = AsyncMock()  # type: ignore[method-assign]
        protocol_child = _protocol_child(cast("MockProvider", audio.provider), audio)
        _refresh(parent, protocol_child)
        # the forward fails at the protocol that carries the group
        audio.set_members = AsyncMock(side_effect=RuntimeError("forward failed"))  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="forward failed"):
            await controller._handle_set_members(parent, ["protocol_child"], new_content=True)

        audio.on_group_content_takeover_aborted.assert_awaited_once_with(11)
        cast("AsyncMock", audio.on_group_content_takeover_finished).assert_not_awaited()


class TestNativeSetMembersGuard:
    """Test the SET_MEMBERS feature guard on native set_members forwarding."""

    async def test_native_set_members_skipped_without_feature(self, mock_mass: MagicMock) -> None:
        """
        Test that set_members is not called on a player without SET_MEMBERS support.

        Regression test for: NotImplementedError raised from
        _cleanup_player_memberships when removing a member from a native player
        whose group membership is managed externally (e.g. a Google Cast group,
        which never advertises SET_MEMBERS).
        """
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        # a native group-like player WITHOUT SET_MEMBERS in supported_features,
        # whose set_members behaves like the PlayerProtocol base class (raises)
        parent, parent_protocol = _attach(provider, "cast_group", "Cast Group")
        parent_protocol._attr_group_members = [_proto("cast_group"), _proto("member")]
        parent_protocol.set_members = AsyncMock(  # type: ignore[method-assign]
            side_effect=NotImplementedError(
                "set_members needs to be implemented when PlayerFeature.SET_MEMBERS is set"
            )
        )
        member, _ = _attach(provider, "member", "Member")
        _refresh(parent, member)
        assert parent.state.group_members == ["cast_group", "member"]

        # must complete without raising NotImplementedError
        await controller._handle_set_members(parent, player_ids_to_remove=["member"])
        parent_protocol.set_members.assert_not_called()


class TestGroupUngroup:
    """Test group and ungroup commands."""

    async def test_group_command(self, mock_mass: MagicMock) -> None:
        """Test the group command (cmd_group)."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader, leader_protocol = _attach(provider, "leader", "Leader")
        leader_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        # a protocol names the devices it can group with by their protocol id
        leader_protocol._attr_can_group_with = {_proto("member")}

        member, member_protocol = _attach(provider, "member", "Member")
        # Make sure member is already powered on to skip power handling
        member_protocol._attr_powered = True

        # Update state after modifying attributes and registering with controller
        _refresh(leader, member)

        # Track if set_members was called
        set_members_called = False
        original_set_members = leader_protocol.set_members

        async def mock_set_members(
            player_ids_to_add: list[str] | None = None,
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            nonlocal set_members_called
            set_members_called = True
            # Call the original to update group_members
            await original_set_members(player_ids_to_add, player_ids_to_remove)

        leader_protocol.set_members = mock_set_members  # type: ignore[method-assign]

        # Mock power handling to skip power control (focus is on grouping logic)
        async def mock_handle_cmd_power(
            player_id: str, powered: bool, skip_auto_play: bool = False
        ) -> None:
            pass

        controller._handle_cmd_power = mock_handle_cmd_power  # type: ignore[method-assign]

        # Execute group command
        await controller.cmd_group("member", "leader")

        # Verify set_members was called
        assert set_members_called
        # Verify member was added to leader's group, which the leader reports by protocol id
        assert _proto("member") in leader_protocol._attr_group_members
        leader.update_state(signal_event=False)
        assert "member" in leader.state.group_members

    async def test_remove_accepted_on_native_synced_child(self, mock_mass: MagicMock) -> None:
        """
        A removal addressed at the native sync leader survives a stale member list.

        The child is natively synced to the leader while the leader's state
        snapshot does not (yet) list it; the removal must still reach the
        set_members of the leader's protocol instead of being dropped.
        """
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        airplay = MockProvider("airplay", instance_id="airplay", mass=mock_mass)

        leader, _ = _attach(provider, "leader", "Leader")
        leader_audio = _attach_audio(airplay, leader, "ap_leader", "Leader")
        leader_audio._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        child, _ = _attach(provider, "child", "Child")
        _attach_audio(airplay, child, "ap_child", "Child")
        leader.set_active_output_protocol("ap_leader")
        _refresh(leader, child)

        # the membership exists live but the leader's state snapshot is stale,
        # and the child's published synced_to points at the leader player
        leader_audio._attr_group_members = ["ap_leader", "ap_child"]
        _refresh(child)
        assert "child" not in leader.state.group_members
        assert child.state.synced_to == "leader"

        leader_audio.set_members = AsyncMock()  # type: ignore[method-assign]
        await controller._handle_set_members(leader, player_ids_to_remove=["child"])

        leader_audio.set_members.assert_awaited_once_with(
            player_ids_to_add=None, player_ids_to_remove=["ap_child"]
        )


class TestPlayerAvailability:
    """Test player availability checks in grouping."""

    def test_unavailable_player_rejected(self, mock_mass: MagicMock) -> None:
        """Test that an unavailable player is left out when grouping."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader, leader_protocol = _attach(provider, "leader", "Leader")
        leader_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_protocol._attr_can_group_with = {_proto("member")}
        leader_protocol.set_members = AsyncMock()  # type: ignore[method-assign]

        member, member_protocol = _attach(provider, "member", "Member")
        member_protocol._attr_available = False  # Mark as unavailable
        _refresh(leader, member)

        asyncio.run(controller.cmd_set_members("leader", player_ids_to_add=["member"]))

        leader_protocol.set_members.assert_not_awaited()
        assert "member" not in leader.state.group_members


class TestPlayerCommandPermission:
    """The command decorator enforces the player filter but exempts the caller's own client player."""

    async def _stop(
        self,
        mock_mass: MagicMock,
        player_id: str,
        *,
        private: bool = False,
        own_player_id: str | None = None,
    ) -> AsyncMock:
        """Send a stop command as a restricted user and return the isolated handler mock."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        if private:
            # a browser session: its player has the id of its protocol, the client id
            player, protocol = attach_mock(provider, player_id, "Client", player_id=player_id)
            protocol._attr_session_only = True
        else:
            player, protocol = _attach(provider, player_id, "Client")
        protocol._attr_private = private
        _refresh(player)
        controller.get_active_queue = MagicMock(return_value=None)  # type: ignore[method-assign]
        handler = AsyncMock()
        controller._handle_cmd_stop = handler  # type: ignore[method-assign]
        with _restricted_user(["kitchen"], own_player_id=own_player_id):
            await controller.cmd_stop(player_id)
        return handler

    async def test_command_on_the_own_client_player_is_permitted(
        self, mock_mass: MagicMock
    ) -> None:
        """A private client player the user connected on accepts commands even when filtered out."""
        handler = await self._stop(
            mock_mass, "browser_session", private=True, own_player_id="browser_session"
        )
        handler.assert_awaited_once_with("browser_session")

    async def test_a_shared_speaker_claimed_as_the_client_player_is_refused(
        self, mock_mass: MagicMock
    ) -> None:
        """Announcing a shared speaker's id as the client id does not grant command access."""
        with pytest.raises(InsufficientPermissions):
            await self._stop(mock_mass, "living_room", private=False, own_player_id="living_room")


class TestPlayerCommandAvailability:
    """The command decorator refuses commands for players that cannot take them."""

    async def test_command_for_unknown_player_raises(self, mock_mass: MagicMock) -> None:
        """A command addressed at an unknown player id raises PlayerUnavailableError."""
        controller = self._setup(mock_mass)

        with pytest.raises(PlayerUnavailableError):
            await controller.cmd_stop("missing")

    async def test_command_for_unavailable_player_raises(
        self, mock_mass: MagicMock, provider: MockProvider
    ) -> None:
        """A command addressed at an unavailable player raises PlayerUnavailableError."""
        controller = self._setup(mock_mass)
        player, protocol = _attach(provider, "p1", "Player 1")
        protocol._attr_available = False
        _refresh(player)
        controller._handle_cmd_volume_set = AsyncMock()  # type: ignore[method-assign]

        with pytest.raises(PlayerUnavailableError):
            await controller.cmd_volume_set("p1", 50)

        controller._handle_cmd_volume_set.assert_not_awaited()

    async def test_command_by_protocol_id_runs_on_the_available_player(
        self, mock_mass: MagicMock, provider: MockProvider
    ) -> None:
        """A protocol id resolves to its player, which is available on another protocol."""
        controller = self._setup(mock_mass)
        player, _ = _attach(provider, "parent", "Parent")
        audio = _attach_audio(provider, player, "audio_parent", "Parent")
        audio._attr_available = False
        _refresh(player)

        await controller.cmd_stop("audio_parent")

        cast("AsyncMock", controller._handle_cmd_stop).assert_awaited_once_with("parent")

    async def test_command_by_protocol_id_of_an_unavailable_player_raises(
        self, mock_mass: MagicMock, provider: MockProvider
    ) -> None:
        """A protocol id whose player is unavailable raises PlayerUnavailableError."""
        controller = self._setup(mock_mass)
        player, native = _attach(provider, "parent", "Parent")
        native._attr_available = False
        audio = _attach_audio(provider, player, "audio_parent", "Parent")
        audio._attr_available = False
        _refresh(player)
        controller._handle_cmd_volume_set = AsyncMock()  # type: ignore[method-assign]

        with pytest.raises(PlayerUnavailableError):
            await controller.cmd_volume_set("audio_parent", 50)

        controller._handle_cmd_volume_set.assert_not_awaited()

    def _setup(self, mock_mass: MagicMock) -> PlayerController:
        """Return the controller of the provider fixture, with stubbed stop handling."""
        controller = _controller(mock_mass)
        controller.get_active_queue = MagicMock(return_value=None)  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        return controller


class TestStateForwarding:
    """Test forwarding of player state changes to related players."""

    def test_sync_leader_updates_are_forwarded_to_sync_children(self, mock_mass: MagicMock) -> None:
        """A regular sync leader must notify children via the sync-parent callback."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader, leader_protocol = _attach(provider, "leader", "Leader")
        child, child_protocol = _attach(provider, "child", "Child")

        leader_protocol._attr_group_members = [_proto("leader"), _proto("child")]
        _refresh(leader, child)

        with (
            patch.object(child_protocol, "on_sync_parent_updated") as on_sync_parent_updated,
            patch.object(child_protocol, "on_group_updated") as on_group_updated,
        ):
            changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            controller._forward_state_update(leader, changed_values)

        on_sync_parent_updated.assert_called_once_with(leader, changed_values)
        on_group_updated.assert_not_called()

    def test_group_updates_are_forwarded_to_children_via_group_callback(
        self, mock_mass: MagicMock
    ) -> None:
        """A group player must continue to notify children via the group callback."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        group_player, group_protocol = _attach(
            provider, "group", "Group", player_type=PlayerType.GROUP
        )
        child, child_protocol = _attach(provider, "child", "Child")

        group_protocol._attr_group_members = ["group", "child"]
        _refresh(group_player, child)

        with (
            patch.object(child_protocol, "on_group_updated") as on_group_updated,
            patch.object(child_protocol, "on_sync_parent_updated") as on_sync_parent_updated,
        ):
            changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            controller._forward_state_update(group_player, changed_values)

        on_group_updated.assert_called_once_with(
            group_player,
            changed_values,
        )
        on_sync_parent_updated.assert_not_called()

    def test_sync_leader_updates_also_reach_its_group_player(self, mock_mass: MagicMock) -> None:
        """A sync leader must notify its group player as well as its own sync children."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        group_player, group_protocol = _attach(
            provider, "group", "Group", player_type=PlayerType.GROUP
        )
        leader, leader_protocol = _attach(provider, "leader", "Leader")
        follower, follower_protocol = _attach(provider, "follower", "Follower")

        group_protocol._attr_group_members = ["leader", "follower"]
        leader_protocol._attr_group_members = [_proto("leader"), _proto("follower")]
        _refresh(group_player, leader, follower)

        with (
            patch.object(group_protocol, "on_group_member_updated") as on_group_member_updated,
            patch.object(follower_protocol, "on_sync_parent_updated") as on_sync_parent_updated,
        ):
            changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            controller._forward_state_update(leader, changed_values)

        on_group_member_updated.assert_called_once_with(leader, changed_values)
        on_sync_parent_updated.assert_called_once_with(leader, changed_values)

    def test_group_player_is_notified_under_restricted_user_context(
        self, mock_mass: MagicMock
    ) -> None:
        """The fan-out must not be narrowed by the user that triggered the update."""
        controller, group_player, member = _group_with_member(mock_mass)
        group_protocol = group_player.own_protocol
        assert group_protocol is not None

        with _restricted_user(["member"]):
            assert group_player not in controller.all_players()
            with patch.object(group_protocol, "on_group_member_updated") as on_group_member_updated:
                changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
                controller._forward_state_update(member, changed_values)

        on_group_member_updated.assert_called_once_with(member, changed_values)

    def test_unavailable_group_player_is_still_notified(self, mock_mass: MagicMock) -> None:
        """A group player mirrors its members, so it must update while unavailable too."""
        controller, group_player, member = _group_with_member(mock_mass)
        group_protocol = cast("MockProtocol", group_player.own_protocol)
        group_protocol._attr_available = False
        _refresh(group_player)
        assert group_player.state.available is False

        with patch.object(group_protocol, "on_group_member_updated") as on_group_member_updated:
            changed_values = {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            controller._forward_state_update(member, changed_values)

        on_group_member_updated.assert_called_once_with(member, changed_values)

    def test_disabled_group_player_is_not_notified(self, mock_mass: MagicMock) -> None:
        """A disabled player takes no part, unlike one that is merely unavailable."""
        controller, group_player, member = _group_with_member(mock_mass)
        group_player._config.enabled = False
        _refresh(group_player)
        assert group_player.state.enabled is False
        group_protocol = group_player.own_protocol
        assert group_protocol is not None

        with patch.object(group_protocol, "on_group_member_updated") as on_group_member_updated:
            controller._forward_state_update(
                member, {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            )

        on_group_member_updated.assert_not_called()

    def test_uninitialized_group_player_is_not_notified(self, mock_mass: MagicMock) -> None:
        """A player the controller is still attaching is not fully set up yet."""
        controller, group_player, member = _group_with_member(mock_mass, initialize_group=False)
        group_protocol = group_player.own_protocol
        assert group_protocol is not None

        with patch.object(group_protocol, "on_group_member_updated") as on_group_member_updated:
            controller._forward_state_update(
                member, {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            )

        on_group_member_updated.assert_not_called()


class TestUnscopedPlayerLookups:
    """Derived player state must not depend on who triggered the recalculation."""

    def test_iter_players_ignores_the_user_filter(self, mock_mass: MagicMock) -> None:
        """iter_players is the internal view: every registered player, no user filter."""
        controller, group_player, member = _group_with_member(mock_mass)

        with _restricted_user(["member"]):
            assert controller.all_players() == [member]
            assert set(controller.iter_players()) == {group_player, member}

    def test_provider_filter_still_ignores_the_user_filter(self, mock_mass: MagicMock) -> None:
        """A lookup scoped to one provider must still ignore the user filter."""
        controller, group_player, member = _group_with_member(mock_mass)

        with _restricted_user(["member"]):
            found = set(controller.iter_players(provider_filter="test"))

        assert found == {group_player, member}

    def test_provider_sees_all_of_its_own_protocols(self, mock_mass: MagicMock) -> None:
        """A provider owns its protocols, so a user filter must never hide them from it."""
        _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        other_provider = MockProvider("other_provider", instance_id="other", mass=mock_mass)

        plain, plain_protocol = _attach(provider, "plain", "Plain")
        # an audio protocol of this provider on a player of another provider, which
        # the user cannot see
        other, _ = _attach(other_provider, "other", "Other")
        audio = _attach_audio(provider, other, "audio_other", "Other")
        _refresh(plain, other)

        with _restricted_user(["plain"]):
            owned = set(cast("Any", provider).protocols)

        assert owned == {plain_protocol, audio}

    def test_active_group_survives_a_restricted_user_context(self, mock_mass: MagicMock) -> None:
        """A member must still know its group when the triggering user cannot see it."""
        _ctrl, group_player, member = _group_with_member(mock_mass)
        group_protocol = cast("MockProtocol", group_player.own_protocol)
        group_protocol._attr_group_members = ["member"]
        group_protocol._attr_powered = True
        _refresh(group_player)

        with _restricted_user(["member"]):
            member.update_state(signal_event=False, force_update=True)
            assert member.state.active_group == "group"

    def test_synced_to_survives_a_restricted_user_context(self, mock_mass: MagicMock) -> None:
        """A follower must still resolve its sync leader across a user filter."""
        _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)

        leader, leader_protocol = _attach(provider, "leader", "Leader")
        follower, _ = _attach(provider, "follower", "Follower")

        leader_protocol._attr_group_members = [_proto("leader"), _proto("follower")]
        _refresh(leader, follower)

        with _restricted_user(["follower"]):
            follower.update_state(signal_event=False, force_update=True)
            assert follower.state.synced_to == "leader"

    def test_can_group_with_survives_a_restricted_user_context(self, mock_mass: MagicMock) -> None:
        """Expanding a provider id into players must not drop players the user cannot see."""
        _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        mock_mass.get_provider.return_value = provider

        player_a, protocol_a = _attach(provider, "player_a", "Player A")
        player_b, _ = _attach(provider, "player_b", "Player B")

        # a provider instance id stands for "every player of that provider"
        protocol_a._attr_can_group_with = {"test"}
        _refresh(player_a, player_b)

        with _restricted_user(["player_a"]):
            player_a.update_state(signal_event=False, force_update=True)
            assert "player_b" in player_a.state.can_group_with


class TestSleepTimer:
    """Test native sleep timer handling."""

    @staticmethod
    def _player(mock_mass: MagicMock) -> tuple[PlayerController, Player]:
        """Return a controller with one available player."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, _ = _attach(provider, "player_1", "Player 1")
        _refresh(player)
        return controller, player

    def test_set_and_clear_sleep_timer(self, mock_mass: MagicMock) -> None:
        """Setting a sleep timer exposes state and schedules the stop callback."""
        controller, player = self._player(mock_mass)

        expires_at = controller.set_sleep_timer("player_1", 30)

        assert controller.get_sleep_timer("player_1") == expires_at
        assert player.sleep_timer_expires_at == expires_at
        assert player.state.sleep_timer_mode == SleepTimerMode.DURATION
        mock_mass.call_later.assert_called_with(
            30,
            controller._handle_sleep_timer_expired,
            "player_1",
            task_id="player_sleep_timer_player_1",
        )
        mock_mass.signal_event.assert_any_call(
            EventType.PLAYER_SLEEP_TIMER_UPDATED,
            object_id="player_1",
            data=expires_at,
        )

        controller.clear_sleep_timer("player_1")

        # get_sleep_timer reads the model field directly, so this also asserts it cleared
        assert controller.get_sleep_timer("player_1") is None
        mock_mass.cancel_timer.assert_called_with("player_sleep_timer_player_1")
        mock_mass.signal_event.assert_any_call(
            EventType.PLAYER_SLEEP_TIMER_UPDATED,
            object_id="player_1",
            data=None,
        )
        assert player.state.sleep_timer_mode is None

    async def test_sleep_timer_expiry_stops_player(self, mock_mass: MagicMock) -> None:
        """An expired sleep timer clears its state and stops playback."""
        controller, player = self._player(mock_mass)
        player.set_sleep_timer(SleepTimerMode.DURATION, 123.0)
        controller.cmd_stop = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_sleep_timer_expired("player_1")

        assert controller.get_sleep_timer("player_1") is None
        assert player.sleep_timer_expires_at is None
        controller.cmd_stop.assert_awaited_once_with("player_1")
        mock_mass.signal_event.assert_any_call(
            EventType.PLAYER_SLEEP_TIMER_UPDATED,
            object_id="player_1",
            data=None,
        )

    def test_set_sleep_timer_rejects_invalid_duration(self, mock_mass: MagicMock) -> None:
        """A non-positive or float-overflowing duration raises and schedules nothing."""
        controller, _player = self._player(mock_mass)
        mock_mass.call_later.reset_mock()

        # None/0/-30 are no positive duration; 10**400 exceeds the float range for the expiry math
        for invalid in (None, 0, -30, 10**400):
            with pytest.raises(InvalidDataError):
                controller.set_sleep_timer("player_1", invalid)
        with pytest.raises(InvalidDataError):
            controller.set_sleep_timer("player_1", mode=SleepTimerMode.UNKNOWN)

        assert controller.get_sleep_timer("player_1") is None
        mock_mass.call_later.assert_not_called()

    def test_set_and_clear_queue_sleep_timer(self, mock_mass: MagicMock) -> None:
        """A timer that stops at a point in the queue is the queue's, the player keeps the mode."""
        controller, player = self._player(mock_mass)
        set_target = mock_mass.player_queues.set_sleep_timer_target
        set_target.side_effect = InvalidDataError("nothing plays")

        with pytest.raises(InvalidDataError):
            controller.set_sleep_timer("player_1", mode=SleepTimerMode.END_OF_CHAPTER)
        assert player.sleep_timer_mode is None

        def _set_target(queue_id: str, mode: SleepTimerMode) -> float:
            # the queue shows its target on the player
            controller.update_sleep_timer(queue_id, mode, 123.0)
            return 123.0

        set_target.side_effect = _set_target
        controller.set_sleep_timer("player_1", 30)
        mock_mass.call_later.reset_mock()
        expires_at = controller.set_sleep_timer("player_1", mode=SleepTimerMode.END_OF_CHAPTER)

        assert expires_at == 123.0
        set_target.assert_called_with("player_1", SleepTimerMode.END_OF_CHAPTER)
        assert player.sleep_timer_mode == SleepTimerMode.END_OF_CHAPTER
        assert player.sleep_timer_expires_at == 123.0  # type: ignore[unreachable]
        # the queue's timer replaces the duration timer, which stops nothing any more
        mock_mass.cancel_timer.assert_called_with("player_sleep_timer_player_1")
        # the queue stops itself, no stop is scheduled here
        assert not any(
            c.kwargs.get("task_id") == "player_sleep_timer_player_1"
            for c in mock_mass.call_later.call_args_list
        )
        mock_mass.signal_event.assert_any_call(
            EventType.PLAYER_SLEEP_TIMER_UPDATED,
            object_id="player_1",
            data=123.0,
        )

        # a duration timer replaces the queue's, the queue advances on its own again
        mock_mass.player_queues.drop_sleep_timer_target.reset_mock()
        controller.set_sleep_timer("player_1", 30)
        mock_mass.player_queues.drop_sleep_timer_target.assert_called_once_with("player_1")
        replaced_state = player.state
        assert replaced_state.sleep_timer_mode == SleepTimerMode.DURATION

        controller.clear_sleep_timer("player_1")

        mock_mass.player_queues.drop_sleep_timer_target.assert_called_with("player_1")
        assert player.sleep_timer_mode is None
        assert player.sleep_timer_expires_at is None

    @pytest.mark.parametrize(
        ("update_mode", "expires_at", "update", "expected"),
        [
            # position reports jitter
            (SleepTimerMode.END_OF_CHAPTER, 123.0, 123.4, 123.0),
            (SleepTimerMode.END_OF_CHAPTER, 123.0, 125.0, 125.0),
            # paused, and playing again
            (SleepTimerMode.END_OF_CHAPTER, 123.0, None, None),
            (SleepTimerMode.END_OF_CHAPTER, None, 125.0, 125.0),
            # a chapter timer moved to an item without chapters
            (SleepTimerMode.END_OF_ITEM, 123.0, 123.4, 123.4),
        ],
    )
    def test_update_sleep_timer(
        self,
        mock_mass: MagicMock,
        update_mode: SleepTimerMode,
        expires_at: float | None,
        update: float | None,
        expected: float | None,
    ) -> None:
        """The queue's estimate replaces the expiry once it moved by more than report jitter."""
        controller, player = self._player(mock_mass)
        player.set_sleep_timer(SleepTimerMode.END_OF_CHAPTER, expires_at)

        controller.update_sleep_timer("player_1", update_mode, update)

        assert player.sleep_timer_expires_at == expected
        assert player.sleep_timer_mode == update_mode


class _ReportsSyncedTo(MockProtocol):
    """A protocol whose provider reports the leader it is synced to itself."""

    reported_synced_to: str | None = None

    @property
    def synced_to(self) -> str | None:
        """Return the protocol id of the leader the provider reports."""
        return self.reported_synced_to


class TestCmdUngroupNewBranches:
    """
    Regression tests for the post-refactor cmd_ungroup flow.

    The refactor changed two things:

    - ``cmd_ungroup`` on a group player no longer calls ``cmd_set_members``
      (which would hit the "Cannot remove static member" guard); instead it
      stops or powers off the group entirely.
    - ``cmd_ungroup`` on a static member of a group recurses to ungroup the
      group, because static members cannot be released individually.
    """

    @pytest.mark.asyncio
    async def test_ungroup_group_player_with_power_uses_power_off(
        self, mock_mass: MagicMock
    ) -> None:
        """Ungroup on a group with explicit power control routes through cmd_power(False)."""
        # ensure power_control resolves to NATIVE so cmd_ungroup uses the power path
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_POWER_CONTROL: PLAYER_CONTROL_NATIVE})
        )
        controller = _controller(mock_mass)
        provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        group, group_protocol = _attach(provider, "g1", "Group", player_type=PlayerType.GROUP)
        group_protocol._attr_powered = True
        group_protocol._attr_group_members = ["member"]
        group_protocol._attr_supported_features = {PlayerFeature.POWER}

        # populate state.type / state.power_control / state.group_members
        _refresh(group)

        called: dict[str, bool | str] = {}

        async def _power(
            player_id: str,
            powered: bool,
            skip_auto_play: bool = False,  # noqa: ARG001
        ) -> None:
            called["player_id"] = player_id
            called["powered"] = powered

        controller._handle_cmd_power = _power  # type: ignore[method-assign]

        await controller.cmd_ungroup("g1")

        assert called == {"player_id": "g1", "powered": False}

    @pytest.mark.asyncio
    async def test_ungroup_powerless_group_calls_stop(self, mock_mass: MagicMock) -> None:
        """Ungroup on a powerless group falls through to _handle_cmd_stop."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        group, group_protocol = _attach(provider, "g1", "Group", player_type=PlayerType.GROUP)
        group_protocol._attr_powered = None  # no power control
        group_protocol._attr_group_members = ["member"]
        # no POWER feature → power_control auto-selects to NONE
        _refresh(group)

        stop_called: list[str] = []

        async def _stop(player_id: str) -> None:
            stop_called.append(player_id)

        controller._handle_cmd_stop = _stop  # type: ignore[method-assign]
        # also stub power to make sure we did NOT go down that branch
        power_called: list[str] = []

        async def _power(
            player_id: str,
            powered: bool,  # noqa: ARG001
            skip_auto_play: bool = False,  # noqa: ARG001
        ) -> None:
            power_called.append(player_id)

        controller._handle_cmd_power = _power  # type: ignore[method-assign]

        await controller.cmd_ungroup("g1")

        assert stop_called == ["g1"]
        assert power_called == []  # powerless group → never goes through cmd_power

    async def test_ungroup_many_skips_unavailable_players(self, mock_mass: MagicMock) -> None:
        """Unknown or unavailable ids are skipped so the remaining players still ungroup."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        group, group_protocol = _attach(provider, "g1", "Group", player_type=PlayerType.GROUP)
        group_protocol._attr_powered = None
        group_protocol._attr_group_members = ["member"]
        gone, gone_protocol = _attach(provider, "gone", "Gone")
        gone_protocol._attr_available = False
        # available follower whose sync leader is unavailable
        leader, leader_protocol = _attach(provider, "leader", "Leader")
        leader_protocol._attr_group_members = [_proto("leader"), _proto("follower")]
        follower, _ = _attach(provider, "follower", "Follower")
        _refresh(group, gone, leader, follower)
        leader_protocol._attr_available = False
        _refresh(leader)
        assert follower.state.synced_to == "leader"
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        await controller.cmd_ungroup_many(["missing", "gone", "follower", "g1"])

        controller._handle_cmd_stop.assert_awaited_once_with("g1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("leader_available", [True, False])
    async def test_ungroup_sync_member_routes_by_leader_availability(
        self, mock_mass: MagicMock, leader_available: bool
    ) -> None:
        """A member leaves through its sync leader, or on its own when that leader is offline."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
        leader, leader_protocol = _attach(provider, "leader", "Leader")
        leader_protocol._attr_group_members = [_proto("leader"), _proto("follower")]
        follower, follower_protocol = _attach(provider, "follower", "Follower")
        _refresh(leader, follower)
        leader_protocol._attr_available = leader_available
        _refresh(leader)
        assert follower.state.synced_to == "leader"
        controller.cmd_set_members = AsyncMock()  # type: ignore[method-assign]

        with patch.object(follower_protocol, "ungroup", new=AsyncMock()) as ungroup:
            await controller.cmd_ungroup("follower")

        if leader_available:
            controller.cmd_set_members.assert_awaited_once_with(
                "leader", player_ids_to_remove=["follower"]
            )
            ungroup.assert_not_awaited()
        else:
            controller.cmd_set_members.assert_not_awaited()
            ungroup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_detach_from_offline_leader_leaves_through_the_synced_audio_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """A member synced over one of its audio protocols leaves through that protocol."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
        airplay = MockProvider("airplay", instance_id="airplay", mass=mock_mass)
        leader, leader_native = _attach(provider, "leader", "Leader")
        leader_audio = _attach_audio(airplay, leader, "ap_leader", "Leader")
        member, member_native = _attach(provider, "member", "Member")
        # the member is grouped over AirPlay, not natively, as its provider reports
        _, member_audio = attach_mock(
            airplay,
            "ap_member",
            "Member",
            role=ProtocolRole.AUDIO,
            player=member,
            protocol_class=_ReportsSyncedTo,
        )
        cast("_ReportsSyncedTo", member_audio).reported_synced_to = "ap_leader"
        # the leader is offline
        leader_native._attr_available = False
        leader_audio._attr_available = False
        _refresh(leader, member)
        assert member.state.synced_to == "leader"
        assert member_native.synced_to is None
        member_native.ungroup = AsyncMock()  # type: ignore[method-assign]
        member_audio.ungroup = AsyncMock()  # type: ignore[method-assign]

        await controller._detach_member(member)

        member_audio.ungroup.assert_awaited_once()
        member_native.ungroup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ungroup_many_continues_when_a_member_cannot_leave_its_offline_leader(
        self, mock_mass: MagicMock
    ) -> None:
        """A member that fails to leave its offline leader doesn't stop the others."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
        leader, leader_protocol = _attach(provider, "leader", "Leader")
        leader_protocol._attr_group_members = [_proto("leader"), _proto("first"), _proto("second")]
        first, first_protocol = _attach(provider, "first", "First")
        second, second_protocol = _attach(provider, "second", "Second")
        _refresh(leader, first, second)
        leader_protocol._attr_available = False
        _refresh(leader)
        assert first.state.synced_to == second.state.synced_to == "leader"

        with (
            patch.object(
                first_protocol,
                "ungroup",
                new=AsyncMock(side_effect=PlayerCommandFailed("no route")),
            ) as first_ungroup,
            patch.object(second_protocol, "ungroup", new=AsyncMock()) as second_ungroup,
        ):
            await controller.cmd_ungroup_many(["first", "second"])

        first_ungroup.assert_awaited_once()
        second_ungroup.assert_awaited_once()


class TestPowerOffEndsTheQueue:
    """
    Tests for how powering a player off ends what it was playing.

    A power off has to clean up exactly as a stop command does. Stopping only the
    player leaves the queue's session open, so its preloading keeps pulling audio
    and a provider serving a live session (Spotify) stays tethered to Music
    Assistant for another track or two.
    """

    @staticmethod
    def _powered_playing_player(
        mock_mass: MagicMock,
    ) -> tuple[PlayerController, Player, MockProtocol, AsyncMock, AsyncMock]:
        """
        Build a powered, playing player that reaches the power-off stop branch.

        :param mock_mass: The mock MusicAssistant instance to build it on.
        :return: The controller, the player, its protocol, its stubbed player stop and
            queue stop.
        """
        # no power control, so the command returns right after the stop - which is
        # the only part of it these tests are about
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_POWER_CONTROL: PLAYER_CONTROL_NONE})
        )
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        protocol._attr_powered = True
        protocol._attr_playback_state = PlaybackState.PLAYING
        _refresh(player)
        player_stop = AsyncMock()
        controller._handle_cmd_stop = player_stop  # type: ignore[method-assign]
        queue_stop = AsyncMock()
        mock_mass.player_queues._handle_stop = queue_stop

        @contextlib.asynccontextmanager
        async def _no_wait(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
            """Stand in for the real wait, which burns its full timeout here."""
            yield

        controller.wait_for_player_update = _no_wait  # type: ignore[method-assign]
        return controller, player, protocol, player_stop, queue_stop

    @pytest.mark.asyncio
    async def test_power_off_stops_the_queue_the_player_is_playing(
        self, mock_mass: MagicMock
    ) -> None:
        """Powering off a player playing its own queue ends the queue, not just the device."""
        controller, _player, _protocol, player_stop, queue_stop = self._powered_playing_player(
            mock_mass
        )
        mock_mass.player_queues.get = MagicMock(return_value=_stub_queue("player_1"))

        await controller._handle_cmd_power("player_1", False)

        queue_stop.assert_awaited_once_with("player_1")
        # the queue stop issues the player stop itself
        player_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_power_off_playing_a_live_source_stops_the_player(
        self, mock_mass: MagicMock
    ) -> None:
        """A player playing a live external source has no queue to end."""
        controller, player, protocol, player_stop, queue_stop = self._powered_playing_player(
            mock_mass
        )
        # a live source publishes its own uri as the active source, which is not a queue id
        protocol._attr_active_source = "spotify_connect--abc://audio_source/main"
        _refresh(player)
        mock_mass.player_queues.get = MagicMock(return_value=None)

        await controller._handle_cmd_power("player_1", False)

        player_stop.assert_awaited_once_with("player_1")
        queue_stop.assert_not_awaited()

    # a stereo pair is a group member like any single speaker, so powering it off must
    # detach it from the group instead of leaving the group streaming to a dead speaker
    @pytest.mark.parametrize("member_type", [PlayerType.PLAYER, PlayerType.STEREO_PAIR])
    @pytest.mark.asyncio
    async def test_powering_off_a_group_member_does_not_end_the_groups_queue(
        self, mock_mass: MagicMock, member_type: PlayerType
    ) -> None:
        """A member of a group is ungrouped instead of stopped, so the group plays on."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_POWER_CONTROL: PLAYER_CONTROL_NONE})
        )
        controller, group_player, member = _group_with_member(mock_mass, member_type=member_type)
        cast("MockProtocol", group_player.own_protocol)._attr_powered = True
        member_protocol = cast("MockProtocol", member.own_protocol)
        member_protocol._attr_powered = True
        member_protocol._attr_playback_state = PlaybackState.PLAYING
        _refresh(group_player, member)
        assert member.state.active_group == "group"
        mock_mass.player_queues.get = MagicMock(return_value=_stub_queue("group"))
        player_stop = AsyncMock()
        controller._handle_cmd_stop = player_stop  # type: ignore[method-assign]
        queue_stop = AsyncMock()
        mock_mass.player_queues._handle_stop = queue_stop
        ungrouped: list[str] = []

        async def _ungroup(player_id: str) -> None:
            ungrouped.append(player_id)

        controller.cmd_ungroup = _ungroup  # type: ignore[method-assign]

        await controller._handle_cmd_power("member", False)

        assert ungrouped == ["member"]
        queue_stop.assert_not_awaited()
        player_stop.assert_not_awaited()


class TestSyncLeaderPowerOffUngroup:
    """
    Tests for detaching a sync leader from its own session when it powers off.

    A sync leader is removed from itself, which either hands leadership to a remaining
    member or dissolves the session - the type of the leader does not change that.
    """

    @staticmethod
    def _sync_leader(
        mock_mass: MagicMock, player_type: PlayerType
    ) -> tuple[PlayerController, Player, MockProtocol]:
        """
        Build a powered sync leader with one synced follower.

        :param mock_mass: The mock MusicAssistant instance to build it on.
        :param player_type: The type to register the leader as.
        :return: The controller, the leader and its protocol.
        """
        mock_mass.config.get_player_value = MagicMock(side_effect=_player_config_stub())
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        # the leader has no power control of its own, so the command returns right after
        # the group handling - which is the only part of it these tests are about
        leader, leader_protocol = _attach(provider, "leader", "Leader", player_type=player_type)
        leader_protocol._attr_group_members = [_proto("leader"), _proto("follower")]
        leader_protocol._attr_powered = True
        follower, _ = _attach(provider, "follower", "Follower")
        _refresh(leader, follower)
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        return controller, leader, leader_protocol

    @staticmethod
    def _report_power_natively(
        controller: PlayerController, leader: Player, leader_protocol: MockProtocol
    ) -> None:
        """
        Give the leader a native power control that reports the new state right away.

        :param controller: The controller the leader is registered on.
        :param leader: The sync leader to power natively.
        :param leader_protocol: The protocol of the leader.
        """
        # the state update the leader reports kicks off palette work on the mocked queue
        # media, which is not what these tests are about
        controller._schedule_palette_fetch = MagicMock()  # type: ignore[method-assign]
        leader_protocol._attr_supported_features.add(PlayerFeature.POWER)
        _refresh(leader)
        assert leader.state.power_control == PLAYER_CONTROL_NATIVE

        async def _power(powered: bool) -> None:
            leader_protocol._attr_powered = powered
            leader.update_state()

        leader_protocol.power = _power  # type: ignore[method-assign]

    @pytest.mark.parametrize("player_type", [PlayerType.PLAYER, PlayerType.STEREO_PAIR])
    @pytest.mark.asyncio
    async def test_ma_power_off_ungroups_a_sync_leader(
        self, mock_mass: MagicMock, player_type: PlayerType
    ) -> None:
        """An MA power off removes a sync leader from its own session."""
        controller, leader, _protocol = self._sync_leader(mock_mass, player_type)
        assert leader.state.group_members == ["leader", "follower"]
        ungrouped: list[str] = []

        async def _ungroup(player_id: str) -> None:
            ungrouped.append(player_id)

        controller.cmd_ungroup = _ungroup  # type: ignore[method-assign]

        await controller._handle_cmd_power("leader", False)

        assert ungrouped == ["leader"]

    @pytest.mark.parametrize("player_type", [PlayerType.PLAYER, PlayerType.STEREO_PAIR])
    def test_external_power_off_ungroups_a_sync_leader(
        self, mock_mass: MagicMock, player_type: PlayerType
    ) -> None:
        """A power control switched off outside MA removes a sync leader too."""
        controller, leader, _protocol = self._sync_leader(mock_mass, player_type)
        # the handler hands the coroutine to create_task, so the stub only records the call
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]

        controller.signal_player_state_update(leader, {"powered": (True, False)})

        controller.cmd_ungroup.assert_called_once_with("leader")

    @pytest.mark.asyncio
    async def test_ma_power_off_ungroups_a_sync_leader_once(
        self, mock_mass: MagicMock, running_background_tasks: None
    ) -> None:
        """
        The power off a leader reports back on an MA power command does not ungroup it again.

        A provider may keep listing the followers on the leader for a while after the
        ungroup (Sonos does), so the on->off transition would otherwise pass for an
        external power off of a sync leader.
        """
        controller, leader, protocol = self._sync_leader(mock_mass, PlayerType.PLAYER)
        self._report_power_natively(controller, leader, protocol)
        controller.cmd_ungroup = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_cmd_power("leader", False)

        assert leader.state.powered is False
        assert leader.state.group_members == ["leader", "follower"]
        controller.cmd_ungroup.assert_awaited_once_with("leader")

    @pytest.mark.asyncio
    async def test_ma_power_off_ungroups_once_when_the_stop_powers_the_device_off(
        self, mock_mass: MagicMock, running_background_tasks: None
    ) -> None:
        """A device that reports off as soon as it is stopped is not ungrouped twice either."""
        controller, leader, protocol = self._sync_leader(mock_mass, PlayerType.PLAYER)
        self._report_power_natively(controller, leader, protocol)
        protocol._attr_playback_state = PlaybackState.PLAYING
        _refresh(leader)
        assert leader.state.playback_state == PlaybackState.PLAYING
        controller.cmd_ungroup = AsyncMock()  # type: ignore[method-assign]

        async def _stop(_player: Player) -> None:
            # the device powers itself off when stopped and reports so right away
            # (AirPlay does), before the power command reaches it
            await protocol.power(False)

        controller._stop_player_or_its_queue = AsyncMock(side_effect=_stop)  # type: ignore[method-assign]

        await controller._handle_cmd_power("leader", False)

        assert leader.state.powered is False
        controller.cmd_ungroup.assert_awaited_once_with("leader")

    @pytest.mark.asyncio
    async def test_external_power_off_after_an_ma_power_off_still_ungroups(
        self, mock_mass: MagicMock, running_background_tasks: None
    ) -> None:
        """An MA power command only claims the power off it caused, not a later one."""
        controller, leader, protocol = self._sync_leader(mock_mass, PlayerType.PLAYER)
        self._report_power_natively(controller, leader, protocol)
        controller.cmd_ungroup = AsyncMock()  # type: ignore[method-assign]
        await controller._handle_cmd_power("leader", False)
        controller.cmd_ungroup.assert_awaited_once_with("leader")

        # a further on->off report stands in for a power off outside of MA later on
        controller.signal_player_state_update(leader, {"powered": (True, False)})

        assert controller.cmd_ungroup.await_count == 2

    def test_a_stale_ma_power_off_does_not_claim_an_external_power_off(
        self, mock_mass: MagicMock
    ) -> None:
        """A power off reported long after an MA power command counts as an external one."""
        controller, leader, _protocol = self._sync_leader(mock_mass, PlayerType.PLAYER)
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]
        leader.set_power_off_issued_at(
            time.monotonic() - players_group_commands.POWER_OFF_IN_PROGRESS_EXPIRY - 1
        )

        controller.signal_player_state_update(leader, {"powered": (True, False)})

        controller.cmd_ungroup.assert_called_once_with("leader")
        assert leader.power_off_issued_at is None

    @pytest.mark.parametrize("player_type", [PlayerType.PLAYER, PlayerType.STEREO_PAIR])
    @pytest.mark.asyncio
    async def test_power_off_leaves_the_synced_followers_powered(
        self,
        mock_mass: MagicMock,
        player_type: PlayerType,
        running_background_tasks: None,
    ) -> None:
        """A sync leader's power off leaves the power of the followers it leaves behind alone."""
        controller, _leader, _protocol = self._sync_leader(mock_mass, player_type)
        follower = controller.get_player("follower")
        assert follower is not None
        follower_protocol = cast("MockProtocol", follower.own_protocol)
        follower_protocol._attr_supported_features.add(PlayerFeature.POWER)
        _refresh(follower)
        follower_protocol.power = AsyncMock()  # type: ignore[method-assign]
        # a provider may keep listing the followers on the leader for a while after the
        # ungroup (Sonos does); the stubbed ungroup keeps them in place just like that
        controller.cmd_ungroup = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_cmd_power("leader", False)

        controller.cmd_ungroup.assert_awaited_once_with("leader")
        follower_protocol.power.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_power_off_of_a_groups_leader_leaves_its_members_powered(
        self, mock_mass: MagicMock, running_background_tasks: None
    ) -> None:
        """
        Powering off the member a group player elected as leader releases only that member.

        The followers on it are the group's members: the group re-forms around another one
        of them, so their power state is theirs to keep.
        """
        controller, leader, _protocol = self._sync_leader(mock_mass, PlayerType.PLAYER)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        group, group_protocol = _attach(provider, "group", "Group", player_type=PlayerType.GROUP)
        group_protocol._attr_group_members = ["leader", "follower"]
        group_protocol._attr_powered = True
        follower = controller.get_player("follower")
        assert follower is not None
        follower_protocol = cast("MockProtocol", follower.own_protocol)
        follower_protocol._attr_supported_features.add(PlayerFeature.POWER)
        follower_protocol.power = AsyncMock()  # type: ignore[method-assign]
        _refresh(group, leader, follower)
        assert leader.state.active_group == "group"
        ungrouped: list[str] = []

        async def _ungroup(player_id: str) -> None:
            ungrouped.append(player_id)

        controller.cmd_ungroup = _ungroup  # type: ignore[method-assign]

        await controller._handle_cmd_power("leader", False)

        assert ungrouped == ["leader"]
        follower_protocol.power.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ma_power_off_ungroups_a_leader_synced_over_its_audio_protocol(
        self, mock_mass: MagicMock
    ) -> None:
        """
        An MA power off ungroups a leader whose session lives on its active audio protocol.

        The native protocol of the leader lists no group members; the followers exist only
        through its active output protocol and surface on the resolved state. An external
        power off already reads that resolved state, so an MA power off must ungroup it too.
        """
        mock_mass.config.get_player_value = MagicMock(side_effect=_player_config_stub())
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        sendspin = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)
        leader, leader_native = _attach(provider, "leader", "Leader")
        leader_native._attr_powered = True
        leader_audio = _attach_audio(sendspin, leader, "leader_audio", "Leader")
        leader_audio._attr_group_members = ["leader_audio", "follower_audio"]
        follower, _ = _attach(provider, "follower", "Follower")
        _attach_audio(sendspin, follower, "follower_audio", "Follower")
        leader.set_active_output_protocol("leader_audio")
        _refresh(leader, follower)
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]

        # the session lives on the audio protocol: the native list is empty, the resolved
        # one is not
        assert leader_native.group_members == []
        assert leader.state.group_members == ["leader", "follower"]

        ungrouped: list[str] = []

        async def _ungroup(player_id: str) -> None:
            ungrouped.append(player_id)

        controller.cmd_ungroup = _ungroup  # type: ignore[method-assign]

        await controller._handle_cmd_power("leader", False)

        assert ungrouped == ["leader"]


class TestExternalPowerOffUnsync:
    """
    Tests for unsyncing a player when its power is turned off outside of MA.

    When a player's (final) power state flips on->off because its linked power
    control was switched off directly - rather than via an MA power command -
    the player must be removed from any (sync)group it is part of.
    """

    def _make_synced_player(
        self, mock_mass: MagicMock, player_type: PlayerType = PlayerType.PLAYER
    ) -> tuple[PlayerController, Player]:
        """
        Build a controller with a player synced to a registered leader.

        :param mock_mass: The mock MusicAssistant instance to build it on.
        :param player_type: The type to register the synced player as.
        """
        controller = _controller(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        leader, leader_protocol = _attach(provider, "leader", "Leader")
        leader_protocol._attr_group_members = [_proto("leader"), _proto("p1")]
        player, _ = _attach(provider, "p1", "Player", player_type=player_type)
        _refresh(leader, player)
        # isolate the unsync branch from the unrelated state-forwarding machinery
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]
        return controller, player

    @pytest.mark.parametrize("player_type", [PlayerType.PLAYER, PlayerType.STEREO_PAIR])
    def test_power_off_unsyncs_synced_player(
        self, mock_mass: MagicMock, player_type: PlayerType
    ) -> None:
        """An on->off power transition ungroups a synced player."""
        controller, player = self._make_synced_player(mock_mass, player_type)
        assert player.state.synced_to == "leader"

        controller.signal_player_state_update(player, {"powered": (True, False)})

        controller.cmd_ungroup.assert_called_once_with("p1")  # type: ignore[attr-defined]

    def test_power_on_does_not_unsync(self, mock_mass: MagicMock) -> None:
        """An off->on power transition leaves the player synced."""
        controller, player = self._make_synced_player(mock_mass)

        controller.signal_player_state_update(player, {"powered": (False, True)})

        controller.cmd_ungroup.assert_not_called()  # type: ignore[attr-defined]

    def test_no_power_control_is_ignored(self, mock_mass: MagicMock) -> None:
        """A None->off transition (player without power control) is ignored."""
        controller, player = self._make_synced_player(mock_mass)

        controller.signal_player_state_update(player, {"powered": (None, False)})

        controller.cmd_ungroup.assert_not_called()  # type: ignore[attr-defined]

    def test_power_off_ungrouped_player_is_not_ungrouped(self, mock_mass: MagicMock) -> None:
        """Powering off a player that is not in any group has nothing to ungroup."""
        controller = _controller(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        player, _ = _attach(provider, "p1", "Player")
        _refresh(player)
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]

        controller.signal_player_state_update(player, {"powered": (True, False)})

        controller.cmd_ungroup.assert_not_called()
        # it was not playing either, so there is no queue to end
        assert _scheduled_queue_stops(mock_mass) == []

    def test_power_off_of_a_group_player_does_not_ungroup_it(self, mock_mass: MagicMock) -> None:
        """
        A group player has no group of its own to leave.

        Its members are released by its own power off, and ungrouping a group player is
        defined as powering it off, so routing it through here would achieve nothing.
        """
        controller, group_player, _member = _group_with_member(mock_mass)
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]
        assert group_player.state.group_members == ["member"]

        controller.signal_player_state_update(group_player, {"powered": (True, False)})

        controller.cmd_ungroup.assert_not_called()


class TestExternalPowerOffEndsTheQueue:
    """
    Tests for ending the queue when a player is powered off outside of MA.

    Switching a speaker off with its own remote or flipping its linked power control
    directly never reaches the MA power command, so the queue session it was playing
    survived: its preloading kept pulling audio and a provider serving a live session
    (Spotify) stayed tethered for another track or two.
    """

    @staticmethod
    def _standalone_playing_player(
        mock_mass: MagicMock,
        player_type: PlayerType = PlayerType.PLAYER,
    ) -> tuple[PlayerController, Player, MockProtocol, AsyncMock]:
        """
        Build an ungrouped player that is powered on and playing its own queue.

        :param mock_mass: The mock MusicAssistant instance to build it on.
        :param player_type: The type to register the player as.
        :return: The controller, the player, its protocol and the stubbed queue stop.
        """
        controller = _controller(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "p1", "Player", player_type=player_type)
        # advertise POWER so the player resolves to native power control and its
        # reported power state is the one that flips
        protocol._attr_supported_features.add(PlayerFeature.POWER)
        protocol._attr_powered = True
        protocol._attr_playback_state = PlaybackState.PLAYING
        _refresh(player)
        assert player.state.powered is True
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]
        mock_mass.player_queues.get = MagicMock(return_value=_stub_queue("p1"))
        queue_stop = AsyncMock()
        mock_mass.player_queues._handle_stop = queue_stop
        return controller, player, protocol, queue_stop

    # a stereo pair renders audio and owns a queue just like a single speaker does,
    # and an MA power off ends its queue without looking at the type either
    @pytest.mark.parametrize("player_type", [PlayerType.PLAYER, PlayerType.STEREO_PAIR])
    def test_power_off_schedules_the_queue_stop(
        self, mock_mass: MagicMock, player_type: PlayerType
    ) -> None:
        """An on->off power transition on a playing player ends its queue."""
        controller, player, _protocol, _queue_stop = self._standalone_playing_player(
            mock_mass, player_type
        )

        controller.signal_player_state_update(player, {"powered": (True, False)})

        assert len(scheduled := _scheduled_queue_stops(mock_mass)) == 1
        assert scheduled[0].args == (
            players_power.EXTERNAL_POWER_OFF_STOP_DELAY,
            controller._stop_queue_on_external_power_off,
            "p1",
        )

    def test_a_device_reporting_its_stop_along_with_the_power_off_is_covered(
        self, mock_mass: MagicMock
    ) -> None:
        """A player that powers itself off reports idle in the same update."""
        controller, player, protocol, _queue_stop = self._standalone_playing_player(mock_mass)
        protocol._attr_playback_state = PlaybackState.IDLE
        _refresh(player)

        controller.signal_player_state_update(
            player,
            {
                "powered": (True, False),
                "playback_state": (PlaybackState.PLAYING, PlaybackState.IDLE),
            },
        )

        assert len(_scheduled_queue_stops(mock_mass)) == 1

    def test_power_off_of_an_idle_player_is_ignored(self, mock_mass: MagicMock) -> None:
        """
        A player that was already idle has no queue to end.

        This is also what keeps an MA power off from ending the queue twice: it stops
        the player before it powers it off, so the power change finds it idle.
        """
        controller, player, protocol, _queue_stop = self._standalone_playing_player(mock_mass)
        protocol._attr_playback_state = PlaybackState.IDLE
        _refresh(player)

        controller.signal_player_state_update(player, {"powered": (True, False)})

        assert _scheduled_queue_stops(mock_mass) == []

    def test_power_on_does_not_end_the_queue(self, mock_mass: MagicMock) -> None:
        """An off->on power transition leaves the queue alone."""
        controller, player, _protocol, _queue_stop = self._standalone_playing_player(mock_mass)

        controller.signal_player_state_update(player, {"powered": (False, True)})

        assert _scheduled_queue_stops(mock_mass) == []

    # a stereo pair reaches the queue-stop gate (it renders audio) but must take the
    # ungroup route, exactly like a single speaker that is part of a group
    @pytest.mark.parametrize("member_type", [PlayerType.PLAYER, PlayerType.STEREO_PAIR])
    def test_a_grouped_player_is_ungrouped_instead(
        self, mock_mass: MagicMock, member_type: PlayerType
    ) -> None:
        """A member of a group is detached from it, which ends the group's queue."""
        controller, _group_player, member = _group_with_member(mock_mass, member_type=member_type)
        cast("MockProtocol", member.own_protocol)._attr_playback_state = PlaybackState.PLAYING
        _refresh(member)
        assert member.state.active_group == "group"
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]

        controller.signal_player_state_update(member, {"powered": (True, False)})

        controller.cmd_ungroup.assert_called_once_with("member")
        assert _scheduled_queue_stops(mock_mass) == []

    def test_a_group_leader_ends_its_own_queue(self, mock_mass: MagicMock) -> None:
        """A group player is not mistaken for a member, so its own queue is ended."""
        controller, group, group_protocol, _queue_stop = self._standalone_playing_player(
            mock_mass, PlayerType.GROUP
        )
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        member, _ = _attach(provider, "member", "Member")
        _refresh(member)
        group_protocol._attr_group_members = ["member"]
        _refresh(group)
        assert group.state.group_members == ["member"]

        controller.signal_player_state_update(group, {"powered": (True, False)})

        assert len(_scheduled_queue_stops(mock_mass)) == 1
        controller.cmd_ungroup.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_the_scheduled_stop_ends_the_players_own_queue(
        self, mock_mass: MagicMock
    ) -> None:
        """The player is still off when the wait is over, so its queue is ended."""
        controller, player, protocol, queue_stop = self._standalone_playing_player(mock_mass)
        protocol._attr_powered = False
        _refresh(player)

        await controller._stop_queue_on_external_power_off("p1")

        queue_stop.assert_awaited_once_with("p1")

    @pytest.mark.asyncio
    async def test_power_that_comes_straight_back_leaves_the_queue_playing(
        self, mock_mass: MagicMock
    ) -> None:
        """A power control reporting its entity as briefly unavailable must not stop the music."""
        controller, _player, _protocol, queue_stop = self._standalone_playing_player(mock_mass)

        await controller._stop_queue_on_external_power_off("p1")

        queue_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_power_on_that_overlaps_the_wait_keeps_its_playback(
        self, mock_mass: MagicMock
    ) -> None:
        """
        A power on running while the stop waits must not have its playback stopped.

        cmd_power holds the playback lock while it powers the player on and resumes
        the queue, and the player only reports itself powered somewhere in there.
        """
        controller, player, protocol, queue_stop = self._standalone_playing_player(mock_mass)
        protocol._attr_powered = False
        _refresh(player)

        async with controller.get_player_lock("p1", PlayerLockPurpose.PLAYBACK):
            stopper = asyncio.create_task(controller._stop_queue_on_external_power_off("p1"))
            # let the stop run up to the lock it has to wait for
            for _ in range(5):
                await asyncio.sleep(0)
            queue_stop.assert_not_awaited()
            # the power on lands before it hands the lock over
            protocol._attr_powered = True
            _refresh(player)

        await stopper

        queue_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_another_players_queue_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A player hearing someone else's audio must not end that queue."""
        controller, player, protocol, queue_stop = self._standalone_playing_player(mock_mass)
        protocol._attr_powered = False
        protocol._attr_active_source = "other_player"
        _refresh(player)
        mock_mass.player_queues.get = MagicMock(return_value=_stub_queue("other_player"))

        await controller._stop_queue_on_external_power_off("p1")

        queue_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unavailable_player_is_handled_quietly(self, mock_mass: MagicMock) -> None:
        """A player that went unavailable along with its power is not worth a warning."""
        controller, player, protocol, queue_stop = self._standalone_playing_player(mock_mass)
        protocol._attr_powered = False
        _refresh(player)
        queue_stop.side_effect = PlayerUnavailableError("Player p1 is not available")

        await controller._stop_queue_on_external_power_off("p1")

        queue_stop.assert_awaited_once_with("p1")


class _SessionedGroup(MockProtocol):
    """A group protocol that holds its members."""

    @property
    def is_active_session(self) -> bool:
        """Return that the group holds its members."""
        return True


class TestPlayMediaOverride:
    """
    Tests for the new CONF_PLAY_MEDIA_OVERRIDES_GROUP behavior.

    When a captured child player receives an explicit play_media command, the
    default behavior is to *release* it from the active group/sync and play
    directly on the targeted player. The legacy behavior (forward to the
    leader) is preserved via the per-player config opt-out.
    """

    @staticmethod
    def _captured_member(
        mock_mass: MagicMock, *, static: bool | None = None
    ) -> tuple[PlayerController, Player, MockProtocol, Player]:
        """
        Build a group that holds one member, with play_media itself recorded per player.

        :param static: Whether the member is static (None: the group lists no static members
            and has no set_members support).
        :return: The controller, the group, its protocol and the member.
        """
        controller = _controller(mock_mass)
        group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
        member_provider = MockProvider("test", instance_id="test", mass=mock_mass)
        group, group_protocol = _attach(
            group_provider,
            "g1",
            "Group",
            player_type=PlayerType.GROUP,
            protocol_class=_SessionedGroup,
        )
        group_protocol._attr_powered = None
        group_protocol._attr_group_members = ["member"]
        if static is not None:
            group_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
            group_protocol._attr_static_group_members = ["member"] if static else []
        member, _ = _attach(member_provider, "member", "Member")
        _refresh(group, member)
        # sanity: the member is captured by the group
        assert member.state.active_group == "g1"
        # the play_media wrapper acquires a playback lock; start without any
        controller._player_command_locks = {}
        return controller, group, group_protocol, member

    @pytest.mark.asyncio
    async def test_override_disabled_redirects_to_group(self, mock_mass: MagicMock) -> None:
        """With override disabled, play_media on a captured child redirects to the group."""
        controller, _group, _protocol, _member = self._captured_member(mock_mass)
        _set_play_media_override(mock_mass, False)

        played_on: list[str] = []

        async def _handle_play_media(player_id: str, media: object) -> None:  # noqa: ARG001
            played_on.append(player_id)

        controller._handle_play_media = _handle_play_media  # type: ignore[method-assign]

        media = MagicMock(uri="x", source_id="src")
        await controller.play_media("member", media)

        # legacy behavior: redirected to the group leader
        assert played_on == ["g1"]

    @pytest.mark.asyncio
    async def test_override_releases_dynamic_member(self, mock_mass: MagicMock) -> None:
        """With override enabled, play_media on a dynamic group member releases it first."""
        # NOT a static member ⇒ dynamic — can be removed via set_members
        controller, _group, _protocol, _member = self._captured_member(mock_mass, static=False)

        # default: override enabled
        _set_play_media_override(mock_mass, True)

        set_members_calls: list[dict[str, object]] = []

        async def _cmd_set_members(
            target_player: str,
            player_ids_to_add: list[str] | None = None,  # noqa: ARG001
            player_ids_to_remove: list[str] | None = None,
        ) -> None:
            set_members_calls.append(
                {"player_id": target_player, "remove": player_ids_to_remove or []}
            )

        controller.cmd_set_members = _cmd_set_members  # type: ignore[method-assign]

        played_on: list[str] = []

        async def _handle_play_media(player_id: str, media: object) -> None:  # noqa: ARG001
            played_on.append(player_id)

        controller._handle_play_media = _handle_play_media  # type: ignore[method-assign]

        media = MagicMock(uri="x", source_id="src")
        with patch.object(
            controller,
            "wait_for_player_update",
            _skip_player_update_wait,
        ):
            await controller.play_media("member", media)

        # the member was removed from the group ...
        assert set_members_calls == [{"player_id": "g1", "remove": ["member"]}]
        # ... and then play_media was issued directly on the member, NOT on the group
        assert played_on == ["member"]

    @pytest.mark.asyncio
    async def test_override_stops_static_group(self, mock_mass: MagicMock) -> None:
        """With override enabled, play_media on a STATIC group member stops the group."""
        # static member - cannot be removed individually; no power control ⇒ stop
        controller, _group, _protocol, _member = self._captured_member(mock_mass, static=True)

        _set_play_media_override(mock_mass, True)

        stop_calls: list[str] = []
        power_calls: list[tuple[str, bool]] = []

        async def _stop(player_id: str) -> None:
            stop_calls.append(player_id)

        async def _power(
            player_id: str,
            powered: bool,
            skip_auto_play: bool = False,  # noqa: ARG001
        ) -> None:
            power_calls.append((player_id, powered))

        controller._handle_cmd_stop = _stop  # type: ignore[method-assign]
        controller._handle_cmd_power = _power  # type: ignore[method-assign]

        played_on: list[str] = []

        async def _handle_play_media(player_id: str, media: object) -> None:  # noqa: ARG001
            played_on.append(player_id)

        controller._handle_play_media = _handle_play_media  # type: ignore[method-assign]

        media = MagicMock(uri="x", source_id="src")
        with patch.object(
            controller,
            "wait_for_player_update",
            _skip_player_update_wait,
        ):
            await controller.play_media("member", media)

        # powerless group + static member: we should have stopped the group ...
        assert stop_calls == ["g1"]
        # ... not powered it off ...
        assert power_calls == []
        # ... and play_media was issued directly on the member
        assert played_on == ["member"]


class TestStaticGroupReleaseUnderTheGroupLock:
    """A command that first ends what a member's static group plays holds the group's lock."""

    @pytest.fixture(autouse=True)
    def _shrink_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_SLOW_THRESHOLD", 0.01)
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_TIMEOUT", 0.05)

    @staticmethod
    def _static_member(
        mock_mass: MagicMock,
    ) -> tuple[PlayerController, list[tuple[str, frozenset[str]]]]:
        """
        Build a static group with one member; record what is done and under which locks.

        :return: The controller and the record: per action the locks its task held.
        """
        controller, _group, _protocol, _member = TestPlayMediaOverride._captured_member(
            mock_mass, static=True
        )
        _set_play_media_override(mock_mass, True)
        done: list[tuple[str, frozenset[str]]] = []

        def _record(action: str) -> None:
            task = asyncio.current_task()
            assert task is not None
            done.append((action, frozenset(controller._task_held_locks.get(task, set()))))

        async def _stop(player_id: str) -> None:
            _record(f"stop {player_id}")

        async def _power(player_id: str, powered: bool, skip_auto_play: bool = False) -> None:  # noqa: ARG001
            _record(f"power {player_id} {powered}")

        async def _select_source(player_id: str, source: str | None) -> None:
            _record(f"select_source {player_id} {source}")

        async def _play_media(player_id: str, media: object) -> None:  # noqa: ARG001
            _record(f"play_media {player_id}")

        controller._handle_cmd_stop = _stop  # type: ignore[method-assign]
        controller._handle_cmd_power = _power  # type: ignore[method-assign]
        controller._handle_select_source = _select_source  # type: ignore[method-assign]
        controller._handle_play_media = _play_media  # type: ignore[method-assign]
        return controller, done

    @staticmethod
    def _command(controller: PlayerController, name: str) -> Coroutine[Any, Any, None]:
        """Return the command that releases the member from its static group."""
        if name == "select_source":
            return controller.select_source("member", "hdmi")
        if name == "play_media":
            return controller.play_media("member", MagicMock(uri="x", source_id="src"))
        return controller.cmd_ungroup("member")

    @pytest.mark.parametrize(
        ("name", "busy"),
        [
            ("select_source", "g1"),
            ("select_source", "member"),
            ("play_media", "g1"),
            ("play_media", "member"),
            # the ungroup acts on the group only
            ("cmd_ungroup", "g1"),
        ],
    )
    async def test_a_busy_group_or_member_leaves_the_group_playing(
        self, mock_mass: MagicMock, name: str, busy: str
    ) -> None:
        """A command that cannot get its locks fails before it stops the group."""
        controller, done = self._static_member(mock_mass)

        with patch.object(controller, "wait_for_player_update", _skip_player_update_wait):
            async with _lock_held_elsewhere(controller, busy):
                with pytest.raises(ResourceBusyError):
                    await self._command(controller, name)

        assert done == []

    @pytest.mark.parametrize("name", ["select_source", "play_media"])
    async def test_the_group_is_stopped_and_the_member_commanded_under_both_locks(
        self, mock_mass: MagicMock, name: str
    ) -> None:
        """The group's lock and the member's are held from the stop until the command."""
        controller, done = self._static_member(mock_mass)

        with patch.object(controller, "wait_for_player_update", _skip_player_update_wait):
            await self._command(controller, name)

        both = frozenset({"playback_g1", "playback_member"})
        assert [action for action, _locks in done] == [
            "stop g1",
            "select_source member hdmi" if name == "select_source" else "play_media member",
        ]
        assert all(locks == both for _action, locks in done)


class TestOwnerChangesWhileWaitingForTheLock:
    """A command takes its locks in the order of the owner its player has once it holds them."""

    @pytest.fixture(autouse=True)
    def _shrink_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_SLOW_THRESHOLD", 0.01)
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_TIMEOUT", 0.5)

    @staticmethod
    def _member(
        mock_mass: MagicMock, *, captured: bool
    ) -> tuple[PlayerController, Player, MockProtocol, Player, list[tuple[str, frozenset[str]]]]:
        """
        Build a group and a player that is its member or not; record the stops and their locks.

        :param captured: Whether the group holds the player to begin with.
        :return: The controller, the group, its protocol, the player and the record of stops.
        """
        controller, group, protocol, member = TestPlayMediaOverride._captured_member(mock_mass)
        if not captured:
            protocol._attr_group_members = []
            _refresh(group, member)
            assert member.state.active_group is None
        stops: list[tuple[str, frozenset[str]]] = []

        async def _stop(player_id: str) -> None:
            task = asyncio.current_task()
            assert task is not None
            stops.append((player_id, frozenset(controller._task_held_locks.get(task, set()))))

        controller._handle_cmd_stop = _stop  # type: ignore[method-assign]
        controller.get_active_queue = MagicMock(return_value=None)  # type: ignore[method-assign]
        return controller, group, protocol, member, stops

    async def test_a_player_that_joins_a_group_meanwhile_is_stopped_through_it(
        self, mock_mass: MagicMock
    ) -> None:
        """The command lets go of the player's lock and stops the group under the group's alone."""
        controller, group, protocol, member, stops = self._member(mock_mass, captured=False)

        async with _lock_held_elsewhere(controller, "member"):
            stop = asyncio.create_task(controller.cmd_stop("member"))
            await asyncio.sleep(0.05)
            protocol._attr_group_members = ["member"]
            _refresh(group, member)
        await stop

        assert stops == [("g1", frozenset({"playback_g1"}))]

    async def test_a_stop_is_refused_for_a_group_the_user_may_not_use(
        self, mock_mass: MagicMock
    ) -> None:
        """A user who may use the player only is refused once it joined a group meanwhile."""
        controller, group, protocol, member, stops = self._member(mock_mass, captured=False)

        with _restricted_user(["member"]):
            async with _lock_held_elsewhere(controller, "member"):
                stop = asyncio.create_task(controller.cmd_stop("member"))
                await asyncio.sleep(0.05)
                protocol._attr_group_members = ["member"]
                _refresh(group, member)
            with pytest.raises(InsufficientPermissions):
                await stop

        assert stops == []

    async def test_a_stop_on_a_player_the_user_may_use_stops_it(self, mock_mass: MagicMock) -> None:
        """A user who may use the player stops it when its group did not change."""
        controller, _group, _protocol, _member, stops = self._member(mock_mass, captured=False)

        with _restricted_user(["member"]):
            await controller.cmd_stop("member")

        assert stops == [("member", frozenset({"playback_member"}))]

    async def test_a_player_whose_group_ends_meanwhile_is_stopped_on_its_own(
        self, mock_mass: MagicMock
    ) -> None:
        """The command lets go of the group's lock and runs with the player's lock alone."""
        controller, group, protocol, member, stops = self._member(mock_mass, captured=True)

        async with _lock_held_elsewhere(controller, "g1"):
            stop = asyncio.create_task(controller.cmd_stop("member"))
            await asyncio.sleep(0.05)
            protocol._attr_group_members = []
            _refresh(group, member)
        await stop

        assert stops == [("member", frozenset({"playback_member"}))]

    async def test_a_player_the_command_holds_that_joined_a_group_fails_at_once(
        self, mock_mass: MagicMock
    ) -> None:
        """A command that already holds the player does not wait for the group it joined."""
        controller, group, protocol, member, _stops = self._member(mock_mass, captured=False)

        async with controller.get_player_lock("member"):
            protocol._attr_group_members = ["member"]
            _refresh(group, member)
            async with _lock_held_elsewhere(controller, "g1"):
                waits = record_lock_waits(controller)
                with pytest.raises(ResourceBusyError):
                    async with controller.get_group_and_player_lock("member"):
                        pytest.fail("the body ran without the group's lock")

        assert waits == []

    async def test_a_player_whose_owner_keeps_changing_fails_as_busy(
        self, controller: PlayerController
    ) -> None:
        """The locks are taken again a bounded number of times, then the command fails."""
        owners = itertools.cycle(["g1", "p1"])
        checks: list[str] = []

        def _owner(_player_id: str) -> str:
            checks.append(owner := next(owners))
            return owner

        controller._playback_owner_id = _owner  # type: ignore[assignment]
        with pytest.raises(ResourceBusyError) as busy:
            async with controller.get_group_and_player_lock("p1"):
                pytest.fail("the body ran with an owner that changed")

        assert busy.value.translation_key == "player_busy"
        assert len(checks) == 2 * PLAYER_LOCK_OWNER_ATTEMPTS
        assert not controller._lock_holders


class TestNestedGroupLockIsReentered:
    """Taking a player out of its group takes the group's lock again, never for the first time."""

    @pytest.mark.parametrize("name", ["select_source", "play_media", "cmd_ungroup"])
    async def test_the_group_lock_is_never_awaited_while_the_member_is_held(
        self, mock_mass: MagicMock, name: str
    ) -> None:
        """Every wait for the group's lock happens while the task holds no lock of the member."""
        controller, _group, _protocol, _member = TestPlayMediaOverride._captured_member(
            mock_mass, static=False
        )
        _set_play_media_override(mock_mass, True)
        controller._handle_set_members = AsyncMock()  # type: ignore[method-assign]
        controller._handle_select_source = AsyncMock()  # type: ignore[method-assign]
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        waits: list[tuple[str, frozenset[str]]] = []
        acquire = controller._acquire_player_lock

        async def _record(
            lock: asyncio.Lock, lock_key: str, purpose: PlayerLockPurpose, player_id: str
        ) -> None:
            task = asyncio.current_task()
            assert task is not None
            waits.append((lock_key, frozenset(controller._task_held_locks.get(task, set()))))
            await acquire(lock, lock_key, purpose, player_id)

        controller._acquire_player_lock = _record  # type: ignore[method-assign]
        with patch.object(controller, "wait_for_player_update", _skip_player_update_wait):
            await TestStaticGroupReleaseUnderTheGroupLock._command(controller, name)

        controller._handle_set_members.assert_awaited_once()
        assert ("playback_g1", frozenset()) in waits
        assert all("playback_member" not in held for key, held in waits if key == "playback_g1")


class TestReleaseWaitForPlayMedia:
    """
    The release of a captured player for an explicit play_media waits for its confirmation.

    The loop runs on a virtual clock, so the waits are measured in the seconds real devices
    take, and the order of the leave, the play and the end of the wait does not depend on the
    speed of the machine.
    """

    # the bound of the wait, in seconds
    BOUND = players_playback.RELEASE_FOR_PLAY_MEDIA_TIMEOUT

    @pytest.fixture(autouse=True)
    async def _virtual_clock(self) -> AsyncIterator[None]:
        with virtual_loop_clock():
            yield

    @staticmethod
    def _captured_member(
        mock_mass: MagicMock, capture: str
    ) -> tuple[PlayerController, MockProtocol, Player]:
        """
        Build a member synced to a leader, or a dynamic or static member of a group.

        :return: The controller, the protocol of the leader or group, and the member.
        """
        controller = _controller(mock_mass)
        provider = MockProvider("test", instance_id="test", mass=mock_mass)
        member, _ = _attach(provider, "member", "Member")
        if capture == "synced":
            owner, owner_protocol = _attach(provider, "owner", "Leader")
            owner_protocol._attr_group_members = [_proto("owner"), _proto("member")]
        else:
            group_provider = MockProvider("test_group", instance_id="test_group", mass=mock_mass)
            owner, owner_protocol = _attach(
                group_provider,
                "owner",
                "Group",
                player_type=PlayerType.GROUP,
                protocol_class=_SessionedGroup,
            )
            owner_protocol._attr_powered = None
            owner_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
            owner_protocol._attr_group_members = ["member"]
            owner_protocol._attr_static_group_members = ["member"] if capture == "static" else []
        controller._player_command_locks = {}
        _refresh(owner, member)
        _set_play_media_override(mock_mass, True)
        return controller, owner_protocol, member

    @staticmethod
    def _confirm_release_after(
        controller: PlayerController,
        owner: MockProtocol,
        member: Player,
        delay: float | None,
        events: list[str],
    ) -> None:
        """Let the release command return at once and the player report its leave later."""

        def _report_left() -> None:
            events.append("left")
            owner._attr_group_members = [_proto("owner")] if owner.type == PlayerType.PLAYER else []
            owner.update_state()
            member.update_state()

        async def _release(*_args: object, **_kwargs: object) -> None:
            if delay is not None:
                asyncio.get_running_loop().call_later(delay, _report_left)

        controller.cmd_ungroup = _release  # type: ignore[method-assign]
        controller.cmd_set_members = _release  # type: ignore[method-assign]
        controller._handle_cmd_stop = _release  # type: ignore[method-assign]

    @classmethod
    async def _play_media(cls, controller: PlayerController, events: list[str]) -> None:
        """
        Play on the member, and add the play and the end of the wait to the events.

        The end of the wait is "bound", the time twice as long "twice the bound".
        """
        loop = asyncio.get_running_loop()

        async def _handle_play_media(player_id: str, media: object) -> None:  # noqa: ARG001
            events.append(f"play on {player_id}")

        controller._handle_play_media = _handle_play_media  # type: ignore[method-assign]
        markers = [
            loop.call_later(cls.BOUND, events.append, "bound"),
            loop.call_later(2 * cls.BOUND, events.append, "twice the bound"),
        ]
        try:
            await controller.play_media("member", MagicMock(uri="x", source_id="src"))
        finally:
            for marker in markers:
                marker.cancel()

    @pytest.mark.parametrize("capture", ["synced", "dynamic", "static"])
    async def test_plays_as_soon_as_the_player_reports_its_leave(
        self, mock_mass: MagicMock, capture: str
    ) -> None:
        """A leave reported quickly lets the play go out right after it."""
        controller, owner, member = self._captured_member(mock_mass, capture)
        events: list[str] = []
        self._confirm_release_after(controller, owner, member, 0.5, events)

        await self._play_media(controller, events)

        assert events == ["left", "play on member"]
        assert member.state.synced_to is None
        assert member.state.active_group is None

    @pytest.mark.parametrize("capture", ["synced", "dynamic", "static"])
    async def test_a_leave_reported_late_still_plays_on_the_released_player(
        self, mock_mass: MagicMock, capture: str
    ) -> None:
        """A leave reported as late as a Sonos speaker reports it through its own event."""
        controller, owner, member = self._captured_member(mock_mass, capture)
        events: list[str] = []
        self._confirm_release_after(controller, owner, member, 6.7, events)

        await self._play_media(controller, events)

        assert events == ["left", "play on member"]
        assert member.state.synced_to is None
        assert member.state.active_group is None
        # the bound of the server outlasts it
        assert self.BOUND > 6.7

    async def test_a_leave_never_reported_plays_at_the_bound(self, mock_mass: MagicMock) -> None:
        """Without a confirmation the play goes out once the wait ran out, for the player to judge."""
        controller, owner, member = self._captured_member(mock_mass, "synced")
        events: list[str] = []
        self._confirm_release_after(controller, owner, member, None, events)

        await self._play_media(controller, events)

        assert events == ["bound", "play on member"]
        assert member.state.synced_to == "owner"


class TestExternalSourcePlayPause:
    """Pause/play handling for externally-initiated sources (no active output protocol)."""

    @staticmethod
    def _make_external_source_player(
        provider: MockProvider,
        mock_mass: MagicMock,
        *,
        playback_state: PlaybackState,
        can_play_pause: bool = True,
        supports_pause: bool = True,
    ) -> MockProtocol:
        """Build a player playing a passive external source and return its protocol."""
        player, protocol = _attach(provider, "player_1", "Test Player")
        protocol._attr_supported_features = {PlayerFeature.PAUSE} if supports_pause else set()
        protocol._attr_source_list = [
            PlayerSource(
                id="spotify",
                name="Spotify",
                passive=True,
                can_play_pause=can_play_pause,
                can_next_previous=True,
                can_seek=True,
            )
        ]
        protocol._attr_active_source = "spotify"
        protocol._attr_playback_state = playback_state
        mock_mass.player_queues = MagicMock()
        mock_mass.player_queues.get = MagicMock(return_value=None)
        _refresh(player)
        return protocol

    def test_pause_external_source_forwards_to_player(
        self, mock_mass: MagicMock, controller: PlayerController, provider: MockProvider
    ) -> None:
        """Pausing a pausable external source forwards to the device, not STOP."""
        protocol = self._make_external_source_player(
            provider, mock_mass, playback_state=PlaybackState.PLAYING
        )
        protocol.pause = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(controller._handle_cmd_pause("player_1"))

        protocol.pause.assert_awaited_once()
        controller._handle_cmd_stop.assert_not_called()

    def test_play_external_source_unpauses_player(
        self, mock_mass: MagicMock, controller: PlayerController, provider: MockProvider
    ) -> None:
        """Unpausing a paused external source forwards to the device, not a restart."""
        protocol = self._make_external_source_player(
            provider, mock_mass, playback_state=PlaybackState.PAUSED
        )
        protocol.play = AsyncMock()  # type: ignore[method-assign]
        protocol.play_media = AsyncMock()  # type: ignore[method-assign]
        controller._handle_select_source = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(controller._handle_cmd_play("player_1"))

        protocol.play.assert_awaited_once()
        protocol.play_media.assert_not_called()
        controller._handle_select_source.assert_not_called()

    def test_pause_falls_back_to_stop_without_pause_support(
        self, mock_mass: MagicMock, controller: PlayerController, provider: MockProvider
    ) -> None:
        """A player that cannot pause natively still falls back to STOP."""
        protocol = self._make_external_source_player(
            provider,
            mock_mass,
            playback_state=PlaybackState.PLAYING,
            supports_pause=False,
        )
        protocol.pause = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]

        asyncio.run(controller._handle_cmd_pause("player_1"))

        controller._handle_cmd_stop.assert_awaited_once()
        protocol.pause.assert_not_called()


class TestPlayOnAPausedQueue:
    """Play on a player that is paused on its own queue."""

    @staticmethod
    def _paused_on_own_queue(
        mock_mass: MagicMock, controller: PlayerController, provider: MockProvider
    ) -> Player:
        """Build a player that supports pause and is paused on its own queue."""
        player, protocol = _attach(provider, "player_1", "Test Player")
        protocol._attr_supported_features = {PlayerFeature.PAUSE}
        protocol._attr_playback_state = PlaybackState.PAUSED
        mock_mass.player_queues = MagicMock()
        mock_mass.player_queues.get = MagicMock(return_value=MagicMock(queue_id="player_1"))
        mock_mass.player_queues.resume = AsyncMock()
        player.set_active_mass_source("player_1")
        _refresh(player)
        controller._handle_cmd_play = AsyncMock()  # type: ignore[method-assign]
        return player

    @pytest.mark.parametrize("stream_lost", [False, True], ids=["stream-kept", "stream-lost"])
    async def test_play_unpauses_unless_the_queue_ended_the_stream(
        self,
        mock_mass: MagicMock,
        controller: PlayerController,
        provider: MockProvider,
        stream_lost: bool,
    ) -> None:
        """A stream the queue ended while the player stayed paused is started again."""
        self._paused_on_own_queue(mock_mass, controller, provider)
        mock_mass.player_queues.has_lost_paused_stream = MagicMock(return_value=stream_lost)

        await controller.cmd_play("player_1")

        if stream_lost:
            mock_mass.player_queues.resume.assert_awaited_once_with("player_1")
            cast("AsyncMock", controller._handle_cmd_play).assert_not_awaited()
        else:
            cast("AsyncMock", controller._handle_cmd_play).assert_awaited_once_with("player_1")
            mock_mass.player_queues.resume.assert_not_awaited()


class TestProtocolOutputPlayPause:
    """Play/pause on a player rendering through one of its audio protocols."""

    @staticmethod
    def _make_player_on_protocol(
        mock_mass: MagicMock, *, playback_state: PlaybackState
    ) -> tuple[Player, MockProtocol]:
        """Build a player playing the MA queue through an audio protocol that cannot pause."""
        native_provider = MockProvider("chromecast", instance_id="chromecast", mass=mock_mass)
        player, native = _attach(native_provider, "player_1", "Test Player")
        native._attr_supported_features.add(PlayerFeature.PAUSE)
        native._attr_playback_state = playback_state

        audio_provider = MockProvider("sendspin", instance_id="sendspin", mass=mock_mass)
        audio = _attach_audio(audio_provider, player, "audio_1", "Test Player")
        audio._attr_playback_state = playback_state

        mock_mass.player_queues = MagicMock()
        # a non-empty queue, so the MA queue source advertises play/pause support
        queue = MagicMock()
        queue.items = [MagicMock()]
        queue.queue_id = "player_1"
        mock_mass.player_queues.get = MagicMock(return_value=queue)
        player.set_active_output_protocol("audio_1")
        player.set_active_mass_source("player_1")
        _refresh(player)
        return player, native

    async def test_pause_on_protocol_without_pause_ends_the_queue(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """
        The native transport has no session to pause while a protocol renders the audio.

        The pause falls back to a stop of the player's own queue, which stops the player.
        """
        _player, native = self._make_player_on_protocol(
            mock_mass, playback_state=PlaybackState.PLAYING
        )
        native.pause = AsyncMock()  # type: ignore[method-assign]
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        mock_mass.player_queues._handle_stop = AsyncMock()

        await controller._handle_cmd_pause("player_1")

        native.pause.assert_not_called()
        mock_mass.player_queues._handle_stop.assert_awaited_once_with("player_1")
        # the queue stop issues the player stop itself
        controller._handle_cmd_stop.assert_not_awaited()

    async def test_play_on_protocol_without_pause_does_not_unpause_natively(
        self, mock_mass: MagicMock, controller: PlayerController
    ) -> None:
        """Unpausing must not hit the native transport either; the source is restarted."""
        _player, native = self._make_player_on_protocol(
            mock_mass, playback_state=PlaybackState.PAUSED
        )
        native.play = AsyncMock()  # type: ignore[method-assign]
        controller._handle_select_source = AsyncMock()  # type: ignore[method-assign]

        await controller._handle_cmd_play("player_1")

        native.play.assert_not_called()
        # the MA queue source is restarted, not some other source
        controller._handle_select_source.assert_awaited_once_with("player_1", "player_1")


class TestPauseWithoutPauseSupport:
    """
    Pause on a player that plays its own queue and can not pause.

    The pause falls back to a stop of the queue, which releases the queue's audio and the
    music source right away. A later play resumes the queue at the paused position.
    """

    @staticmethod
    def _player_on_own_queue(
        mock_mass: MagicMock, player_type: PlayerType = PlayerType.PLAYER
    ) -> tuple[PlayerController, Player]:
        """
        Build a playing player without pause support that plays its own queue.

        :param mock_mass: The mock MusicAssistant instance to build it on.
        :param player_type: The type to register the player as.
        :return: The controller and the player, with the player and queue stop stubbed.
        """
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1", player_type=player_type)
        protocol._attr_playback_state = PlaybackState.PLAYING
        queue = _stub_queue("player_1")
        # a queue with items advertises play/pause on its source
        queue.items = 1
        mock_mass.player_queues.get = MagicMock(
            side_effect=lambda queue_id: queue if queue_id == "player_1" else None
        )
        _refresh(player)
        controller._handle_cmd_stop = AsyncMock()  # type: ignore[method-assign]
        mock_mass.player_queues._handle_stop = AsyncMock()
        return controller, player

    async def test_pause_on_a_group_player_ends_its_queue(self, mock_mass: MagicMock) -> None:
        """A group player (sync or universal group) can not pause either."""
        controller, _player = self._player_on_own_queue(mock_mass, PlayerType.GROUP)

        await controller._handle_cmd_pause("player_1")

        mock_mass.player_queues._handle_stop.assert_awaited_once_with("player_1")
        cast("AsyncMock", controller._handle_cmd_stop).assert_not_awaited()


class TestMirrorsParentMedia:
    """Tests for _mirrors_parent_media (palette-fetch gating for grouped players)."""

    @staticmethod
    def _fake_player(
        *,
        player_id: str = "p1",
        active_group: str | None = None,
        synced_to: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            player_id=player_id,
            state=SimpleNamespace(active_group=active_group, synced_to=synced_to),
        )

    def test_standalone_player_owns_media(self, controller: PlayerController) -> None:
        """A standalone player resolves its own media (and palette)."""
        assert controller._mirrors_parent_media(self._fake_player()) is False  # type: ignore[arg-type]

    def test_group_member_mirrors(self, controller: PlayerController) -> None:
        """A group member borrows its parent's media."""
        assert controller._mirrors_parent_media(self._fake_player(active_group="g1")) is True  # type: ignore[arg-type]

    def test_synced_member_mirrors(self, controller: PlayerController) -> None:
        """A synced member borrows its leader's media."""
        assert controller._mirrors_parent_media(self._fake_player(synced_to="leader")) is True  # type: ignore[arg-type]

    def test_self_referential_parent_owns_media(self, controller: PlayerController) -> None:
        """A self-referential active_group/synced_to is not a real parent, so resolve locally."""
        player = self._fake_player(player_id="p1", synced_to="p1", active_group="p1")
        assert controller._mirrors_parent_media(player) is False  # type: ignore[arg-type]


class TestExternalPowerControl:
    """Power commands redirected to an external PlayerControl must forward and gate correctly."""

    def _make_player(
        self, mock_mass: MagicMock, control: PlayerControl
    ) -> tuple[PlayerController, Player]:
        """Build a controller with a single player whose power control is the given control."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_POWER_CONTROL: control.id})
        )
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, _ = _attach(provider, "player_1", "Player 1")
        controller._controls = {control.id: control}
        # auto-play would otherwise resume the (mocked) player queue on power on
        _set_config_value(player, CONF_AUTO_PLAY, False)
        _refresh(player)
        return controller, player

    async def test_power_on_forwards_to_control(self, mock_mass: MagicMock) -> None:
        """Powering on a player redirects to its external control's power_on callback."""
        power_on = AsyncMock()
        power_off = AsyncMock()
        control = PlayerControl(
            id="ext_power",
            provider="test",
            name="External Power",
            supports_power=True,
            power_on=power_on,
            power_off=power_off,
        )

        def _report_powered_on() -> None:
            control.power_state = True

        # the control only reports on once switched on, which releases wait_for_power_on
        power_on.side_effect = _report_powered_on
        controller, player = self._make_player(mock_mass, control)
        assert player.state.powered is False

        await controller._handle_cmd_power("player_1", True)

        power_on.assert_awaited_once()
        power_off.assert_not_awaited()

    async def test_power_on_waits_on_the_control(self, mock_mass: MagicMock) -> None:
        """Powering on waits for the control to report on, not for the player itself."""
        control = PlayerControl(
            id="ext_power",
            provider="test",
            name="External Power",
            supports_power=True,
            power_on=AsyncMock(),
            power_off=AsyncMock(),
        )
        controller, player = self._make_player(mock_mass, control)
        assert player.state.powered is False

        with patch(
            "music_assistant.controllers.players.power.wait_for_power_on", AsyncMock()
        ) as wait_for_power_on:
            await controller._handle_cmd_power("player_1", True)

        wait_for_power_on.assert_awaited_once()
        assert wait_for_power_on.await_args is not None
        assert wait_for_power_on.await_args.args[2] is control

    async def test_power_off_forwards_to_control(self, mock_mass: MagicMock) -> None:
        """Powering off a player redirects to its external control's power_off callback."""
        power_on = AsyncMock()
        power_off = AsyncMock()
        control = PlayerControl(
            id="ext_power",
            provider="test",
            name="External Power",
            supports_power=True,
            power_state=True,
            power_on=power_on,
            power_off=power_off,
        )
        controller, player = self._make_player(mock_mass, control)
        assert player.state.powered is True

        await controller._handle_cmd_power("player_1", False)

        power_off.assert_awaited_once()
        power_on.assert_not_awaited()

    async def test_control_without_power_support_raises(self, mock_mass: MagicMock) -> None:
        """A power command redirected to a control lacking power support is rejected."""
        power_on = AsyncMock()
        power_off = AsyncMock()
        control = PlayerControl(
            id="ext_power",
            provider="test",
            name="External Power",
            supports_power=False,
            power_on=power_on,
            power_off=power_off,
        )
        controller, player = self._make_player(mock_mass, control)
        assert player.state.powered is False

        with pytest.raises(UnsupportedFeaturedException):
            await controller._handle_cmd_power("player_1", True)

        power_on.assert_not_awaited()
        power_off.assert_not_awaited()


class TestOfflinePowerControl:
    """A player switched off at its external power control is powered on through that control."""

    @pytest.fixture(autouse=True)
    async def _virtual_clock(self) -> AsyncIterator[None]:
        # the device comes online, and a wait for it gives up, at a time of the clock rather
        # than after a real wait: how fast the machine is does not change what happens first
        with virtual_loop_clock():
            yield

    def _setup(
        self,
        mock_mass: MagicMock,
        *,
        auto_play: bool = False,
        comes_online: bool = True,
    ) -> tuple[PlayerController, Player, MockProtocol, PlayerControl]:
        """
        Build a controller with an offline player behind a switched off external control.

        :param mock_mass: The mocked MusicAssistant instance to attach the controller to.
        :param auto_play: The player's 'auto play on power on' setting.
        :param comes_online: Whether the device comes online once its control is switched on.
        :return: The controller, the player, its protocol and the control.
        """
        control = PlayerControl(
            id="switch.smart_plug",
            provider="hass",
            name="Smart plug",
            supports_power=True,
            power_on=AsyncMock(),
            power_off=AsyncMock(),
        )
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_POWER_CONTROL: control.id})
        )
        mock_mass.player_queues.resume = AsyncMock()
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        protocol._attr_available = False
        controller._controls = {control.id: control}
        _set_config_value(player, CONF_AUTO_PLAY, auto_play)
        _refresh(player)

        def _switch_on() -> None:
            control.power_state = True
            # the control reports its new state while the device is still starting up
            player.refresh_state()
            if comes_online:
                # the device boots and reconnects a moment after its power came on
                asyncio.get_running_loop().call_later(
                    0.05, setattr, protocol, "_attr_available", True
                )

        cast("AsyncMock", control.power_on).side_effect = _switch_on
        return controller, player, protocol, control

    async def test_power_on_waits_until_the_device_is_online(self, mock_mass: MagicMock) -> None:
        """Powering on switches the control on and returns once the device is reachable."""
        controller, player, _protocol, control = self._setup(mock_mass)
        assert player.state.available is True
        assert player.state.powered is False
        power_on = cast("AsyncMock", control.power_on)
        switch_on = power_on.side_effect
        state_while_starting: list[tuple[bool, bool | None]] = []

        def _record_state() -> None:
            switch_on()
            state_while_starting.append((player.state.available, player.state.powered))

        power_on.side_effect = _record_state

        await controller.cmd_power("player_1", True)

        power_on.assert_awaited_once()
        # it does not drop out of sight while it starts up
        assert state_while_starting == [(True, True)]
        assert player._reachable is True
        assert not player.power_on_in_progress
        assert player.state.available is True
        assert player.state.powered is True

    async def test_a_control_reporting_on_after_the_device_is_online_is_waited_for(
        self, mock_mass: MagicMock
    ) -> None:
        """A device that is online before its control reports on is returned as powered."""
        controller, _player, protocol, control = self._setup(mock_mass)

        def _switch_on_slow_report() -> None:
            loop = asyncio.get_running_loop()
            loop.call_later(0.01, setattr, protocol, "_attr_available", True)
            loop.call_later(0.8, setattr, control, "power_state", True)

        cast("AsyncMock", control.power_on).side_effect = _switch_on_slow_report

        online_player = await controller._power_on_switched_off_player("player_1")

        assert online_player._reachable is True
        assert control.power_state is True
        online_player.update_state(signal_event=False)
        assert online_player.state.powered is True

    async def test_auto_play_runs_once_the_device_is_online(self, mock_mass: MagicMock) -> None:
        """The 'auto play on power on' setting resumes the queue on the reachable device."""
        controller, player, _protocol, _control = self._setup(mock_mass, auto_play=True)
        online_at_resume: list[bool] = []
        resume = cast("AsyncMock", mock_mass.player_queues.resume)
        resume.side_effect = lambda _queue_id: online_at_resume.append(player._reachable)

        await controller.cmd_power("player_1", True)

        resume.assert_awaited_once_with("player_1")
        assert online_at_resume == [True]

    async def test_power_on_follows_a_new_object_of_the_returning_device(
        self, mock_mass: MagicMock
    ) -> None:
        """A provider registering a new protocol object for the returning device ends the wait."""
        controller, player, protocol, control = self._setup(mock_mass, comes_online=False)
        provider = cast("MockProvider", protocol.provider)

        def _reattach() -> None:
            # a reconnect: another object of the same protocol takes the place of the old one
            attach_mock(provider, _proto("player_1"), "Player 1", player=player)

        def _switch_on() -> None:
            control.power_state = True
            asyncio.get_running_loop().call_later(0.05, _reattach)

        cast("AsyncMock", control.power_on).side_effect = _switch_on

        await controller.cmd_power("player_1", True)

        assert not player.power_on_in_progress
        assert player.attached_protocols[_proto("player_1")] is not protocol
        assert player.state.available is True
        assert player.state.powered is True

    async def test_power_on_times_out_when_the_device_stays_offline(
        self, mock_mass: MagicMock
    ) -> None:
        """A device that does not come online fails the power on and is unavailable again."""
        controller, player, _protocol, _control = self._setup(mock_mass, comes_online=False)

        with (
            patch.object(players_power, "OFFLINE_POWER_ON_TIMEOUT", 0.05),
            pytest.raises(PlayerUnavailableError),
        ):
            await controller.cmd_power("player_1", True)

        assert not player.power_on_in_progress
        # its control is on while the device is still unreachable: a real fault
        assert player.state.available is False

    async def test_power_off_of_a_switched_off_player_is_a_no_op(
        self, mock_mass: MagicMock
    ) -> None:
        """Powering off a player that is already switched off neither raises nor switches."""
        controller, _player, _protocol, control = self._setup(mock_mass)

        await controller.cmd_power("player_1", False)

        cast("AsyncMock", control.power_off).assert_not_awaited()

    @pytest.mark.parametrize(
        ("command", "handler", "argument"),
        [
            ("play_media", "_handle_play_media", PlayerMedia(uri="http://ma/stream.flac")),
            ("select_source", "_handle_select_source", "spotify_connect"),
        ],
    )
    async def test_playback_command_powers_the_player_on_first(
        self, mock_mass: MagicMock, command: str, handler: str, argument: object
    ) -> None:
        """A command that starts playback only reaches its handler once the device is online."""
        controller, player, _protocol, control = self._setup(mock_mass, auto_play=True)
        online_at_handler: list[bool] = []
        setattr(
            controller,
            handler,
            AsyncMock(side_effect=lambda *_args: online_at_handler.append(player._reachable)),
        )

        await getattr(controller, command)("player_1", argument)

        cast("AsyncMock", control.power_on).assert_awaited_once()
        assert online_at_handler == [True]
        # the command plays what it was asked to, so the queue is not resumed on top of it
        cast("AsyncMock", mock_mass.player_queues.resume).assert_not_awaited()

    async def test_concurrent_power_on_switches_the_control_once(
        self, mock_mass: MagicMock
    ) -> None:
        """A playback command during a running power on waits for it instead of switching again."""
        controller, player, _protocol, control = self._setup(mock_mass)
        online_at_handler: list[bool] = []
        for handler in ("_handle_play_media", "_handle_select_source"):
            setattr(
                controller,
                handler,
                AsyncMock(side_effect=lambda *_args: online_at_handler.append(player._reachable)),
            )

        await asyncio.gather(
            controller.play_media("player_1", PlayerMedia(uri="http://ma/stream.flac")),
            controller.select_source("player_1", "spotify_connect"),
        )

        cast("AsyncMock", control.power_on).assert_awaited_once()
        assert online_at_handler == [True, True]

    async def test_overlapping_power_ons_resume_the_queue_once(self, mock_mass: MagicMock) -> None:
        """A power on that joins one under way leaves the auto play to the one that started it."""
        controller, _player, _protocol, control = self._setup(mock_mass, auto_play=True)

        await asyncio.gather(
            controller.cmd_power("player_1", True),
            controller.cmd_power("player_1", True),
        )

        cast("AsyncMock", control.power_on).assert_awaited_once()
        cast("AsyncMock", mock_mass.player_queues.resume).assert_awaited_once_with("player_1")

    @pytest.mark.parametrize(
        ("command", "args"),
        [
            ("cmd_stop", ()),
            ("cmd_pause", ()),
            ("cmd_volume_set", (50,)),
            ("cmd_volume_up", ()),
            ("cmd_volume_down", ()),
            ("cmd_volume_mute", (True,)),
            ("cmd_group_volume", (50,)),
            ("cmd_group_volume_up", ()),
            ("cmd_group_volume_down", ()),
            ("cmd_group_volume_mute", (True,)),
            ("cmd_ungroup", ()),
        ],
    )
    async def test_commands_without_anything_to_act_on_are_ignored(
        self, mock_mass: MagicMock, command: str, args: tuple[object, ...]
    ) -> None:
        """Stop, (group) volume and ungroup quietly do nothing on a switched off player."""
        controller, player, _protocol, control = self._setup(mock_mass)
        untouched = {
            name: AsyncMock()
            for name in (
                "_handle_cmd_stop",
                "_handle_cmd_pause",
                "_handle_cmd_volume_set",
                "_handle_cmd_volume_mute",
                "cmd_set_members",
                "_detach_member",
            )
        }
        for name, mock in untouched.items():
            setattr(controller, name, mock)
        controller._record_volume_target = MagicMock()  # type: ignore[method-assign]
        controller.get_active_queue = MagicMock()  # type: ignore[method-assign]

        assert await getattr(controller, command)("player_1", *args) is None

        for mock in untouched.values():
            mock.assert_not_awaited()
        controller._record_volume_target.assert_not_called()
        controller.get_active_queue.assert_not_called()
        cast("AsyncMock", control.power_on).assert_not_awaited()
        assert player.state.available is True

    async def test_other_commands_are_refused(self, mock_mass: MagicMock) -> None:
        """A command that neither starts playback nor can be skipped is refused."""
        controller, _player, _protocol, control = self._setup(mock_mass)

        with pytest.raises(PlayerUnavailableError):
            await controller.cmd_next_track("player_1")
        with pytest.raises(PlayerUnavailableError):
            await controller.cmd_set_members("player_1", player_ids_to_add=["other"])

        cast("AsyncMock", control.power_on).assert_not_awaited()

    async def test_switched_off_player_is_not_added_to_a_group(
        self, mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Grouping skips a member that is only available through its power control."""
        controller, player, protocol, _control = self._setup(mock_mass)
        leader, leader_protocol = _attach(
            cast("MockProvider", protocol.provider), "leader", "Leader"
        )
        leader_protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        leader_protocol._attr_can_group_with = {_proto("player_1")}
        _refresh(leader)
        assert player.state.available is True

        with caplog.at_level(logging.WARNING):
            await controller.cmd_set_members("leader", player_ids_to_add=["player_1"])

        assert _proto("player_1") not in leader_protocol.group_members
        assert "Player player_1 is not available" in caplog.text

    def test_going_offline_with_its_control_detaches_a_synced_player(
        self, mock_mass: MagicMock
    ) -> None:
        """A synced player that goes offline along with its control is detached directly."""
        controller, player, protocol, control = self._setup(mock_mass)
        control.power_state = True
        protocol._attr_available = True
        leader, leader_protocol = _attach(
            cast("MockProvider", protocol.provider), "leader", "Leader"
        )
        leader_protocol._attr_group_members = [_proto("leader"), _proto("player_1")]
        _refresh(leader, player)
        assert player.state.synced_to == "leader"
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        controller.cmd_ungroup = MagicMock(return_value="ungroup-coro")  # type: ignore[method-assign]
        controller._cleanup_player_memberships = MagicMock(  # type: ignore[method-assign]
            return_value="cleanup-coro"
        )
        # the smart plug is switched off, which takes the device down with it
        control.power_state = False
        protocol._attr_available = False

        controller.signal_player_state_update(player, {"powered": (True, False)})

        controller._cleanup_player_memberships.assert_called_once_with("player_1")
        controller.cmd_ungroup.assert_not_called()

    @pytest.mark.parametrize("command", ["cmd_power", "play_media", "play_announcement"])
    async def test_no_lock_is_held_while_the_device_comes_online(
        self, mock_mass: MagicMock, command: str
    ) -> None:
        """Other commands get the player locks at once while a power on waits for the device."""
        controller, player, protocol, control = self._setup(mock_mass, comes_online=False)
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        controller._run_announcement = AsyncMock()  # type: ignore[method-assign]
        arguments: dict[str, tuple[object, ...]] = {
            "cmd_power": (True,),
            "play_media": (PlayerMedia(uri="http://ma/stream.flac"),),
            "play_announcement": ("http://test/announcement.mp3",),
        }
        pending = asyncio.create_task(getattr(controller, command)("player_1", *arguments[command]))
        await asyncio.sleep(0.1)
        assert player.power_on_in_progress
        waits = record_lock_waits(controller)

        async with controller.get_group_and_player_lock("player_1"):
            pass

        assert waits == []

        protocol._attr_available = True
        await pending
        cast("AsyncMock", control.power_on).assert_awaited_once()

    async def test_power_off_while_starting_up_aborts_the_pending_play(
        self, mock_mass: MagicMock
    ) -> None:
        """Switching a starting player off again ends its power on, and the play it was for."""
        controller, player, _protocol, control = self._setup(mock_mass, comes_online=False)
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]

        def _switch_off() -> None:
            control.power_state = False
            player.refresh_state()

        cast("AsyncMock", control.power_off).side_effect = _switch_off
        pending = asyncio.create_task(
            controller.play_media("player_1", PlayerMedia(uri="http://ma/stream.flac"))
        )
        await asyncio.sleep(0.1)
        loop = asyncio.get_running_loop()
        started = loop.time()

        async with asyncio.timeout(0.2):
            await controller.cmd_power("player_1", False)
        with pytest.raises(PlayerUnavailableError, match="switched off"):
            await pending

        cast("AsyncMock", control.power_off).assert_awaited_once()
        assert loop.time() - started < 1.5
        controller._handle_play_media.assert_not_awaited()
        assert not player.power_on_in_progress
        assert player.state.available is True
        assert player.state.powered is False

    async def test_power_off_before_the_control_reported_on_aborts_the_pending_play(
        self, mock_mass: MagicMock
    ) -> None:
        """A power off is not dropped as 'already off' while the control has not reported on."""
        controller, player, _protocol, control = self._setup(mock_mass, comes_online=False)
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        # the switch is on, but it has not reported its new state yet
        cast("AsyncMock", control.power_on).side_effect = None
        pending = asyncio.create_task(
            controller.play_media("player_1", PlayerMedia(uri="http://ma/stream.flac"))
        )
        await asyncio.sleep(0.1)
        assert player.state.powered is False
        loop = asyncio.get_running_loop()
        started = loop.time()

        await controller.cmd_power("player_1", False)
        with pytest.raises(PlayerUnavailableError, match="switched off"):
            await pending

        cast("AsyncMock", control.power_off).assert_awaited_once()
        assert loop.time() - started < 1.5
        controller._handle_play_media.assert_not_awaited()
        assert not player.power_on_in_progress

    @pytest.mark.parametrize("command", ["cmd_stop", "cmd_pause"])
    async def test_stop_or_pause_while_starting_up_aborts_the_pending_play(
        self, mock_mass: MagicMock, command: str
    ) -> None:
        """A stop or pause calls off the power on of a starting player, and the play it was for."""
        controller, player, _protocol, control = self._setup(mock_mass, comes_online=False)
        controller._handle_play_media = AsyncMock()  # type: ignore[method-assign]
        pending = asyncio.create_task(
            controller.play_media("player_1", PlayerMedia(uri="http://ma/stream.flac"))
        )
        await asyncio.sleep(0.1)
        assert player.power_on_in_progress

        await getattr(controller, command)("player_1")
        async with asyncio.timeout(1.5):
            with pytest.raises(PlayerUnavailableError, match="switched off"):
                await pending

        controller._handle_play_media.assert_not_awaited()
        # the control is left on, unlike with a power off
        cast("AsyncMock", control.power_off).assert_not_awaited()
        assert control.power_state is True
        assert not player.power_on_in_progress

    @pytest.mark.parametrize("published", ["another control", "no control"])
    async def test_power_on_inside_a_change_of_the_power_control_switches_the_new_control(
        self, mock_mass: MagicMock, published: str
    ) -> None:
        """A power on that lands while a new power control is being applied switches that one."""
        controller, player, protocol, control = self._setup(mock_mass)
        get_player_value = mock_mass.config.get_player_value
        stored_with_control = get_player_value.side_effect
        other = PlayerControl(
            id="switch.other_plug",
            provider="hass",
            name="Other plug",
            supports_power=True,
            power_on=AsyncMock(),
        )
        if published == "another control":
            controller._controls[other.id] = other
            get_player_value.side_effect = _player_config_stub({CONF_POWER_CONTROL: other.id})
            _refresh(player)
        else:
            # published while its device could be reached, which then dropped off
            get_player_value.side_effect = _player_config_stub()
            protocol._attr_available = True
            _refresh(player)
            protocol._attr_available = False
        # the new control is stored and the player has its config, it has not published yet
        get_player_value.side_effect = stored_with_control
        player.set_config(player.config)
        assert player.power_control != control.id
        assert player.offline_by_power_control

        await controller._power_on_switched_off_player("player_1")

        cast("AsyncMock", control.power_on).assert_awaited_once()
        cast("AsyncMock", other.power_on).assert_not_awaited()
        assert player.power_control == control.id
        assert player._reachable

    async def test_a_user_without_access_can_not_switch_it_on(self, mock_mass: MagicMock) -> None:
        """Only a user with access to the player can switch it on."""
        controller, _player, _protocol, control = self._setup(mock_mass)

        with _restricted_user(["kitchen"]), pytest.raises(InsufficientPermissions):
            await controller._power_on_switched_off_player("player_1")

        cast("AsyncMock", control.power_on).assert_not_awaited()

    @pytest.mark.parametrize("action", ["resume", "play_index"])
    async def test_a_queue_action_by_a_user_without_access_does_not_switch_it_on(
        self, mock_mass: MagicMock, action: str
    ) -> None:
        """A queue action that powers the player on is refused before the control is switched."""
        _ctrl, _player, _protocol, control = self._setup(mock_mass)
        queues: Any = PlayerQueuesController.__new__(PlayerQueuesController)
        queues.mass = mock_mass
        queues._queue_data = {"player_1": MagicMock()}
        args = (0,) if action == "play_index" else ()

        with _restricted_user(["kitchen"]), pytest.raises(InsufficientPermissions):
            await getattr(queues, action)("player_1", *args)

        cast("AsyncMock", control.power_on).assert_not_awaited()

    @pytest.mark.parametrize("action", ["stop", "pause"])
    async def test_a_queue_stop_or_pause_while_starting_up_aborts_the_pending_play(
        self, mock_mass: MagicMock, action: str
    ) -> None:
        """A queue stop or pause calls off the power on of a starting player, and its play."""
        _ctrl, player, _protocol, control = self._setup(mock_mass, comes_online=False)
        queues: Any = PlayerQueuesController.__new__(PlayerQueuesController)
        queues.mass = mock_mass
        queue = cast("PlayerQueue", MagicMock(extra_attributes={}))
        queues._queue_data = {"player_1": PlayerQueueData(queue=queue)}
        queues.signal_update = MagicMock()
        queues.on_player_update = MagicMock()
        queues.play_index = AsyncMock()
        pending = asyncio.create_task(queues.resume("player_1"))
        await asyncio.sleep(0.1)
        assert player.power_on_in_progress

        await getattr(queues, action)("player_1")
        async with asyncio.timeout(1.5):
            with pytest.raises(PlayerUnavailableError, match="switched off"):
                await pending

        queues.play_index.assert_not_awaited()
        cast("AsyncMock", control.power_off).assert_not_awaited()
        assert control.power_state is True
        assert not player.power_on_in_progress

    async def test_a_control_reporting_off_after_the_call_keeps_waiting(
        self, mock_mass: MagicMock
    ) -> None:
        """A control that has not reported on yet is still on its way, not switched off again."""
        controller, player, protocol, control = self._setup(mock_mass, comes_online=False)

        def _slow_report() -> None:
            # the switch is on, its new state is only reported a moment later
            loop = asyncio.get_running_loop()
            loop.call_later(0.8, setattr, control, "power_state", True)
            loop.call_later(0.8, setattr, protocol, "_attr_available", True)

        cast("AsyncMock", control.power_on).side_effect = _slow_report

        await controller.cmd_power("player_1", True)

        assert player._reachable is True

    async def test_play_pause_on_a_player_cut_off_mid_song_powers_it_on_and_plays(
        self, mock_mass: MagicMock
    ) -> None:
        """A device that still reported playing when it lost power is played, not paused."""
        controller, player, protocol, control = self._setup(mock_mass, comes_online=False)
        protocol._attr_playback_state = PlaybackState.PLAYING
        _refresh(player)
        assert player.state.playback_state == PlaybackState.IDLE
        controller._handle_cmd_pause = AsyncMock()  # type: ignore[method-assign]

        def _switch_on() -> None:
            control.power_state = True
            # the device boots fresh and reports what it is doing now
            protocol._attr_playback_state = PlaybackState.IDLE
            asyncio.get_running_loop().call_later(0.05, setattr, protocol, "_attr_available", True)

        cast("AsyncMock", control.power_on).side_effect = _switch_on

        await controller.cmd_play_pause("player_1")

        cast("AsyncMock", control.power_on).assert_awaited_once()
        cast("AsyncMock", mock_mass.player_queues.resume).assert_awaited_once()
        controller._handle_cmd_pause.assert_not_awaited()

    def test_reachability_change_refreshes_the_players_of_the_provider(
        self, mock_mass: MagicMock
    ) -> None:
        """A device dropping out keeps its availability, but its peers still recalculate."""
        controller, player, protocol, _control = self._setup(mock_mass)
        protocol._attr_available = True
        _refresh(player)
        _attach(cast("MockProvider", protocol.provider), "peer", "Peer")
        controller.trigger_player_update = MagicMock()  # type: ignore[method-assign]
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]

        protocol._attr_available = False
        player.update_state()

        assert player.state.available is True
        controller.trigger_player_update.assert_any_call("peer", debounce_delay=2)

    def _announce_through_the_default_flow(
        self, controller: PlayerController, mock_mass: MagicMock, steps: list[str]
    ) -> None:
        """
        Let announcements take the default flow, recording when the clip gets played.

        :param controller: The controller the announcements are played on.
        :param mock_mass: The mocked MusicAssistant instance the controller belongs to.
        :param steps: Filled with what happened, in order.
        """
        render = MagicMock()
        render.wait_ready = AsyncMock(return_value=True)
        render.wait_finished = AsyncMock(return_value=3.0)
        renderer = mock_mass.streams.announcement_renderer
        renderer.register = MagicMock(return_value=render)
        renderer.get = MagicMock(return_value=render)
        renderer.unregister = AsyncMock()
        mock_mass.streams.get_announcement_url = MagicMock(
            return_value="http://ma/announcement/player_1.mp3"
        )
        # an idle, empty queue: nothing shown whose palette would be fetched
        mock_mass.player_queues.get = MagicMock(return_value=_stub_queue("player_1"))
        controller._resolve_ready_announce_player = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )
        controller._wait_for_playback_state = AsyncMock()  # type: ignore[method-assign]
        controller.get_announcement_volume = MagicMock(return_value=None)  # type: ignore[method-assign]
        player = controller._players["player_1"]
        controller._handle_play_media = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda *_args: steps.append(f"play (online: {player._reachable})")
        )

    @pytest.mark.usefixtures("running_background_tasks")
    async def test_announcement_powers_the_player_on_and_leaves_it_on(
        self, mock_mass: MagicMock
    ) -> None:
        """A switched off player is powered on for an announcement and then stays on."""
        controller, player, _protocol, control = self._setup(mock_mass)
        steps: list[str] = []
        self._announce_through_the_default_flow(controller, mock_mass, steps)
        power_on = cast("AsyncMock", control.power_on)
        switch_on = power_on.side_effect

        def _record_switch_on() -> None:
            steps.append("switch on")
            switch_on()

        power_on.side_effect = _record_switch_on
        cast("AsyncMock", control.power_off).side_effect = lambda: steps.append("switch off")

        await controller.play_announcement("player_1", "http://test/announcement.mp3")

        # the default flow restores the power state it found, which is on by then
        assert steps == ["switch on", "play (online: True)"]
        assert not player.power_on_in_progress
        assert player.state.powered is True

    async def test_announcement_power_on_timeout_leaves_the_control_alone(
        self, mock_mass: MagicMock
    ) -> None:
        """A device that does not come online fails the announcement without switching back."""
        controller, _player, _protocol, control = self._setup(mock_mass, comes_online=False)
        controller._run_announcement = AsyncMock()  # type: ignore[method-assign]

        with (
            patch.object(players_power, "OFFLINE_POWER_ON_TIMEOUT", 0.05),
            pytest.raises(PlayerUnavailableError),
        ):
            await controller.play_announcement("player_1", "http://test/announcement.mp3")

        cast("AsyncMock", control.power_on).assert_awaited_once()
        controller._run_announcement.assert_not_awaited()
        cast("AsyncMock", control.power_off).assert_not_awaited()


class TestIdlePowerOff:
    """A player with an idle power off time is switched off once it has been stopped that long."""

    def _setup(
        self, mock_mass: MagicMock, *, minutes: int = 30
    ) -> tuple[PlayerController, Player, MockProtocol, AsyncMock]:
        """
        Build a controller with a stopped player that is switched on at its external control.

        :param mock_mass: The mocked MusicAssistant instance to attach the controller to.
        :param minutes: The player's idle power off time.
        :return: The controller, the player, its protocol and the power off callback of its
            control.
        """
        power_off = AsyncMock()
        control = PlayerControl(
            id="switch.smart_plug",
            provider="hass",
            name="Smart plug",
            supports_power=True,
            power_state=True,
            power_on=AsyncMock(),
            power_off=power_off,
        )
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_POWER_CONTROL: control.id})
        )
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        controller._controls = {control.id: control}
        _set_config_value(player, CONF_IDLE_POWER_OFF, minutes)
        _refresh(player)
        assert player.state.powered is True
        assert player.state.playback_state == PlaybackState.IDLE
        return controller, player, protocol, power_off

    async def test_powers_off_once_stopped_for_the_configured_time(
        self, mock_mass: MagicMock
    ) -> None:
        """The power off follows the configured number of minutes after the player stopped."""
        controller, player, _protocol, power_off = self._setup(mock_mass)

        controller._check_idle_power_off(player)
        # the count starts when the player is first seen stopped
        assert ATTR_IDLE_SINCE in player.extra_data
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60 + 5
        controller._check_idle_power_off(player)
        mock_mass.create_task.assert_not_called()

        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60
        controller._check_idle_power_off(player)
        mock_mass.create_task.assert_called_once()
        await mock_mass.create_task.call_args.args[0]

        power_off.assert_awaited_once()
        # a power off that does not come through is tried again after a full idle time
        assert ATTR_IDLE_SINCE not in player.extra_data

    @pytest.mark.parametrize("playback_state", [PlaybackState.PLAYING, PlaybackState.PAUSED])
    def test_playback_starts_the_count_over(
        self, mock_mass: MagicMock, playback_state: PlaybackState
    ) -> None:
        """A playing or paused player is not idle, so its count restarts once it stops."""
        controller, player, protocol, _power_off = self._setup(mock_mass)
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60
        protocol._attr_playback_state = playback_state
        _refresh(player)

        controller._check_idle_power_off(player)

        assert ATTR_IDLE_SINCE not in player.extra_data
        mock_mass.create_task.assert_not_called()

    @pytest.mark.parametrize(
        ("minutes", "attribute", "value"),
        [
            pytest.param(0, None, None, id="not_configured"),
            pytest.param(30, "_attr_available", False, id="unavailable"),
            pytest.param(30, ATTR_ANNOUNCEMENT_IN_PROGRESS, True, id="announcing"),
        ],
    )
    def test_player_that_is_not_idle_is_left_on(
        self, mock_mass: MagicMock, minutes: int, attribute: str | None, value: bool | None
    ) -> None:
        """No idle power off without the setting, while unavailable or about to announce."""
        controller, player, protocol, _power_off = self._setup(mock_mass, minutes=minutes)
        if attribute == ATTR_ANNOUNCEMENT_IN_PROGRESS:
            player.extra_data[attribute] = value
        elif attribute:
            setattr(protocol, attribute, value)
            _refresh(player)
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60

        controller._check_idle_power_off(player)

        assert ATTR_IDLE_SINCE not in player.extra_data
        mock_mass.create_task.assert_not_called()

    def test_player_switched_off_elsewhere_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A player that is already powered off has nothing to count down."""
        controller, player, _protocol, _power_off = self._setup(mock_mass)
        controller._controls["switch.smart_plug"].power_state = False
        player.refresh_state(signal_event=False)
        assert player.state.powered is False
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60

        controller._check_idle_power_off(player)

        assert ATTR_IDLE_SINCE not in player.extra_data
        mock_mass.create_task.assert_not_called()

    async def test_playback_started_while_waiting_for_the_lock_is_left_alone(
        self, mock_mass: MagicMock
    ) -> None:
        """A play command that holds the player when its time is up keeps the player on."""
        controller, player, protocol, power_off = self._setup(mock_mass)
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60
        controller._check_idle_power_off(player)

        async with _lock_held_elsewhere(controller, "player_1"):
            power_off_task = asyncio.create_task(mock_mass.create_task.call_args.args[0])
            await _yield_to_loop()
            protocol._attr_playback_state = PlaybackState.PLAYING
            _refresh(player)
        await power_off_task

        power_off.assert_not_awaited()

    async def test_playback_that_ends_while_waiting_for_the_lock_starts_the_count_over(
        self, mock_mass: MagicMock
    ) -> None:
        """An announcement that holds the player when its time is up delays the power off."""
        controller, player, _protocol, power_off = self._setup(mock_mass)
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60
        controller._check_idle_power_off(player)

        async with _lock_held_elsewhere(controller, "player_1"):
            power_off_task = asyncio.create_task(mock_mass.create_task.call_args.args[0])
            await _yield_to_loop()
            controller.signal_player_state_update(
                player, {"playback_state": (PlaybackState.IDLE, PlaybackState.PLAYING)}
            )
            controller.signal_player_state_update(
                player, {"playback_state": (PlaybackState.PLAYING, PlaybackState.IDLE)}
            )
            controller._check_idle_power_off(player)
        await power_off_task

        power_off.assert_not_awaited()
        assert time.monotonic() - player.extra_data[ATTR_IDLE_SINCE] < 60

    async def test_play_command_starts_the_count_over(self, mock_mass: MagicMock) -> None:
        """Playback the device reports only after the play command returned is not cut short."""
        controller, player, _protocol, power_off = self._setup(mock_mass)
        mock_mass.player_queues.resume = AsyncMock()
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60
        controller._check_idle_power_off(player)
        power_off_idle = mock_mass.create_task.call_args.args[0]

        await controller._handle_cmd_resume("player_1")
        # the device still reports idle when the power off gets its turn
        controller._check_idle_power_off(player)
        await power_off_idle

        power_off.assert_not_awaited()

    async def test_play_command_starts_the_count_over_for_its_members(
        self, mock_mass: MagicMock
    ) -> None:
        """A member is not taken out of a group that starts playing before it reports so."""
        controller, leader, leader_protocol, _power_off = self._setup(mock_mass)
        mock_mass.player_queues.resume = AsyncMock()
        member, _ = _attach(cast("MockProvider", leader_protocol.provider), "player_2", "Player 2")
        leader_protocol._attr_group_members = [_proto("player_1"), _proto("player_2")]
        _refresh(leader, member)
        assert leader.state.group_members == ["player_1", "player_2"]
        member.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60

        await controller._handle_cmd_resume("player_1")

        assert ATTR_IDLE_SINCE not in member.extra_data

    def test_playback_between_two_checks_starts_the_count_over(self, mock_mass: MagicMock) -> None:
        """Playback that starts and ends before the next check still counts as activity."""
        controller, player, _protocol, _power_off = self._setup(mock_mass)
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60

        controller.signal_player_state_update(
            player, {"playback_state": (PlaybackState.PLAYING, PlaybackState.IDLE)}
        )
        controller._check_idle_power_off(player)

        assert time.monotonic() - player.extra_data[ATTR_IDLE_SINCE] < 60

    def test_player_still_starting_up_is_not_idle(self, mock_mass: MagicMock) -> None:
        """A device that is switched on but not online yet does not count down."""
        controller, player, protocol, _power_off = self._setup(mock_mass, minutes=1)
        protocol._attr_available = False
        player.set_power_on_in_progress(True)
        player.refresh_state(signal_event=False)
        assert player.state.available is True
        assert player.state.powered is True
        assert player.state.playback_state == PlaybackState.IDLE
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 60

        controller._check_idle_power_off(player)

        assert ATTR_IDLE_SINCE not in player.extra_data
        mock_mass.create_task.assert_not_called()

    async def test_busy_player_is_not_powered_off(
        self, mock_mass: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A power off that can't get the player lock in time gives up and keeps the count."""
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_SLOW_THRESHOLD", 0.01)
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_TIMEOUT", 0.05)
        controller, player, _protocol, power_off = self._setup(mock_mass)
        player.extra_data[ATTR_IDLE_SINCE] = time.monotonic() - 30 * 60
        controller._check_idle_power_off(player)

        async with _lock_held_elsewhere(controller, "player_1"):
            await mock_mass.create_task.call_args.args[0]

        power_off.assert_not_awaited()
        assert ATTR_IDLE_SINCE in player.extra_data


class TestSelectSourcePowerOn:
    """Selecting a player's own source powers it on first."""

    async def test_switched_off_player_is_powered_on_without_auto_play(
        self, mock_mass: MagicMock
    ) -> None:
        """The selected source plays, not the queue that auto play would resume."""
        mock_mass.config.get_player_value = MagicMock(
            side_effect=_player_config_stub({CONF_POWER_CONTROL: PLAYER_CONTROL_FAKE})
        )
        mock_mass.cache.set = AsyncMock()
        mock_mass.player_queues.get = MagicMock(return_value=None)
        mock_mass.player_queues.resume = AsyncMock()
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        protocol._attr_supported_features.add(PlayerFeature.SELECT_SOURCE)
        protocol._attr_source_list = [PlayerSource(id="hdmi", name="HDMI")]
        protocol.select_source = AsyncMock()  # type: ignore[method-assign]
        _set_config_value(player, CONF_AUTO_PLAY, True)
        player.refresh_state(signal_event=False)
        assert player.state.powered is False

        await controller._handle_select_source("player_1", "hdmi")

        assert player.extra_data[ATTR_FAKE_POWER] is True
        protocol.select_source.assert_awaited_once_with("hdmi")
        mock_mass.player_queues.resume.assert_not_awaited()


class TestCurrentMediaTimeUpdates:
    """Playback-position anchor semantics of timing-only state updates."""

    def _make_player(self, mock_mass: MagicMock) -> tuple[PlayerController, Player, MockProtocol]:
        """Build a controller with a single playing player with a known position anchor."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test", mass=mock_mass)
        player, protocol = _attach(provider, "player_1", "Player 1")
        # no queue registered: current_media resolves from the player's native media
        mock_mass.player_queues.get = MagicMock(return_value=None)
        now = time.time()
        protocol._attr_playback_state = PlaybackState.PLAYING
        protocol._attr_elapsed_time = 17
        protocol._attr_elapsed_time_last_updated = now
        protocol.set_current_media(uri="http://test/stream", title="Test")
        assert protocol._attr_current_media is not None
        protocol._attr_current_media.elapsed_time = 17
        protocol._attr_current_media.elapsed_time_last_updated = now
        player.update_state(signal_event=False)
        # isolate from the unrelated state-forwarding machinery
        controller._forward_state_update = MagicMock()  # type: ignore[method-assign]
        mock_mass.signal_event.reset_mock()
        mock_mass.player_queues.on_player_elapsed_time_corrected.reset_mock()
        return controller, player, protocol

    def _player_updated_signalled(self, mock_mass: MagicMock) -> bool:
        """Return whether a PLAYER_UPDATED event was signalled."""
        return any(
            call.args and call.args[0] == EventType.PLAYER_UPDATED
            for call in mock_mass.signal_event.call_args_list
        )

    def test_regular_tick_is_suppressed(self, mock_mass: MagicMock) -> None:
        """A regular playback tick (position and anchor advance together) emits nothing."""
        _ctrl, player, protocol = self._make_player(mock_mass)
        assert protocol._attr_current_media is not None
        assert protocol._attr_elapsed_time_last_updated is not None

        protocol._attr_elapsed_time = 18
        protocol._attr_elapsed_time_last_updated += 1
        protocol._attr_current_media.elapsed_time = 18
        assert protocol._attr_current_media.elapsed_time_last_updated is not None
        protocol._attr_current_media.elapsed_time_last_updated += 1
        player.update_state()

        assert not self._player_updated_signalled(mock_mass)
        mock_mass.player_queues.on_player_elapsed_time_corrected.assert_not_called()
        # the previous anchor was preserved: steady playback changes nothing
        assert player.state.elapsed_time == 17

    def test_anchor_only_change_is_suppressed(self, mock_mass: MagicMock) -> None:
        """An anchor-only change (no significant corrected position change) emits nothing."""
        _ctrl, player, protocol = self._make_player(mock_mass)
        assert protocol._attr_current_media is not None
        assert protocol._attr_elapsed_time_last_updated is not None

        protocol._attr_elapsed_time_last_updated += 0.5
        assert protocol._attr_current_media.elapsed_time_last_updated is not None
        protocol._attr_current_media.elapsed_time_last_updated += 0.5
        player.update_state()

        assert not self._player_updated_signalled(mock_mass)
        mock_mass.player_queues.on_player_elapsed_time_corrected.assert_not_called()

    def test_corrected_position_jump_emits_player_updated(self, mock_mass: MagicMock) -> None:
        """A corrected-position jump of the current media (e.g. seek) emits a player update."""
        _ctrl, player, protocol = self._make_player(mock_mass)
        assert protocol._attr_current_media is not None

        protocol._attr_current_media.elapsed_time = 61
        protocol._attr_current_media.elapsed_time_last_updated = time.time()
        player.update_state()

        assert self._player_updated_signalled(mock_mass)
        # the adopted anchor is visible to consumers
        assert player.state.current_media is not None
        assert player.state.current_media.elapsed_time == 61

    def test_player_position_jump_corrects_queue(self, mock_mass: MagicMock) -> None:
        """A player-level corrected-position jump re-bases the queue timing."""
        controller, player, protocol = self._make_player(mock_mass)

        protocol._attr_elapsed_time = 61
        protocol._attr_elapsed_time_last_updated = time.time()
        player.update_state()

        # the queue is corrected and a follow-up player update is scheduled
        # (which re-anchors current_media onto the corrected queue time),
        # but no full player update is emitted for the jump itself
        mock_mass.player_queues.on_player_elapsed_time_corrected.assert_called_once_with(player)
        assert not self._player_updated_signalled(mock_mass)
        cast("MagicMock", controller._forward_state_update).assert_called_once()
        assert player.state.elapsed_time == 61

    def test_simultaneous_player_and_media_jump_emits_immediately(
        self, mock_mass: MagicMock
    ) -> None:
        """A jump reaching player and current_media in one pass corrects the queue and emits."""
        _ctrl, player, protocol = self._make_player(mock_mass)
        assert protocol._attr_current_media is not None
        now = time.time()

        protocol._attr_elapsed_time = 61
        protocol._attr_elapsed_time_last_updated = now
        protocol._attr_current_media.elapsed_time = 61
        protocol._attr_current_media.elapsed_time_last_updated = now
        player.update_state()

        # the queue is re-based AND the full update is emitted right away
        # (current_media already holds the fresh position in the same pass)
        mock_mass.player_queues.on_player_elapsed_time_corrected.assert_called_once_with(player)
        assert self._player_updated_signalled(mock_mass)


class TestScheduleActiveOutputProtocolClear:
    """Test the deferred clear of a player's active output protocol."""

    def test_schedule_starts_cancellable_clear_task(self, mock_mass: MagicMock) -> None:
        """Scheduling defers the clear to a single, per-player, cancellable task."""
        controller = PlayerController(mock_mass)
        player = MagicMock()
        player.player_id = "player_1"

        controller.schedule_active_output_protocol_clear(player)

        mock_mass.create_task.assert_called_once()
        # close the coroutine passed to the mocked create_task to avoid a
        # "coroutine was never awaited" warning
        mock_mass.create_task.call_args.args[0].close()
        # no abort_existing: a duplicate schedule must reuse the pending clear
        # (deduped by task_id) instead of replacing it with an untracked task
        assert mock_mass.create_task.call_args.kwargs == {
            "task_id": "clear_active_protocol_player_1",
        }

    @pytest.mark.asyncio
    async def test_clears_protocol_once_player_idle(self, mock_mass: MagicMock) -> None:
        """The protocol is cleared after waiting for the player to reach IDLE."""
        controller = PlayerController(mock_mass)
        player = MagicMock()
        player.player_id = "player_1"

        with patch.object(controller, "_wait_for_playback_state", new=AsyncMock()) as wait_mock:
            await controller._clear_active_output_protocol_when_idle(player)

        wait_mock.assert_awaited_once_with(player, PlaybackState.IDLE, timeout=10)
        player.set_active_output_protocol.assert_called_once_with(None)


class TestRemovePlayerControl:
    """Test removing a registered player control."""

    def test_removal_refreshes_the_players_that_used_it(self, mock_mass: MagicMock) -> None:
        """Test that only the players configured to use the removed control are refreshed."""
        mock_mass.loop = MagicMock()
        controller = PlayerController(mock_mass)
        using_control = MagicMock()
        using_control.state.power_control = "switch.amp"
        using_control.state.volume_control = PLAYER_CONTROL_NATIVE
        using_control.state.mute_control = PLAYER_CONTROL_NATIVE
        unrelated = MagicMock()
        unrelated.state.power_control = PLAYER_CONTROL_NATIVE
        unrelated.state.volume_control = PLAYER_CONTROL_NATIVE
        unrelated.state.mute_control = PLAYER_CONTROL_NATIVE
        controller._players = {"using_control": using_control, "unrelated": unrelated}
        controller._controls = {
            "switch.amp": PlayerControl(id="switch.amp", provider="test_prov", name="Amp")
        }

        controller.remove_player_control("switch.amp")

        assert controller.player_controls() == []
        mock_mass.loop.call_soon.assert_called_once_with(using_control.refresh_state)

    async def test_a_returning_control_is_picked_back_up(self, mock_mass: MagicMock) -> None:
        """Test that a control removed and registered again re-attaches to its player."""
        # run the scheduled refresh straight away so each step is observable
        mock_mass.loop = MagicMock()
        mock_mass.loop.call_soon.side_effect = lambda callback, *args: callback(*args)
        mock_mass.config.get_player_value.side_effect = _player_config_stub(
            {CONF_POWER_CONTROL: "switch.amp"}
        )
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
        mock_mass.get_provider.return_value = provider
        player, _ = _attach(provider, "player", "Player")
        control = PlayerControl(id="switch.amp", provider="test_prov", name="Amp")

        await controller.register_or_update_player_control(control)
        assert player.state.power_control == "switch.amp"

        # the Home Assistant plugin drops and re-registers its controls around a reload
        controller.remove_player_control(control.id)
        assert player.state.power_control == PLAYER_CONTROL_NONE

        await controller.register_or_update_player_control(control)
        assert player.state.power_control == "switch.amp"

    def test_removing_an_unknown_control_does_nothing(self, mock_mass: MagicMock) -> None:
        """Test that removing a control that was never registered is a no-op."""
        mock_mass.loop = MagicMock()
        controller = PlayerController(mock_mass)
        player = MagicMock()
        player.state.power_control = PLAYER_CONTROL_NATIVE
        player.state.volume_control = PLAYER_CONTROL_NATIVE
        player.state.mute_control = PLAYER_CONTROL_NATIVE
        controller._players = {"player": player}

        controller.remove_player_control("switch.gone")

        mock_mass.loop.call_soon.assert_not_called()


@pytest.mark.parametrize("scope", ["player", "protocol"])
class TestConfigChangeRestartsPlayback:
    """
    Test that a changed setting which needs a reload restarts playback.

    A setting is stored on the player or on one of its protocols (such as the output
    codec); a change of either one restarts the queue.
    """

    @staticmethod
    def _config(scope: str, *, requires_reload: bool) -> PlayerConfig:
        """Build the config of the given scope holding a single setting."""
        key = CONF_OUTPUT_CODEC if scope == "protocol" else "player_setting"
        return PlayerConfig(
            provider="test_prov",
            player_id="player_1" if scope == "player" else _proto("player_1"),
            values={
                key: ConfigEntry(
                    key=key,
                    type=ConfigEntryType.STRING,
                    label="Setting",
                    value="flac",
                    requires_reload=requires_reload,
                )
            },
        )

    @staticmethod
    def _prepare(mock_mass: MagicMock, queue_state: PlaybackState) -> PlayerController:
        """Register a player whose active queue is in the given state."""
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
        player, _ = _attach(provider, "player_1", "Player 1")
        player.set_active_mass_source("player_1")
        _refresh(player)
        assert player.state.active_source == "player_1"
        queue = MagicMock()
        queue.queue_id = "player_1"
        queue.state = queue_state
        mock_mass.player_queues.get = MagicMock(return_value=queue)
        mock_mass.player_queues.stop = AsyncMock()
        mock_mass.config.get_player_object_config = AsyncMock(return_value=player.config)
        return controller

    async def _change(
        self, controller: PlayerController, scope: str, config: PlayerConfig, key: str
    ) -> None:
        """Let the config controller report a changed setting of the given scope."""
        config_controller = cast("MagicMock", controller.mass.config)
        config_controller.get_protocol_config = AsyncMock(return_value=config)
        if scope == "player":
            await controller.on_player_config_change(config, {f"values/{key}"})
        else:
            await controller.on_protocol_config_change(_proto("player_1"), {f"values/{key}"})

    async def test_reload_setting_restarts_playback(self, mock_mass: MagicMock, scope: str) -> None:
        """Test that changing a reload-requiring setting stops and resumes the queue."""
        controller = self._prepare(mock_mass, PlaybackState.PLAYING)
        config = self._config(scope, requires_reload=True)

        await self._change(controller, scope, config, next(iter(config.values)))

        mock_mass.player_queues.stop.assert_awaited_once_with("player_1")
        assert [scheduled.args for scheduled in _scheduled_resumes(mock_mass)] == [
            (1, mock_mass.player_queues.resume, "player_1", False)
        ]

    async def test_plain_setting_does_not_restart_playback(
        self, mock_mass: MagicMock, scope: str
    ) -> None:
        """Test that a setting which applies on the fly leaves playback alone."""
        controller = self._prepare(mock_mass, PlaybackState.PLAYING)
        config = self._config(scope, requires_reload=False)

        await self._change(controller, scope, config, next(iter(config.values)))

        mock_mass.player_queues.stop.assert_not_awaited()
        assert _scheduled_resumes(mock_mass) == []

    async def test_untouched_reload_setting_does_not_restart_playback(
        self, mock_mass: MagicMock, scope: str
    ) -> None:
        """Test that only a changed reload-requiring setting restarts playback."""
        controller = self._prepare(mock_mass, PlaybackState.PLAYING)

        await self._change(controller, scope, self._config(scope, requires_reload=True), CONF_ICON)

        mock_mass.player_queues.stop.assert_not_awaited()
        assert _scheduled_resumes(mock_mass) == []

    async def test_idle_queue_is_left_alone(self, mock_mass: MagicMock, scope: str) -> None:
        """Test that a reload-requiring change does not start playback on an idle queue."""
        controller = self._prepare(mock_mass, PlaybackState.IDLE)
        config = self._config(scope, requires_reload=True)

        await self._change(controller, scope, config, next(iter(config.values)))

        mock_mass.player_queues.stop.assert_not_awaited()
        assert _scheduled_resumes(mock_mass) == []


def _scheduled_resumes(mock_mass: MagicMock) -> list[Any]:
    """Return the ``call_later`` calls that scheduled a queue resume."""
    return [
        scheduled
        for scheduled in mock_mass.call_later.call_args_list
        if mock_mass.player_queues.resume in scheduled.args
    ]


class TestAddCurrentlyPlayingToFavorites:
    """Test who a favorite added from a player belongs to."""

    PLAYBACK_USER = User(user_id="user-a", username="user-a", role=UserRole.USER)
    SERVICE_USER = User(user_id="service", username="service", role=UserRole.SERVICE)
    OTHER_USER = User(user_id="user-b", username="user-b", role=UserRole.USER)
    ADMIN_USER = User(user_id="admin", username="admin", role=UserRole.ADMIN)

    @pytest.mark.parametrize(
        ("session_user", "playback_user", "expected"),
        [
            pytest.param(None, PLAYBACK_USER, PLAYBACK_USER, id="no-session-user"),
            pytest.param(SERVICE_USER, PLAYBACK_USER, PLAYBACK_USER, id="service-account"),
            pytest.param(SERVICE_USER, None, SERVICE_USER, id="service-account-anonymous-queue"),
            pytest.param(OTHER_USER, PLAYBACK_USER, OTHER_USER, id="user-session"),
            pytest.param(ADMIN_USER, PLAYBACK_USER, ADMIN_USER, id="admin-session"),
        ],
    )
    async def test_favorite_owner(
        self,
        mock_mass: MagicMock,
        session_user: User | None,
        playback_user: User | None,
        expected: User,
    ) -> None:
        """
        Test that a call without a user, or from a service account, counts for the playback user.

        :param mock_mass: The mocked MusicAssistant instance.
        :param session_user: The user the command is called as.
        :param playback_user: The user the queue plays for.
        :param expected: The user the favorite must be recorded for.
        """
        controller = _controller(mock_mass)
        provider = MockProvider("test_provider", instance_id="test_prov", mass=mock_mass)
        player, _ = _attach(provider, "player1", "Player 1")
        _refresh(player)
        queue = MagicMock(queue_id="player1")
        queue.current_item.media_item.media_type = MediaType.TRACK
        acting_users: list[User | None] = []

        async def _capture_acting_user(item: Any) -> None:  # noqa: ARG001
            acting_users.append(get_current_user())

        mock_mass.music.add_item_to_favorites = AsyncMock(side_effect=_capture_acting_user)

        token = current_user.set(session_user)
        try:
            with (
                patch.object(controller, "get_active_queue", return_value=queue),
                patch.object(
                    players_playback,
                    "resolve_playback_user",
                    AsyncMock(return_value=playback_user),
                ),
            ):
                await controller.add_currently_playing_to_favorites("player1")
        finally:
            current_user.reset(token)

        assert acting_users == [expected]


class TestPlayerLockTimeouts:
    """A command that cannot get a player lock in time fails; the warning names the holder."""

    @pytest.fixture(autouse=True)
    def _shrink_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_SLOW_THRESHOLD", 0.01)
        monkeypatch.setattr(players_locks, "PLAYER_LOCK_TIMEOUT", 0.05)

    async def test_a_nested_acquisition_keeps_its_holder_named(
        self, controller: PlayerController, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A holder that took its lock a second time is still the one a waiter names."""
        held = asyncio.Event()
        release = asyncio.Event()

        async def _holder_waiting_for_its_device() -> None:
            async with controller.get_player_lock("p1"):
                async with controller.get_player_lock("p1"):
                    pass
                held.set()
                await release.wait()

        holder = asyncio.create_task(_holder_waiting_for_its_device())
        await held.wait()
        with caplog.at_level(logging.WARNING), pytest.raises(ResourceBusyError):
            async with controller.get_player_lock("p1"):
                pytest.fail("the body ran without the lock")
        release.set()
        await holder

        assert "_holder_waiting_for_its_device" in caplog.text

    @pytest.mark.parametrize("holder_state", ["unknown", "done"])
    async def test_a_lock_without_a_live_holder_still_fails_as_busy(
        self,
        controller: PlayerController,
        caplog: pytest.LogCaptureFixture,
        holder_state: str,
    ) -> None:
        """The warning of a wait that times out never fails on the holder it describes."""
        lock = controller._player_command_locks.setdefault("playback_p1", asyncio.Lock())
        await lock.acquire()
        if holder_state == "done":
            finished = asyncio.create_task(asyncio.sleep(0))
            await finished
            controller._lock_holders["playback_p1"] = finished
        try:
            with caplog.at_level(logging.WARNING), pytest.raises(ResourceBusyError):
                async with controller.get_player_lock("p1"):
                    pytest.fail("the body ran without the lock")
        finally:
            lock.release()

        assert "held by a task that is gone" in caplog.text

    async def test_a_group_and_player_lock_takes_the_group_lock_first(
        self, mock_mass: MagicMock
    ) -> None:
        """A grouped player waits for the group's lock, then for its own."""
        controller, _group, _member = _group_with_member(mock_mass)
        acquisitions: list[str] = []
        acquire_lock = controller.get_player_lock

        def _record(
            player_id: str, purpose: PlayerLockPurpose = PlayerLockPurpose.PLAYBACK
        ) -> contextlib.AbstractAsyncContextManager[None]:
            acquisitions.append(player_id)
            return acquire_lock(player_id, purpose)

        controller.get_player_lock = _record  # type: ignore[assignment]
        async with controller.get_group_and_player_lock("member"):
            pass

        assert acquisitions == ["group", "member"]


def test_state_update_during_attach_is_not_published(mock_mass: MagicMock) -> None:
    """A state update of a player whose attach did not finish yet emits no player events."""
    controller = _controller(mock_mass)
    provider = MockProvider("test", instance_id="test", mass=mock_mass)
    player, _ = _attach(provider, "p1", "Player")
    player.initialized.clear()
    changed = {"volume_level": (10, 20), "supported_features": (set(), {PlayerFeature.VOLUME_SET})}
    published = {EventType.PLAYER_UPDATED, EventType.PLAYER_CONFIG_UPDATED}

    controller.signal_player_state_update(player, changed)

    assert not [c for c in mock_mass.signal_event.call_args_list if c.args[0] in published]

    player.set_initialized()
    controller.signal_player_state_update(player, changed)

    assert {c.args[0] for c in mock_mass.signal_event.call_args_list} >= published
