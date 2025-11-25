"""
Plex Connect plugin for Music Assistant.

This plugin allows Music Assistant players to appear as controllable devices
in the official Plex apps (Plexamp, web player, etc.). Each plugin instance
links a single MA player to Plex, making it available for remote control.

Multiple instances can be created to expose multiple MA players to Plex.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderFeature

from music_assistant.models.plugin import PluginProvider

from .player_remote import PlayerRemoteInstance

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.plex import PlexProvider

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_PLEX_PROVIDER_ID = "plex_provider_id"
CONF_PLAYER_NAME = "player_name"
CONF_DEVICE_CLASS = "device_class"

# No special features needed for this plugin
SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PlexConnectProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    # Get available Plex music providers
    plex_providers = [
        provider
        for provider in mass.get_providers()
        if provider.domain == "plex" and provider.type.value == "music"
    ]

    # Get player name default if player is selected
    player_name_default = None
    if values and values.get(CONF_MASS_PLAYER_ID):
        player_id = str(values.get(CONF_MASS_PLAYER_ID))
        if player := mass.players.get(player_id):
            player_name_default = player.display_name

    return (
        ConfigEntry(
            key=CONF_PLEX_PROVIDER_ID,
            type=ConfigEntryType.STRING,
            label="Plex Music Provider",
            description="Select the Plex music provider to use for this connection.",
            required=True,
            options=[
                ConfigValueOption(provider.name, provider.instance_id)
                for provider in plex_providers
            ],
        ),
        ConfigEntry(
            key=CONF_MASS_PLAYER_ID,
            type=ConfigEntryType.STRING,
            label="Music Assistant Player",
            description="Select the MA player to advertise as a Plex remote client.",
            required=True,
            options=[
                ConfigValueOption(x.display_name, x.player_id)
                for x in sorted(
                    mass.players.all(False, False), key=lambda p: p.display_name.lower()
                )
            ],
        ),
        ConfigEntry(
            key=CONF_PLAYER_NAME,
            type=ConfigEntryType.STRING,
            label="Player Name in Plex",
            description=(
                "Custom name for this player as it appears in Plex apps. "
                "Leave empty to use the player's name."
            ),
            required=False,
            default_value=player_name_default,
        ),
        ConfigEntry(
            key=CONF_DEVICE_CLASS,
            type=ConfigEntryType.STRING,
            label="Device Class",
            description="How this player appears in Plex apps.",
            required=False,
            default_value="speaker",
            options=[
                ConfigValueOption("Speaker", "speaker"),
                ConfigValueOption("Phone", "phone"),
                ConfigValueOption("Tablet", "tablet"),
                ConfigValueOption("Set-Top Box", "stb"),
                ConfigValueOption("TV", "tv"),
                ConfigValueOption("PC", "pc"),
                ConfigValueOption("Cloud", "cloud"),
            ],
        ),
    )


class PlexConnectProvider(PluginProvider):
    """Plex Connect plugin provider implementation."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the plugin provider.

        :param mass: MusicAssistant instance.
        :param manifest: Provider manifest.
        :param config: Provider configuration.
        """
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self.mass_player_id = cast("str", self.config.get_value(CONF_MASS_PLAYER_ID))
        self.plex_provider_id = cast("str", self.config.get_value(CONF_PLEX_PROVIDER_ID))
        self.custom_player_name = cast("str | None", self.config.get_value(CONF_PLAYER_NAME))
        self.device_class = cast("str", self.config.get_value(CONF_DEVICE_CLASS)) or "speaker"

        self._plex_provider: PlexProvider | None = None
        self._player_instance: PlayerRemoteInstance | None = None
        self._allocated_port: int | None = None
        self._stop_called: bool = False
        self._on_unload_callbacks: list[Callable[..., None]] = []

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Wait for the Plex provider to be available (with timeout)
        max_retries = 30  # 15 seconds total
        retry_delay = 0.5
        for attempt in range(max_retries):
            self._plex_provider = self.mass.get_provider(self.plex_provider_id)  # type: ignore[assignment]
            if self._plex_provider:
                break
            if attempt == 0:
                self.logger.info(
                    f"Waiting for Plex provider {self.plex_provider_id} to become available..."
                )
            await asyncio.sleep(retry_delay)
        else:
            timeout_seconds = max_retries * retry_delay
            self.logger.error(
                f"Plex provider {self.plex_provider_id} not found after {timeout_seconds}s"
            )
            return

        self.logger.debug(f"Plex provider {self.plex_provider_id} is ready")

        # Subscribe to player events first
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_mass_player_event,
                (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED),
                id_filter=self.mass_player_id,
            )
        )

        # Now try to setup the player instance
        player = self.mass.players.get(self.mass_player_id)
        if not player:
            self.logger.info(
                f"Player {self.mass_player_id} not found yet, waiting for PLAYER_ADDED event"
            )
        else:
            # Setup the player instance immediately
            await self._setup_player_instance()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider.

        :param is_removed: Whether the provider is being removed.
        """
        self._stop_called = True

        # Stop player instance
        if self._player_instance:
            await self._player_instance.stop()
            self._player_instance = None

        # Unsubscribe from events
        for callback in self._on_unload_callbacks:
            callback()
        self._on_unload_callbacks.clear()

    def _is_port_available(self, port: int) -> bool:
        """Check if a port is available by attempting to bind to it.

        :param port: Port number to check.
        :return: True if port is available, False otherwise.
        """
        try:
            # Try to bind to the port on all interfaces
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", port))
                return True
        except OSError:
            return False

    def _find_available_port(self) -> int:
        """Find the first available port starting from 32500.

        :return: First available port number.
        """
        port = 32500
        max_attempts = 100  # Prevent infinite loop
        attempts = 0

        while attempts < max_attempts:
            if self._is_port_available(port):
                return port
            port += 1
            attempts += 1

        # Fallback - should rarely happen
        msg = f"Could not find available port in range 32500-{32500 + max_attempts}"
        raise RuntimeError(msg)

    async def _setup_player_instance(self) -> None:
        """Set up the Plex remote control instance for the player."""
        # Don't create duplicate instances
        if self._player_instance:
            self.logger.debug("Player instance already exists, skipping setup")
            return

        if not self._plex_provider:
            self.logger.error("Cannot setup player instance: Plex provider not available")
            return

        player = self.mass.players.get(self.mass_player_id)
        if not player:
            self.logger.warning(f"Player {self.mass_player_id} not found")
            return

        # Allocate a port if we haven't already
        if not self._allocated_port:
            self._allocated_port = self._find_available_port()

        # Use custom name if provided, otherwise use player's display name
        player_name = self.custom_player_name or player.display_name

        # Create remote control instance
        self._player_instance = PlayerRemoteInstance(
            plex_provider=self._plex_provider,
            ma_player_id=self.mass_player_id,
            player_name=player_name,
            port=self._allocated_port,
            device_class=self.device_class,
            remote_control=True,
        )

        try:
            await self._player_instance.start()
            self.logger.info(
                f"Plex Connect ready: '{player_name}' is now available in Plex apps "
                f"on port {self._allocated_port}"
            )
        except Exception as e:
            self.logger.exception(f"Failed to start Plex remote control: {e}")
            self._player_instance = None

    async def _teardown_player_instance(self) -> None:
        """Tear down the Plex remote control instance."""
        if self._player_instance:
            await self._player_instance.stop()
            self._player_instance = None

    def _on_mass_player_event(self, event: MassEvent) -> None:
        """Handle player added/removed events.

        :param event: The event that occurred.
        """
        if event.object_id != self.mass_player_id:
            return

        if event.event == EventType.PLAYER_REMOVED:
            # Player was removed - stop the instance
            self.logger.info(f"Player {self.mass_player_id} removed, stopping Plex Connect")
            self.mass.create_task(self._teardown_player_instance())

        elif event.event == EventType.PLAYER_ADDED:
            # Player was added - start the instance (if not already running)
            if not self._player_instance:
                self.logger.info(f"Player {self.mass_player_id} added, starting Plex Connect")
                self.mass.create_task(self._setup_player_instance())
