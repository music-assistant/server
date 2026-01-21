"""Media Assistant Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType

from music_assistant.constants import CONF_ENTRY_HTTP_PROFILE
from music_assistant.models.player import Player, PlayerMedia

from .constants import CONF_ROKU_APP_ID

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
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
        return True

    @property
    def poll_interval(self) -> int:
        """Return the interval in seconds to poll the player for state updates."""
        return 5 if self.powered else 30

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        default_entries = await super().get_config_entries(action=action, values=values)
        return [
            *default_entries,
            CONF_ENTRY_HTTP_PROFILE,
        ]

    async def power(self, powered: bool) -> None:
        """Handle POWER command on the player."""
        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received POWER command on player %s", self.display_name)

        try:
            device_info = await self.roku.update()
            app_running = False
            if device_info.app is not None:
                app_running = device_info.app.app_id == self.provider.config.get_value(
                    CONF_ROKU_APP_ID
                )
        except Exception:
            self.logger.error("Failed to get app state on: %s", self.name)

        try:
            # There's no real way to "Power" on the app since device wake up / app start
            # is handled by The roku once it receives the Play Media request
            if not powered:
                if app_running:
                    await self.roku.remote("home")
                    await self.roku.remote("power")
        except Exception:
            self.logger.error("Failed to change Power state on: %s", self.name)

        # update the player state in the player manager
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        await self.roku.remote("volume_mute")

        logger = self.provider.logger.getChild(self.player_id)
        logger.info(
            "Received VOLUME_MUTE command on player %s with muted %s", self.display_name, muted
        )
        self._attr_volume_muted = muted
        self.update_state()

    async def play(self) -> None:
        """Play command."""
        await self.roku.remote("play")

        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PLAY command on player %s", self.display_name)
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def stop(self) -> None:
        """Stop command."""
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
            self._attr_current_media = None
            self.update_state()
        except Exception:
            self.logger.error("Failed to send stop signal to: %s", self.name)

    async def pause(self) -> None:
        """Pause command."""
        await self.roku.remote("play")

        logger = self.provider.logger.getChild(self.player_id)
        logger.info("Received PAUSE command on player %s", self.display_name)
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        try:
            device_info = await self.roku.update()

            app_running = False

            if device_info.app is not None:
                app_running = (
                    device_info.app.app_id == self.provider.config.get_value(CONF_ROKU_APP_ID)
                    if not device_info.app.screensaver
                    else False
                )

            f_media = {
                "u": media.uri,
                "t": "a",
                "albumName": media.album or "",
                "songName": media.title,
                "artistName": (
                    "Music Assistant Radio"
                    if media.media_type == MediaType.RADIO
                    else media.artist
                    if media.artist is not None
                    else ("Flow Mode" if self.flow_mode else "Music Assistant")
                ),
                "albumArt": ("" if self.flow_mode else media.image_url or ""),
                "songFormat": "flac",
                "duration": media.duration or "",
                "isLive": (
                    "true"
                    if media.media_type == MediaType.RADIO
                    or media.duration is None
                    or self.flow_mode
                    else ""
                ),
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
                        position = int(media_state["position"].split(" ", 1)[0]) / 1000
                        if self.elapsed_time is not None:
                            if abs(position - self.elapsed_time) > 10:
                                self._attr_current_media = self.queued
                        self._attr_elapsed_time = position
                        self._attr_elapsed_time_last_updated = time.time()
                    except Exception:
                        self.logger.info(
                            "Playback Position received from %s Was Invalid", self.name
                        )

                self.update_state()

                if not self.current_media or self._attr_playback_state != PlaybackState.PLAYING:
                    return

                image_url = self.current_media.image_url or ""

                album_name = self.current_media.album or ""
                song_name = self.current_media.title or ""
                artist_name = self.current_media.artist or ""
                if app_running and self.flow_mode:
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
        self.logger.info("Player %s unloaded", self.name)
