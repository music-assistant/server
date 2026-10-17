"""PKCE (RFC 7636, S256) helpers for OAuth authorization code flows."""

from __future__ import annotations

import base64
import hashlib
import secrets


def generate_pkce_pair() -> tuple[str, str]:
    """Return a new random PKCE (code_verifier, S256 code_challenge) pair."""
    # 96 random bytes encode to 128 characters, the longest verifier RFC 7636 allows
    code_verifier = secrets.token_urlsafe(96)
    return code_verifier, pkce_challenge(code_verifier)


def pkce_challenge(code_verifier: str) -> str:
    """
    Return the PKCE S256 code challenge for a code verifier.

    :param code_verifier: The code verifier the token request will carry.
    """
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
