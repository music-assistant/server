"""Helpers for the Smart Playlist plugin: rules dataclass, validation, and JSON I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import aiofiles
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.json import json_dumps, json_loads

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
    seed_track_name: str | None = None
    min_popularity: int | None = None
    logic: str = LOGIC_AND
    limit: int = DEFAULT_TRACK_LIMIT
    is_dynamic: bool = True
    genre_names: dict[int, str] = field(default_factory=dict)
    artist_names: dict[int, str] = field(default_factory=dict)
    album_names: dict[int, str] = field(default_factory=dict)
    year_from: int | None = None
    year_to: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "genre_ids": self.genre_ids,
            "artist_ids": self.artist_ids,
            "album_ids": self.album_ids,
            "favorites_only": self.favorites_only,
            "seed_track_uri": self.seed_track_uri,
            "seed_track_name": self.seed_track_name,
            "min_popularity": self.min_popularity,
            "logic": self.logic,
            "limit": self.limit,
            "is_dynamic": self.is_dynamic,
            "genre_names": {str(k): v for k, v in self.genre_names.items()},
            "artist_names": {str(k): v for k, v in self.artist_names.items()},
            "album_names": {str(k): v for k, v in self.album_names.items()},
            "year_from": self.year_from,
            "year_to": self.year_to,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SmartPlaylistRules:
        """Deserialize from dictionary."""
        raw_genre_names: dict[str, str] = data.get("genre_names", {})
        raw_artist_names: dict[str, str] = data.get("artist_names", {})
        raw_album_names: dict[str, str] = data.get("album_names", {})
        return cls(
            genre_ids=[int(x) for x in data.get("genre_ids", [])],
            artist_ids=[int(x) for x in data.get("artist_ids", [])],
            album_ids=[int(x) for x in data.get("album_ids", [])],
            favorites_only=data.get("favorites_only", False),
            seed_track_uri=data.get("seed_track_uri"),
            seed_track_name=data.get("seed_track_name"),
            min_popularity=data.get("min_popularity"),
            logic=data.get("logic", LOGIC_AND),
            limit=data.get("limit", DEFAULT_TRACK_LIMIT),
            is_dynamic=data.get("is_dynamic", True),
            genre_names={int(k): v for k, v in raw_genre_names.items()},
            artist_names={int(k): v for k, v in raw_artist_names.items()},
            album_names={int(k): v for k, v in raw_album_names.items()},
            year_from=data.get("year_from"),
            year_to=data.get("year_to"),
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
        if self.seed_track_uri:
            label = self.seed_track_name or self.seed_track_uri
            parts.append(f"Similar to: {label}")
        if self.min_popularity is not None:
            parts.append(f"Min. popularity: {self.min_popularity}")
        if self.year_from is not None or self.year_to is not None:
            if self.year_from is not None and self.year_to is not None:
                parts.append(f"Year: {self.year_from}-{self.year_to}")
            elif self.year_from is not None:
                parts.append(f"Year: from {self.year_from}")
            else:
                parts.append(f"Year: to {self.year_to}")
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
    if (
        rules.year_from is not None
        and rules.year_to is not None
        and rules.year_from > rules.year_to
    ):
        msg = (
            f"year_from must be less than or equal to year_to, got "
            f"{rules.year_from}>{rules.year_to}"
        )
        raise InvalidDataError(msg)


async def read_json(path: str) -> dict[str, Any]:
    """Read a JSON file and return its contents."""
    async with aiofiles.open(path, encoding="utf-8") as fh:
        return cast("dict[str, Any]", json_loads(await fh.read()))


async def write_json(path: str, data: dict[str, Any]) -> None:
    """Write data as JSON to a file."""
    async with aiofiles.open(path, "w", encoding="utf-8") as fh:
        await fh.write(json_dumps(data, indent=True))
