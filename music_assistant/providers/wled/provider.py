"""WLED Audio Sync provider for Music Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.models.plugin import PluginProvider

from .bridge import WledBridgeManager
from .constants import (
    CONF_GAIN_DB,
    CONF_LATENCY_MS,
    CONF_PORT,
    CONF_SCALING_MODE,
    DEFAULT_GAIN_DB,
    DEFAULT_LATENCY_MS,
    DEFAULT_PORT,
    SCALING_MODES,
)
from .packet import DEFAULT_SCALING_MODE

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.wled.packet import ScalingMode

LOGGER = logging.getLogger(__name__)


def _scaling_mode_from_config(config: ProviderConfig) -> ScalingMode:
    """Resolve the configured scaling mode, falling back to the default if invalid/unset."""
    value = str(config.get_value(CONF_SCALING_MODE) or DEFAULT_SCALING_MODE)
    if value in SCALING_MODES:
        return cast("ScalingMode", value)
    return DEFAULT_SCALING_MODE


def _port_from_config(config: ProviderConfig) -> int:
    """Resolve the configured zone port."""
    return int(float(str(config.get_value(CONF_PORT) or DEFAULT_PORT)))


class WledProvider(PluginProvider):
    """
    Provider that drives WLED's Audio Sync UDP protocol from MA playback.

    Each instance represents one sync zone, identified by a UDP port: any
    number of physical WLED devices join the zone by setting their own
    audioSyncPort (WLED's Usermods -> Audio Reactive -> Sync Settings) to
    match. Grouping the resulting virtual player with a real speaker player
    is what makes that zone's lights react to that speaker's audio.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._bridge_manager: WledBridgeManager | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries for the WLED Audio Sync provider."""
        return (
            ConfigEntry(
                key=CONF_PORT,
                type=ConfigEntryType.INTEGER,
                # setup_flow.py picks a free port before creation and stores it via
                # session.finish() (setup_data, not values) -- pull that in as the
                # default so a freshly-created instance reflects the port the user
                # actually chose/confirmed, not always the hardcoded default.
                default_value=self.get_setup_value(CONF_PORT, DEFAULT_PORT),
                range=(1024, 65535),
                category="settings",
            ),
            ConfigEntry(
                key=CONF_LATENCY_MS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_LATENCY_MS,
                range=(0, 3000),
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_GAIN_DB,
                type=ConfigEntryType.FLOAT,
                default_value=DEFAULT_GAIN_DB,
                range=(-20, 40),
                immediate_apply=True,
                category="settings",
            ),
            ConfigEntry(
                key=CONF_SCALING_MODE,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_SCALING_MODE,
                options=[
                    ConfigValueOption(mode, title=mode.replace("_", " ").title())
                    for mode in SCALING_MODES
                ],
                immediate_apply=True,
                category="settings",
            ),
        )

    async def handle_async_init(self) -> None:
        """
        Reject this config if another WLED instance already claims the same zone port.

        One zone == one port == one provider instance by design (see class
        docstring); physical devices join a zone via their own port setting,
        never by adding another MA instance. Without this check, two
        instances sharing a port would silently fight over the same Sendspin
        client_id (derived from the port) instead of failing loudly -- the
        second instance's registration kicks the first one's connection.
        """
        port = _port_from_config(self.config)
        siblings = await self.mass.config.get_provider_configs(
            provider_domain=self.domain, include_values=True
        )
        for sibling in siblings:
            if sibling.instance_id == self.instance_id:
                continue
            if _port_from_config(sibling) == port:
                sibling_name = sibling.name or sibling.default_name
                raise SetupFailedError(
                    f"Zone port {port} is already used by WLED instance '{sibling_name}'. "
                    "Each WLED instance needs its own port -- physical devices join a zone "
                    "by setting their own audioSyncPort to match, not by adding another "
                    "instance here."
                )

    async def loaded_in_mass(self) -> None:
        """Start the sync-zone bridge for this instance's configured port."""
        port = _port_from_config(self.config)
        gain_db = float(str(self.config.get_value(CONF_GAIN_DB) or DEFAULT_GAIN_DB))
        scaling_mode = _scaling_mode_from_config(self.config)
        self._bridge_manager = WledBridgeManager(self)
        await self._bridge_manager.start(port, gain_db=gain_db, scaling_mode=scaling_mode)
        self.available = True

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._bridge_manager:
            await self._bridge_manager.stop()
            self._bridge_manager = None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Handle config changes."""
        immediate_keys = {CONF_LATENCY_MS, CONF_GAIN_DB, CONF_SCALING_MODE}
        if changed_keys and changed_keys <= immediate_keys and self._bridge_manager:
            self._bridge_manager.update_settings(
                latency_ms=int(float(str(config.get_value(CONF_LATENCY_MS) or DEFAULT_LATENCY_MS))),
                gain_db=float(str(config.get_value(CONF_GAIN_DB) or DEFAULT_GAIN_DB)),
                scaling_mode=_scaling_mode_from_config(config),
            )
            self.config = config
            return

        # A changed port requires re-registering the Sendspin client, so fall
        # back to a full reload.
        await super().update_config(config, changed_keys)
