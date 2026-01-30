"""
AriaCast Receiver plugin for Music Assistant.

This plugin implements an AriaCast protocol server that receives PCM audio
streams over WebSocket and makes them available as a source for any player.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from music_assistant_models.config_entries import (ConfigEntry,
                                                   ConfigValueOption)
from music_assistant_models.enums import (ConfigEntryType, ContentType,
                                          ImageType, PlaybackState,
                                          ProviderFeature, StreamType)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import AudioFormat, MediaItemImage
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.models.plugin import PluginProvider, PluginSource

from .config import AudioConfig, ServerConfig
from .helpers import get_local_ip
from .metadata import MetadataHandler

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (ConfigValueType,
                                                       ProviderConfig)
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_ARIACAST_NAME = "ariacast_name"
CONF_STREAMING_PORT = "streaming_port"
CONF_DISCOVERY_PORT = "discovery_port"
CONF_ALLOW_PLAYER_SWITCH = "allow_player_switch"

# Special value for auto player selection
PLAYER_ID_AUTO = "__auto__"

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# Basic hardcoded configuration (like standalone server)
BASIC_CONFIG = ServerConfig(
    SERVER_NAME="Music Assistant",
    VERSION="1.0",
    PLATFORM="MusicAssistant",
    DISCOVERY_PORT=12888,
    STREAMING_PORT=12889,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AriaCastReceiverProvider(mass, manifest, config)


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
            description="The Music Assistant player connected to this AriaCast receiver. "
            "When you stream audio via AriaCast, the audio will play on the selected player. "
            "Set to 'Auto' to automatically select a currently playing player.",
            multi_value=False,
            default_value=PLAYER_ID_AUTO,
            options=[
                ConfigValueOption("Auto (prefer playing player)", PLAYER_ID_AUTO),
                *(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in sorted(
                        mass.players.all(False, False), key=lambda p: p.display_name.lower()
                    )
                ),
            ],
            required=True,
        ),
        ConfigEntry(
            key=CONF_ALLOW_PLAYER_SWITCH,
            type=ConfigEntryType.BOOLEAN,
            label="Allow manual player switching",
            description="When enabled, you can select this plugin as a source on any player.",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_ARIACAST_NAME,
            type=ConfigEntryType.STRING,
            label="Server Name",
            description="The name that will appear in the AriaCast app discovery list on your Android device. "
            "This helps you identify this Music Assistant server when connecting from your phone or tablet.",
            default_value="Music Assistant",
        ),
        ConfigEntry(
            key=CONF_STREAMING_PORT,
            type=ConfigEntryType.INTEGER,
            label="Streaming Port",
            description="WebSocket port for audio streaming (default: 12889). DON'T change unless necessary.",
            default_value=12889,
        ),
        ConfigEntry(
            key=CONF_DISCOVERY_PORT,
            type=ConfigEntryType.INTEGER,
            label="Discovery Port",
            description="UDP port for device discovery (default: 12888). DON'T change unless necessary.",
            default_value=12888,
        ),
    )


class AriaCastReceiverProvider(PluginProvider):
    """Implementation of an AriaCast Receiver Plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize provider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        
        # Configuration
        self._default_player_id: str = (
            cast("str", self.config.get_value(CONF_MASS_PLAYER_ID)) or PLAYER_ID_AUTO
        )
        allow_switch_value = self.config.get_value(CONF_ALLOW_PLAYER_SWITCH)
        self._allow_player_switch: bool = (
            cast("bool", allow_switch_value) if allow_switch_value is not None else True
        )
        
        # Server configuration - use BASIC_CONFIG as base
        ariacast_name = cast("str", self.config.get_value(CONF_ARIACAST_NAME)) or BASIC_CONFIG.SERVER_NAME
        streaming_port = cast("int", self.config.get_value(CONF_STREAMING_PORT)) or BASIC_CONFIG.STREAMING_PORT
        discovery_port = cast("int", self.config.get_value(CONF_DISCOVERY_PORT)) or BASIC_CONFIG.DISCOVERY_PORT
        
        self.server_config = ServerConfig(
            SERVER_NAME=ariacast_name,
            VERSION=BASIC_CONFIG.VERSION,
            PLATFORM=BASIC_CONFIG.PLATFORM,
            STREAMING_PORT=streaming_port,
            DISCOVERY_PORT=discovery_port,
        )
        
        # State
        self._active_player_id: str | None = None
        self._stop_called: bool = False
        self._server_task: asyncio.Task[None] | None = None
        self._discovery_task: asyncio.Task[None] | None = None
        self._audio_client: web.WebSocketResponse | None = None
        self._control_client: web.WebSocketResponse | None = None
        self._playback_started: bool = False
        
        # Audio buffer
        self.max_frames = 50  # 1 second buffer
        self.frame_queue: deque[bytes] = deque(maxlen=self.max_frames)
        self.received_frames = 0
        self.played_frames = 0
        
        # Metadata handler
        self.metadata_handler = MetadataHandler()
        self.metadata_clients: list[web.WebSocketResponse] = []  # Track metadata WebSocket clients
        
        # Artwork storage (like AirPlay)
        self._artwork_bytes: bytes | None = None
        self._artwork_timestamp: int = 0
        
        # Source details
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.name,
            # passive=False means this source will show up in the source list
            passive=not self._allow_player_switch,
            can_play_pause=True,
            can_seek=False,
            can_next_previous=True,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                codec_type=ContentType.PCM_S16LE,
                sample_rate=48000,
                bit_depth=16,
                channels=2,
            ),
            metadata=StreamMetadata(
                title=f"AriaCast | {ariacast_name}",
            ),
            stream_type=StreamType.CUSTOM,
        )
        self._source_details.on_select = self._on_source_selected
        self._source_details.on_play = self._cmd_play
        self._source_details.on_pause = self._cmd_pause
        self._source_details.on_next = self._cmd_next
        self._source_details.on_previous = self._cmd_previous

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Start AriaCast server
        self._server_task = self.mass.create_task(self._run_server())
        # Start UDP discovery
        self._discovery_task = self.mass.create_task(self._run_discovery())

    async def reload(self) -> None:
        """Handle reload of the provider configuration.
        
        Called when the user changes settings in Music Assistant.
        Note: Some changes (like server name) will only take effect after restart.
        """
        # Update configuration values
        new_name = cast("str", self.config.get_value(CONF_ARIACAST_NAME)) or BASIC_CONFIG.SERVER_NAME
        old_name = self.server_config.SERVER_NAME
        
        if new_name != old_name:
            self.logger.debug("Server name changed from '%s' to '%s'", old_name, new_name)
            # Update the config
            self.server_config.SERVER_NAME = new_name
            # Update source details
            self._source_details.metadata.title = f"AriaCast | {new_name}"
            # Trigger player update
            if self._source_details.in_use_by:
                self.mass.players.trigger_player_update(self._source_details.in_use_by)
        
        # Update player switching setting
        allow_switch_value = self.config.get_value(CONF_ALLOW_PLAYER_SWITCH)
        new_allow_switch = cast("bool", allow_switch_value) if allow_switch_value is not None else True
        if new_allow_switch != self._allow_player_switch:
            self._allow_player_switch = new_allow_switch
            self._source_details.passive = not new_allow_switch
        
        # Update default player
        new_player_id = cast("str", self.config.get_value(CONF_MASS_PLAYER_ID)) or PLAYER_ID_AUTO
        if new_player_id != self._default_player_id:
            self._default_player_id = new_player_id

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True
        
        # Close audio client if connected
        if self._audio_client and not self._audio_client.closed:
            await self._audio_client.close()
        
        # Stop server task
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._server_task
        
        # Stop discovery task
        if self._discovery_task and not self._discovery_task.done():
            self._discovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._discovery_task

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    @property
    def active_player_id(self) -> str | None:
        """Return the currently active player ID for this plugin."""
        return self._active_player_id

    def _get_target_player_id(self) -> str | None:
        """Determine the target player ID for playback."""
        # If there's an active player, use it
        if self._active_player_id:
            if self.mass.players.get(self._active_player_id):
                return self._active_player_id
            self._active_player_id = None

        # Handle auto selection
        if self._default_player_id == PLAYER_ID_AUTO:
            all_players = list(self.mass.players.all(False, False))
            # Try to find a playing player
            for player in all_players:
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.logger.debug("Auto-selecting playing player: %s", player.display_name)
                    return player.player_id
            # Fallback to first available player
            if all_players:
                first_player = all_players[0]
                self.logger.debug("Auto-selecting first available player: %s", first_player.display_name)
                return first_player.player_id
            return None

        # Use the specific default player if configured
        if self.mass.players.get(self._default_player_id):
            return self._default_player_id
        self.logger.warning("Configured default player '%s' no longer exists", self._default_player_id)
        return None

    async def _on_source_selected(self) -> None:
        """Handle callback when this source is selected on a player."""
        new_player_id = self._source_details.in_use_by
        if not new_player_id:
            return

        # Check if manual player switching is allowed
        if not self._allow_player_switch:
            current_target = self._get_target_player_id()
            if new_player_id != current_target:
                self.logger.debug("Manual player switching disabled, ignoring selection on %s", new_player_id)
                self._source_details.in_use_by = current_target
                self.mass.players.trigger_player_update(new_player_id)
                return

        # If there's already an active player, stop it
        if self._active_player_id and self._active_player_id != new_player_id:
            try:
                await self.mass.players.cmd_stop(self._active_player_id)
            except Exception as err:
                self.logger.debug("Failed to stop previous player %s: %s", self._active_player_id, err)

        # Update the active player
        self._active_player_id = new_player_id
        self.logger.debug("Active player set to: %s", new_player_id)

        # Persist selected player if not in auto mode
        if self._default_player_id != PLAYER_ID_AUTO:
            self._save_last_player_id(new_player_id)

    def _clear_active_player(self) -> None:
        """Clear the active player and revert to default."""
        prev_player_id = self._active_player_id
        self._active_player_id = None
        self._source_details.in_use_by = None

        if prev_player_id:
            self.logger.debug("Playback ended on player %s, clearing active player", prev_player_id)
            self.mass.players.trigger_player_update(prev_player_id)

    def _save_last_player_id(self, player_id: str) -> None:
        """Persist the selected player ID to config as the new default."""
        if self._default_player_id == player_id:
            return
        try:
            self.mass.config.set_raw_provider_config_value(
                self.instance_id, CONF_MASS_PLAYER_ID, player_id
            )
            self._default_player_id = player_id
        except Exception as err:
            self.logger.debug("Failed to persist player ID: %s", err)

    async def _run_discovery(self) -> None:
        """Run UDP discovery server."""
        try:
            transport, protocol = await asyncio.get_event_loop().create_datagram_endpoint(
                lambda: UDPDiscoveryProtocol(self.server_config, self.logger),
                local_addr=("0.0.0.0", self.server_config.DISCOVERY_PORT),
                allow_broadcast=True,  # Enable broadcast reception
            )
            self.logger.debug("UDP Discovery listening on port %s", self.server_config.DISCOVERY_PORT)
            
            # Keep the task running
            while not self._stop_called:
                await asyncio.sleep(1)
                
        except Exception as e:
            self.logger.error("UDP Discovery error: %s", e)
        finally:
            if 'transport' in locals():
                transport.close()

    async def _run_server(self) -> None:
        """Run the AriaCast WebSocket server."""
        app = web.Application()
        app.router.add_get('/audio', self.handle_audio_ws)
        app.router.add_get('/control', self.handle_control_ws)
        app.router.add_get('/metadata', self.handle_metadata_ws)
        app.router.add_post('/metadata', self.handle_metadata_api)
        app.router.add_get('/stats', self.handle_stats_ws)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.server_config.STREAMING_PORT)
        await site.start()
        
        self.logger.info("AriaCast Server listening on port %s", self.server_config.STREAMING_PORT)
        
        # Keep the server running
        try:
            while not self._stop_called:
                await asyncio.sleep(1)
        finally:
            await runner.cleanup()

    async def handle_audio_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /audio endpoint."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        if self._audio_client is not None:
            await ws.send_json({"error": "Another client is already connected"})
            await ws.close()
            return ws
        
        self._audio_client = ws
        self._playback_started = False
        self.frame_queue.clear()
        peer = request.remote
        self.logger.debug("Audio client connected: %s", peer)
        
        # Send handshake
        try:
            # Combine READY status with legacy handshake parameters
            # This satisfies both new (waiting for READY) and old (waiting for params) clients
            handshake = {
                "status": "READY",
                "type": "handshake",
                "sampleRate": self.server_config.AUDIO.SAMPLE_RATE,
                "channels": self.server_config.AUDIO.CHANNELS,
                "sampleWidth": self.server_config.AUDIO.SAMPLE_WIDTH,
                "frameSize": self.server_config.AUDIO.FRAME_SIZE,
            }
            await ws.send_json(handshake)
        except Exception as e:
            self.logger.warning("Failed to send handshake: %s", e)

        prebuffer = 15  # 15 frames minimal prebuffer (300ms)

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    # Receive audio frame
                    data = msg.data
                    if len(data) == self.server_config.AUDIO.FRAME_SIZE:
                        # Drop muted frames to avoid buffer buildup during silence
                        if not any(data):
                            continue

                        self.frame_queue.append(data)
                        self.received_frames += 1
                        
                        # Start playback after prebuffering
                        if not self._playback_started and len(self.frame_queue) >= prebuffer:
                            self._playback_started = True  # Prevent multiple calls
                            target_player_id = self._get_target_player_id()
                            if target_player_id:
                                self._active_player_id = target_player_id
                                # Use a task to not block the receiver loop
                                self.mass.create_task(self._start_playback(target_player_id))
                            else:
                                self.logger.warning("No player available for AriaCast playback")
                                self._playback_started = False

                                
                elif msg.type == web.WSMsgType.ERROR:
                    self.logger.error("WebSocket error: %s", ws.exception())
                    break
                    
        except Exception as e:
            self.logger.error("Error in audio handler: %s", e)
        finally:
            self.logger.debug("Audio client disconnected: %s", peer)
            self._audio_client = None
            self._playback_started = False
            
            # Clear active player
            current_player_id = self._source_details.in_use_by
            self._clear_active_player()
            
            # Deselect source from player
            if current_player_id:
                await self.mass.players.select_source(current_player_id, None)
        
        return ws

    async def _start_playback(self, player_id: str) -> None:
        """Start playback on the selected player."""
        try:
            # Set in_use_by BEFORE selecting source, to avoid race conditions 
            # where the player connects before select_source returns
            self._source_details.in_use_by = player_id
            self._active_player_id = player_id
            
            await self.mass.players.select_source(player_id, self.instance_id)
            # Ensure it is still set (in case select_source cleared it on error)
            self._source_details.in_use_by = player_id
        except Exception as e:
            self.logger.error("Failed to start playback on %s: %s", player_id, e)
            self._source_details.in_use_by = None
            self._active_player_id = None
            self._playback_started = False

    async def handle_metadata_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /metadata endpoint."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        peer = request.remote
        self.logger.debug("Metadata client connected: %s", peer)
        
        # Add to metadata clients list
        self.metadata_clients.append(ws)
        
        # Send current metadata
        try:
            current_metadata = self.metadata_handler.get()
            await ws.send_json({
                "type": "metadata",
                "data": current_metadata,
            })
        except Exception:
            pass
        
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("type") == "update":
                            metadata = data.get("data", {})
                            self.metadata_handler.update(metadata)
                            self._update_source_metadata(metadata)
                            # Broadcast to all other metadata clients
                            await self._broadcast_metadata(metadata, exclude_ws=ws)
                    except json.JSONDecodeError:
                        pass
                elif msg.type == web.WSMsgType.ERROR:
                    break
        except Exception as e:
            self.logger.debug("Error in metadata handler: %s", e)
        finally:
            # Remove from clients list
            if ws in self.metadata_clients:
                self.metadata_clients.remove(ws)
            self.logger.debug("Metadata client disconnected: %s", peer)
        
        return ws

    async def handle_metadata_api(self, request: web.Request) -> web.Response:
        """HTTP API handler for metadata updates (POST)."""
        try:
            text = await request.text()
            if not text:
                return web.Response(status=400, text="Empty payload")
            
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return web.Response(status=400, text="Invalid JSON")

            # Handle both wrapped (in "data") and unwrapped payload
            metadata = data.get("data") if isinstance(data, dict) else None
            if metadata is None:
                if isinstance(data, dict) and any(k in data for k in ["title", "artist", "album", "durationMs", "isPlaying", "positionMs"]):
                    metadata = data
                else:
                    metadata = {}
            
            if metadata:
                self.metadata_handler.update(metadata)
                self._update_source_metadata(metadata)
                await self._broadcast_metadata(metadata)
            
            return web.Response(status=200, text="OK")
        except Exception as e:
            self.logger.error("Error handling metadata API: %s", e, exc_info=True)
            return web.Response(status=500, text=str(e))

    async def handle_stats_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /stats endpoint."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        peer = request.remote
        self.logger.debug("Stats client connected: %s", peer)
        
        try:
            while not ws.closed and not self._stop_called:
                # Send current stats
                stats = {
                    "bufferedFrames": len(self.frame_queue),
                    "droppedFrames": 0,
                    "receivedFrames": self.received_frames,
                }
                try:
                    await ws.send_json(stats)
                except Exception:
                    break
                await asyncio.sleep(1)  # Update every second
        except Exception as e:
            self.logger.debug("Error in stats handler: %s", e)
        finally:
            self.logger.debug("Stats client disconnected: %s", peer)
        
        return ws

    async def handle_control_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket handler for /control endpoint."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        self._control_client = ws
        peer = request.remote
        self.logger.debug("Control client connected: %s", peer)
        
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.ERROR:
                    self.logger.error("Control WebSocket error: %s", ws.exception())
                    break
        except Exception as e:
            self.logger.error("Error in control handler: %s", e)
        finally:
            self.logger.debug("Control client disconnected: %s", peer)
            if self._control_client == ws:
                self._control_client = None
        
        return ws

    async def _send_control_command(self, action: str) -> None:
        """Send a control command to the connected client."""
        if not self._control_client or self._control_client.closed:
            self.logger.debug("Cannot send command '%s': No control client connected", action)
            return
            
        try:
            command = {"action": action}
            await self._control_client.send_json(command)
        except Exception as e:
            self.logger.error("Failed to send control command '%s': %s", action, e)

    async def _cmd_play(self) -> None:
        """Handle Play command."""
        await self._send_control_command("play")

    async def _cmd_pause(self) -> None:
        """Handle Pause command."""
        await self._send_control_command("pause")

    async def _cmd_next(self) -> None:
        """Handle Next command."""
        await self._send_control_command("next")

    async def _cmd_previous(self) -> None:
        """Handle Previous command."""
        await self._send_control_command("previous")

    async def _broadcast_metadata(self, metadata: dict[str, Any], exclude_ws: web.WebSocketResponse | None = None) -> None:
        """Broadcast metadata update to all connected WebSocket clients.
        
        Args:
            metadata: Metadata dictionary to broadcast
            exclude_ws: Optional WebSocket to exclude from broadcast (e.g., the sender)
        """
        if not metadata:
            return
        
        # Prepare message
        message = {
            "type": "update",
            "data": metadata,
        }
        
        # Broadcast to all metadata clients
        for client in self.metadata_clients[:]:  # Copy list to avoid modification during iteration
            if client == exclude_ws:
                continue
            try:
                await client.send_json(message)
            except Exception as e:
                self.logger.debug("Failed to send metadata to client: %s", e)
                # Remove dead clients
                if client in self.metadata_clients:
                    self.metadata_clients.remove(client)

    def _update_source_metadata(self, metadata: dict[str, Any]) -> None:
        """Update source metadata from received metadata.
        
        Supports metadata fields:
        - title: Track title
        - artist: Artist name
        - album: Album name
        - artwork_url: Album artwork URL (will be downloaded and proxied)
        - duration_ms: Track duration in milliseconds
        - position_ms: Current playback position in milliseconds
        - is_playing: Playback state
        """
        if self._source_details.metadata is None:
            ariacast_name = cast("str", self.config.get_value(CONF_ARIACAST_NAME)) or self.name
            self._source_details.metadata = StreamMetadata(title=f"AriaCast | {ariacast_name}")

        has_changes = False

        # Update title
        if "title" in metadata and metadata["title"] is not None:
            new_title = str(metadata["title"])
            if self._source_details.metadata.title != new_title:
                self._source_details.metadata.title = new_title
                has_changes = True

        # Update artist
        if "artist" in metadata and metadata["artist"] is not None:
            new_artist = str(metadata["artist"])
            if self._source_details.metadata.artist != new_artist:
                self._source_details.metadata.artist = new_artist
                has_changes = True

        # Update album
        if "album" in metadata and metadata["album"] is not None:
            new_album = str(metadata["album"])
            if self._source_details.metadata.album != new_album:
                self._source_details.metadata.album = new_album
                has_changes = True

        # Update artwork - download and store like AirPlay does
        # Handle both artwork_url (snake_case) and artworkUrl (camelCase)
        artwork_url = metadata.get("artwork_url") or metadata.get("artworkUrl")
        if artwork_url and isinstance(artwork_url, str) and artwork_url.startswith("http"):
            # Schedule download in background
            self.mass.create_task(self._download_artwork(artwork_url))


        # Update duration
        duration = metadata.get("duration_ms") or metadata.get("durationMs")
        if duration is not None:
            try:
                new_duration = int(duration) / 1000
                if self._source_details.metadata.duration != new_duration:
                    self._source_details.metadata.duration = new_duration
                    has_changes = True
            except (ValueError, TypeError):
                pass

        # Update position/elapsed time
        position = metadata.get("position_ms") or metadata.get("positionMs")
        if position is not None:
            try:
                new_position = int(position) / 1000
                # Always update position as it changes constantly
                self._source_details.metadata.elapsed_time = new_position
                self._source_details.metadata.elapsed_time_last_updated = time.time()
                has_changes = True
            except (ValueError, TypeError):
                pass

        # Trigger update on connected player
        if has_changes:
            if self._source_details.in_use_by:
                self.mass.players.trigger_player_update(self._source_details.in_use_by)
            elif self._active_player_id:
                self.mass.players.trigger_player_update(self._active_player_id)

    async def _download_artwork(self, artwork_url: str) -> None:
        """Download artwork from URL and update metadata with proxy URL.
        
        Args:
            artwork_url: URL of the artwork image to download
        """
        try:
            
            # Download the image
            async with self.mass.http_session.get(artwork_url, timeout=10) as response:
                if response.status == 200:
                    self._artwork_bytes = await response.read()
                    self._artwork_timestamp = int(time.time() * 1000)
                    
                    # Create proxy URL using Music Assistant's image system
                    image = MediaItemImage(
                        type=ImageType.THUMB,
                        path="artwork",  # Path identifier for resolve_image()
                        provider=self.instance_id,
                        remotely_accessible=False,
                    )
                    base_url = self.mass.metadata.get_image_url(image)
                    # Add timestamp for cache-busting
                    self._source_details.metadata.image_url = f"{base_url}&t={self._artwork_timestamp}"
                    
                    # Trigger player update to show new artwork
                    if self._source_details.in_use_by:
                        self.mass.players.trigger_player_update(self._source_details.in_use_by)
        except Exception:
            pass

    async def resolve_image(self, path: str) -> bytes:
        """Resolve an image from an image path.

        This returns raw bytes of the artwork image downloaded from the AriaCast client.

        Args:
            path: The image path (should be "artwork" for AriaCast cover art).
            
        Returns:
            Raw image bytes, or empty bytes if no artwork is available.
        """
        if path == "artwork" and self._artwork_bytes:
            return self._artwork_bytes
        # Return empty bytes if no artwork is available
        return b""

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Return the custom audio stream for this source.
        
        This is called by the player controller to get the PCM audio stream.
        Audio frames are pulled from the queue which is populated by the WebSocket client.
        """
        self.logger.debug("Audio stream requested by player %s", player_id)
        

        
        # Stream audio frames from the queue until playback stops
        try:
            # Check stopping condition: stop called OR (in_use_by matches AND player matches active)
            # We relax the in_use_by check to avoid race condition dropout
            while not self._stop_called:
                # If active player changed to something else, stop
                if self._active_player_id and self._active_player_id != player_id:
                     break
                     
                if self.frame_queue:
                    frame = self.frame_queue.popleft()
                    self.played_frames += 1
                    yield frame
                else:
                    # No data available, wait a bit to avoid busy loop
                    await asyncio.sleep(0.005)
        finally:
            self.logger.debug("Audio stream ended for player %s", player_id)
            self._playback_started = False
            self.frame_queue.clear()


class UDPDiscoveryProtocol(asyncio.DatagramProtocol):
    """UDP discovery protocol handler."""

    def __init__(self, config: ServerConfig, logger) -> None:
        """Initialize the protocol."""
        self.config = config
        self.logger = logger
        self.transport = None

    def connection_made(self, transport):
        """Handle connection made."""
        self.transport = transport
        # Enable socket reuse and broadcast
        sock = transport.get_extra_info('socket')
        if sock:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except Exception as e:
                self.logger.debug("Failed to set socket options: %s", e)

    def datagram_received(self, data: bytes, addr):
        """Handle discovery request."""
        try:
            message = data.decode("utf-8").strip()
            if message == "DISCOVER_AUDIOCAST":
                local_ip = self._get_local_ip()
                response = {
                    "server_name": self.config.SERVER_NAME,
                    "ip": local_ip,
                    "port": self.config.STREAMING_PORT,
                    "samplerate": self.config.AUDIO.SAMPLE_RATE,
                    "channels": self.config.AUDIO.CHANNELS,
                }
                self.transport.sendto(json.dumps(response).encode(), addr)
                self.logger.debug("Sent discovery response to %s", addr)
        except Exception as e:
            self.logger.debug("Error handling discovery request: %s", e)
    
    @staticmethod
    def _get_local_ip() -> str:
        """Get local IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"
