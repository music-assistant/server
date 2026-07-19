"""Helpers for consuming JSON:API responses from the official Tidal API."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote


class JsonApiDocument:
    """
    Wrapper around a parsed JSON:API document.

    Indexes the ``included`` resources by (type, id) so that relationship
    linkages on the primary resource(s) can be resolved to the full resource
    objects returned via ``?include=``.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        """Initialize the document from a parsed JSON:API response."""
        self.raw = raw
        self._included: dict[tuple[str, str], dict[str, Any]] = {
            (resource["type"], resource["id"]): resource
            for resource in raw.get("included", [])
            if resource.get("type") and resource.get("id")
        }

    @property
    def data(self) -> dict[str, Any]:
        """Return the primary single-resource object."""
        data = self.raw.get("data")
        return data if isinstance(data, dict) else {}

    @property
    def data_list(self) -> list[dict[str, Any]]:
        """Return the primary resource collection."""
        data = self.raw.get("data")
        return data if isinstance(data, list) else []

    @property
    def next_cursor(self) -> str | None:
        """Return the opaque cursor for the next page, if any."""
        links = self.raw.get("links") or {}
        next_link = links.get("next")
        if not isinstance(next_link, str) or not next_link:
            return None
        # The API returns either a full path with a page[cursor] query param
        # or the bare cursor value; callers only need the cursor.
        if "page[cursor]=" in next_link:
            return unquote(next_link.split("page[cursor]=", 1)[1].split("&", 1)[0])
        return next_link

    def resolve(self, identifier: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve a resource identifier ({type, id}) to its included resource."""
        if "type" not in identifier or "id" not in identifier:
            return None
        return self._included.get((identifier["type"], identifier["id"]))

    def linkage_ids(self, resource: dict[str, Any], relationship: str) -> set[str]:
        """Return the ids of a resource's relationship linkage (no include needed)."""
        rel = (resource.get("relationships") or {}).get(relationship) or {}
        linkage = rel.get("data")
        if linkage is None:
            return set()
        if isinstance(linkage, dict):
            linkage = [linkage]
        return {str(item["id"]) for item in linkage if item.get("id")}

    def related(self, resource: dict[str, Any], relationship: str) -> list[dict[str, Any]]:
        """
        Resolve a resource's relationship to the included resource objects.

        :param resource: The resource object whose relationship to resolve.
        :param relationship: The relationship name (e.g. "artists", "coverArt").
        """
        rel = (resource.get("relationships") or {}).get(relationship) or {}
        linkage = rel.get("data")
        if linkage is None:
            return []
        if isinstance(linkage, dict):
            linkage = [linkage]
        resolved = []
        for identifier in linkage:
            key = (identifier.get("type"), identifier.get("id"))
            if included := self._included.get(key):
                resolved.append(included)
        return resolved

    def related_one(self, resource: dict[str, Any], relationship: str) -> dict[str, Any] | None:
        """Resolve a to-one relationship to a single included resource object."""
        resolved = self.related(resource, relationship)
        return resolved[0] if resolved else None
