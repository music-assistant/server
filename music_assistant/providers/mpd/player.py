"""MPD Player implementation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from mpd import CommandError, FailureResponseCode, MPDError
from mpd import ConnectionError as MPDConnectionError
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
    COMMAND_TIMEOUT,
    CONF_ENTRY_OUTPUT_CODEC_MPD,
    CONNECT_TIMEOUT,
    ELAPSED_POLL_INTERVAL,
    HEALTHCHECK_POLL_INTERVAL,
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
        Return True because polling doubles as the connection health check.

        MPD only pushes state over the idle connection, which receives nothing
        while the player is idle, so a socket that died without a RST (a
        rebooted or re-associated player) is never noticed from that side.

        :return: Always True.
        """
        return True

    @property
    def poll_interval(self) -> int:
        """
        Return the poll interval: fast while playing, slower while idle.

        :return: The interval in seconds.
        """
        if self._attr_playback_state == PlaybackState.PLAYING:
            return ELAPSED_POLL_INTERVAL
        return HEALTHCHECK_POLL_INTERVAL

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
        errors: dict[str, str | SetupFlowError] | None = None
        while True:
            values = await session.form(entries, step_id="user", errors=errors, last_step=True)
            try:
                await session.finish({CONF_PASSWORD: str(values[CONF_PASSWORD])})
                return
            except SetupFlowError as err:
                errors = {"base": err}

    async def poll(self) -> None:
        """Fetch current MPD state to update elapsed time."""
        await self._fetch_and_sync_state()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Establish both MPD connections and start the idle loop."""
        # A failed attempt or a dead idle loop can leave connected clients behind
        await self._disconnect()
        try:
            # Only publish the clients once both are connected. A poll or command
            # that runs while this attempt is in flight must find no client rather
            # than a half-connected one: it would report a lost connection, and the
            # reconnect that arms would tear down this attempt right after it succeeded.
            async with asyncio.timeout(CONNECT_TIMEOUT):
                client, idle_client, status = await self._open_connections()
            self._client = client
            self._idle_client = idle_client
            self._attr_available = True
            self._attr_needs_setup = False
            self._attr_setup_reason = None
            self._attr_device_info = DeviceInfo(
                model=f"MPD {client.mpd_version}",
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
            self.logger.warning(
                "Failed to connect to MPD at %s:%s: %s",
                self.host,
                self.port,
                str(err) or type(err).__name__,
            )
            self._attr_available = False
            self.update_state()
            self.reconnect()

    async def _open_connections(self) -> tuple[MPDClient, MPDClient, dict[str, Any]]:
        """
        Open the command and idle connections and fetch the initial status.

        Both are closed again when any step fails, including cancellation by a newer
        attempt, so a failed attempt never leaves a connection behind.

        :return: The command client, the idle client and the initial MPD status.
        """
        client = MPDClient()
        idle_client: MPDClient | None = None
        try:
            await client.connect(self.host, self.port)
            if self.password:
                await client.password(self.password)

            idle_client = MPDClient()
            await idle_client.connect(self.host, self.port)
            if self.password:
                await idle_client.password(self.password)

            status = await client.status()
        except BaseException:
            client.disconnect()
            if idle_client is not None:
                idle_client.disconnect()
            raise
        return client, idle_client, status

    async def _disconnect(self) -> None:
        """Cancel any pending reconnect, stop the idle loop and disconnect both MPD clients."""
        self.mass.cancel_timer(self._reconnect_task_id)
        # A connect attempt may still be in flight (for up to CONNECT_TIMEOUT) and
        # would otherwise arm a new reconnect after this teardown
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

    def _handle_connection_lost(self, err: Exception) -> None:
        """
        Mark the player unavailable and schedule a reconnect after a connection loss.

        Without this, a command that fails on a dead socket only raises for that
        one call and leaves the player available-but-broken until the idle loop
        happens to report the disconnect.

        :param err: The error that revealed the lost connection.
        """
        if not self._attr_available:
            # A reconnect is already pending. Re-arming it would only push it back,
            # since scheduling the same task id restarts its timer.
            return
        self.logger.warning(
            "Lost connection to MPD at %s:%s: %s",
            self.host,
            self.port,
            # python-mpd2 signals a closed connection with a bare ConnectionError()
            str(err) or type(err).__name__,
        )
        self._attr_available = False
        self.update_state()
        self.reconnect()

    @asynccontextmanager
    async def _command(self, action: str) -> AsyncIterator[None]:
        """
        Bound the MPD commands sent inside this block and map their failures.

        python-mpd2 does not time out commands, so on a half-open socket a command
        would wait until the kernel gives up on the connection. A connection error
        or no reply within COMMAND_TIMEOUT counts as a lost connection; the
        reconnect also replaces a client left with an abandoned request. An MPD
        error reply (CommandError) only fails this command.

        :param action: Name of the player action, used in the error message.
        """
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                yield
        except TimeoutError as err:
            reason = f"no reply within {COMMAND_TIMEOUT} seconds"
            self._handle_connection_lost(TimeoutError(reason))
            raise PlayerCommandFailed(f"{action} failed: {reason}") from err
        except (MPDConnectionError, OSError) as err:
            self._handle_connection_lost(err)
            raise PlayerCommandFailed(f"{action} failed: {err}") from err
        except MPDError as err:
            raise PlayerCommandFailed(f"{action} failed: {err}") from err

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
        except (MPDError, OSError) as err:
            self._handle_connection_lost(err)

    # ------------------------------------------------------------------
    # State sync
    # ------------------------------------------------------------------

    async def _fetch_and_sync_state(self) -> None:
        """Fetch current MPD status and update MA player state."""
        if self._client is None:
            return
        try:
            # python-mpd2 does not time out commands: on a half-open socket this
            # would otherwise block the players controller loop for minutes, so
            # cap it and treat the timeout as a lost connection. TimeoutError is an
            # OSError, so it must be handled first.
            async with asyncio.timeout(COMMAND_TIMEOUT):
                status = await self._client.status()
        except TimeoutError:
            self._handle_connection_lost(
                TimeoutError(f"no status reply within {COMMAND_TIMEOUT} seconds")
            )
            return
        except (MPDConnectionError, OSError) as err:
            self._handle_connection_lost(err)
            return
        except MPDError as err:
            self.logger.warning("Failed to fetch MPD status: %s", err)
            return
        await self._sync_state(status)
        self.update_state()

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
        async with self._command("play_media"):
            await self._client.clear()
            await self._client.add(url)
            await self._client.play(0)
        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def play(self) -> None:
        """Resume playback."""
        if self._client is None:
            return
        async with self._command("play"):
            await self._client.pause(0)
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def stop(self) -> None:
        """Stop playback and clear current media."""
        if self._client is None:
            return
        async with self._command("stop"):
            await self._client.stop()
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def pause(self) -> None:
        """Pause playback."""
        if self._client is None:
            return
        async with self._command("pause"):
            await self._client.pause(1)
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """
        Set the player volume.

        :param volume_level: Volume level (0-100).
        """
        if self._client is None:
            return
        async with self._command("volume_set"):
            await self._client.setvol(volume_level)
        self._attr_volume_level = volume_level
        self.update_state()

    async def seek(self, position: int) -> None:
        """
        Seek to a position in the current track.

        :param position: Position in seconds.
        """
        if self._client is None:
            return
        async with self._command("seek"):
            await self._client.seekcur(position)
