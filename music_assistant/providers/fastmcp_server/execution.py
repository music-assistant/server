"""Authentication, authorization, execution, audit, and response dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import hashlib
import heapq
import inspect
import json
import re
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_REQUEST, METHOD_NOT_FOUND
from music_assistant_models.auth import AuthProviderType, Scope
from music_assistant_models.translations import TRANSLATION_RESOLVER

from .audit import (
    ANONYMOUS_USER_ID,
    NO_TOKEN_CLIENT_ID,
    AuditRecord,
    AuditSink,
    emit_audit_record,
    is_privileged_capability,
)
from .auth import LEGACY_TOKEN_CLIENT_ID, LOOKUP_FAILURE_CLIENT_ID
from .capabilities import Capability
from .catalog import (
    CatalogFingerprint,
    CatalogSnapshot,
    CatalogView,
    DynamicEntry,
    RequestCatalogContext,
)
from .command_policy import (
    CommandDecision,
    CommandPreflight,
    command_is_hard_denied,
    postflight_command,
    preflight_command,
    resolve_command_policy,
    revalidate_preflight_command_sync,
)
from .command_profiles import (
    COMMAND_PROFILES,
    CommandProfile,
    aliases_by_command,
)
from .commands.authorization import normalize_scope
from .confirmation_context import _dispatcher_confirmation
from .dynamic_serialization import bounded_json_value
from .dynamic_signatures import (
    UnsupportedSignatureError,
    compile_signature,
)
from .errors import ToolFailureCode, tool_failure
from .performance import PerformanceTracker
from .policy import PolicyMode, PolicySnapshot
from .target_filters import enforce_target_filters, filter_collection_result

if TYPE_CHECKING:
    from fastmcp import Context
    from fastmcp.server.auth import AccessToken

    from .token_identity import TokenIdentity

_ALIASES_BY_COMMAND = aliases_by_command()

_COMPACT_ITEMS = 25
_FULL_ITEMS = 200
_COMPACT_BYTES = 12_288
_FULL_BYTES = 65_536
_COMPACT_STRING = 2_048
_FULL_STRING = 8_192
_CALL_TIMEOUT_SECONDS = 60
CATALOG_REVISION = 2

__all__ = [
    "CatalogFingerprint",
    "CatalogSnapshot",
    "CatalogView",
    "DynamicAPIAdapter",
    "DynamicEntry",
    "RequestCatalogContext",
]


def _public_tool_error(exc: ToolError) -> ToolError:
    """Map internal failures to the finite, redacted public vocabulary."""
    message = str(exc)
    if message.startswith("["):
        return exc
    lowered = message.casefold()
    if "authentication" in lowered:
        return tool_failure(ToolFailureCode.AUTHENTICATION_REQUIRED, "Authentication is required")
    if "requires confirmation" in lowered or "require confirmation" in lowered:
        capability_match = re.search(r"Capability '([^']+)'", message)
        supported = {str(capability) for capability in Capability}
        capability = capability_match.group(1) if capability_match else None
        detail = (
            f"Capability {capability!r} requires confirmation; set it to Allow or use an "
            "elicitation-capable client"
            if capability in supported
            else "The operation requires confirmation from an elicitation-capable client"
        )
        return tool_failure(
            ToolFailureCode.CONFIRMATION_REQUIRED,
            detail,
        )
    if "cancelled" in lowered:
        return tool_failure(ToolFailureCode.OPERATION_CANCELLED, "Operation cancelled by user")
    if "timed out" in lowered:
        return tool_failure(ToolFailureCode.EXECUTION_TIMEOUT, "Command execution timed out")
    if "response exceeds" in lowered:
        return tool_failure(ToolFailureCode.RESPONSE_TOO_LARGE, "Response is too large")
    if "not found" in lowered or "not permitted" in lowered or "not allowed" in lowered:
        return tool_failure(
            ToolFailureCode.NOT_FOUND_OR_FORBIDDEN,
            "Tool was not found or is not permitted",
        )
    return tool_failure(ToolFailureCode.EXECUTION_FAILED, "Command execution failed")


@dataclass(frozen=True, slots=True)
class AuthorizedInvocation:
    """One fully authorized native invocation ready for execution."""

    entry: DynamicEntry
    arguments: dict[str, Any]
    auth: tuple[AccessToken, Any] | None
    impersonated_user: Any | None
    preflight: CommandPreflight
    policy: PolicySnapshot


class _InvocationAuthorizationError(ToolError):
    """Carry the actual request seal into the single denial audit path."""

    def __init__(self, message: str, invocation: AuthorizedInvocation) -> None:
        super().__init__(message)
        self.invocation = invocation


@dataclass(slots=True)
class DynamicCatalogDiagnostics:
    """Last live-registry inspection state exposed through debug health."""

    available: bool = False
    registry_type: str = "missing"
    handlers_seen: int = 0
    handlers_visible: int = 0
    incompatible_handlers: tuple[str, ...] = ()
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class _SnapshotDiagnostics:
    """Cached compatibility results from one base snapshot build."""

    available: bool
    registry_type: str
    handlers_seen: int
    handlers_visible: int
    incompatible_handlers: tuple[str, ...]
    last_error: str | None


@dataclass(frozen=True, slots=True)
class _RegistryCapture:
    """One immutable caller-safe view of the live MA command registry."""

    fingerprint: CatalogFingerprint
    registry_type: str
    items: tuple[tuple[str, Any], ...] | None


@dataclass(slots=True)
class _ListReductionCandidate:
    """Mutable heap state for one list in a response-reduction trial."""

    items: list[Any]
    depth: int
    order: int
    active: bool = True
    revision: int = 0


class DynamicAPIAdapter:
    """Discover, authorize and execute MA command handlers at request time."""

    def __init__(
        self,
        mass: Any,
        *,
        auth_required_provider: Callable[[], bool],
        token_provider: Callable[[], AccessToken | None],
        scope_checker: Callable[[Any, Any], bool] | None = None,
        policy_provider: Callable[[str], PolicySnapshot],
        default_policy_provider: Callable[[], PolicySnapshot],
        identity_provider: Callable[[str], TokenIdentity | None] | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        """Initialise the adapter with request-aware policy providers."""
        self.mass = mass
        self._auth_required_provider = auth_required_provider
        self._token_provider = token_provider
        self._scope_checker = scope_checker or self._default_scope_checker
        self._policy_provider = policy_provider
        self._default_policy_provider = default_policy_provider
        self._identity_provider = identity_provider
        self._audit_sink = audit_sink or emit_audit_record
        self._snapshot: CatalogSnapshot | None = None
        self._snapshot_diagnostics: _SnapshotDiagnostics | None = None
        self._snapshot_lock = asyncio.Lock()
        self._diagnostics = DynamicCatalogDiagnostics()
        self._performance = PerformanceTracker()

    def diagnostics(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of dynamic-catalog compatibility."""
        return dataclasses.asdict(self._diagnostics)

    def performance(self) -> dict[str, int | float]:
        """Return bounded latency statistics without mutating catalog diagnostics."""
        return self._performance.summary()

    def record_performance(self, elapsed_ms: float) -> None:
        """Record one discovery or execution operation for health metrics."""
        self._performance.record(elapsed_ms)

    async def base_snapshot(self) -> CatalogSnapshot:
        """Return the compiled snapshot for the current live command registry."""
        capture = self._capture_registry()
        if self._snapshot is not None and self._snapshot.fingerprint == capture.fingerprint:
            return self._snapshot
        async with self._snapshot_lock:
            capture = self._capture_registry()
            if self._snapshot is None or self._snapshot.fingerprint != capture.fingerprint:
                snapshot, diagnostics = self._compile_snapshot(capture)
                self._snapshot = snapshot
                self._snapshot_diagnostics = diagnostics
                self._publish_snapshot_diagnostics()
            return self._snapshot

    async def visible_catalog(self) -> CatalogView:
        """Return a request-filtered view of the current base snapshot."""
        return (await self.catalog_context()).view

    async def catalog_context(self) -> RequestCatalogContext:
        """Capture a request-specific catalog view from exactly one base snapshot."""
        snapshot = await self.base_snapshot()
        require_auth = self._auth_required_provider()
        auth = await self._authentication() if require_auth else None
        if require_auth and auth is None:
            return RequestCatalogContext(snapshot, CatalogView(snapshot.fingerprint, ()))

        user = auth[1] if auth is not None else None
        policy = self._request_policy(auth)
        entries = [
            dataclasses.replace(entry, policy_mode=self._catalog_mode(entry, policy))
            for entry in snapshot.entries
            if (
                auth is None
                or entry.required_scope is None
                or self._scope_is_allowed(user, getattr(entry.handler, "required_scope", None))
            )
            and entry.decision is not None
            and entry.decision.effective_mode(policy) is not PolicyMode.DENY
        ]
        visible = tuple(sorted(entries, key=lambda entry: entry.name))
        return RequestCatalogContext(snapshot, CatalogView(snapshot.fingerprint, visible))

    async def visible_entries(self) -> list[DynamicEntry]:
        """Return canonical commands visible to the current authenticated user."""
        return list((await self.visible_catalog()).entries)

    async def get_visible_entry(self, name: str) -> DynamicEntry | None:
        """Resolve one visible entry by canonical public name."""
        return (await self.visible_catalog()).by_name.get(name)

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        response_mode: str,
        fields: list[str] | None,
        max_items: int | None,
        ctx: Context,
    ) -> dict[str, Any]:
        """Strictly parse, execute and bound one visible MA API command."""
        started = time.perf_counter()
        try:
            return await self._call(
                name,
                arguments,
                response_mode=response_mode,
                fields=fields,
                max_items=max_items,
                ctx=ctx,
            )
        except ToolError as exc:
            raise _public_tool_error(exc) from exc
        finally:
            self.record_performance((time.perf_counter() - started) * 1000)

    async def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        response_mode: str,
        fields: list[str] | None,
        max_items: int | None,
        ctx: Context,
    ) -> dict[str, Any]:
        """Run the internal authorization pipeline before public error mapping."""
        if response_mode not in {"compact", "full"}:
            raise tool_failure(
                ToolFailureCode.INVALID_ARGUMENTS,
                "response_mode must be 'compact' or 'full'",
            )
        entry = await self.get_visible_entry(name)
        if entry is None:
            await self._audit_denied_name(name)
            raise ToolError(f"Tool {name!r} not found or not permitted")
        auth = await self._authentication() if self._auth_required_provider() else None
        if auth is None and self._auth_required_provider():
            await self._audit_denied_name(name)
            raise ToolError("Authentication is required")

        call_arguments = dict(arguments)
        if entry.profile is not None:
            try:
                call_arguments = entry.profile.convert_arguments(call_arguments)
            except ValueError as exc:
                raise tool_failure(
                    ToolFailureCode.INVALID_ARGUMENTS,
                    "Arguments do not match the tool schema",
                ) from exc
        impersonated = call_arguments.pop("user", None) if entry.allow_impersonation else None
        impersonating = bool(impersonated)
        try:
            if entry.compiled_signature is None:
                raise ValueError(f"Tool {entry.name!r} has no compiled signature")
            parsed = entry.compiled_signature.parse(call_arguments)
        except (KeyError, TypeError, ValueError) as exc:
            raise tool_failure(
                ToolFailureCode.INVALID_ARGUMENTS,
                "Arguments do not match the tool schema",
            ) from exc
        if entry.profile is not None:
            for excluded_name in entry.profile.excluded_arguments:
                parsed.pop(excluded_name, None)

        initial_invocation = await self._authorize_call_audited(
            entry,
            auth,
            parsed,
            impersonated=impersonated,
        )
        confirmation_evidence = await self._confirm(
            initial_invocation,
            ctx,
            impersonating=impersonating,
        )
        auth = (
            await self._authentication(revalidate=True) if self._auth_required_provider() else None
        )
        if auth is None and self._auth_required_provider():
            self._audit_invocation(
                initial_invocation,
                "authorization.denied",
                impersonating=impersonating,
            )
            raise ToolError("Authentication is required")
        invocation = await self._authorize_call_audited(
            initial_invocation.entry,
            auth,
            parsed,
            impersonated=impersonated,
        )
        if not self._confirmation_evidence(invocation, impersonating=impersonating).issubset(
            confirmation_evidence
        ):
            confirmation_evidence |= await self._confirm(
                invocation,
                ctx,
                impersonating=impersonating,
            )
            auth = (
                await self._authentication(revalidate=True)
                if self._auth_required_provider()
                else None
            )
            if auth is None and self._auth_required_provider():
                self._audit_invocation(
                    invocation,
                    "authorization.denied",
                    impersonating=impersonating,
                )
                raise ToolError("Authentication is required")
            invocation = await self._authorize_call_audited(
                invocation.entry,
                auth,
                parsed,
                impersonated=impersonated,
            )
        invocation, result = await self._execute_authorized(
            invocation,
            confirmation_evidence,
            impersonated=impersonated,
            impersonating=impersonating,
        )
        translation_token = TRANSLATION_RESOLVER.set(self.mass.translations.get_translation)
        try:
            return self._bounded_envelope(
                name,
                result,
                response_mode=response_mode,
                fields=fields,
                max_items=max_items,
                profile=invocation.entry.profile,
            )
        finally:
            TRANSLATION_RESOLVER.reset(translation_token)

    async def _execute_authorized(
        self,
        invocation: AuthorizedInvocation,
        confirmation_evidence: frozenset[str],
        *,
        impersonated: Any,
        impersonating: bool,
    ) -> tuple[AuthorizedInvocation, Any]:
        """Seal authorization, execute once, and record the controlled outcome."""
        execution_started = False
        try:
            async with asyncio.timeout(_CALL_TIMEOUT_SECONDS):
                try:
                    invocation = await self._finalize_invocation(
                        invocation,
                        impersonated=impersonated,
                    )
                except ToolError as exc:
                    denied_invocation = (
                        exc.invocation
                        if isinstance(exc, _InvocationAuthorizationError)
                        else invocation
                    )
                    self._audit_invocation(
                        denied_invocation,
                        "authorization.denied",
                        impersonating=impersonating,
                    )
                    raise
                if not self._confirmation_evidence(
                    invocation,
                    impersonating=impersonating,
                ).issubset(confirmation_evidence):
                    self._audit_invocation(
                        invocation,
                        "authorization.denied",
                        impersonating=impersonating,
                    )
                    raise ToolError(
                        "Authorization changed to require confirmation; retry the operation"
                    )
                execution_started = True
                result = await self._execute(
                    invocation.entry,
                    invocation.arguments,
                    invocation.auth,
                    invocation.impersonated_user,
                    self._confirmed_capabilities(invocation).intersection(confirmation_evidence),
                )
                result = filter_collection_result(
                    invocation.impersonated_user
                    or (None if invocation.auth is None else invocation.auth[1]),
                    invocation.entry.command,
                    result,
                )
                result = await self._postflight(invocation, result)
        except TimeoutError as exc:
            if execution_started:
                self._audit_execution(invocation, "execution.failed", impersonating=impersonating)
            raise ToolError(f"Command {invocation.entry.command!r} timed out") from exc
        except ToolError:
            if execution_started:
                self._audit_execution(invocation, "execution.failed", impersonating=impersonating)
            raise
        except Exception as exc:
            if execution_started:
                self._audit_execution(invocation, "execution.failed", impersonating=impersonating)
            raise tool_failure(
                ToolFailureCode.EXECUTION_FAILED,
                "Command execution failed",
            ) from exc
        self._audit_execution(invocation, "execution.succeeded", impersonating=impersonating)
        return invocation, result

    def _capture_registry(self) -> _RegistryCapture:
        """Capture the caller-safe subset used by compilation and diagnostics."""
        handlers = getattr(self.mass, "command_handlers", {})
        registry_type = type(handlers)
        registry_kind = (
            f"{'mapping' if isinstance(handlers, Mapping) else 'invalid'}:"
            f"{registry_type.__module__}.{registry_type.__qualname__}"
        )
        if not isinstance(handlers, Mapping):
            return _RegistryCapture(
                fingerprint=self._fingerprint(registry_kind, ()),
                registry_type=registry_type.__name__,
                items=None,
            )
        items = tuple(
            sorted(
                (command, handler)
                for command, handler in handlers.items()
                if not self._command_is_denied(command)
            )
        )
        return _RegistryCapture(
            fingerprint=self._fingerprint(registry_kind, items),
            registry_type=registry_type.__name__,
            items=items,
        )

    @classmethod
    def _fingerprint(
        cls,
        registry_kind: str,
        items: tuple[tuple[str, Any], ...],
    ) -> str:
        """Digest every live descriptor that affects authorization or schemas."""
        descriptors = []
        for command, handler in items:
            target = getattr(handler, "target", None)
            scope = getattr(handler, "required_scope", None)
            profile = COMMAND_PROFILES.get(command)
            descriptors.append(
                {
                    "command": command,
                    "handler_id": id(handler),
                    "target_id": id(target),
                    "target": cls._callable_identity(target),
                    "authenticated": bool(getattr(handler, "authenticated", True)),
                    "required_scope": str(getattr(scope, "value", scope)),
                    "allow_impersonation": bool(getattr(handler, "allow_impersonation", False)),
                    "alias": str(getattr(handler, "alias", False)),
                    "signature": str(getattr(handler, "signature", None)),
                    "type_hints": sorted(
                        (
                            str(name),
                            cls._type_identity(value),
                        )
                        for name, value in (
                            getattr(handler, "type_hints", {}).items()
                            if isinstance(getattr(handler, "type_hints", None), Mapping)
                            else ()
                        )
                    ),
                    "description": cls._description(target, command) if callable(target) else "",
                    "profile": repr(profile),
                }
            )
        payload = json.dumps(
            [CATALOG_REVISION, registry_kind, descriptors],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _callable_identity(value: Any) -> str:
        """Return a stable descriptive identity for one callable."""
        return f"{getattr(value, '__module__', '')}.{getattr(value, '__qualname__', '')}"

    @staticmethod
    def _type_identity(value: Any) -> str:
        """Return stable schema-affecting type metadata."""
        module = getattr(value, "__module__", "")
        qualname = getattr(value, "__qualname__", "")
        return f"{module}.{qualname}" if qualname else repr(value)

    def _compile_snapshot(
        self, capture: _RegistryCapture
    ) -> tuple[CatalogSnapshot, _SnapshotDiagnostics]:
        """Compile the base descriptors and compatibility errors atomically."""
        if capture.items is None:
            return CatalogSnapshot(capture.fingerprint, ()), _SnapshotDiagnostics(
                available=False,
                registry_type=capture.registry_type,
                handlers_seen=0,
                handlers_visible=0,
                incompatible_handlers=(),
                last_error="mass.command_handlers is not a mapping",
            )

        entries: list[DynamicEntry] = []
        incompatible: list[str] = []
        for command, handler in capture.items:
            if not self._handler_is_discoverable(command, handler):
                incompatible.append(str(command))
                continue
            scope = getattr(handler, "required_scope", None)
            profile = COMMAND_PROFILES.get(command)
            decision = resolve_command_policy(command, scope, profile)
            if decision.hard_denied:
                continue
            try:
                entries.append(self._compile_entry(command, handler, decision))
            except UnsupportedSignatureError:
                incompatible.append(str(command))
        incompatible_handlers = tuple(sorted(incompatible))
        diagnostics = _SnapshotDiagnostics(
            available=True,
            registry_type=capture.registry_type,
            handlers_seen=len(capture.items),
            handlers_visible=len(entries),
            incompatible_handlers=incompatible_handlers,
            last_error=(
                f"{len(incompatible)} incompatible handler(s) skipped" if incompatible else None
            ),
        )
        return CatalogSnapshot(
            capture.fingerprint,
            tuple(sorted(entries, key=lambda entry: entry.name)),
        ), diagnostics

    def _publish_snapshot_diagnostics(self) -> None:
        """Expose only caller-independent compatibility diagnostics."""
        diagnostics = self._snapshot_diagnostics
        if diagnostics is None:
            return
        self._diagnostics = DynamicCatalogDiagnostics(
            available=diagnostics.available,
            registry_type=diagnostics.registry_type,
            handlers_seen=diagnostics.handlers_seen,
            handlers_visible=diagnostics.handlers_visible,
            incompatible_handlers=diagnostics.incompatible_handlers,
            last_error=diagnostics.last_error,
        )

    async def _authentication(
        self,
        *,
        revalidate: bool = False,
    ) -> tuple[AccessToken, Any] | None:
        """Resolve the MCP access token to an enabled MA user."""
        token = self._token_provider()
        if token is None:
            return None
        if self._identity_provider is not None:
            try:
                user = await self.mass.webserver.auth.authenticate_with_token(token.token)
            except Exception:
                return None
            if user is None or getattr(user, "enabled", True) is False:
                return None
            identity = self._identity_provider(token.token)
            if identity is not None:
                if str(getattr(user, "user_id", "")) != identity.user_id:
                    return None
                expected_client_id = identity.token_id or LEGACY_TOKEN_CLIENT_ID
                if token.client_id != expected_client_id:
                    return None
                try:
                    live_token_id = await self.mass.webserver.auth.get_token_id_from_token(
                        token.token
                    )
                except Exception:
                    return None
                if live_token_id != identity.token_id:
                    return None
            elif token.client_id == LOOKUP_FAILURE_CLIENT_ID:
                try:
                    await self.mass.webserver.auth.get_token_id_from_token(token.token)
                except Exception:
                    return token, user
                return None
            else:
                return None
            return token, user
        if revalidate:
            try:
                user = await self.mass.webserver.auth.authenticate_with_token(token.token)
            except Exception:
                return None
            if getattr(user, "user_id", None) != token.client_id:
                return None
        else:
            user = self.mass.webserver.auth.get_user(token.client_id)
            if inspect.isawaitable(user):
                user = await user
        if user is None or getattr(user, "enabled", True) is False:
            return None
        return token, user

    @staticmethod
    def _command_is_denied(command: str) -> bool:
        """Return whether a command crosses an intentionally hidden boundary."""
        return command_is_hard_denied(command)

    @classmethod
    def _handler_is_discoverable(cls, command: str, handler: Any) -> bool:
        """Reject aliases, auth boundaries, and transport internals."""
        return bool(
            not cls._command_is_denied(command)
            and getattr(handler, "authenticated", True)
            and not getattr(handler, "alias", False)
            and callable(getattr(handler, "target", None))
            and isinstance(getattr(handler, "signature", None), inspect.Signature)
            and isinstance(getattr(handler, "type_hints", None), Mapping)
        )

    @classmethod
    def _compile_entry(cls, command: str, handler: Any, decision: CommandDecision) -> DynamicEntry:
        """Compile a live MA handler into a catalog entry."""
        scope = getattr(handler, "required_scope", None)
        profile = COMMAND_PROFILES.get(command)
        compiled_signature = compile_signature(
            handler.signature,
            handler.type_hints,
            allow_extra_kwargs=profile.allow_extra_kwargs if profile is not None else False,
        )
        return DynamicEntry(
            name=f"ma_api:{command}",
            command=command,
            description=cls._description(handler.target, command),
            input_schema=cls._entry_input_schema(
                compiled_signature.input_schema,
                profile,
                allow_impersonation=bool(getattr(handler, "allow_impersonation", False)),
            ),
            required_scope=str(getattr(scope, "value", scope)) if scope is not None else None,
            allow_impersonation=bool(getattr(handler, "allow_impersonation", False)),
            handler=handler,
            search_aliases=(
                profile.search_aliases
                if profile is not None
                else _ALIASES_BY_COMMAND.get(command, ())
            ),
            output_schema=compiled_signature.output_schema(),
            annotations=dict(decision.annotations),
            profile=profile,
            compiled_signature=compiled_signature,
            decision=decision,
        )

    @staticmethod
    def _description(target: Callable[..., Any], command: str) -> str:
        """Extract a compact first paragraph from the handler docstring."""
        doc = inspect.getdoc(target) or ""
        paragraph = doc.split("\n\n", 1)[0].replace("\n", " ").strip()
        return paragraph or f"Music Assistant API command {command}."

    @staticmethod
    def _entry_input_schema(
        input_schema: Mapping[str, Any],
        profile: CommandProfile | None,
        *,
        allow_impersonation: bool,
    ) -> dict[str, Any]:
        """Apply provider aliases, exclusions, and impersonation to an input schema."""
        schema = dict(input_schema)
        properties = dict(schema["properties"])
        schema["properties"] = properties
        required = list(schema.get("required", []))
        alias_requirements: list[dict[str, Any]] = []
        if profile is not None:
            for name in profile.excluded_arguments:
                properties.pop(name, None)
            required = [name for name in required if name not in profile.excluded_arguments]
            for alias, canonical in profile.argument_aliases.items():
                canonical_schema = properties.get(canonical)
                if canonical_schema is None:
                    continue
                properties[alias] = {
                    **canonical_schema,
                    "description": f"Compatibility alias for {canonical!r}.",
                }
                if canonical in required:
                    required.remove(canonical)
                    alias_requirements.append(
                        {"anyOf": [{"required": [canonical]}, {"required": [alias]}]}
                    )
        if allow_impersonation:
            properties["user"] = {
                "type": "string",
                "description": "Optional MA user id or username to impersonate.",
            }
        if required:
            schema["required"] = required
        else:
            schema.pop("required", None)
        if alias_requirements:
            schema["allOf"] = alias_requirements
        return schema

    async def _confirm(
        self,
        invocation: AuthorizedInvocation,
        ctx: Context,
        *,
        impersonating: bool = False,
    ) -> frozenset[str]:
        """Confirm each invocation whose effective request policy requires it."""
        evidence = self._confirmation_evidence(invocation, impersonating=impersonating)
        if not evidence:
            return frozenset()
        capability = self._confirmation_capability(invocation)
        prompt = f"Run {invocation.entry.name} using capability {capability}?"
        self._audit_invocation(
            invocation,
            "confirmation.requested",
            capability=capability,
            impersonating=impersonating,
        )
        try:
            await self._confirm_capability(ctx, prompt, capability)
        except NotImplementedError:
            self._audit_invocation(
                invocation,
                "confirmation.unsupported",
                capability=capability,
                impersonating=impersonating,
            )
            raise
        except ToolError as exc:
            outcome = (
                "confirmation.declined"
                if str(exc) == "Operation cancelled by user"
                else "confirmation.unsupported"
            )
            self._audit_invocation(
                invocation,
                outcome,
                capability=capability,
                impersonating=impersonating,
            )
            raise
        self._audit_invocation(
            invocation,
            "confirmation.accepted",
            capability=capability,
            impersonating=impersonating,
        )
        return evidence

    async def _execute(
        self,
        entry: DynamicEntry,
        parsed: dict[str, Any],
        auth: tuple[AccessToken, Any] | None,
        impersonated_user: Any | None,
        confirmed_capabilities: frozenset[str],
    ) -> Any:
        """Execute under MA's own request context and collect generators."""
        context_tokens = self._set_auth_context(auth)
        confirmation_scope = (
            _dispatcher_confirmation(entry.command, confirmed_capabilities)
            if confirmed_capabilities
            else contextlib.nullcontext()
        )
        try:
            if impersonated_user is not None:
                from music_assistant.controllers.webserver.helpers import (  # noqa: PLC0415
                    auth_middleware,
                )

                variable = auth_middleware.impersonated_user
                context_tokens.append((variable, variable.set(impersonated_user)))
            with confirmation_scope:
                result = entry.handler.target(**parsed)
                if inspect.isawaitable(result):
                    result = await result
                if inspect.isasyncgen(result):
                    return await self._collect_generator(result)
                return result
        finally:
            for variable, token in reversed(context_tokens):
                variable.reset(token)

    def _reauthorize_entry(
        self,
        entry: DynamicEntry,
        auth: tuple[AccessToken, Any] | None,
        policy: PolicySnapshot | None = None,
    ) -> DynamicEntry:
        """Repeat live handler, scope, policy and capability checks before execution."""
        handlers = getattr(self.mass, "command_handlers", {})
        handler = handlers.get(entry.command) if isinstance(handlers, Mapping) else None
        if handler is None or handler is not entry.handler:
            raise ToolError(f"Tool {entry.name!r} not found or not permitted")
        if not self._handler_is_discoverable(entry.command, handler):
            raise ToolError(f"Tool {entry.name!r} not found or not permitted")
        if auth is None and self._auth_required_provider():
            raise ToolError("Authentication is required")
        if auth is not None and getattr(auth[1], "enabled", True) is False:
            raise ToolError("Authentication is required")
        if policy is None:
            policy = self._request_policy(auth)
        scope = getattr(handler, "required_scope", None)
        if auth is not None and scope is not None and not self._scope_is_allowed(auth[1], scope):
            raise ToolError(f"Tool {entry.name!r} not found or not permitted")
        profile = COMMAND_PROFILES.get(entry.command)
        decision = resolve_command_policy(entry.command, scope, profile)
        if decision.effective_mode(policy) is PolicyMode.DENY:
            raise ToolError(f"Tool {entry.name!r} not found or not permitted")
        return dataclasses.replace(
            entry,
            annotations=dict(decision.annotations),
            decision=decision,
        )

    async def _preflight(
        self,
        decision: CommandDecision,
        arguments: Mapping[str, Any],
        auth: tuple[AccessToken, Any] | None,
    ) -> CommandPreflight:
        """Run request-dependent policy checks under the current MA auth context."""
        context_tokens = self._set_auth_context(auth)
        try:
            return await preflight_command(
                self.mass,
                decision,
                arguments,
            )
        finally:
            for variable, token in reversed(context_tokens):
                variable.reset(token)

    async def _postflight(self, invocation: AuthorizedInvocation, result: Any) -> Any:
        """Sanitize a native result under the final authorized request context."""
        decision = invocation.entry.decision
        if decision is None:
            raise ToolError(f"Tool {invocation.entry.name!r} not found or not permitted")
        context_tokens = self._set_auth_context(invocation.auth)
        try:
            return await postflight_command(
                self.mass,
                decision,
                invocation.arguments,
                invocation.preflight,
                result,
            )
        finally:
            for variable, token in reversed(context_tokens):
                variable.reset(token)

    async def _authorize_call_audited(
        self,
        entry: DynamicEntry,
        auth: tuple[AccessToken, Any] | None,
        arguments: Mapping[str, Any],
        *,
        impersonated: Any,
    ) -> AuthorizedInvocation:
        """Authorize once and record one controlled denial on failure."""
        try:
            return await self._authorize_call(
                entry,
                auth,
                arguments,
                impersonated=impersonated,
            )
        except ToolError as exc:
            if isinstance(exc, _InvocationAuthorizationError):
                invocation = exc.invocation
                self._audit_invocation(
                    invocation,
                    "authorization.denied",
                    impersonating=impersonated is not None,
                )
                raise
            policy = self._request_policy(auth)
            decision = entry.decision or resolve_command_policy(
                entry.command,
                entry.required_scope,
                entry.profile,
            )
            capability = self._decision_audit_capability(decision, policy)
            self._emit_audit(
                auth,
                command=entry.command,
                capability=capability,
                mode=decision.effective_mode(policy).value,
                outcome="authorization.denied",
            )
            raise

    async def _authorize_call(
        self,
        entry: DynamicEntry,
        auth: tuple[AccessToken, Any] | None,
        arguments: Mapping[str, Any],
        *,
        impersonated: Any,
    ) -> AuthorizedInvocation:
        """Refresh authorization, impersonation, target filters and request preflight."""
        policy = self._request_policy(auth)
        entry = self._reauthorize_entry(entry, auth, policy)
        impersonated_user = (
            await self._resolve_impersonated_user(auth, str(impersonated)) if impersonated else None
        )
        if impersonated_user is not None:
            self._enforce_target_filters(impersonated_user, entry.command, arguments)
        elif auth is not None:
            self._enforce_target_filters(auth[1], entry.command, arguments)
        decision = entry.decision
        if decision is None:
            decision = resolve_command_policy(entry.command, entry.required_scope, entry.profile)
        preflight = await self._preflight(decision, arguments, auth)
        policy = self._request_policy(auth)
        actual_invocation = AuthorizedInvocation(
            entry=entry,
            arguments=dict(arguments),
            auth=auth,
            impersonated_user=impersonated_user,
            preflight=preflight,
            policy=policy,
        )
        try:
            entry = self._reauthorize_entry(entry, auth, policy)
            if impersonated_user is not None:
                if getattr(impersonated_user, "enabled", True) is False:
                    raise ToolError("Unable to impersonate requested user")
                self._enforce_target_filters(impersonated_user, entry.command, arguments)
            elif auth is not None:
                self._enforce_target_filters(auth[1], entry.command, arguments)
            decision = entry.decision or decision
            actual_invocation = dataclasses.replace(actual_invocation, entry=entry)
            if decision.effective_mode(policy, preflight.additional_required) is PolicyMode.DENY:
                denied = self._denied_capability(decision, preflight, policy)
                suffix = f" (requires {denied})" if denied is not None else ""
                raise ToolError(f"Tool {entry.name!r} not found or not permitted{suffix}")
        except ToolError as exc:
            raise _InvocationAuthorizationError(str(exc), actual_invocation) from exc
        return actual_invocation

    async def _finalize_invocation(
        self,
        invocation: AuthorizedInvocation,
        *,
        impersonated: Any,
    ) -> AuthorizedInvocation:
        """Revalidate awaited identities, then synchronously seal authorization."""
        decision = invocation.entry.decision
        if decision is None:
            raise ToolError(f"Tool {invocation.entry.name!r} not found or not permitted")
        preflight = await self._preflight(decision, invocation.arguments, invocation.auth)
        auth, impersonated_user = await self._final_authentication(
            impersonated=impersonated,
        )
        # No authorization-sensitive await is permitted below this point.
        preflight = revalidate_preflight_command_sync(
            self.mass,
            decision,
            invocation.arguments,
            preflight,
        )
        policy = self._request_policy(auth or invocation.auth)
        final_invocation = dataclasses.replace(
            invocation,
            auth=auth or invocation.auth,
            impersonated_user=impersonated_user,
            preflight=preflight,
            policy=policy,
        )
        if auth is None and self._auth_required_provider():
            raise _InvocationAuthorizationError(
                "Authentication is required",
                final_invocation,
            )
        try:
            if not self._authentication_is_still_exact(auth):
                raise ToolError("Authentication is required")
            entry = self._reauthorize_entry(invocation.entry, auth, policy)
            final_invocation = dataclasses.replace(final_invocation, entry=entry)
            if impersonated_user is not None:
                previous_target_id = getattr(invocation.impersonated_user, "user_id", None)
                caller = auth[1] if auth is not None else None
                caller_id = getattr(caller, "user_id", None)
                target_id = getattr(impersonated_user, "user_id", None)
                if (
                    not isinstance(caller_id, str)
                    or not caller_id
                    or not isinstance(target_id, str)
                    or not target_id
                    or target_id != previous_target_id
                    or (
                        caller_id != target_id
                        and not self._scope_is_allowed(caller, Scope.USERS_IMPERSONATE)
                    )
                ):
                    raise ToolError("Unable to impersonate requested user")
                if getattr(impersonated_user, "enabled", True) is False:
                    raise ToolError("Unable to impersonate requested user")
                self._enforce_target_filters(
                    impersonated_user,
                    invocation.entry.command,
                    invocation.arguments,
                )
            elif auth is not None:
                self._enforce_target_filters(
                    auth[1],
                    invocation.entry.command,
                    invocation.arguments,
                )
            decision = entry.decision
            if decision is None:
                raise ToolError(f"Tool {entry.name!r} not found or not permitted")
            if decision.effective_mode(policy, preflight.additional_required) is PolicyMode.DENY:
                denied = self._denied_capability(decision, preflight, policy)
                suffix = f" (requires {denied})" if denied is not None else ""
                raise ToolError(f"Tool {entry.name!r} not found or not permitted{suffix}")
        except ToolError as exc:
            raise _InvocationAuthorizationError(str(exc), final_invocation) from exc
        return final_invocation

    async def _final_authentication(
        self,
        *,
        impersonated: Any,
    ) -> tuple[tuple[AccessToken, Any] | None, Any | None]:
        """
        Refresh the caller, then resolve the target as the authoritative final await.

        MA exposes independent async caller and target reads rather than a
        transactional pair snapshot. Keeping both inside this final authorization
        coroutine, with the target read last, is the strongest available ordering.
        """
        auth = (
            await self._authentication(revalidate=True) if self._auth_required_provider() else None
        )
        if auth is None or not impersonated:
            return auth, None
        target = await self._resolve_impersonated_user(auth, str(impersonated))
        return auth, target

    def _authentication_is_still_exact(
        self,
        auth: tuple[AccessToken, Any] | None,
    ) -> bool:
        """Re-read request-local exact identity synchronously after target lookup."""
        if auth is None:
            return not self._auth_required_provider()
        token, user = auth
        if getattr(user, "enabled", True) is False:
            return False
        current = self._token_provider()
        if current is None or current.token != token.token or current.client_id != token.client_id:
            return False
        if self._identity_provider is None:
            return True
        identity = self._identity_provider(token.token)
        if identity is None:
            return token.client_id == LOOKUP_FAILURE_CLIENT_ID
        expected = identity.token_id or LEGACY_TOKEN_CLIENT_ID
        return str(getattr(user, "user_id", "")) == identity.user_id and token.client_id == expected

    async def _audit_denied_name(self, name: str) -> None:
        """Record a denied canonical name without exposing request inputs."""
        snapshot = await self.base_snapshot()
        entry = snapshot.by_name.get(name)
        token = self._token_provider()
        auth: tuple[AccessToken, Any] | None = None
        if token is not None:
            identity = self._identity_provider(token.token) if self._identity_provider else None
            auth = (token, identity)
        if entry is None or entry.decision is None:
            self._emit_audit(
                auth,
                command="unknown",
                capability="unknown",
                mode=PolicyMode.DENY.value,
                outcome="authorization.denied",
            )
            return
        policy = self._request_policy(auth)
        decision = entry.decision
        self._emit_audit(
            auth,
            command=entry.command,
            capability=self._decision_audit_capability(decision, policy),
            mode=decision.effective_mode(policy).value,
            outcome="authorization.denied",
        )

    def _audit_execution(
        self,
        invocation: AuthorizedInvocation,
        outcome: str,
        *,
        impersonating: bool,
    ) -> None:
        """Record one privileged non-provider execution outcome."""
        if invocation.entry.command.startswith("fastmcp/"):
            return
        capability = self._invocation_audit_capability(invocation)
        if not is_privileged_capability(capability):
            return
        self._audit_invocation(
            invocation,
            outcome,
            capability=capability,
            impersonating=impersonating,
        )

    def _audit_invocation(
        self,
        invocation: AuthorizedInvocation,
        outcome: str,
        *,
        capability: str | None = None,
        impersonating: bool,
    ) -> None:
        """Record one invocation outcome using fixed authorization fields."""
        self._emit_audit(
            invocation.auth,
            command=invocation.entry.command,
            capability=capability or self._invocation_audit_capability(invocation),
            mode=self._invocation_mode(invocation, impersonating=impersonating).value,
            outcome=outcome,
        )

    def _emit_audit(
        self,
        auth: tuple[AccessToken, Any] | None,
        *,
        command: str,
        capability: str,
        mode: str,
        outcome: str,
    ) -> None:
        """Send one value-free record to the configured audit boundary."""
        token, user = auth if auth is not None else (None, None)
        self._audit_sink(
            AuditRecord(
                user_id=str(getattr(user, "user_id", "") or ANONYMOUS_USER_ID),
                client_id=str(getattr(token, "client_id", "") or NO_TOKEN_CLIENT_ID),
                command=command,
                capability=capability,
                mode=mode,
                outcome=outcome,
            )
        )

    @staticmethod
    def _decision_audit_capability(
        decision: CommandDecision,
        policy: PolicySnapshot,
    ) -> str:
        """Choose one deterministic capability relevant to a decision."""
        required = sorted(decision.required_capabilities)
        if required:
            return required[0]
        alternatives = sorted(
            decision.alternative_capabilities,
            key=lambda capability: (
                policy.mode(capability) is PolicyMode.DENY,
                capability,
            ),
        )
        return alternatives[0] if alternatives else "unknown"

    @classmethod
    def _invocation_audit_capability(cls, invocation: AuthorizedInvocation) -> str:
        """Choose one deterministic capability relevant to a final invocation."""
        decision = invocation.entry.decision
        if decision is None:
            return "unknown"
        denied = cls._denied_capability(decision, invocation.preflight, invocation.policy)
        if denied is not None:
            return denied
        required = sorted(decision.required_capabilities | invocation.preflight.additional_required)
        if required:
            return required[0]
        return cls._decision_audit_capability(decision, invocation.policy)

    def _policy(self, token: AccessToken) -> PolicySnapshot:
        """Resolve the immutable policy for the exact current bearer."""
        return self._policy_provider(token.token)

    def _request_policy(self, auth: tuple[AccessToken, Any] | None) -> PolicySnapshot:
        """Resolve exact-bearer policy or the explicit auth-off global default."""
        if auth is not None:
            return self._policy(auth[0])
        return self._default_policy_provider()

    @staticmethod
    def _catalog_mode(entry: DynamicEntry, policy: PolicySnapshot) -> PolicyMode:
        """Return a conservative prompt requirement for every executable path."""
        decision = entry.decision
        if decision is None:
            return PolicyMode.CONFIRM
        mode = decision.effective_mode(policy)
        if entry.allow_impersonation and mode is PolicyMode.ALLOW:
            return PolicyMode.CONFIRM
        if mode is PolicyMode.ALLOW and any(
            policy.mode(capability) is PolicyMode.CONFIRM
            for capability in decision.alternative_capabilities
        ):
            return PolicyMode.CONFIRM
        if (
            decision.secret_capability is not None
            and policy.mode(decision.secret_capability) is PolicyMode.CONFIRM
            and mode is PolicyMode.ALLOW
        ):
            return PolicyMode.CONFIRM
        return mode

    @staticmethod
    def _invocation_mode(
        invocation: AuthorizedInvocation,
        *,
        impersonating: bool,
    ) -> PolicyMode:
        """Resolve fixed, preflight, and impersonation policy for one invocation."""
        decision = invocation.entry.decision
        if decision is None:
            return PolicyMode.DENY
        mode = decision.effective_mode(
            invocation.policy,
            invocation.preflight.additional_required,
        )
        if impersonating and mode is PolicyMode.ALLOW:
            return PolicyMode.CONFIRM
        return mode

    @staticmethod
    def _confirmation_capability(invocation: AuthorizedInvocation) -> str:
        """Name the capability responsible for an invocation confirmation."""
        decision = invocation.entry.decision
        if decision is None:
            return "unknown"
        required = decision.required_capabilities | invocation.preflight.additional_required
        for capability in sorted(required):
            if invocation.policy.mode(capability) is PolicyMode.CONFIRM:
                return capability
        alternatives = sorted(decision.alternative_capabilities)
        if not any(
            invocation.policy.mode(capability) is PolicyMode.ALLOW for capability in alternatives
        ):
            for capability in alternatives:
                if invocation.policy.mode(capability) is PolicyMode.CONFIRM:
                    return capability
        return next(iter(sorted(required)), "impersonation")

    def _confirmation_evidence(
        self,
        invocation: AuthorizedInvocation,
        *,
        impersonating: bool,
    ) -> frozenset[str]:
        """Return the exact confirmation reasons that must have been elicited."""
        if self._invocation_mode(invocation, impersonating=impersonating) is PolicyMode.ALLOW:
            return frozenset()
        evidence = set(self._confirmed_capabilities(invocation))
        if impersonating:
            evidence.add("impersonation")
        if not evidence:
            evidence.add(self._confirmation_capability(invocation))
        return frozenset(evidence)

    @staticmethod
    def _confirmed_capabilities(invocation: AuthorizedInvocation) -> frozenset[str]:
        """Return final confirm-mode capabilities granted to the target invocation."""
        decision = invocation.entry.decision
        if decision is None:
            return frozenset()
        required = decision.required_capabilities | invocation.preflight.additional_required
        confirmed = {
            capability
            for capability in required
            if invocation.policy.mode(capability) is PolicyMode.CONFIRM
        }
        if not any(
            invocation.policy.mode(capability) is PolicyMode.ALLOW
            for capability in decision.alternative_capabilities
        ):
            confirmed.update(
                capability
                for capability in decision.alternative_capabilities
                if invocation.policy.mode(capability) is PolicyMode.CONFIRM
            )
        return frozenset(confirmed)

    @staticmethod
    def _denied_capability(
        decision: CommandDecision,
        preflight: CommandPreflight,
        policy: PolicySnapshot,
    ) -> str | None:
        """Name one denied capability that blocked request-specific authorization."""
        for capability in sorted(preflight.additional_required):
            if policy.mode(capability) is PolicyMode.DENY:
                return capability
        for capability in sorted(decision.required_capabilities):
            if policy.mode(capability) is PolicyMode.DENY:
                return capability
        if decision.alternative_capabilities and all(
            policy.mode(capability) is PolicyMode.DENY
            for capability in decision.alternative_capabilities
        ):
            return next(iter(sorted(decision.alternative_capabilities)))
        return None

    @staticmethod
    async def _confirm_capability(ctx: Context, prompt: str, capability: str) -> None:
        """Elicit one non-persistent confirmation with actionable unsupported errors."""
        try:
            result = await ctx.elicit(prompt, response_type=bool)  # type: ignore[arg-type, unused-ignore]
        except NotImplementedError:
            raise ToolError(
                f"Capability {capability!r} requires confirmation; set it to Allow or use an "
                "elicitation-capable client"
            ) from None
        except McpError as exc:
            if exc.error.code in (INVALID_REQUEST, METHOD_NOT_FOUND):
                raise ToolError(
                    f"Capability {capability!r} requires confirmation; set it to Allow or use an "
                    "elicitation-capable client"
                ) from exc
            raise
        if getattr(result, "action", None) != "accept" or not getattr(result, "data", None):
            raise ToolError("Operation cancelled by user")

    async def _resolve_impersonated_user(
        self,
        auth: tuple[AccessToken, Any] | None,
        requested_user: str,
    ) -> Any:
        """Resolve and authorize an impersonated identity before elicitation."""
        context_tokens = self._set_auth_context(auth)
        try:
            from music_assistant.controllers.webserver.helpers import (  # noqa: PLC0415
                auth_middleware,
            )

            return await auth_middleware.resolve_impersonated_user(
                self.mass,
                AuthProviderType.BUILTIN,
                requested_user,
            )
        except Exception as exc:
            raise ToolError(f"Unable to impersonate requested user: {exc}") from exc
        finally:
            for variable, token in reversed(context_tokens):
                variable.reset(token)

    def _enforce_target_filters(
        self,
        user: Any,
        command: str,
        arguments: Mapping[str, Any],
    ) -> None:
        """Reject direct target identifiers outside the current user's filters."""
        enforce_target_filters(self.mass, user, command, arguments)

    @staticmethod
    def _set_auth_context(
        auth: tuple[AccessToken, Any] | None,
    ) -> list[tuple[Any, Any]]:
        """Set task-local MA authentication context variables."""
        try:
            from music_assistant.controllers.webserver.helpers import (  # noqa: PLC0415
                auth_middleware,
            )
        except ImportError:
            return []
        token, user = auth if auth is not None else (None, None)
        values = {
            "current_user": user,
            "current_token": getattr(token, "token", None),
            "current_client_id": getattr(token, "client_id", None),
        }
        context_tokens: list[tuple[Any, Any]] = []
        for name, value in values.items():
            variable = getattr(auth_middleware, name, None)
            if variable is not None and hasattr(variable, "set"):
                context_tokens.append((variable, variable.set(value)))
        return context_tokens

    @staticmethod
    async def _collect_generator(generator: AsyncGenerator[Any, Any]) -> list[Any]:
        """Collect an API async generator; response bounding happens afterwards."""
        values: list[Any] = []
        try:
            async for value in generator:
                values.append(value)
                if len(values) > _FULL_ITEMS:
                    break
        finally:
            await generator.aclose()
        return values

    @classmethod
    def _bounded_envelope(
        cls,
        name: str,
        result: Any,
        *,
        response_mode: str,
        fields: list[str] | None,
        max_items: int | None,
        profile: CommandProfile | None = None,
    ) -> dict[str, Any]:
        """Return a deterministic, JSON-safe response inside the mode budget."""
        compact = response_mode == "compact"
        item_cap = _COMPACT_ITEMS if compact else _FULL_ITEMS
        if max_items is not None:
            item_cap = max(1, min(item_cap, int(max_items)))
        byte_cap = _COMPACT_BYTES if compact else _FULL_BYTES
        string_cap = _COMPACT_STRING if compact else _FULL_STRING
        normalized = bounded_json_value(
            result,
            item_cap=item_cap,
            string_cap=string_cap,
            max_depth=6 if compact else 12,
        )
        raw = normalized.value
        total_count = normalized.total_count
        if compact and profile is not None:
            raw = profile.project_compact(raw)
        field_string_cap = max(
            [string_cap, *(len(field) for field in fields or [] if isinstance(field, str))]
        )
        normalized_fields = bounded_json_value(
            fields or [],
            item_cap=len(fields) if fields else 1,
            string_cap=field_string_cap,
            max_depth=1,
        ).value
        safe_fields = (
            [field for field in normalized_fields if isinstance(field, str)]
            if isinstance(normalized_fields, list)
            else []
        )
        data = cls._project_fields(raw, safe_fields)
        envelope: dict[str, Any] = {
            "command": name,
            "data": data,
            "truncated": normalized.truncated,
            "returned_count": len(data) if isinstance(data, list) else (0 if data is None else 1),
            "bytes": 0,
            "applied": {
                "mode": response_mode,
                "fields": safe_fields,
                "max_items": item_cap,
            },
        }
        if total_count is not None:
            envelope["total_count"] = total_count
        cls._fit_bytes(envelope, byte_cap)
        cls._set_measured_bytes(envelope)
        if envelope["bytes"] > byte_cap:
            mode = str(envelope["applied"]["mode"])
            raise ToolError(f"Response exceeds the {mode} byte budget")
        return envelope

    @staticmethod
    def _project_fields(value: Any, fields: list[str] | None) -> Any:
        """Retain requested top-level fields from dicts or list items."""
        if not fields:
            return value
        selected = set(fields)
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key in selected}
        if isinstance(value, list):
            return [
                {key: item for key, item in row.items() if key in selected}
                if isinstance(row, dict)
                else row
                for row in value
            ]
        return value

    @classmethod
    def _fit_bytes(cls, envelope: dict[str, Any], byte_cap: int) -> None:
        """Apply the original global list-reduction policy within the byte cap."""
        envelope["bytes"] = byte_cap
        if cls._encoded_size(envelope) <= byte_cap:
            return

        original_data = envelope["data"]
        max_removals = cls._count_list_items(original_data)
        if max_removals:
            envelope["truncated"] = True
            smallest_data = cls._simulate_list_removals(original_data, max_removals)
            envelope["data"] = smallest_data
            cls._set_returned_count(envelope)
            if cls._encoded_size(envelope) <= byte_cap:
                low = 1
                high = max_removals
                best_data = smallest_data
                while low < high:
                    midpoint = (low + high) // 2
                    candidate_data = cls._simulate_list_removals(original_data, midpoint)
                    envelope["data"] = candidate_data
                    cls._set_returned_count(envelope)
                    if cls._encoded_size(envelope) <= byte_cap:
                        high = midpoint
                        best_data = candidate_data
                    else:
                        low = midpoint + 1
                envelope["data"] = best_data
                cls._set_returned_count(envelope)
                return

        envelope["data"] = cls._minimal_json_shape(original_data)
        envelope["truncated"] = True
        cls._set_returned_count(envelope)
        envelope.pop("total_count", None)
        if cls._encoded_size(envelope) <= byte_cap:
            return
        envelope["applied"]["fields"] = []
        if cls._encoded_size(envelope) <= byte_cap:
            return
        mode = str(envelope["applied"]["mode"])
        raise ToolError(f"Response exceeds the {mode} byte budget")

    @classmethod
    def _simulate_list_removals(cls, value: Any, removals: int) -> Any:
        """Return a copy after a bounded number of original-policy list removals."""
        reduced = copy.deepcopy(value)
        candidates: list[_ListReductionCandidate] = []
        candidates_by_id: dict[int, _ListReductionCandidate] = {}
        heap: list[tuple[int, int, int, int, int]] = []

        def collect(item: Any, depth: int) -> None:
            if isinstance(item, list):
                candidate_index = len(candidates)
                candidate = _ListReductionCandidate(item, depth, candidate_index)
                candidates.append(candidate)
                candidates_by_id[id(item)] = candidate
                if item:
                    heap.append(
                        (-len(item), depth, candidate.order, candidate.revision, candidate_index)
                    )
                for child in item:
                    collect(child, depth + 1)
            elif isinstance(item, dict):
                for child in item.values():
                    collect(child, depth + 1)

        def invalidate(item: Any) -> None:
            if isinstance(item, list):
                candidate = candidates_by_id.get(id(item))
                if candidate is not None:
                    candidate.active = False
                    candidate.revision += 1
                for child in item:
                    invalidate(child)
            elif isinstance(item, dict):
                for child in item.values():
                    invalidate(child)

        collect(reduced, 0)
        heapq.heapify(heap)
        removed = 0
        while removed < removals and heap:
            negative_length, _depth, _order, revision, candidate_index = heapq.heappop(heap)
            candidate = candidates[candidate_index]
            if (
                not candidate.active
                or candidate.revision != revision
                or len(candidate.items) != -negative_length
            ):
                continue
            removed_item = candidate.items.pop()
            removed += 1
            invalidate(removed_item)
            candidate.revision += 1
            if candidate.items:
                heapq.heappush(
                    heap,
                    (
                        -len(candidate.items),
                        candidate.depth,
                        candidate.order,
                        candidate.revision,
                        candidate_index,
                    ),
                )
        return reduced

    @classmethod
    def _count_list_items(cls, value: Any) -> int:
        """Return a safe upper bound on logical removals for a JSON tree."""
        if isinstance(value, list):
            return len(value) + sum(cls._count_list_items(item) for item in value)
        if isinstance(value, dict):
            return sum(cls._count_list_items(item) for item in value.values())
        return 0

    @staticmethod
    def _minimal_json_shape(value: Any) -> Any:
        """Return the smallest JSON value retaining the result's top-level type."""
        if isinstance(value, dict):
            return {}
        if isinstance(value, list):
            return []
        if isinstance(value, str):
            return ""
        if isinstance(value, bool):
            return False
        if isinstance(value, int | float):
            return 0
        return None

    @staticmethod
    def _set_returned_count(envelope: dict[str, Any]) -> None:
        """Refresh the envelope's top-level returned item count."""
        data = envelope["data"]
        envelope["returned_count"] = (
            len(data) if isinstance(data, list) else (0 if data is None else 1)
        )

    @staticmethod
    def _encoded_size(value: Any) -> int:
        """Measure the compact UTF-8 JSON representation."""
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        )

    @classmethod
    def _set_measured_bytes(cls, envelope: dict[str, Any]) -> None:
        """Stabilize the self-referential encoded byte count."""
        for _attempt in range(3):
            measured = cls._encoded_size(envelope)
            if envelope["bytes"] == measured:
                return
            envelope["bytes"] = measured

    def _scope_is_allowed(self, user: Any, scope: Any) -> bool:
        """Normalize one MA scope before delegating its authorization decision."""
        normalized = normalize_scope(scope)
        return normalized is not None and bool(self._scope_checker(user, normalized))

    @staticmethod
    def _default_scope_checker(user: Any, scope: Any) -> bool:
        """Delegate authorization to MA's current scope implementation."""
        from music_assistant.controllers.webserver.helpers.auth_middleware import (  # noqa: PLC0415
            has_scope,
        )

        return bool(has_scope(user, scope))
