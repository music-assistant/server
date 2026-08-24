"""Pure response, revision, and cursor primitives for command discovery."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict, cast

from .catalog import CatalogFingerprint, DynamicEntry

type DiscoveryMode = Literal["search", "catalog"]

SEARCH_DEFAULT_LIMIT = 5
CATALOG_DEFAULT_LIMIT = 25
MAX_PAGE_LIMIT = 50
CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 2048


class DiscoveryItem(TypedDict):
    """One schema-free discovery result."""

    name: str
    description: NotRequired[str]
    schema: NotRequired[dict[str, Any]]
    policy_mode: Literal["allow", "confirm"]


class DiscoveryPage(TypedDict):
    """One stable page returned by the discovery tool."""

    mode: DiscoveryMode
    items: list[DiscoveryItem]
    total: int
    next_cursor: str | None
    catalog_revision: str


@dataclass(frozen=True, slots=True)
class CursorState:
    """Stateless continuation data encoded into an opaque cursor."""

    version: int
    mode: DiscoveryMode
    query: str
    offset: int
    revision: str


class PaginationError(ValueError):
    """Stable pagination failure suitable for tool or resource transport."""

    def __init__(self, code: str, message: str) -> None:
        """Store a stable code alongside the client-facing message."""
        super().__init__(message)
        self.code = code


def normalize_query(value: str | None) -> str:
    """Normalize query text for mode selection and cursor comparison."""
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(normalized.split())


def resolve_limit(mode: DiscoveryMode, limit: int | None) -> int:
    """Return the mode default or validate one strict explicit page size."""
    if limit is None:
        return SEARCH_DEFAULT_LIMIT if mode == "search" else CATALOG_DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise PaginationError("invalid_limit", "limit must be an integer from 1 through 50")
    return limit


def catalog_revision(
    fingerprint: CatalogFingerprint,
    entries: Sequence[DynamicEntry],
) -> str:
    """Digest the registry and caller-visible fields that affect discovery."""
    payload = [
        CURSOR_VERSION,
        fingerprint,
        [
            [entry.name, entry.description, list(entry.search_aliases), entry.policy_mode]
            for entry in entries
        ],
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def encode_cursor(state: CursorState) -> str:
    """Encode validated continuation state as unpadded base64url JSON."""
    _validate_state(state)
    payload = {
        "v": state.version,
        "m": state.mode,
        "q": state.query,
        "o": state.offset,
        "r": state.revision,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise PaginationError("invalid_cursor", "cursor is malformed")
    return cursor


def decode_cursor(cursor: str) -> CursorState:
    """Decode and validate an opaque continuation cursor."""
    if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise PaginationError("invalid_cursor", "cursor is malformed")
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if not isinstance(payload, dict) or set(payload) != {"v", "m", "q", "o", "r"}:
            raise ValueError
        mode = payload["m"]
        if mode not in ("search", "catalog"):
            raise ValueError
        state = CursorState(
            version=payload["v"],
            mode=cast("DiscoveryMode", mode),
            query=payload["q"],
            offset=payload["o"],
            revision=payload["r"],
        )
        _validate_state(state)
    except PaginationError:
        raise
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PaginationError("invalid_cursor", "cursor is malformed") from exc
    return state


def _validate_state(state: CursorState) -> None:
    """Reject state that cannot have been emitted for a next page."""
    valid = (
        type(state.version) is int
        and state.version == CURSOR_VERSION
        and state.mode in ("search", "catalog")
        and isinstance(state.query, str)
        and normalize_query(state.query) == state.query
        and type(state.offset) is int
        and state.offset > 0
        and isinstance(state.revision, str)
        and bool(state.revision)
        and (
            (state.mode == "search" and bool(state.query))
            or (state.mode == "catalog" and not state.query)
        )
    )
    if not valid:
        message = (
            "cursor version is unsupported"
            if type(state.version) is int and state.version != CURSOR_VERSION
            else "cursor is malformed"
        )
        raise PaginationError("invalid_cursor", message)
