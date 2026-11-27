"""Filter tracks from explicitly selected smart playlist sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.enums import AlbumType
from music_assistant_models.media_items import Album

from music_assistant.providers.smart_playlist.helpers import LOGIC_AND

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.media_items import Artist, ItemMapping, Track

    from music_assistant.providers.smart_playlist.helpers import SmartPlaylistRules

type ItemKey = tuple[str, str]


@dataclass(slots=True)
class SourceFilters:
    """Apply smart playlist filters to selected tracks and their library equivalents."""

    rules: SmartPlaylistRules
    artist_keys: set[ItemKey]
    album_keys: set[ItemKey]
    excluded_artist_keys: set[ItemKey]
    excluded_album_keys: set[ItemKey]
    folder_ids: set[str]
    genre_matches: dict[int, set[ItemKey]]
    genre_names: dict[int, str]
    excluded_genres: set[ItemKey]
    excluded_genre_names: set[str]
    allowed_album_ids: set[int]
    excluded_album_ids: set[int]

    def matches(self, track: Track, library: Track | None) -> bool:
        """Return whether the track passes all applicable rules."""
        return self._matches_conditions(track, library) and self._passes_post_filters(
            track, library
        )

    @staticmethod
    def _matches_entity(
        items: Sequence[ItemMapping | Artist | Album], keys: set[ItemKey]
    ) -> bool | None:
        if not items:
            return None
        if {(item.provider, item.item_id) for item in items} & keys:
            return True
        if any(provider == item.provider for item in items for provider, _ in keys):
            return False
        return None

    def _matches_conditions(self, track: Track, library: Track | None) -> bool:
        rules = self.rules
        effective = library or track
        key = (track.provider, track.item_id)
        genres = {genre.lower() for genre in track.metadata.genres or set()}
        conditions: list[bool | None] = []
        if rules.genre_ids:
            checks: list[bool | None] = []
            for genre_id in rules.genre_ids:
                name = self.genre_names[genre_id].lower()
                if key in self.genre_matches[genre_id]:
                    checks.append(True)
                elif library is not None:
                    checks.append(False if library.metadata.genres else None)
                elif name and name in genres:
                    checks.append(True)
                elif genres and name:
                    checks.append(False)
                else:
                    checks.append(None)
            conditions.append(
                (False if False in checks else None if None in checks else True)
                if rules.logic == LOGIC_AND
                else (True if True in checks else None if None in checks else False)
            )
        if rules.artist_ids:
            match = self._matches_entity(list(effective.artists), self.artist_keys)
            conditions.append(match)
        if rules.album_ids:
            match = self._matches_entity(
                [effective.album] if effective.album else [], self.album_keys
            )
            conditions.append(match)
        if rules.folder_paths:
            conditions.append(library.item_id in self.folder_ids if library else None)
        if rules.favorites_only:
            conditions.append(track.favorite is True if library else track.favorite)
        if not conditions:
            return True
        if rules.logic == LOGIC_AND:
            return False not in conditions
        return any(condition is not False for condition in conditions)

    def _passes_post_filters(self, track: Track, library: Track | None) -> bool:
        rules = self.rules
        effective = library or track
        key = (track.provider, track.item_id)
        genres = {genre.lower() for genre in track.metadata.genres or set()}
        if track.uri in rules.excluded_track_uris:
            return False
        if self._matches_entity(list(effective.artists), self.excluded_artist_keys) is True:
            return False
        if (
            self._matches_entity(
                [effective.album] if effective.album else [], self.excluded_album_keys
            )
            is True
        ):
            return False
        if key in self.excluded_genres or (library is None and genres & self.excluded_genre_names):
            return False
        if rules.exclude_favorites and track.favorite is True:
            return False
        return self._passes_metadata_filters(track, library)

    def _passes_metadata_filters(self, track: Track, library: Track | None) -> bool:
        rules = self.rules
        effective = library or track
        explicit = effective.metadata.explicit
        if explicit is None:
            explicit = track.metadata.explicit
        if rules.explicit is not None and explicit is not None and explicit != rules.explicit:
            return False
        popularity = effective.metadata.popularity
        if popularity is None:
            popularity = track.metadata.popularity
        if rules.min_popularity is not None and popularity is not None:
            if popularity < rules.min_popularity:
                return False
        year = effective.album.year if effective.album else None
        if year is None and track.album:
            year = track.album.year
        if year is not None:
            if rules.year_from is not None and year < rules.year_from:
                return False
            if rules.year_to is not None and year > rules.year_to:
                return False
        if library and library.album and library.album.provider == "library":
            album_id = int(library.album.item_id)
            if rules.album_types and album_id not in self.allowed_album_ids:
                return False
            if album_id in self.excluded_album_ids:
                return False
        elif (
            isinstance(effective.album, Album)
            and effective.album.album_type
            and effective.album.album_type != AlbumType.UNKNOWN
        ):
            album_type = effective.album.album_type.value
            if rules.album_types and album_type not in rules.album_types:
                return False
            if album_type in rules.excluded_album_types:
                return False
        duration = effective.duration or track.duration
        if duration:
            if rules.min_duration is not None and duration < rules.min_duration:
                return False
            if rules.max_duration is not None and duration > rules.max_duration:
                return False
        return True
