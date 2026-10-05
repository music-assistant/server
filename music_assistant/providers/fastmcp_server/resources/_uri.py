"""URI parsing for MCP resources."""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLOWED_SCHEMES: frozenset[str] = frozenset({"library", "player", "queue"})
ALLOWED_TYPES: frozenset[str] = frozenset(
    {"artist", "album", "track", "playlist", "radio", "podcast", "audiobook"}
)
_ID_RE = re.compile(r"^[A-Za-z0-9._:%@\-]+$")


@dataclass(frozen=True)
class ResourceURI:
    """A parsed MCP resource URI: ``<scheme>://[<type>/]<id>``."""

    scheme: str
    type: str | None
    id: str


def parse_resource_uri(uri: str) -> ResourceURI:
    """
    Parse and validate a resource URI.

    :param uri: input URI (``library://artist/123``, ``player://kitchen``).
    :raises ValueError: if scheme/type/id are missing, unknown, or contain
        characters that could enable path traversal.
    """
    if "://" not in uri:
        msg = f"Invalid URI (missing scheme): {uri!r}"
        raise ValueError(msg)
    scheme, _, rest = uri.partition("://")
    if scheme not in ALLOWED_SCHEMES:
        msg = f"Unsupported scheme: {scheme!r}"
        raise ValueError(msg)
    if not rest or ".." in rest:
        msg = f"Invalid URI body: {rest!r}"
        raise ValueError(msg)

    if "/" in rest:
        if scheme != "library":
            msg = f"{scheme}:// URIs must not contain a path separator, got {uri!r}"
            raise ValueError(msg)
        type_, _, identifier = rest.partition("/")
        if type_ not in ALLOWED_TYPES:
            msg = f"Unknown library type: {type_!r}"
            raise ValueError(msg)
        if not identifier or not _ID_RE.match(identifier):
            msg = f"Invalid id: {identifier!r}"
            raise ValueError(msg)
        return ResourceURI(scheme=scheme, type=type_, id=identifier)

    if scheme == "library":
        msg = f"library:// URIs require a type segment, got {uri!r}"
        raise ValueError(msg)
    if not _ID_RE.match(rest):
        msg = f"Invalid id: {rest!r}"
        raise ValueError(msg)
    return ResourceURI(scheme=scheme, type=None, id=rest)
