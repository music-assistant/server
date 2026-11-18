"""Invocation-local proof that MCP elicitation was accepted."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class _ConfirmationGrant:
    """Opaque revocable grant owned by one dispatcher task invocation."""

    marker: object
    owner_task: object
    command: str
    capabilities: frozenset[str]
    active: bool = True


_MARKER = object()
_CURRENT_GRANT: ContextVar[_ConfirmationGrant | None] = ContextVar(
    "mcp_confirmation_grant",
    default=None,
)


@contextmanager
def _dispatcher_confirmation(command: str, capabilities: frozenset[str]) -> Iterator[None]:
    """Scope accepted confirmation capabilities to one target invocation."""
    grant = _ConfirmationGrant(_MARKER, asyncio.current_task(), command, capabilities)
    token = _CURRENT_GRANT.set(grant)
    try:
        yield
    finally:
        grant.active = False
        _CURRENT_GRANT.reset(token)


def capability_was_confirmed(command: str, capability: str) -> bool:
    """Return whether the active dispatcher invocation confirmed a capability."""
    grant = _CURRENT_GRANT.get()
    try:
        current_task = asyncio.current_task()
    except RuntimeError:
        current_task = None
    return bool(
        grant is not None
        and grant.marker is _MARKER
        and grant.active
        and grant.owner_task is current_task
        and grant.command == command
        and capability in grant.capabilities
    )
