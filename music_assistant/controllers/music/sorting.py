"""Sort field definitions and the sort options of media listings."""

from __future__ import annotations

from typing import Final

from music_assistant_models.api import SortOptionInfo
from music_assistant_models.enums import ListingType, MediaType, SortDirection, SortField

# what each sort field offers: whether it takes a direction and its default, and the label key
SORT_FIELD_DEFINITIONS: Final[dict[SortField, SortOptionInfo]] = {
    SortField.NAME: SortOptionInfo(
        field=SortField.NAME,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="name",
    ),
    SortField.SORT_NAME: SortOptionInfo(
        field=SortField.SORT_NAME,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="sort_name",
    ),
    SortField.TIMESTAMP_ADDED: SortOptionInfo(
        field=SortField.TIMESTAMP_ADDED,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="timestamp_added",
    ),
    SortField.TIMESTAMP_MODIFIED: SortOptionInfo(
        field=SortField.TIMESTAMP_MODIFIED,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="timestamp_modified",
    ),
    SortField.LAST_PLAYED: SortOptionInfo(
        field=SortField.LAST_PLAYED,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="last_played",
    ),
    SortField.PLAY_COUNT: SortOptionInfo(
        field=SortField.PLAY_COUNT,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="play_count",
    ),
    SortField.DURATION: SortOptionInfo(
        field=SortField.DURATION,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="duration",
    ),
    SortField.YEAR: SortOptionInfo(
        field=SortField.YEAR,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="year",
    ),
    SortField.POSITION: SortOptionInfo(
        field=SortField.POSITION,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="position",
    ),
    SortField.ARTIST_NAME: SortOptionInfo(
        field=SortField.ARTIST_NAME,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="artist_name",
    ),
    SortField.RANDOM: SortOptionInfo(
        field=SortField.RANDOM,
        supports_direction=False,
        label_key="random",
    ),
    SortField.RANDOM_PLAY_COUNT: SortOptionInfo(
        field=SortField.RANDOM_PLAY_COUNT,
        supports_direction=False,
        label_key="random_play_count",
    ),
    SortField.TRACK_NUMBER: SortOptionInfo(
        field=SortField.TRACK_NUMBER,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="track_number",
    ),
    SortField.ALBUM_NAME: SortOptionInfo(
        field=SortField.ALBUM_NAME,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="album_name",
    ),
    SortField.PROVIDER: SortOptionInfo(
        field=SortField.PROVIDER,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="provider",
    ),
    SortField.FAVORITE_TIMESTAMP: SortOptionInfo(
        field=SortField.FAVORITE_TIMESTAMP,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="favorite_timestamp",
    ),
    SortField.ORIGINAL: SortOptionInfo(
        field=SortField.ORIGINAL,
        supports_direction=False,
        label_key="original",
    ),
}

# the sort fields every library listing offers, the listing default first
_LIBRARY_SORT_FIELDS: Final[tuple[SortField, ...]] = (
    SortField.SORT_NAME,
    SortField.NAME,
    SortField.TIMESTAMP_ADDED,
    SortField.TIMESTAMP_MODIFIED,
    SortField.LAST_PLAYED,
    SortField.PLAY_COUNT,
    SortField.FAVORITE_TIMESTAMP,
)


def _library_sort_options(*own_fields: SortField) -> tuple[SortOptionInfo, ...]:
    """Return the sort options of a library listing: the shared fields, its own, then random."""
    fields = (*_LIBRARY_SORT_FIELDS, *own_fields, SortField.RANDOM, SortField.RANDOM_PLAY_COUNT)
    return tuple(SORT_FIELD_DEFINITIONS[field] for field in fields)


# the sort options each listing offers, the first one being the listing's default
LISTING_SORT_OPTIONS: Final[dict[ListingType, tuple[SortOptionInfo, ...]]] = {
    ListingType.LIBRARY_ARTISTS: _library_sort_options(),
    ListingType.LIBRARY_ALBUMS: _library_sort_options(SortField.YEAR, SortField.ARTIST_NAME),
    ListingType.LIBRARY_TRACKS: _library_sort_options(SortField.DURATION, SortField.ARTIST_NAME),
    ListingType.LIBRARY_PLAYLISTS: _library_sort_options(),
    ListingType.LIBRARY_RADIOS: _library_sort_options(),
    ListingType.LIBRARY_AUDIOBOOKS: _library_sort_options(SortField.DURATION),
    ListingType.LIBRARY_PODCASTS: _library_sort_options(),
    ListingType.LIBRARY_GENRES: _library_sort_options(),
}

# the library listing of each media type
LIBRARY_LISTINGS: Final[dict[MediaType, ListingType]] = {
    MediaType.ARTIST: ListingType.LIBRARY_ARTISTS,
    MediaType.ALBUM: ListingType.LIBRARY_ALBUMS,
    MediaType.TRACK: ListingType.LIBRARY_TRACKS,
    MediaType.PLAYLIST: ListingType.LIBRARY_PLAYLISTS,
    MediaType.RADIO: ListingType.LIBRARY_RADIOS,
    MediaType.AUDIOBOOK: ListingType.LIBRARY_AUDIOBOOKS,
    MediaType.PODCAST: ListingType.LIBRARY_PODCASTS,
    MediaType.GENRE: ListingType.LIBRARY_GENRES,
}

# the sort fields the library listing of each media type offers, the listing default first
MEDIA_TYPE_SORT_FIELDS: Final[dict[MediaType, tuple[SortField, ...]]] = {
    media_type: tuple(option.field for option in LISTING_SORT_OPTIONS[listing])
    for media_type, listing in LIBRARY_LISTINGS.items()
}


def get_default_direction(field: SortField) -> SortDirection:
    """
    Get the default sort direction of a field.

    :param field: The sort field.
    :return: The field's default direction, ASC for fields without one.
    """
    return SORT_FIELD_DEFINITIONS[field].default_direction or SortDirection.ASC
