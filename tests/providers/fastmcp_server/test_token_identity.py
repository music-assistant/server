"""Bearer-token identity binding and request policy resolution tests."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.fastmcp_server.auth import LOOKUP_FAILURE_CLIENT_ID, MASTokenVerifier
from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.policy import (
    PolicyMode,
    PolicyProfile,
    PolicyResolver,
    PolicySelection,
)
from music_assistant.providers.fastmcp_server.token_identity import (
    AuthenticatedPolicyResolver,
    TokenIdentityRegistry,
)


def test_registry_retains_only_bounded_sha256_fingerprints() -> None:
    """The LRU boundary evicts old bindings and never stores bearer strings."""
    registry = TokenIdentityRegistry(capacity=2)

    registry.bind("raw-bearer-a", user_id="u1", token_id="token-a")
    registry.bind("raw-bearer-b", user_id="u1", token_id="token-b")
    assert registry.lookup("raw-bearer-a") is not None
    registry.bind("raw-bearer-c", user_id="u2", token_id="token-c")

    fingerprints = tuple(registry._entries)
    assert fingerprints == (
        hashlib.sha256(b"raw-bearer-a").hexdigest(),
        hashlib.sha256(b"raw-bearer-c").hexdigest(),
    )
    assert registry.lookup("raw-bearer-b") is None
    assert "raw-bearer" not in repr(registry)
    assert all(
        set(vars(identity)) == {"user_id", "token_id"} for identity in registry._entries.values()
    )


@pytest.mark.asyncio
async def test_verifier_binds_authenticated_ma_token_id(
    mock_mass: MagicMock, mock_user: MagicMock
) -> None:
    """Successful authentication records MA's sanctioned token identity."""
    registry = TokenIdentityRegistry()
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    mock_mass.webserver.auth.get_token_id_from_token = AsyncMock(return_value="token-id-1")
    verifier = MASTokenVerifier(mock_mass, identity_registry=registry)

    result = await verifier.verify_token("secret-bearer")

    assert result is not None
    identity = registry.lookup("secret-bearer")
    assert identity is not None
    assert identity.user_id == "u1"
    assert identity.token_id == "token-id-1"


@pytest.mark.asyncio
async def test_verifier_records_authenticated_legacy_token(
    mock_mass: MagicMock, mock_user: MagicMock
) -> None:
    """MA-confirmed absence of a token ID is retained as a legacy identity."""
    registry = TokenIdentityRegistry()
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    mock_mass.webserver.auth.get_token_id_from_token = AsyncMock(return_value=None)
    verifier = MASTokenVerifier(mock_mass, identity_registry=registry)

    assert await verifier.verify_token("legacy-bearer") is not None
    identity = registry.lookup("legacy-bearer")
    assert identity is not None
    assert identity.token_id is None


@pytest.mark.asyncio
async def test_rejected_token_evicts_stale_identity(mock_mass: MagicMock) -> None:
    """Revocation makes an earlier binding inert as soon as fresh authentication rejects it."""
    registry = TokenIdentityRegistry()
    registry.bind("revoked-bearer", user_id="u1", token_id="revoked-id")
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=None)
    verifier = MASTokenVerifier(mock_mass, identity_registry=registry)

    assert await verifier.verify_token("revoked-bearer") is None
    assert registry.lookup("revoked-bearer") is None


@pytest.mark.asyncio
async def test_token_id_lookup_failure_fails_closed_without_leaking_bearer(
    mock_mass: MagicMock,
    mock_user: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ID lookup outage removes stale identity and logs no secret text."""
    registry = TokenIdentityRegistry()
    registry.bind("do-not-log-this", user_id="u1", token_id="stale-id")
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    mock_mass.webserver.auth.get_token_id_from_token = AsyncMock(
        side_effect=RuntimeError("lookup failed for do-not-log-this")
    )
    verifier = MASTokenVerifier(mock_mass, identity_registry=registry)

    with caplog.at_level("ERROR", logger="music_assistant.providers.fastmcp_server.auth"):
        assert await verifier.verify_token("do-not-log-this") is not None

    assert registry.lookup("do-not-log-this") is None
    assert "do-not-log-this" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["", "   ", " token-id ", 42, object()])
async def test_malformed_lookup_result_uses_lookup_failure_policy(
    malformed: object,
    mock_mass: MagicMock,
    mock_user: MagicMock,
) -> None:
    """Only exact None is legacy; every malformed token ID remains Safe queries."""
    registry = TokenIdentityRegistry()
    registry.bind("bearer", user_id="u1", token_id="stale-id")
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    mock_mass.webserver.auth.get_token_id_from_token = AsyncMock(return_value=malformed)
    verifier = MASTokenVerifier(mock_mass, identity_registry=registry)
    policies = PolicyResolver(default=PolicySelection.profile(PolicyProfile.TRUSTED))
    resolver = AuthenticatedPolicyResolver(registry, policies)

    access_token = await verifier.verify_token("bearer")

    assert access_token is not None
    assert access_token.client_id == LOOKUP_FAILURE_CLIENT_ID
    assert registry.lookup("bearer") is None
    assert resolver.resolve("bearer").profile is PolicyProfile.SAFE_QUERIES


def test_authenticated_policy_resolution_distinguishes_legacy_and_lookup_failure() -> None:
    """Legacy bindings inherit the default while missing bindings are Safe queries."""
    registry = TokenIdentityRegistry()
    registry.bind("legacy", user_id="u1", token_id=None)
    registry.bind("known", user_id="u1", token_id="known-id")
    policies = PolicyResolver(
        default=PolicySelection.profile(PolicyProfile.TRUSTED),
        overrides={"known-id": PolicySelection.profile(PolicyProfile.HOME_CONTROL)},
    )
    resolver = AuthenticatedPolicyResolver(registry, policies)

    assert resolver.resolve("legacy").profile is PolicyProfile.TRUSTED
    assert resolver.resolve("known").profile is PolicyProfile.HOME_CONTROL
    failed = resolver.resolve("lookup-failed")
    assert failed.profile is PolicyProfile.SAFE_QUERIES
    assert failed.mode(Capability.QUERY_LIBRARY) is PolicyMode.ALLOW
    assert failed.mode(Capability.CONTROL_PLAYBACK) is PolicyMode.DENY
