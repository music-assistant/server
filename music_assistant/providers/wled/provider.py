"""WLED Audio Sync provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PORT
from music_assistant.models.plugin import PluginProvider

from .bridge import WledBridge
from .constants import (
    CONF_GAIN_DB,
    CONF_LATENCY_MS,
    CONF_SCALING_MODE,
    DEFAULT_GAIN_DB,
    DEFAULT_LATENCY_MS,
    DEFAULT_PORT,
    DEFAULT_SCALING_MODE,
    ScalingMode,
)
from .settings import get_settings

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.providers.sendspin.provider import SendspinProvider

# changed_keys arrive namespaced as 'values/<key>'.
_IMMEDIATE_APPLY_KEYS = {
    f"values/{key}" for key in (CONF_LATENCY_MS, CONF_GAIN_DB, CONF_SCALING_MODE)
}


class WledProvider(PluginProvider):
    """Provider that drives WLED's Audio Sync UDP protocol from MA playback, one zone per port."""

    _bridge: WledBridge | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries for the WLED Audio Sync provider."""
        return (
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
                    ConfigValueOption(mode.value, mode.value.replace("_", " ").capitalize())
                    for mode in ScalingMode
                ],
                immediate_apply=True,
                category="settings",
            ),
        )

    async def handle_async_init(self) -> None:
        """Reject a port used by another instance, then start the sync zone."""
        port = cast("int", self.get_setup_value(CONF_PORT, DEFAULT_PORT))
        for sibling in await self.mass.config.get_provider_configs(provider_domain=self.domain):
            if sibling.instance_id == self.instance_id:
                continue
            if self.mass.config.get_provider_setup_value(sibling.instance_id, CONF_PORT) == port:
                raise SetupFailedError(
                    f"Zone port {port} is already used by another WLED instance",
                    translation_key="port_in_use",
                    translation_owner=f"provider.{self.domain}",
                )
        sendspin_provider = cast("SendspinProvider", self.mass.get_provider("sendspin"))
        settings = get_settings(self.config)
        bridge = WledBridge(
            self,
            sendspin_provider,
            port,
            gain_db=settings.gain_db,
            scaling_mode=settings.scaling_mode,
            latency_ms=settings.latency_ms,
        )
        await bridge.start()
        self._bridge = bridge

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._bridge:
            await self._bridge.stop()
            self._bridge = None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Apply changed playback settings in place."""
        if self._bridge and changed_keys and changed_keys <= _IMMEDIATE_APPLY_KEYS:
            self._bridge.update_settings(*get_settings(config))
            self.config = config
            return
        await super().update_config(config, changed_keys)
