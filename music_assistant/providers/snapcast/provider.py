"""Snapcast Player Provider implementation."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature
from snapcast.control.server import Snapserver

from music_assistant.helpers.process import check_output
from music_assistant.models.player_provider import PlayerProvider

from . import (
    CONF_SERVER_BUFFER_SIZE,
    CONF_SERVER_CONTROL_PORT,
    CONF_SERVER_HOST,
    CONF_USE_EXTERNAL_SERVER,
    SnapCastStreamType,
)
from .player import SnapcastPlayer

if TYPE_CHECKING:
    from snapcast.control.client import Snapclient
    from snapcast.control.group import Snapgroup
    from snapcast.control.stream import Snapstream


class SnapcastPlayerProvider(PlayerProvider):
    """Snapcast Player Provider for synchronized audio playback."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)
        self.snapcast: Snapserver | None = None
        self._players: dict[str, SnapcastPlayer] = {}
        self._snapserver_process: subprocess.Popen | None = None
        self._server_host: str = "127.0.0.1"
        self._control_port: int = 1705
        self._use_external_server: bool = False
        self._stream_pipes: dict[str, str] = {}
        self._temp_dir: Path | None = None

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._use_external_server = self.config.get_value(CONF_USE_EXTERNAL_SERVER)

        if self._use_external_server:
            self._server_host = self.config.get_value(CONF_SERVER_HOST)
            self._control_port = self.config.get_value(CONF_SERVER_CONTROL_PORT)
            self.logger.info(
                "Using external snapcast server at %s:%s", self._server_host, self._control_port
            )
        else:
            # Start built-in snapserver
            await self._start_built_in_snapserver()

        # Connect to snapserver
        await self._connect_to_snapserver()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Clean up players
        for player in self._players.values():
            with suppress(Exception):
                await player.stop()

        # Disconnect from snapserver
        if self.snapcast:
            try:
                await self.snapcast.stop()
            except Exception as err:
                self.logger.debug("Error stopping snapcast connection: %s", err)

        # Stop built-in snapserver if running
        if self._snapserver_process and not self._use_external_server:
            try:
                self._snapserver_process.terminate()
                try:
                    self._snapserver_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._snapserver_process.kill()
                    self._snapserver_process.wait()
            except Exception as err:
                self.logger.debug("Error stopping snapserver: %s", err)

        # Clean up temp directory and pipes
        if self._temp_dir and self._temp_dir.exists():
            try:
                for pipe_path in self._stream_pipes.values():
                    if os.path.exists(pipe_path):
                        Path(pipe_path).unlink(missing_ok=True)
                # Note: temp_dir will be cleaned up automatically if using tempfile
            except Exception as err:
                self.logger.debug("Error cleaning up pipes: %s", err)

    async def _start_built_in_snapserver(self) -> None:
        """Start the built-in snapserver."""
        try:
            # Check if snapserver is available
            returncode, output = await check_output("snapserver", "--version")
            if returncode != 0:
                raise RuntimeError("snapserver binary not found")

            # Validate version
            try:
                version_parts = output.decode().strip().split(".")
                if len(version_parts) >= 2:
                    major, minor = int(version_parts[0]), int(version_parts[1])
                    if major == 0 and (minor < 27 or minor == 30):
                        raise RuntimeError(
                            f"Unsupported snapserver version: {output.decode().strip()}"
                        )
            except (ValueError, IndexError):
                self.logger.warning("Could not parse snapserver version: %s", output.decode())

            # Create temporary directory for pipes
            self._temp_dir = Path(tempfile.mkdtemp(prefix="snapcast_"))

            # Create named pipes for streams
            music_pipe = self._temp_dir / "music"
            announcement_pipe = self._temp_dir / "announcement"

            os.mkfifo(str(music_pipe))
            os.mkfifo(str(announcement_pipe))

            self._stream_pipes[SnapCastStreamType.MUSIC] = str(music_pipe)
            self._stream_pipes[SnapCastStreamType.ANNOUNCEMENT] = str(announcement_pipe)

            # Create snapserver config
            config_content = self._create_snapserver_config()
            with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as config_file:
                config_file.write(config_content)
                config_file_name = config_file.name

            # Start snapserver process
            cmd = [
                "snapserver",
                "--config",
                config_file_name,
                "--logging.level",
                "debug" if self.logger.isEnabledFor(logging.DEBUG) else "info",
            ]

            self.logger.info("Starting snapserver: %s", " ".join(cmd))
            self._snapserver_process = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            # Wait for server to start
            await asyncio.sleep(3)

            if self._snapserver_process.poll() is not None:
                stdout, stderr = self._snapserver_process.communicate()
                raise RuntimeError(f"Snapserver failed to start: {stderr}")

            self.logger.info("Snapserver started successfully")

        except Exception as err:
            self.logger.error("Failed to start snapserver: %s", err)
            raise

    def _create_snapserver_config(self) -> str:
        """Create snapserver configuration."""
        buffer_ms = self.config.get_value(CONF_SERVER_BUFFER_SIZE)

        music_pipe = self._stream_pipes[SnapCastStreamType.MUSIC]
        announcement_pipe = self._stream_pipes[SnapCastStreamType.ANNOUNCEMENT]

        return f"""
[server]
datadir = {self._temp_dir}
buffer = {buffer_ms}

[http]
enabled = true
port = 1780

[tcp]
enabled = true
port = {self._control_port}

[stream]
stream = pipe://{music_pipe}?name=music&mode=read&idle_threshold=100
stream = pipe://{announcement_pipe}?name=announcement&mode=read&idle_threshold=100

[logging]
filter.snapserver = *:info
"""

    async def _connect_to_snapserver(self) -> None:
        """Connect to the snapserver."""
        max_retries = 10
        retry_delay = 1

        for attempt in range(max_retries):
            try:
                self.snapcast = Snapserver(
                    loop=asyncio.get_event_loop(),
                    host=self._server_host,
                    port=self._control_port,
                    reconnect=True,
                )

                # Set up event handlers
                self.snapcast.set_on_connect_callback(self._on_snapserver_connect)
                self.snapcast.set_on_disconnect_callback(self._on_snapserver_disconnect)
                self.snapcast.set_on_update_callback(self._on_snapserver_update)

                # Start connection
                await self.snapcast.start()

                self.logger.info(
                    "Connected to snapserver at %s:%s", self._server_host, self._control_port
                )
                return

            except Exception as err:
                if attempt < max_retries - 1:
                    self.logger.debug(
                        "Failed to connect to snapserver (attempt %d/%d): %s",
                        attempt + 1,
                        max_retries,
                        err,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # exponential backoff
                else:
                    self.logger.error(
                        "Failed to connect to snapserver after %d attempts: %s", max_retries, err
                    )
                    raise

    def _on_snapserver_connect(self, server: Snapserver) -> None:
        """Handle snapserver connection."""
        self.logger.info("Snapserver connected")
        asyncio.create_task(self._update_players_from_server())

    def _on_snapserver_disconnect(self, server: Snapserver) -> None:
        """Handle snapserver disconnection."""
        self.logger.warning("Snapserver disconnected")
        # Mark all players as unavailable
        for player in self._players.values():
            player._attr_available = False
            player.update_state()

    def _on_snapserver_update(self, server: Snapserver) -> None:
        """Handle snapserver updates."""
        self.logger.debug("Snapserver update received")
        asyncio.create_task(self._update_players_from_server())

    async def _update_players_from_server(self) -> None:
        """Update players based on snapserver state."""
        if not self.snapcast:
            return

        try:
            current_clients = set()

            # Process all groups and clients
            for group in self.snapcast.groups:
                for client in group.clients:
                    current_clients.add(client.identifier)
                    await self._setup_client_player(client, group)

            # Remove players for clients that are no longer connected
            players_to_remove = []
            for player_id, player in self._players.items():
                client_id = player_id.replace("snapcast_", "")
                if client_id not in current_clients:
                    players_to_remove.append(player_id)

            for player_id in players_to_remove:
                player = self._players.pop(player_id, None)
                if player:
                    player._attr_available = False
                    player.update_state()
                    self.logger.info("Removed snapcast client: %s", player.name)

        except Exception as err:
            self.logger.error("Error updating players from server: %s", err)

    async def _setup_client_player(self, client: Snapclient, group: Snapgroup) -> None:
        """Set up a player for a snapcast client."""
        player_id = f"snapcast_{client.identifier}"

        if player_id not in self._players:
            # Create new player
            player = SnapcastPlayer(
                provider=self,
                client=client,
                group=group,
            )
            self._players[player_id] = player
            await self.mass.players.register_or_update(player)

            self.logger.info("Registered snapcast client: %s (%s)", client.name, client.host.host)
        else:
            # Update existing player
            player = self._players[player_id]
            player.update_from_client(client, group)

    def get_stream_pipe_path(self, stream_type: SnapCastStreamType) -> str | None:
        """Get the pipe path for a stream type."""
        return self._stream_pipes.get(stream_type)

    async def get_music_stream(self) -> Snapstream | None:
        """Get the music stream."""
        if not self.snapcast:
            return None
        for stream in self.snapcast.streams:
            if stream.identifier == "music":
                return stream
        return None

    async def get_announcement_stream(self) -> Snapstream | None:
        """Get the announcement stream."""
        if not self.snapcast:
            return None
        for stream in self.snapcast.streams:
            if stream.identifier == "announcement":
                return stream
        return None

    def get_client_group(self, client_id: str) -> Snapgroup | None:
        """Get the group containing a specific client."""
        if not self.snapcast:
            return None

        for group in self.snapcast.groups:
            for client in group.clients:
                if client.identifier == client_id:
                    return group
        return None

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        if player := self._players.get(player_id):
            await player.poll()
