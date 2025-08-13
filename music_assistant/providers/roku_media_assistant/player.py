"""Media Assistant Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType

from music_assistant.constants import CONF_ENTRY_HTTP_PROFILE, MASS_LOGO_ONLINE
from music_assistant.models.player import Player, PlayerMedia

from .constants import CONF_ROKU_APP_ID

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from rokuecp import Roku

    from .provider import MediaAssistantprovider


class MediaAssistantPlayer(Player):
    """MediaAssistantPlayer in Music Assistant."""

    def __init__(
        self,
        provider: MediaAssistantprovider,
        player_id: str,
        roku_name: str,
        roku: Roku,
        queued: PlayerMedia | None = None,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)
        # init some static variables
        self.roku = roku
        self.queued = queued
        self._attr_name = roku_name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.POWER,  # if the player can be turned on/off
            PlayerFeature.PAUSE,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.ENQUEUE,
        }
        self._attr_volume_muted = False
        self._attr_volume_level = 100

        self.lock = asyncio.Lock()  # Held when connecting or disconnecting the device

    async def setup(self) -> None:
        """Set up player in MA."""
        self._attr_available = False
        self._attr_powered = False
        await self.mass.players.register_or_update(self)

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
        return 5 if self.powered else 30

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        # OPTIONAL
        # this method is optional and should be implemented if you need player specific
        # configuration entries. If you do not need player specific configuration entries,
        # you can leave this method out completely to accept the default implementation.
        # Please note that you need to call the super() method to get the default entries.
        default_entries = await super().get_config_entries()
        return [
            *default_entries,
            CONF_ENTRY_HTTP_PROFILE,
        ]

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.POWER
        # this method should send a power on/off command to the given player.

        try:
            device_info = await self.roku.update()

            app_running = False

            if device_info.app is not None:
                app_running = device_info.app.app_id == self.provider.config.get_value(
                    CONF_ROKU_APP_ID
                )

            # There's no real way to "Power" on the app since device wake up / app start
            # is handled by The roku once it receives the Play Media request
            if not powered:
                self._attr_active_source = None
                if app_running:
                    await self.roku.remote("home")
                    await self.roku.remote("power")

            logger = self.provider.logger.getChild(self.player_id)
            logger.info("Received POWER command on player %s", self.display_name)
            # update the player state in the player manager
            self.update_state()
        except Exception:
            self.logger.error("Failed to change Power state on: %s", self.name)

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.VOLUME_MUTE
        # this method should send a volume mute command to the given player.

        await self.roku.remote("volume_mute")

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

        await self.roku.remote("play")

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

        try:
            device_info = await self.roku.update()

            app_running = False

            if device_info.app is not None:
                app_running = device_info.app.app_id == self.provider.config.get_value(
                    CONF_ROKU_APP_ID
                )

            if app_running:
                # The closet thing the app has to playback stop,
                # is sending a empty media object.
                # I hope to implement a better solution into the app.
                await self.roku_input(
                    {
                        "u": " ",
                        "t": "a",
                        "songName": "Music Assistant",
                        "artistName": "Waiting for Playback...",
                    },
                )

            logger = self.provider.logger.getChild(self.player_id)
            logger.info("Received STOP command on player %s", self.display_name)
            self._attr_playback_state = PlaybackState.IDLE
            self.update_state()
        except Exception:
            self.logger.error("Failed to send stop signal to: %s", self.name)

    async def pause(self) -> None:
        """Pause command."""
        # OPTIONAL - required only if you specified PlayerFeature.PAUSE
        # this method should send a pause command to the given player.

        await self.roku.remote("play")

        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PAUSE command on player %s", self.display_name)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

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

        if not (queue := self.mass.player_queues.get_active_queue(self.player_id)):
            return

        try:
            device_info = await self.roku.update()

            app_running = False

            if device_info.app is not None:
                app_running = (
                    device_info.app.app_id == self.provider.config.get_value(CONF_ROKU_APP_ID)
                    if not device_info.app.screensaver
                    else False
                )

            current_duration = 0

            if queue.current_item is not None and queue.current_item.media_item is not None:
                current_duration = cast("int", queue.current_item.media_item.duration)

            f_media = {
                "u": media.uri,
                "t": "a",
                "albumName": media.album,
                "songName": media.title,
                "artistName": "Music Assistant Radio"
                if media.media_type == MediaType.RADIO
                else media.artist,
                "albumArt": media.image_url,
                "songFormat": "flac",
                "duration": "" if media.duration is None else current_duration,
                "timeOffset": "" if media.duration is None else (current_duration - media.duration),
                "isLive": "true" if media.media_type == MediaType.RADIO else "",
            }

            if queue.flow_mode and queue.current_item:
                current_item = queue.current_item

                image_url = (
                    self.mass.metadata.get_image_url(current_item.image, size=512)
                    if current_item.image
                    else MASS_LOGO_ONLINE
                )

                album_name = ""
                song_name = ""
                artist_name = ""

                if current_item.media_item is not None:
                    media_item = current_item.media_item

                    song_name = media_item.name if media_item is not None else ""

                    if hasattr(media_item, "album"):
                        album_name = media_item.album.name if media_item.album is not None else ""

                    if hasattr(media_item, "artist_str"):
                        artist_name = media_item.artist_str

                f_media = {
                    "u": media.uri,
                    "t": "a",
                    "albumName": album_name,
                    "songName": song_name,
                    "artistName": artist_name,
                    "albumArt": image_url,
                    "songFormat": "flac",
                    "isLive": "true",
                }

            if app_running:
                await self.roku_input(f_media)
            else:
                await self.roku.launch(
                    cast("str", self.provider.config.get_value(CONF_ROKU_APP_ID)),
                    f_media,
                )

            logger = self.provider.logger.getChild(self.player_id)
            logger.info(
                "Received PLAY_MEDIA command on player %s with uri %s", self.display_name, media.uri
            )
            self._attr_powered = True
            self._attr_current_media = media
            self.update_state()
        except Exception:
            self.logger.error("Failed to Play Media on: %s", self.name)
            return

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next (queue) item on the player."""
        # OPTIONAL - required only if you specified PlayerFeature.ENQUEUE
        # This method is optional and should be implemented if you want to support
        # enqueuing of the next item on the player.
        # This will be called when the player reports it started buffering a queue item
        # and when the queue items updated.
        # A PlayerProvider implementation is in itself responsible for handling this
        # so that the queue items keep playing until its empty or the player stopped.

        try:
            device_info = await self.roku.update()

            app_running = False

            if device_info.app is not None:
                app_running = device_info.app.app_id == self.provider.config.get_value(
                    CONF_ROKU_APP_ID
                )

            if app_running:
                await self.roku_input(
                    {
                        "u": media.uri,
                        "t": "a",
                        "albumName": media.album,
                        "songName": media.title,
                        "artistName": media.artist,
                        "albumArt": media.image_url,
                        "songFormat": "flac",
                        "duration": media.duration,
                        "enqueue": "true",
                    },
                )
                self.queued = media
        except Exception:
            self.logger.error("Failed to Enqueue Media on: %s", self.name)
            return

    async def poll(self) -> None:
        """Poll player for state updates."""
        # OPTIONAL - This is called by the Player Manager if the 'needs_poll' property is True.

        # Pull Device State
        try:
            device_info = await self.roku.update()
            self._attr_available = True
        except Exception:
            self._attr_available = False
            self.logger.error("Failed to retrieve Update from: %s", self.name)
            self.update_state()
            return

        app_running = False

        if device_info.app is not None:
            app_running = device_info.app.app_id == self.provider.config.get_value(CONF_ROKU_APP_ID)

        # Update Device State
        if app_running:
            self._attr_active_source = self.player_id
        else:
            self._attr_active_source = None

        self._attr_powered = app_running

        # If Media's Playing update its state
        if self.powered and app_running:
            try:
                media_state = await self.roku._get_media_state()

                play_states: dict[str, PlaybackState] = {
                    "play": PlaybackState.PLAYING,
                    "pause": PlaybackState.PAUSED,
                }

                self._attr_playback_state = play_states.get(
                    media_state["@state"], PlaybackState.IDLE
                )

                if "position" in media_state:
                    try:
                        self._attr_elapsed_time = (
                            int(media_state["position"].split(" ", 1)[0]) / 1000
                        )
                        self._attr_elapsed_time_last_updated = time.time()
                    except Exception:
                        self.logger.info(
                            "Playback Position received from %s Was Invalid", self.name
                        )

                if not (queue := self.mass.player_queues.get_active_queue(self.player_id)):
                    return

                if (
                    self._attr_playback_state == PlaybackState.PLAYING
                    and queue.next_item
                    and queue.current_item
                    and queue.current_item.duration
                ):
                    if queue.elapsed_time >= queue.current_item.duration:
                        self._attr_current_media = self.queued

                if (
                    self._attr_playback_state == PlaybackState.PLAYING
                    and queue.current_item
                    and queue.flow_mode
                ):
                    current_item = queue.current_item

                    image_url = (
                        self.mass.metadata.get_image_url(current_item.image, size=512)
                        if current_item.image
                        else MASS_LOGO_ONLINE
                    )

                    album_name = ""
                    song_name = ""
                    artist_name = ""

                    if current_item.media_item is not None:
                        media_item = current_item.media_item

                        song_name = media_item.name if media_item is not None else ""

                        if hasattr(media_item, "album"):
                            album_name = (
                                media_item.album.name if media_item.album is not None else ""
                            )

                        if hasattr(media_item, "artist_str"):
                            artist_name = media_item.artist_str

                    if app_running:
                        await self.roku_input(
                            {
                                "u": "",
                                "t": "m",
                                "albumName": album_name,
                                "songName": song_name,
                                "artistName": artist_name,
                                "albumArt": image_url,
                                "isLive": "true",
                            },
                        )
            except Exception:
                self.logger.warning("Failed to update media state for: %s", self.name)

        self.update_state()

    async def roku_input(self, params: dict[str, Any] | None = None) -> None:
        """Send request to the running application on the Roku device."""
        if params is None:
            params = {}

        encoded = urlencode(params)
        await self.roku._request(f"input?{encoded}", method="POST", encoded=True)

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        # OPTIONAL
        # this method is optional and should be implemented if you need to handle
        # any logic when the player is unloaded from the Player controller.
        # This is called when the player is removed from the Player controller.
        self.logger.info("Player %s unloaded", self.name)
