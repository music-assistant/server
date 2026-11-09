"""
Scenario harness for the player lifecycle: fake player providers on a real server.

A scenario boots a real MusicAssistant on a temporary storage folder, loads fake player
providers that impersonate real provider domains, and drives it through the entry points a
real provider or client uses. See README.md in this folder.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field, replace
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeVar
from unittest.mock import patch

from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.player import DeviceInfo
from music_assistant_models.player_control import PlayerControl

from music_assistant.helpers.util import load_provider_module
from music_assistant.mass import MusicAssistant
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.models.player_provider import PlayerProvider
from tests.conftest import full_mass_context

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable, Coroutine, Iterator

    from music_assistant_models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.player import Player as PlayerState
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models.player import PlayerMedia

_T = TypeVar("_T")

# Player providers that keep their real implementation, as they own no device.
REAL_PLAYER_PROVIDERS = frozenset({"universal_player", "universal_group", "sync_group"})

# Modules of the players code whose time.monotonic() follows the loop clock.
MONOTONIC_MODULES = (
    "music_assistant.controllers.players.controller",
    "music_assistant.providers.sync_group.player",
)

# The provider instance that owns the fake power controls: one that is always loaded.
POWER_CONTROL_PROVIDER = "builtin"

# Seconds of loop time per step of advance(): fine enough to keep the order of the server's
# debounce timers (0.25 s and up).
TICK = 0.1

# Real seconds settle() waits for work that does not wait for the clock, before it fails.
SETTLE_TIMEOUT = 5.0

DEFAULT_FEATURES = frozenset(
    {
        PlayerFeature.PLAY_MEDIA,
        PlayerFeature.PAUSE,
        PlayerFeature.VOLUME_SET,
        PlayerFeature.VOLUME_MUTE,
    }
)

type Role = Literal["native", "audio"]

# Today the role of a protocol is its player type. Kept in this one place so the scenarios
# stay valid when the role becomes an attribute of its own.
_ROLE_TYPES: dict[Role, PlayerType] = {
    "native": PlayerType.PLAYER,
    "audio": PlayerType.PROTOCOL,
}


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
    An object belongs to the server and provider instance it was created on; after a restart
    or a provider reload, Scenario.protocol() returns the current one.
    """

    def __init__(self, provider: FakePlayerProvider, protocol_id: str, device: _Device) -> None:
        """
        Initialize the protocol object of a device; use FakePlayerProvider.protocol().

        :param provider: The fake provider this protocol belongs to.
        :param protocol_id: ID of the protocol object (the player id it registers with).
        :param device: What the device reports, kept across restarts.
        """
        super().__init__(provider, protocol_id)
        # every command received, as a tuple of the command name and its arguments
        self.commands: list[tuple[Any, ...]] = []
        self._device = device
        self._attr_supported_features = set(DEFAULT_FEATURES)
        self._attr_device_info = DeviceInfo(model=f"Fake {provider.domain}", manufacturer="Fake")
        self._set_reported(identifiers=dict(device.identifiers), **device.state)

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
        """Handle a set members command."""
        members = dict.fromkeys(self._attr_group_members)
        members.update(dict.fromkeys(player_ids_to_add or []))
        for player_id in player_ids_to_remove or []:
            members.pop(player_id, None)
        others = [player_id for player_id in members if player_id != self.player_id]
        self._apply(
            "set_members",
            player_ids_to_add,
            player_ids_to_remove,
            group_members=[self.player_id, *others] if others else [],
        )

    def _apply(self, command: str, *args: Any, **state: Any) -> None:
        """Record a command and report the state it leads to."""
        self.commands.append((command, *args))
        if state:
            self._set_reported(**state)
            self.update_state()

    def _set_reported(self, **state: Any) -> None:
        """Set reported state without publishing it, and keep it for the device."""
        for key, value in state.items():
            if key == "identifiers":
                for identifier_type, identifier in value.items():
                    self._attr_device_info.add_identifier(identifier_type, identifier)
                    if identifier:
                        self._device.identifiers[identifier_type] = identifier
                    else:
                        self._device.identifiers.pop(identifier_type, None)
                continue
            if not hasattr(self, f"_attr_{key}"):
                msg = f"{type(self).__name__} reports no state named {key!r}"
                raise AttributeError(msg)
            setattr(self, f"_attr_{key}", value)
            self._device.state[key] = value


class FakePlayerProvider(PlayerProvider):
    """
    A player provider of an impersonated domain, owning the FakeProtocols a test creates.

    Like a real provider, it attaches the devices that are present when it loads and when one
    of its players gets enabled.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        devices: dict[str, _Device],
    ) -> None:
        """
        Initialize the provider.

        :param mass: The server this provider instance runs on.
        :param manifest: The (fake) manifest of the impersonated domain.
        :param config: The provider instance config.
        :param devices: The devices of the scenario, by protocol id.
        """
        super().__init__(mass, manifest, config, {ProviderFeature.REMOVE_PLAYER})
        self._devices = devices
        self._protocols: dict[str, FakeProtocol] = {}

    def protocol(
        self,
        protocol_id: str,
        name: str,
        *,
        role: Role = "native",
        identifiers: dict[IdentifierType, str] | None = None,
        **state: Any,
    ) -> FakeProtocol:
        """
        Create a new device with its protocol object of this provider; attach it to use it.

        :param protocol_id: ID of the protocol object (the player id it registers with).
        :param name: The name the device reports.
        :param role: ``"native"`` for the vendor's own control plane, ``"audio"`` for an audio
            protocol of a device (AirPlay, Cast, DLNA, ...).
        :param identifiers: The device identifiers it reports (MAC, IP, UUID, ...).
        :param state: Other reported state, as taken by Scenario.report().
        """
        self._devices[protocol_id] = _Device(
            provider_id=self.instance_id,
            state={"name": name, "type": _ROLE_TYPES[role], **state},
            identifiers=dict(identifiers or {}),
        )
        self._protocols.pop(protocol_id, None)
        return self._protocol_for(protocol_id)

    async def discover_players(self) -> None:
        """Attach the present devices of this provider that are not attached."""
        for protocol_id, device in self._devices.items():
            if device.provider_id != self.instance_id or not device.present:
                continue
            if self.mass.players.get_protocol(protocol_id) is None:
                await self.mass.players.register_or_update(self._protocol_for(protocol_id))

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from this provider."""
        await self.mass.players.unregister(player_id, permanent=True)

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

    Every action waits until the server finished the work it started, so its outcome can be
    asserted right after. The event loop clock stands still, also while the server works, and
    only moves through advance().
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
        self._events: list[Event] = []
        self._devices: dict[str, _Device] = {}
        self._power_controls: dict[str, FakePowerControl] = {}
        self._commands: set[asyncio.Task[Any]] = set()
        self._patches = contextlib.ExitStack()
        self._server: contextlib.AsyncExitStack | None = None
        self._clock: _LoopClock

    async def start(self) -> None:
        """Boot the server."""
        self._patches.enter_context(self._patch_environment())
        await self._boot()

    async def close(self) -> None:
        """Stop the server and undo what the scenario patched."""
        try:
            for task in self._commands:
                task.cancel()
            await asyncio.gather(*self._commands, return_exceptions=True)
            await self._shutdown()
        finally:
            self._patches.close()

    async def add_provider(self, domain: str) -> FakePlayerProvider:
        """
        Add and load a new instance of a fake player provider.

        :param domain: The player provider domain to impersonate, e.g. ``"airplay"``.
        """
        if domain in REAL_PLAYER_PROVIDERS:
            msg = f"{domain} keeps its real implementation"
            raise ValueError(msg)
        if self.mass.get_provider_manifest(domain).type != ProviderType.PLAYER:
            msg = f"{domain} is no player provider"
            raise ValueError(msg)
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
            protocol._set_reported(type=_ROLE_TYPES[role])
        protocol._device.present = True
        await self.mass.players.register_or_update(protocol)
        await self.settle()

    async def detach(self, protocol: FakeProtocol) -> None:
        """
        Detach a protocol object: its provider lost the device.

        :param protocol: The protocol object to detach.
        """
        self._check_current(protocol)
        protocol._device.present = False
        await self.mass.players.unregister(protocol.player_id, permanent=False)
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
        if "type" in state:
            msg = "a role changes with attach(protocol, role=...)"
            raise ValueError(msg)
        protocol._set_reported(**state)
        protocol.update_state()
        await self.settle()

    async def remove(self, player_id: str) -> None:
        """
        Remove a player the way a user does.

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

    async def command(self, command: Coroutine[Any, Any, _T]) -> asyncio.Task[_T]:
        """
        Start a command in the background and let it run until it waits; await the task after.

        For a command that waits for the clock or a device, e.g. a power on through an external
        power control. settle() waits for the work the command starts, not for the command.

        :param command: The command, e.g. ``scenario.mass.players.cmd_power(player_id, True)``.
        """
        task = asyncio.create_task(command)
        self._commands.add(task)
        await self.settle()
        return task

    async def advance(self, seconds: float) -> None:
        """
        Move the loop clock forward, running every timer that falls due on the way.

        The players code's time.monotonic() follows the loop clock; the wall clock
        (``time.time()``) does not move.

        :param seconds: Seconds to pass.
        """
        target = self._clock.time() + seconds
        while (now := self._clock.time()) < target:
            self._clock.advance(min(TICK, target - now))
            await self.settle()

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
        settings_file = self._tmp_path / "data" / "settings.json"
        data = json.loads(settings_file.read_text()) if settings_file.exists() else {}
        _merge(data, values)
        settings_file.write_text(json.dumps(data))
        await self._boot()

    async def settle(self) -> None:
        """
        Let the server finish the work that is due now, without moving the clock.

        Raises a RuntimeError when work that does not wait for the clock keeps running.
        """
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + SETTLE_TIMEOUT
        quiet_checks = 0
        while quiet_checks < 2:
            # two passes per check: what a callback hands to call_soon runs on the next one
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if not (busy := self._busy_tasks()):
                quiet_checks += 1
                continue
            quiet_checks = 0
            if time.monotonic() > deadline:
                msg = (
                    f"Still running after {SETTLE_TIMEOUT} s without waiting for the clock: {busy}"
                )
                raise RuntimeError(msg)
            # the loop clock stands still, so a real pause bounds the wait, not a loop timeout
            pause = loop.run_in_executor(None, time.sleep, 0.005)
            await asyncio.wait({*busy, pause}, return_when=asyncio.FIRST_COMPLETED)

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
            raise LookupError(msg)
        if not isinstance(provider, FakePlayerProvider):
            msg = f"{instance_id_or_domain} is no fake provider"
            raise TypeError(msg)
        return provider

    def visible_players(self) -> list[VisiblePlayer]:
        """Return the players clients see (`players/all`), ordered by name and id."""
        players = [self._visible(state) for state in self.mass.players.all_player_states()]
        return sorted(players, key=lambda player: (player.name, player.player_id))

    def player(self, player_id: str) -> PlayerState | None:
        """
        Return the state of a player as clients see it.

        :param player_id: ID of the player.
        """
        return self.mass.players.get_player_state(player_id)

    async def player_config(self, player_id: str) -> PlayerConfig | None:
        """
        Return the stored config of a player, as clients read it.

        :param player_id: ID of the player.
        """
        try:
            return await self.mass.config.get_player_config(player_id)
        except KeyError:
            return None

    def queue(self, queue_id: str) -> PlayerQueue | None:
        """
        Return the queue of a player.

        :param queue_id: ID of the queue, which is the id of its player.
        """
        return self.mass.player_queues.get(queue_id)

    def events(self, *event_types: EventType, object_id: str | None = None) -> list[Event]:
        """
        Return the events emitted since the start or the last clear_events(), in order.

        Consecutive repeats of an update event (``*_UPDATED``) for one object are collapsed,
        so the result reads as a sequence of transitions; other events are all listed.

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

    def clear_events(self) -> None:
        """Forget the events emitted so far."""
        self._events.clear()

    def assert_single_player(self, *protocols: FakeProtocol) -> VisiblePlayer:
        """
        Assert that exactly one visible player carries the given protocols, and return it.

        A player carries a protocol when it is that protocol or lists it as an output.

        :param protocols: The protocol objects of one device.
        """
        protocol_ids = {protocol.player_id for protocol in protocols}
        carriers = [
            state
            for state in self.mass.players.all_player_states()
            if protocol_ids & _carried_ids(state)
        ]
        found = [self._visible(state) for state in carriers]
        assert len(carriers) == 1, f"expected one player for {sorted(protocol_ids)}: {found}"
        assert protocol_ids <= _carried_ids(carriers[0]), (
            f"{carriers[0].player_id} does not carry all of {sorted(protocol_ids)}"
        )
        return found[0]

    async def _boot(self) -> None:
        """Start a server on the scenario's storage, with the power controls registered."""
        self._server = contextlib.AsyncExitStack()
        self.mass = await self._server.enter_async_context(full_mass_context(self._tmp_path))
        for control in self._power_controls.values():
            await self._register_power_control(control)
        await self.settle()

    async def _shutdown(self) -> None:
        """Stop the running server, if any."""
        if self._server is not None:
            server, self._server = self._server, None
            # stopping waits on timeouts of its own, which need real time to pass
            with self._clock.running():
                await server.aclose()

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
                f"{protocol.player_id} belongs to a server or provider instance that is gone, "
                f"use scenario.protocol({protocol.player_id!r})"
            )
            raise RuntimeError(msg)

    def _on_event(self, event: MassEvent) -> None:
        """Capture an event the server emitted."""
        self._events.append(Event(event.event, event.object_id))

    def _busy_tasks(self) -> set[asyncio.Task[Any]]:
        """Return the running tasks that do not wait for the clock, except the commands."""
        current = asyncio.current_task()
        return {
            task
            for task in asyncio.all_tasks()
            if task is not current
            and not task.done()
            and task not in self._commands
            and not _waits_for_clock(task)
        }

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
        Patch what a scenario fakes: the player providers, ARP lookups and the clock.

        The manifest loading is the one private piece of the server touched here: before any
        provider loads, a fake domain needs a manifest that allows several instances and has
        no requirements, dependencies or network discovery. It is also the earliest point of
        a boot with the new instance at hand, so the events of the boot itself are captured.
        """
        fake_domains: set[str] = set()
        load_manifests = MusicAssistant._MusicAssistant__load_provider_manifests  # type: ignore[attr-defined]
        loop = asyncio.get_running_loop()
        self._clock = _LoopClock(loop.time)

        async def _load_manifests(mass: MusicAssistant) -> None:
            mass.subscribe(self._on_event)
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
            return FakePlayerProvider(mass, manifest, config, self._devices)

        async def _load_module(domain: str, requirements: list[str]) -> Any:
            if domain in fake_domains:
                return SimpleNamespace(setup=_setup_provider)
            return await load_provider_module(domain, requirements)

        async def _arp_lookup(ip_address: str) -> str | None:
            return self.arp.get(ip_address)

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    MusicAssistant, "_MusicAssistant__load_provider_manifests", _load_manifests
                )
            )
            stack.enter_context(patch("music_assistant.mass.load_provider_module", _load_module))
            stack.enter_context(patch("music_assistant.helpers.util.get_mac_address", _arp_lookup))
            stack.enter_context(patch.object(loop, "time", self._clock.time))
            for module in MONOTONIC_MODULES:
                stack.enter_context(patch(f"{module}.time", _LoopTime(self._clock)))
            yield


@dataclass
class _Device:
    """What a device reports, kept across restarts, and whether its provider can find it."""

    provider_id: str
    # reported state by attribute name, as taken by Scenario.report()
    state: dict[str, Any]
    identifiers: dict[IdentifierType, str] = field(default_factory=dict)
    present: bool = False


class _LoopClock:
    """The loop clock of a scenario: it stands still, except where real time is let through."""

    def __init__(self, real_time: Callable[[], float]) -> None:
        """
        Initialize the clock at the current time.

        :param real_time: The real clock of the loop.
        """
        self._real_time = real_time
        self._now = real_time()
        self._running_since: float | None = None

    def time(self) -> float:
        """Return the current loop time."""
        if self._running_since is None:
            return self._now
        return self._now + self._real_time() - self._running_since

    def advance(self, seconds: float) -> None:
        """
        Move the clock forward.

        :param seconds: Seconds to pass.
        """
        self._now += seconds

    @contextlib.contextmanager
    def running(self) -> Iterator[None]:
        """Let real time pass on the clock while in this context."""
        self._running_since = self._real_time()
        try:
            yield
        finally:
            self._now = self.time()
            self._running_since = None


class _LoopTime:
    """The time module as the players code sees it, with monotonic() on the loop clock."""

    def __init__(self, clock: _LoopClock) -> None:
        """
        Initialize the module stand-in.

        :param clock: The loop clock monotonic() reads.
        """
        self.monotonic = clock.time

    def __getattr__(self, name: str) -> Any:
        """Return everything else from the time module."""
        return getattr(time, name)


def _waits_for_clock(task: asyncio.Task[Any]) -> bool:
    """Return whether a task waits in asyncio.sleep() for the loop clock to move."""
    awaited: Any = task.get_coro()
    while awaited is not None:
        if getattr(awaited, "cr_code", None) is asyncio.sleep.__code__:
            # sleep(0) only yields to the loop, so that task has work due now
            return bool(awaited.cr_frame.f_locals["delay"] > 0)
        awaited = getattr(awaited, "cr_await", None)
    return False


def _fake_manifest(manifest: ProviderManifest) -> ProviderManifest:
    """Return the manifest of a fake domain, based on the real one."""
    return replace(
        manifest,
        requirements=[],
        multi_instance=True,
        builtin=False,
        allow_disable=True,
        depends_on=None,
        mdns_discovery=None,
        upnp_discovery=None,
        has_setup_flow=False,
    )


def _carried_ids(state: PlayerState) -> set[str]:
    """Return the ids of the protocol objects a player state stands for."""
    return {state.player_id} | {output.output_protocol_id for output in state.output_protocols}


def _merge(target: dict[str, Any], values: dict[str, Any]) -> None:
    """Merge nested values into a dict in place; a None value deletes the key."""
    for key, value in values.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value
