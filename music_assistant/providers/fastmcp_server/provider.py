"""
MCP Server provider — main PluginProvider implementation.

The provider is a thin lifecycle wrapper over :class:`MCPServerRuntime` from
``server.py``. ``handle_async_init`` constructs the runtime and starts it;
``unload`` shuts it down; ``update_config`` either hot-swaps request policy
middleware (for permission-only changes) or restarts the runtime.
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from music_assistant.models.plugin import PluginProvider

from .constants import is_hot_swappable_key

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigActionResult,
        ConfigEntry,
        ProviderConfig,
    )

    from .commands import ProviderCommandSet
    from .server import MCPServerRuntime


LOGGER = logging.getLogger(__name__)


class MCPServerProvider(PluginProvider):  # type: ignore[misc, unused-ignore]
    """Music Assistant plugin provider wrapping an MCP server runtime."""

    _runtime: MCPServerRuntime | None = None
    _commands: ProviderCommandSet | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        from .config import build_config_entries  # noqa: PLC0415
        from .constants import (  # noqa: PLC0415
            CONF_MANUAL_TOKEN_IDS,
            CONF_MOUNT_PATH,
            DEFAULT_MOUNT_PATH,
        )
        from .policy_config import current_user_mcp_tokens  # noqa: PLC0415

        tokens = await current_user_mcp_tokens(self.mass)
        return build_config_entries(
            self.mass,
            str(self.get_config_value(CONF_MOUNT_PATH, DEFAULT_MOUNT_PATH)),
            tokens=tokens,
            manual_token_ids=self.get_config_value(CONF_MANUAL_TOKEN_IDS, []) or (),
            stored_value_provider=self._raw_policy_value,
        )

    async def handle_config_action(
        self, action: str
    ) -> tuple[ConfigEntry, ...] | ConfigActionResult | None:
        """Handle a one-shot config action button press and report its outcome."""
        if action == "open_connect":
            from music_assistant_models.config_entries import ConfigActionResult  # noqa: PLC0415
            from music_assistant_models.errors import ActionUnavailable  # noqa: PLC0415

            from ._init_helpers import _dispatch_open_connect  # noqa: PLC0415
            from .constants import CONF_CONNECT_EXTERNAL_URL, CONF_MOUNT_PATH  # noqa: PLC0415

            url = await _dispatch_open_connect(
                self.mass,
                {
                    CONF_MOUNT_PATH: self.get_config_value(CONF_MOUNT_PATH),
                    CONF_CONNECT_EXTERNAL_URL: self.get_config_value(CONF_CONNECT_EXTERNAL_URL),
                },
            )
            if url is None:
                raise ActionUnavailable(
                    "The Connect Wizard could not be opened; check the provider logs.",
                    translation_key="connect_wizard_unavailable",
                    translation_owner=self.translation_owner,
                )
            return ConfigActionResult(open_url=url)
        return await super().handle_config_action(action)

    async def handle_async_init(self) -> None:
        """Build policy runtime, register commands, then mount MCP atomically."""
        from .commands import ProviderCommandSet  # noqa: PLC0415
        from .server import MCPServerRuntime  # noqa: PLC0415

        runtime = MCPServerRuntime(
            self.mass,
            self.config,
            self.logger,
            policy_change_callback=self._apply_policy_token_ids,
        )
        self._runtime = runtime
        self._commands = ProviderCommandSet(
            self.mass,
            config_provider=lambda: self.config,
            policy_provider=self._resolve_command_policy,
            diagnostics_provider=lambda: (
                self._runtime.dynamic_diagnostics()
                if self._runtime is not None
                else {"available": False, "last_error": "MCP runtime not started"}
            ),
            audit_client_id_provider=self._resolve_audit_client_id,
            raw_policy_value_provider=self._raw_policy_value,
        )
        try:
            self._commands.start()
            await runtime.start()
        except BaseException:
            try:
                if self._runtime is not None:
                    with suppress(BaseException):
                        await self._runtime.stop()
            finally:
                try:
                    if self._commands is not None:
                        with suppress(BaseException):
                            self._commands.stop()
                finally:
                    self._runtime = None
                    self._commands = None
            raise

    async def loaded_in_mass(self) -> None:
        """Log the public URL once everything is wired up."""
        if self._runtime is not None:
            self.logger.info("MCP server mounted at %s", self._runtime.public_url)

    async def unload(self, is_removed: bool = False) -> None:
        """Stop the MCP endpoint before withdrawing its MA commands."""
        _ = is_removed  # Required by Music Assistant's provider lifecycle signature.
        try:
            if self._runtime is not None:
                await self._runtime.stop()
        finally:
            self._runtime = None
            try:
                if self._commands is not None:
                    self._commands.stop()
            finally:
                self._commands = None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Apply config changes — hot-swap when possible, restart otherwise."""
        self.config = config
        self._persist_policy_suffix_index(config, changed_keys)
        if self._commands is not None:
            self._commands.update_config(config)
        if self._runtime is None:
            if self._commands is not None:
                await self._start_runtime(config)
            return
        normalized_keys = {k.removeprefix("values/") for k in changed_keys}
        if all(is_hot_swappable_key(key) for key in normalized_keys):
            await self._runtime.apply_config_change(config, normalized_keys)
        else:
            await self._runtime.stop()
            self._runtime = None
            await self._start_runtime(config)

    async def _start_runtime(self, config: ProviderConfig) -> None:
        """Create and start a runtime, leaving no failed instance attached."""
        from .server import MCPServerRuntime  # noqa: PLC0415

        if self._commands is not None:
            self._commands.update_config(config, active_token_ids=frozenset())
        runtime = MCPServerRuntime(
            self.mass,
            config,
            self.logger,
            policy_change_callback=self._apply_policy_token_ids,
        )
        self._runtime = runtime
        try:
            await runtime.start()
        except BaseException:
            self._runtime = None
            with suppress(BaseException):
                await runtime.stop()
            raise

    def _resolve_command_policy(self, bearer_token: str | None) -> Any:
        """Resolve through the currently attached runtime for every handler call."""
        runtime = self._runtime
        resolver = getattr(runtime, "resolve_request_policy", None)
        if callable(resolver):
            return resolver(bearer_token)
        from .policy_config import build_policy_resolver  # noqa: PLC0415

        return build_policy_resolver(
            self.config,
            raw_value_provider=self._raw_policy_value,
        ).resolve(None)

    def _resolve_audit_client_id(self, bearer_token: str | None) -> str:
        """Resolve a safe audit label through the currently attached runtime."""
        runtime = self._runtime
        resolver = getattr(runtime, "audit_client_id", None)
        if callable(resolver):
            return str(resolver(bearer_token))
        from .audit import NO_TOKEN_CLIENT_ID  # noqa: PLC0415

        return NO_TOKEN_CLIENT_ID

    def _apply_policy_token_ids(self, token_ids: frozenset[str]) -> None:
        """Refresh event retention when authenticated token identities change."""
        if self._commands is not None:
            self._commands.update_config(self.config, active_token_ids=token_ids)

    def _raw_policy_value(self, key: str) -> object:
        """Read one preserved policy value through MA's sanctioned raw API."""
        from .policy_config import raw_provider_config_value  # noqa: PLC0415

        return raw_provider_config_value(
            self.mass, str(getattr(getattr(self, "config", None), "instance_id", "")), key
        )

    def _persist_policy_suffix_index(
        self,
        config: ProviderConfig,
        changed_keys: set[str],
    ) -> None:
        """Persist non-reversible suffixes for newly rendered token policy rows."""
        from .constants import CONF_POLICY_TOKEN_SUFFIXES  # noqa: PLC0415

        suffixes = {
            match.group(1)
            for key in changed_keys
            if (match := re.search(r"([0-9a-f]{64})$", key.removeprefix("values/")))
        }
        if not suffixes:
            return
        current = config.get_value(CONF_POLICY_TOKEN_SUFFIXES, [])
        if isinstance(current, list | tuple | set | frozenset):
            suffixes.update(
                str(value) for value in current if re.fullmatch(r"[0-9a-f]{64}", str(value))
            )
        ordered = sorted(suffixes)
        entry = getattr(config, "values", {}).get(CONF_POLICY_TOKEN_SUFFIXES)
        if entry is not None:
            entry.value = ordered
        config_controller = getattr(self.mass, "config", None)
        setter = getattr(config_controller, "set_raw_provider_config_value", None)
        instance_id = str(getattr(config, "instance_id", ""))
        if callable(setter) and instance_id:
            setter(
                instance_id,
                CONF_POLICY_TOKEN_SUFFIXES,
                ordered,
                immediate=True,
            )
