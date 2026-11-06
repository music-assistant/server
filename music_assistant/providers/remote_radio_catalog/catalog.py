"""Validate remote radio catalogs without retaining partial or malformed results."""

# Invalid remote data consistently raises ValueError, including wrong JSON value types.
# ruff: noqa: TRY004

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from music_assistant.helpers.playlists import parse_m3u

MAX_CATALOG_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Station:
    """A station identifier is local to exactly one configured provider instance."""

    item_id: str
    name: str
    url: str
    logo: str | None = None


def validate_url(value: object) -> str:
    """Require an absolute HTTP(S) URL without embedded credentials."""
    if not isinstance(value, str):
        raise ValueError("An HTTP or HTTPS URL is required")
    value = value.strip()
    parts = urlsplit(value)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(char.isspace() for char in value)
    ):
        raise ValueError("An HTTP or HTTPS URL without embedded credentials is required")
    # Accessing port validates malformed numeric ports too.
    _ = parts.port
    return value


def parse_catalog(text: str, base_url: str) -> dict[str, Station]:
    """Parse JSON or M3U, rejecting ambiguous IDs and incomplete catalogs atomically."""
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise ValueError("Empty response is not a radio catalog")
    if text.startswith(("[", "{")):
        stations = _parse_json(json.loads(text), base_url)
    else:
        if not text.startswith("#EXTM3U") and not all(
            line.startswith(("http://", "https://"))
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        ):
            raise ValueError("Response is not an M3U radio catalog")
        if any(line.strip().startswith("#EXT-X-") for line in text.splitlines()):
            raise ValueError("An HLS stream playlist is not a station catalog")
        entries = parse_m3u(text)
        if not entries and not text.startswith("#EXTM3U"):
            raise ValueError("Response is not an M3U radio catalog")
        stations = [_station(None, entry.title, entry.path, None, base_url) for entry in entries]
    result: dict[str, Station] = {}
    for station in stations:
        if station.item_id in result and result[station.item_id] != station:
            raise ValueError("Conflicting station IDs in the same catalog")
        result[station.item_id] = station
    return result


def _parse_json(data: object, base_url: str) -> list[Station]:
    """Accept a station list, a stations envelope, or an Icecast status document."""
    icecast = False
    if isinstance(data, dict):
        if "stations" in data:
            data = data["stations"]
        elif isinstance(data.get("icestats"), dict):
            icecast = True
            data = data["icestats"].get("source", [])
            if isinstance(data, dict):
                data = [data]
        else:
            raise ValueError("JSON must contain stations or icestats")
    if not isinstance(data, list):
        raise ValueError("Stations must be a JSON array")
    result = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("Each station must be a JSON object")
        result.append(
            _station(
                entry.get("id"),
                entry.get("server_name" if icecast else "name"),
                entry.get("listenurl" if icecast else "url"),
                entry.get("logo"),
                base_url,
            )
        )
    return result


def _station(item_id: object, name: object, url: object, logo: object, base_url: str) -> Station:
    """Normalize station values and use a stable URL digest when no ID is provided."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Every station needs a stream URL")
    # Relative paths are useful in hosted M3U files; reject local/foreign schemes.
    stream_url = validate_url(urljoin(base_url, url.strip()))
    if item_id is not None and (not isinstance(item_id, (str, int)) or isinstance(item_id, bool)):
        raise ValueError("Station ID must be a string or integer")
    normalized_id = str(item_id).strip() if item_id is not None else ""
    if not normalized_id:
        normalized_id = hashlib.sha256(stream_url.encode()).hexdigest()
    if name is not None and not isinstance(name, str):
        raise ValueError("Station name must be a string")
    station_name = (name or "").strip() or urlsplit(stream_url).hostname or "Radio"
    if logo is not None and not isinstance(logo, str):
        raise ValueError("Station logo must be a URL")
    return Station(
        normalized_id,
        station_name,
        stream_url,
        validate_url(urljoin(base_url, logo)) if logo else None,
    )
