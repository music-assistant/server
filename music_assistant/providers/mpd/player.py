"""MPD Player implementation."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast

from mpd import CommandError, FailureResponseCode, MPDError
from mpd.asyncio import MPDClient
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    CONF_PASSWORD,
)
from music_assistant.models.player import Player
from music_assistant.models.setup_flow import SetupFlowError

from .constants import (
    CONF_ENTRY_OUTPUT_CODEC_MPD,
    ELAPSED_POLL_INTERVAL,
    MPD_STATE_MAP,
    RECONNECT_DELAY,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

    from .provider import MPDPlayerProvider


class MPDPlayer(Player):
    """Represents a single MPD server as a Music Assistant player."""

    _attr_poll_interval = ELAPSED_POLL_INTERVAL

    def __init__(
        self,
        provider: MPDPlayerProvider,
        player_id: str,
        host: str,
        port: int = 6600,
    ) -> None:
        """
        Initialize MPDPlayer.

        :param provider: The MPDPlayerProvider instance.
        :param player_id: Unique player identifier.
        :param host: Hostname or IP address of the MPD server.
        :param port: TCP port MPD is listening on.
        """
        super().__init__(provider, player_id)
        self.host = host
        self.port = port

        # Two separate MPD connections are required:
        # - _client: for sending commands (play, stop, setvol, etc.)
        # - _idle_client: dedicated to the blocking idle() loop
        # They must be separate because idle() monopolises the connection
        # until a subsystem changes, preventing any other commands from being sent.
        self._client: MPDClient | None = None
        self._idle_client: MPDClient | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._reconnect_task_id: str = f"mpd_reconnect_{self.player_id}"
        self._attr_needs_setup: bool = False
        self._attr_setup_reason: str | None = None

        self._attr_name = f"MPD ({host})"
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PAUSE,
            PlayerFeature.SEEK,
            PlayerFeature.VOLUME_SET,
        }

        def _schedule_disconnect() -> None:
            self.mass.call_later(0, self._disconnect, task_id=f"mpd_disconnect_{self.player_id}")

        self._on_unload_callbacks.append(_schedule_disconnect)

    @property
    def needs_setup(self) -> bool:
        """
        Return True if the player requires a password to be configured.

        :return: True when a password is required but has not yet been provided.
        """
        return self._attr_needs_setup

    @property
    def needs_poll(self) -> bool:
        """
        Return True when elapsed time polling is required.

        MPD's idle mechanism does not push elapsed time continuously;
        polling fills this gap during active playback.

        :return: True when playback is active.
        """
        return self._attr_playback_state == PlaybackState.PLAYING

    async def get_config_entries(self) -> list[ConfigEntry]:
        """
        Return player-level config entries.

        :return: List of ConfigEntry objects for this player.
        """
        return [
            CONF_ENTRY_OUTPUT_CODEC_MPD,
            CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
        ]

    async def on_config_updated(self) -> None:
        """Reconnect to MPD when player configuration changes."""
        self.password = cast("str | None", self.get_setup_value(CONF_PASSWORD) or None)
        self._attr_needs_setup = False
        self._attr_setup_reason = None
        await self._connect()

    async def run_setup_flow(self, session: SetupSession) -> None:
        """Run the setup flow: collect the MPD server password."""
        entries = [
            ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=True)
        ]
        errors: dict[str, str] | None = None
        while True:
            values = await session.form(entries, step_id="user", errors=errors, last_step=True)
            try:
                await session.finish({CONF_PASSWORD: str(values[CONF_PASSWORD])})
                return
            except SetupFlowError as err:
                errors = {"base": err.translation_key or str(err)}

    async def poll(self) -> None:
        """Fetch current MPD state to update elapsed time."""
        await self._fetch_and_sync_state()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Establish both MPD connections and start the idle loop."""
        # a failed attempt or a dead idle loop can leave connected clients behind
        await self._disconnect()
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
            self._attr_needs_setup = False
            self._attr_setup_reason = None
            self._attr_device_info = DeviceInfo(
                model=f"MPD {self._client.mpd_version}",
                manufacturer="Music Player Daemon",
            )
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, self.host)
            self.logger.info("Connected to MPD at %s:%s", self.host, self.port)
            self._idle_task = self.mass.create_task(self._idle_loop())
            await self._sync_state(status)
            self.update_state()

        except CommandError as err:
            await self._disconnect()
            if err.errno in (FailureResponseCode.PASSWORD, FailureResponseCode.PERMISSION):
                self.logger.warning(
                    "Authentication failed for MPD at %s:%s — configure password in player settings",
                    self.host,
                    self.port,
                )
                self._attr_available = False
                self._attr_needs_setup = True
                self._attr_setup_reason = "password_required"
                self.update_state()
            else:
                self.logger.warning("MPD command error at %s:%s: %s", self.host, self.port, err)
                self._attr_available = False
                self.update_state()
                self.reconnect()
        except (MPDError, OSError) as err:
            await self._disconnect()
            self.logger.warning("Failed to connect to MPD at %s:%s: %s", self.host, self.port, err)
            self._attr_available = False
            self.update_state()
            self.reconnect()

    async def _disconnect(self) -> None:
        """Cancel any pending reconnect, stop the idle loop and disconnect both MPD clients."""
        self.mass.cancel_timer(self._reconnect_task_id)
        # connecting has no timeout, so an attempt may still be in flight and would
        # otherwise arm a new reconnect after this teardown
        reconnect_task = self.mass.get_task(self._reconnect_task_id)
        if reconnect_task is not None and reconnect_task is not asyncio.current_task():
            self.mass.cancel_task(self._reconnect_task_id)
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        for client in (self._client, self._idle_client):
            if client:
                client.disconnect()
        self._client = None
        self._idle_client = None

    def reconnect(self) -> None:
        """Schedule a reconnect attempt, deduplicating any pending reconnect tasks."""
        self.mass.call_later(RECONNECT_DELAY, self._connect, task_id=self._reconnect_task_id)

    # ------------------------------------------------------------------
    # Background idle loop
    # ------------------------------------------------------------------

    async def _idle_loop(self) -> None:
        """Receive push state changes from MPD and sync to MA."""
        if self._idle_client is None:
            return
        try:
            # MPD's idle command blocks until a subsystem changes, then yields
            # the subsystem name. We act on player/mixer/playlist changes.
            async for subsystem in self._idle_client.idle():
                if subsystem in ("player", "mixer", "playlist"):
                    await self._fetch_and_sync_state()
        except MPDError as err:
            self.logger.warning("MPD idle loop disconnected: %s", err)
            self._attr_available = False
            self.update_state()
            self.reconnect()

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
        """
        Map an MPD status dict onto MA player attributes.

        :param status: Status dict as returned by the MPD ``status`` command.
        """
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
        """
        Send a play command to MPD using the MA stream URL.

        :param media: Details of the media item to play.
        """
        if self._client is None:
            return
        url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        self.logger.debug("Sending stream URL to MPD: %s", url)
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
        """Stop playback and clear current media."""
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
        """
        Set the player volume.

        :param volume_level: Volume level (0-100).
        """
        if self._client is None:
            return
        try:
            await self._client.setvol(volume_level)
            self._attr_volume_level = volume_level
            self.update_state()
        except MPDError as err:
            raise PlayerCommandFailed(f"volume_set failed: {err}") from err

    async def seek(self, position: int) -> None:
        """
        Seek to a position in the current track.

        :param position: Position in seconds.
        """
        if self._client is None:
            return
        try:
            await self._client.seekcur(position)
        except MPDError as err:
            raise PlayerCommandFailed(f"seek failed: {err}") from err
