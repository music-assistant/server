"""Constants for the TeddyCloud music provider."""

from __future__ import annotations

from typing import Final

# Provider-specific config entry key (the TeddyCloud server base URL).
CONF_URL: Final[str] = "url"

# TeddyCloud HTTP API.
TAG_INDEX_PATH: Final[str] = "/api/getTagIndex"
CONTENT_DOWNLOAD_PATH: Final[str] = "/content/download"

# A TAF (Tonie Audio Format) file is an OGG/Opus stream with a fixed 4096-byte protobuf
# header prepended. TeddyCloud serves it header-stripped with ?skip_header=true; the raw
# header is read (once, cached) to derive the total duration.
TAF_HEADER_SIZE: Final[int] = 4096

# Fallback author/publisher for tags without a recognised series (e.g. custom tonies).
DEFAULT_ARTIST: Final[str] = "TeddyCloud"

# How long (seconds) to keep the parsed tag index in memory before refetching.
TAG_CACHE_TTL: Final[int] = 60
