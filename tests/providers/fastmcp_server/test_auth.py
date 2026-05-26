"""Tests for ``provider.auth.MASTokenVerifier``."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.fastmcp_server.auth import MASTokenVerifier


def _make_jwt(payload: dict[str, object]) -> str:
    """Forge an unsigned-but-structurally-valid JWT for audience-claim tests.

    The signature isn't checked by ``MASTokenVerifier`` (verification is MA's
    job); we only inspect the payload's ``aud`` claim.
    """
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.signature"


@pytest.mark.asyncio
async def test_valid_token_returns_access_token(mock_mass: MagicMock, mock_user: MagicMock) -> None:
    """A valid token yields an AccessToken bound to the canonical resource URI."""
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    verifier = MASTokenVerifier(
        mock_mass,
        base_url="http://localhost:8095",
        public_resource_uri="http://localhost:8095/mcp/v1",
    )

    token = await verifier.verify_token("valid-token")

    assert token is not None
    assert token.client_id == "u1"
    assert token.scopes == []
    assert token.resource == "http://localhost:8095/mcp/v1"
    assert token.token == "valid-token"


@pytest.mark.asyncio
async def test_invalid_token_returns_none(mock_mass: MagicMock) -> None:
    """An invalid (rejected) token returns None."""
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=None)
    verifier = MASTokenVerifier(mock_mass)
    assert await verifier.verify_token("nope") is None


@pytest.mark.asyncio
async def test_disabled_user_returns_none(mock_mass: MagicMock, mock_user: MagicMock) -> None:
    """A user marked disabled is rejected even if the token is valid."""
    mock_user.enabled = False
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    verifier = MASTokenVerifier(mock_mass)
    assert await verifier.verify_token("valid-but-disabled") is None


@pytest.mark.asyncio
async def test_authenticate_called_once(mock_mass: MagicMock, mock_user: MagicMock) -> None:
    """We delegate exactly once per verify_token call (no retry storm)."""
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    verifier = MASTokenVerifier(mock_mass)
    await verifier.verify_token("t")
    mock_mass.webserver.auth.authenticate_with_token.assert_awaited_once_with("t")


@pytest.mark.asyncio
async def test_underlying_exception_swallowed(mock_mass: MagicMock) -> None:
    """If MA's auth raises, we log and return None — never propagate."""
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    verifier = MASTokenVerifier(mock_mass)
    assert await verifier.verify_token("any") is None


# ── audience binding (C6) ────────────────────────────────────────────────────


_RESOURCE = "http://localhost:8095/mcp/v1"


@pytest.mark.asyncio
async def test_legacy_token_passes_in_soft_mode(mock_mass: MagicMock, mock_user: MagicMock) -> None:
    """Non-JWT (legacy hash) tokens have no aud; soft mode accepts them."""
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    verifier = MASTokenVerifier(mock_mass, public_resource_uri=_RESOURCE, enforce_audience=False)
    assert await verifier.verify_token("legacy-hash-token") is not None


@pytest.mark.asyncio
async def test_legacy_token_rejected_in_strict_mode(
    mock_mass: MagicMock, mock_user: MagicMock
) -> None:
    """Strict mode rejects tokens that have no audience claim at all."""
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    verifier = MASTokenVerifier(mock_mass, public_resource_uri=_RESOURCE, enforce_audience=True)
    assert await verifier.verify_token("legacy-hash-token") is None


@pytest.mark.asyncio
async def test_jwt_with_matching_aud_accepted_in_strict_mode(
    mock_mass: MagicMock, mock_user: MagicMock
) -> None:
    """A JWT carrying ``aud == public_resource_uri`` passes strict enforcement."""
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    verifier = MASTokenVerifier(mock_mass, public_resource_uri=_RESOURCE, enforce_audience=True)
    token = _make_jwt({"sub": "u1", "aud": _RESOURCE})
    assert await verifier.verify_token(token) is not None


@pytest.mark.asyncio
async def test_jwt_with_mismatched_aud_rejected_in_strict_mode(
    mock_mass: MagicMock, mock_user: MagicMock
) -> None:
    """A JWT issued for a different audience is rejected in strict mode."""
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    verifier = MASTokenVerifier(mock_mass, public_resource_uri=_RESOURCE, enforce_audience=True)
    token = _make_jwt({"sub": "u1", "aud": "http://other.example/api"})
    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_jwt_with_aud_list_accepted(mock_mass: MagicMock, mock_user: MagicMock) -> None:
    """RFC 8707 allows ``aud`` to be a list — match is membership."""
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    verifier = MASTokenVerifier(mock_mass, public_resource_uri=_RESOURCE, enforce_audience=True)
    token = _make_jwt({"sub": "u1", "aud": ["http://other.example", _RESOURCE]})
    assert await verifier.verify_token(token) is not None
