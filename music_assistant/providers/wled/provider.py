"""WLED Audio Sync provider for Music Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.helpers.util import try_parse_float, try_parse_int
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

# changed_keys arrive namespaced as 'values/<key>' (see Config.get_changed_values).
_IMMEDIATE_APPLY_KEYS = {
    f"values/{key}" for key in (CONF_LATENCY_MS, CONF_GAIN_DB, CONF_SCALING_MODE)
}


def _scaling_mode_from_config(config: ProviderConfig) -> ScalingMode:
    """Resolve the configured scaling mode, falling back to the default if invalid/unset."""
    value = str(config.get_value(CONF_SCALING_MODE, DEFAULT_SCALING_MODE))
    if value in SCALING_MODES:
        return cast("ScalingMode", value)
    return DEFAULT_SCALING_MODE


def _port_from_config(mass: MusicAssistant, config: ProviderConfig) -> int:
    """
    Resolve the configured zone port straight from storage.

    Reading through config.get_value() breaks for an unloaded/failed sibling:
    get_provider_config_entries() only returns the server-injected default
    entries there (no CONF_PORT entry to resolve), so it always reads back as
    DEFAULT_PORT even when a real port was set. Reading the raw stored values
    and setup_data instead works regardless of load state -- a port entry
    only ever ends up in one of the two: unchanged since setup, it's still in
    setup_data; changed via the options UI, save_provider_config persists the
    override to values without touching setup_data.
    """
    instance_id = config.instance_id
    value = mass.config.get_raw_provider_config_value(instance_id, CONF_PORT)
    if value is None:
        value = mass.config.get_provider_setup_value(instance_id, CONF_PORT)
    # A port is never legitimately 0 (range starts at 1024), so falling back to the
    # default here can't clobber a real configured value the way it would for a
    # latency/gain setting where 0 is meaningful.
    return try_parse_int(value, DEFAULT_PORT) or DEFAULT_PORT


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
        port = _port_from_config(self.mass, self.config)
        siblings = await self.mass.config.get_provider_configs(
            provider_domain=self.domain, include_values=True
        )
        for sibling in siblings:
            if sibling.instance_id == self.instance_id:
                continue
            if _port_from_config(self.mass, sibling) == port:
                sibling_name = sibling.name or sibling.default_name
                raise SetupFailedError(
                    f"Zone port {port} is already used by WLED instance '{sibling_name}'. "
                    "Each WLED instance needs its own port -- physical devices join a zone "
                    "by setting their own audioSyncPort to match, not by adding another "
                    "instance here."
                )

    async def loaded_in_mass(self) -> None:
        """Start the sync-zone bridge for this instance's configured port."""
        port = _port_from_config(self.mass, self.config)
        # 0 is a valid latency/gain setting, so unlike the port above, a parse failure
        # (never expected for an already-validated config value) is the only case that
        # should fall back to the default -- try_parse_int/float only does that on error,
        # never on a falsy-but-valid value, which "or DEFAULT" would get wrong.
        latency_ms = cast(
            "int",
            try_parse_int(
                self.config.get_value(CONF_LATENCY_MS, DEFAULT_LATENCY_MS), DEFAULT_LATENCY_MS
            ),
        )
        gain_db = cast(
            "float",
            try_parse_float(self.config.get_value(CONF_GAIN_DB, DEFAULT_GAIN_DB), DEFAULT_GAIN_DB),
        )
        scaling_mode = _scaling_mode_from_config(self.config)
        self._bridge_manager = WledBridgeManager(self)
        # available reflects whether the bridge actually came up -- the Sendspin
        # provider may not be loaded (yet), in which case start() logs and
        # returns without creating a bridge, and this instance has no virtual
        # player or UDP transport to be "available" for.
        self.available = await self._bridge_manager.start(
            port, gain_db=gain_db, scaling_mode=scaling_mode, latency_ms=latency_ms
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._bridge_manager:
            await self._bridge_manager.stop()
            self._bridge_manager = None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Handle config changes."""
        if changed_keys and changed_keys <= _IMMEDIATE_APPLY_KEYS and self._bridge_manager:
            self._bridge_manager.update_settings(
                latency_ms=cast(
                    "int",
                    try_parse_int(
                        config.get_value(CONF_LATENCY_MS, DEFAULT_LATENCY_MS), DEFAULT_LATENCY_MS
                    ),
                ),
                gain_db=cast(
                    "float",
                    try_parse_float(
                        config.get_value(CONF_GAIN_DB, DEFAULT_GAIN_DB), DEFAULT_GAIN_DB
                    ),
                ),
                scaling_mode=_scaling_mode_from_config(config),
            )
            self.config = config
            return

        # A changed port requires re-registering the Sendspin client, so fall
        # back to a full reload.
        await super().update_config(config, changed_keys)
