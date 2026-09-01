"""
Token verifier delegating to MA's existing authentication subsystem.

The plugin does not implement JWT decoding or scope checks of its own — this
is intentional. ``mass.webserver.auth.authenticate_with_token`` already handles
both JWT (PR #2891) and legacy hash tokens, and updates the sliding-window
expiry on every successful call. Wiring our own JWT decode here would only
duplicate the work and create two sources of truth.

Passing ``base_url`` upstream to :class:`fastmcp.server.auth.TokenVerifier`
lets FastMCP's built-in ``RequireAuthMiddleware`` populate the
``resource_metadata="…"`` parameter in ``WWW-Authenticate`` headers on
401 responses (RFC 9728 / MCP authorization spec MUST).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import TYPE_CHECKING

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

    from .token_identity import TokenIdentityRegistry

LOGGER = logging.getLogger(__name__)

# Bandit B105: this is a non-secret audit category, never credential material.
LEGACY_TOKEN_CLIENT_ID = "ma-token:legacy"  # nosec B105
LOOKUP_FAILURE_CLIENT_ID = "ma-token:lookup-failed"


def request_identity_holds(
    token: AccessToken,
    user: object,
    identity: object | None,
    *,
    live_token_id: object = None,
    lookup_failed: bool = False,
) -> bool:
    """Return whether one sealed request identity still matches the live user."""
    if user is None or getattr(user, "enabled", True) is False:
        return False
    if identity is None:
        return False
    if str(getattr(user, "user_id", "")) != getattr(identity, "user_id", None):
        return False
    expected = getattr(identity, "token_id", None) or LEGACY_TOKEN_CLIENT_ID
    if token.client_id != expected:
        return False
    if lookup_failed:
        return False
    if live_token_id is None:
        return True
    return live_token_id == getattr(identity, "token_id", None)


def _is_valid_token_id(value: object) -> bool:
    """Return whether a lookup result has MA's non-empty URL-safe token-ID shape."""
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and all(char.isascii() and (char.isalnum() or char in "_-") for char in value)
    )


def _extract_jwt_audience(token: str) -> str | list[str] | None:
    """
    Best-effort decode of a JWT payload to extract the ``aud`` claim.

    Returns ``None`` for non-JWT tokens (legacy MA hash tokens), malformed
    payloads, or JWTs without an ``aud`` claim. Does **not** verify the
    signature — that's MA's responsibility via ``authenticate_with_token``;
    we only read the audience claim to compare against this MCP server's
    canonical URI.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    # Restore base64url padding stripped by JWT spec.
    pad = "=" * (-len(payload_segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_segment + pad)
        claims = json.loads(raw)
    except binascii.Error, ValueError, UnicodeDecodeError:
        return None
    aud = claims.get("aud") if isinstance(claims, dict) else None
    if isinstance(aud, (str, list)) or aud is None:
        return aud
    return None


def _audience_matches(aud: str | list[str] | None, expected: str) -> bool:
    """RFC 8707: token is bound to ``expected`` if its ``aud`` is or contains it."""
    if aud is None:
        return False
    if isinstance(aud, str):
        return aud == expected
    return expected in aud


class MASTokenVerifier(TokenVerifier):
    """Verify Bearer tokens against ``mass.webserver.auth``."""

    def __init__(
        self,
        mass: MusicAssistant,
        *,
        base_url: str | None = None,
        public_resource_uri: str | None = None,
        enforce_audience: bool = False,
        identity_registry: TokenIdentityRegistry | None = None,
    ) -> None:
        """
        Bind the verifier to a MusicAssistant instance.

        :param mass: MusicAssistant instance used to authenticate tokens.
        :param base_url: Public base URL of this MA instance (used by FastMCP
            to build the ``resource_metadata`` URL advertised in 401 responses
            and the ``aud`` claim binding).
        :param public_resource_uri: Canonical URI of the MCP server (the value
            FastMCP will report as ``resource``). Used to populate
            ``AccessToken.resource`` so downstream code can audience-check.
        :param enforce_audience: When ``True``, reject Bearer tokens whose
            ``aud`` claim is missing or does not contain ``public_resource_uri``.
            When ``False`` (default), only logs a warning so operators can
            migrate gracefully once MA-side issues audience-bound tokens.
        :param identity_registry: Shared bounded token-identity registry. A
            private registry is created when omitted.
        """
        # ``base_url`` is optional on TokenVerifier — passing ``None`` is
        # equivalent to not setting it. Forward verbatim so FastMCP can later
        # build the resource_metadata URL from this verifier.
        super().__init__(base_url=base_url)
        self._mass = mass
        self._public_resource_uri = public_resource_uri
        self._enforce_audience = enforce_audience
        if identity_registry is None:
            from .token_identity import TokenIdentityRegistry  # noqa: PLC0415

            identity_registry = TokenIdentityRegistry()
        self._identity_registry = identity_registry

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Validate the bearer token and produce an ``AccessToken`` for FastMCP.

        :param token: Raw bearer token from the ``Authorization`` header.
        :return: ``AccessToken`` if the token is valid and the user is enabled,
            otherwise ``None``.
        """
        # Audience check runs FIRST — it is pure (no side effects) and rejects
        # tokens minted for other MA endpoints in strict mode. Calling
        # ``authenticate_with_token`` first would refresh MA's sliding-window
        # expiry for that token on every MCP request, keeping an attacker's
        # stolen non-MCP token alive indefinitely via the MCP endpoint.
        if not self._check_audience(token):
            self._identity_registry.discard(token)
            return None

        try:
            user = await self._mass.webserver.auth.authenticate_with_token(token)
        except Exception:
            self._identity_registry.discard(token)
            LOGGER.error("MA token verification raised")
            return None

        if user is None or not getattr(user, "enabled", True):
            self._identity_registry.discard(token)
            return None

        client_id = LOOKUP_FAILURE_CLIENT_ID
        try:
            token_id = await self._mass.webserver.auth.get_token_id_from_token(token)
        except Exception:
            self._identity_registry.discard(token)
            self._identity_registry.record_resolution_failure()
            LOGGER.error("MA token identity lookup raised; using Safe queries policy")
        else:
            if token_id is None:
                self._identity_registry.bind(
                    token,
                    user_id=str(getattr(user, "user_id", "")),
                    token_id=None,
                )
                client_id = LEGACY_TOKEN_CLIENT_ID
            elif _is_valid_token_id(token_id):
                self._identity_registry.bind(
                    token,
                    user_id=str(getattr(user, "user_id", "")),
                    token_id=token_id,
                )
                client_id = token_id
            else:
                self._identity_registry.discard(token)
                self._identity_registry.record_resolution_failure()
                LOGGER.error(
                    "MA token identity lookup returned invalid data; using Safe queries policy"
                )

        # MCP SDK's AccessToken pydantic model has no `claims` field — extras
        # are silently dropped — so we don't try to forward username/role here.
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=[],
            expires_at=None,
            resource=self._public_resource_uri,
        )

    def _check_audience(self, token: str) -> bool:
        """
        Return ``True`` if the token's audience is acceptable for this server.

        In soft mode (``enforce_audience=False``) — always returns ``True`` and
        only emits a warning when a JWT's ``aud`` is missing or mismatched.
        In strict mode — rejects tokens missing or with a wrong ``aud``.
        Non-JWT (legacy hash) tokens have no claim to inspect: they pass in
        soft mode and fail in strict mode.
        """
        expected = self._public_resource_uri
        if not expected:
            return True
        aud = _extract_jwt_audience(token)
        if _audience_matches(aud, expected):
            return True
        if self._enforce_audience:
            LOGGER.warning(
                "Rejected token: aud=%r does not match MCP resource URI %r",
                aud,
                expected,
            )
            return False
        if aud is None:
            LOGGER.debug(
                "Token has no `aud` claim; accepting because enforce_audience=False",
            )
        else:
            LOGGER.warning(
                "Token aud=%r does not match resource URI %r; accepting because "
                "enforce_audience=False (set CONF_ENFORCE_AUDIENCE to enforce).",
                aud,
                expected,
            )
        return True
