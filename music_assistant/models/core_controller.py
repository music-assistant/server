"""Model/base for a Core controller within Music Assistant."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, TypeVar, overload

from music_assistant_models.config_entries import ConfigValueType
from music_assistant_models.enums import ProviderStage, ProviderType
from music_assistant_models.errors import ActionUnavailable
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_LOG_LEVEL, MASS_LOGGER_NAME

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, CoreConfig

    from music_assistant.helpers.json import SerializableType
    from music_assistant.mass import MusicAssistant

# TypeVar for config value type inference
_ConfigValueT = TypeVar("_ConfigValueT", bound=ConfigValueType)


class CoreController:
    """Base representation of a Core controller within Music Assistant."""

    domain: str  # used as identifier (=name of the module)
    manifest: ProviderManifest  # some info for the UI only
    # config: the controller's active configuration, assigned at startup/reload and kept
    # up to date by the config controller, so internal code can read config values
    # (including entry defaults) without rebuilding the config entries
    config: CoreConfig

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        self.mass = mass
        self.initialized = asyncio.Event()
        self._set_logger()
        self.manifest = ProviderManifest(
            type=ProviderType.CORE,
            domain=self.domain,
            name=f"{self.domain.title()} Core controller",
            description=f"{self.domain.title()} Core controller",
            codeowners=["@music-assistant"],
            stage=ProviderStage.STABLE,
            icon="puzzle-outline",
            builtin=True,
            allow_disable=False,
        )

    @property
    def translation_owner(self) -> str:
        """Return the "core.<domain>" namespace this module's translation strings resolve under."""
        return f"core.{self.domain}"

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return all Config Entries for this core module (if any).

        Include ``ConfigEntryType.ACTION`` entries for one-shot buttons and handle
        their presses in ``handle_config_action``.
        """
        return ()

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...]:
        """
        Handle a one-shot action button press from this module's config and re-render.

        Override to run the side effect for each ``ConfigEntryType.ACTION`` entry this
        module declares, then return the (possibly refreshed) config entries to display.

        :param action: The action id of the pressed button (an entry's ``action`` key).
        """
        raise ActionUnavailable(f"Unknown action: {action}")

    @overload
    def get_config_value(
        self, key: str, default: _ConfigValueT, *, return_type: type[_ConfigValueT] = ...
    ) -> _ConfigValueT: ...

    @overload
    def get_config_value(
        self, key: str, default: ConfigValueType = ..., *, return_type: type[_ConfigValueT]
    ) -> _ConfigValueT: ...

    @overload
    def get_config_value(
        self, key: str, default: ConfigValueType = ..., *, return_type: None = ...
    ) -> ConfigValueType: ...

    def get_config_value(
        self,
        key: str,
        default: ConfigValueType = None,
        *,
        return_type: type[_ConfigValueT | ConfigValueType] | None = None,
    ) -> _ConfigValueT | ConfigValueType:
        """
        Return a single config value from this core controller's active configuration.

        Entry defaults are already applied to the active configuration, so the
        default is only returned when the key itself is not present.

        :param key: The config key to retrieve.
        :param default: Value to return when the key is not present in the config.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        return self.config.get_value(key, default)

    async def get_diagnostics(self) -> dict[str, SerializableType] | None:
        """
        Return optional diagnostics info for this controller to include in diagnostics reports.

        Return None (the default) when this controller has nothing to contribute.
        Keep the returned data small, JSON serializable and free of sensitive values.
        """
        return None

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""

    async def post_setup(self) -> None:
        """Handle logic after all core controllers have been set up."""

    async def close(self) -> None:
        """Handle logic on server stop."""

    async def reload(self, config: CoreConfig | None = None) -> None:
        """Reload this core controller."""
        await self.close()
        if config is None:
            config = await self.mass.config.get_core_config(self.domain)
        log_level = str(config.get_value(CONF_LOG_LEVEL))
        self._set_logger(log_level)
        self.config = config
        await self.setup(config)
        await self.post_setup()

    async def update_config(self, config: CoreConfig, changed_keys: set[str]) -> None:
        """Handle logic when the config is updated."""
        # always update the stored config so dynamic reads pick up new values
        self.config = config

        # apply log level change dynamically (doesn't require reload)
        if f"values/{CONF_LOG_LEVEL}" in changed_keys:
            log_value = str(config.get_value(CONF_LOG_LEVEL))
            self._set_logger(log_value)

        # reload if any changed value entry has requires_reload set to True
        needs_reload = any(
            (entry := config.values.get(key.removeprefix("values/"))) is not None
            and entry.requires_reload is True
            for key in changed_keys
            if key.startswith("values/")
        )
        if needs_reload:
            self.logger.info(
                "Config updated, reloading %s core controller",
                self.manifest.name,
            )
            task_id = f"core_reload_{self.domain}"
            self.mass.call_later(1, self.reload, config, task_id=task_id)

    def _set_logger(self, log_level: str | None = None) -> None:
        """Set the logger settings."""
        mass_logger = logging.getLogger(MASS_LOGGER_NAME)
        self.logger = mass_logger.getChild(self.domain)
        if log_level is None:
            log_level = str(
                self.mass.config.get_raw_core_config_value(self.domain, CONF_LOG_LEVEL, "GLOBAL")
            )
        if log_level == "GLOBAL":
            self.logger.setLevel(mass_logger.level)
        else:
            self.logger.setLevel(log_level)
        if logging.getLogger().level > self.logger.level:
            # if the root logger's level is higher, we need to adjust that too
            logging.getLogger().setLevel(self.logger.level)
