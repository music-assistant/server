"""Request-bound authorization regressions for native MCP resources."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ResourceError
from fastmcp.server.auth.auth import AccessToken
from music_assistant_models.auth import Scope

from music_assistant.providers.fastmcp_server.audit import AuditRecord
from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.policy import (
    PolicyMode,
    PolicyProfile,
    policy_snapshot,
)
from music_assistant.providers.fastmcp_server.resource_authorization import ResourceAuthorizer
from music_assistant.providers.fastmcp_server.token_identity import TokenIdentityRegistry


def _user(**changes: Any) -> Any:
    values = {
        "user_id": "user-1",
        "enabled": True,
        "role": "user",
        "player_filter": ["player-1"],
        "provider_filter": ["spotify--1"],
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _policy(**modes: PolicyMode):
    return policy_snapshot(PolicyProfile.CUSTOM, modes)


def _authorizer(
    *,
    user: Any | None = None,
    resolved_id: str = "token-id",
    scope: bool = True,
    audits: list[AuditRecord] | None = None,
    checked_scopes: list[Scope] | None = None,
) -> ResourceAuthorizer:
    bearer = AccessToken(token="bearer", client_id="token-id", scopes=[])
    identities = TokenIdentityRegistry()
    identities.bind("bearer", user_id="user-1", token_id="token-id")
    mass = SimpleNamespace(
        webserver=SimpleNamespace(
            auth=SimpleNamespace(
                authenticate_with_token=AsyncMock(return_value=user or _user()),
                get_token_id_from_token=AsyncMock(return_value=resolved_id),
            )
        )
    )
    records = audits if audits is not None else []

    def check_scope(_user: Any, checked: Scope) -> bool:
        if checked_scopes is not None:
            checked_scopes.append(checked)
        return scope

    return ResourceAuthorizer(
        mass,
        auth_required_provider=lambda: True,
        token_provider=lambda: bearer,
        identity_provider=identities.lookup,
        policy_provider=lambda _token: _policy(
            **{
                str(Capability.QUERY_LIBRARY): PolicyMode.ALLOW,
                str(Capability.QUERY_PLAYERS): PolicyMode.ALLOW,
                str(Capability.QUERY_QUEUE): PolicyMode.ALLOW,
            }
        ),
        default_policy_provider=lambda: _policy(),
        scope_checker=check_scope,
        audit_sink=records.append,
    )


@pytest.mark.parametrize(
    ("uri", "tag", "scope"),
    [
        ("library://track/17", Capability.QUERY_LIBRARY, Scope.LIBRARY_READ),
        ("player://player-1", Capability.QUERY_PLAYERS, Scope.PLAYERS_READ),
        ("queue://player-1", Capability.QUERY_QUEUE, Scope.QUEUES_READ),
    ],
)
async def test_resource_authorization_uses_fresh_exact_identity_and_ma_scope(
    uri: str, tag: Capability, scope: Scope
) -> None:
    """Every resource family authenticates the bearer and checks its MA read scope."""
    checked_scopes: list[Scope] = []
    authorizer = _authorizer(scope=False, checked_scopes=checked_scopes)
    with pytest.raises(ResourceError, match="not permitted"):
        await authorizer.authorize(uri, {str(tag)})
    assert authorizer.mass.webserver.auth.authenticate_with_token.await_count == 1
    assert checked_scopes == [scope]


async def test_resource_authorization_rejects_changed_exact_token_identity_once() -> None:
    """A cached concrete URI cannot outlive its exact MA token-ID binding."""
    audits: list[AuditRecord] = []
    authorizer = _authorizer(resolved_id="replacement", audits=audits)
    with pytest.raises(ResourceError, match="Authentication is required"):
        await authorizer.authorize("player://player-1", {str(Capability.QUERY_PLAYERS)})
    assert len(audits) == 1
    assert audits[0].command == "resource:player"
    assert audits[0].capability == str(Capability.QUERY_PLAYERS)
    assert audits[0].mode == "allow"
    assert audits[0].outcome == "authorization.denied"


async def test_resource_policy_allow_to_deny_during_auth_uses_live_denial() -> None:
    """The request policy is resolved only after all authentication awaits."""
    user = _user()
    policies = [
        _policy(**{str(Capability.QUERY_PLAYERS): PolicyMode.ALLOW}),
    ]
    bearer = AccessToken(token="bearer", client_id="token-id", scopes=[])
    identities = TokenIdentityRegistry()
    identities.bind("bearer", user_id="user-1", token_id="token-id")

    async def authenticate_then_revoke(_bearer: str) -> Any:
        policies[0] = _policy(**{str(Capability.QUERY_PLAYERS): PolicyMode.DENY})
        return user

    audits: list[AuditRecord] = []
    authorizer = ResourceAuthorizer(
        SimpleNamespace(
            webserver=SimpleNamespace(
                auth=SimpleNamespace(
                    authenticate_with_token=authenticate_then_revoke,
                    get_token_id_from_token=AsyncMock(return_value="token-id"),
                )
            )
        ),
        auth_required_provider=lambda: True,
        token_provider=lambda: bearer,
        identity_provider=identities.lookup,
        policy_provider=lambda _token: policies[0],
        default_policy_provider=lambda: _policy(),
        scope_checker=lambda _user, _scope: True,
        audit_sink=audits.append,
    )

    with pytest.raises(ResourceError, match="request policy"):
        await authorizer.authorize("player://player-1", {str(Capability.QUERY_PLAYERS)})
    assert len(audits) == 1
    assert audits[0].mode == "deny"


async def test_resource_user_disabled_during_token_id_lookup_is_denied() -> None:
    """Enabled-user state is synchronously sealed after the final identity await."""
    user = _user()
    bearer = AccessToken(token="bearer", client_id="token-id", scopes=[])
    identities = TokenIdentityRegistry()
    identities.bind("bearer", user_id="user-1", token_id="token-id")

    async def lookup_then_disable(_bearer: str) -> str:
        user.enabled = False
        return "token-id"

    authorizer = ResourceAuthorizer(
        SimpleNamespace(
            webserver=SimpleNamespace(
                auth=SimpleNamespace(
                    authenticate_with_token=AsyncMock(return_value=user),
                    get_token_id_from_token=lookup_then_disable,
                )
            )
        ),
        auth_required_provider=lambda: True,
        token_provider=lambda: bearer,
        identity_provider=identities.lookup,
        policy_provider=lambda _token: _policy(**{str(Capability.QUERY_PLAYERS): PolicyMode.ALLOW}),
        default_policy_provider=lambda: _policy(),
        scope_checker=lambda _user, _scope: True,
    )

    with pytest.raises(ResourceError, match="Authentication is required"):
        await authorizer.authorize("player://player-1", {str(Capability.QUERY_PLAYERS)})


async def test_unknown_resource_denial_uses_fixed_redacted_audit_fields() -> None:
    """Caller-controlled URI values never enter the resource audit boundary."""
    audits: list[AuditRecord] = []
    authorizer = _authorizer(audits=audits)
    with pytest.raises(ResourceError):
        await authorizer.authorize("secret://caller-value/token", set())
    assert len(audits) == 1
    assert audits[0].command == "resource:unknown"
    assert audits[0].capability == "unknown"
    assert audits[0].mode == "deny"
    assert "caller-value" not in repr(audits)


@pytest.mark.parametrize("uri", ["player://player-2", "queue://player-2"])
async def test_player_and_queue_resources_apply_user_player_filter(uri: str) -> None:
    """Direct player/queue URIs obey the same target upper bound as MA commands."""
    authorizer = _authorizer()
    with pytest.raises(ResourceError, match="not permitted"):
        await authorizer.authorize(
            uri,
            {str(Capability.QUERY_QUEUE if uri.startswith("queue") else Capability.QUERY_PLAYERS)},
        )


async def test_library_resource_provider_filter_hides_foreign_item_and_audits_once() -> None:
    """A fetched library item outside provider_filter is suppressed without leaking values."""
    audits: list[AuditRecord] = []
    authorizer = _authorizer(audits=audits)
    request = await authorizer.authorize("library://track/17", {str(Capability.QUERY_LIBRARY)})
    assert request is not None
    foreign = SimpleNamespace(provider="tidal--2", provider_mappings=set())
    assert request.library_item_allowed(foreign) is False
    assert request.library_item_allowed(foreign) is False
    assert len(audits) == 1
    assert audits[0] == AuditRecord(
        user_id="user-1",
        client_id="token-id",
        command="resource:library",
        capability=str(Capability.QUERY_LIBRARY),
        mode="allow",
        outcome="authorization.denied",
    )


async def test_library_resource_provider_filter_accepts_one_allowed_mapping() -> None:
    """Any exact allowed provider mapping keeps the library item visible."""
    authorizer = _authorizer()
    request = await authorizer.authorize("library://track/17", {str(Capability.QUERY_LIBRARY)})
    assert request is not None
    item = SimpleNamespace(
        provider="library",
        provider_mappings=(SimpleNamespace(provider_instance="spotify--1"),),
    )
    assert request.library_item_allowed(item) is True
