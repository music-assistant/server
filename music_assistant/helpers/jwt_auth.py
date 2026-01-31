"""JWT token helper for Music Assistant authentication.

Future OIDC Support:
- Consuming external OIDC providers (Google, Keycloak, etc.): Can be added without
  changes to token structure. MA would validate external OIDC tokens and issue its
  own JWT tokens (similar to current Home Assistant OAuth flow).

- Acting as OIDC provider for third parties: Would require implementing OAuth2
  refresh token flow with a dedicated /auth/token endpoint for token refresh.
  Short-lived access tokens (15 min) + long-lived refresh tokens would be needed
  for proper OIDC compliance.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any

import jwt

from music_assistant.helpers.datetime import utc

if TYPE_CHECKING:
    from music_assistant_models.auth import User


class JWTHelper:
    """Helper class for JWT token operations."""

    def __init__(self, secret_key: str) -> None:
        """Initialize JWT helper.

        :param secret_key: Secret key for signing JWTs.
        """
        self.secret_key = secret_key
        self.algorithm = "HS256"

    def encode_token(
        self,
        user: User,
        token_id: str,
        token_name: str,
        expires_at: datetime,
        is_long_lived: bool = False,
    ) -> str:
        """Encode a JWT token for a user.

        :param user: User object to create token for.
        :param token_id: Unique token identifier.
        :param token_name: Human-readable token name.
        :param expires_at: Token expiration datetime.
        :param is_long_lived: Whether this is a long-lived token.
        :return: Encoded JWT token string.
        """
        now = utc()
        payload = {
            "sub": user.user_id,
            "jti": token_id,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "username": user.username,
            "role": user.role.value,
            "token_name": token_name,
            "is_long_lived": is_long_lived,
        }

        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)

    def decode_token(self, token: str, verify_exp: bool = True) -> dict[str, Any]:
        """Decode and verify a JWT token.

        :param token: JWT token string to decode.
        :param verify_exp: Whether to verify token expiration.
        :return: Decoded token payload.
        :raises jwt.InvalidTokenError: If token is invalid or expired.
        """
        options = {"verify_exp": verify_exp}
        payload: dict[str, Any] = jwt.decode(
            token,
            self.secret_key,
            algorithms=[self.algorithm],
            options=options,
        )
        return payload

    @staticmethod
    def generate_secret_key() -> str:
        """Generate a secure random secret key for JWT signing.

        :return: Base64-encoded 256-bit random key.
        """
        return secrets.token_urlsafe(32)  # 32 bytes = 256 bits

    def get_token_id(self, token: str) -> str | None:
        """Extract token ID (jti) from JWT without full validation.

        :param token: JWT token string.
        :return: Token ID or None if invalid.
        """
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": False},
            )
            jti = payload.get("jti")
            return str(jti) if jti else None
        except Exception:
            return None
