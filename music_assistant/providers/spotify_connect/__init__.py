"""
Spotify Connect plugin for Music Assistant.

We tie a single player to a single Spotify Connect daemon.
The provider has multi instance support,
so multiple players can be linked to multiple Spotify Connect daemons.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiohttp.web import Response
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    PlaybackState,
    ProviderFeature,
    ProviderType,
    StreamType,
)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.models.plugin import PluginProvider, PluginSource
from music_assistant.providers.spotify.helpers import get_librespot_binary

if TYPE_CHECKING:
    from aiohttp.web import Request
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.spotify.provider import SpotifyProvider

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_HANDOFF_MODE = "handoff_mode"
CONNECT_ITEM_ID = "spotify_connect"
CONF_PUBLISH_NAME = "publish_name"

EVENTS_SCRIPT = pathlib.Path(__file__).parent.resolve().joinpath("events.py")

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SpotifyConnectProvider(mass, manifest, config)


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
            description="Select the player for which you want to enable Spotify Connect.",
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
            key=CONF_PUBLISH_NAME,
            type=ConfigEntryType.STRING,
            label="Name to display in the Spotify app",
            description="How should this Spotify Connect device be named in the Spotify app?",
            default_value="Music Assistant",
        ),
        # ConfigEntry(
        #     key=CONF_HANDOFF_MODE,
        #     type=ConfigEntryType.BOOLEAN,
        #     label="Enable handoff mode",
        #     default_value=False,
        #     description="The default behavior of the Spotify Connect plugin is to "
        #     "forward the actual Spotify Connect audio stream as-is to the player. "
        #     "The Spotify audio is basically just a live audio stream. \n\n"
        #     "For controlling the playback (and queue contents), "
        #     "you need to use the Spotify app. Also, depending on the player's "
        #     "buffering strategy and capabilities, the audio may not be fully in sync with "
        #     "what is shown in the Spotify app. \n\n"
        #     "When enabling handoff mode, the Spotify Connect plugin will instead "
        #     "forward the Spotify playback request to the Music Assistant Queue, so basically "
        #     "the spotify app can be used to initiate playback, but then MA will take over "
        #     "the playback and manage the queue, which is the normal operating mode of MA. \n\n"
        #     "This mode however means that the Spotify app will not report the actual playback ",
        #     required=False,
        # ),
    )


class SpotifyConnectProvider(PluginProvider):
    """Implementation of a Spotify Connect Plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self.mass_player_id = cast("str", self.config.get_value(CONF_MASS_PLAYER_ID))
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self._librespot_bin: str | None = None
        self._stop_called: bool = False
        self._runner_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._librespot_proc: AsyncProcess | None = None
        self._librespot_started = asyncio.Event()
        self.named_pipe = f"/tmp/{self.instance_id}"  # noqa: S108
        connect_name = cast("str", self.config.get_value(CONF_PUBLISH_NAME)) or self.name
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.name,
            # we set passive to true because we
            # dont allow this source to be selected directly
            passive=True,
            # Playback control capabilities will be enabled when Spotify Web API is available
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                codec_type=ContentType.PCM_S16LE,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
            metadata=StreamMetadata(
                title=f"Spotify Connect | {connect_name}",
            ),
            stream_type=StreamType.NAMED_PIPE,
            path=self.named_pipe,
        )
        self._audio_buffer: asyncio.Queue[bytes] = asyncio.Queue(10)
        # Web API integration for playback control
        self._connected_spotify_username: str | None = None
        self._spotify_provider: SpotifyProvider | None = None
        self._on_unload_callbacks: list[Callable[..., None]] = []
        self._runner_error_count = 0
        self._spotify_device_id: str | None = None
        self._last_session_connected_time: float = 0

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._librespot_bin = await get_librespot_binary()
        if self.mass.players.get(self.mass_player_id):
            self._setup_player_daemon()

        # Subscribe to events
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_mass_player_event,
                (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED),
                id_filter=self.mass_player_id,
            )
        )
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_provider_event,
                (EventType.PROVIDERS_UPDATED),
            )
        )
        self._on_unload_callbacks.append(
            self.mass.streams.register_dynamic_route(
                f"/{self.instance_id}",
                self._handle_custom_webservice,
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task
        for callback in self._on_unload_callbacks:
            callback()

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    async def _check_spotify_provider_match(self) -> None:
        """Check if a Spotify music provider is available with matching username."""
        # Username must be available (set from librespot output)
        if not self._connected_spotify_username:
            return

        # Look for a Spotify music provider with matching username
        for provider in self.mass.get_providers():
            if provider.domain == "spotify" and provider.type == ProviderType.MUSIC:
                # Check if the username matches
                if hasattr(provider, "_sp_user") and provider._sp_user:
                    spotify_username = provider._sp_user.get("id")
                    if spotify_username == self._connected_spotify_username:
                        self.logger.debug(
                            "Found matching Spotify music provider - "
                            "enabling playback control via Web API"
                        )
                        self._spotify_provider = cast("SpotifyProvider", provider)
                        self._update_source_capabilities()
                        return

        # No matching provider found
        if self._spotify_provider is not None:
            self.logger.debug(
                "Spotify music provider no longer available - disabling playback control"
            )
            self._spotify_provider = None
            self._update_source_capabilities()

    def _update_source_capabilities(self) -> None:
        """Update source capabilities based on Web API availability."""
        has_web_api = self._spotify_provider is not None
        self._source_details.can_play_pause = has_web_api
        self._source_details.can_seek = has_web_api
        self._source_details.can_next_previous = has_web_api

        # Register or unregister callbacks based on availability
        if has_web_api:
            self._source_details.on_play = self._on_play
            self._source_details.on_pause = self._on_pause
            self._source_details.on_next = self._on_next
            self._source_details.on_previous = self._on_previous
            self._source_details.on_seek = self._on_seek
        else:
            self._source_details.on_play = None
            self._source_details.on_pause = None
            self._source_details.on_next = None
            self._source_details.on_previous = None
            self._source_details.on_seek = None

        # Trigger player update to reflect capability changes
        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(self._source_details.in_use_by)

    async def _on_play(self) -> None:
        """Handle play command via Spotify Web API."""
        attached_player = self.mass.players.get(self.mass_player_id)
        if attached_player and attached_player.playback_state == PlaybackState.IDLE:
            # edge case: player is not paused, so we need to select this source first
            await self.mass.players.select_source(self.mass_player_id, self.instance_id)
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            # First try to transfer playback to this device if needed
            await self._ensure_active_device()
            await self._spotify_provider._put_data("me/player/play")
        except Exception as err:
            self.logger.warning("Failed to send play command via Spotify Web API: %s", err)
            raise

    async def _on_pause(self) -> None:
        """Handle pause command via Spotify Web API."""
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            await self._spotify_provider._put_data("me/player/pause")
        except Exception as err:
            self.logger.warning("Failed to send pause command via Spotify Web API: %s", err)
            raise

    async def _on_next(self) -> None:
        """Handle next track command via Spotify Web API."""
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            await self._spotify_provider._post_data("me/player/next", want_result=False)
        except Exception as err:
            self.logger.warning("Failed to send next track command via Spotify Web API: %s", err)
            raise

    async def _on_previous(self) -> None:
        """Handle previous track command via Spotify Web API."""
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            await self._spotify_provider._post_data("me/player/previous")
        except Exception as err:
            self.logger.warning("Failed to send previous command via Spotify Web API: %s", err)
            raise

    async def _on_seek(self, position: int) -> None:
        """Handle seek command via Spotify Web API."""
        if not self._spotify_provider:
            raise UnsupportedFeaturedException(
                "Playback control requires a matching Spotify music provider"
            )
        try:
            # Spotify Web API expects position in milliseconds
            position_ms = position * 1000
            await self._spotify_provider._put_data(f"me/player/seek?position_ms={position_ms}")
        except Exception as err:
            self.logger.warning("Failed to send seek command via Spotify Web API: %s", err)
            raise

    async def _get_spotify_device_id(self) -> str | None:
        """Get the Spotify Connect device ID for this instance.

        :return: Device ID if found, None otherwise.
        """
        if not self._spotify_provider:
            return None

        try:
            # Get list of available devices from Spotify Web API
            devices_data = await self._spotify_provider._get_data("me/player/devices")
            devices = devices_data.get("devices", [])

            # Look for our device by name
            connect_name = cast("str", self.config.get_value(CONF_PUBLISH_NAME)) or self.name
            for device in devices:
                if device.get("name") == connect_name and device.get("type") == "Speaker":
                    device_id: str | None = device.get("id")
                    self.logger.debug("Found Spotify Connect device ID: %s", device_id)
                    return device_id

            self.logger.debug(
                "Could not find Spotify Connect device '%s' in available devices", connect_name
            )
            return None
        except Exception as err:
            self.logger.debug("Failed to get Spotify devices: %s", err)
            return None

    async def _ensure_active_device(self) -> None:
        """
        Ensure this Spotify Connect device is the active player on Spotify.

        Transfers playback to this device if it's not already active.
        """
        if not self._spotify_provider:
            return

        try:
            # Get current playback state
            try:
                playback_data = await self._spotify_provider._get_data("me/player")
                current_device = playback_data.get("device", {}) if playback_data else {}
                current_device_id = current_device.get("id")
            except Exception as err:
                if getattr(err, "status", None) == 204:
                    # No active device
                    current_device_id = None
                else:
                    raise

            # Get our device ID if we don't have it cached
            if not self._spotify_device_id:
                self._spotify_device_id = await self._get_spotify_device_id()

            # If we couldn't find our device ID, we can't transfer
            if not self._spotify_device_id:
                self.logger.debug("Cannot transfer playback - device ID not found")
                return

            # Check if we're already the active device
            if current_device_id == self._spotify_device_id:
                self.logger.debug("Already the active Spotify device")
                return

            # Transfer playback to this device
            self.logger.info("Transferring Spotify playback to this device")
            await self._spotify_provider._put_data(
                "me/player",
                data={"device_ids": [self._spotify_device_id], "play": False},
            )
        except Exception as err:
            self.logger.debug("Failed to ensure active device: %s", err)
            # Don't raise - this is a best-effort operation

    def _on_provider_event(self, event: MassEvent) -> None:
        """Handle provider added/removed events to check for Spotify provider."""
        # Re-check for matching Spotify provider when providers change
        if self._connected_spotify_username:
            self.mass.create_task(self._check_spotify_provider_match())

    def _process_librespot_stderr_line(self, line: str) -> None:
        """
        Process a single line from librespot stderr output.

        :param line: A line from librespot's stderr output.
        """
        if (
            not self._librespot_started.is_set()
            and "Using StdoutSink (pipe) with format: S16" in line
        ):
            self._librespot_started.set()
        if "error sending packet Os" in line:
            return
        if "dropping truncated packet" in line:
            return
        if "couldn't parse packet from " in line:
            return
        if "Authenticated as '" in line:
            # Extract username from librespot authentication message
            # Format: "Authenticated as 'username'"
            try:
                parts = line.split("Authenticated as '")
                if len(parts) > 1:
                    username_part = parts[1].split("'")
                    if len(username_part) > 0 and username_part[0]:
                        username = username_part[0]
                        self._connected_spotify_username = username
                        self.logger.debug("Authenticated to Spotify as: %s", username)
                        # Check for provider match now that we have the username
                        self.mass.create_task(self._check_spotify_provider_match())
                    else:
                        self.logger.warning("Could not parse Spotify username from line: %s", line)
                else:
                    self.logger.warning("Could not parse Spotify username from line: %s", line)
            except Exception as err:
                self.logger.warning("Error parsing Spotify username from line: %s - %s", line, err)
            return
        self.logger.debug(line)

    async def _librespot_runner(self) -> None:
        """Run the spotify connect daemon in a background task."""
        assert self._librespot_bin
        self.logger.info("Starting Spotify Connect background daemon")
        os.environ["MASS_CALLBACK"] = f"{self.mass.streams.base_url}/{self.instance_id}"
        await check_output("rm", "-f", self.named_pipe)
        await asyncio.sleep(0.1)
        await check_output("mkfifo", self.named_pipe)
        await asyncio.sleep(0.1)
        try:
            # Get initial volume from player, or use 20 as fallback
            initial_volume = 20
            _player = self.mass.players.get(self.mass_player_id)
            if _player and _player.volume_level:
                initial_volume = _player.volume_level

            args: list[str] = [
                self._librespot_bin,
                "--name",
                cast("str", self.config.get_value(CONF_PUBLISH_NAME)) or self.name,
                "--cache",
                self.cache_dir,
                "--disable-audio-cache",
                "--bitrate",
                "320",
                "--backend",
                "pipe",
                "--device",
                self.named_pipe,
                "--dither",
                "none",
                # disable volume control
                "--mixer",
                "softvol",
                "--volume-ctrl",
                "fixed",
                "--initial-volume",
                str(initial_volume),
                "--enable-volume-normalisation",
                # forward events to the events script
                "--onevent",
                str(EVENTS_SCRIPT),
                "--emit-sink-events",
            ]
            self._librespot_proc = librespot = AsyncProcess(
                args, stdout=False, stderr=True, name=f"librespot[{self.name}]"
            )
            await librespot.start()

            # keep reading logging from stderr until exit
            async for line in librespot.iter_stderr():
                self._process_librespot_stderr_line(line)
        finally:
            await librespot.close()
            self.logger.info("Spotify Connect background daemon stopped for %s", self.name)
            await check_output("rm", "-f", self.named_pipe)
            if not self._librespot_started.is_set():
                self.unload_with_error("Unable to initialize librespot daemon.")
            # auto restart if not stopped manually
            elif not self._stop_called and self._runner_error_count >= 5:
                self.unload_with_error("Librespot daemon failed to start multiple times.")
            elif not self._stop_called:
                self._runner_error_count += 1
                self.mass.call_later(2, self._setup_player_daemon)

    def _setup_player_daemon(self) -> None:
        """Handle setup of the spotify connect daemon for a player."""
        self._librespot_started.clear()
        self._runner_task = self.mass.create_task(self._librespot_runner())

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

    async def _handle_custom_webservice(self, request: Request) -> Response:  # noqa: PLR0915
        """Handle incoming requests on the custom webservice."""
        json_data = await request.json()
        self.logger.debug("Received metadata on webservice: \n%s", json_data)

        event_name = json_data.get("event")

        # handle session connected event
        # extract the connected username and check for matching Spotify provider
        if event_name == "session_connected":
            # Track when session connected for volume event filtering
            self._last_session_connected_time = time.time()
            username = json_data.get("user_name")
            self.logger.debug(
                "Session connected event - username from event: %s, current username: %s",
                username,
                self._connected_spotify_username,
            )
            if username and username != self._connected_spotify_username:
                self.logger.info("Spotify Connect session connected for user: %s", username)
                self._connected_spotify_username = username
                await self._check_spotify_provider_match()
            elif not username:
                self.logger.warning("Session connected event received but no username in payload")

        # handle session disconnected event
        if event_name == "session_disconnected":
            self.logger.info("Spotify Connect session disconnected")
            self._connected_spotify_username = None
            if self._spotify_provider is not None:
                self._spotify_provider = None
                self._update_source_capabilities()

        # handle paused event - clear in_use_by so UI shows correct active source
        # this happens when MA starts playing while Spotify Connect was active
        if event_name == "paused" and self._source_details.in_use_by:
            self.logger.debug("Spotify Connect paused, releasing player %s", self.mass_player_id)
            self._source_details.in_use_by = None
            self.mass.players.trigger_player_update(self.mass_player_id)

        # handle session connected event
        # this player has become the active spotify connect player
        # we need to start the playback
        if event_name in ("sink", "playing") and (not self._source_details.in_use_by):
            # Check for matching Spotify provider now that playback is starting
            # This ensures the Spotify music provider has had time to initialize
            if not self._connected_spotify_username or not self._spotify_provider:
                await self._check_spotify_provider_match()

            # Make this device the active Spotify player via Web API
            if self._spotify_provider:
                self.mass.create_task(self._ensure_active_device())

            # initiate playback by selecting this source on the default player
            self.mass.create_task(
                self.mass.players.select_source(self.mass_player_id, self.instance_id)
            )
            self._source_details.in_use_by = self.mass_player_id

        # parse metadata fields
        if common_meta := json_data.get("common_metadata_fields", {}):
            uri = common_meta.get("uri", "Unknown")
            title = common_meta.get("name", "Unknown")
            image_url = images[0] if (images := common_meta.get("covers")) else None
            if self._source_details.metadata is None:
                self._source_details.metadata = StreamMetadata(uri=uri, title=title)
            self._source_details.metadata.uri = uri
            self._source_details.metadata.title = title
            self._source_details.metadata.artist = None
            self._source_details.metadata.album = None
            self._source_details.metadata.image_url = image_url
            self._source_details.metadata.description = None
            duration_ms = common_meta.get("duration_ms", 0)
            self._source_details.metadata.duration = (
                int(duration_ms) // 1000 if duration_ms is not None else None
            )

        if track_meta := json_data.get("track_metadata_fields", {}):
            if artists := track_meta.get("artists"):
                if self._source_details.metadata is not None:
                    self._source_details.metadata.artist = artists[0]
            if self._source_details.metadata is not None:
                self._source_details.metadata.album = track_meta.get("album")

        if episode_meta := json_data.get("episode_metadata_fields", {}):
            if self._source_details.metadata is not None:
                self._source_details.metadata.description = episode_meta.get("description")

        if "position_ms" in json_data:
            if self._source_details.metadata is not None:
                self._source_details.metadata.elapsed_time = int(json_data["position_ms"]) // 1000
                self._source_details.metadata.elapsed_time_last_updated = int(time.time())

        if event_name == "volume_changed" and (volume := json_data.get("volume")):
            # Ignore volume_changed events that fire immediately after session_connect
            # We want to use the volume from MA in that case
            time_since_connect = time.time() - self._last_session_connected_time
            if time_since_connect < 2.0:
                self.logger.debug(
                    "Ignoring initial volume_changed event (%.2fs after session_connect)",
                    time_since_connect,
                )
            else:
                # Spotify Connect volume is 0-65535
                volume = int(int(volume) / 65535 * 100)
                try:
                    await self.mass.players.cmd_volume_set(self.mass_player_id, volume)
                except UnsupportedFeaturedException:
                    self.logger.debug(
                        "Player %s does not support volume control", self.mass_player_id
                    )

        # signal update to connected player
        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(self._source_details.in_use_by)

        return Response()
