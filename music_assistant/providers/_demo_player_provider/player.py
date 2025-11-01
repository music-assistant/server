"""Demo Player implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import PlayerSource

from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from .provider import DemoPlayerprovider


class DemoPlayer(Player):
    """DemoPlayer in Music Assistant."""

    def __init__(self, provider: DemoPlayerprovider, player_id: str) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        # init some static variables
        self._attr_name = f"Demo Player {player_id}"
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PLAY_ANNOUNCEMENT,
        }
        self._set_attributes()

    async def on_config_updated(self) -> None:
        """Handle logic when the player is loaded or updated."""
        # OPTIONAL
        # This method is optional and should be implemented if you need to handle
        # any initialization logic after the config was initially loaded or updated.
        # This is called after the player is registered and self.config was loaded.
        # And also when the config was updated.
        # You don't need to call update_state() here.

    @property
    def needs_poll(self) -> bool:
        """Return if the player needs to be polled for state updates."""
        # MANDATORY
        # this should return True if the player needs to be polled for state updates,
        # If you player does not need to be polled, you can return False.
        return True

    @property
    def poll_interval(self) -> int:
        """Return the interval in seconds to poll the player for state updates."""
        # OPTIONAL
        # used in conjunction with the needs_poll property.
        # this should return the interval in seconds to poll the player for state updates.
        return 5 if self.playback_state == PlaybackState.PLAYING else 30

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

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        # OPTIONAL
        # this method is optional and should be implemented if you need player specific
        # configuration entries. If you do not need player specific configuration entries,
        # you can leave this method out completely to accept the default implementation.
        # Please note that you need to call the super() method to get the default entries.
        default_entries = await super().get_config_entries(action=action, values=values)
        return [
            *default_entries,
            # example of a player specific config entry
            # you can also override a default entry by specifying the same key
            # as a default entry, but with a different type or default value.
            ConfigEntry(
                key="demo_player_setting",
                type=ConfigEntryType.STRING,
                label="Demo Player Setting",
                required=False,
                default_value="default_value",
                description="This is a demo player setting.",
            ),
        ]

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.POWER
        # this method should send a power on/off command to the given player.
        logger = self.provider.logger.getChild(self.player_id)
        if powered:
            # In this demo implementation we just set the power state to ON
            # and optimistically update the state.
            # In a real implementation you would read the actual value from the player
            # either from a callback or by polling the player.
            logger.info("Received POWER ON command on player %s", self.display_name)
            self._attr_powered = True
        else:
            # In this demo implementation we just set the power state to OFF
            # and optimistically update the state.
            # In a real implementation you would read the actual value from the player
            # either from a callback or by polling the player.
            logger.info("Received POWER OFF command on player %s", self.display_name)
            self._attr_powered = False
        # update the player state in the player manager
        self.update_state()

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
        self._attr_volume_level = volume_level  # volume level is between 0 and 100
        # update the player state in the player manager
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.VOLUME_MUTE
        # this method should send a volume mute command to the given player.
        logger = self.provider.logger.getChild(self.player_id)
        logger.info(
            "Received VOLUME_MUTE command on player %s with muted %s", self.display_name, muted
        )
        self._attr_volume_muted = muted
        self.update_state()

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
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

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
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_active_source = None
        self._attr_current_media = None
        self.update_state()

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
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

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
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

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
        self._set_attributes()
        self.update_state()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        # OPTIONAL
        # this method is optional and should be implemented if you need to handle
        # any logic when the player is unloaded from the Player controller.
        # This is called when the player is removed from the Player controller.
        self.logger.info("Player %s unloaded", self.name)

    def _set_attributes(self) -> None:
        """Update/set (dynamic) properties."""
        self._attr_powered = True
        self._attr_volume_muted = False
        self._attr_volume_level = 50
