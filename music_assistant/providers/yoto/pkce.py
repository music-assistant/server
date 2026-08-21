"""OAuth PKCE helpers for Yoto browser authorization."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import ClientError
from music_assistant_models.errors import LoginFailed

if TYPE_CHECKING:
    from aiohttp import ClientSession

AUTHORIZE_URL = "https://login.yotoplay.com/authorize"
TOKEN_URL = "https://login.yotoplay.com/oauth/token"
AUDIENCE = "https://api.yotoplay.com"
SCOPES = "family:library:view offline_access"


def build_authorization(client_id: str, redirect_uri: str) -> tuple[str, str]:
    """Build a Yoto authorization URL and return it with its secret verifier."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    query = urlencode(
        {
            "audience": AUDIENCE,
            "scope": SCOPES,
            "response_type": "code",
            "client_id": client_id,
            "code_challenge": challenge.rstrip("="),
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
        }
    )
    return f"{AUTHORIZE_URL}?{query}", verifier


def extract_authorization_code(callback_url: str, redirect_uri: str) -> str:
    """Validate a pasted callback URL and return its authorization code."""
    callback = urlsplit(callback_url.strip())
    expected = urlsplit(redirect_uri)
    if (callback.scheme, callback.netloc, callback.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise LoginFailed("Yoto callback URL does not match the registered redirect URL")
    query = parse_qs(callback.query)
    if error := query.get("error"):
        description = query.get("error_description", error)[0]
        raise LoginFailed(f"Yoto authorization was not completed: {description}")
    if not (code := query.get("code", [""])[0]):
        raise LoginFailed("Yoto callback URL contains no authorization code")
    return code


async def exchange_code(
    session: ClientSession,
    client_id: str,
    redirect_uri: str,
    verifier: str,
    callback_url: str,
) -> str:
    """Exchange a pasted callback code and return the rotating refresh token."""
    code = extract_authorization_code(callback_url, redirect_uri)
    try:
        async with session.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code_verifier": verifier,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as response:
            body: Any = await response.json(content_type=None)
            if not response.ok:
                raise LoginFailed("Yoto rejected the authorization code")
    except (ClientError, TimeoutError, ValueError) as err:
        raise LoginFailed("Yoto token exchange failed") from err
    refresh_token = body.get("refresh_token") if isinstance(body, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise LoginFailed("Yoto token response was malformed")
    return refresh_token
