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
import logging
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
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

from music_assistant.helpers.util import load_provider_module
from music_assistant.mass import MusicAssistant
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.models.player_provider import PlayerProvider
from tests.conftest import full_mass_context

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.player import Player as PlayerState
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models.player import PlayerMedia

LOGGER = logging.getLogger(__name__)

# Player providers that keep their real implementation, as they own no device.
REAL_PLAYER_PROVIDERS = frozenset({"universal_player", "sync_group"})

# Seconds of loop time per step of advance(): fine enough to keep the order of the server's
# debounce timers (0.25 s and up).
TICK = 0.1

# Real seconds settle() waits for the tasks it saw starting. A task still running by then
# waits for the loop clock to move and is left alone from then on.
SETTLE_TIMEOUT = 1.0

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
    # protocol domain of each output, mapped to whether that output is available
    outputs: dict[str, bool]


class FakeProtocol(PlayerProtocol):
    """
    A provider object for one device: the test sets what it reports, its commands are recorded.

    Commands are applied to the reported state the way a well-behaved device reports them
    back (power, volume, playback), so the server never waits for a device that stays silent.
    """

    def __init__(
        self,
        provider: FakePlayerProvider,
        protocol_id: str,
        name: str,
        role: Role,
        identifiers: dict[IdentifierType, str],
        state: dict[str, Any],
    ) -> None:
        """
        Initialize the protocol object; use FakePlayerProvider.protocol() to create one.

        :param provider: The fake provider this protocol belongs to.
        :param protocol_id: ID of the protocol object (the player id it registers with).
        :param name: The name the device reports.
        :param role: The role of this protocol on its device.
        :param identifiers: The device identifiers it reports (MAC, IP, UUID, ...).
        :param state: Other reported state, as taken by Scenario.report().
        """
        super().__init__(provider, protocol_id)
        # every command received, as a tuple of the command name and its arguments
        self.commands: list[tuple[Any, ...]] = []
        self._attr_name = name
        self._attr_supported_features = set(DEFAULT_FEATURES)
        self._attr_device_info = DeviceInfo(model=f"Fake {provider.domain}", manufacturer="Fake")
        self._set_reported(role=role, identifiers=identifiers, **state)

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
        """Set reported state, as taken by Scenario.report(), without publishing it."""
        for key, value in state.items():
            if key == "role":
                self._attr_type = _ROLE_TYPES[value]
            elif key == "identifiers":
                for identifier_type, identifier in value.items():
                    self._attr_device_info.add_identifier(identifier_type, identifier)
            elif hasattr(self, f"_attr_{key}"):
                setattr(self, f"_attr_{key}", value)
            else:
                msg = f"{type(self).__name__} reports no state named {key!r}"
                raise AttributeError(msg)


class FakePlayerProvider(PlayerProvider):
    """A player provider of an impersonated domain, owning the FakeProtocols a test creates."""

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
        Create a protocol object of this provider; Scenario.attach() attaches it.

        :param protocol_id: ID of the protocol object (the player id it registers with).
        :param name: The name the device reports.
        :param role: ``"native"`` for the vendor's own control plane, ``"audio"`` for an audio
            protocol of a device (AirPlay, Cast, DLNA, ...).
        :param identifiers: The device identifiers it reports (MAC, IP, UUID, ...).
        :param state: Other reported state, as taken by Scenario.report().
        """
        return FakeProtocol(self, protocol_id, name, role, identifiers or {}, state)

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from this provider."""
        await self.mass.players.unregister(player_id, permanent=True)


class Scenario:
    """
    A real server with fake player providers, driven the way providers and clients drive it.

    Every action waits until the server finished the work it started, so its outcome can be
    asserted right after. The event loop clock only moves through advance().
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
        self._patches = contextlib.ExitStack()
        self._server: contextlib.AsyncExitStack | None = None
        self._clock_offset = 0.0
        self._idle_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        """Boot the server."""
        self._patches.enter_context(self._patch_environment())
        await self._boot()

    async def close(self) -> None:
        """Stop the server and undo what the scenario patched."""
        try:
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

    async def remove_provider(self, provider: FakePlayerProvider) -> None:
        """
        Remove a provider instance the way a user does.

        :param provider: The provider to remove.
        """
        await self.mass.config.remove_provider_config(provider.instance_id)
        await self.settle()

    async def attach(self, protocol: FakeProtocol) -> None:
        """
        Attach a protocol object: its provider discovered the device, or found it again.

        :param protocol: The protocol object to attach.
        """
        await self.mass.players.register_or_update(protocol)
        await self.settle()

    async def detach(self, protocol: FakeProtocol) -> None:
        """
        Detach a protocol object: its provider lost the device.

        :param protocol: The protocol object to detach.
        """
        await self.mass.players.unregister(protocol.player_id, permanent=False)
        await self.settle()

    async def remove(self, player_id: str) -> None:
        """
        Remove a player the way a user does.

        :param player_id: ID of the player to remove.
        """
        await self.mass.players.remove(player_id)
        await self.settle()

    async def report(self, protocol: FakeProtocol, **state: Any) -> None:
        """
        Let a protocol object report new state, the way its provider publishes a device update.

        :param protocol: The protocol object that reports.
        :param state: Values by attribute name without the ``_attr_`` prefix (``available``,
            ``powered``, ``volume_level``, ``playback_state``, ``supported_features``, ...),
            plus ``role`` and ``identifiers`` (merged into the reported device info, a None
            value drops one).
        """
        protocol._set_reported(**state)
        protocol.update_state()
        await self.settle()

    async def advance(self, seconds: float) -> None:
        """
        Move the event loop clock forward, running every timer that falls due on the way.

        Only the event loop clock moves, the wall clock (``time.time()``) does not.

        :param seconds: Seconds to pass.
        """
        target = self._clock_offset + seconds
        while self._clock_offset < target:
            self._clock_offset = min(self._clock_offset + TICK, target)
            await self.settle()

    async def restart(self) -> None:
        """Stop the server and start a new one on the same storage (settings.json and cache)."""
        await self._shutdown()
        await self._boot()

    async def seed_config(self, values: dict[str, Any]) -> None:
        """
        Restart the server on its stored config, with the given values merged in.

        :param values: Nested settings.json content, e.g. ``{"players": {player_id: {...}}}``.
        """
        await self._shutdown()
        settings_file = self._tmp_path / "data" / "settings.json"
        data = json.loads(settings_file.read_text()) if settings_file.exists() else {}
        _merge(data, values)
        settings_file.write_text(json.dumps(data))
        await self._boot()

    async def settle(self) -> None:
        """Let the server finish the work that is due now, without moving the clock."""
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while True:
            # two passes: the callbacks of a task that just finished run on the next one
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if not (busy := self._busy_tasks()):
                return
            if time.monotonic() > deadline:
                LOGGER.debug("Tasks left waiting for the clock: %s", busy)
                self._idle_tasks.update(busy)
                return
            await asyncio.wait(busy, timeout=0.01)

    def provider(self, instance_id_or_domain: str) -> FakePlayerProvider:
        """
        Return a loaded fake provider, which after a restart is a new instance.

        :param instance_id_or_domain: Instance id, or the domain of a provider with one instance.
        """
        provider = self.mass.get_provider(instance_id_or_domain)
        assert isinstance(provider, FakePlayerProvider), f"no fake provider {instance_id_or_domain}"
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

    def queue(self, queue_id: str) -> PlayerQueue | None:
        """
        Return the queue of a player.

        :param queue_id: ID of the queue, which is the id of its player.
        """
        return self.mass.player_queues.get(queue_id)

    def stored(self, key: str) -> Any:
        """
        Return a value of the stored config (what the server persists to settings.json).

        :param key: Path of the value, e.g. ``"players/RINCON_1/enabled"``.
        """
        return self.mass.config.get(key)

    def events(self, *event_types: EventType, object_id: str | None = None) -> list[Event]:
        """
        Return the events emitted since the start or the last clear_events(), in order.

        Consecutive repeats of one event for one object are collapsed, so the result reads
        as a sequence of transitions.

        :param event_types: Only return events of these types (all when omitted).
        :param object_id: Only return events for this object (player id, queue id, ...).
        """
        result: list[Event] = []
        for event in self._events:
            if event_types and event.type not in event_types:
                continue
            if object_id is not None and event.object_id != object_id:
                continue
            if not result or result[-1] != event:
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
        """Start a server on the scenario's storage and capture its events."""
        self._server = contextlib.AsyncExitStack()
        self.mass = await self._server.enter_async_context(full_mass_context(self._tmp_path))
        self._server.callback(self.mass.subscribe(self._on_event))
        # the tasks a booted server keeps running are no work settle() should wait for
        current = asyncio.current_task()
        self._idle_tasks = {task for task in asyncio.all_tasks() if task is not current}

    async def _shutdown(self) -> None:
        """Stop the running server, if any."""
        if self._server is not None:
            server, self._server = self._server, None
            await server.aclose()

    def _on_event(self, event: MassEvent) -> None:
        """Capture an event the server emitted."""
        self._events.append(Event(event.event, event.object_id))

    def _busy_tasks(self) -> set[asyncio.Task[Any]]:
        """Return the tasks that started since the boot and are still running."""
        current = asyncio.current_task()
        return {
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done() and task not in self._idle_tasks
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
            outputs={output.protocol_domain: output.available for output in state.output_protocols},
        )

    @contextlib.contextmanager
    def _patch_environment(self) -> Iterator[None]:
        """
        Patch what a scenario fakes: the player providers, ARP lookups and the loop clock.

        The one piece of server internals this touches is the manifest loading: before any
        provider loads, a fake domain needs a manifest that allows several instances and has
        no requirements, dependencies or network discovery.
        """
        fake_domains: set[str] = set()
        load_manifests = MusicAssistant._MusicAssistant__load_provider_manifests  # type: ignore[attr-defined]
        loop = asyncio.get_running_loop()
        loop_time = loop.time

        async def _load_manifests(mass: MusicAssistant) -> None:
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

        async def _load_module(domain: str, requirements: list[str]) -> Any:
            if domain in fake_domains:
                return SimpleNamespace(setup=_setup_fake_provider)
            return await load_provider_module(domain, requirements)

        async def _arp_lookup(ip_address: str) -> str | None:
            return self.arp.get(ip_address)

        with (
            patch.object(
                MusicAssistant, "_MusicAssistant__load_provider_manifests", _load_manifests
            ),
            patch("music_assistant.mass.load_provider_module", _load_module),
            patch("music_assistant.helpers.util.get_mac_address", _arp_lookup),
            patch.object(loop, "time", lambda: loop_time() + self._clock_offset),
        ):
            yield


async def _setup_fake_provider(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> FakePlayerProvider:
    """Create the provider instance of a fake domain."""
    return FakePlayerProvider(mass, manifest, config, {ProviderFeature.REMOVE_PLAYER})


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
    """Merge nested values into a dict, in place."""
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value
