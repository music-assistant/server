"""Tests for ``provider.auth.MASTokenVerifier``."""

from __future__ import annotations

import base64
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.fastmcp_server.auth import MASTokenVerifier


def _make_jwt(payload: dict[str, object]) -> str:
    """
    Forge an unsigned-but-structurally-valid JWT for audience-claim tests.

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
async def test_underlying_exception_swallowed(
    mock_mass: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """
    If MA's auth raises, we log AT ERROR LEVEL and return None — never propagate.

    The log level matters: a refactor that demotes ``LOGGER.exception`` to
    ``LOGGER.debug`` would silently strip operator visibility for auth-system
    outages while keeping the test green. Pinning the captured record's
    level prevents that.
    """
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    verifier = MASTokenVerifier(mock_mass)

    with caplog.at_level(logging.ERROR, logger="music_assistant.providers.fastmcp_server.auth"):
        assert await verifier.verify_token("any") is None

    assert any(
        rec.levelno >= logging.ERROR and "MA token verification raised" in rec.message
        for rec in caplog.records
    ), f"expected an ERROR-level 'MA token verification raised' log; got {caplog.records!r}"


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


@pytest.mark.asyncio
async def test_strict_mode_audience_mismatch_skips_authenticate(
    mock_mass: MagicMock, mock_user: MagicMock
) -> None:
    """
    A token rejected on audience must not reach ``authenticate_with_token``.

    Calling MA's auth refreshes the sliding-window expiry for the token. If
    audience was checked second (after authenticate), an attacker holding a
    valid MA token issued for a different endpoint could keep it alive
    indefinitely by repeatedly hitting the MCP endpoint — even though MCP
    rejects the request. The check now runs first; MA is never consulted.
    """
    mock_mass.webserver.auth.authenticate_with_token = AsyncMock(return_value=mock_user)
    verifier = MASTokenVerifier(mock_mass, public_resource_uri=_RESOURCE, enforce_audience=True)
    token = _make_jwt({"sub": "u1", "aud": "http://other.example/api"})

    assert await verifier.verify_token(token) is None
    mock_mass.webserver.auth.authenticate_with_token.assert_not_awaited()


# ── _extract_jwt_audience: malformed payload paths ───────────────────────────


def _malformed_payload_jwt(raw_payload: bytes) -> str:
    """
    Forge a JWT-shaped token with a non-standard payload segment.

    The payload segment is base64url-encoded (no padding); skipping the JSON
    encoding step lets us hand the decoder arbitrary bytes to exercise the
    error paths (binascii decode failure, non-object JSON, etc.).
    """
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(raw_payload).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def _payload_jwt(payload: object) -> str:
    """Forge a JWT with ``json.dumps(payload)`` as the payload (no claims object guard)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


@pytest.mark.parametrize(
    ("token", "case"),
    [
        # Not three dot-separated segments → not a JWT at all → None.
        ("no.dots", "fewer than 3 segments"),
        ("a.b.c.d", "more than 3 segments"),
        ("plain-token-with-no-dots", "no dots at all (legacy hash form)"),
        # Three segments but the payload isn't valid base64.
        ("hdr.!!not-base64!!.sig", "payload is not valid base64url"),
        # Three segments, payload decodes to bytes that aren't valid UTF-8.
        (_malformed_payload_jwt(b"\xff\xfe\xfd"), "payload is not valid UTF-8"),
        # Three segments, payload IS JSON but not an object — must not crash.
        (_payload_jwt([1, 2, 3]), "payload is a JSON array, not an object"),
        (_payload_jwt("just a string"), "payload is a JSON string, not an object"),
        # ``aud`` is present but the wrong type (int) — must not crash either.
        (_payload_jwt({"sub": "u1", "aud": 42}), "aud claim is an int"),
        (_payload_jwt({"sub": "u1", "aud": {"weird": "shape"}}), "aud claim is a dict"),
    ],
)
def test_extract_jwt_audience_malformed_returns_none(token: str, case: str) -> None:
    """
    Every malformed-JWT path collapses to ``None`` — never an unhandled raise.

    Verified against ``provider.auth._extract_jwt_audience`` so a refactor
    that broadens the try/except boundary (or worse, lets the exception
    propagate to ``verify_token``) is caught.
    """
    from music_assistant.providers.fastmcp_server.auth import _extract_jwt_audience  # noqa: PLC0415

    assert _extract_jwt_audience(token) is None, case
