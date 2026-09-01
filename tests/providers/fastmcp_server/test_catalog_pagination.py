"""Tests for pure catalog pagination primitives."""

from __future__ import annotations

import base64
from dataclasses import replace
from typing import cast

import pytest

from music_assistant.providers.fastmcp_server.catalog import DynamicEntry
from music_assistant.providers.fastmcp_server.catalog_pagination import (
    CATALOG_DEFAULT_LIMIT,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    SEARCH_DEFAULT_LIMIT,
    CursorState,
    PaginationError,
    catalog_revision,
    decode_cursor,
    encode_cursor,
    normalize_query,
    resolve_limit,
)


def _entry(
    name: str,
    *,
    description: str = "Description",
    aliases: tuple[str, ...] = (),
) -> DynamicEntry:
    return DynamicEntry(
        name=name,
        command=name.removeprefix("ma_api:"),
        description=description,
        input_schema={"type": "object", "properties": {}},
        required_scope=None,
        allow_impersonation=False,
        handler=object(),
        search_aliases=aliases,
    )


def test_normalize_query_is_unicode_and_whitespace_stable() -> None:
    """Normalize equivalent Unicode and whitespace forms."""
    assert normalize_query("  ALBUM\u00a0 Tracks  ") == "album tracks"
    assert normalize_query("\uff21\uff2c\uff22\uff35\uff2d") == "album"
    assert normalize_query(None) == ""


def test_resolve_limit_uses_mode_defaults_and_bounds() -> None:
    """Choose defaults and retain valid explicit bounds."""
    assert resolve_limit("search", None) == SEARCH_DEFAULT_LIMIT
    assert resolve_limit("catalog", None) == CATALOG_DEFAULT_LIMIT
    assert resolve_limit("catalog", 1) == 1
    assert resolve_limit("search", MAX_PAGE_LIMIT) == MAX_PAGE_LIMIT


@pytest.mark.parametrize("value", [0, 51, -1, True, 1.5, "5"])
def test_resolve_limit_rejects_non_strict_or_out_of_range_values(value: object) -> None:
    """Reject non-integer and out-of-range requested page sizes."""
    with pytest.raises(PaginationError) as exc_info:
        resolve_limit("search", cast("int | None", value))
    assert exc_info.value.code == "invalid_limit"
    assert "1 through 50" in str(exc_info.value)


def test_cursor_round_trips_without_padding() -> None:
    """Encode and decode an opaque unpadded cursor."""
    state = CursorState(
        version=1,
        mode="search",
        query="album tracks",
        offset=5,
        revision="abc123",
    )
    encoded = encode_cursor(state)
    assert "=" not in encoded
    assert decode_cursor(encoded) == state


def test_encode_cursor_rejects_output_beyond_maximum_length() -> None:
    """Reject cursor state that cannot produce a decodable cursor."""
    state = CursorState(
        version=1,
        mode="search",
        query="a" * MAX_CURSOR_LENGTH,
        offset=5,
        revision="abc123",
    )

    with pytest.raises(PaginationError) as exc_info:
        encode_cursor(state)

    assert exc_info.value.code == "invalid_cursor"


@pytest.mark.parametrize(
    "cursor",
    ["", "not-json", "e30", "x" * 2049],
)
def test_decode_cursor_rejects_malformed_payloads(cursor: str) -> None:
    """Reject malformed or oversized cursors consistently."""
    with pytest.raises(PaginationError) as exc_info:
        decode_cursor(cursor)
    assert exc_info.value.code == "invalid_cursor"


def test_encode_cursor_rejects_unsupported_version() -> None:
    """Refuse to emit an unsupported cursor version."""
    encoded = encode_cursor(
        CursorState(version=1, mode="catalog", query="", offset=25, revision="rev")
    )
    raw = decode_cursor(encoded)
    with pytest.raises(PaginationError, match="version") as exc_info:
        encode_cursor(replace(raw, version=2))
    assert exc_info.value.code == "invalid_cursor"


def test_decode_cursor_rejects_unsupported_version() -> None:
    """Refuse a cursor payload from an unsupported version."""
    raw = b'{"v":2,"m":"catalog","q":"","o":25,"r":"rev"}'
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(PaginationError, match="version") as exc_info:
        decode_cursor(encoded)
    assert exc_info.value.code == "invalid_cursor"


def test_catalog_revision_changes_for_discovery_or_visibility_changes() -> None:
    """Include discovery-visible registry details in the revision."""
    fingerprint = (1, "fake", (("music/search", 1),))
    first = _entry("ma_api:music/search", aliases=("find music",))
    same = _entry("ma_api:music/search", aliases=("find music",))
    described = _entry("ma_api:music/search", description="Changed")
    hidden = _entry("ma_api:players/all")
    revision = catalog_revision(fingerprint, (first, hidden))
    assert catalog_revision(fingerprint, (same, hidden)) == revision
    assert catalog_revision(fingerprint, (described, hidden)) != revision
    assert catalog_revision(fingerprint, (first,)) != revision
    assert catalog_revision((2, "fake", ()), (first, hidden)) != revision
