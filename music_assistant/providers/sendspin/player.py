"""Sendspin Player implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from io import BytesIO
from typing import TYPE_CHECKING, cast

from aiosendspin.models import AudioCodec, MediaCommand
from aiosendspin.models.types import PlaybackStateType, PlayerCommand
from aiosendspin.models.types import RepeatMode as SendspinRepeatMode
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
    ControllerEvent,
    ControllerGroupRole,
    ControllerNextEvent,
    ControllerPauseEvent,
    ControllerPlayEvent,
    ControllerPreviousEvent,
    ControllerRepeatEvent,
    ControllerShuffleEvent,
    ControllerStopEvent,
    MetadataGroupRole,
)
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
    RepeatMode,
)
from music_assistant_models.media_items import Album, Artist, is_track
from music_assistant_models.player import DeviceInfo
from PIL import Image

from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import Player, PlayerMedia

from .constants import (
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
]

# Config constants for Sendspin audio format
CONF_PREFERRED_SENDSPIN_FORMAT = "preferred_sendspin_format"
SENDSPIN_FORMAT_AUTOMATIC = "automatic"


def format_to_option_value(fmt: SupportedAudioFormat) -> str:
    """Convert SupportedAudioFormat to "codec:sample_rate:bit_depth:channels"."""
    return f"{fmt.codec.value}:{fmt.sample_rate}:{fmt.bit_depth}:{fmt.channels}"


def option_value_to_format(value: str) -> tuple[AudioCodec, SendspinAudioFormat] | None:
    """Parse option value back to (AudioCodec, SendspinAudioFormat).

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
    except (ValueError, KeyError):
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
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

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

    def event_cb(self, client: SendspinClient, event: ClientEvent) -> None:
        """Event callback registered to the sendspin client."""
        match event:
            case ClientGroupChangedEvent(new_group=new_group):
                if self.unsub_group_event_cb is not None:
                    self.unsub_group_event_cb()
                self.unsub_group_event_cb = new_group.add_event_listener(self.group_event_cb)
                self._on_group_changed(new_group)
                self.update_state()

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

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        self._unsubscribe_client_callbacks()


class SendspinPlayer(SendspinBasePlayer):
    """A sendspin audio player in Music Assistant."""

    _attr_type = PlayerType.PROTOCOL

    last_sent_artwork_url: str | None = None
    last_sent_artist_artwork_url: str | None = None
    playback_session: SendspinPlaybackSession
    is_web_player: bool = False

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
        self._attr_expose_to_ha_by_default = not self.is_web_player
        self._attr_hidden_by_default = self.is_web_player
        # register web/app player as native player type because it doesn't need to be linked
        # every web/app player is just a standalone player.
        self._attr_type = PlayerType.PLAYER if self.is_web_player else PlayerType.PROTOCOL

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
                    CONF_SENDSPIN_STATIC_DELAY, DEFAULT_SENDSPIN_STATIC_DELAY
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
        super().group_event_cb(group, event)
        match event:
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

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        self.logger.debug(
            "Received PLAY_MEDIA command on player %s with uri %s", self.display_name, media.uri
        )

        # Set current media; elapsed_time will be updated once audio actually commits.
        self._attr_current_media = media
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        # playback_state will be set by the group state change event

        # Stop previous stream in case we were already playing something.
        # Do not call group.stop() here to avoid STOPPED-event races with next-track transitions.
        await self.playback_session.cancel("new media requested")
        await self.playback_session.start(media)
        self.update_state()

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
            self.config.get_value(CONF_SENDSPIN_STATIC_DELAY, DEFAULT_SENDSPIN_STATIC_DELAY),
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
        for player_id in player_ids_to_add or []:
            member_player = self.mass.players.get_player(player_id, True)
            member_player = cast("SendspinPlayer", member_player)
            await self.api.group.add_client(member_player.api)
        # self.group_members will be updated by the group event callback

    async def _send_album_artwork(self, current_item: QueueItem) -> str | None:
        """
        Send album artwork to the sendspin group.

        Args:
            current_item: The current queue item.
        """
        artwork_url = None
        if current_item.image is not None:
            artwork_url = self.mass.metadata.get_image_url(current_item.image)

        if artwork_url != self.last_sent_artwork_url:
            # Image changed, resend the artwork
            self.last_sent_artwork_url = artwork_url
            if artwork_url is not None and current_item.media_item is not None:
                image_data = await self.mass.metadata.get_image_data_for_item(
                    current_item.media_item
                )
                if image_data is not None:
                    image = await asyncio.to_thread(Image.open, BytesIO(image_data))
                    if (artwork_role := self._artwork_role) is not None:
                        await artwork_role.set_album_artwork(image)
            # Clear artwork if none available
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
                artist_image_data = await self.mass.metadata.get_thumbnail(
                    artist_artwork_url, provider="builtin"
                )
                if isinstance(artist_image_data, bytes):
                    artist_image = await asyncio.to_thread(Image.open, BytesIO(artist_image_data))
                    if (artwork_role := self._artwork_role) is not None:
                        await artwork_role.set_artist_artwork(artist_image)
            elif (artwork_role := self._artwork_role) is not None:
                await artwork_role.set_artist_artwork(None)

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
        if (metadata_role := self._metadata_role) is not None:
            metadata_role.set_metadata(Metadata())
        if (artwork_role := self._artwork_role) is not None:
            await artwork_role.set_album_artwork(None)
            await artwork_role.set_artist_artwork(None)
        self.last_sent_artwork_url = None
        self.last_sent_artist_artwork_url = None

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

        # Send album and artist artwork
        if queue_item:
            await self._send_album_artwork(queue_item)
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
        repeat = SendspinRepeatMode.OFF
        if queue and queue.repeat_mode == RepeatMode.ALL:
            repeat = SendspinRepeatMode.ALL
        elif queue and queue.repeat_mode == RepeatMode.ONE:
            repeat = SendspinRepeatMode.ONE

        shuffle = queue.shuffle_enabled if queue else False
        is_playing = self.state.playback_state == PlaybackState.PLAYING

        # Prefer queue/media elapsed as source of truth. Only interpolate while
        # actively playing; for paused/idle states keep the last fixed position.
        elapsed_time: float | None = (
            float(current_media.elapsed_time) if current_media.elapsed_time is not None else None
        )
        if is_playing and current_media.corrected_elapsed_time is not None:
            elapsed_time = current_media.corrected_elapsed_time
        if elapsed_time is None:
            elapsed_time = self.corrected_elapsed_time if is_playing else self.elapsed_time
        track_progress = max(0, int(elapsed_time * 1000)) if elapsed_time is not None else 0

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

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        entries: list[ConfigEntry] = []
        # Build dynamic format options from player's supported formats
        player_role = self._player_role
        if player_role is not None:
            supported_formats = player_role.get_supported_formats()
            if supported_formats:
                format_options = [
                    ConfigValueOption(
                        title="Automatic (let client decide)",
                        value=SENDSPIN_FORMAT_AUTOMATIC,
                    ),
                ]
                for fmt in supported_formats:
                    format_options.append(
                        ConfigValueOption(
                            title=format_to_display_string(fmt),
                            value=format_to_option_value(fmt),
                        )
                    )
                entries.append(
                    ConfigEntry(
                        key=CONF_PREFERRED_SENDSPIN_FORMAT,
                        type=ConfigEntryType.STRING,
                        label="Preferred audio format",
                        description="Select the audio format to use for playback on this player.",
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
                    label="Static playback delay (ms)",
                    description=(
                        "Offset in milliseconds to keep this player in sync with other players. "
                        "Increase if audio plays too late, for example to compensate for latency "
                        "from an amp, active speakers, or the OS."
                    ),
                    required=False,
                    default_value=DEFAULT_SENDSPIN_STATIC_DELAY,
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
