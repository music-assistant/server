"""Shared building blocks of the redirect based sign-in flow."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import urlparse

from music_assistant_models.errors import MusicAssistantError, RateLimited

from music_assistant.helpers.pkce import pkce_challenge

if TYPE_CHECKING:
    from aiohttp import web
    from music_assistant_models.auth import AuthProviderType

# Seconds a sign-in may take, and how many may be pending at once
PENDING_LOGIN_TTL: Final = 600
MAX_PENDING_LOGINS: Final = 100
# Seconds a one-time code of a finished sign-in stays valid, and how many may be open at once
AUTHORIZATION_CODE_TTL: Final = 60
MAX_AUTHORIZATION_CODES: Final = 100
# The longest client_state a sign-in echoes back to its return URL
MAX_CLIENT_STATE_LENGTH: Final = 256
# URL scheme of the Music Assistant mobile app
NATIVE_APP_SCHEME: Final = "musicassistant://"
# base64url (unpadded) SHA-256 digest, the only code challenge form accepted
PKCE_CHALLENGE_RE: Final = re.compile(r"[A-Za-z0-9_-]{43}")
# a PKCE code verifier (RFC 7636: 43 to 128 unreserved characters)
PKCE_VERIFIER_RE: Final = re.compile(r"[A-Za-z0-9._~-]{43,128}")
# an authorization code as identity providers issue them (URL safe characters only)
AUTH_CODE_RE: Final = re.compile(r"[A-Za-z0-9._~-]{1,512}")

type RedirectTarget = Literal["server", "app"]
type LoginPurpose = Literal["login", "link", "test"]


class AuthTransport(StrEnum):
    """How a client reaches the server."""

    DIRECT = "direct"  # browser or app talks to the server over HTTP(S)
    REMOTE = "remote"  # remote app over the WebRTC data channel
    INGRESS = "ingress"  # Home Assistant ingress (already signed in)


# How the client of the current HTTP request or websocket command reaches the server, and the
# origin (scheme and host) it says it used, which is untrusted until checked against known hosts
current_auth_transport: ContextVar[AuthTransport] = ContextVar(
    "current_auth_transport", default=AuthTransport.DIRECT
)
current_auth_origin: ContextVar[str | None] = ContextVar("current_auth_origin", default=None)


@dataclass
class PendingLogin:
    """A sign-in that was started and waits for the browser to return."""

    state: str  # "w.<token>" (web) or "n.<token>" (native app)
    provider_id: str
    transport: AuthTransport
    redirect_uri: str  # exactly what was sent to the identity provider
    redirect_target: RedirectTarget
    expires_at: float  # monotonic
    idp_code_verifier: str
    purpose: LoginPurpose = "login"
    link_user_id: str | None = None  # purpose link: the user who started it
    nonce: str | None = None
    return_url: str | None = None
    client_code_challenge: str | None = None  # S256 only
    client_state: str | None = None
    device_name: str | None = None
    provider_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthorizationCode:
    """A finished sign-in, waiting for its client to exchange the one-time code."""

    state: str
    user_id: str
    purpose: LoginPurpose
    client_code_challenge: str  # S256 only
    expires_at: float  # monotonic
    device_name: str | None = None
    identity_id: str | None = None  # purpose link: the identity that was linked


@dataclass
class ExternalIdentity:
    """A verified identity that an auth provider returns at the end of a sign-in."""

    provider_type: AuthProviderType
    issuer: str
    subject: str
    email: str | None = None
    email_verified: bool = False
    username: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None  # https only
    groups: list[str] = field(default_factory=list)
    claims: dict[str, Any] = field(default_factory=dict)  # snapshot, size-capped


class LoginFlowError(MusicAssistantError):
    """A sign-in that cannot be completed, with a translation key that tells the user why."""

    def __init__(self, translation_key: str, message: str | None = None) -> None:
        """
        Initialize the error.

        :param translation_key: The translation key of the reason shown to the user.
        :param message: Optional message for the log, the translation key if omitted.
        """
        super().__init__(message or translation_key, translation_key=translation_key)


class PendingLoginStore:
    """In-memory store of the pending sign-ins, each usable once."""

    def __init__(self) -> None:
        """Initialize an empty store."""
        self._pending: dict[str, PendingLogin] = {}

    def start(
        self,
        provider_id: str,
        transport: AuthTransport,
        redirect_uri: str,
        idp_code_verifier: str,
        *,
        redirect_target: RedirectTarget = "server",
        return_url: str | None = None,
        client_code_challenge: str | None = None,
        purpose: LoginPurpose = "login",
        client_state: str | None = None,
        device_name: str | None = None,
    ) -> PendingLogin:
        """
        Start a sign-in and return it, with a new state.

        :param provider_id: The id of the login provider the sign-in is for.
        :param transport: How the client that starts the sign-in reaches the server.
        :param redirect_uri: The callback URL sent to the identity provider.
        :param idp_code_verifier: The PKCE code verifier for the identity provider.
        :param redirect_target: Whether the browser returns to the server or the remote app.
        :param return_url: The validated URL the client returns to after signing in.
        :param client_code_challenge: The client's PKCE S256 code challenge, if any.
        :param purpose: What the sign-in is for.
        :param client_state: The client's own state, echoed back to the return URL.
        :param device_name: The device name for the access token of the sign-in.
        :raises RateLimited: If too many sign-ins are pending.
        """
        now = time.monotonic()
        # anyone can start a sign-in without logging in, so abandoned ones expire and new
        # ones are refused while the limit is reached, keeping the pending ones valid
        for expired in [key for key, entry in self._pending.items() if entry.expires_at <= now]:
            del self._pending[expired]
        if len(self._pending) >= MAX_PENDING_LOGINS:
            raise RateLimited("Too many sign-ins are pending")
        prefix = "n" if return_url and return_url.startswith(NATIVE_APP_SCHEME) else "w"
        pending = PendingLogin(
            state=f"{prefix}.{secrets.token_urlsafe(32)}",
            provider_id=provider_id,
            transport=transport,
            redirect_uri=redirect_uri,
            redirect_target=redirect_target,
            expires_at=now + PENDING_LOGIN_TTL,
            idp_code_verifier=idp_code_verifier,
            purpose=purpose,
            return_url=return_url,
            client_code_challenge=client_code_challenge,
            client_state=client_state,
            device_name=device_name,
        )
        self._pending[pending.state] = pending
        return pending

    def get(self, state: str) -> PendingLogin | None:
        """
        Return a pending sign-in without using it, unless it is unknown or expired.

        :param state: The state of the sign-in.
        """
        pending = self._pending.get(state)
        if pending is None or pending.expires_at <= time.monotonic():
            return None
        return pending

    def pop(self, state: str) -> PendingLogin | None:
        """
        Remove a pending sign-in and return it, unless it is unknown or expired.

        :param state: The state of the sign-in.
        """
        pending = self._pending.pop(state, None)
        if pending is None or pending.expires_at <= time.monotonic():
            return None
        return pending

    def drop_provider(self, provider_id: str) -> int:
        """
        Remove the pending sign-ins of a login provider and return how many were removed.

        :param provider_id: The id of the login provider.
        """
        states = [key for key, entry in self._pending.items() if entry.provider_id == provider_id]
        for state in states:
            del self._pending[state]
        return len(states)


class AuthorizationCodeStore:
    """In-memory store of the one-time codes of finished sign-ins, each usable once."""

    def __init__(self) -> None:
        """Initialize an empty store."""
        self._codes: dict[str, AuthorizationCode] = {}

    def issue(
        self,
        state: str,
        user_id: str,
        client_code_challenge: str,
        *,
        purpose: LoginPurpose = "login",
        device_name: str | None = None,
        identity_id: str | None = None,
    ) -> str:
        """
        Issue a one-time code for a finished sign-in and return it.

        :param state: The state the code is bound to.
        :param user_id: The id of the user the sign-in resolved to.
        :param client_code_challenge: The client's PKCE S256 code challenge.
        :param purpose: What the sign-in was for.
        :param device_name: The device name for the access token of the sign-in.
        :param identity_id: For purpose link, the id of the identity that was linked.
        :raises RateLimited: If too many codes are open.
        """
        now = time.monotonic()
        for expired in [key for key, entry in self._codes.items() if entry.expires_at <= now]:
            del self._codes[expired]
        if len(self._codes) >= MAX_AUTHORIZATION_CODES:
            raise RateLimited("Too many sign-ins are waiting to be completed")
        code = secrets.token_urlsafe(32)
        self._codes[_hash_code(code)] = AuthorizationCode(
            state=state,
            user_id=user_id,
            purpose=purpose,
            client_code_challenge=client_code_challenge,
            expires_at=now + AUTHORIZATION_CODE_TTL,
            device_name=device_name,
            identity_id=identity_id,
        )
        return code

    def redeem(self, code: str, state: str, code_verifier: str) -> AuthorizationCode | None:
        """
        Use up a one-time code and return its sign-in, if the state and verifier match.

        Any attempt uses the code up, also one with the wrong state or verifier.

        :param code: The one-time code.
        :param state: The state the client presents with the code.
        :param code_verifier: The client's PKCE code verifier.
        :return: The sign-in of the code, or None when the code is unknown, expired or does
            not match the state or verifier.
        """
        entry = self._codes.pop(_hash_code(code), None)
        if entry is None or entry.expires_at <= time.monotonic():
            return None
        if not hmac.compare_digest(entry.state.encode(), state.encode()):
            return None
        if not verify_pkce(code_verifier, entry.client_code_challenge):
            return None
        return entry


def set_auth_request_context(transport: AuthTransport, origin: str | None) -> None:
    """
    Set how the client of the current request or command reaches the server.

    :param transport: How the client reaches the server.
    :param origin: The origin the client says it used, if any.
    """
    current_auth_transport.set(transport)
    current_auth_origin.set(origin)


def get_request_origin(request: web.Request) -> str | None:
    """
    Return the origin (scheme and host) a client says it used to reach the server.

    The Origin header wins, else the (forwarded) scheme and host of the request. The result
    is untrusted: it must match a known host before it is used.

    :param request: The HTTP request, or the websocket upgrade request.
    """
    if (origin := request.headers.get("Origin")) and origin != "null":
        return origin
    host = _first_header_value(request, "X-Forwarded-Host") or request.host
    scheme = _first_header_value(request, "X-Forwarded-Proto") or request.scheme
    return f"{scheme}://{host}" if host else None


def get_origin_host(origin: str | None) -> str | None:
    """
    Return the host (and any port) of an origin, None when absent or malformed.

    :param origin: The origin (scheme and host).
    """
    if not origin:
        return None
    try:
        return urlparse(origin).netloc or None
    except ValueError:
        return None


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """
    Return whether a code verifier matches a PKCE S256 code challenge.

    :param code_verifier: The code verifier presented by the client.
    :param code_challenge: The code challenge the client started the sign-in with.
    """
    if not PKCE_VERIFIER_RE.fullmatch(code_verifier):
        return False
    return hmac.compare_digest(pkce_challenge(code_verifier), code_challenge)


def _hash_code(code: str) -> str:
    """
    Return the key a one-time code is stored under, so the store never holds a usable code.

    :param code: The one-time code.
    """
    return hashlib.sha256(code.encode()).hexdigest()


def _first_header_value(request: web.Request, name: str) -> str | None:
    """
    Return the first value of a header that proxies may send as a comma separated list.

    :param request: The HTTP request.
    :param name: The name of the header.
    """
    value = request.headers.get(name, "").split(",")[0].strip()
    return value or None
