"""OIDC key rotation recovery, signature verification and refresh throttling."""

from __future__ import annotations

import asyncio
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from music_assistant_models.errors import AuthenticationFailed

from music_assistant.controllers.webserver.helpers.oidc_provider import OIDCLoginProvider

from .test_oidc import IdentityProvider, auth_manager, idp, provider
from .test_oidc_lifecycle import configure, logout_token, sign_in, token_exists

__all__ = ["auth_manager", "idp", "provider"]


@pytest.fixture
async def active_session(
    provider: OIDCLoginProvider, idp: IdentityProvider
) -> tuple[OIDCLoginProvider, str]:
    """Create a tracked session and warm the provider's signing-key cache."""
    configured = configure(provider)
    _, token, _ = await sign_in(configured, idp)
    assert idp.jwks_requests == 1
    return configured, token


@pytest.mark.parametrize("operation", ["sign_in", "logout"])
@pytest.mark.parametrize("omit_kid", [False, True])
async def test_rotation_recovers_with_unchanged_or_missing_key_id(
    active_session: tuple[OIDCLoginProvider, str],
    idp: IdentityProvider,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    omit_kid: bool,
) -> None:
    """A cached key cannot block a valid sign-in or leave a logged-out session active."""
    configured, session_token = active_session
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signed = _signed_token(idp, operation, omit_kid=omit_kid)
    if operation == "sign_in":
        monkeypatch.setattr(idp, "token", lambda: signed)

    await _submit_token(configured, idp, operation, signed)

    assert idp.jwks_requests == 2
    assert idp.discovery_requests == 1
    assert await token_exists(configured, session_token) == (operation == "sign_in")


@pytest.mark.parametrize("operation", ["sign_in", "logout"])
@pytest.mark.parametrize("omit_kid", [False, True])
async def test_concurrent_forged_tokens_cannot_amplify_key_refreshes(
    active_session: tuple[OIDCLoginProvider, str],
    idp: IdentityProvider,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    omit_kid: bool,
) -> None:
    """Forged signatures stay rejected while concurrent requests share one forced fetch."""
    configured, session_token = active_session
    signed = _signed_token(
        idp,
        operation,
        omit_kid=omit_kid,
        key=rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )
    if operation == "sign_in":
        monkeypatch.setattr(idp, "token", lambda: signed)

    results = await asyncio.gather(
        *(_submit_token(configured, idp, operation, signed) for _ in range(6)),
        return_exceptions=True,
    )

    assert all(isinstance(result, AuthenticationFailed) for result in results)
    assert idp.jwks_requests == 2
    assert await token_exists(configured, session_token)


@pytest.mark.parametrize("operation", ["sign_in", "logout"])
@pytest.mark.parametrize("rotate", [False, True])
async def test_claim_validation_stays_required_after_key_refresh(
    active_session: tuple[OIDCLoginProvider, str],
    idp: IdentityProvider,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    rotate: bool,
) -> None:
    """Invalid claims never trigger a refresh or become valid after signature recovery."""
    configured, session_token = active_session
    if rotate:
        idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signed = _signed_token(idp, operation, overrides={"aud": "another-client"})
    if operation == "sign_in":
        monkeypatch.setattr(idp, "token", lambda: signed)

    with pytest.raises(AuthenticationFailed):
        await _submit_token(configured, idp, operation, signed)

    assert idp.jwks_requests == (2 if rotate else 1)
    assert await token_exists(configured, session_token)


@pytest.mark.parametrize("operation", ["sign_in", "logout"])
@pytest.mark.parametrize("key_set", ["missing", "ambiguous"])
async def test_signature_recovery_rejects_unusable_refreshed_keys(
    active_session: tuple[OIDCLoginProvider, str],
    idp: IdentityProvider,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    key_set: str,
) -> None:
    """A refresh must still select exactly one trusted public signing key."""
    configured, session_token = active_session
    public_key = jwt.algorithms.RSAAlgorithm.to_jwk(idp.key.public_key(), as_dict=True)
    public_key.update({"kid": idp.kid, "alg": "RS256", "use": "sig"})
    idp.jwks_override = {"keys": [] if key_set == "missing" else [public_key, public_key]}
    signed = _signed_token(
        idp, operation, key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    if operation == "sign_in":
        monkeypatch.setattr(idp, "token", lambda: signed)

    with pytest.raises(AuthenticationFailed):
        await _submit_token(configured, idp, operation, signed)

    assert idp.jwks_requests == 2
    assert await token_exists(configured, session_token)


def _signed_token(
    idp: IdentityProvider,
    operation: str,
    *,
    omit_kid: bool = False,
    key: rsa.RSAPrivateKey | None = None,
    overrides: dict[str, Any] | None = None,
) -> str:
    """Sign an ID or logout token with optional key and claim fault injection."""
    original = idp.token() if operation == "sign_in" else logout_token(idp)
    claims = jwt.decode(original, options={"verify_signature": False})
    claims.update(overrides or {})
    headers = jwt.get_unverified_header(original)
    if omit_kid:
        headers.pop("kid")
    return jwt.encode(claims, key or idp.key, algorithm="RS256", headers=headers)


async def _submit_token(
    configured: OIDCLoginProvider, idp: IdentityProvider, operation: str, signed: str
) -> None:
    """Submit a token through the provider's normal sign-in or logout validation."""
    if operation == "sign_in":
        await configured.complete_authorization(idp.pending, {"code": "valid-code"})
    else:
        await configured.handle_backchannel_logout(signed)
