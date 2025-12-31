"""Wiim Player implementation."""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo, PlayerSource
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
            # PlayerFeature.PLAY_ANNOUNCEMENT,
        }
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

    # async def on_config_updated(self) -> None:
    #     """Handle logic when the player is loaded or updated."""
    #     # OPTIONAL
    #     # This method is optional and should be implemented if you need to handle
    #     # any initialization logic after the config was initially loaded or updated.
    #     # This is called after the player is registered and self.config was loaded.
    #     # And also when the config was updated.
    #     # You don't need to call update_state() here.

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        # MANDATORY
        # this should return True if the player needs to be polled for state updates,
        # If you player does not need to be polled, you can return False.
        return False

    @property
    def poll_interval(self) -> int:
        """Return the interval in seconds to poll the player for state updates."""
        # OPTIONAL
        # used in conjunction with the needs_poll property.
        # this should return the interval in seconds to poll the player for state updates.
        return 5

    @property
    def _source_list(self) -> list[PlayerSource]:
        """Return list of available (native) sources for this player."""
        # OPTIONAL - required only if you specified PlayerFeature.SELECT_SOURCE
        # this is an optional property that you can implement if your
        # player supports (external) source control (aux, HDMI, etc.).
        # If your player does not support sources, you can leave this out completely.
        return [
            PlayerSource(
                id="line_in",
                name="Line-In",
                passive=False,
                can_play_pause=False,
                can_next_previous=False,
                can_seek=False,
            ),
            PlayerSource(
                id="spotify_connect",
                name="Spotify",
                # by specifying passive=True, we indicate that this source
                # is not actively selectable by the user from the UI.
                passive=True,
                can_play_pause=True,
                can_next_previous=True,
                can_seek=True,
            ),
        ]

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.VOLUME_SET
        # this method should send a volume set command to the given player.

        # In this demo implementation we just set the volume level
        # and optimistically update the state.
        # In a real implementation you would send a command to the actual player and
        # get the actual value from the player either from a callback or by polling the player.
        logger = self.provider.logger.getChild(self.player_id)
        logger.info(
            "Received VOLUME_SET command on player %s with level %s",
            self.display_name,
            volume_level,
        )
        await self.device.async_set_volume(
            volume_level
        )  # update the player state in the player manager

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.VOLUME_MUTE
        # this method should send a volume mute command to the given player.
        logger = self.provider.logger.getChild(self.player_id)
        logger.info(
            "Received VOLUME_MUTE command on player %s with muted %s", self.display_name, muted
        )
        await self.device.async_set_mute(muted)

    async def play(self) -> None:
        """Play command."""
        # MANDATORY
        # this method is mandatory and should be implemented.
        # this method should send a play/resume command to the given player.
        # normally this is the point where you would resume playback
        # on your actual player device.

        # In this demo implementation we just set the playback state to PLAYING
        # and optimistically set the playback state to PLAYING.
        # In a real implementation you actually send a command to the player
        # wait for the player to report a new state before updating the playback state.
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PLAY command on player %s", self.display_name)
        await self.device.async_play()

    async def stop(self) -> None:
        """Stop command."""
        # MANDATORY
        # this method is mandatory and should be implemented.
        # this method should send a stop command to the given player.
        # normally this is the point where you would stop playback
        # on your actual player device.

        # In this demo implementation we just set the playback state to IDLE
        # and optimistically set the playback state to IDLE.
        # In a real implementation you actually send a command to the player
        # wait for the player to report a new state before updating the playback state.
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received STOP command on player %s", self.display_name)
        await self.device.async_stop()

    async def pause(self) -> None:
        """Pause command."""
        # OPTIONAL - required only if you specified PlayerFeature.PAUSE
        # this method should send a pause command to the given player.

        # In this demo implementation we just set the playback state to PAUSED
        # and optimistically set the playback state to PAUSED.
        # In a real implementation you actually send a command to the player
        # wait for the player to report a new state before updating the playback state.
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PAUSE command on player %s", self.display_name)
        await self.device.async_pause()

    async def next_track(self) -> None:
        """Next command."""
        # OPTIONAL - required only if you specified PlayerFeature.NEXT_PREVIOUS
        # this method should send a next track command to the given player.
        # Note that this is only needed/used if the player is playing a 3rd party
        # stream (e.g. Spotify, YouTube, etc.) and the player supports skipping to the next track.
        # When the player is playing MA content, this is already handled in the Queue controller.

    async def previous_track(self) -> None:
        """Previous command."""
        # OPTIONAL - required only if you specified PlayerFeature.NEXT_PREVIOUS
        # this method should send a previous track command to the given player.
        # Note that this is only needed/used if the player is playing a 3rd party
        # stream (e.g. Spotify, YouTube, etc.) and the player supports skipping to the next track.
        # When the player is playing MA content, this is already handled in the Queue controller.

    async def seek(self, position: int) -> None:
        """SEEK command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.SEEK
        # this method should send a seek command to the given player.
        # the position is the position in seconds to seek to in the current playing item.

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        # MANDATORY
        # This method is mandatory and should be implemented.
        # This method should handle the play_media command for the given player.
        # It will be called when media needs to be played on the player.
        # The media object contains all the details needed to play the item.

        # In 99% of the cases this will be called by the Queue controller to play
        # a single item from the queue on the player and the uri within the media
        # object will then contain the URL to play that single queue item.

        # If your player provider does not support enqueuing of items,
        # the queue controller will simply call this play_media method for
        # each item in the queue to play them one by one.

        # In order to support true gapless and/or enqueuing, we offer the option of
        # 'flow_mode' playback. In that case the queue controller will stitch together
        # all songs in the playbook queue into a single stream and send that to the player.
        # In that case the URI (and metadata) received here is that of the 'flow mode' stream.

        # Examples of player providers that use flow mode for playback by default are AirPlay,
        # SnapCast and Fully Kiosk.

        # Examples of player providers that optionally use 'flow mode' are Google Cast and
        # Home Assistant. They provide a config entry to enable flow mode playback.

        # Examples of player providers that natively support enqueuing of items are Sonos,
        # Slimproto and Google Cast.

        # In this demo implementation we just optimistically set the state.
        # In a real implementation you actually send a command to the player
        # wait for the player to report a new state before updating the playback state.
        logger = self.provider.logger.getChild(self.player_id)
        logger.info(
            "Received PLAY_MEDIA command on player %s with uri %s", self.display_name, media.uri
        )

        await self.device.async_play(uri=media.uri)

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next (queue) item on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.ENQUEUE
        # This method is optional and should be implemented if you want to support
        # enqueuing of the next item on the player.
        # This will be called when the player reports it started buffering a queue item
        # and when the queue items updated.
        # A PlayerProvider implementation is in itself responsible for handling this
        # so that the queue items keep playing until its empty or the player stopped.

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (native) playback of an announcement on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.PLAY_ANNOUNCEMENT
        # This method is optional and should be implemented if the player supports
        # NATIVE playback of announcements (with ducking etc.).
        # The announcement object contains all the details needed to play the announcement.
        # The volume_level is optional and can be used to set the volume level for the announcement.
        # If you do not use the announcement playerfeature, the default behavior is to play the
        # announcement as a regular media item using the play_media method and the MA player manager
        # will take care of setting the volume level for the announcement and resuming etc.

    async def select_source(self, source: str) -> None:
        """Handle SELECT SOURCE command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.SELECT_SOURCE
        # This method is optional and should be implemented if the player supports
        # selecting a source (e.g. HDMI, AUX, etc.) on the player.
        # The source is the source ID to select on the player.
        # available sources are specified in the Player.source_list property

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.SET_MEMBERS
        # This method is optional and should be implemented if the player supports
        # syncing/grouping with other players.

    async def poll(self) -> None:
        """Poll player for state updates."""
        # OPTIONAL - This is called by the Player Manager if the 'needs_poll' property is True.

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        # OPTIONAL
        # this method is optional and should be implemented if you need to handle
        # any logic when the player is unloaded from the Player controller.
        # This is called when the player is removed from the Player controller.
        self.logger.info("Player %s unloaded", self.name)

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

            if self.device.play_mode is not None:
                # Find the InputMode enum member by its value and then get its display_name
                try:
                    self._attr_active_source = self.device.play_mode
                except ValueError:
                    logger.warning(
                        "Device %s: Unknown play_mode value from SDK: %s",
                        self._attr_name,
                        self.device.play_mode,
                    )
                    self._attr_active_source = "Wifi"

            # Current Track Info / Media Metadata
            if self.device.current_track_info:
                self._attr_current_media = PlayerMedia(
                    uri=self.device.current_track_info.get("uri") or "",
                    media_type=MediaType.UNKNOWN,
                    title=self.device.current_track_info.get("title"),
                    artist=self.device.current_track_info.get("artist"),
                    album=self.device.current_track_info.get("album"),
                    image_url=self.device.current_track_info.get("albumArtURI"),
                    duration=self.device.current_track_duration,
                    elapsed_time=self.device.current_position,
                    # elapsed_time_last_updated=utcnow(),
                )
            else:
                self._attr_current_media = None

        # elif group_info and group_info.get("role") == "follower":
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
