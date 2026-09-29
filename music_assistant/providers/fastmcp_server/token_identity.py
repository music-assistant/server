"""Bounded bearer-token identity bindings for request policy resolution."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .policy import PolicyProfile, PolicyResolver, PolicySnapshot, policy_snapshot


@dataclass(frozen=True)
class TokenIdentity:
    """Music Assistant identity associated with one authenticated bearer."""

    user_id: str
    token_id: str | None


class TokenIdentityRegistry:
    """Least-recently-used token identity bindings keyed by SHA-256 only."""

    def __init__(self, capacity: int = 1024, on_change: Callable[[], None] | None = None) -> None:
        """Initialize a registry with a fixed positive entry capacity."""
        if capacity < 1:
            raise ValueError("Token identity registry capacity must be positive")
        self._capacity = capacity
        self._on_change = on_change
        self._entries: OrderedDict[str, TokenIdentity] = OrderedDict()
        self._token_resolution_failures = 0

    @property
    def token_resolution_failures(self) -> int:
        """Return the aggregate count of authoritative token-ID lookup failures."""
        return self._token_resolution_failures

    def record_resolution_failure(self) -> None:
        """Increment the value-free authoritative token-ID failure counter."""
        self._token_resolution_failures += 1

    def bind(self, bearer_token: str, *, user_id: str, token_id: str | None) -> None:
        """Bind one authenticated bearer without retaining its raw value."""
        fingerprint = self._fingerprint(bearer_token)
        identity = TokenIdentity(user_id=user_id, token_id=token_id)
        changed = self._entries.get(fingerprint) != identity
        self._entries[fingerprint] = identity
        self._entries.move_to_end(fingerprint)
        evicted = False
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
            evicted = True
        if (changed or evicted) and self._on_change is not None:
            self._on_change()

    def discard(self, bearer_token: str) -> None:
        """Forget any cached identity for one bearer fingerprint."""
        removed = self._entries.pop(self._fingerprint(bearer_token), None)
        if removed is not None and self._on_change is not None:
            self._on_change()

    def lookup(self, bearer_token: str) -> TokenIdentity | None:
        """Return and refresh one binding by transient bearer input."""
        fingerprint = self._fingerprint(bearer_token)
        identity = self._entries.get(fingerprint)
        if identity is not None:
            self._entries.move_to_end(fingerprint)
        return identity

    def token_ids(self) -> frozenset[str]:
        """Return the currently bound non-legacy Music Assistant token IDs."""
        return frozenset(
            identity.token_id
            for identity in self._entries.values()
            if identity.token_id is not None
        )

    @staticmethod
    def _fingerprint(bearer_token: str) -> str:
        """Return the non-reversible in-memory registry key for a bearer."""
        return hashlib.sha256(bearer_token.encode()).hexdigest()


class AuthenticatedPolicyResolver:
    """Resolve immutable policies from authenticated bearer identity bindings."""

    def __init__(self, identities: TokenIdentityRegistry, policies: PolicyResolver) -> None:
        """Bind the identity registry to one immutable policy resolver."""
        self._identities = identities
        self._policies = policies
        self._lookup_failure = policy_snapshot(PolicyProfile.SAFE_QUERIES)

    @property
    def policies(self) -> PolicyResolver:
        """Return the current immutable token-ID policy resolver."""
        return self._policies

    def replace(self, policies: PolicyResolver) -> None:
        """Atomically replace the immutable resolver used by future requests."""
        self._policies = policies

    def resolve(self, bearer_token: str) -> PolicySnapshot:
        """Resolve one authenticated bearer, failing closed when identity is unknown."""
        identity = self._identities.lookup(bearer_token)
        if identity is None:
            return self._lookup_failure
        return self._policies.resolve(identity.token_id)
