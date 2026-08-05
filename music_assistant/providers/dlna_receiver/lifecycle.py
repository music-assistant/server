"""Event-driven lifecycle management for per-player DLNA renderers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import EventType, IdentifierType

from .constants import UDN_NAMESPACE
from .models import RendererInstance
from .renderer import UPnPRenderer
from .ssdp import SSDPAdvertiser

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

SetTransportUriCallback = Callable[[RendererInstance, str, str | None], Awaitable[None]]
PlayCallback = Callable[[RendererInstance, str], Awaitable[bool]]
InstanceCallback = Callable[[RendererInstance], Awaitable[None]]
PositionCallback = Callable[[RendererInstance], tuple[int, int]]
VolumeCallback = Callable[[RendererInstance, int], Awaitable[None]]
MuteCallback = Callable[[RendererInstance, bool], Awaitable[None]]
RemovedCallback = Callable[[str, RendererInstance], None]


@dataclass(frozen=True)
class RendererCallbacks:
    """Bind provider behavior to renderer protocol callbacks."""

    on_set_av_transport_uri: SetTransportUriCallback
    on_play: PlayCallback
    on_pause: InstanceCallback
    on_stop: InstanceCallback
    on_get_position: PositionCallback
    on_set_volume: VolumeCallback
    on_set_mute: MuteCallback
    on_instance_removed: RemovedCallback


def deterministic_udn(player_id: str) -> str:
    """Return the stable renderer UDN for a Music Assistant player."""
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, UDN_NAMESPACE)
    return f"uuid:{uuid.uuid5(namespace, player_id or '__default__')}"


class RendererRegistry:
    """Create and remove renderer instances as Music Assistant players change."""

    def __init__(
        self,
        mass: MusicAssistant,
        target_spec: str,
        friendly_prefix: str,
        bind_ip: str,
        base_port: int,
        callbacks: RendererCallbacks,
    ) -> None:
        """Initialize registry configuration without opening network resources."""
        self.mass = mass
        self.instances: dict[str, RendererInstance] = {}
        self._target_spec = target_spec
        self._friendly_prefix = friendly_prefix
        self._bind_ip = bind_ip
        self._base_port = base_port
        self._next_port = base_port
        self._callbacks = callbacks
        self._ports: dict[str, int] = {}
        self._own_uuids: set[str] = set()
        self._lock = asyncio.Lock()
        self._unsubscribe: Callable[[], None] | None = None
        self._unloading = asyncio.Event()

    async def start(self) -> None:
        """Subscribe to player events and create the initial renderer set."""
        self._unloading.clear()
        self._unsubscribe = self.mass.subscribe(
            self._on_player_event,
            (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED),
        )
        try:
            async with self._lock:
                for player_id, player_name in self._initial_player_specs():
                    await self._create_instance(player_id, player_name)
        except BaseException:
            try:
                await self.stop()
            except Exception:
                LOGGER.exception("Failed to roll back renderer registry startup")
            raise

    async def stop(self) -> None:
        """Unsubscribe and stop all renderer instances."""
        self._unloading.set()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        async with self._lock:
            errors: list[Exception] = []
            for source_id in list(self.instances):
                try:
                    await self._remove_instance(source_id)
                except Exception as err:
                    errors.append(err)
            if errors:
                raise ExceptionGroup("Failed to stop renderer registry", errors)

    def _on_player_event(self, event: MassEvent) -> None:
        """Schedule reconciliation for an added or removed player."""
        if self._unloading.is_set() or not event.object_id:
            return
        self.mass.create_task(self._reconcile_player, event.object_id)

    async def _reconcile_player(self, player_id: str) -> None:
        """Converge one player to its final desired renderer state."""
        if self._unloading.is_set():
            return
        async with self._lock:
            if self._unloading.is_set():
                return
            player = self._desired_player(player_id)
            if player is None:
                await self._remove_instance(player_id)
                return
            if player_id not in self.instances:
                await self._create_instance(player_id, self._display_name(player))

    def _initial_player_specs(self) -> list[tuple[str, str]]:
        """Resolve configured targets for initial startup."""
        if self._target_spec == "*":
            return [
                (player.player_id, self._display_name(player))
                for player in self._eligible_all_players()
            ]

        specs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for value in self._target_spec.split(","):
            player_id = value.strip()
            if not player_id or player_id in seen:
                continue
            seen.add(player_id)
            player = self.mass.players.get_player(player_id)
            specs.append((player_id, self._display_name(player) if player else player_id))
        return specs

    def _desired_player(self, player_id: str) -> Any | None:
        """Return a player when the configured policy wants a renderer for it."""
        if self._target_spec != "*":
            targets = {item.strip() for item in self._target_spec.split(",") if item.strip()}
            if player_id not in targets:
                return None
            return self.mass.players.get_player(player_id)

        return next(
            (player for player in self._eligible_all_players() if player.player_id == player_id),
            None,
        )

    def _eligible_all_players(self) -> list[Any]:
        """Return non-protocol players excluding this registry's own renderers."""
        players = self.mass.players.all_players(
            return_unavailable=True,
            return_protocol_players=False,
        )
        candidate_uuids = self._own_uuids | {
            _normalize_uuid(deterministic_udn(player.player_id)) for player in players
        }
        result: list[Any] = []
        for player in players:
            identifier = getattr(getattr(player, "device_info", None), "identifiers", {}).get(
                IdentifierType.UUID
            )
            if identifier and _normalize_uuid(identifier) in candidate_uuids:
                LOGGER.debug(
                    "Filtering out own renderer player: %s (%s)",
                    player.player_id,
                    self._display_name(player),
                )
                continue
            result.append(player)
        return result

    async def _create_instance(self, player_id: str, player_name: str) -> RendererInstance:
        """Create and start one renderer instance."""
        port = self._ports.get(player_id)
        if port is None:
            port = self._next_port
            self._ports[player_id] = port
            self._next_port += 1

        friendly_name = (
            f"{self._friendly_prefix} — {player_name}" if player_name else self._friendly_prefix
        )
        udn = deterministic_udn(player_id)
        self._own_uuids.add(_normalize_uuid(udn))
        renderer = UPnPRenderer(
            friendly_name=friendly_name,
            bind_ip=self._bind_ip,
            http_port=port,
            udn=udn,
            session=self.mass.http_session,
        )
        instance = RendererInstance(
            player_id=player_id,
            player_name=player_name,
            renderer=renderer,
            ssdp=SSDPAdvertiser(
                udn=udn,
                description_url=renderer.description_url,
                bind_ip=self._bind_ip,
            ),
        )
        renderer.on_set_av_transport_uri = lambda uri, metadata: (
            self._callbacks.on_set_av_transport_uri(instance, uri, metadata)
        )
        renderer.on_play = lambda previous_state: self._callbacks.on_play(instance, previous_state)
        renderer.on_pause = lambda: self._callbacks.on_pause(instance)
        renderer.on_stop = lambda: self._callbacks.on_stop(instance)
        renderer.on_get_position = lambda: self._callbacks.on_get_position(instance)
        renderer.on_set_volume = lambda volume: self._callbacks.on_set_volume(instance, volume)
        renderer.on_set_mute = lambda mute: self._callbacks.on_set_mute(instance, mute)

        try:
            await renderer.start()
            instance.ssdp.description_url = renderer.description_url
            await instance.ssdp.start()
        except BaseException:
            try:
                await self._stop_instance_resources(instance)
            except Exception:
                LOGGER.exception("Failed to clean up renderer after startup failure")
            raise

        self.instances[player_id] = instance
        LOGGER.info(
            "Renderer '%s' → player '%s' on port %d (UDN: %s)",
            friendly_name,
            player_id,
            port,
            udn,
        )
        return instance

    async def _remove_instance(self, source_id: str) -> None:
        """Send byebye and stop one renderer instance."""
        instance = self.instances.pop(source_id, None)
        if instance is None:
            return
        errors: list[Exception] = []
        try:
            await self._stop_instance_resources(instance)
        except Exception as err:
            errors.append(err)
        try:
            self._callbacks.on_instance_removed(source_id, instance)
        except Exception as err:
            errors.append(err)
        if errors:
            raise ExceptionGroup(f"Failed to remove renderer {source_id}", errors)

    @staticmethod
    async def _stop_instance_resources(instance: RendererInstance) -> None:
        """Attempt to stop every network resource owned by an instance."""
        errors: list[Exception] = []
        for stop in (instance.ssdp.stop, instance.renderer.stop):
            try:
                await stop()
            except Exception as err:
                errors.append(err)
        if errors:
            raise ExceptionGroup("Failed to stop renderer resources", errors)

    @staticmethod
    def _display_name(player: Any) -> str:
        """Return the best display name exposed by a player object."""
        return str(player.display_name or player.name or player.player_id)


def _normalize_uuid(value: str) -> str:
    """Normalize UUID representations used by UPnP and Music Assistant."""
    return value.strip().lower().removeprefix("uuid:").replace("-", "").replace(":", "")
