"""Helpers for the Smart Playlist plugin: rules dataclass, validation, and JSON I/O."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import aiofiles
from music_assistant_models.enums import AlbumType
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.json import json_dumps, json_loads

LOGIC_AND = "AND"
LOGIC_OR = "OR"
DEFAULT_TRACK_LIMIT = 100
MAX_SEEDS = 10
RULES_FILENAME = "smart_playlist_rules.json"


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise InvalidDataError(f"Invalid value for {field_name}: {value!r}") from err


def _coerce_optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise InvalidDataError(f"Invalid value for {field_name}: {value!r}") from err


def _coerce_optional_bool(value: Any, field_name: str) -> bool | None:
    """Coerce a value to bool | None, raising InvalidDataError on bad input."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise InvalidDataError(
        f"Invalid value for {field_name}: {value!r}. Expected True, False, or None."
    )


def _coerce_id_list(value: Any, field_name: str) -> list[int]:
    """Coerce a value to a list of ints, raising InvalidDataError on bad input."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidDataError(f"Expected list for {field_name}, got {type(value).__name__}")
    result = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError) as err:
            raise InvalidDataError(f"Invalid id in {field_name}: {item!r}") from err
    return result


def _coerce_str_list(value: Any, field_name: str) -> list[str]:
    """Coerce a value to a list of strs, raising InvalidDataError on bad input."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidDataError(f"Expected list for {field_name}, got {type(value).__name__}")
    return [str(item) for item in value]


def _coerce_int_keyed_dict(value: Any, field_name: str) -> dict[int, str]:
    """Coerce a value to a dict[int, str], raising InvalidDataError on bad input."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidDataError(f"Expected dict for {field_name}, got {type(value).__name__}")
    result = {}
    for k, v in value.items():
        try:
            result[int(k)] = str(v)
        except (TypeError, ValueError) as err:
            raise InvalidDataError(f"Invalid key in {field_name}: {k!r}") from err
    return result


def _coerce_str_keyed_dict(value: Any, field_name: str) -> dict[str, str]:
    """Coerce a value to a dict[str, str], raising InvalidDataError on bad input."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidDataError(f"Expected dict for {field_name}, got {type(value).__name__}")
    return {str(k): str(v) for k, v in value.items()}


@dataclass
class SmartPlaylistRules:
    """Rules that define which tracks are included in a smart playlist."""

    genre_ids: list[int] = field(default_factory=list)
    artist_ids: list[int] = field(default_factory=list)
    album_ids: list[int] = field(default_factory=list)
    favorites_only: bool = False
    seed_track_uris: list[str] = field(default_factory=list)
    seed_artist_uris: list[str] = field(default_factory=list)
    seed_album_uris: list[str] = field(default_factory=list)
    seed_playlist_uris: list[str] = field(default_factory=list)
    seed_names: dict[str, str] = field(default_factory=dict)
    min_popularity: int | None = None
    logic: str = LOGIC_AND
    limit: int = DEFAULT_TRACK_LIMIT
    is_dynamic: bool = True
    genre_names: dict[int, str] = field(default_factory=dict)
    artist_names: dict[int, str] = field(default_factory=dict)
    album_names: dict[int, str] = field(default_factory=dict)
    year_from: int | None = None
    year_to: int | None = None
    excluded_artist_ids: list[int] = field(default_factory=list)
    excluded_album_ids: list[int] = field(default_factory=list)
    excluded_track_uris: list[str] = field(default_factory=list)
    excluded_artist_names: dict[int, str] = field(default_factory=dict)
    excluded_album_names: dict[int, str] = field(default_factory=dict)
    excluded_genre_ids: list[int] = field(default_factory=list)
    excluded_genre_names: dict[int, str] = field(default_factory=dict)
    album_types: list[str] = field(default_factory=list)
    excluded_album_types: list[str] = field(default_factory=list)
    explicit: bool | None = None
    min_duration: int | None = None
    max_duration: int | None = None
    last_played_before_value: int | None = None
    last_played_before_unit: str | None = None  # "hours", "days", "weeks", "months"

    def all_seed_uris(self) -> list[str]:
        """Return every seed URI across the four seed lists, deduplicated, original order."""
        seen: set[str] = set()
        result: list[str] = []
        for uri in (
            *self.seed_track_uris,
            *self.seed_artist_uris,
            *self.seed_album_uris,
            *self.seed_playlist_uris,
        ):
            if uri and uri not in seen:
                seen.add(uri)
                result.append(uri)
        return result

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "genre_ids": self.genre_ids,
            "artist_ids": self.artist_ids,
            "album_ids": self.album_ids,
            "favorites_only": self.favorites_only,
            "seed_track_uris": self.seed_track_uris,
            "seed_artist_uris": self.seed_artist_uris,
            "seed_album_uris": self.seed_album_uris,
            "seed_playlist_uris": self.seed_playlist_uris,
            "seed_names": self.seed_names,
            "min_popularity": self.min_popularity,
            "logic": self.logic,
            "limit": self.limit,
            "is_dynamic": self.is_dynamic,
            "genre_names": {str(k): v for k, v in self.genre_names.items()},
            "artist_names": {str(k): v for k, v in self.artist_names.items()},
            "album_names": {str(k): v for k, v in self.album_names.items()},
            "year_from": self.year_from,
            "year_to": self.year_to,
            "excluded_artist_ids": self.excluded_artist_ids,
            "excluded_album_ids": self.excluded_album_ids,
            "excluded_track_uris": self.excluded_track_uris,
            "excluded_artist_names": {str(k): v for k, v in self.excluded_artist_names.items()},
            "excluded_album_names": {str(k): v for k, v in self.excluded_album_names.items()},
            "excluded_genre_ids": self.excluded_genre_ids,
            "excluded_genre_names": {str(k): v for k, v in self.excluded_genre_names.items()},
            "album_types": self.album_types,
            "excluded_album_types": self.excluded_album_types,
            "explicit": self.explicit,
            "min_duration": self.min_duration,
            "max_duration": self.max_duration,
            "last_played_before_value": self.last_played_before_value,
            "last_played_before_unit": self.last_played_before_unit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SmartPlaylistRules:
        """Deserialize from dictionary."""
        return cls(
            genre_ids=_coerce_id_list(data.get("genre_ids"), "genre_ids"),
            artist_ids=_coerce_id_list(data.get("artist_ids"), "artist_ids"),
            album_ids=_coerce_id_list(data.get("album_ids"), "album_ids"),
            favorites_only=data.get("favorites_only", False),
            seed_track_uris=_coerce_str_list(data.get("seed_track_uris"), "seed_track_uris"),
            seed_artist_uris=_coerce_str_list(data.get("seed_artist_uris"), "seed_artist_uris"),
            seed_album_uris=_coerce_str_list(data.get("seed_album_uris"), "seed_album_uris"),
            seed_playlist_uris=_coerce_str_list(
                data.get("seed_playlist_uris"), "seed_playlist_uris"
            ),
            seed_names=_coerce_str_keyed_dict(data.get("seed_names"), "seed_names"),
            min_popularity=_coerce_optional_int(data.get("min_popularity"), "min_popularity"),
            logic=data.get("logic", LOGIC_AND),
            limit=_coerce_int(data.get("limit", DEFAULT_TRACK_LIMIT), "limit"),
            is_dynamic=data.get("is_dynamic", True),
            genre_names=_coerce_int_keyed_dict(data.get("genre_names"), "genre_names"),
            artist_names=_coerce_int_keyed_dict(data.get("artist_names"), "artist_names"),
            album_names=_coerce_int_keyed_dict(data.get("album_names"), "album_names"),
            year_from=_coerce_optional_int(data.get("year_from"), "year_from"),
            year_to=_coerce_optional_int(data.get("year_to"), "year_to"),
            excluded_artist_ids=_coerce_id_list(
                data.get("excluded_artist_ids"), "excluded_artist_ids"
            ),
            excluded_album_ids=_coerce_id_list(
                data.get("excluded_album_ids"), "excluded_album_ids"
            ),
            excluded_track_uris=_coerce_str_list(
                data.get("excluded_track_uris"), "excluded_track_uris"
            ),
            excluded_artist_names=_coerce_int_keyed_dict(
                data.get("excluded_artist_names"), "excluded_artist_names"
            ),
            excluded_album_names=_coerce_int_keyed_dict(
                data.get("excluded_album_names"), "excluded_album_names"
            ),
            excluded_genre_ids=_coerce_id_list(
                data.get("excluded_genre_ids"), "excluded_genre_ids"
            ),
            excluded_genre_names=_coerce_int_keyed_dict(
                data.get("excluded_genre_names"), "excluded_genre_names"
            ),
            album_types=_coerce_str_list(data.get("album_types"), "album_types"),
            excluded_album_types=_coerce_str_list(
                data.get("excluded_album_types"), "excluded_album_types"
            ),
            explicit=_coerce_optional_bool(data.get("explicit"), "explicit"),
            min_duration=_coerce_optional_int(data.get("min_duration"), "min_duration"),
            max_duration=_coerce_optional_int(data.get("max_duration"), "max_duration"),
            last_played_before_value=_coerce_optional_int(
                data.get("last_played_before_value"), "last_played_before_value"
            ),
            last_played_before_unit=data.get("last_played_before_unit"),
        )

    def human_readable(self) -> str:
        """Return a human-readable summary of the rules."""
        parts: list[str] = []
        if self.favorites_only:
            parts.append("Favorites only")
        if self.explicit is True:
            parts.append("Explicit only")
        elif self.explicit is False:
            parts.append("No explicit content")
        if self.genre_ids:
            names = [self.genre_names.get(gid, str(gid)) for gid in self.genre_ids]
            parts.append(f"Genres: {', '.join(names)}")
        if self.artist_ids:
            names = [self.artist_names.get(aid, str(aid)) for aid in self.artist_ids]
            parts.append(f"Artists: {', '.join(names)}")
        if self.album_ids:
            names = [self.album_names.get(aid, str(aid)) for aid in self.album_ids]
            parts.append(f"Albums: {', '.join(names)}")
        seed_uris = self.all_seed_uris()
        if seed_uris:
            labels = [self.seed_names.get(uri, uri) for uri in seed_uris]
            parts.append(f"Similar to: {', '.join(labels)}")
        if self.excluded_artist_ids:
            names = [
                self.excluded_artist_names.get(aid, str(aid)) for aid in self.excluded_artist_ids
            ]
            parts.append(f"Excl. artists: {', '.join(names)}")
        if self.excluded_album_ids:
            names = [
                self.excluded_album_names.get(aid, str(aid)) for aid in self.excluded_album_ids
            ]
            parts.append(f"Excl. albums: {', '.join(names)}")
        if self.excluded_genre_ids:
            names = [
                self.excluded_genre_names.get(gid, str(gid)) for gid in self.excluded_genre_ids
            ]
            parts.append(f"Excl. genres: {', '.join(names)}")
        if self.excluded_track_uris:
            parts.append(f"Excl. {len(self.excluded_track_uris)} track(s)")
        if self.album_types:
            parts.append(f"Album types: {', '.join(self.album_types)}")
        if self.excluded_album_types:
            parts.append(f"Excl. album types: {', '.join(self.excluded_album_types)}")
        if self.min_popularity is not None:
            parts.append(f"Min. popularity: {self.min_popularity}")
        if year_range := self._format_year_range():
            parts.append(year_range)
        if duration_range := self._format_duration_range():
            parts.append(duration_range)
        if last_played_str := self._format_last_played():
            parts.append(last_played_str)
        if not parts:
            return "No rules (all library tracks)"
        connector = f" {self.logic} "
        return connector.join(parts)

    def _format_year_range(self) -> str | None:
        """Format year filter as human-readable string."""
        if self.year_from is not None and self.year_to is not None:
            return f"Year: {self.year_from}-{self.year_to}"
        if self.year_from is not None:
            return f"Year: from {self.year_from}"
        if self.year_to is not None:
            return f"Year: to {self.year_to}"
        return None

    def _format_duration_range(self) -> str | None:
        """Format duration filter as human-readable string."""
        if self.min_duration is not None and self.max_duration is not None:
            return f"Duration: {self.min_duration}-{self.max_duration}s"
        if self.min_duration is not None:
            return f"Duration: ≥{self.min_duration}s"
        if self.max_duration is not None:
            return f"Duration: ≤{self.max_duration}s"
        return None

    def _format_last_played(self) -> str | None:
        """Format last played filter as human-readable string."""
        if self.last_played_before_value is None or self.last_played_before_unit is None:
            return None
        unit_display = self.last_played_before_unit  # hours, days, weeks, months
        return f"Not played in {self.last_played_before_value} {unit_display}"


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
    total_seeds = len(rules.all_seed_uris())
    if total_seeds > MAX_SEEDS:
        msg = f"Too many seeds: {total_seeds} > {MAX_SEEDS}"
        raise InvalidDataError(msg)
    valid_album_types = {t.value for t in AlbumType}
    for at in rules.album_types:
        if at not in valid_album_types:
            msg = f"Invalid album_types value: {at!r}. Must be one of {sorted(valid_album_types)}"
            raise InvalidDataError(msg)
    for at in rules.excluded_album_types:
        if at not in valid_album_types:
            msg = f"Invalid excluded_album_types value: {at!r}. Must be one of {sorted(valid_album_types)}"
            raise InvalidDataError(msg)
    if rules.min_duration is not None and rules.min_duration < 0:
        msg = f"min_duration must be >= 0, got {rules.min_duration}"
        raise InvalidDataError(msg)
    if rules.max_duration is not None and rules.max_duration < 0:
        msg = f"max_duration must be >= 0, got {rules.max_duration}"
        raise InvalidDataError(msg)
    if (
        rules.min_duration is not None
        and rules.max_duration is not None
        and rules.min_duration > rules.max_duration
    ):
        msg = f"min_duration must be <= max_duration, got {rules.min_duration}>{rules.max_duration}"
        raise InvalidDataError(msg)

    # Validate last_played fields
    if (rules.last_played_before_value is None) != (rules.last_played_before_unit is None):
        msg = (
            "last_played_before_value and last_played_before_unit must both be set or both be None"
        )
        raise InvalidDataError(msg)
    if rules.last_played_before_value is not None and rules.last_played_before_value < 1:
        msg = f"last_played_before_value must be >= 1, got {rules.last_played_before_value}"
        raise InvalidDataError(msg)
    if rules.last_played_before_unit is not None and rules.last_played_before_unit not in (
        "hours",
        "days",
        "weeks",
        "months",
    ):
        msg = f"last_played_before_unit must be hours/days/weeks/months, got {rules.last_played_before_unit}"
        raise InvalidDataError(msg)


async def read_json(path: str) -> dict[str, Any]:
    """Read a JSON file and return its contents."""
    async with aiofiles.open(path, encoding="utf-8") as fh:
        return cast("dict[str, Any]", json_loads(await fh.read()))


def _atomic_write(path: str, payload: str) -> None:
    """Write payload to a temp file and atomically replace path, cleaning up on failure."""
    tmp = Path(f"{path}.tmp")
    replaced = False
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
        tmp.replace(path)
        replaced = True
    finally:
        # On any failure mid-write, don't leave a stray temp file behind to accumulate.
        if not replaced:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)


async def write_json(path: str, data: dict[str, Any]) -> None:
    """Write data as JSON to a file atomically (temp file + replace, off the event loop)."""
    # The whole write+rename+cleanup runs in one thread so a cancelled await can't race the
    # rename against cleanup; the rename itself is atomic, so the destination is never truncated.
    payload = json_dumps(data, indent=True)
    await asyncio.to_thread(_atomic_write, path, payload)
