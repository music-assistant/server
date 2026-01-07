"""Wiim Player implementation."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo
from wiim import PlayingStatus, WiimDevice

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpService, UpnpStateVariable

    from .provider import WiimProvider

SDK_TO_MA_STATE: dict[PlayingStatus, PlaybackState] = {
    PlayingStatus.PLAYING: PlaybackState.PLAYING,
    PlayingStatus.PAUSED: PlaybackState.PAUSED,
    PlayingStatus.STOPPED: PlaybackState.IDLE,
    PlayingStatus.LOADING: PlaybackState.UNKNOWN,  # TODO Is this the right status?
}


class WiimPlayer(Player):
    """Wiim Player in Music Assistant."""

    def __init__(self, provider: WiimProvider, player_id: str, device: WiimDevice) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)

        # init some static variables
        self._attr_name = device.name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.SET_MEMBERS,
            # PlayerFeature.PLAY_ANNOUNCEMENT,
        }
        self._attr_can_group_with = {provider.instance_id}
        self.device = device

        device.rendering_control_event_callback = self._handle_sdk_rendering_control_event
        device.av_transport_event_callback = self._handle_sdk_av_transport_event

    def _handle_sdk_av_transport_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        self._update_ma_state_from_sdk_cache()

    def _handle_sdk_rendering_control_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        self._update_ma_state_from_sdk_cache()

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        return False

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info(
            "Received VOLUME_SET command on player %s with level %s",
            self.display_name,
            volume_level,
        )
        await self.device.async_set_volume(
            volume_level
        )  # update the player state in the player manager

        self._update_ma_state_from_sdk_cache()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info(
            "Received VOLUME_MUTE command on player %s with muted %s", self.display_name, muted
        )
        await self.device.async_set_mute(muted)

        self._update_ma_state_from_sdk_cache()

    async def play(self) -> None:
        """Play command."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PLAY command on player %s", self.display_name)
        await self.device.async_play()

        self._update_ma_state_from_sdk_cache()

    async def stop(self) -> None:
        """Stop command."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received STOP command on player %s", self.display_name)
        await self.device.async_stop()

        self._update_ma_state_from_sdk_cache()

    async def pause(self) -> None:
        """Pause command."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PAUSE command on player %s", self.display_name)
        await self.device.async_pause()

        self._update_ma_state_from_sdk_cache()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info(
            "Received PLAY_MEDIA command on player %s with uri %s", self.display_name, media.uri
        )

        # self._attr_current_media = media

        await self.device.async_play(uri=media.uri)

        self._update_ma_state_from_sdk_cache()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        # OPTIONAL
        # this method is optional and should be implemented if you need to handle
        # any logic when the player is unloaded from the Player controller.
        # This is called when the player is removed from the Player controller.
        self.logger.info("Player %s unloaded", self.name)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.SET_MEMBERS
        # This method is optional and should be implemented if the player supports
        # syncing/grouping with other players.
        self.logger.info("Player %s set members", self.name)

        if player_ids_to_add:
            for i in player_ids_to_add:
                await cast("WiimProvider", self.provider).wiim_controller.async_join_group(
                    self.player_id, i
                )

        if player_ids_to_remove:
            for i in player_ids_to_remove:
                await cast("WiimProvider", self.provider).wiim_controller.async_ungroup_device(i)

    def _update_ma_state_from_sdk_cache(self) -> None:
        """Update MA state from SDK's cache/HTTP poll attributes.

        This is the main method for updating this entity's MA attributes.
        Crucially, it also handles propagating metadata to followers if this is a leader.
        """
        logger = self.logger
        logger.debug("Device %s: Updating MA state from SDK cache/HTTP poll", self._attr_name)

        self._attr_available = self.device.available

        # Update DeviceInfo if name changes
        if self.device.name != self._attr_name:
            self._attr_name = self.device.name

            self._attr_device_info = DeviceInfo(
                model=self.device.model_name,
                manufacturer=self.device.manufacturer or "Unknown Manufacturer",
                software_version=self.device.firmware_version,
            )

        if not self._attr_available:
            # If device is unavailable, clear media-related attributes
            self._attr_current_media = None
            self._attr_active_source = None
            self.update_state()
            return

        # Update common attributes first
        self._attr_volume_level = self.device.volume if self.device.volume is not None else None
        self._attr_volume_muted = self.device.is_muted

        # Determine current group role (leader/follower/standalone)
        is_current_device_leader = False
        group_info = cast("WiimProvider", self.provider).wiim_controller.get_device_group_info(
            self.device.udn
        )
        if group_info and (
            group_info.get("role") == "leader" or group_info.get("role") == "standalone"
        ):
            is_current_device_leader = True
        elif group_info and group_info.get("role") == "follower":
            pass

        self._is_group_leader = is_current_device_leader

        if self._is_group_leader:
            # This device is a leader or standalone, update its
            # media metadata from its own SDK device state.
            if self.device.playing_status is not None:
                self._attr_playback_state = SDK_TO_MA_STATE.get(
                    self.device.playing_status, PlaybackState.IDLE
                )

            group_members = cast("WiimProvider", self.provider).wiim_controller.get_group_members(
                self.player_id
            )

            self._attr_group_members = [i.udn for i in group_members if i.udn != self.player_id]

            # if self.device.play_mode is not None:
            #     # Find the InputMode enum member by its value and then get its display_name
            #     try:
            #         self._attr_active_source = self.device.play_mode
            #     except ValueError:
            #         logger.warning(
            #             "Device %s: Unknown play_mode value from SDK: %s",
            #             self._attr_name,
            #             self.device.play_mode,
            #         )
            #         self._attr_active_source = "Wifi"

            # # Current Track Info / Media Metadata
            # if self.device.current_track_info:
            #     self._attr_current_media = PlayerMedia(
            #         uri=self.device.current_track_info.get("uri") or "",
            #         media_type=MediaType.UNKNOWN,
            #         title=self.device.current_track_info.get("title"),
            #         artist=self.device.current_track_info.get("artist"),
            #         album=self.device.current_track_info.get("album"),
            #         image_url=self.device.current_track_info.get("albumArtURI"),
            #         duration=self.device.current_track_duration,
            #         elapsed_time=self.device.current_position,
            #         # elapsed_time_last_updated=utcnow(),
            #     )
            # else:
            #     self._attr_current_media = None

        elif group_info and group_info.get("role") == "follower":
            self._attr_group_members.clear()
        #     # This device is a follower. It should actively pull metadata from its leader.
        #     leader_udn = group_info.get("leader_udn")
        #     if leader_udn:
        #         leader_entity_id = self._get_entity_id_for_udn(leader_udn)
        #         leader_state = (
        #             self.hass.states.get(leader_entity_id) if leader_entity_id else None
        #         )

        #         if leader_state and leader_entity_id != self.entity_id:
        #             SDK_LOGGER.debug(
        #                 f"Follower {self.entity_id}: Actively pulling metadata from leader
        #  {leader_entity_id}"
        #             )
        #             # Pull metadata from leader's state machine state
        #             self._attr_media_title = leader_state.attributes.get("media_title")
        #             self._attr_media_artist = leader_state.attributes.get(
        #                 "media_artist"
        #             )
        #             self._attr_media_album_name = leader_state.attributes.get(
        #                 "media_album_name"
        #             )
        #             # For image, use entity_picture from attributes, which might
        # be a local proxy path
        #             self._attr_media_image_url = leader_state.attributes.get(
        #                 "entity_picture"
        #             )
        #             self._attr_media_content_id = leader_state.attributes.get(
        #                 "media_content_id"
        #             )
        #             self._attr_media_content_type = leader_state.attributes.get(
        #                 "media_content_type"
        #             )
        #             self._attr_media_duration = leader_state.attributes.get(
        #                 "media_duration"
        #             )
        #             self._attr_media_position = leader_state.attributes.get(
        #                 "media_position"
        #             )
        #             self._attr_media_position_updated_at = leader_state.attributes.get(
        #                 "media_position_updated_at"
        #             )
        #             self._attr_source = leader_state.attributes.get("source")
        #             self._attr_shuffle = leader_state.attributes.get("shuffle", False)
        #             self._attr_repeat = leader_state.attributes.get(
        #                 "repeat", RepeatMode.OFF
        #             )
        #             self._attr_supported_features = leader_state.attributes.get(
        #                 "supported_features", SUPPORT_WIIM_BASE
        #             )
        #         else:
        #             SDK_LOGGER.debug(
        #                 f"Follower {self.entity_id}: Leader entity {leader_udn} not found
        # or is self. Clearing own media metadata."
        #             )
        #             # If leader not found or is self (which means an inconsistent state),
        #  clear media info
        #             self._attr_media_title = None
        #             self._attr_media_artist = None
        #             self._attr_media_album_name = None
        #             self._attr_media_image_url = None
        #             self._attr_media_content_id = None
        #             self._attr_media_content_type = None
        #             self._attr_media_duration = None
        #             self._attr_media_position = None
        #             self._attr_media_position_updated_at = None
        #             self._attr_state = MediaPlayerState.IDLE
        #     else:
        #         SDK_LOGGER.debug(
        #             f"Follower {self.entity_id}: No leader UDN found in group info.
        # Clearing own media metadata."
        #         )
        #         # No leader_udn in group_info for a follower, clear media info
        #         self._attr_media_title = None
        #         self._attr_media_artist = None
        #         self._attr_media_album_name = None
        #         self._attr_media_image_url = None
        #         self._attr_media_content_id = None
        #         self._attr_media_content_type = None
        #         self._attr_media_duration = None
        #         self._attr_media_position = None
        #         self._attr_media_position_updated_at = None
        #         self._attr_state = MediaPlayerState.IDLE

        # # Update the group_members attribute
        # self._update_supported_features()
        self.update_state()
