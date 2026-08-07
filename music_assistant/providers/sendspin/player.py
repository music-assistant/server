"""Sendspin Player implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from io import BytesIO
from typing import TYPE_CHECKING, ClassVar, cast

from aiosendspin.models import AudioCodec, MediaCommand
from aiosendspin.models.management import (
    ManagementSetPairingConfigPayload,
    SetDynamicPinConfig,
    SetStaticPinConfig,
    SetUnpairedAccessConfig,
)
from aiosendspin.models.types import PairMethod, PlaybackStateType, PlayerCommand
from aiosendspin.models.types import RepeatMode as SendspinRepeatMode
from aiosendspin.models.visualizer import BeatAvailability, BeatTiming
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.pairing import (
    SERVER_FIRST_MESSAGE_TIMEOUT_S,
    SERVER_GESTURE_TIMEOUT_S,
    PairingError,
)
from aiosendspin.noise.trust_store import PskCategory
from aiosendspin.server import ClientEvent, GroupEvent, SendspinGroup, VolumeChangedEvent
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.client import DisconnectBehaviour
from aiosendspin.server.events import (
    ClientGroupChangedEvent,
    GroupDeletedEvent,
    GroupMemberAddedEvent,
    GroupMemberRemovedEvent,
    GroupStateChangedEvent,
)
from aiosendspin.server.roles import (
    ArtworkGroupRole,
    ColorGroupRole,
    ControllerEvent,
    ControllerGroupRole,
    ControllerNextEvent,
    ControllerPauseEvent,
    ControllerPlayEvent,
    ControllerPreviousEvent,
    ControllerRepeatEvent,
    ControllerSeekEvent,
    ControllerSeekRelativeEvent,
    ControllerShuffleEvent,
    ControllerStopEvent,
    MetadataGroupRole,
    VisualizerGroupRole,
)
from aiosendspin.server.roles.color.state import Color
from aiosendspin.server.roles.metadata.state import Metadata
from aiosendspin.server.roles.player.events import StaticDelayChangedEvent
from aiosendspin.server.roles.player.types import PlayerRoleProtocol
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderType,
    RepeatMode,
)
from music_assistant_models.errors import MediaNotFoundError, PlayerCommandFailed
from music_assistant_models.media_items import Album, Artist, is_track
from music_assistant_models.player import DeviceInfo
from PIL import Image

from music_assistant.constants import HIDDEN_ANNOUNCE_VOLUME_CONFIG_ENTRIES
from music_assistant.controllers.streams.audio_analysis import SMART_FADES_ANALYSIS_DOMAIN
from music_assistant.helpers.util import is_valid_mac_address, join_task
from music_assistant.models.player import Player, PlayerMedia
from music_assistant.models.setup_flow import AbortFlow, StepExpiredError

from .constants import (
    BRIDGE_PREFIX,
    CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_DISABLE,
    CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_ENABLE,
    CONF_ACTION_MANAGEMENT_ENTER,
    CONF_ACTION_MANAGEMENT_EXIT,
    CONF_ACTION_MANAGEMENT_STATIC_PIN_DISABLE,
    CONF_ACTION_MANAGEMENT_STATIC_PIN_ENABLE,
    CONF_ACTION_MANAGEMENT_UNPAIRED_DISABLE,
    CONF_ACTION_MANAGEMENT_UNPAIRED_ENABLE,
    CONF_ACTION_REVOKE_UNPAIRED,
    CONF_ACTION_UNPAIR,
    CONF_CAST_AUDIO_UNSUPPORTED,
    CONF_PAIRING_METHOD,
    CONF_PAIRING_PIN,
    CONF_PAIRING_TOKEN,
    CONF_SENDSPIN_STATIC_DELAY,
    DEFAULT_SENDSPIN_STATIC_DELAY,
    PAIR_METHOD_DYNAMIC_PIN,
    PAIR_METHOD_PIN,
    PAIR_METHOD_STATIC_PIN,
    PAIR_METHOD_TOKEN,
    PAIR_METHOD_UNPAIRED,
)
from .helpers import (
    AlertText,
    SecurityActionError,
    action_entry,
    alert_entry,
    effective_pair_methods,
    effective_unpaired_access,
    error_alert,
    mac_from_bridge_client_id,
    pair_method_descriptor,
)
from .playback import SendspinPlaybackSession

# Supported group commands for Sendspin players
SUPPORTED_GROUP_COMMANDS = [
    MediaCommand.PLAY,
    MediaCommand.PAUSE,
    MediaCommand.STOP,
    MediaCommand.NEXT,
    MediaCommand.PREVIOUS,
    MediaCommand.REPEAT_OFF,
    MediaCommand.REPEAT_ONE,
    MediaCommand.REPEAT_ALL,
    MediaCommand.SHUFFLE,
    MediaCommand.UNSHUFFLE,
    MediaCommand.SEEK,
    MediaCommand.SEEK_RELATIVE,
]

# Config constants for Sendspin audio format
CONF_PREFERRED_SENDSPIN_FORMAT = "preferred_sendspin_format"
SENDSPIN_FORMAT_AUTOMATIC = "automatic"


def format_to_option_value(fmt: SupportedAudioFormat) -> str:
    """Convert SupportedAudioFormat to "codec:sample_rate:bit_depth:channels"."""
    return f"{fmt.codec.value}:{fmt.sample_rate}:{fmt.bit_depth}:{fmt.channels}"


def option_value_to_format(value: str) -> tuple[AudioCodec, SendspinAudioFormat] | None:
    """
    Parse option value back to (AudioCodec, SendspinAudioFormat).

    :param value: Option value in format "codec:sample_rate:bit_depth:channels".
    :return: Tuple of (AudioCodec, SendspinAudioFormat) or None if parsing fails.
    """
    try:
        codec_str, sample_rate_str, bit_depth_str, channels_str = value.split(":")
        codec = AudioCodec(codec_str)
        audio_format = SendspinAudioFormat(
            sample_rate=int(sample_rate_str),
            bit_depth=int(bit_depth_str),
            channels=int(channels_str),
        )
        return (codec, audio_format)
    except ValueError, KeyError:
        return None


def format_to_display_string(fmt: SupportedAudioFormat) -> str:
    """Convert to display string like "FLAC 48kHz/24bit stereo"."""
    codec_name = fmt.codec.name
    sample_rate_khz = fmt.sample_rate / 1000
    # Format sample rate: show as integer if whole number, otherwise one decimal
    if sample_rate_khz == int(sample_rate_khz):
        sample_rate_str = f"{int(sample_rate_khz)}kHz"
    else:
        sample_rate_str = f"{sample_rate_khz:.1f}kHz"
    if fmt.channels == 2:
        channels_str = "stereo"
    elif fmt.channels == 1:
        channels_str = "mono"
    else:
        channels_str = f"{fmt.channels}ch"
    return f"{codec_name} {sample_rate_str}/{fmt.bit_depth}bit {channels_str}"


_MANAGEMENT_ACTIONS = {
    CONF_ACTION_MANAGEMENT_UNPAIRED_ENABLE: ManagementSetPairingConfigPayload(
        unpaired_access=SetUnpairedAccessConfig(enabled=True)
    ),
    CONF_ACTION_MANAGEMENT_UNPAIRED_DISABLE: ManagementSetPairingConfigPayload(
        unpaired_access=SetUnpairedAccessConfig(enabled=False)
    ),
    CONF_ACTION_MANAGEMENT_STATIC_PIN_ENABLE: ManagementSetPairingConfigPayload(
        static_pin=SetStaticPinConfig(enabled=True)
    ),
    CONF_ACTION_MANAGEMENT_STATIC_PIN_DISABLE: ManagementSetPairingConfigPayload(
        static_pin=SetStaticPinConfig(enabled=False)
    ),
    CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_ENABLE: ManagementSetPairingConfigPayload(
        dynamic_pin=SetDynamicPinConfig(enabled=True)
    ),
    CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_DISABLE: ManagementSetPairingConfigPayload(
        dynamic_pin=SetDynamicPinConfig(enabled=False)
    ),
}

# Seconds the setup flow gives the operator to enter the PIN. Advisory: the authoritative end
# is the device's own attempt timeout (spec-recommended 2 minutes), which aborts the attempt.
PAIR_PIN_ENTRY_TIMEOUT = 120.0
# Seconds the setup flow waits for pairing to complete after the PIN is submitted.
PAIR_CONFIRM_TIMEOUT = 30.0

# Terminal pairing-error slugs that map to a dedicated setup_flow.abort reason;
# anything else falls back to the generic "pairing_failed" abort.
_PAIRING_ABORT_REASONS = {
    "pairing_error_concurrent": "pairing_error_concurrent",
    "pairing_error_no_pin_method": "no_pair_methods",
    "pairing_error_not_connected": "pairing_error_not_connected",
}

# How the operator gets a method's secret, per the device's own descriptor hints: where a
# configured secret is found, or which channel conveys a per-session PIN. Values outside these
# maps render nothing.
_SECRET_HINT_LABELS = {
    PairMethod.STATIC_PIN: {
        "device": "static_pin_location_device",
        "leaflet": "static_pin_location_leaflet",
        "operator": "static_pin_location_operator",
    },
    PairMethod.PAIRING_PSK: {
        "device": "pairing_psk_location_device",
        "leaflet": "pairing_psk_location_leaflet",
        "operator": "pairing_psk_location_operator",
    },
    PairMethod.DYNAMIC_PIN: {
        "display": "dynamic_pin_channel_display",
        "speaker": "dynamic_pin_channel_speaker",
    },
}


def _pin_error_slug(error: Exception | None) -> str:
    """Return the strings.json errors slug for a retryable PIN failure (re-rendered form)."""
    if error is None:
        return "pairing_error_generic"
    return error_alert(error).key


def _pairing_abort_reason(error: Exception | None) -> str:
    """Return the setup_flow.abort reason slug for a terminal pairing failure."""
    if error is None:
        return "pairing_failed"
    key = error.alert_key if isinstance(error, SecurityActionError) else error_alert(error).key
    return _PAIRING_ABORT_REASONS.get(key, "pairing_failed")


if TYPE_CHECKING:
    from aiosendspin.models.core import ClientHelloPayload
    from aiosendspin.models.management import ManagementResultData, PairingMethodConfig
    from aiosendspin.models.player import SupportedAudioFormat
    from aiosendspin.noise.trust_store import ServerPairingRecord
    from aiosendspin.server.client import SendspinClient
    from music_assistant_models.media_items import MediaItemPalette
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.player_queues.state import PlayerQueueData
    from music_assistant.models.setup_flow import SetupSession
    from music_assistant.providers.chromecast.sendspin_bridge import SendspinBridgeManager
    from music_assistant.providers.hass import HomeAssistantProvider

    from .provider import PinPairingSession, SendspinProvider


class SendspinBasePlayer(Player):
    """
    Base class for Sendspin players in Music Assistant.

    Provides shared device-info, group membership, and event handling logic
    that is common to both audio and non-audio (visualizer) player types.
    """

    api: SendspinClient
    unsub_event_cb: Callable[[], None] | None
    unsub_group_event_cb: Callable[[], None] | None
    is_web_player: bool = False
    # transient alert produced by the most recent security action, surfaced on the next
    # config-entries render (the settings page re-renders after invoking an action)
    _pending_security_alert: AlertText | None = None

    def __init__(
        self,
        provider: SendspinProvider,
        player_id: str,
        initial_hello: ClientHelloPayload | None = None,
    ) -> None:
        """
        Initialize the base Sendspin player.

        :param provider: The Sendspin provider instance.
        :param player_id: The unique player identifier.
        :param initial_hello: Optional hello payload from the client.
        """
        super().__init__(provider, player_id)
        sendspin_client = provider.server_api.get_client(player_id)
        assert sendspin_client is not None
        self.api = sendspin_client
        self.unsub_event_cb = None
        self.unsub_group_event_cb = None
        self.logger = self.provider.logger.getChild(player_id)
        self._attr_can_group_with = {provider.instance_id}
        self._attr_power_control = PLAYER_CONTROL_NONE
        self._refresh_client_info(sendspin_client, hello_payload=initial_hello)
        self._subscribe_client_callbacks()

    def event_cb(self, client: SendspinClient, event: ClientEvent) -> None:
        """Event callback registered to the sendspin client."""
        match event:
            case ClientGroupChangedEvent(new_group=new_group):
                if self.unsub_group_event_cb is not None:
                    self.unsub_group_event_cb()
                self.unsub_group_event_cb = new_group.add_event_listener(self.group_event_cb)
                self._on_group_changed(new_group)
                self.update_state()

    def group_event_cb(self, group: SendspinGroup, event: GroupEvent) -> None:
        """Event callback registered to the sendspin group this player belongs to."""
        if self.synced_to is not None:
            # Only handle group events as the leader, except for:
            # - GroupMemberRemovedEvent: to handle being removed from a group
            # - GroupStateChangedEvent: to update playback state when leader stops/disconnects
            if not isinstance(event, (GroupMemberRemovedEvent, GroupStateChangedEvent)):
                return
        match event:
            case GroupStateChangedEvent(state=state):
                match state:
                    case PlaybackStateType.PLAYING:
                        self._attr_playback_state = PlaybackState.PLAYING
                    case PlaybackStateType.PAUSED:
                        self._attr_playback_state = PlaybackState.PAUSED
                    case PlaybackStateType.STOPPED:
                        self._attr_playback_state = PlaybackState.IDLE
                        self._attr_elapsed_time = 0
                        self._attr_elapsed_time_last_updated = time.time()
                        self._on_group_stopped()
                self.update_state()
            case GroupMemberAddedEvent(client_id=client_id):
                is_group_leader = (
                    bool(group.clients) and group.clients[0].client_id == self.player_id
                )
                if is_group_leader and (
                    not self._attr_group_members or self._attr_group_members[0] != self.player_id
                ):
                    self._attr_group_members = [self.player_id, *self._attr_group_members]
                if client_id not in self._attr_group_members:
                    self._attr_group_members.append(client_id)
                    self.update_state()
                self._schedule_membership_sync(group)
            case GroupMemberRemovedEvent(client_id=client_id):
                self.mass.create_task(self._handle_group_member_removed(group, client_id))
                self._schedule_membership_sync(group)
            case GroupDeletedEvent():
                pass

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self._unsubscribe_client_callbacks()

    @property
    def needs_setup(self) -> bool:
        """
        Whether the device is connected and encrypted but not yet usable for playback.

        An unpaired device that has not been paired or allowed unpaired playback still
        connects, but the server activates no roles for it. Reporting needs_setup keeps it
        out of the ready-to-play targets while its settings (and the pairing actions) stay
        reachable. Legacy unencrypted devices and the built-in web player can play as-is.
        """
        if self._is_bridge_or_web_player:
            return False

        # Deliberately no is_connected gate: between a (re)connect's hello and its first
        # client/state, security and active roles are already valid while is_connected is not.

        if self.api.connection_security is None:
            return False
        return not self.api.active_roles

    @property
    def setup_reason(self) -> str | None:
        """Return the reason this device needs setup (pairing), or None when it does not."""
        return "pairing_required" if self.needs_setup else None

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        entries = await super().get_config_entries()
        entries.extend(await self._get_security_config_entries())
        return entries

    async def handle_config_action(self, action: str) -> list[ConfigEntry]:
        """Run an unpair/management action, then re-render the entries."""
        if self.api.connection is not None:
            provider = cast("SendspinProvider", self.provider)
            alert = await self._handle_security_action(provider, action)
            if alert is not None:
                self._pending_security_alert = alert
        return await self.get_config_entries()

    async def run_setup_flow(self, session: SetupSession) -> None:
        """
        Drive pairing (or an unpaired-access grant) for this encrypted Sendspin device.

        An unpaired device picks between the offered pair methods and - when the device
        permits it - playback without pairing; re-running the flow on a paired device
        verifies its presence via a dynamic PIN. Pairing succeeds as a side effect of
        the provider pairing calls; the flow finishes with no persisted values. Bridge/
        web players and unencrypted (legacy) connections have nothing to pair.

        :param session: The setup flow session used to interact with the user.
        """
        security = self.api.connection_security
        if self._is_bridge_or_web_player or security is None:
            raise AbortFlow("nothing_to_configure")
        provider = cast("SendspinProvider", self.provider)
        record = await provider.server_api.pairing_store.record_by_client_id(self.player_id)
        if security.psk_category is PskCategory.LONG_TERM and record is not None:
            await self._run_verify_presence_flow(session, provider, record)
            await session.finish({})
            return
        trusted_unpaired = (
            await provider.server_api.pairing_store.trusted_unpaired(self.player_id) is not None
        )
        options = self._pairing_method_options(provider, offer_unpaired=not trusted_unpaired)
        if not options:
            raise AbortFlow("no_pair_methods")
        if len(options) == 1 and options[0] != PAIR_METHOD_UNPAIRED:
            method = options[0]
        else:
            # a lone unpaired-access option still renders the form: granting
            # unauthenticated playback needs an explicit user choice
            values = await session.form(
                [
                    ConfigEntry(
                        key=CONF_PAIRING_METHOD,
                        type=ConfigEntryType.STRING,
                        required=True,
                        default_value=options[0],
                        options=[ConfigValueOption(value=option) for option in options],
                    )
                ],
                step_id="select_method",
            )
            method = str(values[CONF_PAIRING_METHOD])
        if method == PAIR_METHOD_UNPAIRED:
            await provider.set_trusted_unpaired(self.player_id, enabled=True)
        elif method == PAIR_METHOD_TOKEN:
            await self._run_token_pairing_flow(session, provider)
        else:
            await self._run_pin_pairing_flow(
                session, provider, static=method == PAIR_METHOD_STATIC_PIN
            )
        await session.finish({})

    @property
    def _is_bridge_or_web_player(self) -> bool:
        """Whether this is a protocol bridge or built-in web/app player (skips pairing setup)."""
        return self.player_id.startswith(BRIDGE_PREFIX) or self.is_web_player

    def _subscribe_client_callbacks(self) -> None:
        """Subscribe to client and group events for the currently bound client."""
        self.api.disconnect_behaviour = DisconnectBehaviour.UNGROUP
        self.unsub_event_cb = self.api.add_event_listener(self.event_cb)
        self.unsub_group_event_cb = self.api.group.add_event_listener(self.group_event_cb)

    def _unsubscribe_client_callbacks(self) -> None:
        """Unsubscribe any active client and group listeners."""
        if self.unsub_event_cb is not None:
            with suppress(Exception):
                self.unsub_event_cb()
            self.unsub_event_cb = None
        if self.unsub_group_event_cb is not None:
            with suppress(Exception):
                self.unsub_group_event_cb()
            self.unsub_group_event_cb = None

    def _refresh_client_info(
        self,
        sendspin_client: SendspinClient,
        hello_payload: ClientHelloPayload | None = None,
    ) -> None:
        """
        Refresh shared player attributes from a Sendspin client hello/info payload.

        :param sendspin_client: The Sendspin client instance.
        :param hello_payload: Optional hello payload to use instead of client info.
        """
        client_info = hello_payload or sendspin_client.info
        preserved_identifiers = dict(self._attr_device_info.identifiers)
        self._attr_name = client_info.name
        if device_info := client_info.device_info:
            self._attr_device_info = DeviceInfo(
                model=device_info.product_name or "Unknown model",
                manufacturer=device_info.manufacturer or "Unknown Manufacturer",
                software_version=device_info.software_version,
            )
        else:
            self._attr_device_info = DeviceInfo()
        for id_type, id_value in preserved_identifiers.items():
            self._attr_device_info.add_identifier(id_type, id_value)
        # Add player_id as MAC identifier for protocol linking (if it's a valid MAC)
        # This enables linking with bridged players (e.g., AirPlay via Sendspin bridge)
        if IdentifierType.MAC_ADDRESS not in self._attr_device_info.identifiers:
            if _mac := mac_from_bridge_client_id(self.player_id):
                self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, _mac)
            elif is_valid_mac_address(self.player_id):
                self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, self.player_id)
        self._attr_available = True

    @property
    def _artwork_role(self) -> ArtworkGroupRole | None:
        """Get the ArtworkGroupRole for this player's group."""
        role = self.api.group.group_role("artwork")
        if isinstance(role, ArtworkGroupRole):
            return role
        return None

    @property
    def _metadata_role(self) -> MetadataGroupRole | None:
        """Get the MetadataGroupRole for this player's group."""
        role = self.api.group.group_role("metadata")
        if isinstance(role, MetadataGroupRole):
            return role
        return None

    @property
    def _color_role(self) -> ColorGroupRole | None:
        """Get the ColorGroupRole for this player's group."""
        role = self.api.group.group_role("color")
        if isinstance(role, ColorGroupRole):
            return role
        return None

    @property
    def _visualizer_role(self) -> VisualizerGroupRole | None:
        """Get the VisualizerGroupRole for this player's group."""
        role = self.api.group.group_role("visualizer")
        if isinstance(role, VisualizerGroupRole):
            return role
        return None

    @property
    def _controller_role(self) -> ControllerGroupRole | None:
        """Get the ControllerGroupRole for this player's group."""
        role = self.api.group.group_role("controller")
        if isinstance(role, ControllerGroupRole):
            return role
        return None

    @property
    def _player_role(self) -> PlayerRoleProtocol | None:
        """Get the player role for this client (not group role)."""
        for role in self.api.roles_by_family("player"):
            if isinstance(role, PlayerRoleProtocol):
                return role
        return None

    def _on_group_changed(self, new_group: SendspinGroup) -> None:
        """
        Handle group change logic.

        Syncs playback state from the new group and schedules membership sync.
        Override in subclasses for additional behaviour.

        :param new_group: The new group this player has been assigned to.
        """
        # Sync playback state from the new group
        match new_group.state:
            case PlaybackStateType.PLAYING:
                self._attr_playback_state = PlaybackState.PLAYING
            case PlaybackStateType.PAUSED:
                self._attr_playback_state = PlaybackState.PAUSED
            case PlaybackStateType.STOPPED:
                self._attr_playback_state = PlaybackState.IDLE
                self._attr_elapsed_time = 0
                self._attr_elapsed_time_last_updated = time.time()
        # Update in case this is a newly created group
        # GroupMemberAddedEvent or GroupMemberRemovedEvent will be fired before this
        # so group members are already up to date at this point
        self._schedule_membership_sync(new_group)

    def _on_group_stopped(self) -> None:
        """Handle the group transitioning to STOPPED state."""

    async def _sync_membership_from_group(self, group: SendspinGroup) -> None:
        """
        Sync MA player group membership from authoritative group state.

        :param group: The Sendspin group to sync from.
        """
        # Ignore stale events from a group we no longer belong to.
        if group is not self.api.group:
            return
        group_client_ids = [client.client_id for client in group.clients]
        is_leader = bool(group_client_ids) and group_client_ids[0] == self.player_id
        desired_group_members = group_client_ids if is_leader else []
        if self._attr_group_members != desired_group_members:
            self._attr_group_members = desired_group_members
            self.update_state()

    def _schedule_membership_sync(self, group: SendspinGroup) -> None:
        """Schedule a coalesced membership reconciliation task for this player."""
        self.mass.create_task(
            self._sync_membership_from_group(group),
            task_id=f"sendspin_membership_sync_{self.player_id}",
            abort_existing=True,
        )

    async def _handle_group_member_removed(self, group: SendspinGroup, client_id: str) -> None:
        """Handle a group member being removed asynchronously."""
        if client_id == self.player_id:
            was_leader = (
                bool(self._attr_group_members) and self._attr_group_members[0] == self.player_id
            )
            if was_leader and len(group.clients) > 0:
                # We were removed as the group leader but other clients remain.
                # Don't stop the group -- the PushStream keeps running for
                # remaining members (playback session was transferred in set_members).
                self.logger.debug(
                    "Player %s removed as group leader; group continues for remaining members",
                    self.player_id,
                )
            elif not was_leader:
                self.logger.debug(
                    "Player %s removed from group as non-leader; keeping old group playing",
                    self.player_id,
                )
            # Clear members for our detached/solo state.
            self._attr_group_members = []
            self.update_state()
        elif client_id in self._attr_group_members:
            # Someone else left our group
            self._attr_group_members.remove(client_id)
            self.update_state()

    async def _get_security_config_entries(self) -> list[ConfigEntry]:
        """Build the pairing/security section entries."""
        if self._is_bridge_or_web_player:
            return []
        provider = cast("SendspinProvider", self.provider)
        # surface (and clear) any alert produced by the most recent action
        alert: AlertText | None = self._pending_security_alert
        self._pending_security_alert = None
        status, actions = await self._security_state_entries(provider)
        entries = [status] if status is not None else []
        if alert is not None:
            entries.append(alert_entry(alert))
        return entries + actions

    async def _security_state_entries(
        self, provider: SendspinProvider
    ) -> tuple[ConfigEntry | None, list[ConfigEntry]]:
        """Return the current security-status entry and the available action entries."""
        if not self.api.is_connected:
            # The action dropped the connection (e.g. unpair); the client will reconnect
            return ConfigEntry(key="security_status_disconnected", type=ConfigEntryType.LABEL), []

        security = self.api.connection_security
        if security is None:
            return ConfigEntry(key="security_status_unencrypted", type=ConfigEntryType.ALERT), []

        record = await provider.server_api.pairing_store.record_by_client_id(self.player_id)

        # psk_category is fixed at handshake time: right after an unpair the live connection
        # still reports LONG_TERM while the record is already gone. The settings view refetches
        # in exactly that window and will not observe the later reconnect until reopened, so
        # require the record too or else render the unpaired end state immediately.
        if security.psk_category is PskCategory.LONG_TERM and record is not None:
            return (
                ConfigEntry(key="security_status_paired", type=ConfigEntryType.LABEL),
                await self._paired_entries(
                    provider, provider.pairing_config_snapshot(self.player_id)
                ),
            )

        trusted_unpaired = (
            await provider.server_api.pairing_store.trusted_unpaired(self.player_id) is not None
        )
        if not trusted_unpaired:
            return None, []
        return (
            ConfigEntry(key="security_status_unpaired", type=ConfigEntryType.ALERT),
            [action_entry(CONF_ACTION_REVOKE_UNPAIRED)],
        )

    async def _paired_entries(
        self,
        provider: SendspinProvider,
        pairing_config: ManagementResultData | None,
    ) -> list[ConfigEntry]:
        """Return the action entries for the paired view (management, unpair)."""
        entries: list[ConfigEntry] = []
        if provider.get_management_session(self.player_id) is not None:
            # Render from the snapshot the enter/patch actions keep fresh; only fetch if it is
            # unexpectedly empty (e.g. a session opened without a fetch).
            management_config = pairing_config
            if management_config is None:
                try:
                    management_config = await provider.management_get_pairing_config(self.player_id)
                except SecurityActionError as err:
                    provider.exit_management(self.player_id)
                    entries.append(alert_entry(error_alert(err)))
            if management_config is not None:
                entries.extend(self._management_section_entries(management_config))
                return entries
        entries.append(action_entry(CONF_ACTION_MANAGEMENT_ENTER))
        entries.append(action_entry(CONF_ACTION_UNPAIR, advanced=True))
        return entries

    @staticmethod
    def _management_section_entries(config: ManagementResultData) -> list[ConfigEntry]:
        """Return the entries for the open device-management section."""
        entries: list[ConfigEntry] = [
            ConfigEntry(key="management_status", type=ConfigEntryType.LABEL)
        ]
        if config.unpaired_access is not None:
            action = (
                CONF_ACTION_MANAGEMENT_UNPAIRED_DISABLE
                if config.unpaired_access.enabled
                else CONF_ACTION_MANAGEMENT_UNPAIRED_ENABLE
            )
            entries.append(action_entry(action))
        entries.extend(
            SendspinBasePlayer._management_pin_method_entries(
                config.static_pin,
                CONF_ACTION_MANAGEMENT_STATIC_PIN_ENABLE,
                CONF_ACTION_MANAGEMENT_STATIC_PIN_DISABLE,
            )
        )
        entries.extend(
            SendspinBasePlayer._management_pin_method_entries(
                config.dynamic_pin,
                CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_ENABLE,
                CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_DISABLE,
            )
        )
        entries.append(action_entry(CONF_ACTION_MANAGEMENT_EXIT))
        return entries

    @staticmethod
    def _management_pin_method_entries(
        method: PairingMethodConfig | None,
        enable_action: str,
        disable_action: str,
    ) -> list[ConfigEntry]:
        """Return the toggle for one PIN pairing method, empty if the device lacks it."""
        if method is None:
            return []
        action = disable_action if method.enabled else enable_action
        return [action_entry(action, advanced=True)]

    async def _handle_security_action(
        self, provider: SendspinProvider, action: str
    ) -> AlertText | None:
        """Execute an unpair/management action, returning a localized alert on failure."""
        try:
            if action == CONF_ACTION_UNPAIR:
                await provider.unpair_client(self.player_id)
            elif action == CONF_ACTION_REVOKE_UNPAIRED:
                await provider.set_trusted_unpaired(self.player_id, enabled=False)
            elif action == CONF_ACTION_MANAGEMENT_ENTER:
                provider.enter_management(self.player_id)
                try:
                    await provider.management_get_pairing_config(self.player_id)
                except SecurityActionError:
                    provider.exit_management(self.player_id)
                    raise
            elif action == CONF_ACTION_MANAGEMENT_EXIT:
                provider.exit_management(self.player_id)
            elif action in _MANAGEMENT_ACTIONS:
                await provider.management_set_pairing_config(
                    self.player_id, _MANAGEMENT_ACTIONS[action]
                )
        except (
            HandshakeAbortedError,
            PairingError,
            TimeoutError,
            OSError,
            SecurityActionError,
            ValueError,
        ) as err:
            return error_alert(err)
        return None

    def _pairing_method_options(
        self, provider: SendspinProvider, *, offer_unpaired: bool
    ) -> list[str]:
        """
        Return the pairing-method option values the device currently offers for setup.

        :param offer_unpaired: Include the unpaired-playback grant when the device permits it.
        """
        info = self.api.info_or_none
        pairing_config = provider.pairing_config_snapshot(self.player_id)
        pair_methods = effective_pair_methods(info, pairing_config)
        usable_pin_methods = {
            descriptor.method
            for descriptor in pair_methods
            if descriptor.method in (PairMethod.DYNAMIC_PIN, PairMethod.STATIC_PIN)
        }
        options: list[str] = []
        if usable_pin_methods:
            # Static PIN is only a distinct, meaningful choice when both PIN methods are usable;
            # opposite it the other option names the dynamic PIN rather than PINs in general.
            both_pin_methods = usable_pin_methods >= {PairMethod.DYNAMIC_PIN, PairMethod.STATIC_PIN}
            options.append(PAIR_METHOD_DYNAMIC_PIN if both_pin_methods else PAIR_METHOD_PIN)
            if both_pin_methods:
                options.append(PAIR_METHOD_STATIC_PIN)
        if any(descriptor.method is PairMethod.PAIRING_PSK for descriptor in pair_methods):
            options.append(PAIR_METHOD_TOKEN)
        if offer_unpaired and effective_unpaired_access(info, pairing_config):
            options.append(PAIR_METHOD_UNPAIRED)
        return options

    async def _pairing_succeeded(
        self, provider: SendspinProvider, pin_session: PinPairingSession
    ) -> bool:
        """Whether the attempt finished cleanly (or a long-term pairing record now exists)."""
        if pin_session.finished and pin_session.error is None:
            return True
        if pin_session.verify:
            return False
        # a confirm wait that outlived its deadline: the record is the proof of success
        return (
            await provider.server_api.pairing_store.record_by_client_id(self.player_id) is not None
        )

    async def _run_verify_presence_flow(
        self, session: SetupSession, provider: SendspinProvider, record: ServerPairingRecord
    ) -> None:
        """Confirm a paired device's physical presence via its dynamic PIN."""
        info = self.api.info_or_none
        pairing_config = provider.pairing_config_snapshot(self.player_id)
        offers_dynamic_pin = any(
            descriptor.method is PairMethod.DYNAMIC_PIN
            for descriptor in effective_pair_methods(info, pairing_config)
        )
        # presence proven by a dynamic-PIN pairing itself needs no re-verification
        if not offers_dynamic_pin or PairMethod.DYNAMIC_PIN in record.pair_methods:
            raise AbortFlow("already_paired")
        await self._run_pin_pairing_flow(session, provider, static=False, verify=True)

    async def _run_pin_pairing_flow(
        self,
        session: SetupSession,
        provider: SendspinProvider,
        *,
        static: bool,
        verify: bool = False,
    ) -> None:
        """
        Pair via PIN: the device wait, PIN entry and the retry-in-place loop.

        A retryable failure re-renders the PIN form (start_pin_pairing resumes the session in
        place); a terminal failure aborts the flow and an expired device wait propagates as a
        timed_out abort. On any non-success exit the finally tears down a device-side session
        still in flight - including when the flow is cancelled.

        :param static: Pair with the device's static PIN instead of a dynamic one.
        :param verify: Verify an already-paired device's presence (dynamic PIN only).
        """
        succeeded = False
        errors: dict[str, str] | None = None
        try:
            while True:
                try:
                    pin_session = await provider.start_pin_pairing(
                        self.player_id, static=static, verify=verify
                    )
                except SecurityActionError as err:
                    raise AbortFlow(_pairing_abort_reason(err)) from err
                await self._await_pin_request(session, pin_session)
                if not pin_session.awaiting_pin:
                    # The attempt ended before a PIN could be entered.
                    if await self._pairing_succeeded(provider, pin_session):
                        succeeded = True
                        return
                    if pin_session.can_retry:
                        errors = {"base": _pin_error_slug(pin_session.error)}
                        continue
                    raise AbortFlow(_pairing_abort_reason(pin_session.error))
                try:
                    pin_values = await session.form(
                        self._pin_form_entries(provider, pin_session),
                        step_id="verify_pin" if verify else "enter_pin",
                        errors=errors,
                        expires_in=PAIR_PIN_ENTRY_TIMEOUT,
                    )
                except StepExpiredError:
                    # The countdown mirrors the device's attempt timeout, which aborts the
                    # attempt around now; retry in place rather than dropping the flow.
                    errors = {"base": "pairing_error_timeout"}
                    continue
                errors = None
                try:
                    provider.submit_pin(self.player_id, str(pin_values[CONF_PAIRING_PIN]).strip())
                except SecurityActionError as err:
                    # the session ended underneath us (cancelled/timed out); start afresh
                    errors = {"base": err.alert_key}
                    continue
                task = pin_session.task
                if task is not None and not task.done():
                    # Join the pairing task: the step deadline must not cancel it.
                    with suppress(StepExpiredError):
                        await session.progress_until(
                            join_task(task),
                            step_id="confirming",
                            text="confirming",
                            expires_in=PAIR_CONFIRM_TIMEOUT,
                        )
                if await self._pairing_succeeded(provider, pin_session):
                    succeeded = True
                    return
                if pin_session.can_retry:
                    errors = {"base": _pin_error_slug(pin_session.error)}
                    continue
                raise AbortFlow(_pairing_abort_reason(pin_session.error))
        finally:
            if succeeded:
                provider.clear_pin_session(self.player_id)
            elif provider.get_pin_session(self.player_id) is not None:
                await provider.cancel_pin_pairing(self.player_id)

    async def _await_pin_request(
        self,
        session: SetupSession,
        pin_session: PinPairingSession,
    ) -> None:
        """Wait until the device asks for its PIN, showing what it is waiting on."""
        if pin_session.awaiting_first_message:
            await session.progress_until(
                pin_session.wait_first_message(),
                step_id="awaiting_device",
                text="awaiting_device",
                expires_in=SERVER_FIRST_MESSAGE_TIMEOUT_S,
            )
        if pin_session.awaiting_gesture:
            await session.progress_until(
                pin_session.wait_pin_request(),
                step_id="awaiting_gesture",
                text="awaiting_gesture",
                expires_in=SERVER_GESTURE_TIMEOUT_S,
            )

    def _pin_form_entries(
        self, provider: SendspinProvider, pin_session: PinPairingSession
    ) -> list[ConfigEntry]:
        """Return the PIN form fields, hinting how the operator gets the PIN and how long it is."""
        entries = self._secret_hint_entries(provider, pin_session.method)
        if pin_session.pin_length is not None:
            entries.append(
                ConfigEntry(
                    key="dynamic_pin_digits",
                    type=ConfigEntryType.LABEL,
                    translation_params=[str(pin_session.pin_length)],
                )
            )
        entries.append(
            ConfigEntry(key=CONF_PAIRING_PIN, type=ConfigEntryType.STRING, required=True)
        )
        return entries

    def _secret_hint_entries(
        self, provider: SendspinProvider, method: PairMethod
    ) -> list[ConfigEntry]:
        """Return LABEL entries for the device's hints on obtaining a method's pairing secret."""
        descriptor = pair_method_descriptor(
            effective_pair_methods(
                self.api.info_or_none, provider.pairing_config_snapshot(self.player_id)
            ),
            method,
        )
        if descriptor is None:
            return []
        hints = (
            descriptor.out_channels if method is PairMethod.DYNAMIC_PIN else descriptor.locations
        )
        labels = _SECRET_HINT_LABELS[method]
        return [
            ConfigEntry(key=key, type=ConfigEntryType.LABEL)
            for hint in hints or []
            if (key := labels.get(hint)) is not None
        ]

    async def _run_token_pairing_flow(
        self, session: SetupSession, provider: SendspinProvider
    ) -> None:
        """Pair via a pasted pairing token, re-rendering the form on a recoverable failure."""
        errors: dict[str, str] | None = None
        while True:
            token_values = await session.form(
                [
                    *self._secret_hint_entries(provider, PairMethod.PAIRING_PSK),
                    ConfigEntry(key=CONF_PAIRING_TOKEN, type=ConfigEntryType.STRING, required=True),
                ],
                step_id="enter_token",
                errors=errors,
            )
            try:
                await provider.pair_with_token(
                    self.player_id, str(token_values[CONF_PAIRING_TOKEN]).strip()
                )
            except (
                SecurityActionError,
                PairingError,
                HandshakeAbortedError,
                TimeoutError,
                OSError,
            ) as err:
                errors = {"base": error_alert(err).key}
                continue
            return


class SendspinPlayer(SendspinBasePlayer):
    """A sendspin audio player in Music Assistant."""

    _attr_type = PlayerType.PROTOCOL

    last_sent_artwork_url: str | None = None
    last_sent_artist_artwork_url: str | None = None
    _last_beat_queue_item_id: str | None = None
    _last_beat_anchor_us: int | None = None
    # Background poller that retries _send_beat_schedule when analysis is
    # not yet available. Cancelled on track change / stop / successful push.
    _beat_retry_task: asyncio.Task[None] | None = None
    # Queue item the current poller is targeting (so a track switch cancels it).
    _beat_retry_queue_item_id: str | None = None
    playback_session: SendspinPlaybackSession
    static_delay_default_ms: int = DEFAULT_SENDSPIN_STATIC_DELAY
    # HA media_player entity announcements are relayed to (ESPHome-backed devices)
    _hass_announce_entity_id: str | None = None

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    def __init__(
        self,
        provider: SendspinProvider,
        player_id: str,
        initial_hello: ClientHelloPayload | None = None,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id, initial_hello)
        hello_payload = initial_hello or self.api.info
        self.playback_session = SendspinPlaybackSession(self)
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.MULTI_DEVICE_DSP,
        }
        # Keep volume/mute features of the first registration as a workaround for Cast.
        if hello_payload.player_support:
            _supported_commands = hello_payload.player_support.supported_commands
            if PlayerCommand.VOLUME in _supported_commands:
                self._attr_supported_features.add(PlayerFeature.VOLUME_SET)
            if PlayerCommand.MUTE in _supported_commands:
                self._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)

    @property
    def supported_sample_rates(self) -> list[tuple[int, int]] | None:
        """Return supported (sample_rate, bit_depth) tuples derived from the player role."""
        # not cached: the player role / reported formats can change after the
        # client (re)registers, so we always re-resolve from the live role state
        if (player_role := self._player_role) is not None:
            formats = player_role.get_supported_formats() or []
            rates = sorted({(fmt.sample_rate, fmt.bit_depth) for fmt in formats})
            if rates:
                return rates
        return [(44100, 16)]

    def preserve_control_features_from(self, other: SendspinPlayer) -> None:
        """Keep the first registration's volume/mute features as a workaround for Cast."""
        for feature in (PlayerFeature.VOLUME_SET, PlayerFeature.VOLUME_MUTE):
            if feature in other.supported_features:
                self._attr_supported_features.add(feature)
            else:
                self._attr_supported_features.discard(feature)

    def set_hass_announce_entity(self, entity_id: str | None) -> None:
        """
        Set or clear the Home Assistant entity used to relay announcements.

        ESPHome devices support announcements natively (ducking any running
        playback), but that capability is only reachable through their Home
        Assistant media_player entity; the PLAY_ANNOUNCEMENT feature follows it.

        :param entity_id: The HA media_player entity id, or None to clear.
        """
        self._hass_announce_entity_id = entity_id
        if entity_id is not None:
            self._attr_supported_features.add(PlayerFeature.PLAY_ANNOUNCEMENT)
        else:
            self._attr_supported_features.discard(PlayerFeature.PLAY_ANNOUNCEMENT)

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""
        entity_id = self._hass_announce_entity_id
        hass = cast("HomeAssistantProvider | None", self.mass.get_provider("hass"))
        if entity_id is None or hass is None or not hass.available:
            raise PlayerCommandFailed(
                f"Announcement relay via Home Assistant is not available for {self.display_name}"
            )
        self.logger.info(
            "Playing announcement %s on %s (via Home Assistant)",
            announcement.uri,
            self.display_name,
        )
        if volume_level is not None:
            # the device's announcement pipeline plays at its own volume;
            # the announce volume config entries are hidden for this player
            self.logger.debug("Ignoring announcement volume level for player %s", self.display_name)
        await hass.play_announcement_on_entity(entity_id, announcement)
        self.logger.debug("Playing announcement on %s completed", self.display_name)

    def restore_bridge_identity(
        self, previous_device_info: DeviceInfo, previous_type: PlayerType
    ) -> None:
        """Keep bridge players exposed as protocol bridges after client attach updates."""
        if previous_type != PlayerType.PROTOCOL:
            return
        if not (
            IdentifierType.CAST_UUID in previous_device_info.identifiers
            or IdentifierType.AIRPLAY_ID in previous_device_info.identifiers
        ):
            return
        refreshed_identifiers = dict(self._attr_device_info.identifiers)
        self._attr_device_info = DeviceInfo(
            model=previous_device_info.model,
            manufacturer=previous_device_info.manufacturer,
            software_version=self._attr_device_info.software_version,
        )
        for id_type, id_value in refreshed_identifiers.items():
            self._attr_device_info.add_identifier(id_type, id_value)
        self.is_web_player = False
        self._attr_hidden_by_default = False
        self._attr_expose_to_ha_by_default = True
        self._attr_type = PlayerType.PROTOCOL

    def _subscribe_client_callbacks(self) -> None:
        """Subscribe to client and group events for the currently bound client."""
        super()._subscribe_client_callbacks()
        self.api.disconnect_behaviour = DisconnectBehaviour.STOP
        if controller_role := self._controller_role:
            controller_role.set_supported_commands(SUPPORTED_GROUP_COMMANDS)

    def _refresh_client_info(
        self,
        sendspin_client: SendspinClient,
        hello_payload: ClientHelloPayload | None = None,
    ) -> None:
        """Refresh player attributes from a Sendspin client hello/info payload."""
        super()._refresh_client_info(sendspin_client, hello_payload=hello_payload)
        client_info = hello_payload or sendspin_client.info
        if device_info := client_info.device_info:
            # determine if this is a web/app player based on product name or manufacturer
            # TODO: make this part of the spec and let clients explicitly report if they
            # are a web/app player instead of relying on heuristics
            self.is_web_player = (
                device_info.product_name
                in (
                    "Web Browser",
                    "Web Player",
                    "Mobile Application",
                    "PWA",
                )
                or device_info.manufacturer == "Music Assistant"
            )
        else:
            self.is_web_player = False
        if client_info.player_support:
            for role in sendspin_client.roles_by_family("player"):
                volume = role.get_player_volume()
                muted = role.get_player_muted()
                if volume is not None:
                    self._attr_volume_level = volume
                if muted is not None:
                    self._attr_volume_muted = muted
                if volume is not None or muted is not None:
                    break
        # virtual players are server-owned anchors that follow the same
        # hidden/standalone semantics as web players, without relying on the
        # device-info heuristics above
        is_standalone = self.is_web_player or cast(
            "SendspinProvider", self.provider
        ).is_virtual_player(self.player_id)
        self._attr_expose_to_ha_by_default = not is_standalone
        self._attr_hidden_by_default = is_standalone
        # register web/app player as native player type because it doesn't need to be linked
        # every web/app player is just a standalone player.
        self._attr_type = PlayerType.PLAYER if is_standalone else PlayerType.PROTOCOL

    def event_cb(self, client: SendspinClient, event: ClientEvent) -> None:
        """Event callback registered to the sendspin client."""
        match event:
            case VolumeChangedEvent(volume=volume, muted=muted):
                self._attr_volume_level = volume
                self._attr_volume_muted = muted
                self.update_state()
            case StaticDelayChangedEvent(static_delay_ms=delay_ms):
                self.logger.debug("Static delay changed to %d ms", delay_ms)
                current = self.config.get_value(
                    CONF_SENDSPIN_STATIC_DELAY, self.static_delay_default_ms
                )
                if current != delay_ms:
                    self.mass.config.set_raw_player_config_value(
                        self.player_id, CONF_SENDSPIN_STATIC_DELAY, delay_ms
                    )
            case _:
                super().event_cb(client, event)

    def _on_group_changed(self, new_group: SendspinGroup) -> None:
        """Handle group change with controller commands and playback session cancellation."""
        if controller_role := self._controller_role:
            controller_role.set_supported_commands(SUPPORTED_GROUP_COMMANDS)
        # Cancel active playback - push stream belongs to the old group
        self.mass.create_task(self.playback_session.cancel("group changed"))
        super()._on_group_changed(new_group)

    def _on_group_stopped(self) -> None:
        """Cancel playback session when group stops and we are the leader."""
        if self.synced_to is not None:
            return
        # Bind the cancel to the session task that is live right now: by the time
        # the deferred task below runs, play_media may already have started a fresh
        # session, which must not be torn down by this stale group-stopped event.
        stale_task = self.playback_session.playback_task
        if stale_task is None or stale_task.done():
            return
        self.mass.create_task(self._cancel_stale_playback_session(stale_task))

    async def _cancel_stale_playback_session(self, task: asyncio.Task[None]) -> None:
        """Cancel the playback session only if the given task is still the active one."""
        if self.playback_session.playback_task is not task:
            return
        await self.playback_session.cancel("group stopped")

    def group_event_cb(self, group: SendspinGroup, event: GroupEvent) -> None:
        """Event callback registered to the sendspin group this player belongs to."""
        # Leader only: a synced follower's self.state.current_media is a reference to
        # the leader's PlayerMedia object, so the refresh below would mutate the leader's
        # anchor from every follower. The metadata push (also leader-only) is what these
        # refreshes exist to serve, so followers have nothing to do here.
        is_resume = (
            isinstance(event, GroupStateChangedEvent)
            and event.state == PlaybackStateType.PLAYING
            and self._attr_playback_state == PlaybackState.PAUSED
            and self.synced_to is None
        )
        if is_resume:
            # _attr_elapsed_time_last_updated is only advanced by playback.py's commit
            # loop, which stops while paused - so it's still anchored to the moment
            # playback paused. Fast-forward it now, before update_state() below flips
            # playback_state to PLAYING, so corrected_elapsed_time doesn't extrapolate
            # across the paused span.
            self._attr_elapsed_time_last_updated = time.time()
        super().group_event_cb(group, event)
        if is_resume and self.state.current_media is not None:
            # send_current_media_metadata() (scheduled below) reads self.state.current_media,
            # which the queue controller rebuilds from its own cached elapsed-time anchor -
            # only refreshed via a 500ms-debounced callback, so it's still stale here even
            # after the fix above. Patch this update's snapshot directly so the imminent
            # metadata push doesn't race ahead of that debounce with a stale value.
            self.state.current_media.elapsed_time_last_updated = time.time()
        match event:
            case GroupStateChangedEvent(state=state) if self.synced_to is None and state in (
                PlaybackStateType.PLAYING,
                PlaybackStateType.PAUSED,
            ):
                # Push progress explicitly: current_media's identity is unchanged across
                # pause/resume so update_state() above won't debounce a metadata push
                # through the normal media-changed callback.
                self.mass.create_task(
                    self.send_current_media_metadata(),
                    task_id=f"sendspin_metadata_{self.player_id}",
                    abort_existing=True,
                )
            case ControllerEvent() as controller_event:
                if self.synced_to is None:
                    self.mass.create_task(self._handle_controller_event(controller_event))

    async def _handle_controller_event(self, event: ControllerEvent) -> None:
        """Handle a controller event from the ControllerGroupRole."""
        queue = self.mass.player_queues.get_active_queue(self.player_id)
        match event:
            case ControllerPlayEvent():
                await self.mass.players.cmd_play(self.player_id)
            case ControllerPauseEvent():
                await self.mass.players.cmd_pause(self.player_id)
            case ControllerStopEvent():
                await self.mass.players.cmd_stop(self.player_id)
            case ControllerNextEvent():
                await self.mass.players.cmd_next_track(self.player_id)
            case ControllerPreviousEvent():
                await self.mass.players.cmd_previous_track(self.player_id)
            case ControllerRepeatEvent(mode=mode) if queue:
                match mode:
                    case SendspinRepeatMode.OFF:
                        self.mass.player_queues.set_repeat(queue.queue_id, RepeatMode.OFF)
                    case SendspinRepeatMode.ONE:
                        self.mass.player_queues.set_repeat(queue.queue_id, RepeatMode.ONE)
                    case SendspinRepeatMode.ALL:
                        self.mass.player_queues.set_repeat(queue.queue_id, RepeatMode.ALL)
            case ControllerShuffleEvent(shuffle=shuffle) if queue:
                await self.mass.player_queues.set_shuffle(queue.queue_id, shuffle_enabled=shuffle)
            case ControllerSeekEvent(position_ms=position_ms) if (
                queue and queue.current_item and queue.current_item.duration
            ):
                # Clamp in case track duration changed after we advertised the seek range.
                duration_ms = int(queue.current_item.duration * 1000)
                await self.mass.player_queues.seek(
                    queue.queue_id, max(0, min(position_ms, duration_ms)) // 1000
                )
            case ControllerSeekRelativeEvent(offset_ms=offset_ms) if (
                queue and queue.current_item and queue.current_item.duration
            ):
                # Clamp current position + offset to the 0..duration range.
                target_ms = int(queue.corrected_elapsed_time * 1000) + offset_ms
                duration_ms = int(queue.current_item.duration * 1000)
                await self.mass.player_queues.seek(
                    queue.queue_id, max(0, min(target_ms, duration_ms)) // 1000
                )

    async def _sync_membership_from_group(self, group: SendspinGroup) -> None:
        """Sync MA/player + playback session membership from authoritative group state."""
        # Ignore stale events from a group we no longer belong to.
        if group is not self.api.group:
            return
        group_client_ids = [client.client_id for client in group.clients]
        is_leader = bool(group_client_ids) and group_client_ids[0] == self.player_id
        desired_group_members = group_client_ids if is_leader else []
        desired_session_members = group_client_ids[1:] if is_leader else []
        if self._attr_group_members != desired_group_members:
            self._attr_group_members = desired_group_members
            self.update_state()
        # Only use STOP when we actually lead other members.
        self.api.disconnect_behaviour = (
            DisconnectBehaviour.STOP
            if is_leader and len(desired_session_members) > 0
            else DisconnectBehaviour.UNGROUP
        )
        await self.playback_session.sync_members(set(desired_session_members))

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        roles = self.api.roles_by_family("player")
        for role in roles:
            role.set_player_volume(volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        roles = self.api.roles_by_family("player")
        for role in roles:
            role.set_player_mute(muted)
        # Native clients don't always emit a VolumeChangedEvent in response to a
        # mute command, so update our state directly to keep MA in sync.
        self._attr_volume_muted = muted
        self.update_state()

    async def stop(self) -> None:
        """Stop command."""
        self.logger.debug("Received STOP command on player %s", self.display_name)
        self.mark_stop_called()
        self._attr_current_media = None
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()
        # group.stop() snapshots the live position, which it can only do while the push
        # stream is up - cancelling first leaves it re-emitting a stale anchor. Teardown
        # goes in finally so a failing group stop can't strand the session, and nothing
        # may await between the two: the STOPPED event cancels this session inline, and a
        # suspension in between would let that cancel cut pipeline teardown short.
        try:
            await self.api.group.stop()
        finally:
            await self.playback_session.cancel("stop command")

    def _get_cast_bridge_manager(self) -> SendspinBridgeManager | None:
        """Return the Chromecast provider's Sendspin bridge manager, if loaded."""
        chromecast_provider = self.mass.get_provider("chromecast")
        if chromecast_provider is None:
            return None
        manager = getattr(chromecast_provider, "bridge_manager", None)
        if manager is None:
            return None
        return cast("SendspinBridgeManager", manager)

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self.logger.debug(
            "Received PLAY_MEDIA command on player %s with uri %s", self.display_name, media.uri
        )

        self._attr_current_media = media
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None

        await self.playback_session.cancel("new media requested")

        # Cast-only: reset future before start() to avoid racing _on_stream_start
        # and to cancel any stale pending future from a previous timed-out attempt.
        cast_app_ready: asyncio.Future[None] | None = None
        if (mgr := self._get_cast_bridge_manager()) and (
            bridge := mgr.get_bridge_by_client_id(self.player_id)
        ):
            cast_app_ready = bridge.reset_cast_app_ready()

        await self.playback_session.start(media)
        self.update_state()

        if cast_app_ready is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(cast_app_ready), timeout=30.0)
        except BaseException as exc:
            if not cast_app_ready.done():
                cast_app_ready.cancel()
            # Cancel the playback session so we don't keep streaming to a
            # device that never reported ready.
            with suppress(Exception):
                await self.playback_session.cancel("cast app readiness failed")
            if isinstance(exc, TimeoutError):
                raise PlayerCommandFailed(
                    f"Cast app on {self.display_name} did not report ready within 30s",
                    translation_key="cast_app_not_ready",
                    translation_owner=self.translation_owner,
                    translation_args=[self.display_name],
                ) from None
            raise

    async def on_config_updated(self) -> None:
        """Handle logic when the PlayerConfig is first loaded or updated."""
        await self._apply_preferred_format()
        await self._apply_static_delay()

    async def _apply_preferred_format(self) -> None:
        """Read config and set/clear the players preferred format."""
        player_role = self._player_role
        if player_role is None:
            return

        config_value = cast(
            "str",
            self.config.get_value(CONF_PREFERRED_SENDSPIN_FORMAT, SENDSPIN_FORMAT_AUTOMATIC),
        )
        if config_value == SENDSPIN_FORMAT_AUTOMATIC:
            # Automatic mode: clear override and let client decide.
            player_role.set_preferred_format(None, None)
            return

        parsed = option_value_to_format(config_value)
        if parsed is None:
            self.logger.warning(
                "Invalid audio format config value '%s' for player %s",
                config_value,
                self.display_name,
            )
            return

        codec, audio_format = parsed
        if not player_role.set_preferred_format(audio_format, codec):
            self.logger.warning(
                "Failed to set preferred audio format %s %s for player %s",
                codec.name,
                audio_format,
                self.display_name,
            )

    async def _apply_static_delay(self) -> None:
        """Read config and send set_static_delay command if supported."""
        player_role = self._player_role
        if player_role is None:
            return

        config_value = cast(
            "int",
            self.config.get_value(CONF_SENDSPIN_STATIC_DELAY, self.static_delay_default_ms),
        )
        player_role.set_static_delay(config_value)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        for player_id in player_ids_to_remove or []:
            member_player = self.mass.players.get_player(player_id, True)
            member_player = cast("SendspinPlayer", member_player)

            # Dynamic leader switch: transfer the active playback session to the
            # next remaining group member before removing ourselves from the group.
            # This keeps the PushStream alive for the remaining members.
            if (
                player_id == self.player_id
                and self.playback_session.playback_task is not None
                and not self.playback_session.playback_task.done()
            ):
                remaining = [c for c in self.api.group.clients if c.client_id != self.player_id]
                if remaining:
                    new_owner_id = remaining[0].client_id
                    new_owner = self.mass.players.get_player(new_owner_id)
                    if isinstance(new_owner, SendspinPlayer):
                        self.logger.info(
                            "Transferring playback session to %s for dynamic leader switch",
                            new_owner.display_name,
                        )
                        await self.playback_session.transfer_to(new_owner)
                        new_owner.playback_session = self.playback_session
                        self.playback_session = SendspinPlaybackSession(self)

            await self.api.group.remove_client(member_player.api)
        # Cast-only: reset futures before add so a fatal error on a Cast-bridged
        # member (e.g. AudioContext unsupported) raises PlayerCommandFailed.
        # Only track readiness while streaming, only then add_client launches the app.
        bridge_manager = self._get_cast_bridge_manager()
        pending_cast: list[tuple[SendspinPlayer, asyncio.Future[None]]] = []
        try:
            for player_id in player_ids_to_add or []:
                member_player = cast(
                    "SendspinPlayer", self.mass.players.get_player(player_id, True)
                )
                if (
                    self.api.group.has_active_stream
                    and bridge_manager
                    and (bridge := bridge_manager.get_bridge_by_client_id(player_id))
                ):
                    pending_cast.append((member_player, bridge.reset_cast_app_ready()))
                await self.api.group.add_client(member_player.api)

            if pending_cast:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(asyncio.shield(f) for _, f in pending_cast)),
                        timeout=30.0,
                    )
                except TimeoutError:
                    stuck = [m.display_name for m, f in pending_cast if not f.done()]
                    raise PlayerCommandFailed(
                        f"Cast app on {', '.join(stuck)} did not report ready within 30s",
                        translation_key="cast_app_members_not_ready",
                        translation_owner=self.translation_owner,
                        translation_args=[", ".join(stuck)],
                    ) from None
        except BaseException:
            # Roll back Cast members we just added so a failed group operation
            # doesn't leave dead members in the Sendspin group.
            for member, _ in pending_cast:
                with suppress(Exception):
                    await self.api.group.remove_client(member.api)
            raise
        finally:
            for _, f in pending_cast:
                if not f.done():
                    f.cancel()
        # self.group_members will be updated by the group event callback

    async def _send_album_artwork(self, current_media: PlayerMedia) -> str | None:
        """
        Send album artwork to the sendspin group.

        Args:
            current_media: The current player media.
        """
        # image_url is resolved per-source upstream (radio / Spotify Connect / queue items).
        artwork_url = current_media.image_url
        if artwork_url != self.last_sent_artwork_url:
            self.last_sent_artwork_url = artwork_url
            if artwork_url is not None:
                # Fetch from the resolved URL so the bytes match artwork_url, even when
                # radio now-playing art differs from the queue item's own image.
                try:
                    image_data = await self.mass.metadata.get_thumbnail(
                        artwork_url, provider="builtin"
                    )
                except MediaNotFoundError:
                    # artwork file was removed from disk; skip rather than crash the send
                    image_data = None
                if isinstance(image_data, bytes):
                    # decode through the guard so undecodable art (e.g. SVG) is skipped, not crashed
                    image = await self._decode_artwork(image_data)
                    if image is not None and (artwork_role := self._artwork_role) is not None:
                        await artwork_role.set_album_artwork(image)
            elif (artwork_role := self._artwork_role) is not None:
                await artwork_role.set_album_artwork(None)

        return artwork_url

    async def _send_artist_artwork(self, current_item: QueueItem) -> None:
        """Send artist artwork to the sendspin group."""
        artist_artwork_url: str | None = None

        if current_item.media_item is not None and is_track(current_item.media_item):
            artists = current_item.media_item.artists
            if artists:
                primary_artist = artists[0]
                # Prefer a full library artist (has reliable up-to-date artwork) over
                # the ItemMapping in the queue item, which often has image=None.
                result = await self.mass.music.get_library_item_by_prov_id(
                    MediaType.ARTIST, primary_artist.item_id, primary_artist.provider
                )
                artist_item = result if isinstance(result, Artist) else None
                image = artist_item.image if artist_item is not None else primary_artist.image
                if image is not None:
                    artist_artwork_url = self.mass.metadata.get_image_url(image)

        if artist_artwork_url != self.last_sent_artist_artwork_url:
            self.last_sent_artist_artwork_url = artist_artwork_url
            if artist_artwork_url is not None:
                # Fetch bytes from the already-resolved URL to avoid the secondary
                # provider lookup that get_image_data_for_item triggers for ItemMappings.
                try:
                    artist_image_data = await self.mass.metadata.get_thumbnail(
                        artist_artwork_url, provider="builtin"
                    )
                except MediaNotFoundError:
                    # artwork file was removed from disk; skip rather than crash the send
                    artist_image_data = None
                if isinstance(artist_image_data, bytes):
                    artist_image = await self._decode_artwork(artist_image_data)
                    if (
                        artist_image is not None
                        and (artwork_role := self._artwork_role) is not None
                    ):
                        await artwork_role.set_artist_artwork(artist_image)
            elif (artwork_role := self._artwork_role) is not None:
                await artwork_role.set_artist_artwork(None)

    async def _decode_artwork(self, image_data: bytes) -> Image.Image | None:
        """
        Decode artwork bytes into a Pillow image, returning None if undecodable.

        :param image_data: Raw image bytes to decode.
        """

        def _open() -> Image.Image:
            img = Image.open(BytesIO(image_data))
            img.load()
            return img

        try:
            return await asyncio.to_thread(_open)
        except OSError as err:
            self.logger.debug("Skipping undecodable artwork: %s", err)
            return None

    def _on_player_media_updated(self) -> None:
        """Handle callback when the current media of the player is updated."""
        if self.synced_to is not None:
            # Only leader sends metadata
            return
        self.mass.create_task(
            self.send_current_media_metadata(),
            task_id=f"sendspin_metadata_{self.player_id}",
            abort_existing=True,
        )

    async def _clear_current_media_metadata(self) -> None:
        """Clear all metadata and artwork from the sendspin group."""
        # Stop any in-flight beat-analysis polling task
        self._cancel_beat_retry()
        if (metadata_role := self._metadata_role) is not None:
            metadata_role.set_metadata(Metadata())
        if (visualizer_role := self._visualizer_role) is not None:
            visualizer_role.clear_beat_schedule()
            # Reset to PENDING so beats are re-deferred until the next track's analysis lands.
            visualizer_role.set_beat_availability(BeatAvailability.PENDING)
        if (artwork_role := self._artwork_role) is not None:
            await artwork_role.set_album_artwork(None)
            await artwork_role.set_artist_artwork(None)
        if (color_role := self._color_role) is not None:
            color_role.clear()
        if (controller_role := self._controller_role) is not None:
            controller_role.set_seek_max_ms(None)
        self.last_sent_artwork_url = None
        self.last_sent_artist_artwork_url = None
        self._last_beat_queue_item_id = None
        self._last_beat_anchor_us = None

    def _publish_repeat_shuffle(self, repeat: SendspinRepeatMode, *, shuffle: bool) -> None:
        """
        Push repeat/shuffle to controller state for current-spec clients.

        Clients implementing the older spec version still read the copy mirrored
        onto metadata state, for now.
        """
        if (controller_role := self._controller_role) is not None:
            controller_role.set_repeat(repeat)
            controller_role.set_shuffle(shuffle=shuffle)

    def _compute_track_progress_ms(self, current_media: PlayerMedia, *, is_playing: bool) -> int:
        """
        Resolve current track position in ms from queue/media elapsed time.

        Prefer queue/media elapsed as source of truth. Only interpolate while
        actively playing; for paused/idle states keep the last fixed position.
        """
        elapsed_time: float | None = (
            float(current_media.elapsed_time) if current_media.elapsed_time is not None else None
        )
        if is_playing and current_media.corrected_elapsed_time is not None:
            elapsed_time = current_media.corrected_elapsed_time
        if elapsed_time is None:
            elapsed_time = self.corrected_elapsed_time if is_playing else self.elapsed_time
        return max(0, int(elapsed_time * 1000)) if elapsed_time is not None else 0

    async def _refresh_beat_schedule(self) -> None:
        """
        Re-attempt beat hydration only, without re-pushing metadata/artwork.

        Lighter than `send_current_media_metadata`: the retry poller uses this so
        late-arriving analysis lands without re-running the full pipeline.
        """
        current_media = self.state.current_media
        if current_media is None:
            return
        queue_item: QueueItem | None = None
        queue: PlayerQueue | None = None
        if current_media.source_id and current_media.queue_item_id:
            queue = self.mass.player_queues.get(current_media.source_id)
            queue_item = self.mass.player_queues.get_item(
                current_media.source_id, current_media.queue_item_id
            )
        is_playing = self.state.playback_state == PlaybackState.PLAYING
        track_progress = self._compute_track_progress_ms(current_media, is_playing=is_playing)
        await self._send_beat_schedule(queue, queue_item, track_progress, is_playing)

    async def send_current_media_metadata(self) -> None:
        """Send the current media metadata to the sendspin group."""
        if not self.available:
            return
        current_media = self.state.current_media
        if current_media is None:
            await self._clear_current_media_metadata()
            return
        # check if we are playing a MA queue item
        queue_item: QueueItem | None = None
        queue: PlayerQueue | None = None
        if current_media.source_id and current_media.queue_item_id:
            queue = self.mass.player_queues.get(current_media.source_id)
            queue_item = self.mass.player_queues.get_item(
                current_media.source_id, current_media.queue_item_id
            )

        # Runs even without a queue item so radio / Spotify Connect streams still get art.
        await self._send_album_artwork(current_media)
        if queue_item:
            await self._send_artist_artwork(queue_item)

        track_number: int | None = None
        year: int | None = None
        album_artist: str | None = None
        if queue_item and queue_item.media_item and is_track(queue_item.media_item):
            track = queue_item.media_item
            track_number = track.track_number or None
            album_mapping = track.album
            if album_mapping is not None:
                year = album_mapping.year
                if not isinstance(album_mapping, Album):
                    # Cheap DB-only lookup, no external API call; None if not in library
                    result = await self.mass.music.get_library_item_by_prov_id(
                        MediaType.ALBUM, album_mapping.item_id, album_mapping.provider
                    )
                    full_album: Album | None = result if isinstance(result, Album) else None
                else:
                    full_album = album_mapping
                if full_album and full_album.artists:
                    album_artist = full_album.artist_str

        track_duration = current_media.duration or 0
        if controller_role := self._controller_role:
            controller_role.set_seek_max_ms(int(track_duration * 1000) if track_duration else None)
        repeat = SendspinRepeatMode.OFF
        if queue and queue.repeat_mode == RepeatMode.ALL:
            repeat = SendspinRepeatMode.ALL
        elif queue and queue.repeat_mode == RepeatMode.ONE:
            repeat = SendspinRepeatMode.ONE

        shuffle = queue.shuffle_enabled if queue else False
        is_playing = self.state.playback_state == PlaybackState.PLAYING
        track_progress = self._compute_track_progress_ms(current_media, is_playing=is_playing)

        metadata = Metadata(
            title=current_media.title,
            artist=current_media.artist,
            album_artist=album_artist,
            album=current_media.album,
            artwork_url=current_media.image_url,
            year=year,
            track=track_number,
            track_duration=track_duration * 1000 if track_duration is not None else None,
            track_progress=track_progress,
            playback_speed=1000 if is_playing else 0,
            repeat=repeat,
            shuffle=shuffle,
        )

        # Send metadata to the group
        if (metadata_role := self._metadata_role) is not None:
            metadata_role.set_metadata(metadata)

        self._publish_repeat_shuffle(repeat, shuffle=shuffle)

        # Send color palette derived from the cover art (already computed by
        # the players controller with the Sendspin defined minimum contrast values).
        if (color_role := self._color_role) is not None:
            self._send_color_palette(color_role, current_media.palette)

        await self._send_beat_schedule(queue, queue_item, track_progress, is_playing)

    def _send_color_palette(
        self, color_role: ColorGroupRole, palette: MediaItemPalette | None
    ) -> None:
        """Push the palette already attached to current_media to the sendspin group."""
        if palette is None:
            color_role.clear()
            return
        color_role.set_color(
            Color(
                background_dark=palette.background_dark,
                background_light=palette.background_light,
                primary=palette.primary,
                accent=palette.accent,
                on_dark=palette.on_dark,
                on_light=palette.on_light,
            )
        )

    @staticmethod
    def _flow_track_offset_us(pq_data: PlayerQueueData | None, queue_item: QueueItem) -> int | None:
        """
        Return the current track's flow-stream start offset (minus file seek), in µs.

        Sums the streamed duration of every track committed before the current
        one in the queue-flow stream, so beats anchor to this track's start
        rather than the first track's. Returns None when the flow log hasn't
        recorded this track yet, so the caller falls back to the progress anchor.

        A track whose intro was crossfade-trimmed loses its raw seek position
        (only the elapsed-inflated value survives), so its anchor can be off by
        up to the crossfade duration. Exact handling needs a raw-seek field on
        the flow log entry.
        """
        if pq_data is None or queue_item.streamdetails is None:
            return None
        log = pq_data.flow_mode_stream_log
        if not log or log[-1].queue_item_id != queue_item.queue_item_id:
            return None
        track_flow_start_s = sum(entry.seconds_streamed or 0.0 for entry in log[:-1])
        file_seek_s = float(queue_item.streamdetails.seek_position or 0)
        return int((track_flow_start_s - file_seek_s) * 1_000_000)

    async def _send_beat_schedule(
        self,
        queue: PlayerQueue | None,
        queue_item: QueueItem | None,
        track_progress_ms: int,
        is_playing: bool,
    ) -> None:
        """Hydrate per-track beat timings from audio analysis and push to visualizer."""
        visualizer_role = self._visualizer_role
        if visualizer_role is None:
            return
        if not is_playing or queue_item is None or queue_item.streamdetails is None:
            visualizer_role.clear_beat_schedule()
            self._last_beat_queue_item_id = None
            self._last_beat_anchor_us = None
            self._cancel_beat_retry()
            return
        # smart_fades is the only AA provider that emits beats. Without it, no
        # beats will ever arrive for this source.
        if not any(
            p.available and p.domain == "smart_fades"
            for p in self.mass.get_providers(ProviderType.AUDIO_ANALYSIS)
        ):
            visualizer_role.clear_beat_schedule()
            visualizer_role.set_beat_availability(BeatAvailability.UNAVAILABLE)
            self._last_beat_queue_item_id = None
            self._last_beat_anchor_us = None
            self._cancel_beat_retry()
            return
        provider = cast("SendspinProvider", self.provider)
        now_us = provider.server_api.clock.now_us()
        # Anchor beats to the current track's spot in the flow stream's audio
        # timeline. Falls back to the progress-derived anchor until the first
        # chunk commits or while the flow log hasn't recorded this track yet.
        anchor_us: int | None = None
        pq_data = self.mass.player_queues.queue_data_or_none(queue.queue_id) if queue else None
        offset_us = self._flow_track_offset_us(pq_data, queue_item)
        if offset_us is not None:
            anchor_us = self.playback_session.flow_track_anchor_us(offset_us)
        if anchor_us is None:
            anchor_us = now_us - track_progress_ms * 1000
        # Re-push only on track change or seek (anchor jumps beyond natural drift).
        if (
            queue_item.queue_item_id == self._last_beat_queue_item_id
            and self._last_beat_anchor_us is not None
            and abs(anchor_us - self._last_beat_anchor_us) < 500_000
        ):
            return
        sd = queue_item.streamdetails
        analysis = await self.mass.streams.audio_analysis.get_audio_analysis(
            sd.item_id,
            sd.provider,
            media_type=sd.media_type,
            priority=(SMART_FADES_ANALYSIS_DOMAIN,),
        )
        if analysis is None or analysis.beats is None or len(analysis.beats) == 0:
            visualizer_role.clear_beat_schedule()
            # Analysis may still be running (offline NN takes ~5-10 s). Kick a
            # poller so beats land once available, without waiting for the
            # next player media update.
            # This could be solved more elegantly with an event instead of this
            # poller, but that means changes outside the sendspin provider, which
            # is riskier.
            self._schedule_beat_retry(queue_item.queue_item_id)
            return  # don't poison the cache; retry asynchronously
        # Analysis is in — no more retries needed.
        self._cancel_beat_retry()
        downbeats = (
            {float(d) for d in analysis.downbeats} if analysis.downbeats is not None else set()
        )
        beats: list[BeatTiming] = []
        for b in analysis.beats:
            beat_us = int(anchor_us + float(b) * 1_000_000)
            if beat_us < now_us:
                continue
            beats.append(BeatTiming(timestamp_us=beat_us, is_downbeat=float(b) in downbeats))
        visualizer_role.clear_beat_schedule()
        if beats:
            visualizer_role.append_beat_schedule(beats)
        self._last_beat_queue_item_id = queue_item.queue_item_id
        self._last_beat_anchor_us = anchor_us

    # Initial backoff for the beat-analysis poller. The neural beat tracker
    # in smart_fades takes ~5-10 s; retry every 3 s until it lands. Capped so
    # tracks that won't ever have analysis don't poll forever.
    _BEAT_RETRY_INTERVAL_S: ClassVar[float] = 3.0
    _BEAT_RETRY_MAX_ATTEMPTS: ClassVar[int] = 30  # ~90 s of wait

    def _schedule_beat_retry(self, queue_item_id: str) -> None:
        """
        Start the background poller for late-arriving beat analysis.

        If a poller is already running for this queue item, no-op.
        """
        if (
            self._beat_retry_task is not None
            and not self._beat_retry_task.done()
            and self._beat_retry_queue_item_id == queue_item_id
        ):
            return
        self._cancel_beat_retry()
        self._beat_retry_queue_item_id = queue_item_id
        self._beat_retry_task = asyncio.create_task(self._beat_retry_loop(queue_item_id))

    def _cancel_beat_retry(self) -> None:
        """Cancel the beat-analysis poller if running."""
        if self._beat_retry_task is not None and not self._beat_retry_task.done():
            self._beat_retry_task.cancel()
        self._beat_retry_task = None
        self._beat_retry_queue_item_id = None

    async def _beat_retry_loop(self, queue_item_id: str) -> None:
        """Retry beat-schedule hydration until beats land or the track changes."""
        try:
            for _ in range(self._BEAT_RETRY_MAX_ATTEMPTS):
                await asyncio.sleep(self._BEAT_RETRY_INTERVAL_S)
                if self._beat_retry_queue_item_id != queue_item_id:
                    return  # superseded by another poller
                current = self.current_media
                if current is None or current.queue_item_id != queue_item_id:
                    return  # track changed
                if self._last_beat_queue_item_id == queue_item_id:
                    return  # beats were pushed via some other path
                # Re-attempt beat hydration only; _send_beat_schedule will push
                # beats now or schedule another retry.
                try:
                    await self._refresh_beat_schedule()
                except Exception:
                    self.logger.exception("Beat-schedule retry failed for %s", queue_item_id)
                    continue
                if self._last_beat_queue_item_id == queue_item_id:
                    return
            # Cap exhausted without beats landing. Flip the visualizer to
            # UNAVAILABLE so clients (Hue, web) stop waiting and the peak
            # walker / static palette path takes over for this track.
            if (
                self._beat_retry_queue_item_id == queue_item_id
                and (current := self.current_media) is not None
                and current.queue_item_id == queue_item_id
                and (visualizer_role := self._visualizer_role) is not None
            ):
                visualizer_role.set_beat_availability(BeatAvailability.UNAVAILABLE)
        except asyncio.CancelledError:
            return
        finally:
            if self._beat_retry_queue_item_id == queue_item_id:
                self._beat_retry_queue_item_id = None

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        entries: list[ConfigEntry] = []
        # Show alert if this Cast device is known to lack AudioContext support
        if self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_CAST_AUDIO_UNSUPPORTED
        ):
            entries.append(
                ConfigEntry(
                    key="cast_audio_unsupported",
                    type=ConfigEntryType.ALERT,
                    required=False,
                )
            )
        entries.extend(await super().get_config_entries())
        # Build dynamic format options from player's supported formats
        player_role = self._player_role
        if player_role is not None:
            supported_formats = player_role.get_supported_formats()
            if supported_formats:
                format_options = [
                    ConfigValueOption(SENDSPIN_FORMAT_AUTOMATIC),
                ]
                for fmt in supported_formats:
                    format_options.append(
                        ConfigValueOption(
                            format_to_option_value(fmt), title=format_to_display_string(fmt)
                        )
                    )
                entries.append(
                    ConfigEntry(
                        key=CONF_PREFERRED_SENDSPIN_FORMAT,
                        type=ConfigEntryType.STRING,
                        category="protocol_generic",
                        default_value=SENDSPIN_FORMAT_AUTOMATIC,
                        options=format_options,
                        advanced=True,
                    )
                )

        if (
            player_role is not None
            and PlayerCommand.SET_STATIC_DELAY in player_role.state_supported_commands
        ):
            entries.append(
                ConfigEntry(
                    key=CONF_SENDSPIN_STATIC_DELAY,
                    type=ConfigEntryType.INTEGER,
                    required=False,
                    default_value=self.static_delay_default_ms,
                    range=(0, 5000),
                    immediate_apply=True,
                    # Not a advanced option since this will only show up for players where it is likely
                    # necessary to adjust the delay.
                    advanced=False,
                )
            )

        if self._hass_announce_entity_id is not None:
            # announcements are relayed to the device via Home Assistant,
            # which has no volume control for announcements
            entries.extend(HIDDEN_ANNOUNCE_VOLUME_CONFIG_ENTRIES)

        return entries

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await self.playback_session.close()
        await super().on_unload()


class SendspinVisualizerPlayer(SendspinBasePlayer):
    """A non-audio Sendspin player for visualizer/lighting devices."""

    _attr_type = PlayerType.VISUALIZER
    _attr_hidden_by_default = True
    _attr_expose_to_ha_by_default = False

    def __init__(
        self,
        provider: SendspinProvider,
        player_id: str,
        initial_hello: ClientHelloPayload | None = None,
    ) -> None:
        """
        Initialize the visualizer player.

        :param provider: The Sendspin provider instance.
        :param player_id: The unique player identifier.
        :param initial_hello: Optional hello payload from the client.
        """
        super().__init__(provider, player_id, initial_hello)
        self._attr_supported_features = {PlayerFeature.SET_MEMBERS}

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command for the visualizer player."""
        for player_id in player_ids_to_remove or []:
            member = self.mass.players.get_player(player_id, True)
            if isinstance(member, SendspinBasePlayer):
                await self.api.group.remove_client(member.api)
        for player_id in player_ids_to_add or []:
            member = self.mass.players.get_player(player_id, True)
            if isinstance(member, SendspinBasePlayer):
                await self.api.group.add_client(member.api)
