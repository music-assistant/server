"""MPD Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from mpd import MPDError
from mpd.asyncio import MPDClient
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant.constants import CONF_ENTRY_FLOW_MODE, CONF_ENTRY_OUTPUT_CODEC
from music_assistant.models.player import Player

from .constants import ELAPSED_POLL_INTERVAL, RECONNECT_DELAY

if TYPE_CHECKING:
    from .provider import MPDPlayerProvider

# MPD receives a single continuous HTTP stream from MA, so flow mode must always be on.
_CONF_ENTRY_FLOW_MODE_ENFORCED = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_FLOW_MODE.to_dict(),
        "default_value": True,
        "value": True,
        "hidden": True,
    }
)

# FLAC does not work for infinite HTTP streams - MPD cannot probe the header.
# Offer MP3, AAC, WAV only, with MP3 as default.
# from_dict expects options as plain dicts, not ConfigValueOption objects.
_CONF_ENTRY_OUTPUT_CODEC_MP3_DEFAULT = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_OUTPUT_CODEC.to_dict(),
        "default_value": "mp3",
        "options": [
            {"title": "MP3 (lossy)", "value": "mp3"},
            {"title": "AAC (lossy)", "value": "aac"},
            {"title": "WAV (lossless, uncompressed)", "value": "wav"},
        ],
    }
)

# Map MPD state strings to MA PlaybackState
MPD_STATE_MAP: dict[str, PlaybackState] = {
    "play": PlaybackState.PLAYING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.IDLE,
}


class MPDPlayer(Player):
    """Represents a single MPD server as a Music Assistant player.

    Audio is delivered by telling MPD to fetch MA's HTTP stream URL.
    State changes are received via MPD's idle mechanism (push-based),
    with a periodic poll for elapsed time while playing.
    """

    def __init__(
        self,
        provider: MPDPlayerProvider,
        player_id: str,
        host: str,
        port: int = 6600,
        password: str | None = None,
    ) -> None:
        """Initialize MPDPlayer."""
        super().__init__(provider, player_id)
        self.host = host
        self.port = port
        self.password = password

        # Two separate MPD connections are required:
        # - _client: for sending commands (play, stop, setvol, etc.)
        # - _idle_client: dedicated to the blocking idle() loop
        # They must be separate because idle() monopolises the connection
        # until a subsystem changes, preventing any other commands from being sent.
        self._client: MPDClient | None = None
        self._idle_client: MPDClient | None = None

        self._attr_name = f"MPD ({host})"
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PAUSE,
            PlayerFeature.SEEK,
            PlayerFeature.VOLUME_SET,
        }

    @property
    def needs_poll(self) -> bool:
        """Return True if the player needs polling for elapsed time updates."""
        # MPD's idle loop handles state changes, but elapsed time only
        # updates on playback events, so we poll while playing.
        return self._attr_playback_state == PlaybackState.PLAYING

    @property
    def poll_interval(self) -> int:
        """Return poll interval in seconds."""
        return ELAPSED_POLL_INTERVAL

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return player config entries."""
        return [
            _CONF_ENTRY_FLOW_MODE_ENFORCED,
            _CONF_ENTRY_OUTPUT_CODEC_MP3_DEFAULT,
        ]

    async def on_config_updated(self) -> None:
        """Handle initial connection and reconnection on config change."""
        await self._disconnect()
        await self._connect()

    async def poll(self) -> None:
        """Poll MPD for current state (elapsed time while playing)."""
        await self._fetch_and_sync_state()

    async def on_unload(self) -> None:
        """Handle cleanup when the player is unloaded."""
        await self._disconnect()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Connect to MPD and start the idle loop."""
        try:
            self._client = MPDClient()
            await self._client.connect(self.host, self.port)
            if self.password:
                await self._client.password(self.password)

            self._idle_client = MPDClient()
            await self._idle_client.connect(self.host, self.port)
            if self.password:
                await self._idle_client.password(self.password)

            status = await self._client.status()
            self._attr_available = True
            self._attr_device_info = DeviceInfo(
                model=f"MPD {self._client.mpd_version}",
                manufacturer="Music Player Daemon",
            )
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, self.host)
            self.logger.info("Connected to MPD at %s:%s", self.host, self.port)
            self.mass.create_task(self._idle_loop())
            await self._sync_state(status)
            self.update_state()

        except MPDError as err:
            self.logger.warning("Failed to connect to MPD at %s:%s: %s", self.host, self.port, err)
            self._attr_available = False
            self.update_state()
            self.mass.create_task(self._reconnect())

    async def _disconnect(self) -> None:
        """Disconnect both MPD clients."""
        for client in (self._client, self._idle_client):
            if client:
                client.disconnect()
        self._client = None
        self._idle_client = None

    async def _reconnect(self) -> None:
        """Wait then attempt to reconnect."""
        await asyncio.sleep(RECONNECT_DELAY)
        self.logger.debug("Attempting reconnect to MPD at %s:%s", self.host, self.port)
        await self._connect()

    # ------------------------------------------------------------------
    # Background idle loop
    # ------------------------------------------------------------------

    async def _idle_loop(self) -> None:
        """Listen for MPD state changes and sync to MA.

        MPD's idle command blocks until a subsystem changes, then yields
        the subsystem name. We act on player/mixer/playlist changes.
        """
        if self._idle_client is None:
            return
        try:
            async for subsystem in self._idle_client.idle():
                if subsystem in ("player", "mixer", "playlist"):
                    await self._fetch_and_sync_state()
        except MPDError as err:
            self.logger.warning("MPD idle loop disconnected: %s", err)
            self._attr_available = False
            self.update_state()
            self.mass.create_task(self._reconnect())

    # ------------------------------------------------------------------
    # State sync
    # ------------------------------------------------------------------

    async def _fetch_and_sync_state(self) -> None:
        """Fetch current MPD status and update MA player state."""
        if self._client is None:
            return
        try:
            status = await self._client.status()
            await self._sync_state(status)
            self.update_state()
        except MPDError as err:
            self.logger.warning("Failed to fetch MPD status: %s", err)

    async def _sync_state(self, status: dict[str, Any]) -> None:
        """Map MPD status dict onto MA player attributes."""
        mpd_state = status.get("state", "stop")
        self._attr_playback_state = MPD_STATE_MAP.get(mpd_state, PlaybackState.IDLE)

        # Volume: MPD reports -1 when no mixer is available
        volume_str = status.get("volume", "-1")
        if volume_str != "-1":
            self._attr_volume_level = max(0, min(100, int(volume_str)))

        # Elapsed time: MPD reports as a float string, absent when stopped
        elapsed_str = status.get("elapsed")
        if elapsed_str is not None:
            self._attr_elapsed_time = float(elapsed_str)
            self._attr_elapsed_time_last_updated = time.time()
        else:
            self._attr_elapsed_time = 0

    # ------------------------------------------------------------------
    # Player commands
    # ------------------------------------------------------------------

    async def play_media(self, media: PlayerMedia) -> None:
        """Send play command with MA stream URL to MPD."""
        if self._client is None:
            return
        url = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        self.logger.debug("PLAY_MEDIA on %s: %s", self.display_name, url)
        try:
            await self._client.clear()
            await self._client.add(url)
            await self._client.play(0)
            self._attr_current_media = media
            self._attr_playback_state = PlaybackState.PLAYING
            self.update_state()
        except MPDError as err:
            raise PlayerCommandFailed(f"play_media failed: {err}") from err

    async def play(self) -> None:
        """Resume playback."""
        if self._client is None:
            return
        try:
            await self._client.pause(0)
            self._attr_playback_state = PlaybackState.PLAYING
            self.update_state()
        except MPDError as err:
            raise PlayerCommandFailed(f"play failed: {err}") from err

    async def stop(self) -> None:
        """Stop playback."""
        if self._client is None:
            return
        try:
            await self._client.stop()
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_current_media = None
            self.update_state()
        except MPDError as err:
            raise PlayerCommandFailed(f"stop failed: {err}") from err

    async def pause(self) -> None:
        """Pause playback."""
        if self._client is None:
            return
        try:
            await self._client.pause(1)
            self._attr_playback_state = PlaybackState.PAUSED
            self.update_state()
        except MPDError as err:
            raise PlayerCommandFailed(f"pause failed: {err}") from err

    async def volume_set(self, volume_level: int) -> None:
        """Set volume level (0-100)."""
        if self._client is None:
            return
        try:
            await self._client.setvol(volume_level)
            self._attr_volume_level = volume_level
            self.update_state()
        except MPDError as err:
            raise PlayerCommandFailed(f"volume_set failed: {err}") from err

    async def seek(self, position: int) -> None:
        """Seek to position in seconds."""
        if self._client is None:
            return
        try:
            await self._client.seekcur(position)
        except MPDError as err:
            raise PlayerCommandFailed(f"seek failed: {err}") from err
