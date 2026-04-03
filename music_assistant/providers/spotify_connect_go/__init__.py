"""
Spotify Connect Go plugin for Music Assistant.

This plugin uses go-librespot with its web interface for better control capabilities.
We tie a single player to a single Spotify Connect daemon.
The provider has multi instance support,
so multiple players can be linked to multiple Spotify Connect daemons.
"""

from __future__ import annotations

import asyncio
import json
import os
import yaml
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, cast

import aiohttp
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.models.plugin import PluginProvider, PluginSource

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_SERVER_PORT = "server_port"
CONNECT_ITEM_ID = "spotify_connect_go"

# Default server port for go-librespot web interface
DEFAULT_SERVER_PORT = 3678

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SpotifyConnectGoProvider(mass, manifest, config)

async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        CONF_ENTRY_WARN_PREVIEW,
        ConfigEntry(
            key=CONF_MASS_PLAYER_ID,
            type=ConfigEntryType.STRING,
            label="Connected Music Assistant Player",
            description="Select the player for which you want to enable Spotify Connect Go.",
            multi_value=False,
            options=[
                ConfigValueOption(x.display_name, x.player_id)
                for x in mass.players
            ],
            required=True,
        ),
        ConfigEntry(	
            key=CONF_SERVER_PORT,
            type=ConfigEntryType.INTEGER,
            label="Web Interface Port",
            description="Port for the go-librespot web interface (default: 3678)",
            default_value=DEFAULT_SERVER_PORT,
            required=False,
        ),
        ConfigEntry(
            key="metadata_delay",
            type=ConfigEntryType.FLOAT,
            label="Metadata Delay (seconds)",
            description="Delay metadata updates to sync with audio playback (0-10 seconds). Adjust based on your buffer chain latency.",
            default_value=3.5,
            required=False,
            range=(0, 10),
        ),
    )

class SpotifyConnectGoProvider(PluginProvider):
    """Implementation of a Spotify Connect Go Plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        #super().__init__(mass, manifest, SUPPORTED_FEATURES)
        self.mass_player_id = cast("str", self.config.get_value(CONF_MASS_PLAYER_ID))
        self.server_port = cast(
            "int", self.config.get_value(CONF_SERVER_PORT) or DEFAULT_SERVER_PORT
        )
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self.config_dir = os.path.join(self.cache_dir, "config")
        #self._go_librespot_bin = "/usr/local/bin/go-librespot"
        self._go_librespot_bin = "/media/bin/go-librespot"
        self._stop_called: bool = False 
        self._runner_task: asyncio.Task | None = None
        self._websocket_task: asyncio.Task | None = None
        self._go_librespot_proc: AsyncProcess | None = None
        self._go_librespot_started = asyncio.Event()
        self.named_pipe = f"/tmp/{self.instance_id}"  # noqa: S108
        self._api_base_url = f"http://localhost:{self.server_port}"
        self._ws_url = f"ws://localhost:{self.server_port}/events"
        self._ws_session: aiohttp.ClientSession | None = None
        self._ws_connection = None
        # Create the source details
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.manifest.name,
            passive=True,
            can_play_pause=True,
            can_seek=True,
            can_next_previous=True,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                codec_type=ContentType.PCM_S16LE,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
           # metadata=PlayerMedia(
           #     "Spotify Connect Go",
           # ),
            stream_type=StreamType.NAMED_PIPE,
            path=self.named_pipe,
        )
        self._on_unload_callbacks: list[Callable[..., None]] = [
            self.mass.subscribe(
                self._on_mass_player_event,
                (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED),
                id_filter=self.mass_player_id,
            ),
        ]
        self._current_track_uri: str | None = None  # ADD THIS LINE
        self._metadata_update_task: asyncio.Task | None = None  # ADD THIS for cancelling delayed updates

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.AUDIO_SOURCE}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Check if go-librespot binary exists
        if not os.path.exists(self._go_librespot_bin):
            raise FileNotFoundError(
                f"go-librespot binary not found at {self._go_librespot_bin}"
            )
        
        # Create config directory if it doesn't exist
        os.makedirs(self.config_dir, exist_ok=True)
        
        self.player = self.mass.players.get(self.mass_player_id)
        if self.player:
            self._setup_player_daemon()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True
        
        # Close WebSocket connection
        if self._ws_connection:
            await self._ws_connection.close()
        if self._ws_session:
            await self._ws_session.close()
        
        # Stop the go-librespot process
        if self._go_librespot_proc:
            await self._go_librespot_proc.close(True)
        
        # Cancel tasks
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task
        
        if self._websocket_task and not self._websocket_task.done():
            self._websocket_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._websocket_task
        
        for callback in self._on_unload_callbacks:
            callback()

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    async def on_source_play_request(self, player_id: str) -> None:
        """Handle play request for this source."""
        await self._send_api_command("player/resume")

    async def on_source_pause_request(self, player_id: str) -> None:
        """Handle pause request for this source."""
        await self._send_api_command("player/pause")

    async def on_source_stop_request(self, player_id: str) -> None:
        """Handle stop request for this source."""
        await self._send_api_command("player/pause")

    async def on_source_next_request(self, player_id: str) -> None:
        """Handle next track request for this source."""
        await self._send_api_command("player/next", method="POST")

    async def on_source_previous_request(self, player_id: str) -> None:
        """Handle previous track request for this source."""
        await self._send_api_command("player/prev", method="POST")

    async def on_source_seek_request(self, player_id: str, position: int) -> None:
        """Handle seek request for this source (position in seconds)."""
        # Convert seconds to milliseconds
        position = position * 1000
        await self._send_api_command(f"player/seek?position={position}", method="PUT")

    async def _send_api_command(self, endpoint: str, method: str = "POST") -> None:
        """Send a command to the go-librespot API."""
        url = f"{self._api_base_url}/{endpoint}"
        self.logger.debug("Sending %s request to %s", method, url)
        try:
            async with aiohttp.ClientSession() as session:
                if method == "POST":
                    async with session.post(url) as response:
                        response_text = await response.text()
                        self.logger.debug("API response (%s): %s - %s", response.status, endpoint, response_text)
                        if response.status != 200:
                            self.logger.error(
                                "API command failed: %s - Status: %s - Response: %s",
                                endpoint,
                                response.status,
                                response_text,
                            )
                elif method == "PUT":
                    async with session.put(url) as response:
                        response_text = await response.text()
                        self.logger.debug("API response (%s): %s - %s", response.status, endpoint, response_text)
                        if response.status != 200:
                            self.logger.error(
                                "API command failed: %s - Status: %s - Response: %s",
                                endpoint,
                                response.status,
                                response_text,
                            )
        except Exception as e:
            self.logger.error("Failed to send API command %s: %s", endpoint, e)

    def _create_config_file(self) -> str:
        """Create go-librespot config file and return its path."""
        config_path = os.path.join(self.config_dir, "config.yml")
        
        config = {
            "zeroconf_enabled": True,
            "zeroconf_port": 0,
            "credentials": {
                "type": "zeroconf",
                "zeroconf": {
                    "persist_credentials": True
                }
            },
            "server": {
                "enabled": True,
                "address": "0.0.0.0",
                "port": self.server_port,
                "allow_origin": "*",
                "cert_file": "",
                "key_file": ""
            },
            "log_level": "info",
            "device_id": "",
            "device_name": self.name,
            "device_type": "computer",
            "audio_backend": "pipe",
            "audio_device": "",
            "audio_output_pipe": self.named_pipe,
            "audio_output_pipe_format": "s16le",
            "audio_buffer_time": 50000,  # 500ms in microseconds
            "audio_period_count": 4,
            "bitrate": 320,
            "volume_steps": 100,
            "initial_volume": 100,
            "external_volume": False,
            "disable_autoplay": False,
        }
        
        with open(config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
        
        return config_path

    async def _go_librespot_runner(self) -> None:
        """Run the spotify connect daemon in a background task."""
        self.logger.info("Starting Spotify Connect Go background daemon")
        
        # Create named pipe for audio
        await check_output("rm", "-f", self.named_pipe)
        await asyncio.sleep(0.1)
        await check_output("mkfifo", self.named_pipe)
        await check_output("chmod", "666", self.named_pipe)
        await asyncio.sleep(0.1)
        
        # Verify pipe was created
        if os.path.exists(self.named_pipe):
            self.logger.info("Named pipe created successfully at %s", self.named_pipe)
        else:
            self.logger.error("Failed to create named pipe at %s", self.named_pipe)
        
        # Create config file
        config_file = self._create_config_file()
        self.logger.debug("Created config file at: %s", config_file)
        
        try:
            args: list[str] = [
                self._go_librespot_bin,
                "--config_dir",
                self.config_dir,
            ]
            
            self.logger.debug("Starting go-librespot with args: %s", " ".join(args))
            
            self._go_librespot_proc = go_librespot = AsyncProcess(
                args, stdout=False, stderr=True, name=f"go-librespot[{self.name}]"
            )
            await go_librespot.start()
            
            # Give the server time to start
            await asyncio.sleep(3)
            
            # Check if server is responding
            max_retries = 5
            for i in range(max_retries):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"{self._api_base_url}/status") as response:
                            if response.status == 200:
                                self._go_librespot_started.set()
                                self.logger.info("go-librespot web interface is ready")
                                break
                except Exception as e:
                    if i < max_retries - 1:
                        self.logger.debug("Waiting for go-librespot to start (attempt %d/%d)", i+1, max_retries)
                        await asyncio.sleep(1)
                    else:
                        self.logger.error("Failed to connect to go-librespot web interface: %s", e)
            
            # Only start WebSocket listener if the server started successfully
            if self._go_librespot_started.is_set():
                self._websocket_task = self.mass.create_task(self._websocket_listener())
            
            # Create a task to read stderr
            stderr_task = self.mass.create_task(self._read_stderr_output(go_librespot))
            
            # Wait for the process to complete
            return_code = await go_librespot.wait()
            self.logger.info("go-librespot process exited with return code: %s", return_code)
            
            # Cancel stderr task
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task
                    
        except asyncio.CancelledError:
            self.logger.info("go-librespot runner cancelled")
        except Exception as e:
            self.logger.error("Error running go-librespot: %s", e)
        finally:
            if self._go_librespot_proc:
                await self._go_librespot_proc.close(True)
            self.logger.info("Spotify Connect Go background daemon stopped for %s", self.name)
            await check_output("rm", "-f", self.named_pipe)
            
            if not self._go_librespot_started.is_set():
                self.unload_with_error("Unable to initialize go-librespot daemon.")
                return
            
            # Auto restart if not stopped manually
            if not self._stop_called and self._go_librespot_started.is_set():
                self.logger.warning("go-librespot exited unexpectedly, restarting in 5 seconds...")
                await asyncio.sleep(5)
                self._setup_player_daemon()
    
    async def _read_stderr_output(self, process: AsyncProcess) -> None:
        """Read stderr output from go-librespot process."""
        try:
            async for line in process.iter_stderr():
                if "error" in line.lower():
                    self.logger.error("[go-librespot] %s", line)
                else:
                    self.logger.debug("[go-librespot] %s", line)
        except asyncio.CancelledError:
            pass

    async def _websocket_listener(self) -> None:
        """Listen to WebSocket events from go-librespot."""
        retry_count = 0
        max_retries = 10
        
        while not self._stop_called and retry_count < max_retries:
            try:
                self._ws_session = aiohttp.ClientSession()
                async with self._ws_session.ws_connect(self._ws_url) as ws:
                    self._ws_connection = ws
                    self.logger.info("Connected to go-librespot WebSocket")
                    retry_count = 0
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_websocket_event(json.loads(msg.data))
                            self.logger.info("WebSocket message: %s", json.loads(msg.data))
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            self.logger.error("WebSocket error: %s", ws.exception())
                            break
            except aiohttp.ClientError as e:
                retry_count += 1
                self.logger.warning(
                    "WebSocket connection failed (attempt %d/%d): %s", 
                    retry_count, max_retries, e
                )
                if retry_count < max_retries:
                    await asyncio.sleep(2)
            except Exception as e:
                self.logger.error("Unexpected WebSocket error: %s", e)
                break
            finally:
                if self._ws_session and not self._ws_session.closed:
                    await self._ws_session.close()
                self._ws_session = None

    async def _handle_websocket_event(self, event_data: dict) -> None:
        """Handle WebSocket event from go-librespot."""
        event_type = event_data.get("type")
        
        # Log all events for debugging
        self.logger.info("WebSocket event: %s - Data keys: %s", event_type, list(event_data.get("data", {}).keys()) if event_data.get("data") else "None")
        self.logger.info(event_data)
        position = 0
        
        # Handle metadata updates - these can come in various event types
        if event_type in ("metadata", "track_changed", "new_track"):
            self.logger.info("Metadata event received, updating...")
            await self._update_metadata(event_data.get("data", {}))
            player = self.mass.players.get(self.mass_player_id)
            await self._on_resume(player)

        elif event_type in ("will_play", "playback_started", "playing", "active"):
            self.logger.info("Playback starting event: %s", event_type)
            
            is_resume = False
            if data := event_data.get("data", {}):
                is_resume = data.get("resume", False)
                
                # Check if metadata is in the event
                if not is_resume and ("track" in data or "metadata" in data or "name" in data or "title" in data):
                    await self._update_metadata(data)
                # Check if this is a restart of the same track (no metadata, resume=False)
                elif not is_resume and "uri" in data:
                    track_uri = data.get("uri", "")
                    if track_uri == self._current_track_uri:
                        self.logger.info("Track restart detected - resetting position to 0")
                        # Reset position to 0 for track restart
                        if self._source_details.metadata:
                            self._source_details.metadata.elapsed_time = 0
                        if self._source_details.in_use_by:
                            player = self.mass.players.get(self._source_details.in_use_by)
                            if player:
                                import time
                                if hasattr(player, '_attr_elapsed_time'):
                                    player._attr_elapsed_time = 0
                                    player._attr_elapsed_time_last_updated = time.time()
                                if hasattr(player, 'update_state'):
                                    player.update_state()
            
            if not self._source_details.in_use_by:
                self.logger.info("Selecting source on player %s", self.mass_player_id)
                await self.mass.players.select_source(self.mass_player_id, self.instance_id)
                self._source_details.in_use_by = self.mass_player_id
                
                # Verify active_source was set correctly
                await asyncio.sleep(0.1)  # Give MA a moment to process
                player = self.mass.players.get(self.mass_player_id)
                if player:
                    self.logger.info("After select_source - active_source: %s (expected: %s)", 
                                   player.active_source, self.instance_id)
            
            player = self.mass.players.get(self.mass_player_id)
            await self._on_play(player)
            if player:
                if is_resume:
                    self.logger.debug("Resume detected - MA will restart progress tracking")
                
                player_attrs = [attr for attr in dir(player) if 'next' in attr.lower() or 'prev' in attr.lower() or 'skip' in attr.lower()]
                self.logger.info("Player attributes related to skip: %s", player_attrs)

                self.logger.info("Player active_source: %s, our instance_id: %s", 
                               player.active_source, self.instance_id)
                self.logger.info("Source in_use_by: %s", self._source_details.in_use_by)
                self.logger.info("Source metadata: %s", self._source_details.metadata)
            
                if hasattr(player, 'can_next'):
                    player.can_next = True
                    self.logger.debug("Set player.can_next = True")
                if hasattr(player, 'can_previous'):
                    player.can_previous = True
                    self.logger.debug("Set player.can_previous = True")
                if hasattr(player, 'can_next_previous'):
                    player.can_next_previous = True
                    self.logger.debug("Set player.can_next_previous = True")
                
                self.logger.debug("Player %s updated with source %s", self.mass_player_id, self.instance_id)
                
        elif event_type in ("playback_paused", "paused", "inactive"):
            self.logger.debug("Playback paused")
            
            if self._source_details.in_use_by:
                player = self.mass.players.get(self._source_details.in_use_by)
                if player:
                    import time
                    # Update position if provided, or keep current
                    if data := event_data.get("data", {}):
                        if "position" in data:
                            position_sec = data.get("position") / 1000
                            if self._source_details.metadata:
                                self._source_details.metadata.elapsed_time = position_sec
                            if hasattr(player, '_attr_elapsed_time'):
                                player._attr_elapsed_time = position_sec
                    
                    # Freeze the timestamp to stop progress calculation
                    if hasattr(player, '_attr_elapsed_time_last_updated'):
                        player._attr_elapsed_time_last_updated = time.time()
                        self.logger.debug("Froze elapsed_time at %s seconds", player._attr_elapsed_time)
                    
                    if hasattr(player, 'update_state'):
                        player.update_state()
                    
        elif event_type in ("stopped", "session_disconnected"):
            self.logger.info("Playback stopped/disconnected event: %s", event_type)
            
            previous_player_id = self._source_details.in_use_by
            previous_player = self.mass.players.get(previous_player_id) if previous_player_id else None
            
            if previous_player:
                await self._on_stop(previous_player)
            
            if event_type == "session_disconnected":
                self.logger.info("Session disconnected - clearing everything")
                self._source_details.in_use_by = None
                self._source_details.metadata = None
                
                if previous_player:
                    previous_player.current_media = None
            else:
                self.logger.info("Playback stopped - keeping source and metadata for potential resume")
                    
        elif event_type == "volume_changed":
            volume = event_data.get("data", {}).get("volume", 0)
            self.logger.debug("Volume changed to %d", volume)
            
            if self._source_details.in_use_by:
                try:
                    await self.mass.players.cmd_volume_set(self._source_details.in_use_by, volume)
                except UnsupportedFeaturedException:
                    self.logger.debug("Player %s does not support volume control", self._source_details.in_use_by)
                    
        elif event_type in ("seek", "seeked", "position_correction"):
            if data := event_data.get("data", {}):
                if "position" in data:
                    import time
                    position_ms = data.get("position")
                    position_sec = position_ms / 1000
                    self.logger.debug("Position update: %s ms", position_ms)
                    
                    # Check for track URI in position updates
                    current_uri = data.get("uri", "")
                    if current_uri and current_uri != self._current_track_uri:
                        self.logger.warning("Position update has different URI - ignoring stale data")
                        return
                    
                    # Detect if position jumped backwards (track restart)
                    position_jumped_back = False
                    if self._source_details.metadata:
                        old_position = self._source_details.metadata.elapsed_time
                        # If position jumps back by more than 3 seconds, it's a restart
                        if old_position > position_sec + 3:
                            self.logger.info("Position jumped backwards from %s to %s - track restarted", 
                                           old_position, position_sec)
                            position_jumped_back = True
                        self._source_details.metadata.elapsed_time = position_sec
                    
                    if self._source_details.in_use_by:
                        player = self.mass.players.get(self._source_details.in_use_by)
                        if player:
                            # Cap position to duration
                            if self._source_details.metadata and self._source_details.metadata.duration:
                                position_sec = min(position_sec, self._source_details.metadata.duration)
                            
                            if hasattr(player, '_attr_elapsed_time'):
                                player._attr_elapsed_time = position_sec
                                player._attr_elapsed_time_last_updated = time.time()
                                self.logger.debug("Set player position to %s at timestamp %s", 
                                               position_sec, time.time())
                                
                            if hasattr(player, 'update_state'):
                                player.update_state()
                                
                            self.logger.debug("Updated position to %s seconds", position_sec)
                            
        # ... rest of event handlers remain the same ...
        elif event_type == "preload_next":
            # go-librespot is preloading the next track
            self.logger.debug("Preloading next track")
            
        elif event_type == "end_of_track":
            # Track finished playing
            self.logger.debug("Track ended")
            # Metadata for next track should arrive soon
            
        elif event_type in ("session_connected", "device_became_active"):
            # Device is now active
            self.logger.info("Device became active")
            if data := event_data.get("data", {}):
                if user_name := data.get("user_name"):
                    self.logger.info("User connected: %s", user_name)
                    
        elif event_type == "session_client_changed":
            # Client controlling the session changed
            if data := event_data.get("data", {}):
                client_name = data.get("client_name", "Unknown")
                self.logger.info("Control client changed to: %s", client_name)
                
        elif event_type == "loading":
            # Track is loading
            self.logger.debug("Loading track...")
            if data := event_data.get("data", {}):
                track_id = data.get("track_id", "")
                self.logger.debug("Loading track ID: %s", track_id)
                
        else:
            # Log unknown event types for debugging
            self.logger.debug("Unhandled WebSocket event type: %s with data: %s", event_type, event_data.get("data"))
			
    async def get_source_metadata(self, source_id: str) -> PlayerMedia | None:
        """Get current metadata for the given source."""
        if source_id == self.instance_id:
            return self._source_details.metadata
        return None			

    async def _update_metadata(self, metadata: dict) -> None:
        """Update metadata from go-librespot events."""
        self.logger.debug("_update_metadata called with metadata keys: %s", list(metadata.keys()) if metadata else "None")
        
        if not metadata:
            self.logger.warning("_update_metadata: No metadata provided")
            return
        
        track_info = metadata.get("track", metadata)
        self.logger.debug("track_info keys: %s", list(track_info.keys()))
        
        from music_assistant_models.player import PlayerMedia
        
        # Extract URI first to detect track changes
        track_uri = track_info.get("uri", "")
        is_new_track = (track_uri != self._current_track_uri)
        
        if is_new_track:
            self.logger.info("New track detected: %s", track_uri)
            self._current_track_uri = track_uri
        
        # Extract fields using go-librespot's field names
        title = track_info.get("name", "Unknown")
        
        # Handle artist_names
        artist = "Unknown"
        if artist_names := track_info.get("artist_names"):
            if isinstance(artist_names, list) and artist_names:
                artist = artist_names[0] if isinstance(artist_names[0], str) else str(artist_names[0])
            elif isinstance(artist_names, str):
                artist = artist_names
        
        album_name = track_info.get("album_name", "Unknown")
        image_url = track_info.get("album_cover_url")
        duration = track_info.get("duration")
        
        self.logger.info("Creating PlayerMedia: title=%s, artist=%s, album=%s", title, artist, album_name)
        
        media = PlayerMedia(
            uri=track_uri.replace("spotify:", "spotifyconnect:"),
            title=title,
            artist=artist,
            album=album_name,
            media_type=MediaType.TRACK,
            duration=duration,
        )
        
        if image_url:
            media.image_url = image_url
        
        # Handle duration - go-librespot sends milliseconds
        if duration := track_info.get("duration"):
            media.duration = duration / 1000
            self.logger.debug("Track duration: %s seconds", media.duration)
        
        # Force position to 0 for new tracks, but honor position when reconnecting
        if is_new_track:
            # Check if this looks like a reconnection (position > 5 seconds) or truly new track
            reported_position = track_info.get("position", 0) / 1000 if "position" in track_info else 0
            
            if reported_position > 5:
                # Likely reconnecting to in-progress track, honor the position
                media.elapsed_time = reported_position
                self.logger.info("Reconnecting to track at position: %s seconds", reported_position)
            else:
                # New track starting from beginning
                media.elapsed_time = 0
                self.logger.info("New track - forcing elapsed_time to 0")
        elif "position" in track_info:
            position = track_info.get("position")
            media.elapsed_time = position / 1000
            self.logger.debug("Same track - position: %s seconds", media.elapsed_time)
        else:
            media.elapsed_time = 0
            self.logger.debug("No position in metadata, setting elapsed_time to 0")
        
        # Add extra metadata
        if track_number := track_info.get("track_number"):
            media.track_number = track_number
        if disc_number := track_info.get("disc_number"):
            media.disc_number = disc_number
        
        # Update source metadata
        self._source_details.metadata = media
        self.logger.info("Updated source metadata: %s - %s (uri: %s)", 
                       media.title, media.artist, media.uri)
        
        # Update player elapsed time when metadata changes
        if self._source_details.in_use_by:
            player = self.mass.players.get(self._source_details.in_use_by)
            if player:
                import time
                if hasattr(player, '_attr_elapsed_time'):
                    # **ADD: Cap elapsed_time to duration to prevent overflow**
                    safe_elapsed_time = media.elapsed_time
                    if media.duration and safe_elapsed_time > media.duration:
                        safe_elapsed_time = media.duration
                        self.logger.warning("Capping elapsed_time from %s to duration %s", 
                                          media.elapsed_time, media.duration)
                    
                    player._attr_elapsed_time = safe_elapsed_time
                    player._attr_elapsed_time_last_updated = time.time()
                    self.logger.debug("Set player elapsed_time to %s at %s", 
                                    safe_elapsed_time, player._attr_elapsed_time_last_updated)
                
                if hasattr(player, '_attr_current_media'):
                    player._attr_current_media = media
                    self.logger.debug("Set player _attr_current_media")
                
                if hasattr(player, 'update_state'):
                    player.update_state()
                     
    async def handle_player_command(self, player_id: str, command: str, **kwargs) -> None:
            """Handle player commands."""
            self.logger.info("Received command %s for player %s", command, player_id)
            
            if player_id != self.mass_player_id:
                return
                
            if command == "next":
                await self._send_api_command("player/next", method="POST")
            elif command == "previous":
                await self._send_api_command("player/prev", method="POST")
            elif command == "play":
                await self._send_api_command("player/resume", method="POST")
            elif command == "pause":
                await self._send_api_command("player/pause", method="POST")                 

    def _setup_player_daemon(self) -> None:
        """Handle setup of the spotify connect daemon for a player."""
        self._go_librespot_started.clear()
        self._runner_task = self.mass.create_task(self._go_librespot_runner())

    def _on_mass_player_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked player."""
        if event.object_id != self.mass_player_id:
            return
        if event.event == EventType.PLAYER_REMOVED:
            self._stop_called = True
            self.mass.create_task(self.unload())
            return
        if event.event == EventType.PLAYER_ADDED:
            self._setup_player_daemon()
            return


    async def _on_play(self, player):
        """Starting playback from beginning."""
        pass

    async def _on_resume(self, player):
        """Resuming from paused state."""
        pass  # MA handles this automatically

    async def _on_pause(self, player):
        """Freeze position."""
        pass  # MA handles this automatically

    async def _on_seek(self, player, new_position_seconds: float):
        """Jump to a new position."""
        pass

    async def _on_stop(self, player):
        """Reset everything."""
        pass
