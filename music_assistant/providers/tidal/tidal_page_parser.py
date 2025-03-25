"""Parser for Tidal page structures with lazy loading."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType

from music_assistant.constants import CACHE_CATEGORY_RECOMMENDATIONS

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, Playlist, Track

    from music_assistant.providers.tidal import TidalProvider


class TidalPageParser:
    """Parser for Tidal page structures with lazy loading."""

    def __init__(self, provider: TidalProvider) -> None:
        """Initialize the parser with the Tidal provider instance."""
        self.provider = provider
        self.logger = provider.logger
        self._content_map: dict[str, dict[str, Any]] = {
            "MIX": {},
            "PLAYLIST": {},
            "ALBUM": {},
            "TRACK": {},
            "ARTIST": {},
        }
        self._module_map: list[dict[str, Any]] = []
        self._page_path: str | None = None
        self._parsed_at: int = 0

    def parse_page_structure(self, page_data: dict[str, Any], page_path: str) -> None:
        """Parse Tidal page structure into indexed modules."""
        self._page_path = page_path
        self._parsed_at = int(time.time())
        self._module_map = []

        # Extract modules from rows
        module_idx = 0
        for row_idx, row in enumerate(page_data.get("rows", [])):
            for module in row.get("modules", []):
                # Store basic module info for later processing
                module_info = {
                    "title": module.get("title", ""),
                    "type": module.get("type", ""),
                    "raw_data": module,
                    "module_idx": module_idx,
                    "row_idx": row_idx,
                }
                self._module_map.append(module_info)
                module_idx += 1

    def get_module_items(
        self, module_info: dict[str, Any]
    ) -> tuple[list[Playlist | Album | Track | Artist], MediaType]:
        """Extract media items from a module with simplified type handling."""
        result: list[Playlist | Album | Track | Artist] = []
        type_counts: dict[MediaType, int] = {
            MediaType.PLAYLIST: 0,
            MediaType.ALBUM: 0,
            MediaType.TRACK: 0,
            MediaType.ARTIST: 0,
        }

        module_data = module_info.get("raw_data", {})
        module_type = module_data.get("type", "")

        # Extract items based on module type
        if module_type == "HIGHLIGHT_MODULE":
            self._process_highlight_module(module_data, result, type_counts)
        elif module_type == "MIXED_TYPES_LIST" or "pagedList" in module_data:
            self._process_paged_list(module_data, module_type, result, type_counts)

        # Determine the primary content type based on counts
        primary_type = self._determine_primary_type(type_counts)
        return result, primary_type

    def _determine_primary_type(self, type_counts: dict[MediaType, int]) -> MediaType:
        """Determine the primary media type based on item counts."""
        primary_type = MediaType.PLAYLIST  # Default
        max_count = 0
        for media_type, count in type_counts.items():
            if count > max_count:
                max_count = count
                primary_type = media_type
        return primary_type

    def _process_highlight_module(
        self,
        module_data: dict[str, Any],
        result: list[Playlist | Album | Track | Artist],
        type_counts: dict[MediaType, int],
    ) -> None:
        """Process highlights from a HIGHLIGHT_MODULE."""
        highlights = module_data.get("highlight", [])
        for highlight in highlights:
            if isinstance(highlight, dict):  # Make sure highlight is a dict
                highlight_item = highlight.get("item", {})
                highlight_type = highlight.get("type", "")
                if isinstance(highlight_item, dict):
                    if parsed_item := self._parse_item(highlight_item, type_counts, highlight_type):
                        result.append(parsed_item)

    def _process_paged_list(
        self,
        module_data: dict[str, Any],
        module_type: str,
        result: list[Playlist | Album | Track | Artist],
        type_counts: dict[MediaType, int],
    ) -> None:
        """Process items from a paged list module."""
        paged_list = module_data.get("pagedList", {})
        items = paged_list.get("items", [])

        # Handle module-specific type inference
        inferred_type: str | None = None
        if module_type in {"ALBUM_LIST", "TRACK_LIST", "PLAYLIST_LIST", "MIX_LIST"}:
            inferred_type = module_type.replace("_LIST", "")

        # Process each item
        for item in items:
            if not item or not isinstance(item, dict):
                continue

            # Use inferred type if no explicit type
            item_type = item.get("type", inferred_type)
            if parsed_item := self._parse_item(item, type_counts, item_type):
                result.append(parsed_item)

    def _parse_item(
        self,
        item: dict[str, Any],
        type_counts: dict[MediaType, int],
        item_type: str = "",
    ) -> Playlist | Album | Track | Artist | None:
        """Parse a single item from Tidal data into a media item."""
        # Handle nested item structure
        if isinstance(item, dict) and "type" in item and "item" in item:
            item_type = item["type"]
            item = item["item"]

        # If no explicit type, try to infer from structure
        if not item_type:
            if "mixType" in item or item.get("subTitle"):
                item_type = "MIX"
            elif "uuid" in item:
                item_type = "PLAYLIST"
            elif "id" in item and "duration" in item:
                item_type = "TRACK"
            elif "id" in item and "artist" in item and "numberOfTracks" in item:
                item_type = "ALBUM"

        # Parse based on detected type
        try:
            if item_type == "MIX":
                media_item: Playlist | Album | Track | Artist = self.provider._parse_playlist(
                    item, is_mix=True
                )
                type_counts[MediaType.PLAYLIST] += 1
                return media_item
            elif item_type == "PLAYLIST":
                media_item = self.provider._parse_playlist(item)
                type_counts[MediaType.PLAYLIST] += 1
                return media_item
            elif item_type == "ALBUM":
                media_item = self.provider._parse_album(item)
                type_counts[MediaType.ALBUM] += 1
                return media_item
            elif item_type == "TRACK":
                media_item = self.provider._parse_track(item)
                type_counts[MediaType.TRACK] += 1
                return media_item
            elif item_type == "ARTIST":
                media_item = self.provider._parse_artist(item)
                type_counts[MediaType.ARTIST] += 1
                return media_item
            return None
        except (KeyError, ValueError, TypeError) as err:
            # Data structure errors
            self.logger.debug("Error parsing item data structure: %s", err)
            return None
        except AttributeError as err:
            # Missing attribute errors
            self.logger.debug("Missing attribute in item: %s", err)
            return None
        except (json.JSONDecodeError, UnicodeError) as err:
            # JSON/text encoding issues
            self.logger.debug("Error decoding item content: %s", err)
            return None

    @classmethod
    async def from_cache(cls, provider: TidalProvider, page_path: str) -> TidalPageParser | None:
        """Create a parser instance from cached data if available and valid."""
        cache_key = f"tidal_page_{page_path}"
        cached_data = await provider.mass.cache.get(
            cache_key,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
            base_key=provider.lookup_key,
        )
        if not cached_data:
            return None

        parser = cls(provider)
        parser._page_path = page_path
        parser._module_map = cached_data.get("module_map", [])
        parser._content_map = cached_data.get("content_map", {})
        parser._parsed_at = cached_data.get("parsed_at", 0)

        return parser

    @property
    def content_stats(self) -> dict[str, int | float]:
        """Get statistics about the parsed content."""
        stats = {
            "modules": len(self._module_map),
            "cache_age_minutes": (time.time() - self._parsed_at) / 60,
        }

        for media_type, items in self._content_map.items():
            stats[f"{media_type.lower()}_count"] = len(items)

        return stats
