"""Fresh request authorization and result filtering for native MCP resources."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from contextvars import Token as ContextToken
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastmcp.exceptions import ResourceError
from music_assistant_models.auth import Scope

from .audit import (
    ANONYMOUS_USER_ID,
    NO_TOKEN_CLIENT_ID,
    AuditRecord,
    AuditSink,
    emit_audit_record,
)
from .auth import request_identity_holds
from .capabilities import Capability
from .commands.authorization import normalize_scope
from .policy import PolicyMode, PolicySnapshot
from .target_filters import TargetKind, collection_row_allowed

if TYPE_CHECKING:
    from fastmcp.server.auth.auth import AccessToken

    from .token_identity import TokenIdentity


_SCOPE_BY_TAG = {
    str(Capability.QUERY_LIBRARY): Scope.LIBRARY_READ,
    str(Capability.QUERY_PLAYERS): Scope.PLAYERS_READ,
    str(Capability.QUERY_QUEUE): Scope.QUEUES_READ,
}
_COMMAND_BY_TAG = {
    str(Capability.QUERY_LIBRARY): "resource:library",
    str(Capability.QUERY_PLAYERS): "resource:player",
    str(Capability.QUERY_QUEUE): "resource:queue",
}

_current_resource_request: ContextVar[AuthorizedResourceRequest | None] = ContextVar(
    "mcp_resource_request",
    default=None,
)


@dataclass(slots=True)
class AuthorizedResourceRequest:
    """Fixed request identity retained while one resource handler runs."""

    user: Any
    token: AccessToken | None
    capability: str
    mode: PolicyMode
    command: str
    audit_sink: AuditSink
    _result_denial_audited: bool = field(default=False, init=False)

    def library_item_allowed(self, item: Any) -> bool:
        """Apply the current user's provider upper bound to one fetched item."""
        if item is None or collection_row_allowed(self.user, item, kind=TargetKind.MUSIC_PROVIDER):
            return True
        if not self._result_denial_audited:
            self._result_denial_audited = True
            self._audit("authorization.denied")
        return False

    def _audit(self, outcome: str) -> None:
        self.audit_sink(
            AuditRecord(
                user_id=str(getattr(self.user, "user_id", "") or ANONYMOUS_USER_ID),
                client_id=str(getattr(self.token, "client_id", "") or NO_TOKEN_CLIENT_ID),
                command=self.command,
                capability=self.capability,
                mode=self.mode.value,
                outcome=outcome,
            )
        )


@dataclass(frozen=True, slots=True)
class _AuthenticationEvidence:
    """Awaited MA authentication facts awaiting one synchronous final seal."""

    user: Any
    live_token_id: Any = None
    token_id_lookup_failed: bool = False


class ResourceAuthorizer:
    """Enforce exact-bearer MA and v2 policy bounds for every resource request."""

    def __init__(
        self,
        mass: Any,
        *,
        auth_required_provider: Callable[[], bool],
        token_provider: Callable[[], AccessToken | None],
        identity_provider: Callable[[str], TokenIdentity | None],
        policy_provider: Callable[[str], PolicySnapshot],
        default_policy_provider: Callable[[], PolicySnapshot],
        scope_checker: Callable[[Any, Scope], bool] | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        """Bind live MA authentication, policy, scope, and audit providers."""
        self.mass = mass
        self._auth_required = auth_required_provider
        self._token = token_provider
        self._identity = identity_provider
        self._policy = policy_provider
        self._default_policy = default_policy_provider
        self._scope_checker = scope_checker or self._default_scope_checker
        self._audit_sink = audit_sink or emit_audit_record

    async def authorize(
        self,
        uri: str,
        tags: set[str],
        *,
        audit_denial: bool = True,
    ) -> AuthorizedResourceRequest | None:
        """Return a fresh request seal, or hide/raise after one controlled audit."""
        capability = _resource_capability(tags)
        command = _COMMAND_BY_TAG.get(capability, "resource:unknown")
        token = self._token()
        evidence = await self._authentication_evidence(token) if self._auth_required() else None
        # No authorization-sensitive await is permitted below this point. Re-read
        # every mutable boundary synchronously and resolve the live policy last.
        user = (
            evidence.user
            if evidence is not None and self._authentication_is_valid(token, evidence)
            else None
        )
        access_error = self._ma_denial(uri, capability, user, token)
        # Resolve the request policy after every other live synchronous bound,
        # immediately before constructing the returned request seal.
        policy = self._policy(token.token) if token is not None else self._default_policy()
        mode = policy.mode(capability) if capability in _SCOPE_BY_TAG else PolicyMode.DENY
        request = AuthorizedResourceRequest(
            user=user,
            token=token,
            capability=capability,
            mode=mode,
            command=command,
            audit_sink=self._audit_sink,
        )
        error = (
            "Resource is not permitted by request policy"
            if mode is not PolicyMode.ALLOW
            else access_error
        )
        if error is None:
            return request
        if audit_denial:
            request._audit("authorization.denied")
            raise ResourceError(error)
        return None

    async def _authentication_evidence(
        self,
        token: AccessToken | None,
    ) -> _AuthenticationEvidence:
        """Perform every MA authentication await without sealing mutable state."""
        if token is None:
            return _AuthenticationEvidence(None)
        try:
            user = await self.mass.webserver.auth.authenticate_with_token(token.token)
        except Exception:
            return _AuthenticationEvidence(None)
        if user is None:
            return _AuthenticationEvidence(None)
        try:
            live_token_id = await self.mass.webserver.auth.get_token_id_from_token(token.token)
        except Exception:
            return _AuthenticationEvidence(user, token_id_lookup_failed=True)
        return _AuthenticationEvidence(user, live_token_id=live_token_id)

    def _authentication_is_valid(
        self,
        token: AccessToken | None,
        evidence: _AuthenticationEvidence,
    ) -> bool:
        """Seal enabled user, request bearer, and exact token binding synchronously."""
        user = evidence.user
        if token is None or user is None or getattr(user, "enabled", True) is False:
            return False
        current_token = self._token()
        if (
            current_token is None
            or current_token.token != token.token
            or current_token.client_id != token.client_id
        ):
            return False
        return request_identity_holds(
            token,
            user,
            self._identity(token.token),
            live_token_id=evidence.live_token_id,
            lookup_failed=evidence.token_id_lookup_failed,
        )

    def _ma_denial(
        self,
        uri: str,
        capability: str,
        user: Any,
        token: AccessToken | None,
    ) -> str | None:
        if not self._auth_required():
            return None
        if token is None or user is None:
            return "Authentication is required"
        scope = _SCOPE_BY_TAG.get(capability)
        normalized = normalize_scope(scope)
        if normalized is None or not self._scope_checker(user, normalized):
            return "Resource is not permitted for the current user"
        if capability in {str(Capability.QUERY_PLAYERS), str(Capability.QUERY_QUEUE)}:
            target = _resource_target(uri)
            allowed = getattr(user, "player_filter", None)
            if (
                not _is_admin(user)
                and isinstance(allowed, list | tuple | set | frozenset)
                and allowed
                and target not in {str(value) for value in allowed}
            ):
                return "Resource target is not permitted for the current user"
        return None

    @staticmethod
    def _default_scope_checker(user: Any, scope: Scope) -> bool:
        from music_assistant.controllers.webserver.helpers.auth_middleware import (  # noqa: PLC0415
            has_scope,
        )

        return bool(has_scope(user, scope))


def bind_resource_request(
    request: AuthorizedResourceRequest,
) -> ContextToken[AuthorizedResourceRequest | None]:
    """Expose one authorized request only while its resource handler runs."""
    return _current_resource_request.set(request)


def reset_resource_request(token: ContextToken[AuthorizedResourceRequest | None]) -> None:
    """Restore the prior task-local resource request."""
    _current_resource_request.reset(token)


def current_resource_request() -> AuthorizedResourceRequest | None:
    """Return the task-local request seal for result filtering."""
    return _current_resource_request.get()


def _resource_capability(tags: set[str]) -> str:
    return next((tag for tag in sorted(tags) if tag in _SCOPE_BY_TAG), "unknown")


def _resource_target(uri: str) -> str:
    parsed = urlsplit(uri)
    return parsed.netloc or parsed.path.strip("/").split("/", 1)[0]


def _is_admin(user: Any) -> bool:
    return str(getattr(user, "role", "")).casefold() == "admin"
