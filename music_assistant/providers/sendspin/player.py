"""Sendspin Player implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from io import BytesIO
from typing import TYPE_CHECKING, ClassVar, cast

from aiosendspin.models import AudioCodec, MediaCommand
from aiosendspin.models.types import PlaybackStateType, PlayerCommand
from aiosendspin.models.types import RepeatMode as SendspinRepeatMode
from aiosendspin.models.visualizer import BeatAvailability, BeatTiming
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

from music_assistant.controllers.streams.audio_analysis import SMART_FADES_ANALYSIS_DOMAIN
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import Player, PlayerMedia

from .constants import (
    CONF_CAST_AUDIO_UNSUPPORTED,
    CONF_SENDSPIN_STATIC_DELAY,
    DEFAULT_SENDSPIN_STATIC_DELAY,
)
from .helpers import mac_from_bridge_client_id
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


if TYPE_CHECKING:
    from aiosendspin.models.core import ClientHelloPayload
    from aiosendspin.models.player import SupportedAudioFormat
    from aiosendspin.server.client import SendspinClient
    from music_assistant_models.config_entries import ConfigValueType
    from music_assistant_models.media_items import MediaItemPalette
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.player_queues.state import PlayerQueueData
    from music_assistant.providers.chromecast.sendspin_bridge import SendspinBridgeManager

    from .provider import SendspinProvider


class SendspinBasePlayer(Player):
    """
    Base class for Sendspin players in Music Assistant.

    Provides shared device-info, group membership, and event handling logic
    that is common to both audio and non-audio (visualizer) player types.
    """

    api: SendspinClient
    unsub_event_cb: Callable[[], None] | None
    unsub_group_event_cb: Callable[[], None] | None

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
    is_web_player: bool = False
    static_delay_default_ms: int = DEFAULT_SENDSPIN_STATIC_DELAY

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
        if self.synced_to is None:
            self.mass.create_task(self.playback_session.cancel("group stopped"))

    def group_event_cb(self, group: SendspinGroup, event: GroupEvent) -> None:
        """Event callback registered to the sendspin group this player belongs to."""
        if (
            isinstance(event, GroupStateChangedEvent)
            and event.state == PlaybackStateType.PLAYING
            and self._attr_playback_state == PlaybackState.PAUSED
            and self._attr_current_media is not None
        ):
            # Resuming the same track leaves current_media's identity unchanged, so
            # the debounced media-updated callback won't fire. Refresh the anchor
            # before update_state() (called by super()) runs, so corrected_elapsed_time
            # extrapolates from the resume moment instead of through the paused span.
            self._attr_current_media.elapsed_time_last_updated = time.time()
        super().group_event_cb(group, event)
        match event:
            case GroupStateChangedEvent(state=state) if self.synced_to is None and state in (
                PlaybackStateType.PLAYING,
                PlaybackStateType.PAUSED,
            ):
                # Sendspin spec requires progress on every playback-state transition
                # (play/pause/resume); current_media identity is unchanged across
                # pause/resume so the media-updated callback alone won't cover it.
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
        await self.playback_session.cancel("stop command")
        await self.api.group.stop()

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

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
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
