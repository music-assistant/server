"""Registration compatibility and lifecycle for native MA API commands."""
# ruff: noqa: TID252 -- provider source is transplanted under the MA package.

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast

from music_assistant_models.auth import Scope
from music_assistant_models.errors import AuthenticationRequired, InsufficientPermissions

from ..audit import (
    ANONYMOUS_USER_ID,
    NO_TOKEN_CLIENT_ID,
    AuditRecord,
    AuditSink,
    emit_audit_record,
    is_privileged_capability,
)
from ..auth import LOOKUP_FAILURE_CLIENT_ID
from ..capabilities import Capability
from ..constants import CONF_DEBUG_EVENT_BUFFER_CAPACITY, CONF_REQUIRE_AUTH
from ..debug.event_buffer import EventBuffer
from ..models import (
    EventBufferStats,
    EventSnapshot,
    HealthSummary,
    LogStatsResult,
    LogTailResult,
    PackageVersions,
    RemoveFromQueueResult,
    RouteList,
)
from ..policy_config import policy_event_buffer_enabled
from . import authorization, debug, queue
from .authorization import authorize_extension

_ResultT = TypeVar("_ResultT")

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

    from ..policy import PolicySnapshot


@dataclass(frozen=True, slots=True)
class ProviderCommand:
    """One provider command with its MA scope and provider-permission capability."""

    command: str
    handler: Callable[..., Any]
    required_scope: str
    required_capability: str


@dataclass(frozen=True, slots=True)
class _ProviderAuditContext:
    """Fixed authorization fields retained across one provider-owned execution."""

    user_id: str
    client_id: str
    command: str
    capability: str
    mode: str


def _scope(value: str) -> Scope:
    """Build the current MA Scope representation."""
    return Scope(value)


def _register(mass: Any, definition: ProviderCommand) -> Callable[[], None]:
    """Register one current-MA command and retain its unregister callback."""
    return cast(
        "Callable[[], None]",
        mass.register_api_command(
            definition.command,
            definition.handler,
            authenticated=True,
            required_scope=_scope(definition.required_scope),
        ),
    )


class ProviderCommandSet:
    """Own and register the provider's minimal native MA command surface."""

    def __init__(
        self,
        mass: Any,
        config_provider: Callable[[], ProviderConfig] | ProviderConfig,
        *,
        policy_provider: Callable[[str | None], PolicySnapshot],
        diagnostics_provider: Callable[[], Mapping[str, Any]] | None = None,
        audit_sink: AuditSink | None = None,
        audit_client_id_provider: Callable[[str | None], str] | None = None,
        raw_policy_value_provider: Callable[[str], Any] | None = None,
    ) -> None:
        """Bind MA state and lazy providers for configuration and diagnostics."""
        self._mass = mass
        self._config_provider: Callable[[], ProviderConfig]
        if hasattr(config_provider, "get_value"):
            fixed_config = cast("ProviderConfig", config_provider)
            self._config_provider = lambda: fixed_config
        else:
            self._config_provider = config_provider
        self._current_config: ProviderConfig | None = None
        self._active_policy_token_ids: frozenset[str] = frozenset()
        self._diagnostics_provider = diagnostics_provider
        self._policy_provider = policy_provider
        self._audit_sink = audit_sink or emit_audit_record
        self._audit_client_id_provider = audit_client_id_provider
        self._raw_policy_value_provider = raw_policy_value_provider
        self._buffer: EventBuffer | None = EventBuffer(
            self._mass, capacity=self._event_buffer_capacity(self._config())
        )
        self._unregister: list[Callable[[], None]] = []

    @property
    def event_buffer(self) -> EventBuffer | None:
        """Return the command-owned event buffer for the MCP debug server."""
        return self._buffer

    def update_config(
        self,
        config: ProviderConfig,
        *,
        active_token_ids: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Make existing handler closures observe the new provider configuration."""
        self._current_config = config
        if active_token_ids is not None:
            self._active_policy_token_ids = frozenset(active_token_ids)
        if self._unregister:
            self._configure_event_buffer(config)

    def start(self) -> None:
        """Register each command, restoring the previous state on partial failure."""
        if self._unregister:
            return
        definitions = self._definitions()
        registered: list[Callable[[], None]] = []
        try:
            for definition in definitions:
                registered.append(_register(self._mass, definition))
            self._configure_event_buffer(self._config())
        except BaseException:
            for unregister in reversed(registered):
                with suppress(BaseException):
                    unregister()
            raise
        self._unregister = registered

    def stop(self) -> None:
        """Unregister in reverse order and detach the event subscriber once."""
        if not self._unregister:
            return
        callbacks, self._unregister = self._unregister, []
        first_error: Exception | None = None
        try:
            for unregister in reversed(callbacks):
                try:
                    unregister()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
        finally:
            if self._buffer is not None:
                try:
                    self._buffer.stop()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error

    def _configure_event_buffer(self, config: ProviderConfig) -> None:
        """Start, stop, or resize the subscription held across MCP restarts."""
        enabled = policy_event_buffer_enabled(
            config,
            active_token_ids=self._active_policy_token_ids,
            raw_value_provider=self._raw_policy_value_provider,
        )
        capacity = self._event_buffer_capacity(config)
        if self._buffer is None or self._buffer.stats().capacity != capacity:
            if self._buffer is not None:
                self._buffer.stop()
            self._buffer = EventBuffer(self._mass, capacity=capacity)
        if not enabled:
            self._buffer.stop()
            return
        self._buffer.start()

    @staticmethod
    def _event_buffer_capacity(config: ProviderConfig) -> int:
        """Read and clamp the configured event buffer capacity."""
        value = config.get_value(CONF_DEBUG_EVENT_BUFFER_CAPACITY)
        capacity = int(value) if isinstance(value, int | float | str) else 500
        return max(50, min(capacity, 5000))

    def _config(self) -> ProviderConfig:
        """Return the most recently applied config, or lazily read the provider state."""
        return self._current_config or self._config_provider()

    def _auth_required(self) -> bool:
        """Preserve the secure default for configs created before this setting existed."""
        value = self._config().get_value(CONF_REQUIRE_AUTH)
        return True if value is None else bool(value)

    def _guard(
        self,
        command: str,
        scope: str,
        capability: Capability,
        arguments: dict[str, object] | None = None,
    ) -> _ProviderAuditContext:
        """Authorize one provider command and audit a controlled denial."""
        from ..policy import PolicyMode  # noqa: PLC0415

        bearer = authorization.current_bearer_token()
        user = authorization.current_user()
        mode = (
            self._policy_provider(bearer).mode(capability)
            if bearer is not None or not self._auth_required()
            else PolicyMode.DENY
        )
        context = _ProviderAuditContext(
            user_id=str(getattr(user, "user_id", "") or ANONYMOUS_USER_ID),
            client_id=self._audit_client_id(bearer),
            command=command,
            capability=str(capability),
            mode=mode.value,
        )
        try:
            authorize_extension(
                self._config(),
                required_scope=scope,
                required_capability=str(capability),
                policy_provider=self._policy_provider,
                require_auth=self._auth_required(),
                confirmation_command=command,
                command=command,
                arguments=arguments,
                mass=self._mass,
            )
        except AuthenticationRequired, InsufficientPermissions:
            self._emit_audit(context, "authorization.denied")
            raise
        return context

    async def _execute_audited(
        self,
        context: _ProviderAuditContext,
        result: Awaitable[_ResultT],
    ) -> _ResultT:
        """Await a provider-owned operation and record privileged outcomes."""
        try:
            value = await result
        except BaseException:
            if is_privileged_capability(context.capability):
                self._emit_audit(context, "execution.failed")
            raise
        if is_privileged_capability(context.capability):
            self._emit_audit(context, "execution.succeeded")
        return value

    def _audit_client_id(self, bearer: str | None) -> str:
        """Resolve an exact ID or a non-authoritative safe label."""
        if self._audit_client_id_provider is not None:
            return self._audit_client_id_provider(bearer)
        return LOOKUP_FAILURE_CLIENT_ID if bearer is not None else NO_TOKEN_CLIENT_ID

    def _emit_audit(self, context: _ProviderAuditContext, outcome: str) -> None:
        """Send one provider-owned record through the value-free audit boundary."""
        self._audit_sink(
            AuditRecord(
                user_id=context.user_id,
                client_id=context.client_id,
                command=context.command,
                capability=context.capability,
                mode=context.mode,
                outcome=outcome,
            )
        )

    def _capability_allowed(self, capability: Capability) -> bool:
        """Return whether the current request may read optional debug detail."""
        from ..policy import PolicyMode  # noqa: PLC0415

        bearer = authorization.current_bearer_token()
        if bearer is None and self._auth_required():
            return False
        return bool(self._policy_provider(bearer).mode(capability) is PolicyMode.ALLOW)

    def _effective_policy_profile(self) -> str:
        """Return the profile active for the exact current request."""
        from ..policy import PolicyProfile  # noqa: PLC0415

        bearer = authorization.current_bearer_token()
        if bearer is None and self._auth_required():
            return PolicyProfile.SAFE_QUERIES.value
        return self._policy_provider(bearer).profile.value

    def _runtime_diagnostics(self) -> dict[str, Any]:
        """Read the public value-only runtime diagnostic mapping once."""
        return dict(self._diagnostics_provider()) if self._diagnostics_provider is not None else {}

    def _definitions(self) -> tuple[ProviderCommand, ...]:
        async def remove_items_safe(queue_id: str, item_ids: list[str]) -> RemoveFromQueueResult:
            audit = self._guard(
                "fastmcp/queue/remove_items_safe",
                "queues.control",
                Capability.DELETE_QUEUE,
                {"queue_id": queue_id, "item_ids": item_ids},
            )
            return await self._execute_audited(
                audit,
                queue.remove_items_safe(self._mass, queue_id, item_ids),
            )

        async def tail_log(
            lines: int = 200,
            level: str | None = None,
            component_regex: str | None = None,
            search: str | None = None,
            since_seconds: int | None = None,
            before: str | None = None,
            name: str = "musicassistant.log",
        ) -> LogTailResult:
            audit = self._guard("fastmcp/debug/tail_log", "system.read", Capability.DEBUG_LOGS)
            return await self._execute_audited(
                audit,
                debug.tail_log(
                    self._mass,
                    lines=lines,
                    level=level,
                    component_regex=component_regex,
                    search=search,
                    since_seconds=since_seconds,
                    before=before,
                    name=name,
                ),
            )

        async def log_stats(
            since_seconds: int | None = None,
            name: str = "musicassistant.log",
        ) -> LogStatsResult:
            audit = self._guard("fastmcp/debug/log_stats", "system.read", Capability.DEBUG_LOGS)
            return await self._execute_audited(
                audit,
                debug.log_stats(self._mass, since_seconds=since_seconds, name=name),
            )

        async def recent_events(
            limit: int = 100,
            event_types: list[str] | None = None,
            id_filter: str | None = None,
            since_seconds: int | None = None,
        ) -> EventSnapshot:
            audit = self._guard(
                "fastmcp/debug/recent_events", "system.read", Capability.DEBUG_EVENTS
            )
            return await self._execute_audited(
                audit,
                debug.recent_events(
                    self._buffer,
                    limit=limit,
                    event_types=event_types,
                    id_filter=id_filter,
                    since_seconds=since_seconds,
                ),
            )

        async def event_buffer_stats() -> EventBufferStats:
            audit = self._guard(
                "fastmcp/debug/event_buffer_stats", "system.read", Capability.DEBUG_EVENTS
            )
            return await self._execute_audited(audit, debug.event_buffer_stats(self._buffer))

        async def health() -> HealthSummary:
            audit = self._guard("fastmcp/debug/health", "system.read", Capability.DEBUG_PROVIDERS)
            runtime_diagnostics = self._runtime_diagnostics()
            return await self._execute_audited(
                audit,
                debug.health(
                    self._mass,
                    buffer=self._buffer,
                    logs_enabled=self._capability_allowed(Capability.DEBUG_LOGS),
                    dynamic_diagnostics_provider=lambda: runtime_diagnostics,
                    policy_schema_version=int(runtime_diagnostics.get("policy_schema_version", 2)),
                    policy_profile=self._effective_policy_profile(),
                    token_resolution_failures=int(
                        runtime_diagnostics.get("token_resolution_failures", 0)
                    ),
                ),
            )

        async def routes() -> RouteList:
            audit = self._guard("fastmcp/debug/routes", "system.read", Capability.DEBUG_PROVIDERS)
            return await self._execute_audited(audit, debug.routes(self._mass))

        async def packages() -> PackageVersions:
            audit = self._guard("fastmcp/debug/packages", "system.read", Capability.DEBUG_PROVIDERS)
            return await self._execute_audited(audit, debug.packages())

        return (
            ProviderCommand(
                "fastmcp/queue/remove_items_safe",
                remove_items_safe,
                "queues.control",
                str(Capability.DELETE_QUEUE),
            ),
            ProviderCommand(
                "fastmcp/debug/tail_log", tail_log, "system.read", str(Capability.DEBUG_LOGS)
            ),
            ProviderCommand(
                "fastmcp/debug/log_stats", log_stats, "system.read", str(Capability.DEBUG_LOGS)
            ),
            ProviderCommand(
                "fastmcp/debug/recent_events",
                recent_events,
                "system.read",
                str(Capability.DEBUG_EVENTS),
            ),
            ProviderCommand(
                "fastmcp/debug/event_buffer_stats",
                event_buffer_stats,
                "system.read",
                str(Capability.DEBUG_EVENTS),
            ),
            ProviderCommand(
                "fastmcp/debug/health", health, "system.read", str(Capability.DEBUG_PROVIDERS)
            ),
            ProviderCommand(
                "fastmcp/debug/routes", routes, "system.read", str(Capability.DEBUG_PROVIDERS)
            ),
            ProviderCommand(
                "fastmcp/debug/packages", packages, "system.read", str(Capability.DEBUG_PROVIDERS)
            ),
        )
