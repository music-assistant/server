"""Core controller configuration handling for the ConfigController."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast, overload

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import (
    ConfigActionResult,
    ConfigEntry,
    ConfigValueType,
    CoreConfig,
)

from music_assistant.constants import (
    CONF_CORE,
    CONF_PLAYER_QUEUES,
    CONFIGURABLE_CORE_CONTROLLERS,
    DEFAULT_CORE_CONFIG_ENTRIES,
)
from music_assistant.controllers.config.constants import _ConfigValueT
from music_assistant.controllers.config.helpers import _with_translation_owner
from music_assistant.controllers.player_queues.constants import CONF_AUTOPLAY_PLAYLIST
from music_assistant.helpers.api import api_command

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.models.core_controller import CoreController


class CoreConfigMixin:
    """Mixin providing core controller configuration handling for the ConfigController."""

    # Type hints for attributes/methods provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant

        def get(self, key: str, default: Any = None) -> Any: ...  # noqa: D102

        def set(self, key: str, value: Any) -> None: ...  # noqa: D102

        def save(self, immediate: bool = False) -> None: ...  # noqa: D102

    @api_command("config/core", required_scope=Scope.CONFIG_CORE_READ)
    async def get_core_configs(self, include_values: bool = False) -> list[CoreConfig]:
        """Return all core controllers config options."""
        return [
            await self.get_core_config(core_controller)
            if include_values
            else cast(
                "CoreConfig",
                CoreConfig.parse([], self._get_raw_core_config(core_controller)),
            )
            for core_controller in CONFIGURABLE_CORE_CONTROLLERS
        ]

    @api_command("config/core/get", required_scope=Scope.CONFIG_CORE_READ)
    async def get_core_config(self, domain: str) -> CoreConfig:
        """Return configuration for a single core controller."""
        raw_conf = self._get_raw_core_config(domain)
        # build the schema straight from the controller (no dynamic UI options):
        # CoreConfig.parse stamps the translation owner itself
        controller: CoreController = getattr(self.mass, domain)
        config_entries = list(await controller.get_config_entries() + DEFAULT_CORE_CONFIG_ENTRIES)
        return cast("CoreConfig", CoreConfig.parse(config_entries, raw_conf))

    @overload
    async def get_core_config_value(
        self,
        domain: str,
        key: str,
        *,
        default: _ConfigValueT,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_core_config_value(
        self,
        domain: str,
        key: str,
        *,
        default: ConfigValueType = ...,
        return_type: type[_ConfigValueT] = ...,
    ) -> _ConfigValueT: ...

    @overload
    async def get_core_config_value(
        self,
        domain: str,
        key: str,
        *,
        default: ConfigValueType = ...,
        return_type: None = ...,
    ) -> ConfigValueType: ...

    @api_command("config/core/get_value", required_scope=Scope.CONFIG_CORE_READ)
    async def get_core_config_value(
        self,
        domain: str,
        key: str,
        *,
        default: ConfigValueType = None,
        return_type: type[_ConfigValueT | ConfigValueType] | None = None,
    ) -> _ConfigValueT | ConfigValueType:
        """
        Return single configentry value for a core controller.

        :param domain: The core controller domain.
        :param key: The config key to retrieve.
        :param default: Optional default value to return if key is not found.
        :param return_type: Optional type hint for type inference (e.g., str, int, bool).
            Note: This parameter is used purely for static type checking and does not
            perform runtime type validation. Callers are responsible for ensuring the
            specified type matches the actual config value type.
        """
        # prefer stored value so we don't have to retrieve all config entries every time
        if (raw_value := self.get_raw_core_config_value(domain, key)) is not None:
            return raw_value
        conf = await self.get_core_config(domain)
        if key not in conf.values:
            if default is not None:
                return default
            msg = f"Config key {key} not found for core controller {domain}"
            raise KeyError(msg)
        return (
            conf.values[key].value
            if conf.values[key].value is not None
            else conf.values[key].default_value
        )

    @api_command("config/core/get_entries", required_scope=Scope.CONFIG_CORE_READ)
    async def get_core_config_entries(self, domain: str) -> list[ConfigEntry]:
        """
        Return Config entries to configure a core controller.

        :param domain: The core controller domain.
        """
        controller: CoreController = getattr(self.mass, domain)
        return await self._resolve_core_config_entries(
            domain, await controller.get_config_entries()
        )

    @api_command("config/core/invoke_action", required_scope=Scope.CONFIG_CORE_WRITE)
    async def invoke_core_config_action(
        self, domain: str, action: str
    ) -> list[ConfigEntry] | ConfigActionResult:
        """
        Run a one-shot action button from a core module's config.

        A ``ConfigActionResult`` holds the outcome to report to the user; an empty list
        means the action ran with nothing to report; a non-empty list holds the entries
        the config form should re-render with.

        :param domain: The core controller domain.
        :param action: The action id of the pressed button.
        """
        controller: CoreController = getattr(self.mass, domain)
        if (result := await controller.handle_config_action(action)) is None:
            return []
        if isinstance(result, ConfigActionResult):
            result.translation_owner = result.translation_owner or f"core.{domain}"
            return result
        return await self._resolve_core_config_entries(domain, result)

    @api_command("config/core/save", required_scope=Scope.CONFIG_CORE_WRITE)
    async def save_core_config(
        self,
        domain: str,
        values: dict[str, ConfigValueType],
    ) -> CoreConfig:
        """Save CoreController Config values."""
        config = await self.get_core_config(domain)
        prev_config = config.to_raw()
        changed_keys = config.update(values)
        # validate the new config
        config.validate()
        if not changed_keys:
            # no changes
            return config
        # save the config first before reloading to avoid issues on reload
        # for example when reloading the webserver we might be cancelled here
        conf_key = f"{CONF_CORE}/{domain}"
        self.set(conf_key, config.to_raw())
        self.save(immediate=True)
        try:
            controller: CoreController = getattr(self.mass, domain)
            await controller.update_config(config, changed_keys)
        except asyncio.CancelledError:
            pass
        except Exception:
            # revert to previous config on error
            self.set(conf_key, prev_config)
            self.save(immediate=True)
            raise
        # reload succeeded; clear last_error and persist the final state
        config.last_error = None
        # return full config
        return await self.get_core_config(domain)

    if TYPE_CHECKING:
        # Overload for when default is provided - return type matches default type
        @overload
        def get_raw_core_config_value(
            self, core_module: str, key: str, default: _ConfigValueT
        ) -> _ConfigValueT: ...

        # Overload for when no default is provided - return ConfigValueType | None
        @overload
        def get_raw_core_config_value(
            self, core_module: str, key: str, default: None = None
        ) -> ConfigValueType | None: ...

    def get_raw_core_config_value(
        self, core_module: str, key: str, default: ConfigValueType = None
    ) -> ConfigValueType:
        """
        Return (raw) single configentry value for a core controller.

        Note that this only returns the stored value without any validation or default.
        """
        return cast(
            "ConfigValueType",
            self.get(
                f"{CONF_CORE}/{core_module}/values/{key}",
                self.get(f"{CONF_CORE}/{core_module}/{key}", default),
            ),
        )

    def set_raw_core_config_value(self, core_module: str, key: str, value: ConfigValueType) -> None:
        """
        Set (raw) single config(entry) value for a core controller.

        Note that this only stores the (raw) value without any validation or default.
        """
        self.ensure_core_config_base(core_module)
        self.set(f"{CONF_CORE}/{core_module}/values/{key}", value)
        # also update the controller's in-place config copy (if any) so
        # object-local value reads stay in sync with raw writes
        controller = getattr(self.mass, core_module, None)
        if (config := getattr(controller, "config", None)) and (entry := config.values.get(key)):
            entry.value = value

    def ensure_core_config_base(self, core_module: str) -> None:
        """
        Make sure the stored config block of a core controller is a valid CoreConfig object.

        Call this before writing a raw value into the block: ``set`` creates missing parents
        on the fly, so a raw write into a not-yet-existing block would leave a stub behind
        that has no ``domain`` key and therefore fails to parse as a CoreConfig.

        :param core_module: The domain of the core controller.
        """
        raw_conf = self.get(f"{CONF_CORE}/{core_module}")
        if not isinstance(raw_conf, dict) or not raw_conf:
            self.set(f"{CONF_CORE}/{core_module}", CoreConfig({}, core_module).to_raw())
        elif "domain" not in raw_conf:
            self.set(f"{CONF_CORE}/{core_module}/domain", core_module)

    def _get_raw_core_config(self, domain: str) -> dict[str, Any]:
        """
        Return the stored raw config of a core controller, safe to parse as a CoreConfig.

        :param domain: The domain of the core controller.
        """
        raw_conf = self.get(f"{CONF_CORE}/{domain}", {})
        if not isinstance(raw_conf, dict):
            raw_conf = {}
        if "domain" not in raw_conf:
            # a raw write into the block (or an install predating the write-path fix)
            # can leave the mandatory domain key behind; fill it in for the caller
            return {**raw_conf, "domain": domain}
        return raw_conf

    async def _resolve_core_config_entries(
        self, domain: str, entries: tuple[ConfigEntry, ...]
    ) -> list[ConfigEntry]:
        """Append the server default entries, resolve dynamic options and stamp the owner."""
        all_entries = list(entries + DEFAULT_CORE_CONFIG_ENTRIES)
        if domain == CONF_PLAYER_QUEUES:
            # populate the global autoplay playlist dropdown for the UI here (not in get_core_config),
            # so the config value/parse path stays free of a library lookup
            playlist_options = await self.mass.config._library_playlist_options()
            for entry in all_entries:
                if entry.key == CONF_AUTOPLAY_PLAYLIST:
                    entry.options = playlist_options
        return _with_translation_owner(all_entries, f"core.{domain}")
