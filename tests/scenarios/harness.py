"""
Scenario harness for the player lifecycle: fake player providers on a real server.

A scenario boots a real MusicAssistant on a temporary storage folder, loads fake player
providers that impersonate real provider domains, and drives it through the entry points a
real provider or client uses, on a virtual clock. See README.md in this folder.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import sys
import time
from dataclasses import dataclass, field, replace
from functools import partial
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeVar, cast
from unittest.mock import patch

from aiosqlite.core import Connection as SqliteConnection
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    ProtocolRole,
    ProviderType,
)
from music_assistant_models.player import DeviceInfo
from music_assistant_models.player_control import PlayerControl
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import (
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_SPLIT_SESSION_ONLY,
    CONF_PLAYER_SPLIT_TREE,
    CONF_PLAYERS,
)
from music_assistant.controllers.player_queues.constants import CACHE_CATEGORY_PLAYER_QUEUE_ITEMS
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.helpers.util import load_provider_module
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.models.player_provider import PlayerProvider
from tests.conftest import full_mass_context

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping

    import pytest
    from music_assistant_models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.player import Player as PlayerState
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models.player import PlayerMedia

_T = TypeVar("_T")

# Player providers that keep their real implementation, as they own no device.
REAL_PLAYER_PROVIDERS = frozenset({"universal_group", "sync_group"})

# The provider instance that owns the fake power controls: one that is always loaded.
POWER_CONTROL_PROVIDER = "builtin"

# The music provider play() plays from: the server's own test provider, whose fake media items
# need no network, loaded on first use.
MUSIC_PROVIDER = "test"
# The track play() plays.
TEST_TRACK_URI = f"{MUSIC_PROVIDER}://track/0_0_0"

# Loop time at the start of every scenario.
CLOCK_START = 1000.0

# Seconds idle() lets pass by default: past the server's debounces and follow-up checks
# (0.25 s to 5 s), short of the 15 s a protocol waits for the identifiers of its device.
IDLE_HORIZON = 10.0

# Tracked server timers of background work that keeps coming back while the server runs
# (scheduled tasks and the task list they update), which idle() ignores.
BACKGROUND_TIMERS = (
    "managed_task_timer_",
    "managed_tasks_signal_event",
    # re-armed by the poll loop every second while a player plays
    "queue_on_player_update_",
    "storage_refresh",
    "playlist_metadata_ai_catch_up",
)

# Real seconds settle() waits for the loop to become quiet before it fails.
SETTLE_TIMEOUT = 5.0

# The warnings every boot logs, which are no failure of a scenario.
BOOT_LOGS = (
    "not installed through one of the supported installation methods",
    "Started streamserver on",
    "SETUP REQUIRED",
)

DEFAULT_FEATURES = frozenset(
    {
        PlayerFeature.PLAY_MEDIA,
        PlayerFeature.PAUSE,
        PlayerFeature.VOLUME_SET,
        PlayerFeature.VOLUME_MUTE,
    }
)

# The one config entry every fake protocol has of its own, for scenarios about protocol config.
FAKE_SETTING = "fake_setting"

type Role = Literal["native", "audio", "control", "virtual"]
type Identifiers = Mapping[IdentifierType, str]

# The role a protocol reports for each role a scenario names.
_ROLES: dict[Role, ProtocolRole] = {
    "native": ProtocolRole.NATIVE,
    "audio": ProtocolRole.AUDIO,
    "control": ProtocolRole.CONTROL,
    "virtual": ProtocolRole.VIRTUAL,
}

# the real clock, for the harness's own time limits
_real_monotonic = time.monotonic


class ScenarioError(Exception):
    """The harness or a precondition of a scenario failed, which is never an expected failure."""


class Event(NamedTuple):
    """An event the server emitted."""

    type: EventType
    object_id: str | None


@dataclass(frozen=True)
class VisiblePlayer:
    """A player as clients see it in the `players/all` listing."""

    player_id: str
    name: str
    available: bool
    powered: bool | None
    # domain of the provider instance the player state names
    provider: str
    # (protocol domain, available) of each output of the player, sorted
    outputs: tuple[tuple[str, bool], ...]


class FakeProtocol(PlayerProtocol):
    """
    A provider object for one device: the test sets what it reports, its commands are recorded.

    Commands are applied to the reported state the way a well-behaved device reports them
    back (power, volume, playback), so the server never waits for a device that stays silent.
    It has one config entry of its own (FAKE_SETTING), like a provider setting of the device.
    An object belongs to the server and provider instance it was created on; after a restart
    or a provider reload, Scenario.protocol() returns the current one.
    """

    def __init__(self, provider: FakePlayerProvider, protocol_id: str, device: _Device) -> None:
        """
        Initialize the protocol object of a device; use FakePlayerProvider.protocol().

        :param provider: The fake provider this protocol belongs to.
        :param protocol_id: ID of the protocol object.
        :param device: What the device reports, kept across restarts.
        """
        super().__init__(provider, protocol_id)
        # every command received, as a tuple of the command name and its arguments
        self.commands: list[tuple[Any, ...]] = []
        # whether the protocol exists only while attached, like a browser session
        self._attr_session_only = device.session_only
        self._device = device
        self._attr_supported_features = set(DEFAULT_FEATURES)
        # whether its native grouping only works while it renders its own stream (AirPlay)
        self._attr_native_grouping_requires_own_stream = False
        self._attr_device_info = DeviceInfo(model=f"Fake {provider.domain}", manufacturer="Fake")
        self._set_reported(identifiers=dict(device.identifiers), **device.state)

    @property
    def native_grouping_requires_own_stream(self) -> bool:
        """Return whether native grouping only works while this device renders its own stream."""
        return self._attr_native_grouping_requires_own_stream

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return the protocol's own config entry."""
        return [
            *await super().get_config_entries(),
            ConfigEntry(
                key=FAKE_SETTING,
                type=ConfigEntryType.STRING,
                label="Fake setting",
                required=False,
            ),
        ]

    async def power(self, powered: bool) -> None:
        """Handle a power command."""
        self._apply("power", powered, powered=powered)

    async def volume_set(self, volume_level: int) -> None:
        """Handle a volume command."""
        self._apply("volume_set", volume_level, volume_level=volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle a mute command."""
        self._apply("volume_mute", muted, volume_muted=muted)

    async def play(self) -> None:
        """Handle a play command."""
        self._apply("play", playback_state=PlaybackState.PLAYING)

    async def pause(self) -> None:
        """Handle a pause command."""
        self._apply("pause", playback_state=PlaybackState.PAUSED)

    async def stop(self) -> None:
        """Handle a stop command."""
        self._apply("stop", playback_state=PlaybackState.IDLE, current_media=None)

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle a play media command."""
        self._apply("play_media", media, playback_state=PlaybackState.PLAYING, current_media=media)

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle the enqueue of the next media."""
        self._apply("enqueue_next_media", media)

    async def next_track(self) -> None:
        """Handle a next track command."""
        self._apply("next_track")

    async def previous_track(self) -> None:
        """Handle a previous track command."""
        self._apply("previous_track")

    async def seek(self, position: int) -> None:
        """Handle a seek command."""
        self._apply("seek", position)

    async def select_source(self, source: str) -> None:
        """Handle a select source command."""
        self._apply("select_source", source, active_source=source)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle a set members command; a device that joins leaves the group it was in."""
        for protocol_id in player_ids_to_add or []:
            for leader in cast("FakePlayerProvider", self.provider)._protocols.values():
                if leader is not self and protocol_id in leader._attr_group_members:
                    leader._report_members(leader._attr_group_members, [protocol_id])
        members = dict.fromkeys(self._attr_group_members)
        members.update(dict.fromkeys(player_ids_to_add or []))
        self._apply(
            "set_members",
            player_ids_to_add,
            player_ids_to_remove,
            group_members=self._members_without(members, player_ids_to_remove or []),
        )

    async def on_remove(self) -> None:
        """Drop this object from its provider, which then finds the device again as a new one."""
        cast("FakePlayerProvider", self.provider)._protocols.pop(self.protocol_id, None)

    def _report_members(self, members: Iterable[str], removed: Iterable[str]) -> None:
        """Report the group without the removed members, the way the device reports it."""
        self._set_reported(group_members=self._members_without(dict.fromkeys(members), removed))
        self.update_state()

    def _members_without(self, members: dict[str, None], removed: Iterable[str]) -> list[str]:
        """Return the group members without the removed ones, itself first, empty when alone."""
        for protocol_id in removed:
            members.pop(protocol_id, None)
        others = [protocol_id for protocol_id in members if protocol_id != self.protocol_id]
        return [self.protocol_id, *others] if others else []

    def _apply(self, command: str, *args: Any, **state: Any) -> None:
        """Record a command and report the state it leads to."""
        self.commands.append((command, *args))
        cast("FakePlayerProvider", self.provider)._timeline.append(
            (self.protocol_id, (command, *args))
        )
        if state:
            self._set_reported(**state)
            self.update_state()

    def _set_reported(self, **state: Any) -> None:
        """Set reported state without publishing it, and keep it for the device."""
        for key, value in state.items():
            if key == "identifiers":
                for identifier_type, identifier in value.items():
                    # None (or no value) drops the identifier
                    self._attr_device_info.add_identifier(identifier_type, identifier)
                    if identifier:
                        self._device.identifiers[identifier_type] = identifier
                    else:
                        self._device.identifiers.pop(identifier_type, None)
                continue
            if not hasattr(self, f"_attr_{key}"):
                msg = f"{type(self).__name__} reports no state named {key!r}"
                raise ScenarioError(msg)
            setattr(self, f"_attr_{key}", value)
            self._device.state[key] = value


class FakePlayerProvider(PlayerProvider):
    """
    A player provider of an impersonated domain, owning the FakeProtocols a test creates.

    Like a real provider, it attaches the devices that are present when it loads and when one
    of its players gets enabled or removed.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        devices: dict[str, _Device],
        timeline: list[tuple[str, tuple[Any, ...]]],
    ) -> None:
        """
        Initialize the provider.

        :param mass: The server this provider instance runs on.
        :param manifest: The (fake) manifest of the impersonated domain.
        :param config: The provider instance config.
        :param devices: The devices of the scenario, by protocol id.
        :param timeline: The commands the devices of the scenario received, in order.
        """
        super().__init__(mass, manifest, config)
        self._devices = devices
        self._timeline = timeline
        self._protocols: dict[str, FakeProtocol] = {}

    def protocol(
        self,
        protocol_id: str,
        name: str,
        *,
        role: Role = "native",
        identifiers: Identifiers | None = None,
        session_only: bool = False,
        **state: Any,
    ) -> FakeProtocol:
        """
        Create a new device with its protocol object of this provider; attach it to use it.

        :param protocol_id: ID of the protocol object.
        :param name: The name the device reports.
        :param role: ``"native"`` for the vendor's own control plane, ``"audio"`` for an audio
            protocol of a device (AirPlay, Cast, DLNA, ...), ``"control"`` for control without
            an audio path (LinkPlay), ``"virtual"`` for a group (with ``type=PlayerType.GROUP``).
        :param identifiers: The device identifiers it reports (MAC, IP, UUID, ...).
        :param session_only: Whether the protocol reports that it exists only while attached,
            like a browser session.
        :param state: Other reported state, as taken by Scenario.report().
        """
        self._devices[protocol_id] = _Device(
            provider_id=self.instance_id,
            state={"name": name, "role": _ROLES[role], **state},
            identifiers=dict(identifiers or {}),
            session_only=session_only,
        )
        self._protocols.pop(protocol_id, None)
        return self._protocol_for(protocol_id)

    async def discover_players(self) -> None:
        """Attach the present devices of this provider that are not attached."""
        for protocol_id, device in self._devices.items():
            if device.provider_id != self.instance_id or not device.present:
                continue
            if self.mass.players.get_protocol(protocol_id) is None:
                await self.mass.players.attach(self._protocol_for(protocol_id))

    def _protocol_for(self, protocol_id: str) -> FakeProtocol:
        """Return the protocol object of a device on this provider instance."""
        if (protocol := self._protocols.get(protocol_id)) is None:
            protocol = FakeProtocol(self, protocol_id, self._devices[protocol_id])
            self._protocols[protocol_id] = protocol
        return protocol


class FakePowerControl:
    """An external power control, like a smart plug in Home Assistant, kept across restarts."""

    def __init__(self, control_id: str, name: str, powered: bool) -> None:
        """
        Initialize the power control.

        :param control_id: ID the control registers with, which a player config can select.
        :param name: Name of the control.
        :param powered: Whether the control is switched on.
        """
        self.control_id = control_id
        self.name = name
        self.powered = powered
        # every power command received: True for on, False for off
        self.commands: list[bool] = []


class Scenario:
    """
    A real server with fake player providers, driven the way providers and clients drive it.

    The event loop clock is virtual: it stands still, also while the server works, and only
    moves through advance() and idle(). Every action settles what is due at the current time;
    what the server put on a timer (debounces, delays) runs once the clock gets there.
    """

    def __init__(self, tmp_path: pathlib.Path) -> None:
        """
        Initialize the scenario; start() boots the server.

        :param tmp_path: Folder holding the server's storage, kept across restarts.
        """
        self.mass: MusicAssistant
        # the fake ARP table: IP address -> MAC address the server resolves it to
        self.arp: dict[str, str] = {}
        self._tmp_path = tmp_path
        self._clock = _VirtualClock()
        self._events: list[Event] = []
        self._removals: list[tuple[str | None, Any]] = []
        self._player_updates: list[tuple[str | None, PlayerState]] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._devices: dict[str, _Device] = {}
        # every command any device received, in order: (protocol id, command)
        self.timeline: list[tuple[str, tuple[Any, ...]]] = []
        self._power_controls: dict[str, FakePowerControl] = {}
        self._commands: set[asyncio.Task[Any]] = set()
        self._threads_busy = 0
        self._errors = _ErrorLog()
        self._expected_logs: list[str] = []
        self._patches = contextlib.ExitStack()
        self._server: contextlib.AsyncExitStack | None = None

    async def start(self) -> None:
        """Boot the server."""
        self._patches.enter_context(self._patch_environment())
        await self._boot()

    async def close(self) -> None:
        """
        Stop the server and undo what the scenario patched.

        Raises ScenarioError when the server logged a warning or worse that is not expected (see
        expect_log()), or left work running.
        """
        try:
            await self._shutdown()
        finally:
            self._patches.close()
        allowed = (*BOOT_LOGS, *self._expected_logs)
        if errors := [
            error for error in self._errors.messages if not any(text in error for text in allowed)
        ]:
            msg = "The server reported a failure:\n" + "\n".join(errors)
            raise ScenarioError(msg)

    def expect_log(self, substring: str) -> None:
        """
        Allow a warning (or worse) the scenario causes on purpose.

        :param substring: Part of the message of the record to allow.
        """
        self._expected_logs.append(substring)

    async def add_provider(self, domain: str) -> FakePlayerProvider:
        """
        Add and load a new instance of a fake player provider.

        :param domain: The player provider domain to impersonate, e.g. ``"airplay"``.
        """
        if domain in REAL_PLAYER_PROVIDERS:
            msg = f"{domain} keeps its real implementation"
            raise ScenarioError(msg)
        if self.mass.get_provider_manifest(domain).type != ProviderType.PLAYER:
            msg = f"{domain} is no player provider"
            raise ScenarioError(msg)
        # the step the add-provider setup flow ends in for a provider without setup input;
        # the flow itself would import the setup module of the real provider
        config: ProviderConfig = await self.mass.config._create_provider_instance(domain, {})
        await self.settle()
        return self.provider(config.instance_id)

    async def reload_provider(self, provider: FakePlayerProvider) -> FakePlayerProvider:
        """
        Reload a provider instance, as a config change does; return the new instance.

        :param provider: The provider to reload.
        """
        await self.mass.load_provider(provider.instance_id)
        await self.settle()
        return self.provider(provider.instance_id)

    async def remove_provider(self, provider: FakePlayerProvider) -> None:
        """
        Remove a provider instance the way a user does.

        :param provider: The provider to remove.
        """
        await self.mass.config.remove_provider_config(provider.instance_id)
        await self.settle()

    async def attach(self, protocol: FakeProtocol, role: Role | None = None) -> None:
        """
        Attach a protocol object: its provider found the device, or found it again.

        :param protocol: The protocol object to attach.
        :param role: A new role of the protocol, for a device that changed its role.
        """
        self._check_current(protocol)
        if role is not None:
            protocol._set_reported(role=_ROLES[role])
        protocol._device.present = True
        await self.mass.players.attach(protocol)
        await self.settle()

    async def detach(self, protocol: FakeProtocol) -> None:
        """
        Detach a protocol object: its provider lost the device.

        :param protocol: The protocol object to detach.
        """
        self._check_current(protocol)
        protocol._device.present = False
        await self.mass.players.detach(protocol.protocol_id)
        await self.settle()

    def set_present(self, protocol_id: str, present: bool) -> None:
        """
        Set whether a device is there for its provider to find, without telling the server.

        For a device that comes or goes while the server is down or its provider reloads.

        :param protocol_id: ID of the protocol object of the device.
        :param present: Whether the provider finds the device when it next looks.
        """
        self._devices[protocol_id].present = present

    async def report(self, protocol: FakeProtocol, **state: Any) -> None:
        """
        Let a protocol object report new state, the way its provider publishes a device update.

        :param protocol: The protocol object that reports.
        :param state: Values by attribute name without the ``_attr_`` prefix (``available``,
            ``powered``, ``volume_level``, ``playback_state``, ``supported_features``, ...),
            plus ``identifiers`` (merged into the reported device info, a None value drops
            one). A role changes with attach().
        """
        self._check_current(protocol)
        if "role" in state:
            msg = "a role changes with attach(protocol, role=...)"
            raise ScenarioError(msg)
        protocol._set_reported(**state)
        protocol.update_state()
        await self.settle()

    async def remove(self, player_id: str) -> None:
        """
        Remove a player the way a user does.

        A device still present comes back as a new player once its provider looks for devices
        again, 5 s later (idle()).

        :param player_id: ID of the player to remove.
        """
        await self.mass.players.remove(player_id)
        await self.settle()

    async def configure_player(self, player_id: str, **values: Any) -> None:
        """
        Change the config of a player the way a user does.

        :param player_id: ID of the player.
        :param values: The config values to change, e.g. ``name="Kitchen"`` or
            ``power_control=control.control_id``.
        """
        await self.mass.config.save_player_config(player_id, values)
        await self.settle()

    async def enable(self, player_id: str) -> None:
        """
        Enable a player the way a user does.

        Its device comes back once its provider looks for devices again, 5 s later (idle()).

        :param player_id: ID of the player.
        """
        await self.configure_player(player_id, enabled=True)

    async def disable(self, player_id: str) -> None:
        """
        Disable a player the way a user does.

        :param player_id: ID of the player.
        """
        await self.configure_player(player_id, enabled=False)

    async def add_power_control(
        self, control_id: str, name: str, *, powered: bool = False
    ) -> FakePowerControl:
        """
        Add an external power control; select it on a player with configure_player().

        :param control_id: ID the control registers with.
        :param name: Name of the control.
        :param powered: Whether the control is switched on.
        """
        control = FakePowerControl(control_id, name, powered)
        self._power_controls[control_id] = control
        await self._register_power_control(control)
        await self.settle()
        return control

    async def switch(self, control: FakePowerControl, powered: bool) -> None:
        """
        Let a power control report that it was switched, e.g. by hand.

        :param control: The power control.
        :param powered: Whether it is switched on now.
        """
        self._set_power(control, powered)
        await self.settle()

    async def play(self, player_id: str) -> None:
        """
        Play a track on a player through its queue, the way a client does.

        The track is one of the server's own test music provider (see MUSIC_PROVIDER), loaded
        on first use; no audio is decoded for it. Returns once the player reports that it
        plays, and raises ScenarioError when it does not get there.

        :param player_id: ID of the player, which is the id of its queue.
        """
        if self.mass.get_provider(MUSIC_PROVIDER) is None:
            await self.mass.config._create_provider_instance(MUSIC_PROVIDER, {})
        task = await self.command(self.mass.player_queues.play_media(player_id, TEST_TRACK_URI))
        if not task.done():
            msg = f"{player_id} did not start playing"
            raise ScenarioError(msg)
        await task
        state = self.player(player_id)
        if state is None or state.playback_state != PlaybackState.PLAYING:
            msg = f"{player_id} did not start playing"
            raise ScenarioError(msg)

    async def command(self, command: Coroutine[Any, Any, _T]) -> asyncio.Task[_T]:
        """
        Start a command in the background and settle; await the task once it is done.

        Run every server call that may wait for the clock (a timeout, a delay, a device to come
        online) this way: the clock only moves through advance(), so awaiting such a call
        directly never returns. A failure of a command that is never awaited fails the scenario.

        :param command: The command, e.g. ``scenario.mass.players.cmd_power(player_id, True)``.
        """
        task = asyncio.create_task(command)
        self._commands.add(task)
        await self.settle()
        return task

    async def advance(self, seconds: float) -> None:
        """
        Move the clock forward, stopping at every timer that falls due on the way.

        time.monotonic() and time.time() of the server code follow the clock.

        :param seconds: Seconds to pass.
        """
        target = self._clock.now + seconds
        await self.settle()
        while (due := _next_timer()) is not None and due <= target:
            self._clock.now = max(self._clock.now, due)
            await self.settle()
        self._clock.now = target
        await self.settle()

    async def idle(self) -> None:
        """
        Let the follow-up work on short timers run (debounces), for an assertion of steady state.

        Raises ScenarioError when the server still has work due within IDLE_HORIZON after it, or
        a protocol that waits for its player at all, which a scenario lets happen with advance().
        """
        await self.advance(IDLE_HORIZON)
        limit = self._clock.now + IDLE_HORIZON
        pending = [
            task_id
            for task_id, handle in self.mass._tracked_timers.items()
            if not handle.cancelled()
            and handle.when() <= limit
            and not task_id.startswith(BACKGROUND_TIMERS)
        ]
        # the wait of a protocol for its identifiers (15 s) is a plain loop timer of the controller
        pending += [
            f"protocol evaluation of {protocol_id}"
            for protocol_id, handle in self.mass.players._pending_protocol_evaluations.items()
            if not handle.cancelled()
        ]
        if pending:
            msg = (
                f"Not idle after {IDLE_HORIZON} s, still pending: {sorted(pending)}; use advance()"
            )
            raise ScenarioError(msg)

    async def restart(self) -> None:
        """Stop the server and start a new one on the same storage (settings.json and cache)."""
        await self._shutdown()
        await self._boot()

    async def seed_config(self, values: dict[str, Any]) -> None:
        """
        Restart the server on its stored config, with the given values merged in.

        :param values: Nested settings.json content, e.g. ``{"players": {player_id: {...}}}``;
            a None value deletes the key.
        """
        await self._shutdown()
        data = json.loads(self._settings_file.read_text()) if self._settings_file.exists() else {}
        _merge(data, values)
        self._settings_file.write_text(json.dumps(data))
        await self._boot()

    async def seed_pre_split_config(self, players: dict[str, dict[str, Any]]) -> None:
        """
        Restart the server on player configs as a version from before the player split left them.

        The stored config is that of a server that never split its player configs: the
        given configs are merged into its players, and it holds no protocol configs.

        :param players: Stored player configs in the shape from before the split, by player id.
        """
        await self.seed_config(
            {
                CONF_PLAYERS: players,
                CONF_PLAYER_PROTOCOLS: None,
                CONF_PLAYER_SPLIT_SESSION_ONLY: None,
                CONF_PLAYER_SPLIT_TREE: None,
            }
        )

    async def settle(self) -> None:
        """
        Run what is due now until the loop is quiet, without moving the clock.

        Quiet: nothing runnable, no timer due and no work in a thread. Raises ScenarioError
        when the loop does not get quiet.
        """
        deadline = _real_monotonic() + SETTLE_TIMEOUT
        while True:
            await asyncio.sleep(0)
            if not (runnable := _runnable()) and not self._threads_busy:
                return
            if _real_monotonic() > deadline:
                msg = (
                    f"The loop did not get quiet in {SETTLE_TIMEOUT} s: {runnable} runnable, "
                    f"{self._threads_busy} thread jobs"
                )
                raise ScenarioError(msg)
            if not runnable:
                # only thread work is left, which reports back through the loop: nothing on
                # the loop can run meanwhile, and a loop timer would wait for the clock
                time.sleep(0.001)  # noqa: ASYNC251

    def wall_time(self) -> float:
        """Return the wall clock time as the server reads it (time.time()), for reported state."""
        return self._clock.wall_time()

    def protocol(self, protocol_id: str) -> FakeProtocol:
        """
        Return the protocol object of a device on the running server and provider instance.

        :param protocol_id: ID of the protocol object of the device.
        """
        device = self._devices[protocol_id]
        return self.provider(device.provider_id)._protocol_for(protocol_id)

    def provider(self, instance_id_or_domain: str) -> FakePlayerProvider:
        """
        Return a loaded fake provider, which after a restart or reload is a new instance.

        :param instance_id_or_domain: Instance id, or the domain of a provider with one instance.
        """
        provider = self.mass.get_provider(instance_id_or_domain)
        if provider is None:
            msg = f"No provider {instance_id_or_domain} is loaded"
            raise ScenarioError(msg)
        if not isinstance(provider, FakePlayerProvider):
            msg = f"{instance_id_or_domain} is no fake provider"
            raise ScenarioError(msg)
        return provider

    def visible_players(self) -> list[VisiblePlayer]:
        """Return the players clients see (`players/all`), ordered by name and id."""
        players = [self._visible(state) for state in self.mass.players.all_player_states()]
        return sorted(players, key=lambda player: (player.name, player.player_id))

    def players_carrying(self, *protocols: FakeProtocol) -> list[VisiblePlayer]:
        """
        Return the visible players that carry any of the given protocols.

        A player carries the protocols it lists as belonging to it.

        :param protocols: The protocol objects of one device.
        """
        protocol_ids = {protocol.protocol_id for protocol in protocols}
        return [
            self._visible(state)
            for state in self.mass.players.all_player_states()
            if protocol_ids & _carried_ids(state)
        ]

    def player_of(self, protocol: FakeProtocol) -> str | None:
        """
        Return the id of the player a protocol is attached to.

        None when the protocol is attached to no player: it is detached, or waits for the
        identifiers that tell which player it belongs to.

        :param protocol: The protocol object of a device.
        """
        attached = self.mass.players.get_protocol(protocol.protocol_id)
        if attached is None or attached.player is None:
            return None
        return attached.player.player_id

    def single_player(self, *protocols: FakeProtocol) -> VisiblePlayer:
        """
        Return the one visible player carrying the given protocols, a precondition of a scenario.

        Raises ScenarioError when there is not exactly one; where that is what the scenario is
        about, assert on players_carrying() in the test instead.

        :param protocols: The protocol objects of one device.
        """
        players = self.players_carrying(*protocols)
        if len(players) != 1 or any(self.players_carrying(p) != players for p in protocols):
            ids = sorted(protocol.protocol_id for protocol in protocols)
            msg = f"Expected one player carrying all of {ids}: {players}"
            raise ScenarioError(msg)
        return players[0]

    def player(self, player_id: str) -> PlayerState | None:
        """
        Return the state of a player as clients see it.

        :param player_id: ID of the player.
        """
        return self.mass.players.get_player_state(player_id)

    async def player_config(self, player_id: str) -> PlayerConfig | None:
        """
        Return the stored config of a player as clients read it, None when none is stored.

        :param player_id: ID of the player.
        """
        if self.mass.config.get(f"{CONF_PLAYERS}/{player_id}") is None:
            return None
        return await self.mass.config.get_player_config(player_id)

    async def player_configs(self) -> list[PlayerConfig]:
        """Return the stored player configs as the settings page lists them, by player id."""
        configs = await self.mass.config.get_player_configs()
        return sorted(configs, key=lambda config: config.player_id)

    def protocol_config(self, protocol_id: str) -> dict[str, Any] | None:
        """
        Return the stored config values of a protocol object by key, None when none is stored.

        :param protocol_id: ID of the protocol object.
        """
        if (stored := self.mass.config.get(f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}")) is None:
            return None
        return dict(stored.get("values") or {})

    async def stored_config_mentioning(self, text: str) -> list[str]:
        """
        Return every key and value of the stored config (settings.json) that mentions a text.

        A key as its path (``players/x``), a value as ``path = value``; for asserting that
        nothing of a player or protocol is persisted, wherever it would be stored.

        :param text: The text to look for, e.g. a protocol id.
        """
        await self.mass.config.async_save()
        return _mentions(json.loads(self._settings_file.read_text()), text)

    def queue(self, queue_id: str) -> PlayerQueue | None:
        """
        Return the queue of a player.

        :param queue_id: ID of the queue, which is the id of its player.
        """
        return self.mass.player_queues.get(queue_id)

    async def saved_queue(self, queue_id: str) -> list[str] | None:
        """
        Return the URIs of the saved items of a queue, None when none were ever saved.

        The saved items are what the queue is restored from after a restart; a queue that never
        held an item has none. A pending (debounced) save is written first.

        :param queue_id: ID of the queue, which is the id of its player.
        """
        self.mass.cancel_timer(f"save_queue_cache_{queue_id}")
        await self.mass.player_queues._save_queue_to_cache(queue_id)
        items = await self.mass.cache.get(
            queue_id,
            provider=self.mass.player_queues.domain,
            category=CACHE_CATEGORY_PLAYER_QUEUE_ITEMS,
        )
        if items is None:
            return None
        return [QueueItem.from_cache(dict(item)).uri for item in items]

    def events(self, *event_types: EventType, object_id: str | None = None) -> list[Event]:
        """
        Return the events emitted since the start or the last clear_events(), in order.

        The events of each boot are included. Consecutive repeats of an update event
        (``*_UPDATED``) for one object are collapsed, so the result reads as a sequence of
        transitions; other events are all listed.

        :param event_types: Only return events of these types (all when omitted).
        :param object_id: Only return events for this object (player id, queue id, ...).
        """
        result: list[Event] = []
        for event in self._events:
            if event_types and event.type not in event_types:
                continue
            if object_id is not None and event.object_id != object_id:
                continue
            if result and result[-1] == event and event.type.value.endswith("_updated"):
                continue
            result.append(event)
        return result

    def removals(self) -> list[tuple[str | None, Any]]:
        """
        Return the player id and the data of every PLAYER_REMOVED event, in order.

        Since the start or the last clear_events(), the events of each boot included.
        """
        return list(self._removals)

    def availability_updates(self, player_id: str) -> list[bool]:
        """
        Return the availability every PLAYER_UPDATED event of a player carried, in order.

        Since the start or the last clear_events(), the events of each boot included.

        :param player_id: ID of the player.
        """
        return [state.available for state in self.player_updates(player_id)]

    def player_updates(self, player_id: str) -> list[PlayerState]:
        """
        Return the state every PLAYER_UPDATED event of a player carried, in order.

        Since the start or the last clear_events(), the events of each boot included.

        :param player_id: ID of the player.
        """
        return [state for object_id, state in self._player_updates if object_id == player_id]

    def clear_events(self) -> None:
        """Forget the events emitted so far."""
        self._events.clear()
        self._removals.clear()
        self._player_updates.clear()

    @property
    def _settings_file(self) -> pathlib.Path:
        """The stored config of the server."""
        return self._tmp_path / "data" / "settings.json"

    async def _boot(self) -> None:
        """Start a server on the scenario's storage, with the power controls registered."""
        self._server = contextlib.AsyncExitStack()
        self.mass = await self._server.enter_async_context(full_mass_context(self._tmp_path))
        # modules a boot imported read the clock as well
        self._patches.enter_context(_patch_module_clocks(self._clock))
        for control in self._power_controls.values():
            await self._register_power_control(control)
        await self.settle()

    async def _shutdown(self) -> None:
        """Stop the running server, if any, and report what of it is left running."""
        # stopping waits on timeouts of its own, so real time runs, but the clock does not move
        with self._clock.running():
            await self._cancel_commands()
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            if self._server is None:
                return
            server, self._server = self._server, None
            await server.aclose()
        # stop() leaves the server's own (tracked) timers armed, e.g. a daily one
        for handle in self.mass._tracked_timers.values():
            handle.cancel()
        # tasks first: their cleanup may arm timers of its own
        await self._stop_leftover_tasks()
        await self._wait_for_thread_work()
        loop: Any = asyncio.get_running_loop()
        for handle in loop._scheduled:
            if not handle.cancelled():
                self._errors.messages.append(f"Timer left by the stopped server: {handle}")
                handle.cancel()

    async def _stop_leftover_tasks(self) -> None:
        """Cancel the tasks the stopped server left, record them and let their cleanup run."""
        current = asyncio.current_task()
        if not (tasks := [task for task in asyncio.all_tasks() if task is not current]):
            return
        for task in tasks:
            self._errors.messages.append(f"Task left by the stopped server: {task}")
            task.cancel()
        with self._clock.running():
            done, still_running = await asyncio.wait(tasks, timeout=SETTLE_TIMEOUT)
        for task in done:
            if not task.cancelled() and (error := task.exception()) is not None:
                self._errors.messages.append(f"Task failed while it was cancelled: {error!r}")
        for task in still_running:
            self._errors.messages.append(f"Task did not stop in {SETTLE_TIMEOUT} s: {task}")

    async def _wait_for_thread_work(self) -> None:
        """Let thread work of the stopped server finish, so it cannot touch the next boot."""
        deadline = _real_monotonic() + SETTLE_TIMEOUT
        with self._clock.running():
            while self._threads_busy and _real_monotonic() < deadline:
                await asyncio.sleep(0.005)
        if self._threads_busy:
            self._errors.messages.append(
                f"{self._threads_busy} thread jobs still running {SETTLE_TIMEOUT} s after stop"
            )

    async def _cancel_commands(self) -> None:
        """Cancel the commands still running and record the failures nobody awaited."""
        running = [task for task in self._commands if not task.done()]
        for task in self._commands - set(running):
            if not task.cancelled() and getattr(task, "_log_traceback", False):
                self._errors.messages.append(f"Command {task.get_coro()!r}: {task.exception()!r}")
        for task in running:
            task.cancel()
        for task, result in zip(
            running, await asyncio.gather(*running, return_exceptions=True), strict=True
        ):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                self._errors.messages.append(f"Command {task.get_coro()!r}: {result!r}")
        self._commands.clear()

    async def _register_power_control(self, control: FakePowerControl) -> None:
        """Register a power control on the running server."""
        player_control = PlayerControl(
            id=control.control_id,
            provider=POWER_CONTROL_PROVIDER,
            name=control.name,
            supports_power=True,
            power_state=control.powered,
            power_on=partial(self._handle_power_command, control, True),
            power_off=partial(self._handle_power_command, control, False),
        )
        await self.mass.players.register_or_update_player_control(player_control)

    async def _handle_power_command(self, control: FakePowerControl, powered: bool) -> None:
        """Record a power command of a control and report it switched."""
        control.commands.append(powered)
        self._set_power(control, powered)

    def _set_power(self, control: FakePowerControl, powered: bool) -> None:
        """Set the power state of a control and let the players that use it know."""
        control.powered = powered
        if player_control := self.mass.players.get_player_control(control.control_id):
            player_control.power_state = powered
        self.mass.players.update_player_control(control.control_id)

    def _check_current(self, protocol: FakeProtocol) -> None:
        """Refuse a protocol object of a server or provider instance that is gone."""
        if self.mass.get_provider(protocol.provider.instance_id) is not protocol.provider:
            msg = (
                f"{protocol.protocol_id} belongs to a server or provider instance that is gone, "
                f"use scenario.protocol({protocol.protocol_id!r})"
            )
            raise ScenarioError(msg)

    def _on_event(self, event: MassEvent) -> None:
        """Capture an event the server emitted."""
        self._events.append(Event(event.event, event.object_id))
        if event.event == EventType.PLAYER_REMOVED:
            self._removals.append((event.object_id, event.data))
        elif event.event == EventType.PLAYER_UPDATED:
            state = event.data.state if isinstance(event.data, Player) else event.data
            self._player_updates.append((event.object_id, state))

    def _visible(self, state: PlayerState) -> VisiblePlayer:
        """Return the visible record of a player state."""
        provider = self.mass.get_provider(state.provider, return_unavailable=True)
        return VisiblePlayer(
            player_id=state.player_id,
            name=state.name,
            available=state.available,
            powered=state.powered,
            provider=provider.domain if provider else state.provider,
            outputs=tuple(
                sorted(
                    (output.protocol_domain, output.available) for output in state.output_protocols
                )
            ),
        )

    @contextlib.contextmanager
    def _patch_environment(self) -> Iterator[None]:
        """
        Patch what a scenario fakes (the player providers, ARP lookups and the clock) and watch.

        Two private pieces are touched. The manifest loading of the server: before any provider
        loads, a fake domain needs a manifest without requirements, dependencies or network
        discovery; it is also the earliest point of a boot
        with the new instance at hand, so the events of the boot itself are captured. And the
        thread dispatch of aiosqlite, which settle() needs to know about, as it does not go
        through the loop's executor. The levels the server sets on loggers for the whole process
        (such as those of the discovery libraries) are set back at the end.
        """
        fake_domains: set[str] = set()
        load_manifests = MusicAssistant._MusicAssistant__load_provider_manifests  # type: ignore[attr-defined]
        unload_provider = MusicAssistant.unload_provider
        sqlite_execute: Any = SqliteConnection._execute
        loop = asyncio.get_running_loop()
        run_in_executor = loop.run_in_executor

        async def _load_manifests(mass: MusicAssistant) -> None:
            self._unsubscribe = mass.subscribe(self._on_event)
            await load_manifests(mass)
            manifests = mass._provider_manifests
            for domain, manifest in list(manifests.items()):
                if manifest.type == ProviderType.PLAYER and domain not in REAL_PLAYER_PROVIDERS:
                    manifests[domain] = _fake_manifest(manifest)
                    fake_domains.add(domain)
            # a provider that depends on a fake one (e.g. Sendspin source) would load against it
            for domain, manifest in list(manifests.items()):
                if manifest.depends_on in fake_domains:
                    del manifests[domain]

        async def _setup_provider(
            mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
        ) -> FakePlayerProvider:
            return FakePlayerProvider(mass, manifest, config, self._devices, self.timeline)

        async def _load_module(manifest: ProviderManifest) -> Any:
            if manifest.domain in fake_domains:
                return SimpleNamespace(setup=_setup_provider)
            return await load_provider_module(manifest)

        async def _arp_lookup(ip_address: str) -> str | None:
            return self.arp.get(ip_address)

        async def _unload_provider(mass: MusicAssistant, *args: Any, **kwargs: Any) -> None:
            # the server stops its providers in a gather that drops their errors
            try:
                await unload_provider(mass, *args, **kwargs)
            except Exception as err:
                self._errors.messages.append(f"Unloading provider {args}: {err!r}")
                raise

        def _thread_done(_future: asyncio.Future[Any]) -> None:
            self._threads_busy -= 1

        def _track(operation: asyncio.Future[_T]) -> asyncio.Future[_T]:
            # a cancelled caller leaves the thread running, so it counts until it is done
            self._threads_busy += 1
            operation.add_done_callback(_thread_done)
            return asyncio.shield(operation)

        def _run_in_executor(executor: Any, func: Callable[..., Any], *args: Any) -> Any:
            return _track(run_in_executor(executor, func, *args))

        async def _sqlite_execute(connection: SqliteConnection, *args: Any, **kwargs: Any) -> Any:
            return await _track(asyncio.ensure_future(sqlite_execute(connection, *args, **kwargs)))

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    MusicAssistant, "_MusicAssistant__load_provider_manifests", _load_manifests
                )
            )
            stack.enter_context(patch("music_assistant.mass.load_provider_module", _load_module))
            stack.enter_context(patch.object(MusicAssistant, "unload_provider", _unload_provider))
            stack.enter_context(patch("music_assistant.helpers.util.get_mac_address", _arp_lookup))
            stack.enter_context(patch.object(StreamsAudio, "get_audio_buffer", _no_audio_buffer))
            stack.enter_context(patch.object(SqliteConnection, "_execute", _sqlite_execute))
            stack.enter_context(patch.object(loop, "run_in_executor", _run_in_executor))
            stack.enter_context(patch.object(loop, "time", self._clock.time))
            stack.enter_context(patch("asyncio.events.TimerHandle", _ArmedTimerHandle))
            stack.enter_context(_patch_module_clocks(self._clock))
            stack.enter_context(_restoring_logger_levels())
            stack.enter_context(self._errors.watching())
            yield


def xfail_origin_error(test_path: pathlib.Path, call: pytest.CallInfo[None]) -> str | None:
    """
    Return why an expected failure does not count, or None when it does.

    It counts only when it is raised in the call phase by the test module itself and is no
    ScenarioError, so a failure in the harness, in a fixture or in the server can not pass as the
    expected one.

    :param test_path: Path of the test module.
    :param call: The call info of the test phase that failed.
    """
    if call.excinfo is None:
        return None
    origin = call.excinfo.traceback[-1]
    if (
        call.when == "call"
        and str(origin.path) == str(test_path)
        and not call.excinfo.errisinstance(ScenarioError)
    ):
        return None
    return (
        f"[xfail for the wrong reason] {call.excinfo.typename} in {call.when} at "
        f"{origin.path}:{origin.lineno + 1}: {call.excinfo.value}"
    )


@dataclass
class _Device:
    """What a device reports, kept across restarts, and whether its provider can find it."""

    provider_id: str
    # reported state by attribute name, as taken by Scenario.report()
    state: dict[str, Any]
    identifiers: dict[IdentifierType, str] = field(default_factory=dict)
    session_only: bool = False
    present: bool = False


class _VirtualClock:
    """The clock of a scenario: it stands still, except while real time is let through."""

    def __init__(self) -> None:
        """Initialize the clock at its start time."""
        self.now = CLOCK_START
        self._wall_start = time.time()
        self._running_since: float | None = None

    def time(self) -> float:
        """Return the current time, as loop.time() and time.monotonic() of the server."""
        if self._running_since is None:
            return self.now
        return self.now + _real_monotonic() - self._running_since

    def wall_time(self) -> float:
        """Return the current wall clock time, as time.time() of the server."""
        return self._wall_start + self.time() - CLOCK_START

    @contextlib.contextmanager
    def running(self) -> Iterator[None]:
        """Let real time pass in this context; the clock is back where it stood after it."""
        self._running_since = _real_monotonic()
        try:
            yield
        finally:
            self._running_since = None


class _ClockedTime:
    """The time module as the server code sees it: monotonic() and time() read the clock."""

    def __init__(self, clock: _VirtualClock) -> None:
        """
        Initialize the module stand-in.

        :param clock: The clock to read.
        """
        self.monotonic = clock.time
        self.time = clock.wall_time

    def __getattr__(self, name: str) -> Any:
        """Return everything else from the time module."""
        return getattr(time, name)


class _ErrorLog(logging.Handler):
    """Collects the warnings (and worse) of the server and the errors of asyncio."""

    def __init__(self) -> None:
        """Initialize the handler."""
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep a record that tells of a failure."""
        if record.name.startswith("asyncio") and record.levelno < logging.ERROR:
            return
        self.messages.append(f"{record.levelname} {record.name}: {record.getMessage()}")

    @contextlib.contextmanager
    def watching(self) -> Iterator[None]:
        """Collect the records of the server and asyncio loggers in this context."""
        loggers = [logging.getLogger(name) for name in ("music_assistant", "asyncio")]
        for logger in loggers:
            logger.addHandler(self)
        try:
            yield
        finally:
            for logger in loggers:
                logger.removeHandler(self)


class _ArmedTimerHandle(asyncio.TimerHandle):
    """A loop timer that runs after the timers armed before it for the same time."""

    __slots__ = ("_armed",)
    _arming = itertools.count()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the timer as the next one armed."""
        super().__init__(*args, **kwargs)
        self._armed = next(self._arming)

    def __lt__(self, other: object) -> bool:
        """Order by due time, then by arming order."""
        if not isinstance(other, asyncio.TimerHandle):
            return NotImplemented
        return (self.when(), self._armed) < (other.when(), getattr(other, "_armed", -1))


@contextlib.contextmanager
def _patch_module_clocks(clock: _VirtualClock) -> Iterator[None]:
    """Let every loaded server module read the time from the clock."""
    clocked = _ClockedTime(clock)
    stand_ins = ((time, clocked), (time.time, clocked.time), (time.monotonic, clocked.monotonic))
    with contextlib.ExitStack() as stack:
        for module_name, module in list(sys.modules.items()):
            if not module_name.startswith("music_assistant") or not isinstance(module, ModuleType):
                continue
            for name in ("time", "monotonic"):
                value = getattr(module, name, None)
                for real, stand_in in stand_ins:
                    if value is real:
                        stack.enter_context(patch.object(module, name, stand_in))
        yield


async def _no_audio_buffer(*_args: Any, **kwargs: Any) -> None:
    """
    Decode nothing ahead of playback: no fake protocol fetches audio, so ffmpeg never runs.

    Serving audio to a protocol is not supported in a scenario and fails loudly.
    """
    if kwargs.get("reason") == "streaming":
        msg = "a scenario cannot serve audio to a protocol"
        raise ScenarioError(msg)


def _runnable() -> int:
    """Return how many callbacks the loop can run now, due timers included."""
    loop: Any = asyncio.get_running_loop()
    # the rule the loop itself applies to its timers
    end_time = loop.time() + loop._clock_resolution
    due = sum(
        1 for handle in loop._scheduled if not handle.cancelled() and handle.when() < end_time
    )
    return len(loop._ready) + due


def _next_timer() -> float | None:
    """Return when the next timer of the loop falls due, if there is one."""
    loop: Any = asyncio.get_running_loop()
    return min(
        (handle.when() for handle in loop._scheduled if not handle.cancelled()), default=None
    )


def _fake_manifest(manifest: ProviderManifest) -> ProviderManifest:
    """Return the manifest of a fake domain, based on the real one."""
    return replace(
        manifest,
        requirements=[],
        builtin=False,
        allow_disable=True,
        depends_on=None,
        mdns_discovery=None,
        upnp_discovery=None,
        has_setup_flow=False,
    )


def _carried_ids(state: PlayerState) -> set[str]:
    """Return the ids of the protocol objects a player state stands for."""
    return {protocol.protocol_id for protocol in state.protocols}


def _mentions(value: Any, text: str, path: str = "") -> list[str]:
    """Return the key paths and the values below a stored value that mention a text."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}/{key}" if path else str(key)
            if text in str(key):
                found.append(child_path)
            found += _mentions(child, text, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found += _mentions(child, text, f"{path}[{index}]")
    elif isinstance(value, str) and text in value:
        found.append(f"{path} = {value!r}")
    return found


def _merge(target: dict[str, Any], values: dict[str, Any]) -> None:
    """Merge nested values into a dict in place; a None value deletes the key."""
    for key, value in values.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge(target[key], value)
        else:
            target[key] = value


@contextlib.contextmanager
def _restoring_logger_levels() -> Iterator[None]:
    """Give every logger whose level the code inside changes its old level back at the end."""
    before = _logger_levels()
    try:
        yield
    finally:
        for name, level in _logger_levels().items():
            if level != (old_level := before.get(name, logging.NOTSET)):
                logging.getLogger(name).setLevel(old_level)


def _logger_levels() -> dict[str, int]:
    """Return the level of the root logger (under "") and of every other logger, by name."""
    levels = {
        name: logger.level
        for name, logger in logging.Logger.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    levels[""] = logging.getLogger().level
    return levels
