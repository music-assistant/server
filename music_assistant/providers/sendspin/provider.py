"""Player Provider for Sendspin."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import (
    AudioCodec,
    ManagementResult,
    PairAbortReason,
    PairMethod,
    PlayerCommand,
    role_family,
)
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.pairing import (
    LocalPairingAbortError,
    PairingAbortError,
    PairingAttempt,
    PairingError,
    PairingTimeoutError,
)
from aiosendspin.noise.pairing_token import decode_token
from aiosendspin.noise.trust_store import FileServerPairingStore, PskCategory
from aiosendspin.server import (
    ClientAddedEvent,
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    ClientRemovedEvent,
    ClientUpdatedEvent,
    SendspinEvent,
    SendspinServer,
)
from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    IdentifierType,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    AlreadyRegisteredError,
    InvalidCommand,
    SetupFailedError,
)

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    CONF_LOG_LEVEL,
    CONF_PLAYERS,
    CONF_PROVIDERS,
    SENDSPIN_SERVER_PORT,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.guest_access import (
    credential_owner,
    credential_owners_for_user_id,
    is_session_scoped_owner,
)
from music_assistant.helpers.util import format_ip_for_url
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.constants import (
    CONF_ALLOW_UNENCRYPTED,
    CONF_MIN_PIN_LENGTH,
    CONF_SENDSPIN_STATIC_DELAY,
    CONF_VIRTUAL_PLAYER_OWNER,
    DEFAULT_MIN_PIN_LENGTH,
    VIRTUAL_PLAYER_ID_PREFIX,
)
from music_assistant.providers.sendspin.helpers import (
    SecurityActionError,
    effective_pair_methods,
    error_alert,
    negotiated_pin_length,
    pair_method_descriptor,
)
from music_assistant.providers.sendspin.player import (
    SendspinBasePlayer,
    SendspinPlayer,
    SendspinVisualizerPlayer,
)
from music_assistant.providers.sendspin.security import (
    IDENTITY_FILENAME,
    get_or_create_server_identity,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from aiosendspin.models.core import PairMethodDescriptor
    from aiosendspin.models.management import (
        ManagementResultData,
        ManagementSetPairingConfigPayload,
    )
    from aiosendspin.noise.trust_store import ServerPairingStore
    from aiosendspin.server.client import SendspinClient
    from aiosendspin.server.connection import SendspinConnection
    from aiosendspin.server.server import ExternalStreamStartRequest
    from music_assistant_models.auth import User
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.providers.hass import HomeAssistantProvider


DEFAULT_SENDSPIN_CLIENT_PORT = 8928
DEFAULT_SENDSPIN_CLIENT_PATH = "/sendspin"
VIRTUAL_PLAYER_REGISTER_TIMEOUT = 10.0
VIRTUAL_PLAYER_CLEANUP_DELAYS = (0.0, 0.5, 2.0)
VIRTUAL_PLAYER_CLEANUP_TIMEOUT = 2.0
WEB_PLAYER_CONNECT_TIMEOUT = 10.0
# Grace period so a network blip keeps the pairing record.
SESSION_PAIRING_EVICTION_GRACE = 120.0

PIN_REQUEST_FEEDBACK_TIMEOUT = 2
PIN_RETRY_IDLE_TIMEOUT = 300
MANAGEMENT_REQUEST_TIMEOUT = 10
MANAGEMENT_IDLE_TIMEOUT = 300


@dataclass
class PinPairingSession:
    """State of an operator PIN pairing session for one client, across retry-in-place attempts."""

    client_id: str
    method: PairMethod
    pin_future: asyncio.Future[str]
    verify: bool = False
    static: bool = False
    pin_length: int | None = None
    task: asyncio.Task[None] | None = None
    pin_request_event: asyncio.Event = field(default_factory=asyncio.Event)
    gesture_event: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None
    retryable: bool = False
    opened_management: bool = False

    @property
    def attempt_running(self) -> bool:
        """Whether an attempt is currently in flight."""
        return self.task is not None and not self.task.done()

    @property
    def awaiting_first_message(self) -> bool:
        """Whether the attempt is still waiting for the client's first pairing message."""
        return (
            self.attempt_running
            and not self.gesture_event.is_set()
            and not self.pin_request_event.is_set()
        )

    @property
    def awaiting_gesture(self) -> bool:
        """Whether the client reported the attempt gesture-gated and still awaits a window."""
        return (
            self.attempt_running
            and self.gesture_event.is_set()
            and not self.pin_request_event.is_set()
        )

    @property
    def awaiting_pin(self) -> bool:
        """Whether the attempt is waiting for the operator to submit a PIN."""
        return self.attempt_running and not self.pin_future.done()

    async def wait_first_message(self) -> None:
        """Resolve once the client asks for a gesture or the PIN, or the attempt ends."""
        await self._wait_events(self.gesture_event, self.pin_request_event)

    async def wait_pin_request(self) -> None:
        """Resolve once the client asks for the PIN, or the attempt ends."""
        await self._wait_events(self.pin_request_event)

    @property
    def can_retry(self) -> bool:
        """Whether a failed attempt can be retried in place."""
        return self.task is not None and self.task.done() and self.retryable

    @property
    def finished(self) -> bool:
        """Whether the session reached a terminal outcome (no retry possible)."""
        return self.task is not None and self.task.done() and not self.retryable

    async def _wait_events(self, *events: asyncio.Event) -> None:
        """Resolve on the first of ``events`` or on the attempt ending, whichever comes first."""
        waiters: list[asyncio.Future[Any]] = [
            asyncio.ensure_future(event.wait()) for event in events
        ]
        if self.task is not None:
            # Shielded: dropping this wait must never cancel the pairing attempt.
            waiters.append(asyncio.shield(self.task))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()


@dataclass
class ManagementSession:
    """State of an operator device-management session for one client."""

    client_id: str
    connection: SendspinConnection
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def _poll_until[T](check: Callable[[], T | None], timeout: float) -> T | None:
    """Poll ``check`` every 0.1s until it returns a value, or ``None`` once ``timeout`` lapses."""
    try:
        async with asyncio.timeout(timeout):
            while True:
                if (result := check()) is not None:
                    return result
                await asyncio.sleep(0.1)
    except TimeoutError:
        return None


def _evict_session_pairing_task_id(client_id: str) -> str:
    """Task id for a client's delayed session-scoped pairing eviction."""
    return f"sendspin_evict_session_pairing_{client_id}"


def _pin_idle_task_id(client_id: str) -> str:
    """Timer/task id for a client's pairing-retry idle timeout."""
    return f"sendspin_pin_idle_{client_id}"


def _management_idle_task_id(client_id: str) -> str:
    """Timer/task id for a client's management-session idle timeout."""
    return f"sendspin_management_idle_{client_id}"


_MANAGEMENT_RESULT_ALERTS = {
    ManagementResult.PERMISSION_DENIED: "management_error_permission_denied",
    ManagementResult.ALREADY_EXISTS: "management_error_already_exists",
    ManagementResult.INVALID: "management_error_invalid",
    ManagementResult.NOT_FOUND: "management_error_not_found",
    ManagementResult.STORAGE_EXHAUSTED: "management_error_storage_exhausted",
}


def _check_management_result(result: ManagementResult) -> None:
    """Raise a structured error for a non-ok management result."""
    if result is ManagementResult.OK:
        return
    alert_key = _MANAGEMENT_RESULT_ALERTS.get(result)
    if alert_key is None:
        raise SecurityActionError("management_error_generic", detail=result.value)
    raise SecurityActionError(alert_key)


async def _sweep_session_pairings(pairing_store: ServerPairingStore) -> int:
    """
    Remove every session-scoped pairing record, returning how many were removed.

    Session-scoped pairings live only as long as their client's connection, and no
    connection survives a server restart.
    """
    swept = 0
    for record in await pairing_store.list_records():
        if record.owner is not None and is_session_scoped_owner(record.owner):
            await pairing_store.remove_record(record.client_id)
            swept += 1
    return swept


def _manual_client_url(address: str) -> str:
    """Convert a manually configured Sendspin host/IP to a client WebSocket URL."""
    stripped_address = address.strip()
    if not stripped_address:
        raise ValueError("Address is empty")

    if "://" in stripped_address:
        return stripped_address

    try:
        parsed_ip = ip_address(stripped_address)
    except ValueError:
        pass
    else:
        return (
            f"ws://{format_ip_for_url(str(parsed_ip))}:"
            f"{DEFAULT_SENDSPIN_CLIENT_PORT}{DEFAULT_SENDSPIN_CLIENT_PATH}"
        )

    parsed_address = urlsplit(f"//{stripped_address}")
    if parsed_address.hostname is None:
        raise ValueError("Address does not contain a host")

    return (
        f"ws://{format_ip_for_url(parsed_address.hostname)}:"
        f"{parsed_address.port or DEFAULT_SENDSPIN_CLIENT_PORT}"
        f"{parsed_address.path or DEFAULT_SENDSPIN_CLIENT_PATH}"
    )


class SendspinProvider(PlayerProvider):
    """Player Provider for Sendspin."""

    reload_on_streams_network_change = True
    server_api: SendspinServer
    unregister_cbs: list[Callable[[], None]]
    _pending_unregisters: dict[str, asyncio.Event]
    _bridge_identifiers: dict[str, dict[IdentifierType, str]]
    _bridge_underlying_players: dict[str, str]
    _bridge_static_delay_defaults: dict[str, int]
    _client_event_versions: dict[str, int]
    _client_event_task_counts: dict[str, int]
    _manual_ip_config: tuple[str, ...]
    _virtual_players: dict[str, str]
    _unloading: bool
    _hass_available: bool

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize a new Sendspin player provider."""
        super().__init__(mass, manifest, config)
        # Handle config option for manual IP's. Read a default here: at construction the
        # config only carries the server defaults + stored raw values (the provider's typed
        # option entries are resolved and applied by the config controller right after this).
        manual_ip_config = cast(
            "list[str]", config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key) or []
        )
        self._manual_ip_config = tuple(address for address in manual_ip_config if address.strip())
        self._pending_unregisters = {}
        self._bridge_identifiers = {}
        self._headless_client_ids: set[str] = set()
        self._bridge_underlying_players = {}
        self._bridge_static_delay_defaults = {}
        self._bridge_player_types: dict[str, PlayerType] = {}
        self._client_event_versions = {}
        self._client_event_task_counts = {}
        self._virtual_players = {}
        self._pin_sessions: dict[str, PinPairingSession] = {}
        self._pending_pairing_evictions: set[str] = set()
        self._management_sessions: dict[str, ManagementSession] = {}
        self._pairing_config_snapshots: dict[
            str, tuple[SendspinConnection, ManagementResultData]
        ] = {}
        self._unloading = False
        self._hass_available = False
        self.unregister_cbs = []

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            CONF_ENTRY_MANUAL_DISCOVERY_IPS,
            ConfigEntry(
                key=CONF_ALLOW_UNENCRYPTED,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_MIN_PIN_LENGTH,
                type=ConfigEntryType.INTEGER,
                range=(4, 12),
                default_value=DEFAULT_MIN_PIN_LENGTH,
            ),
        )

    async def handle_async_init(self) -> None:
        """Load the persistent server identity and pairing store, then create the server."""
        self._set_aiosendspin_log_level()
        storage_dir = Path(self.mass.storage_path) / "sendspin"
        identity_path = storage_dir / IDENTITY_FILENAME
        try:
            identity = await asyncio.to_thread(get_or_create_server_identity, storage_dir)
        except ValueError as err:
            raise SetupFailedError(
                f"The Sendspin server identity at {identity_path} is corrupt: {err}. Restore it "
                "from a backup, or remove the file to start fresh - every paired device will "
                "then need to be re-paired."
            ) from err
        except OSError as err:
            raise SetupFailedError(
                f"Could not read the Sendspin server identity at {identity_path}: {err}. Fix the "
                "file-access problem and reload; do not delete the file or every paired device "
                "will need to be re-paired."
            ) from err
        pairing_store_path = storage_dir / "pairing_store.json"
        try:
            pairing_store = await FileServerPairingStore.open(pairing_store_path)
        except (ValueError, TypeError, KeyError) as err:
            raise SetupFailedError(
                f"The Sendspin pairing store at {pairing_store_path} is corrupt: {err}. Restore it "
                "from a backup, or remove the file to start fresh - this discards all pairings and "
                "unpaired-access approvals."
            ) from err
        except OSError as err:
            raise SetupFailedError(
                f"Could not read the Sendspin pairing store at {pairing_store_path}: {err}. Fix the "
                "file-access problem and reload; do not delete the file or all pairings will be "
                "lost."
            ) from err
        if swept := await _sweep_session_pairings(pairing_store):
            self.logger.info("Removed %d session-scoped pairing(s) from a previous run", swept)
        self.server_api = SendspinServer(
            self.mass.loop,
            identity,
            "Music Assistant",
            self.mass.http_session,
            pairing_store=pairing_store,
            allow_unencrypted=cast("bool", self.config.get_value(CONF_ALLOW_UNENCRYPTED, True)),
            min_pin_length=cast(
                "int", self.config.get_value(CONF_MIN_PIN_LENGTH, DEFAULT_MIN_PIN_LENGTH)
            ),
        )
        # Pitch (YINFFT) is the heaviest visualizer DSP and result quality is
        # still very mixed, needs more testing. Disable it globally for now to
        # spare low-power hosts.
        self.server_api.set_visualizer_pitch_enabled(enabled=False)
        self.unregister_cbs = [
            self.server_api.add_event_listener(self.event_cb),
            self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED),
        ]
        # seed the hass availability snapshot so the first (un)load is seen as a change
        hass = self.mass.get_provider("hass")
        self._hass_available = hass is not None and hass.available

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Handle logic when the config is updated."""
        await super().update_config(config, changed_keys)
        # a log level(-only) change does not reload the provider,
        # so realign aiosendspin's logger here
        if f"values/{CONF_LOG_LEVEL}" in changed_keys:
            self._set_aiosendspin_log_level()

    def event_cb(self, server: SendspinServer, event: SendspinEvent) -> None:
        """Event callback registered to the sendspin server."""
        match event:
            case ClientAddedEvent(client_id):
                event_version = self._begin_client_event(client_id)
                self.mass.create_task(self._handle_client_added(client_id, event_version))
            case ClientRemovedEvent(client_id):
                event_version = self._begin_client_event(client_id)
                self.mass.create_task(self._handle_client_removed(client_id, event_version))
            case ClientUpdatedEvent(client_id):
                event_version = self._begin_client_event(client_id)
                self.mass.create_task(self._handle_client_updated(client_id, event_version))
            # Transport lifecycle events, implemented in another PR.
            case ClientConnectedEvent():
                pass
            case ClientDisconnectedEvent(client_id):
                self._pending_pairing_evictions.add(client_id)
                self.mass.call_later(
                    SESSION_PAIRING_EVICTION_GRACE,
                    self._evict_session_pairing,
                    client_id,
                    task_id=_evict_session_pairing_task_id(client_id),
                )
            case _:
                self.logger.error("Unknown sendspin event: %s", event)

    def on_player_enabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets enabled."""
        # A client that connected while disabled has no player object;
        # replay the add event so re-enabling takes effect immediately.
        if (
            self.server_api.get_client(player_id) is not None
            and self.mass.players.get_player(player_id) is None
        ):
            event_version = self._begin_client_event(player_id)
            self.mass.create_task(self._handle_client_added(player_id, event_version))
            return
        super().on_player_enabled(player_id)

    def register_bridge_identifiers(
        self, client_id: str, identifiers: dict[IdentifierType, str]
    ) -> None:
        """
        Pre-register extra identifiers for a bridge client.

        Called by bridge managers (Chromecast, AirPlay) before registering an
        external player, so that the resulting SendspinPlayer carries the parent
        player's protocol-specific identifiers for cross-protocol matching.

        :param client_id: The bridge client_id that will be used for registration.
        :param identifiers: Extra identifiers to attach to the SendspinPlayer.
        """
        self._bridge_identifiers[client_id] = identifiers

    def register_bridge_underlying_player(self, client_id: str, underlying_player_id: str) -> None:
        """
        Pre-register the underlying player a bridge client runs on top of.

        Called by bridge managers before registering an external player, so that
        the resulting SendspinPlayer carries the derived-transport edge and the
        protocol linking layer can resolve its parent deterministically.

        :param client_id: The bridge client_id that will be used for registration.
        :param underlying_player_id: The player_id of the player the bridge rides on.
        """
        self._bridge_underlying_players[client_id] = underlying_player_id

    def register_bridge_static_delay_default(self, client_id: str, default_ms: int) -> None:
        """
        Register a protocol-specific default static delay for a bridge client.

        If the SendspinPlayer already exists, the default is applied immediately;
        otherwise it is stashed and picked up when the player is created.

        :param client_id: The bridge client_id for which the default applies.
        :param default_ms: Model-specific default static delay in milliseconds.
        """
        existing = self.mass.players.get_player(client_id)
        if isinstance(existing, SendspinPlayer):
            existing.static_delay_default_ms = default_ms
            # If no user-set value exists, push the new default to the device now
            # so already-connected clients pick it up without a config edit.
            if (
                self.mass.config.get_raw_player_config_value(client_id, CONF_SENDSPIN_STATIC_DELAY)
                is None
            ):
                self.mass.create_task(existing._apply_static_delay())
            return
        self._bridge_static_delay_defaults[client_id] = default_ms

    def register_headless_client(self, client_id: str) -> None:
        """
        Mark a client id as headless: no MA player is registered for it.

        Called by in-process consumers (e.g. the MilkDrop visualizer tap) whose
        Sendspin clients exist purely to receive group audio and must never
        surface as players anywhere in the UI or API.
        """
        self._headless_client_ids.add(client_id)

    def register_bridge_player_type(self, client_id: str, player_type: PlayerType) -> None:
        """
        Pre-register a PlayerType override for a bridge client.

        Called by bridge managers to set the player type for the resulting
        player (e.g. PlayerType.LIGHT for Hue Entertainment bridges).
        """
        self._bridge_player_types[client_id] = player_type

    async def apply_bridge_claim(
        self,
        client_id: str,
        identifiers: dict[IdentifierType, str],
        bridge_hello: ClientHelloPayload,
        underlying_player_id: str | None = None,
    ) -> bool:
        """
        Post-claim an already-registered SendspinPlayer as a bridge client.

        Used when a bridge manager reaches setup_bridge AFTER the external client
        has already connected on its own (e.g. a JS Cast receiver reconnecting to
        the server before the Chromecast bridge could register). Attaches the
        bridge's protocol-specific identifiers so cross-protocol matching can
        link the SendspinPlayer to its native peer, and replays the bridge
        hello's supported_commands restriction on the player features.

        :param client_id: The Sendspin client_id whose player should be claimed.
        :param identifiers: Protocol-specific identifiers (e.g. CAST_UUID) to
            attach to the player for cross-protocol matching.
        :param bridge_hello: The bridge's intended ClientHelloPayload. Its
            player_support.supported_commands gates which volume/mute features
            the player is allowed to expose.
        :param underlying_player_id: The player_id of the player the bridge rides
            on, establishing the derived-transport edge for protocol linking.
        :return: True if a matching SendspinPlayer was found and updated.
        """
        player = self.mass.players.get_player(client_id)
        if not isinstance(player, SendspinPlayer):
            return False
        for id_type, id_value in identifiers.items():
            player.device_info.add_identifier(id_type, id_value)
        if underlying_player_id is not None:
            player._attr_underlying_player_id = underlying_player_id
        bridge_supported_commands: list[PlayerCommand] = []
        if bridge_hello.player_support:
            bridge_supported_commands = list(bridge_hello.player_support.supported_commands)
        if PlayerCommand.VOLUME in bridge_supported_commands:
            player._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        else:
            player._attr_supported_features.discard(PlayerFeature.VOLUME_SET)
        if PlayerCommand.MUTE in bridge_supported_commands:
            player._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        else:
            player._attr_supported_features.discard(PlayerFeature.VOLUME_MUTE)
        # Expose the claimed player as a protocol bridge, not a standalone web
        # player. A JS Cast receiver advertises product_name="Web Browser" and
        # would otherwise be classified as is_web_player → PlayerType.PLAYER
        # (hidden). Restore protocol semantics so UI links it under its native peer.
        player.is_web_player = False
        player._attr_hidden_by_default = False
        player._attr_expose_to_ha_by_default = True
        player._attr_type = PlayerType.PROTOCOL
        self.logger.info(
            "Bridge claim applied to existing SendspinPlayer %s (client_id=%s)",
            player.display_name,
            client_id,
        )
        await self.mass.players.register_or_update(player)
        return True

    async def create_virtual_player(
        self,
        owner_instance_id: str,
        display_name: str,
        player_id: str | None = None,
    ) -> str:
        """
        Create a hidden, server-side virtual Sendspin player.

        A virtual player owns its own PlayerQueue and leads a native Sendspin
        group, but never renders audio itself: the audio stream is delivered to
        the guest players that are attached to it through standard grouping.
        It is hidden from the UI and not exposed to Home Assistant by default,
        and is automatically removed when the owning provider unloads.

        :param owner_instance_id: Instance id of the (loaded) provider that owns
            the virtual player and controls its lifecycle.
        :param display_name: Human readable name for the virtual player.
        :param player_id: Optional stable id for the virtual player; a random id
            is generated when omitted. The id is always prefixed with
            ``VIRTUAL_PLAYER_ID_PREFIX``.
        :return: The player_id of the registered virtual player.
        :raises SetupFailedError: If the virtual player can not be created.
        """
        if (owner := self.mass.get_provider(owner_instance_id)) is None:
            raise SetupFailedError(f"Owner provider {owner_instance_id} is not loaded")
        if owner.instance_id != owner_instance_id and (
            len(self.mass.get_provider_instances(owner.domain)) > 1
        ):
            raise SetupFailedError(
                f"Multiple instances exist for {owner_instance_id}: pass an exact instance id"
            )
        # normalize a provider domain to the actual instance id
        owner_instance_id = owner.instance_id
        if player_id is None:
            player_id = uuid4().hex
        elif not re.fullmatch(r"[a-zA-Z0-9_-]+", player_id):
            raise SetupFailedError(
                f"Invalid player_id {player_id}: only alphanumerics, '_' and '-' are allowed"
            )
        if not player_id.startswith(VIRTUAL_PLAYER_ID_PREFIX):
            player_id = f"{VIRTUAL_PLAYER_ID_PREFIX}{player_id}"
        if player_id in self._virtual_players or self.server_api.get_client(player_id) is not None:
            raise SetupFailedError(f"Virtual player {player_id} already exists")
        # a persisted config may only be reclaimed by the same owner
        stored_owner = self._get_virtual_player_config_owner(player_id)
        if stored_owner is not None and stored_owner != owner_instance_id:
            raise SetupFailedError(f"Virtual player {player_id} is owned by {stored_owner}")
        self._virtual_players[player_id] = owner_instance_id
        try:
            self._register_virtual_player_client(player_id, display_name)
            await self._wait_for_virtual_player(player_id)
        except asyncio.CancelledError:
            await self._cleanup_failed_virtual_player_creation(player_id)
            raise
        except Exception as err:
            await self._cleanup_failed_virtual_player_creation(player_id)
            if isinstance(err, SetupFailedError):
                raise
            raise SetupFailedError(f"Failed to create virtual player {player_id}") from err
        # persist the owner so orphaned configs can be swept after a restart
        self.mass.config.set_raw_player_config_value(
            player_id, CONF_VIRTUAL_PLAYER_OWNER, owner_instance_id
        )
        self.logger.info("Virtual player %s created for %s", player_id, owner_instance_id)
        return player_id

    async def remove_virtual_player(self, player_id: str) -> None:
        """
        Remove a virtual Sendspin player and permanently delete its configuration.

        :param player_id: The player_id returned by create_virtual_player.
        :raises ValueError: If the given player_id is not a (known) virtual player.
        """
        if not player_id.startswith(VIRTUAL_PLAYER_ID_PREFIX) or (
            player_id not in self._virtual_players
            and self._get_virtual_player_config_owner(player_id) is None
        ):
            raise ValueError(f"{player_id} is not a virtual player")
        # unregister the player first so the client removed event handler
        # can not race us with a non-permanent unregister
        await self.mass.players.unregister(player_id, permanent=True)
        if self.server_api.get_client(player_id) is not None:
            await self.server_api.remove_client(player_id)
        # the config may linger when the player was never registered
        self.mass.players.delete_player_config(player_id)
        self._virtual_players.pop(player_id, None)
        self.logger.info("Virtual player %s removed", player_id)

    def is_virtual_player(self, player_id: str) -> bool:
        """Return whether the given player_id belongs to a registered virtual player."""
        return player_id in self._virtual_players

    def get_pin_session(self, client_id: str) -> PinPairingSession | None:
        """Return the in-flight or just-finished PIN pairing session for a client."""
        return self._pin_sessions.get(client_id)

    def clear_pin_session(self, client_id: str) -> None:
        """Drop a finished PIN pairing session (after its outcome has been shown)."""
        session = self._pin_sessions.get(client_id)
        if session is not None and session.finished:
            self._cancel_pin_idle_timeout(client_id)
            self._pin_sessions.pop(client_id, None)
            if session.opened_management:
                self.exit_management(client_id)

    async def start_pin_pairing(
        self, client_id: str, *, verify: bool = False, static: bool = False
    ) -> PinPairingSession:
        """
        Begin (or retry in place) an operator PIN pairing attempt with a connected client.

        Returns once the client has asked for the PIN, the attempt has failed, or a short
        feedback window has elapsed, so the caller's first render reflects whether the
        device-side pairing gesture is still pending. The attempt keeps running until the PIN
        is supplied via submit_pin (or it times out / is cancelled). A session left retryable
        by a failed attempt is resumed in place, preserving the chosen method and verify mode.

        :param verify: Re-verify an already-paired device's presence (dynamic PIN only).
        :param static: Pair with the static PIN even when a dynamic PIN is offered.
        """
        session = self._pin_sessions.get(client_id)
        if session is not None and (session.verify != verify or session.static != static):
            # a stale session from an earlier run never resumes; the caller's
            # static/verify choice must win
            if session.attempt_running:
                raise SecurityActionError("pairing_error_concurrent")
            await self.cancel_pin_pairing(client_id)
            session = None
        if session is not None and session.can_retry:
            self._begin_pin_attempt(session)
            await self._pin_request_feedback(session)
            return session
        if session is not None and session.attempt_running:
            return session
        client = self.server_api.get_client(client_id)
        # A disconnected client keeps its last hello, so info alone does not prove it is connected.
        info = client.info_or_none if client is not None and client.is_connected else None
        if info is None:
            raise SecurityActionError("pairing_error_not_connected")
        offered = effective_pair_methods(info, self.pairing_config_snapshot(client_id))
        method = self._pick_pin_method(offered, verify=verify, static=static)
        pin_length = (
            # From the hello advertisement, not the live config: that is what the server's own
            # negotiation reads, so the predicted length matches the PIN the device derives.
            negotiated_pin_length(
                pair_method_descriptor(info.supported_pair_methods or (), PairMethod.DYNAMIC_PIN),
                self.server_api.min_pin_length,
            )
            if method is PairMethod.DYNAMIC_PIN
            else None
        )
        session = PinPairingSession(
            client_id=client_id,
            method=method,
            pin_future=self.mass.loop.create_future(),
            verify=verify,
            static=static,
            pin_length=pin_length,
            opened_management=await self._open_pairing_window(client_id),
        )
        self._pin_sessions[client_id] = session
        self._begin_pin_attempt(session)
        await self._pin_request_feedback(session)
        return session

    def submit_pin(self, client_id: str, pin: str) -> None:
        """Deliver the operator-entered PIN to the in-flight pairing attempt."""
        session = self._pin_sessions.get(client_id)
        if session is None or session.task is None:
            raise SecurityActionError("pairing_error_no_pin_session")
        if not session.pin_future.done():
            session.pin_future.set_result(pin.strip())

    async def cancel_pin_pairing(self, client_id: str) -> None:
        """Abort an in-flight or parked PIN pairing session, restoring normal service."""
        session = self._pin_sessions.pop(client_id, None)
        if session is None:
            return
        self._cancel_pin_idle_timeout(client_id)
        await self._end_pairing_quietly(client_id)
        if session.task is not None:
            with suppress(Exception):
                await session.task
        if session.opened_management:
            self.exit_management(client_id)
        await self._refresh_player(client_id)

    async def pair_with_token(
        self, client_id: str, token_value: str, owner: str | None = None
    ) -> None:
        """
        Pair a connected client using its pasted pairing token.

        :param client_id: The connected client to pair.
        :param token_value: The client's pairing token.
        :param owner: Application-defined authorization id to bind the pairing to;
            ``None`` is a standalone pairing.
        """
        session = self._pin_sessions.get(client_id)
        if session is not None and session.attempt_running:
            raise SecurityActionError("pairing_error_concurrent")
        try:
            token = decode_token(token_value)
        except ValueError as err:
            raise SecurityActionError("pairing_error_token_invalid") from err
        if token.client_id != client_id:
            raise SecurityActionError("pairing_error_token_mismatch")
        try:
            await self.server_api.initiate_pairing(
                client_id,
                PairingAttempt(PairMethod.PAIRING_PSK, pairing_psk=token.pairing_psk, owner=owner),
            )
        except PairingAbortError:
            # Token pairing is single-shot; unpark the connection before surfacing the failure.
            await self._end_pairing_quietly(client_id)
            raise
        except HandshakeAbortedError as err:
            # A client that does not recognize the token's PSK closes the connection
            # without an application-level error (spec); the server has disconnected it.
            raise PairingError(
                "the token was rejected by the device; make sure it is correct"
            ) from err
        await self._refresh_player(client_id)

    async def unpair_client(self, client_id: str) -> None:
        """Drop the pairing with a connected client (both sides forget the credential)."""
        await self.server_api.unpair(client_id)
        await self._refresh_player(client_id)

    async def set_trusted_unpaired(self, client_id: str, enabled: bool) -> None:
        """Approve or revoke unpaired (unauthenticated) playback for a client."""
        if enabled:
            await self.server_api.trust_unpaired(client_id)
        else:
            await self.server_api.untrust_unpaired(client_id)
        await self._refresh_player(client_id)

    async def pair_web_player(self, pairing_token: str) -> None:
        """
        Pair the built-in web player that minted the given pairing token.

        :param pairing_token: The calling web player's version 0 pairing token.
        """
        # The token names the client it belongs to, so this works on every transport,
        # including Ingress where the session carries no client id at all.
        try:
            client_id = decode_token(pairing_token).client_id
        except ValueError as err:
            raise InvalidCommand(
                "The pairing token is not valid",
                translation_key="pairing_error_token_invalid",
                translation_owner=self.translation_owner,
            ) from err
        player = await self._await_connected_client(client_id)
        if not player.is_web_player:
            raise InvalidCommand(f"Client {client_id} is not a built-in web player")
        security = player.api.connection_security
        # An unencrypted client cannot hold a pairing, same as the setup flow refuses it
        if security is None:
            return
        record = await self.server_api.pairing_store.record_by_client_id(client_id)
        # The pairing is bound to the caller's account: a guest's ends with their
        # session or access, a full user's with their account. Only pairings made
        # through the settings/setup flow are standalone.
        user = get_current_user()
        owner = credential_owner(user) if user is not None else None
        # We already paired this web player so no action needed. A record on its own is not
        # enough: the client can have lost its half, leaving a record it cannot authenticate.
        # A record bound to another account is re-paired instead (a browser keeps its
        # identity across logins), so the lifetime always follows the current caller.
        if (
            security.psk_category is PskCategory.LONG_TERM
            and record is not None
            and (record.owner is None or record.owner == owner)
        ):
            return
        try:
            await self.pair_with_token(client_id, pairing_token, owner=owner)
        except (
            SecurityActionError,
            PairingError,
            HandshakeAbortedError,
            TimeoutError,
            OSError,
        ) as err:
            # Report the reason without the request, which carries the pairing token.
            alert = error_alert(err)
            raise InvalidCommand(
                f"Cannot pair web player {client_id}",
                translation_key=alert.key,
                translation_args=alert.params,
                translation_owner=self.translation_owner,
            ) from err

    def get_management_session(self, client_id: str) -> ManagementSession | None:
        """Return the client's management session, dropping one whose connection is gone."""
        session = self._management_sessions.get(client_id)
        if session is None:
            return None
        client = self.server_api.get_client(client_id)
        if client is None or client.connection is not session.connection:
            self._drop_management_session(session)
            return None
        return session

    def enter_management(self, client_id: str) -> ManagementSession:
        """Open (or refresh) the operator management session for a paired connected client."""
        if (session := self.get_management_session(client_id)) is not None:
            self._arm_management_idle_timeout(session)
            return session
        try:
            connection = self.server_api.enable_management(client_id)
        except RuntimeError as err:
            raise SecurityActionError("management_error_generic", detail=str(err)) from err
        session = ManagementSession(client_id=client_id, connection=connection)
        self._management_sessions[client_id] = session
        self._arm_management_idle_timeout(session)
        return session

    def exit_management(self, client_id: str) -> None:
        """Close the client's management session, restoring normal server admission."""
        if (session := self._management_sessions.get(client_id)) is not None:
            self._drop_management_session(session)

    async def management_get_pairing_config(self, client_id: str) -> ManagementResultData:
        """Fetch the device's pairing configuration over its management session."""
        session = self._management_session_or_raise(client_id)
        async with session.lock:
            self._arm_management_idle_timeout(session)
            result, data, _ = await self._management_call(
                session.connection, session.connection.get_pairing_config()
            )
        _check_management_result(result)
        self._pairing_config_snapshots[client_id] = (session.connection, data)
        return data

    async def management_open_pairing_window(self, client_id: str) -> None:
        """Open a pairing window on the device over its management session, sparing the gesture."""
        session = self._management_session_or_raise(client_id)
        async with session.lock:
            self._arm_management_idle_timeout(session)
            result = await self._management_call(
                session.connection, session.connection.open_pairing_window()
            )
        _check_management_result(result)

    def pairing_config_snapshot(self, client_id: str) -> ManagementResultData | None:
        """
        Return the last management-fetched pairing config for the client's current connection.

        While the connection it was fetched on is still the active one, the snapshot is
        fresher than the hello advertisement (which cannot change until reconnect); after a
        reconnect the new hello is authoritative and the snapshot is dropped.
        """
        snapshot = self._pairing_config_snapshots.get(client_id)
        if snapshot is None:
            return None
        connection, data = snapshot
        client = self.server_api.get_client(client_id)
        if client is None or client.connection is not connection:
            self._pairing_config_snapshots.pop(client_id, None)
            return None
        return data

    async def management_set_pairing_config(
        self, client_id: str, patch: ManagementSetPairingConfigPayload
    ) -> None:
        """Apply a pairing-config patch on the device and refresh the cached snapshot."""
        session = self._management_session_or_raise(client_id)
        async with session.lock:
            self._arm_management_idle_timeout(session)
            result = await self._management_call(
                session.connection, session.connection.set_pairing_config(patch)
            )
        _check_management_result(result)
        await self.management_get_pairing_config(client_id)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SYNC_PLAYERS,
        }

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        self.unregister_cbs.append(
            self.mass.register_api_command(
                "sendspin/pair_web_player",
                self.pair_web_player,
                # Guests pair their own web player too, since party mode plays through Sendspin.
                required_scope=Scope.PLAYERS_CONTROL,
            )
        )
        # Pairings bound to a user's access must not outlive it (guest access switched
        # off, account deleted, all sessions revoked).
        self.unregister_cbs.append(
            self.mass.webserver.auth.subscribe_user_access_revoked(self._on_user_access_revoked)
        )
        self._remove_orphan_virtual_player_configs()
        # Start server for handling incoming Sendspin connections from clients
        # and mDNS discovery of new clients
        await self.server_api.start_server(
            port=SENDSPIN_SERVER_PORT,
            host=self.mass.streams.bind_ip,
            advertise_addresses=[self.mass.streams.publish_ip],
        )
        for address in self._manual_ip_config:
            try:
                url = _manual_client_url(address)
            except ValueError as err:
                self.logger.warning(
                    "Ignoring invalid manual Sendspin client address %s: %s", address, err
                )
                continue
            self.logger.debug("Connecting to manually configured Sendspin client at %s", url)
            self.server_api.connect_to_client(
                url,
                retry_initial_connection=True,
                retry_indefinitely=True,
            )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).

        :param is_removed: True when the provider is removed from the configuration.
        """
        self._unloading = True
        # call_later timers are not swept by mass.stop(), so cancel them explicitly here.
        for session in self._pin_sessions.values():
            if session.task is not None:
                session.task.cancel()
            self._cancel_pin_idle_timeout(session.client_id)
        self._pin_sessions.clear()
        for client_id in self._pending_pairing_evictions:
            self.mass.cancel_timer(_evict_session_pairing_task_id(client_id))
        self._pending_pairing_evictions.clear()
        for management_session in self._management_sessions.values():
            self.mass.cancel_timer(_management_idle_task_id(management_session.client_id))
        self._management_sessions.clear()
        self._pairing_config_snapshots.clear()
        player_ids = [player.player_id for player in self.players]
        # Stop the Sendspin server
        await self.server_api.close()

        for cb in self.unregister_cbs:
            cb()
        self.unregister_cbs = []
        self._client_event_task_counts.clear()
        self._client_event_versions.clear()
        self._virtual_players.clear()
        await asyncio.gather(
            *(
                self.mass.players.unregister(player_id, permanent=is_removed)
                for player_id in player_ids
            ),
            return_exceptions=True,
        )

    def _set_aiosendspin_log_level(self) -> None:
        """Keep aiosendspin's (very chatty) logging quiet unless verbose logging is enabled."""
        # aiosendspin logs every protocol message of every client session at debug
        # level, so only pass that through when verbose logging is enabled
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("aiosendspin").setLevel(logging.DEBUG)
        else:
            logging.getLogger("aiosendspin").setLevel(self.logger.level + 10)

    def _begin_client_event(self, client_id: str) -> int:
        """Increment version and in-flight task count for a client event."""
        version = self._client_event_versions.get(client_id, 0) + 1
        self._client_event_versions[client_id] = version
        self._client_event_task_counts[client_id] = (
            self._client_event_task_counts.get(client_id, 0) + 1
        )
        return version

    def _finish_client_event(self, client_id: str) -> None:
        """Drop in-flight bookkeeping and prune version state when idle."""
        task_count = self._client_event_task_counts.get(client_id, 0)
        if task_count <= 1:
            self._client_event_task_counts.pop(client_id, None)
            self._client_event_versions.pop(client_id, None)
            return
        self._client_event_task_counts[client_id] = task_count - 1

    def _is_current_client_event(self, client_id: str, event_version: int) -> bool:
        """Return True if the event version is still the latest for the client."""
        return self._client_event_versions.get(client_id) == event_version

    async def _apply_hass_esphome_enrichment(self, players: Sequence[SendspinBasePlayer]) -> None:
        """
        Apply Home Assistant-sourced enrichment to ESPHome-backed Sendspin players.

        Applies the HA display name and resolves the HA media_player entity that
        announcements are relayed to: ESPHome devices support announcements
        natively, but that capability is only reachable through the HA API.
        Players are correlated by MAC address (the Sendspin client id).
        """
        esphome_players = [
            player for player in players if player.device_info.manufacturer == "ESPHome"
        ]
        if not esphome_players:
            return
        hass = cast("HomeAssistantProvider | None", self.mass.get_provider("hass"))
        if hass is None or not hass.available:
            for player in esphome_players:
                if isinstance(player, SendspinPlayer):
                    player.set_hass_announce_entity(None)
            return
        try:
            device_infos = await hass.get_media_player_device_infos(
                [player.player_id for player in esphome_players], platform="esphome"
            )
        except Exception as err:
            self.logger.warning("Failed to apply Home Assistant enrichment: %s", err)
            return
        for player in esphome_players:
            device_info = device_infos.get(player.player_id.lower())
            if device_info is not None and device_info["name"]:
                player._attr_name = device_info["name"]
            if isinstance(player, SendspinPlayer):
                player.set_hass_announce_entity(
                    device_info["announce_entity_id"] if device_info is not None else None
                )

    async def _refresh_hass_esphome_enrichment(self) -> None:
        """Re-apply the HA enrichment to all registered ESPHome players (in place)."""
        players = [
            player
            for player in self.players
            if isinstance(player, SendspinBasePlayer)
            and player.device_info.manufacturer == "ESPHome"
        ]
        if not players:
            return
        await self._apply_hass_esphome_enrichment(players)
        for player in players:
            if player.initialized.is_set():
                player.update_state()

    def _create_player(
        self,
        client_id: str,
        sendspin_client: SendspinClient,
        existing_player: Player | None,
        initial_hello: ClientHelloPayload | None = None,
    ) -> SendspinBasePlayer:
        """
        Create the appropriate player class based on client roles.

        Priority: player role -> SendspinPlayer, metadata role -> DISPLAY,
        visualizer role -> VISUALIZER. Bridge-registered type overrides the default.
        """
        extra_ids = self._bridge_identifiers.pop(client_id, None)
        bridge_player_type = self._bridge_player_types.pop(client_id, None)
        underlying_player_id = self._bridge_underlying_players.pop(client_id, None)
        if underlying_player_id is None and existing_player is not None:
            underlying_player_id = existing_player.underlying_player_id
        static_delay_default_ms = self._bridge_static_delay_defaults.pop(client_id, None)
        if static_delay_default_ms is None and isinstance(existing_player, SendspinPlayer):
            static_delay_default_ms = existing_player.static_delay_default_ms

        # Select on negotiated (not active) roles: activation follows pairing/trust state,
        # which must not change the player class.
        negotiated_families = {
            role_family(role_id) for role_id in sendspin_client.negotiated_role_ids
        }
        has_player_role = "player" in negotiated_families
        has_metadata_role = "metadata" in negotiated_families
        has_visualizer_role = "visualizer" in negotiated_families

        if has_player_role:
            audio_player = SendspinPlayer(self, client_id, initial_hello=initial_hello)
            if isinstance(existing_player, SendspinPlayer):
                audio_player.preserve_control_features_from(existing_player)
            player: SendspinBasePlayer = audio_player
        elif has_metadata_role or has_visualizer_role:
            default_type = PlayerType.DISPLAY if has_metadata_role else PlayerType.VISUALIZER
            viz_player = SendspinVisualizerPlayer(self, client_id, initial_hello=initial_hello)
            viz_player._attr_type = bridge_player_type or default_type
            player = viz_player
        else:
            audio_player = SendspinPlayer(self, client_id, initial_hello=initial_hello)
            if isinstance(existing_player, SendspinPlayer):
                audio_player.preserve_control_features_from(existing_player)
            player = audio_player

        if extra_ids:
            for id_type, id_value in extra_ids.items():
                player.device_info.add_identifier(id_type, id_value)
        if underlying_player_id is not None:
            player._attr_underlying_player_id = underlying_player_id
        if static_delay_default_ms is not None and isinstance(player, SendspinPlayer):
            player.static_delay_default_ms = static_delay_default_ms
        return player

    @staticmethod
    def _pick_pin_method(
        offered: list[PairMethodDescriptor], *, verify: bool = False, static: bool = False
    ) -> PairMethod:
        """
        Select the preferred usable PIN method from the client's offer.

        :param verify: Restrict to dynamic PIN, the only method that proves device presence.
        :param static: Restrict to static PIN, overriding the dynamic-first default.
        """
        wanted: tuple[PairMethod, ...]
        if verify:
            wanted = (PairMethod.DYNAMIC_PIN,)
        elif static:
            wanted = (PairMethod.STATIC_PIN,)
        else:
            wanted = (PairMethod.DYNAMIC_PIN, PairMethod.STATIC_PIN)
        offered_methods = {descriptor.method for descriptor in offered}
        for method in wanted:
            if method in offered_methods:
                return method
        raise SecurityActionError("pairing_error_no_pin_method")

    async def _open_pairing_window(self, client_id: str) -> bool:
        """
        Open a pairing window over management, sparing the operator the device-side gesture.

        Only works before the attempt starts: the pairing activate takes management off the
        connection's activities. Returns whether a management session was opened here,
        for the caller to close once the pairing session ends.
        """
        opened = self.get_management_session(client_id) is None
        keep = False
        try:
            self.enter_management(client_id)
            await self.management_open_pairing_window(client_id)
            keep = opened
        except SecurityActionError as err:
            self.logger.debug("No pairing window opened on %s: %s", client_id, err)
        finally:
            # Hand back a session opened here unless the caller inherits it, cancellation included.
            if opened and not keep:
                self.exit_management(client_id)
        return keep

    def _begin_pin_attempt(self, session: PinPairingSession) -> None:
        """Start or restart a pairing attempt for the session, resetting per-attempt state."""
        self._cancel_pin_idle_timeout(session.client_id)
        session.error = None
        session.retryable = False
        session.pin_request_event.clear()
        session.gesture_event.clear()
        if session.pin_future.done():
            session.pin_future = self.mass.loop.create_future()
        session.task = self.mass.create_task(self._run_pin_pairing(session))

    async def _pin_request_feedback(self, session: PinPairingSession) -> None:
        """Wait briefly for the attempt to reach the PIN wait (or end), for an accurate render."""
        if session.task is None:
            return
        waiter = self.mass.create_task(session.wait_first_message())
        try:
            await asyncio.wait((waiter,), timeout=PIN_REQUEST_FEEDBACK_TIMEOUT)
        finally:
            waiter.cancel()

    async def _run_pin_pairing(self, session: PinPairingSession) -> None:
        """Run one PIN pairing attempt, classifying the outcome for the UI."""

        def pin_provider() -> asyncio.Future[str]:
            # Invoked only once the client's pair-init has arrived (post-gesture).
            session.pin_request_event.set()
            return session.pin_future

        def on_pair_pending() -> None:
            session.gesture_event.set()

        try:
            await self.server_api.initiate_pairing(
                session.client_id,
                PairingAttempt(
                    session.method,
                    pin_provider=pin_provider,
                    verify=session.verify,
                    on_pair_pending=on_pair_pending,
                    languages=self._spoken_pin_languages()
                    if session.method is PairMethod.DYNAMIC_PIN
                    else (),
                ),
            )
        except PairingTimeoutError as err:
            # The device never answered; aiosendspin cancelled the attempt in band and left
            # pairing, so the connection is still usable and a retry can start afresh.
            session.error = err
            self.logger.debug("PIN pairing with %s timed out: %s", session.client_id, err)
            session.retryable = True
            self._arm_pin_idle_timeout(session)
        except PairingAbortError as err:
            if (
                isinstance(err, LocalPairingAbortError)
                and err.reason is PairAbortReason.USER_CANCELLED
            ):
                # Our own end_pairing cancelled this attempt; the connection is already restored.
                return
            session.error = err
            self.logger.debug("PIN pairing with %s aborted: %s", session.client_id, err)
            session.retryable = True
            self._arm_pin_idle_timeout(session)
        except Exception as err:
            # A non-abort failure: the server has already disconnected the client.
            session.error = err
            self.logger.debug("PIN pairing with %s failed: %s", session.client_id, err)
        else:
            await self._refresh_player(session.client_id)
        finally:
            if not session.pin_future.done():
                session.pin_future.cancel()

    def _spoken_pin_languages(self) -> tuple[str, ...]:
        """
        Return the language preference for a spoken dynamic PIN, most preferred first.

        The metadata locale is the only server-wide language setting, so it stands in for the
        operator's own preference.
        """
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        return (locale, language) if language != locale else (locale,)

    def _arm_pin_idle_timeout(self, session: PinPairingSession) -> None:
        """Schedule restoration of the connection if a failed attempt is left unretried."""
        self.mass.call_later(
            PIN_RETRY_IDLE_TIMEOUT,
            self._pin_idle_timeout,
            session,
            task_id=_pin_idle_task_id(session.client_id),
        )

    def _cancel_pin_idle_timeout(self, client_id: str) -> None:
        """Cancel a pairing idle timeout, whether still pending or already firing."""
        task_id = _pin_idle_task_id(client_id)
        self.mass.cancel_timer(task_id)
        self.mass.cancel_task(task_id)

    async def _pin_idle_timeout(self, session: PinPairingSession) -> None:
        """Terminate an abandoned retryable session, restoring the connection to service."""
        session.retryable = False
        session.error = TimeoutError("timed out waiting for a pairing retry")
        await self._end_pairing_quietly(session.client_id)
        await self._refresh_player(session.client_id)

    async def _end_pairing_quietly(self, client_id: str) -> None:
        """End pairing on a client, tolerating an already-gone connection."""
        try:
            await self.server_api.end_pairing(client_id)
        except Exception as err:
            self.logger.debug("Ending pairing for %s failed: %s", client_id, err)

    def _management_session_or_raise(self, client_id: str) -> ManagementSession:
        """Return the client's management session or raise if none is open."""
        session = self.get_management_session(client_id)
        if session is None:
            raise SecurityActionError("management_error_no_session")
        return session

    async def _management_call[T](self, connection: SendspinConnection, request: Awaitable[T]) -> T:
        """Run a management request with a timeout, mapping transport failures to SecurityActionError."""
        try:
            async with asyncio.timeout(MANAGEMENT_REQUEST_TIMEOUT):
                return await request
        except TimeoutError as err:
            # Replies match requests by order with no id, so a timed-out request left in
            # flight would desync the next one; drop the connection to reset the channel.
            await connection.disconnect()
            raise SecurityActionError("management_error_timeout") from err
        except RuntimeError as err:
            raise SecurityActionError("management_error_generic", detail=str(err)) from err

    def _arm_management_idle_timeout(self, session: ManagementSession) -> None:
        """(Re)start the idle countdown that closes an abandoned management session."""
        self.mass.call_later(
            MANAGEMENT_IDLE_TIMEOUT,
            self._drop_management_session,
            session,
            task_id=_management_idle_task_id(session.client_id),
        )

    def _drop_management_session(self, session: ManagementSession) -> None:
        """Remove a management session, releasing its hold on the connection."""
        self.mass.cancel_timer(_management_idle_task_id(session.client_id))
        self._management_sessions.pop(session.client_id, None)
        with suppress(Exception):
            session.connection.disable_management()

    async def _refresh_player(self, client_id: str) -> None:
        """Re-evaluate a registered player after a pairing/trust change (in place)."""
        player = self.mass.players.get_player(client_id)
        if not isinstance(player, SendspinBasePlayer) or not player.initialized.is_set():
            return
        # A trust change (de)activates roles; role instances are recreated on activation,
        # so pushed config (preferred format, static delay) must be re-applied.
        await player.on_config_updated()
        player.update_state()

    def _on_user_access_revoked(self, user: User) -> None:
        """Handle a user's access being withdrawn (tokens revoked or account deleted)."""
        # Both owner forms, so the match cannot depend on the user's role at mint time.
        for owner in credential_owners_for_user_id(user.user_id):
            self.mass.create_task(self._evict_pairings_for_owner(owner))

    async def _evict_pairings_for_owner(self, owner: str) -> None:
        """Drop every pairing bound to ``owner``, unpairing connected clients in-band."""
        for record in await self.server_api.pairing_store.records_by_owner(owner):
            try:
                await self.server_api.unpair(record.client_id)
            except ValueError:
                # Not connected: there is no client half to notify, drop only our record.
                await self.server_api.pairing_store.remove_record(record.client_id)
            self.logger.info(
                "Removed the pairing of client %s: its owner's access was revoked",
                record.client_id,
            )
            await self._refresh_player(record.client_id)

    async def _evict_session_pairing(self, client_id: str) -> None:
        """Drop a disconnected client's session-scoped pairing (a no-op for durable ones)."""
        self._pending_pairing_evictions.discard(client_id)
        if self._unloading:
            return
        client = self.server_api.get_client(client_id)
        if client is not None and client.is_connected:
            # Already reconnected: the pairing lives until the connection truly ends.
            return
        record = await self.server_api.pairing_store.record_by_client_id(client_id)
        if record is None or record.owner is None or not is_session_scoped_owner(record.owner):
            return
        await self.server_api.pairing_store.remove_record(client_id)
        self.logger.info("Removed the session-scoped pairing of client %s on disconnect", client_id)
        await self._refresh_player(client_id)

    async def _await_connected_client(self, client_id: str) -> SendspinBasePlayer:
        """Return a client's fully registered player, waiting for its connection to land first."""

        # A web player asks to be paired a beat before its Sendspin handshake lands, and
        # the pairing refresh needs a fully registered player to re-apply config onto.
        def _registered_player() -> SendspinBasePlayer | None:
            client = self.server_api.get_client(client_id)
            if client is None or not client.is_connected:
                return None
            player = self.mass.players.get_player(client_id)
            if isinstance(player, SendspinBasePlayer) and player.initialized.is_set():
                return player
            return None

        player = await _poll_until(_registered_player, WEB_PLAYER_CONNECT_TIMEOUT)
        if player is None:
            raise InvalidCommand(f"Client {client_id} did not register")
        return player

    async def _handle_client_added(self, client_id: str, event_version: int) -> None:
        """Handle a new client connection asynchronously."""
        try:
            if self._unloading or client_id in self._headless_client_ids:
                return
            sendspin_client = self.server_api.get_client(client_id)
            if sendspin_client is None:
                self.logger.debug("Client %s disconnected before add handling started", client_id)
                return
            bridge_hello_snapshot = None
            if (
                client_id in self._bridge_identifiers
                and (bridge_hello := sendspin_client.info_or_none) is not None
            ):
                # Snapshot the bridges hello before a reconnect can overwrite it
                bridge_hello_snapshot = deepcopy(bridge_hello)
            if pending_event := self._pending_unregisters.get(client_id):
                self.logger.debug(
                    "Waiting for pending unregister of %s before registering", client_id
                )
                await pending_event.wait()
                if not self._is_current_client_event(client_id, event_version):
                    self.logger.debug("Skipping stale add event for %s after waiting", client_id)
                    return
            # Check if client still exists (may have disconnected while waiting)
            sendspin_client = self.server_api.get_client(client_id)
            if sendspin_client is None:
                self.logger.debug("Client %s disconnected before hello completed", client_id)
                return
            # Wait for client hello to be processed (info becomes available)
            # ClientAddedEvent fires before the hello handshake completes
            for _ in range(50):  # Wait up to 5 seconds
                if sendspin_client.info_or_none is not None:
                    break
                await asyncio.sleep(0.1)
            else:
                self.logger.warning("Client %s hello not received within timeout", client_id)
                return
            if not self._is_current_client_event(client_id, event_version):
                self.logger.debug("Skipping stale add event for %s", client_id)
                return
            if not self.mass.config.get_raw_player_config_value(client_id, CONF_ENABLED, True):
                self.logger.debug("Ignoring disabled sendspin client: %s", client_id)
                return
            existing_player = self.mass.players.get_player(client_id)
            preserved_identifiers = (
                dict(existing_player.device_info.identifiers) if existing_player is not None else {}
            )
            if existing_player is not None:
                self.logger.debug("Refreshing existing player object for %s", client_id)
                await self.mass.players.unregister(client_id)
                if not self._is_current_client_event(client_id, event_version):
                    self.logger.debug("Skipping stale add event for %s after unregister", client_id)
                    return
                sendspin_client = self.server_api.get_client(client_id)
                if sendspin_client is None:
                    self.logger.debug("Client %s disconnected after unregister", client_id)
                    return

            player = self._create_player(
                client_id, sendspin_client, existing_player, bridge_hello_snapshot
            )
            for id_type, id_value in preserved_identifiers.items():
                player.device_info.add_identifier(id_type, id_value)
            self.logger.debug("Client %s connected", client_id)
            await self._apply_hass_esphome_enrichment([player])
            if not self._is_current_client_event(client_id, event_version):
                self.logger.debug("Skipping stale add event for %s after HA enrichment", client_id)
                player._unsubscribe_client_callbacks()
                return
            try:
                await self.mass.players.register(player)
            except AlreadyRegisteredError:
                self.logger.debug(
                    "Client %s already registered while handling add event", client_id
                )
                player._unsubscribe_client_callbacks()
        finally:
            self._finish_client_event(client_id)

    async def _handle_client_removed(self, client_id: str, event_version: int) -> None:
        """Handle a client disconnection asynchronously."""
        try:
            if self._unloading:
                return
            if client_id in self._headless_client_ids:
                self._headless_client_ids.discard(client_id)
                return
            self.logger.debug("Client %s disconnected", client_id)
            if not self._is_current_client_event(client_id, event_version):
                self.logger.debug("Skipping stale remove event for %s", client_id)
                return
            unregister_event = asyncio.Event()
            self._pending_unregisters[client_id] = unregister_event
            try:
                await self.mass.players.unregister(client_id)
            finally:
                self._pending_unregisters.pop(client_id, None)
                unregister_event.set()
        finally:
            self._finish_client_event(client_id)

    async def _handle_client_updated(self, client_id: str, event_version: int) -> None:
        """Handle a client whose hello payload changed on reconnect."""
        try:
            if self._unloading:
                return
            if pending_event := self._pending_unregisters.get(client_id):
                self.logger.debug("Waiting for pending unregister of %s before updating", client_id)
                await pending_event.wait()
                if not self._is_current_client_event(client_id, event_version):
                    self.logger.debug("Skipping stale update event for %s after waiting", client_id)
                    return
            sendspin_client = self.server_api.get_client(client_id)
            if sendspin_client is None:
                return
            if not self._is_current_client_event(client_id, event_version):
                self.logger.debug("Skipping stale update event for %s", client_id)
                return
            existing_player = self.mass.players.get_player(client_id)
            if not isinstance(existing_player, SendspinBasePlayer):
                return
            previous_device_info = existing_player.device_info
            previous_type = existing_player.type
            existing_player._refresh_client_info(sendspin_client)
            if isinstance(existing_player, SendspinPlayer):
                existing_player.restore_bridge_identity(previous_device_info, previous_type)
            await self._apply_hass_esphome_enrichment([existing_player])
            if not self._is_current_client_event(client_id, event_version):
                self.logger.debug("Skipping stale update event for %s after refresh", client_id)
                return
            if previous_type == PlayerType.PROTOCOL and existing_player.type != PlayerType.PROTOCOL:
                existing_player.set_protocol_parent_id(None)
                existing_player._attr_underlying_player_id = None
            await self.mass.players.register_or_update(existing_player)
        finally:
            self._finish_client_event(client_id)

    def _get_virtual_player_config_owner(self, player_id: str) -> str | None:
        """Return the owner instance id from a stored virtual player config, if any."""
        raw_conf = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if not isinstance(raw_conf, dict) or raw_conf.get("provider") != self.instance_id:
            return None
        values = raw_conf.get("values")
        if not isinstance(values, dict):
            return None
        return cast("str | None", values.get(CONF_VIRTUAL_PLAYER_OWNER))

    def _register_virtual_player_client(self, player_id: str, display_name: str) -> None:
        """Register the silent external Sendspin client backing a virtual player."""
        hello = ClientHelloPayload(
            client_id=player_id,
            name=display_name,
            version=1,
            supported_roles=[BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(
                product_name="Virtual Player",
                manufacturer="Music Assistant",
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM,
                        channels=BRIDGE_CHANNELS,
                        sample_rate=BRIDGE_SAMPLE_RATE,
                        bit_depth=BRIDGE_BIT_DEPTH,
                    )
                ],
                buffer_capacity=1_000,
                supported_commands=[],
            ),
        )
        sendspin_client = self.server_api.register_external_player(
            hello, on_stream_start=self._on_virtual_player_stream_start
        )
        for role in sendspin_client.roles_by_family("player"):
            if not isinstance(role, BridgePlayerRole):
                continue
            # audio delivered to the role is simply discarded: the virtual player
            # only anchors the group, the members receive the actual stream
            role.set_callbacks(
                on_audio_chunk=_virtual_player_noop,
                on_volume_change=_virtual_player_noop,
                on_mute_change=_virtual_player_noop,
                on_stream_start=_virtual_player_noop,
                on_stream_end=_virtual_player_noop,
            )
            role.setup_audio_requirements()
            role.set_timing(required_lead_time_ms=0, min_buffer_ms=0)
            break

    async def _wait_for_virtual_player(self, player_id: str) -> None:
        """Wait until the virtual player is registered in MA with its queue."""

        def _registered() -> bool | None:
            if (
                self.mass.players.get_player(player_id) is not None
                and self.mass.player_queues.get(player_id) is not None
            ):
                return True
            return None

        if await _poll_until(_registered, VIRTUAL_PLAYER_REGISTER_TIMEOUT) is None:
            raise SetupFailedError(f"Virtual player {player_id} was not registered in time")

    async def _cleanup_failed_virtual_player_creation(self, player_id: str) -> None:
        """
        Remove a virtual player after its creation does not complete.

        :param player_id: Virtual player to remove.
        """
        last_error: Exception | None = None
        for delay in VIRTUAL_PLAYER_CLEANUP_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(VIRTUAL_PLAYER_CLEANUP_TIMEOUT):
                    await self.remove_virtual_player(player_id)
                return
            except Exception as err:
                last_error = err
        self.logger.warning(
            "Could not clean up failed virtual player creation %s: %s",
            player_id,
            last_error,
        )

    def _on_virtual_player_stream_start(self, _request: ExternalStreamStartRequest) -> None:
        """Accept stream start requests for virtual players (nothing to connect)."""

    async def _on_providers_updated(self, event: MassEvent) -> None:
        """Handle a change in the loaded providers."""
        # during (server) shutdown all providers unload; the startup sweep
        # takes care of configs whose owner is really gone
        if self._unloading or self.mass.closing:
            return
        # remove virtual players whose owning provider is no longer loaded
        for player_id, owner_instance_id in list(self._virtual_players.items()):
            if self.mass.get_provider(owner_instance_id) is not None:
                continue
            self.logger.debug(
                "Removing virtual player %s: owner %s unloaded", player_id, owner_instance_id
            )
            await self.remove_virtual_player(player_id)
        # (re)apply the HA-backed enrichment when the hass plugin (un)loads
        hass = self.mass.get_provider("hass")
        hass_available = hass is not None and hass.available
        if hass_available != self._hass_available:
            self._hass_available = hass_available
            await self._refresh_hass_esphome_enrichment()

    def _remove_orphan_virtual_player_configs(self) -> None:
        """Delete stored configs of virtual players whose owner provider is gone."""
        all_player_configs = self.mass.config.get(CONF_PLAYERS, {})
        for player_id, raw_conf in list(all_player_configs.items()):
            if not isinstance(raw_conf, dict) or raw_conf.get("provider") != self.instance_id:
                continue
            values = raw_conf.get("values")
            if not isinstance(values, dict):
                values = {}
            owner_instance_id = values.get(CONF_VIRTUAL_PLAYER_OWNER)
            if owner_instance_id is None:
                continue
            if self.mass.config.get(f"{CONF_PROVIDERS}/{owner_instance_id}") is not None:
                # owner still configured; it will recreate its virtual players
                continue
            self.logger.debug("Removing orphan virtual player config %s", player_id)
            self.mass.players.delete_player_config(player_id)


def _virtual_player_noop(*_args: object) -> None:
    """No-op callback for virtual players, which never render audio locally."""
