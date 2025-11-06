"""
AirPlay Receiver plugin for Music Assistant.

This plugin allows Music Assistant to receive AirPlay audio streams
and use them as a source for any player. It uses shairport-sync to
receive the AirPlay streams and outputs them as PCM audio.

The provider has multi-instance support, so multiple AirPlay receivers
can be configured with different names.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    ImageType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import AudioFormat, MediaItemImage
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW, VERBOSE_LOG_LEVEL
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.models.plugin import PluginProvider, PluginSource
from music_assistant.providers.airplay_receiver.helpers import get_shairport_sync_binary
from music_assistant.providers.airplay_receiver.metadata import MetadataReader

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_AIRPLAY_NAME = "airplay_name"

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AirPlayReceiverProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        CONF_ENTRY_WARN_PREVIEW,
        ConfigEntry(
            key=CONF_MASS_PLAYER_ID,
            type=ConfigEntryType.STRING,
            label="Connected Music Assistant Player",
            description="Select the player that will play the AirPlay audio stream.",
            multi_value=False,
            options=[
                ConfigValueOption(x.display_name, x.player_id)
                for x in sorted(
                    mass.players.all(False, False), key=lambda p: p.display_name.lower()
                )
            ],
            required=True,
        ),
        ConfigEntry(
            key=CONF_AIRPLAY_NAME,
            type=ConfigEntryType.STRING,
            label="AirPlay Device Name",
            description="How should this AirPlay receiver be named in the AirPlay device list?",
            default_value="Music Assistant",
        ),
    )


class AirPlayReceiverProvider(PluginProvider):
    """Implementation of an AirPlay Receiver Plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self.mass_player_id = cast("str", self.config.get_value(CONF_MASS_PLAYER_ID))
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self._shairport_bin: str | None = None
        self._stop_called: bool = False
        self._runner_task: asyncio.Task[None] | None = None
        self._shairport_proc: AsyncProcess | None = None
        self._dbus_proc: AsyncProcess | None = None  # Local D-Bus daemon process
        self._shairport_started = asyncio.Event()
        self._dbus_available: bool | None = None  # Track if dbus-send is available
        # Initialize named pipe helpers
        audio_pipe_path = f"/tmp/ma_airplay_audio_{self.instance_id}"  # noqa: S108
        metadata_pipe_path = f"/tmp/ma_airplay_metadata_{self.instance_id}"  # noqa: S108
        self.audio_pipe = AsyncNamedPipeWriter(audio_pipe_path, self.logger)
        self.metadata_pipe_writer = AsyncNamedPipeWriter(metadata_pipe_path, self.logger)
        self.config_file = f"/tmp/ma_shairport_sync_{self.instance_id}.conf"  # noqa: S108
        # Use a unique port for each instance (base port 7000 + hash of instance_id)
        self.airplay_port = 7000 + (hash(self.instance_id) % 1000)
        airplay_name = cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name
        # Note: Control capabilities will be updated after checking dbus-send availability
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.name,
            # Set passive to true because we don't allow this source to be selected directly
            # It will be automatically selected when AirPlay playback starts
            passive=True,
            can_play_pause=False,  # Will be set based on dbus availability
            can_seek=False,  # Will be set based on dbus availability
            can_next_previous=False,  # Will be set based on dbus availability
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                codec_type=ContentType.PCM_S16LE,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
            metadata=StreamMetadata(
                title=f"AirPlay | {airplay_name}",
            ),
            stream_type=StreamType.NAMED_PIPE,
            path=self.audio_pipe.path,
            on_play=self._on_play,
            on_pause=self._on_pause,
            on_next=self._on_next,
            on_previous=self._on_previous,
            on_seek=self._on_seek,
        )
        self._on_unload_callbacks: list[Callable[..., None]] = []
        self._runner_error_count = 0
        self._metadata_reader: MetadataReader | None = None

    async def _check_dbus_availability(self) -> bool:
        """Check if D-Bus is available and accessible.

        Checks both that dbus-send is installed and that the D-Bus system bus
        is accessible (e.g., /run/dbus socket is mounted in containers).

        :return: True if D-Bus is fully available, False otherwise.
        """
        # Check if dbus-send command is available
        try:
            await check_output("which", "dbus-send")
        except Exception:
            self.logger.debug("dbus-send command not found")
            return False

        # Check if D-Bus session bus is accessible by trying to ping it
        # Note: This check happens before we start our own dbus-daemon,
        # so it checks if dbus-send command works in general
        try:
            # Just verify dbus-send can be executed
            # We'll start our own session bus later
            return True
        except Exception as err:
            self.logger.debug("D-Bus check failed: %s", err)
            return False

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._shairport_bin = await get_shairport_sync_binary()

        # Check if dbus-send is available for remote control
        self._dbus_available = await self._check_dbus_availability()
        if self._dbus_available:
            self.logger.debug("D-Bus available - enabling remote control features")
            self._source_details.can_play_pause = True
            self._source_details.can_next_previous = True
            # Note: Seeking is not supported by shairport-sync D-Bus interface
            self._source_details.can_seek = False
        else:
            self.logger.info(
                "dbus-send command not available - remote control features "
                "(play/pause/next/previous) will not work. "
                "Playback can still be controlled from the AirPlay source device."
            )

        self.player = self.mass.players.get(self.mass_player_id)
        if self.player:
            self._setup_shairport_daemon()

        # Subscribe to events
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_mass_player_event,
                (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED),
                id_filter=self.mass_player_id,
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True

        # Stop metadata reader
        if self._metadata_reader:
            await self._metadata_reader.stop()

        # Stop shairport-sync process
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task

        # Cleanup callbacks
        for callback in self._on_unload_callbacks:
            callback()

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    async def _create_config_file(self) -> None:
        """Create shairport-sync configuration file from template."""
        # Read template
        template_path = os.path.join(os.path.dirname(__file__), "bin", "shairport-sync.conf")

        def _read_template() -> str:
            with open(template_path, encoding="utf-8") as f:
                return f.read()

        template = await asyncio.to_thread(_read_template)

        # Replace placeholders
        airplay_name = cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name
        config_content = template.replace("{AIRPLAY_NAME}", airplay_name)
        config_content = config_content.replace("{METADATA_PIPE}", self.metadata_pipe_writer.path)
        config_content = config_content.replace("{AUDIO_PIPE}", self.audio_pipe.path)
        config_content = config_content.replace("{PORT}", str(self.airplay_port))

        # Set default volume based on player's current volume
        # Convert player volume (0-100) to AirPlay volume (-30.0 to 0.0 dB)
        player_volume = 100  # Default to 100%
        if self.player and self.player.volume_level is not None:
            player_volume = self.player.volume_level
        # Map 0-100 to -30.0...0.0
        airplay_volume = (player_volume / 100.0) * 30.0 - 30.0
        config_content = config_content.replace("{DEFAULT_VOLUME}", f"{airplay_volume:.1f}")

        # Write config file
        def _write_config() -> None:
            with open(self.config_file, "w", encoding="utf-8") as f:
                f.write(config_content)

        await asyncio.to_thread(_write_config)

    async def _setup_pipes_and_config(self) -> None:
        """Set up named pipes and configuration file for shairport-sync.

        :raises: OSError if pipe or config file creation fails.
        """
        # Remove any existing pipes and config
        await self.audio_pipe.remove()
        await self.metadata_pipe_writer.remove()
        await check_output("rm", "-f", self.config_file)

        # Create named pipes for audio and metadata
        await self.audio_pipe.create()
        await self.metadata_pipe_writer.create()

        # Create configuration file
        await self._create_config_file()

    async def _cleanup_pipes_and_config(self) -> None:
        """Clean up named pipes and configuration file."""
        await self.audio_pipe.remove()
        await self.metadata_pipe_writer.remove()
        await check_output("rm", "-f", self.config_file)

    def _process_shairport_log_line(self, line: str) -> bool:
        """Process a log line from shairport-sync stderr.

        :param line: The log line to process.
        :return: True if processing should continue, False if it should stop.
        """
        # Check for fatal errors
        if "fatal error:" in line.lower() or "unknown option" in line.lower():
            self.logger.error("Fatal error from shairport-sync: %s", line)
            self.unload_with_error(f"shairport-sync fatal error: {line}")
            return False
        if "connection from" in line:
            self.logger.info("AirPlay client connected: %s", line)
        if "Play begin" in line:
            # Initiate playback by selecting this source on the default player
            if not self._source_details.in_use_by:
                self.mass.create_task(
                    self.mass.players.select_source(self.mass_player_id, self.instance_id)
                )
                self._source_details.in_use_by = self.mass_player_id
        if "player stop" in line:
            self.logger.info("AirPlay playback stopped")
            self._source_details.in_use_by = None
        self.logger.log(VERBOSE_LOG_LEVEL, line)
        if not self._shairport_started.is_set():
            self._shairport_started.set()
        return True

    async def _shairport_runner(self) -> None:
        """Run the shairport-sync daemon in a background task."""
        assert self._shairport_bin
        self.logger.info("Starting AirPlay Receiver background daemon")

        await self._setup_pipes_and_config()

        try:
            # Start local D-Bus daemon if dbus-daemon is available
            # This provides D-Bus interface for remote control without requiring host D-Bus
            # Use session bus which doesn't require special configuration
            try:
                await check_output("which", "dbus-daemon")
                self._dbus_proc = AsyncProcess(
                    ["dbus-daemon", "--session", "--nofork", "--print-address"],
                    name=f"dbus-daemon[{self.name}]",
                    stdout=True,
                )
                await self._dbus_proc.start()
                # Give D-Bus time to start and read the bus address
                await asyncio.sleep(0.2)

                # Get the D-Bus address from stdout
                if self._dbus_proc.proc and self._dbus_proc.proc.stdout:
                    dbus_address = await asyncio.wait_for(
                        self._dbus_proc.proc.stdout.readline(), timeout=1.0
                    )
                    if dbus_address:
                        # Set DBUS_SESSION_BUS_ADDRESS for shairport-sync to use
                        dbus_addr_str = dbus_address.decode().strip()
                        os.environ["DBUS_SESSION_BUS_ADDRESS"] = dbus_addr_str
                        self.logger.info(
                            "Started local D-Bus session daemon - PID: %s, address: %s",
                            self._dbus_proc.proc.pid,
                            dbus_addr_str,
                        )
                    else:
                        self.logger.warning("D-Bus daemon started but no address received")
            except Exception as err:
                self.logger.debug(
                    "Could not start local D-Bus daemon: %s - "
                    "D-Bus remote control will not be available",
                    err,
                )

            args: list[str] = [
                self._shairport_bin,
                "--configfile",
                self.config_file,
                "-vv",
            ]

            # Start shairport-sync (includes tinysvcmdns for mDNS advertisement)
            self._shairport_proc = shairport = AsyncProcess(
                args, stderr=True, name=f"shairport-sync[{self.name}]"
            )
            await shairport.start()

            # Check if process started successfully
            await asyncio.sleep(0.1)
            if shairport.returncode is not None:
                self.logger.error(
                    "shairport-sync exited immediately with code %s", shairport.returncode
                )
                return

            # Start metadata reader
            self._metadata_reader = MetadataReader(
                self.metadata_pipe_writer.path, self.logger, self._on_metadata_update
            )
            await self._metadata_reader.start()

            # Keep reading logging from stderr until exit
            self.logger.debug("Starting to read shairport-sync stderr")
            async for stderr_line in shairport.iter_stderr():
                line = stderr_line.strip()
                if not self._process_shairport_log_line(line):
                    break

        finally:
            await shairport.close()
            self.logger.info(
                "AirPlay Receiver background daemon stopped for %s (exit code: %s)",
                self.name,
                shairport.returncode,
            )

            # Stop metadata reader
            if self._metadata_reader:
                await self._metadata_reader.stop()

            # Stop local D-Bus daemon if running
            if self._dbus_proc:
                await self._dbus_proc.close()
                self.logger.debug("Stopped local D-Bus daemon")
                self._dbus_proc = None

            # Clean up pipes and config
            await self._cleanup_pipes_and_config()

            if not self._shairport_started.is_set():
                self.unload_with_error("Unable to initialize shairport-sync daemon.")
            # Auto restart if not stopped manually
            elif not self._stop_called and self._runner_error_count >= 5:
                self.unload_with_error("shairport-sync daemon failed to start multiple times.")
            elif not self._stop_called:
                self._runner_error_count += 1
                self.mass.call_later(2, self._setup_shairport_daemon)

    def _setup_shairport_daemon(self) -> None:
        """Handle setup of the shairport-sync daemon for a player."""
        self._shairport_started.clear()
        self._runner_task = self.mass.create_task(self._shairport_runner())

    def _on_mass_player_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked player."""
        if event.object_id != self.mass_player_id:
            return
        if event.event == EventType.PLAYER_REMOVED:
            self._stop_called = True
            self.mass.create_task(self.unload())
            return
        if event.event == EventType.PLAYER_ADDED:
            self._setup_shairport_daemon()
            return

    def _on_metadata_update(self, metadata: dict[str, Any]) -> None:
        """Handle metadata updates from shairport-sync.

        :param metadata: Dictionary containing metadata updates.
        """
        self.logger.debug("Received metadata update: %s", metadata)

        # Handle metadata start (new track starting)
        # Note: We don't clear the image_url here to avoid flashing between tracks
        # The old image will display until new cover art arrives with a new timestamp
        if "metadata_start" in metadata:
            return

        # Handle volume changes
        if "volume" in metadata and self._source_details.in_use_by:
            volume = metadata["volume"]
            try:
                self.mass.create_task(
                    self.mass.players.cmd_volume_set(self._source_details.in_use_by, volume)
                )
            except UnsupportedFeaturedException:
                self.logger.debug(
                    "Player %s does not support volume control", self._source_details.in_use_by
                )

        # Update source metadata
        if self._source_details.metadata is None:
            airplay_name = cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name
            self._source_details.metadata = StreamMetadata(title=f"AirPlay | {airplay_name}")

        if "title" in metadata:
            self._source_details.metadata.title = metadata["title"]

        if "artist" in metadata:
            self._source_details.metadata.artist = metadata["artist"]

        if "album" in metadata:
            self._source_details.metadata.album = metadata["album"]

        if "duration" in metadata:
            self._source_details.metadata.duration = metadata["duration"]

        if "elapsed_time" in metadata:
            self._source_details.metadata.elapsed_time = metadata["elapsed_time"]
            # Always set elapsed_time_last_updated to current time when we receive elapsed_time
            self._source_details.metadata.elapsed_time_last_updated = time.time()

        # Handle cover art
        if "cover_art_timestamp" in metadata:
            # Use timestamp as query parameter to create a unique URL for each cover art update
            # This prevents browser caching issues when switching between tracks
            timestamp = metadata["cover_art_timestamp"]
            # Build image proxy URL for the cover art
            # The actual image bytes are stored in the metadata reader
            image = MediaItemImage(
                type=ImageType.THUMB,
                path="cover_art",
                provider=self.instance_id,
                remotely_accessible=False,
            )
            base_url = self.mass.metadata.get_image_url(image)
            # Append timestamp as query parameter for cache-busting
            self._source_details.metadata.image_url = f"{base_url}&t={timestamp}"
        elif self._metadata_reader and self._metadata_reader.cover_art_bytes:
            # Maintain image URL if we have cover art but didn't receive it in this update
            # This ensures the image URL persists across metadata updates
            if not self._source_details.metadata.image_url:
                # Generate timestamp for cache-busting even in fallback case
                timestamp = str(int(time.time() * 1000))
                image = MediaItemImage(
                    type=ImageType.THUMB,
                    path="cover_art",
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
                base_url = self.mass.metadata.get_image_url(image)
                self._source_details.metadata.image_url = f"{base_url}&t={timestamp}"

        # Signal update to connected player
        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(self._source_details.in_use_by)

    async def _send_dbus_command(self, method: str) -> None:
        """Send a D-Bus command to shairport-sync native D-Bus interface.

        Uses the native shairport-sync RemoteControl interface which is more reliable
        than MPRIS for controlling playback.

        :param method: The RemoteControl method to call (e.g., 'Play', 'Pause', 'Next', 'Previous').
        """
        if not self._dbus_available:
            self.logger.debug(
                "Skipping D-Bus command %s - D-Bus not available on this system", method
            )
            return

        if not self._shairport_proc or not self._shairport_proc.proc:
            self.logger.debug("Shairport-sync process not running, cannot send D-Bus command")
            return

        try:
            # Use native shairport-sync D-Bus interface
            # Service name includes process ID for multi-instance support
            # shairport-sync registers as org.gnome.ShairportSync.i<PID>
            service_name = f"org.gnome.ShairportSync.i{self._shairport_proc.proc.pid}"
            await check_output(
                "dbus-send",
                "--session",  # Use session bus (configured in shairport-sync.conf)
                "--print-reply",
                "--type=method_call",
                f"--dest={service_name}",
                "/org/gnome/ShairportSync",
                f"org.gnome.ShairportSync.RemoteControl.{method}",
            )
            self.logger.debug("Sent D-Bus command %s to %s", method, service_name)
        except Exception as err:
            self.logger.warning("Failed to send D-Bus command %s: %s", method, err)

    async def _on_play(self) -> None:
        """Handle play command from player controller."""
        await self._send_dbus_command("Play")

    async def _on_pause(self) -> None:
        """Handle pause command from player controller."""
        await self._send_dbus_command("Pause")

    async def _on_next(self) -> None:
        """Handle next track command from player controller."""
        await self._send_dbus_command("Next")

    async def _on_previous(self) -> None:
        """Handle previous track command from player controller."""
        await self._send_dbus_command("Previous")

    async def _on_seek(self, position: int) -> None:
        """Handle seek command from player controller.

        Note: Seeking is not supported by the shairport-sync D-Bus interface.
        The native RemoteControl interface only supports FastForward and Rewind,
        not absolute seeking to a specific position.

        :param position: Position in seconds to seek to.
        """
        self.logger.debug(
            "Seek command not supported - shairport-sync D-Bus interface does not support "
            "absolute seeking (requested position: %d seconds)",
            position,
        )

    async def resolve_image(self, path: str) -> bytes:
        """Resolve an image from an image path.

        This returns raw bytes of the cover art image received from AirPlay metadata.

        :param path: The image path (should be "cover_art" for AirPlay cover art).
        """
        if path == "cover_art" and self._metadata_reader and self._metadata_reader.cover_art_bytes:
            return self._metadata_reader.cover_art_bytes
        # Return empty bytes if no cover art is available
        return b""
