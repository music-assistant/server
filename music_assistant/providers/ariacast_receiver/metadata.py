"""Metadata handler for AriaCast protocol."""

from __future__ import annotations

from typing import Any


class MetadataHandler:
    """Handle metadata updates for AriaCast streams."""

    def __init__(self) -> None:
        """Initialize metadata handler."""
        self.current_metadata: dict[str, Any] = {
            "title": None,
            "artist": None,
            "album": None,
            "artwork_url": None,
            "duration_ms": None,
            "position_ms": None,
            "is_playing": False,
        }

    def update(self, metadata: dict[str, Any]) -> None:
        """Update current metadata with new values.
        
        Args:
            metadata: Dictionary containing metadata updates
        """
        # Map common camelCase keys to snake_case
        key_mapping = {
            "durationMs": "duration_ms",
            "positionMs": "position_ms",
            "artworkUrl": "artwork_url",
            "isPlaying": "is_playing"
        }
        
        # Merge mapped keys into metadata (preferring existing snake_case if present)
        for camel, snake in key_mapping.items():
            if camel in metadata and snake not in metadata:
                metadata[snake] = metadata[camel]

        for key in ["title", "artist", "album", "artwork_url", "duration_ms", "position_ms", "is_playing"]:
            if key in metadata and metadata[key] is not None:
                self.current_metadata[key] = metadata[key]

    def clear(self) -> None:
        """Clear all metadata."""
        self.current_metadata = {
            "title": None,
            "artist": None,
            "album": None,
            "artwork_url": None,
            "duration_ms": None,
            "position_ms": None,
            "is_playing": False,
        }

    def get(self) -> dict[str, Any]:
        """Get current metadata.
        
        Returns:
            Dictionary containing current metadata
        """
        return self.current_metadata.copy()
