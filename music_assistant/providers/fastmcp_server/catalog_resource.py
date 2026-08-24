"""Read-only MCP resource transport for the visible command catalog."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlencode

from fastmcp.exceptions import ResourceError

from .catalog_pagination import DiscoveryPage, PaginationError, resolve_limit

if TYPE_CHECKING:
    from fastmcp import FastMCP


class CatalogPager(Protocol):
    """Minimal discovery-service interface consumed by the resource transport."""

    async def discover(
        self,
        query: str | None = None,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> DiscoveryPage:
        """Return one request-visible discovery page."""


def register_catalog_resource(mcp: FastMCP, pager: CatalogPager) -> None:
    """Register the always-on, request-filtered catalog resource template."""

    @mcp.resource(
        "catalog://commands{?cursor,limit}",
        name="command_catalog",
        description="Browse visible Music Assistant command names by page.",
        mime_type="application/json",
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def command_catalog(
        cursor: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Return one alphabetical page of visible command names."""
        try:
            page_limit = resolve_limit("catalog", limit)
            page = await pager.discover("", cursor=cursor, limit=page_limit)
        except PaginationError as exc:
            raise ResourceError(f"{exc.code}: {exc}") from exc
        next_cursor = page["next_cursor"]
        next_uri = (
            f"catalog://commands?{urlencode({'cursor': next_cursor, 'limit': page_limit})}"
            if next_cursor is not None
            else None
        )
        payload: dict[str, Any] = {
            "items": page["items"],
            "total": page["total"],
            "next_cursor": next_cursor,
            "next_uri": next_uri,
            "catalog_revision": page["catalog_revision"],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
