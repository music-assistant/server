"""Helpers/utilities for the Internet Archive provider."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)

from .constants import (
    IA_DETAILS_URL,
    IA_DOWNLOAD_URL,
    IA_METADATA_URL,
    IA_SEARCH_URL,
    PREFERRED_AUDIO_FORMATS,
    SUPPORTED_AUDIO_FORMATS,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class InternetArchiveClient:
    """Client for communicating with the Internet Archive API."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the Internet Archive client."""
        self.mass = mass

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request and return JSON response with proper error handling."""
        try:
            async with self.mass.http_session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 429:
                    # Rate limited - let throttler handle this
                    backoff_time = int(response.headers.get("Retry-After", 60))
                    raise ResourceTemporarilyUnavailable(
                        "Internet Archive rate limit exceeded", backoff_time=backoff_time
                    )

                if response.status == 404:
                    raise MediaNotFoundError("Item not found on Internet Archive")

                if response.status >= 500:
                    raise ResourceTemporarilyUnavailable(
                        "Internet Archive server error", backoff_time=30
                    )

                response.raise_for_status()
                json_data = await response.json()

                if not isinstance(json_data, dict):
                    raise InvalidDataError(f"Expected JSON object, got {type(json_data).__name__}")

                return json_data

        except aiohttp.ClientError as err:
            raise ResourceTemporarilyUnavailable(f"Network error: {err}") from err
        except TimeoutError as err:
            raise ResourceTemporarilyUnavailable(f"Request timeout: {err}") from err
        except json.JSONDecodeError as err:
            raise InvalidDataError(f"Invalid JSON response: {err}") from err

    async def search(
        self,
        query: str,
        mediatype: str | None = None,
        collection: str | None = None,
        rows: int = 50,
        page: int = 1,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """
        Search the Internet Archive using the advanced search API.

        Args:
            query: Search query string
            mediatype: Optional media type filter (e.g., 'audio')
            collection: Optional collection filter (e.g., 'etree')
            rows: Number of results per page (max 200)
            page: Page number for pagination
            sort: Sort order (e.g., 'downloads desc', 'date desc')

        Returns:
            Search response dictionary containing results and metadata
        """
        params: dict[str, Any] = {
            "output": "json",
            "rows": min(rows, 200),  # IA limits to 200 per request
            "page": page,
            "q": query,
        }
        if sort:
            params["sort"] = sort

        return await self._get_json(IA_SEARCH_URL, params)

    async def get_metadata(self, identifier: str) -> dict[str, Any]:
        """Get metadata for a specific Internet Archive item."""
        url = f"{IA_METADATA_URL}/{identifier}"
        return await self._get_json(url)

    async def get_files(self, identifier: str) -> list[dict[str, Any]]:
        """Get file list for an Internet Archive item."""
        metadata = await self.get_metadata(identifier)
        return list(metadata.get("files", []))

    async def get_audio_files(self, identifier: str) -> list[dict[str, Any]]:
        """
        Get audio files for an item with format preference and deduplication.

        Filters for supported audio formats, removes derivative low-quality files,
        deduplicates by base filename, and selects the best quality format for
        each unique track.

        Args:
            identifier: Internet Archive item identifier

        Returns:
            List of audio file information dictionaries, sorted by filename
            for proper track ordering
        """
        files = await self.get_files(identifier)
        files_by_basename: dict[str, list[dict[str, Any]]] = {}

        for file_info in files:
            filename = file_info.get("name", "")
            file_format = file_info.get("format", "").lower()

            if not self._is_supported_audio_format(file_format):
                continue
            if self._is_derivative_file(file_info, filename):
                continue

            base_name = self._get_base_filename(filename)
            files_by_basename.setdefault(base_name, []).append(file_info)

        preferred_files: list[dict[str, Any]] = []
        for format_versions in files_by_basename.values():
            best_file = self._select_best_audio_format(format_versions)
            if best_file:
                preferred_files.append(best_file)

        return sorted(preferred_files, key=lambda x: x.get("name", ""))

    def _is_supported_audio_format(self, file_format: str) -> bool:
        """Check if the file format is a supported audio format."""
        return any(fmt in file_format for fmt in SUPPORTED_AUDIO_FORMATS)

    def _is_derivative_file(self, file_info: dict[str, Any], filename: str) -> bool:
        """Check if a file is a derivative (low-quality) version."""
        return file_info.get("source", "") == "derivative" and any(
            skip in filename.lower() for skip in ("_64kb", "_vbr", "_sample", "_preview")
        )

    def _get_base_filename(self, filename: str) -> str:
        """Extract base filename without extension and quality indicators for deduplication."""
        # Remove extension first
        base = filename.rsplit(".", 1)[0] if "." in filename else filename

        # Remove common quality indicators from Internet Archive files
        quality_patterns = [
            r"_320kb$",
            r"_256kb$",
            r"_192kb$",
            r"_128kb$",
            r"_64kb$",
            r"_vbr$",
            r"_original$",
            r"_sample$",
            r"_preview$",
        ]

        for pattern in quality_patterns:
            base = re.sub(pattern, "", base, flags=re.IGNORECASE)

        return base

    def _select_best_audio_format(
        self, format_versions: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """
        Select the best audio format from available versions.

        Prefers higher quality formats based on PREFERRED_AUDIO_FORMATS ordering.
        Falls back to first available if no preferred format is found.

        Args:
            format_versions: List of file info dictionaries for the same track

        Returns:
            Best quality file info dictionary, or None if no valid files
        """
        for preferred_format in PREFERRED_AUDIO_FORMATS:
            for file_info in format_versions:
                if preferred_format in file_info.get("format", "").lower():
                    return file_info
        return format_versions[0] if format_versions else None

    def get_download_url(self, identifier: str, filename: str) -> str:
        """
        Get download URL for a specific file.

        Args:
            identifier: Internet Archive item identifier
            filename: Name of the file to download

        Returns:
            Full download URL for the file
        """
        return f"{IA_DOWNLOAD_URL}/{identifier}/{quote(filename)}"

    def get_item_url(self, identifier: str) -> str:
        """
        Get the details page URL for an Internet Archive item.

        Args:
            identifier: Internet Archive item identifier

        Returns:
            Full URL to the item's details page
        """
        return f"{IA_DETAILS_URL}/{identifier}"


def parse_duration(duration_str: str) -> int | None:
    """
    Parse duration string to seconds.

    Handles various duration formats commonly found in Internet Archive metadata:
    - "1:23:45" (hours:minutes:seconds)
    - "12:34" (minutes:seconds)
    - "123" (seconds only)

    Args:
        duration_str: Duration string to parse

    Returns:
        Duration in seconds, or None if parsing fails
    """
    if not duration_str:
        return None
    try:
        if ":" in duration_str:
            parts = duration_str.split(":")
            if len(parts) == 3:  # h:m:s
                hours, minutes, seconds = map(float, parts)
                return int(hours * 3600 + minutes * 60 + seconds)
            if len(parts) == 2:  # m:s
                minutes, seconds = map(float, parts)
                return int(minutes * 60 + seconds)
            return None
        return int(float(duration_str))
    except (ValueError, TypeError):
        return None


def clean_text(text: str | list[str] | None) -> str:
    """
    Clean and normalize text fields from Internet Archive metadata.

    Internet Archive metadata can contain text as strings or lists of strings.
    This function normalizes the input to a clean string.

    Args:
        text: Text to clean (string, list of strings, or None)

    Returns:
        Cleaned text string, or empty string if no valid text found
    """
    if not text:
        return ""
    if isinstance(text, list):
        for item in text:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return ""
    return text.strip()


def extract_year(date_str: str | list[str] | None) -> int | None:
    """
    Extract year from Internet Archive date string.

    Internet Archive dates can be in various formats. This function attempts
    to extract a 4-digit year from the date string.

    Args:
        date_str: Date string or list to extract year from

    Returns:
        4-digit year as integer, or None if extraction fails
    """
    date_text = clean_text(date_str)
    if not date_text:
        return None
    try:
        match = re.search(r"\b(19\d{2}|20\d{2})\b", date_text)
        return int(match.group(1)) if match else None
    except (ValueError, TypeError):
        return None


def get_image_url(identifier: str, filename: str | None = None) -> str | None:
    """
    Get image URL for an Internet Archive item.

    Args:
        identifier: Internet Archive item identifier
        filename: Optional specific image filename

    Returns:
        Full URL to the image, or None if identifier is missing
    """
    if not identifier:
        return None
    if filename:
        return f"{IA_DOWNLOAD_URL}/{identifier}/{quote(filename)}"
    return f"{IA_DOWNLOAD_URL}/{identifier}/__ia_thumb.jpg"
