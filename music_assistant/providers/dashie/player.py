"""Dashie Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.constants import CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

if TYPE_CHECKING:
    from typing import Any

    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

    from .client import DashieClient
    from .provider import DashieProvider

AUDIOMANAGER_STREAM_MUSIC = 4


class DashiePlayer(Player):
    """Dashie Player implementation."""

    def __init__(
        self,
        provider: DashieProvider,
        player_id: str,
        client: DashieClient,
        address: str,
        dev_info: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the Dashie Player."""
        super().__init__(provider, player_id)
        self.client = client
        self._last_pushed_media_title: str | None = None
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.PAUSE,
            PlayerFeature.SEEK,
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.NEXT_PREVIOUS,
        }
        self._attr_name = self.client.device_info.get("deviceName", "Dashie")
        self._attr_device_info = DeviceInfo(
            model=(dev_info or {}).get(
                "model", self.client.device_info.get("deviceModel", "Android")
            ),
            manufacturer=(dev_info or {}).get("manufacturer", "Dashie"),
            software_version=(dev_info or {}).get("software_version"),
        )
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, address)
        self._attr_available = True
        self._attr_needs_poll = True
        self._attr_poll_interval = 3
        self._player_id_sent = False

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return player-specific config entries."""
        return [CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3]

    def set_attributes(self) -> None:
        """Set/update player attributes from device info."""
        info = self.client.device_info
        self._attr_name = info.get("deviceName", "Dashie")
        volume = info.get("audioVolume")
        if volume is not None:
            self._attr_volume_level = int(volume)
        current_url = info.get("soundUrlPlaying", "")
        if not current_url:
            self._attr_playback_state = PlaybackState.IDLE
        elif info.get("soundPaused"):
            self._attr_playback_state = PlaybackState.PAUSED
        else:
            self._attr_playback_state = PlaybackState.PLAYING
        self._attr_available = True

    async def volume_set(self, volume_level: int) -> None:
        """Set the volume level."""
        await self.client.set_audio_volume(volume_level, AUDIOMANAGER_STREAM_MUSIC)
        self._attr_volume_level = volume_level
        self.update_state()

    async def stop(self) -> None:
        """Stop playback."""
        await self.client.stop_sound()
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def play(self) -> None:
        """Resume playback."""
        await self.client.resume_sound()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Pause playback."""
        await self.client.pause_sound()
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def seek(self, position: int) -> None:
        """Seek to a position (seconds)."""
        await self.client.seek_sound(position * 1000)
        self._attr_elapsed_time = float(position)
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media on the device."""
        url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        await self.client.play_sound(url, AUDIOMANAGER_STREAM_MUSIC)
        # In flow mode the initial media object is a placeholder (title="Music Assistant").
        # Real track metadata arrives later via current_media updates, picked up by poll().
        # Only push now if we have a real track title (non-flow / direct play).
        if media.title and media.artist:
            await self._push_media_info(media)
        else:
            self._last_pushed_media_title = None  # ensure poll picks up the real track
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time.time()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    def _get_ma_server_url(self) -> str:
        """Return the MA streams server URL for this provider."""
        streams = self.provider.mass.streams
        return streams.base_url or f"http://{streams.publish_ip}:{streams.publish_port}"

    async def _push_media_info(self, media: PlayerMedia) -> None:
        """Push track metadata to the device for on-screen display."""
        title = media.title or ""
        # Strip "Artist - " prefix from title if artist is already provided separately
        artist = media.artist or ""
        if artist and title.startswith(f"{artist} - "):
            title = title[len(f"{artist} - ") :]
        # Send MA server URL so the device can query the MA API for display/control
        ma_server_url = self._get_ma_server_url()
        self.logger.debug("Pushing media info: %s", title)
        try:
            await self.client.set_media_info(
                title=title,
                artist=media.artist or "",
                album=media.album or "",
                image_url=media.image_url or "",
                duration=int((media.duration or 0) * 1000),  # seconds → milliseconds
                entity_id=self.player_id,
                ma_server_url=ma_server_url,
            )
            self._last_pushed_media_title = title
        except Exception as err:
            self.logger.debug("Failed to push media info: %s", err)

    async def poll(self) -> None:
        """Poll player for state updates."""
        try:
            async with asyncio.timeout(15):
                await self.client.get_device_info()
                self.set_attributes()
                # Push this device's player ID on first successful connect
                if not self._player_id_sent:
                    try:
                        ma_url = self._get_ma_server_url()
                        await self.client.set_player_id(self.player_id, ma_url)
                        self._player_id_sent = True
                        self.logger.info(
                            "Pushed player ID to device: %s (server=%s)", self.player_id, ma_url
                        )
                    except Exception as err:
                        self.logger.warning("Failed to push player ID: %s", err)
                info = self.client.device_info
                # Prefer track-relative position over cumulative flow position
                track_pos_ms = info.get("trackPosition")
                position_ms = (
                    track_pos_ms if track_pos_ms is not None else info.get("audioPosition")
                )
                if position_ms is not None and self._attr_playback_state == PlaybackState.PLAYING:
                    self._attr_elapsed_time = float(position_ms) / 1000.0
                    self._attr_elapsed_time_last_updated = time.time()
                # In flow mode, MA updates current_media when the track changes.
                # Push new metadata to the device if the track title has changed.
                media = self.state.current_media
                self.logger.debug(
                    "Poll media check: title=%s, last_pushed=%s",
                    media.title if media else None,
                    self._last_pushed_media_title,
                )
                if (
                    media is not None
                    and media.title
                    and media.artist  # skip placeholders with no artist
                    and media.title != self._last_pushed_media_title
                ):
                    self.logger.debug("Pushing updated media info: %s", media.title)
                    await self._push_media_info(media)
                self.update_state()
        except Exception as err:
            msg = f"Unable to connect to Dashie device: {err!s}"
            raise PlayerUnavailableError(msg) from err
