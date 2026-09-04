"""Helper functions for the Web Radio Broadcast player provider."""

from __future__ import annotations

from slugify import slugify


def slugify_station_name(name: str) -> str:
    """
    Derive a URL-safe slug from a station's display name.

    :param name: User-supplied station display name.
    :return: Lowercase, hyphen-separated, ASCII slug. May be an empty
        string if the input contains no slug-able characters; callers
        must treat that case as an invalid station name.
    """
    return slugify(name)
