"""Shared building blocks of the redirect based sign-in flow."""

from __future__ import annotations

import hmac
import ipaddress
import re
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from music_assistant_models.errors import RateLimited

from music_assistant.helpers.pkce import pkce_challenge

# Seconds a sign-in may take, and how many may be pending at once
PENDING_LOGIN_TTL: Final = 600
MAX_PENDING_LOGINS: Final = 100
# How many of the pending sign-ins one source (see source_key) may hold
MAX_PENDING_LOGINS_PER_SOURCE: Final = 10
# Token buckets per source: a burst, then one more per refill interval. Every client behind
# one reverse proxy shares a source, so the bursts cover a whole household signing in at once.
SIGN_IN_START_BURST: Final = 30
SIGN_IN_START_REFILL_SECONDS: Final = 6.0
FAILED_SIGN_IN_BURST: Final = 20
FAILED_SIGN_IN_REFILL_SECONDS: Final = 30.0
# Above this many tracked sources, the ones whose bucket refilled completely are dropped
MAX_TRACKED_SOURCES: Final = 1024
# URL scheme of the Music Assistant mobile app
NATIVE_APP_SCHEME: Final = "musicassistant://"
# base64url (unpadded) SHA-256 digest, the only code challenge form accepted
PKCE_CHALLENGE_RE: Final = re.compile(r"[A-Za-z0-9_-]{43}")
# a PKCE code verifier (RFC 7636: 43 to 128 unreserved characters)
PKCE_VERIFIER_RE: Final = re.compile(r"[A-Za-z0-9._~-]{43,128}")
# an authorization code as identity providers issue them (URL safe characters only)
AUTH_CODE_RE: Final = re.compile(r"[A-Za-z0-9._~-]{1,512}")

type RedirectTarget = Literal["server", "app"]


class AuthTransport(StrEnum):
    """How a client reaches the server."""

    DIRECT = "direct"  # browser or app talks to the server over HTTP(S)
    REMOTE = "remote"  # remote app over the WebRTC data channel
    INGRESS = "ingress"  # Home Assistant ingress (already signed in)


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
    source: str  # see source_key
    return_url: str | None = None
    client_code_challenge: str | None = None  # S256 only


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
        key: str,
        redirect_target: RedirectTarget = "server",
        return_url: str | None = None,
        client_code_challenge: str | None = None,
    ) -> PendingLogin:
        """
        Start a sign-in and return it, with a new state.

        :param provider_id: The id of the login provider the sign-in is for.
        :param transport: How the client that starts the sign-in reaches the server.
        :param redirect_uri: The callback URL sent to the identity provider.
        :param idp_code_verifier: The PKCE code verifier for the identity provider.
        :param key: The source of the client (see source_key). Once it has the most sign-ins
            pending a source may have, its oldest one is dropped to make room.
        :param redirect_target: Whether the browser returns to the server or the remote app.
        :param return_url: The validated URL the client returns to after signing in.
        :param client_code_challenge: The client's PKCE S256 code challenge, if any.
        :raises RateLimited: If too many sign-ins are pending.
        """
        now = time.monotonic()
        # anyone can start a sign-in without logging in, so abandoned ones expire and new
        # ones are refused while the limit is reached, keeping the pending ones valid
        for expired in [state for state, entry in self._pending.items() if entry.expires_at <= now]:
            del self._pending[expired]
        # one source may only replace its own sign-ins, so it can not fill the store alone
        own = [state for state, entry in self._pending.items() if entry.source == key]
        if len(own) >= MAX_PENDING_LOGINS_PER_SOURCE:
            del self._pending[own[0]]
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
            source=key,
            return_url=return_url,
            client_code_challenge=client_code_challenge,
        )
        self._pending[pending.state] = pending
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


class SourceRateLimiter:
    """In-memory token bucket per source: a burst of requests, then a steady rate."""

    def __init__(self, burst: int, refill_seconds: float) -> None:
        """
        Initialize the limiter with full buckets.

        :param burst: How many requests a source may make at once.
        :param refill_seconds: Seconds after which a source may make one more request.
        """
        self._burst = burst
        self._refill_seconds = refill_seconds
        self._buckets: dict[str, tuple[float, float]] = {}  # source: (tokens, monotonic)

    def allows(self, key: str) -> bool:
        """
        Return whether a source may make a request now, without counting one.

        :param key: The source (see source_key).
        """
        return self._tokens(key, time.monotonic()) >= 1

    def take(self, key: str) -> bool:
        """
        Count a request from a source and return whether it was allowed.

        :param key: The source (see source_key).
        """
        now = time.monotonic()
        tokens = self._tokens(key, now)
        allowed = tokens >= 1
        self._buckets[key] = (tokens - 1 if allowed else tokens, now)
        if len(self._buckets) > MAX_TRACKED_SOURCES:
            # a full bucket is the same as no bucket, so dropping one forgets nothing
            for full in [k for k in self._buckets if self._tokens(k, now) >= self._burst]:
                del self._buckets[full]
        return allowed

    def _tokens(self, key: str, now: float) -> float:
        """
        Return the tokens a source has at a moment.

        :param key: The source.
        :param now: The moment, in monotonic seconds.
        """
        if (bucket := self._buckets.get(key)) is None:
            return float(self._burst)
        tokens, updated = bucket
        return min(float(self._burst), tokens + (now - updated) / self._refill_seconds)


def source_key(peer_address: str | None, remote_session_id: str | None = None) -> str:
    """
    Return the source that sign-in limits count a client's requests against.

    :param peer_address: The address of the socket peer; never a forwarded header.
    :param remote_session_id: The WebRTC session id of a verified Remote Access connection.
    """
    if remote_session_id:
        return f"remote:{remote_session_id}"
    if not peer_address:
        return "unknown"
    try:
        address = ipaddress.ip_address(peer_address.split("%", 1)[0])
    except ValueError:
        return f"peer:{peer_address}"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return f"peer:{address.ipv4_mapped}"
        # one host usually holds a whole /64, so a single address says little
        return f"peer:{ipaddress.IPv6Network((address, 64), strict=False)}"
    return f"peer:{address}"


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """
    Return whether a code verifier matches a PKCE S256 code challenge.

    :param code_verifier: The code verifier presented by the client.
    :param code_challenge: The code challenge the client started the sign-in with.
    """
    if not PKCE_VERIFIER_RE.fullmatch(code_verifier):
        return False
    return hmac.compare_digest(pkce_challenge(code_verifier), code_challenge)
