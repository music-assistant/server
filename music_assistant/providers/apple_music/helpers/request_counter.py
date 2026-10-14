"""Outgoing request counters of the Apple Music provider, for diagnostics."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from music_assistant.helpers.datetime import utc

# path words are lowercase letters and dashes; ids and storefronts never are
_PATH_WORD = re.compile(r"[a-z][a-z-]*")


def endpoint_family(endpoint: str, params: dict[str, Any]) -> str:
    """
    Return the endpoint without storefront, ids or query, e.g. ``catalog/songs/{id}``.

    :param endpoint: The endpoint as passed to the API client.
    :param params: The query parameters of the request.
    """
    segments = endpoint.split("?", 1)[0].split("/")
    if segments[0] == "catalog":
        del segments[1:2]
    family = "/".join(seg if _PATH_WORD.fullmatch(seg) else "{id}" for seg in segments)
    return f"{family}?ids" if "ids" in params else family


class RequestCounter:
    """Count outgoing requests per endpoint family."""

    def __init__(self) -> None:
        """Initialize the counter."""
        self.started_at = utc().isoformat(timespec="seconds")
        self._counts: defaultdict[str, Counter[str]] = defaultdict(Counter)

    def record(self, family: str, event: str) -> None:
        """
        Count one event for an endpoint family.

        :param family: The endpoint family, see :func:`endpoint_family`.
        :param event: What happened, e.g. ``attempts`` or ``rate_limited``.
        """
        self._counts[family][event] += 1

    def as_dict(self) -> dict[str, dict[str, int]]:
        """Return the counts per endpoint family."""
        return {family: dict(counts) for family, counts in sorted(self._counts.items())}

    def since(self, earlier: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
        """
        Return the counts recorded after an earlier snapshot.

        :param earlier: A snapshot taken with :meth:`as_dict`.
        """
        delta = {
            family: dict(counts - Counter(earlier.get(family, {})))
            for family, counts in sorted(self._counts.items())
        }
        return {family: counts for family, counts in delta.items() if counts}
