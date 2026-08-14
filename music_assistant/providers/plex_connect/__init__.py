"""
Plex Connect plugin for Music Assistant.

This plugin allows Music Assistant players to appear as controllable devices
in the official Plex apps (Plexamp, web player, etc.). Each plugin instance
links a single MA player to Plex, making it available for remote control.

Multiple instances can be created to expose multiple MA players to Plex.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import aiohttp
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderFeature
from music_assistant_models.errors import ActionUnavailable

from music_assistant.helpers.util import is_port_in_use, select_free_port
from music_assistant.models.plugin import PluginProvider

from .plextv import (
    PlexTvAuthError,
    PlexTvClient,
    PlexTvError,
    PlexTvIdentity,
    PlexTvPinExpiredError,
    build_version,
    compute_client_id,
)
from .server import PlayerRemoteInstance

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.plex import PlexProvider

    from .plextv import PlexPin

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_PLEX_PROVIDER_ID = "plex_provider_id"
CONF_PLAYER_NAME = "player_name"
CONF_DEVICE_CLASS = "device_class"
CONF_PORT = "port"
# plex.tv device token, stored in setup_data (encrypted) once the player is linked
CONF_PLEXTV_TOKEN = "plextv_token"
CONF_ACTION_START_LINK = "start_link"
CONF_ACTION_COMPLETE_LINK = "complete_link"
CONF_ACTION_UNLINK = "unlink"

# Bounded poll while the user confirms the link code at plex.tv/link.
PIN_CHECK_ATTEMPTS = 5
PIN_CHECK_INTERVAL = 2.5

# Range to search for a free port when auto-assigning one for an instance.
PORT_RANGE_START = 32500
PORT_RANGE_ATTEMPTS = 100

# No special features needed for this plugin
SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PlexConnectProvider(mass, manifest, config)


class PlexConnectProvider(PluginProvider):
    """Plex Connect plugin provider implementation."""

    reload_on_streams_network_change = True

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """
        Initialize the plugin provider.

        :param mass: MusicAssistant instance.
        :param manifest: Provider manifest.
        :param config: Provider configuration.
        """
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self.mass_player_id = cast("str", self.get_setup_value(CONF_MASS_PLAYER_ID))
        self.plex_provider_id = cast("str", self.get_setup_value(CONF_PLEX_PROVIDER_ID))
        self.custom_player_name = cast("str | None", self.config.get_value(CONF_PLAYER_NAME))
        self.device_class = cast("str", self.config.get_value(CONF_DEVICE_CLASS)) or "speaker"

        self._plex_provider: PlexProvider | None = None
        self._player_instance: PlayerRemoteInstance | None = None
        self._allocated_port: int | None = None
        self._plextv_pin: PlexPin | None = None
        self._plextv_device_id: str | None = None
        self._stop_called: bool = False
        self._on_unload_callbacks: list[Callable[..., None]] = []

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        linked = bool(self.get_setup_value(CONF_PLEXTV_TOKEN))
        return (*self._option_entries(), *self._plextv_link_entries(linked=linked))

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...]:
        """
        Handle a plex.tv link action button press from this provider's options.

        :param action: The action id of the pressed button.
        """
        if action == CONF_ACTION_START_LINK:
            status_key, status_params = await self._plextv_start_link()
        elif action == CONF_ACTION_COMPLETE_LINK:
            status_key, status_params = await self._plextv_complete_link()
        elif action == CONF_ACTION_UNLINK:
            status_key, status_params = self._plextv_unlink()
        else:
            raise ActionUnavailable(f"Unknown action: {action}")
        linked = bool(self.get_setup_value(CONF_PLEXTV_TOKEN))
        return (
            *self._option_entries(),
            *self._plextv_link_entries(
                linked=linked, status_key=status_key, status_params=status_params
            ),
        )

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
        player = self.mass.players.get_player(self.mass_player_id)
        if not player:
            self.logger.info(
                f"Player {self.mass_player_id} not found yet, waiting for PLAYER_ADDED event"
            )
        else:
            # Setup the player instance immediately
            await self._setup_player_instance()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle close/cleanup of the provider.

        :param is_removed: Whether the provider is being removed.
        """
        self._stop_called = True

        if is_removed:
            await self._unregister_from_plextv()

        # Stop player instance
        if self._player_instance:
            await self._player_instance.stop()
            self._player_instance = None

        # Unsubscribe from events
        for callback in self._on_unload_callbacks:
            callback()
        self._on_unload_callbacks.clear()

    def _option_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the editable option entries for this instance."""
        return (
            ConfigEntry(
                key=CONF_PLAYER_NAME,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=None,
            ),
            ConfigEntry(
                key=CONF_DEVICE_CLASS,
                type=ConfigEntryType.STRING,
                required=False,
                default_value="speaker",
                options=[
                    ConfigValueOption("speaker"),
                    ConfigValueOption("phone"),
                    ConfigValueOption("tablet"),
                    ConfigValueOption("stb"),
                    ConfigValueOption("tv"),
                    ConfigValueOption("pc"),
                    ConfigValueOption("cloud"),
                ],
            ),
            ConfigEntry(
                key=CONF_PORT,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=None,
                advanced=True,
                requires_reload=True,
            ),
        )

    def _plextv_link_entries(
        self,
        *,
        linked: bool,
        status_key: str | None = None,
        status_params: list[str] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """
        Return the config entries for the plex.tv link section.

        :param linked: Whether a plex.tv device token is currently stored.
        :param status_key: Optional translation key for a transient status label.
        :param status_params: Optional translation params for the status label.
        """
        if linked:
            flow_entries: tuple[ConfigEntry, ...] = (
                ConfigEntry(key="plextv_linked", type=ConfigEntryType.LABEL),
                ConfigEntry(
                    key=CONF_ACTION_UNLINK,
                    type=ConfigEntryType.ACTION,
                    action=CONF_ACTION_UNLINK,
                    required=False,
                ),
            )
        elif self._plextv_pin is not None:
            flow_entries = (
                ConfigEntry(
                    key="plextv_link_code",
                    type=ConfigEntryType.LABEL,
                    translation_params=[self._plextv_pin.code],
                ),
                ConfigEntry(
                    key=CONF_ACTION_COMPLETE_LINK,
                    type=ConfigEntryType.ACTION,
                    action=CONF_ACTION_COMPLETE_LINK,
                    required=False,
                ),
                ConfigEntry(
                    key=CONF_ACTION_START_LINK,
                    type=ConfigEntryType.ACTION,
                    action=CONF_ACTION_START_LINK,
                    translation_key="start_link_new",
                    required=False,
                ),
            )
        else:
            flow_entries = (
                ConfigEntry(key="plextv_link_intro", type=ConfigEntryType.LABEL),
                ConfigEntry(
                    key=CONF_ACTION_START_LINK,
                    type=ConfigEntryType.ACTION,
                    action=CONF_ACTION_START_LINK,
                    required=False,
                ),
            )
        status_entries: tuple[ConfigEntry, ...] = (
            (
                ConfigEntry(
                    key="plextv_link_status",
                    type=ConfigEntryType.LABEL,
                    translation_key=status_key,
                    translation_params=status_params,
                ),
            )
            if status_key
            else ()
        )
        return (
            ConfigEntry(key="plextv_divider", type=ConfigEntryType.DIVIDER),
            *flow_entries,
            *status_entries,
        )

    async def _plextv_start_link(self) -> tuple[str | None, list[str] | None]:
        """Request a new link PIN from plex.tv and hold it for the complete step."""
        client = self._plextv_client()
        try:
            self._plextv_pin = await client.create_pin()
        except (PlexTvError, aiohttp.ClientError, TimeoutError) as err:
            return "plextv_status_unreachable", [str(err)]
        return None, None

    async def _plextv_complete_link(self) -> tuple[str | None, list[str] | None]:
        """Check the pending PIN and, once confirmed, store and apply the device token."""
        if self._plextv_pin is None:
            return "plextv_status_no_pin", None
        client = self._plextv_client()
        try:
            token: str | None = None
            for attempt in range(PIN_CHECK_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(PIN_CHECK_INTERVAL)
                if token := await client.check_pin(self._plextv_pin.id):
                    break
        except PlexTvPinExpiredError:
            self._plextv_pin = None
            return "plextv_status_expired", None
        except (PlexTvError, aiohttp.ClientError, TimeoutError) as err:
            return "plextv_status_unreachable", [str(err)]
        if not token:
            return "plextv_status_not_confirmed", None
        # persist immediately (encrypted, survives without a separate save) and register now
        self._update_setup_data(CONF_PLEXTV_TOKEN, token)
        self._plextv_pin = None
        self.mass.create_task(self._register_on_plextv())
        return None, None

    def _plextv_unlink(self) -> tuple[str | None, list[str] | None]:
        """Forget the stored plex.tv device token for this player."""
        self._update_setup_data(CONF_PLEXTV_TOKEN, None)
        self._plextv_pin = None
        self._plextv_device_id = None
        return None, None

    def _plextv_client(self) -> PlexTvClient:
        """Return a plex.tv client presenting this instance's player identity."""
        player = self.mass.players.get_player(self.mass_player_id)
        player_name = self.custom_player_name or (
            player.display_name if player else "Music Assistant"
        )
        identity = PlexTvIdentity(
            client_id=compute_client_id(self.plex_provider_id, self.mass_player_id),
            name=player_name,
            version=build_version(self.mass.version),
        )
        return PlexTvClient(self.mass.http_session, identity)

    async def _register_on_plextv(self) -> None:
        """Verify the plex.tv registration and (re)publish this player's connection URI."""
        token = cast("str | None", self.get_setup_value(CONF_PLEXTV_TOKEN))
        if not token:
            self.logger.info(
                "Not linked with plex.tv: this player will not be visible in the Plexamp "
                "mobile apps. Use 'Link with plex.tv' in the plugin settings to enable this."
            )
            return
        if not self._allocated_port:
            self.logger.debug("Player instance not ready, deferring plex.tv registration")
            return
        try:
            client = self._plextv_client()
            device_id = await client.get_device_id(token)
            if not device_id:
                self.logger.warning(
                    "This player is missing from the plex.tv device registry - "
                    "re-link it via the plugin settings"
                )
                return
            self._plextv_device_id = device_id
            uri = f"http://{self.mass.streams.publish_ip}:{self._allocated_port}"
            await client.publish_connection(token, device_id, uri)
            self.logger.info("Published plex.tv connection %s for Plexamp mobile discovery", uri)
        except PlexTvAuthError:
            self.logger.warning(
                "plex.tv rejected the stored device token - "
                "re-link this player via the plugin settings"
            )
        except (PlexTvError, aiohttp.ClientError, TimeoutError) as err:
            self.logger.warning(
                "plex.tv is unreachable (%s) - local network discovery is unaffected, "
                "registration will be retried on the next restart",
                err,
            )

    async def _unregister_from_plextv(self) -> None:
        """Best-effort removal of this player from the plex.tv device registry."""
        token = cast("str | None", self.get_setup_value(CONF_PLEXTV_TOKEN))
        if not token:
            self.logger.debug("No plex.tv device token known, skipping deregistration")
            return
        try:
            client = self._plextv_client()
            device_id = self._plextv_device_id or await client.get_device_id(token)
            if device_id:
                await client.delete_device(token, device_id)
                self.logger.debug("Removed this player from the plex.tv device registry")
        except Exception as err:
            self.logger.debug(
                "Could not remove this player from plex.tv (%s) - "
                "the device can be removed manually from the Plex account settings",
                err,
            )

    async def _resolve_port(self) -> int:
        """
        Return the long-term port for this instance, allocating one if needed.

        The port is persisted in the instance config so it stays stable across restarts.
        A new port is allocated (and persisted) only on first setup, or if the configured
        port is currently taken by another process.

        :return: The port to bind this instance's remote control server to.
        """
        configured_port = self.config.get_value(CONF_PORT)
        # Probe on IPv4 all-interfaces, matching how the remote control server binds
        if isinstance(configured_port, int) and not await is_port_in_use(
            configured_port, host="0.0.0.0"
        ):
            return configured_port

        port = await select_free_port(
            PORT_RANGE_START, PORT_RANGE_START + PORT_RANGE_ATTEMPTS, host="0.0.0.0"
        )
        if port != configured_port:
            try:
                self.mass.config.set_raw_provider_config_value(self.instance_id, CONF_PORT, port)
            except Exception as err:
                self.logger.debug("Failed to persist port %s: %s", port, err)
        return port

    async def _setup_player_instance(self) -> None:
        """Set up the Plex remote control instance for the player."""
        # Don't create duplicate instances
        if self._player_instance:
            self.logger.debug("Player instance already exists, skipping setup")
            return

        if not self._plex_provider:
            self.logger.error("Cannot setup player instance: Plex provider not available")
            return

        player = self.mass.players.get_player(self.mass_player_id)
        if not player:
            self.logger.warning(f"Player {self.mass_player_id} not found")
            return

        # Resolve the long-term port for this instance (persisted across restarts)
        if not self._allocated_port:
            self._allocated_port = await self._resolve_port()

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
            return

        # (Re)publish the plex.tv registration in the background (never blocks startup)
        self.mass.create_task(self._register_on_plextv())

    async def _teardown_player_instance(self) -> None:
        """Tear down the Plex remote control instance."""
        if self._player_instance:
            await self._player_instance.stop()
            self._player_instance = None

    def _on_mass_player_event(self, event: MassEvent) -> None:
        """
        Handle player added/removed events.

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
