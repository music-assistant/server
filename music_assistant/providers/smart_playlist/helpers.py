"""Helpers for the Smart Playlist plugin: rules dataclass, validation, and JSON I/O."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

from music_assistant_models.errors import InvalidDataError

LOGIC_AND = "AND"
LOGIC_OR = "OR"
DEFAULT_TRACK_LIMIT = 100
MAX_SIMILAR_TRACKS = 50
RULES_FILENAME = "smart_playlist_rules.json"


@dataclass
class SmartPlaylistRules:
    """Rules that define which tracks are included in a smart playlist."""

    genre_ids: list[int] = field(default_factory=list)
    artist_ids: list[int] = field(default_factory=list)
    album_ids: list[int] = field(default_factory=list)
    favorites_only: bool = False
    seed_track_uri: str | None = None
    min_popularity: int | None = None
    logic: str = LOGIC_AND
    limit: int = DEFAULT_TRACK_LIMIT
    is_dynamic: bool = True
    genre_names: dict[int, str] = field(default_factory=dict)
    artist_names: dict[int, str] = field(default_factory=dict)
    album_names: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "genre_ids": self.genre_ids,
            "artist_ids": self.artist_ids,
            "album_ids": self.album_ids,
            "favorites_only": self.favorites_only,
            "seed_track_uri": self.seed_track_uri,
            "min_popularity": self.min_popularity,
            "logic": self.logic,
            "limit": self.limit,
            "is_dynamic": self.is_dynamic,
            "genre_names": {str(k): v for k, v in self.genre_names.items()},
            "artist_names": {str(k): v for k, v in self.artist_names.items()},
            "album_names": {str(k): v for k, v in self.album_names.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SmartPlaylistRules:
        """Deserialize from dictionary."""
        raw_genre_names: dict[str, str] = data.get("genre_names", {})
        raw_artist_names: dict[str, str] = data.get("artist_names", {})
        raw_album_names: dict[str, str] = data.get("album_names", {})
        return cls(
            genre_ids=data.get("genre_ids", []),
            artist_ids=data.get("artist_ids", []),
            album_ids=data.get("album_ids", []),
            favorites_only=data.get("favorites_only", False),
            seed_track_uri=data.get("seed_track_uri"),
            min_popularity=data.get("min_popularity"),
            logic=data.get("logic", LOGIC_AND),
            limit=data.get("limit", DEFAULT_TRACK_LIMIT),
            is_dynamic=data.get("is_dynamic", True),
            genre_names={int(k): v for k, v in raw_genre_names.items()},
            artist_names={int(k): v for k, v in raw_artist_names.items()},
            album_names={int(k): v for k, v in raw_album_names.items()},
        )

    def human_readable(self) -> str:
        """Return a human-readable summary of the rules."""
        parts: list[str] = []
        if self.favorites_only:
            parts.append("Favorites only")
        if self.genre_ids:
            names = [self.genre_names.get(gid, str(gid)) for gid in self.genre_ids]
            parts.append(f"Genres: {', '.join(names)}")
        if self.artist_ids:
            names = [self.artist_names.get(aid, str(aid)) for aid in self.artist_ids]
            parts.append(f"Artists: {', '.join(names)}")
        if self.album_ids:
            names = [self.album_names.get(aid, str(aid)) for aid in self.album_ids]
            parts.append(f"Albums: {', '.join(names)}")
            parts.append(f"Similar to: {self.seed_track_uri}")
        if self.min_popularity is not None:
            parts.append(f"Min. popularity: {self.min_popularity}")
        if not parts:
            return "No rules (all library tracks)"
        connector = f" {self.logic} "
        return connector.join(parts)


def validate_rules(rules: SmartPlaylistRules) -> None:
    """Raise InvalidDataError if any rule field is out of allowed range."""
    if rules.logic not in (LOGIC_AND, LOGIC_OR):
        msg = f"Invalid logic operator: {rules.logic}. Must be AND or OR."
        raise InvalidDataError(msg)
    if rules.limit < 1 or rules.limit > 2000:
        msg = f"Track limit must be between 1 and 2000, got {rules.limit}"
        raise InvalidDataError(msg)
    if rules.min_popularity is not None and not (0 <= rules.min_popularity <= 100):
        msg = f"min_popularity must be between 0 and 100, got {rules.min_popularity}"
        raise InvalidDataError(msg)


def read_json(path: str) -> dict[str, Any]:
    """Read a JSON file and return its contents (blocking - run in a thread)."""
    with open(path, encoding="utf-8") as fh:
        return cast("dict[str, Any]", json.load(fh))


def write_json(path: str, data: dict[str, Any]) -> None:
    """Write data as JSON to a file (blocking - run in a thread)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
