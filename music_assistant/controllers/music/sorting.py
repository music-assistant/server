"""Sort field definitions and metadata for media listings."""

from __future__ import annotations

from typing import Final

from music_assistant_models.api import SortOptionInfo
from music_assistant_models.enums import MediaType, SortDirection, SortField

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
}

# the sort fields the library listing of each media type offers
MEDIA_TYPE_SORT_FIELDS: Final[dict[MediaType, list[SortField]]] = {
    MediaType.ARTIST: [
        SortField.NAME,
        SortField.SORT_NAME,
        SortField.TIMESTAMP_ADDED,
        SortField.TIMESTAMP_MODIFIED,
        SortField.LAST_PLAYED,
        SortField.PLAY_COUNT,
        SortField.RANDOM,
        SortField.RANDOM_PLAY_COUNT,
    ],
    MediaType.ALBUM: [
        SortField.NAME,
        SortField.SORT_NAME,
        SortField.TIMESTAMP_ADDED,
        SortField.TIMESTAMP_MODIFIED,
        SortField.LAST_PLAYED,
        SortField.PLAY_COUNT,
        SortField.YEAR,
        SortField.ARTIST_NAME,
        SortField.RANDOM,
        SortField.RANDOM_PLAY_COUNT,
    ],
    MediaType.TRACK: [
        SortField.NAME,
        SortField.SORT_NAME,
        SortField.TIMESTAMP_ADDED,
        SortField.TIMESTAMP_MODIFIED,
        SortField.LAST_PLAYED,
        SortField.PLAY_COUNT,
        SortField.DURATION,
        SortField.ARTIST_NAME,
        SortField.RANDOM,
        SortField.RANDOM_PLAY_COUNT,
    ],
    MediaType.RADIO: [
        SortField.NAME,
        SortField.SORT_NAME,
        SortField.TIMESTAMP_ADDED,
        SortField.TIMESTAMP_MODIFIED,
        SortField.LAST_PLAYED,
        SortField.PLAY_COUNT,
        SortField.RANDOM,
        SortField.RANDOM_PLAY_COUNT,
    ],
    MediaType.PLAYLIST: [
        SortField.NAME,
        SortField.SORT_NAME,
        SortField.TIMESTAMP_ADDED,
        SortField.TIMESTAMP_MODIFIED,
        SortField.LAST_PLAYED,
        SortField.PLAY_COUNT,
        SortField.RANDOM,
        SortField.RANDOM_PLAY_COUNT,
    ],
    MediaType.AUDIOBOOK: [
        SortField.NAME,
        SortField.SORT_NAME,
        SortField.TIMESTAMP_ADDED,
        SortField.TIMESTAMP_MODIFIED,
        SortField.LAST_PLAYED,
        SortField.PLAY_COUNT,
        SortField.DURATION,
        SortField.RANDOM,
        SortField.RANDOM_PLAY_COUNT,
    ],
    MediaType.PODCAST: [
        SortField.NAME,
        SortField.SORT_NAME,
        SortField.TIMESTAMP_ADDED,
        SortField.TIMESTAMP_MODIFIED,
        SortField.LAST_PLAYED,
        SortField.PLAY_COUNT,
        SortField.RANDOM,
        SortField.RANDOM_PLAY_COUNT,
    ],
    MediaType.GENRE: [
        SortField.NAME,
        SortField.SORT_NAME,
        SortField.TIMESTAMP_ADDED,
        SortField.TIMESTAMP_MODIFIED,
        SortField.LAST_PLAYED,
        SortField.PLAY_COUNT,
        SortField.RANDOM,
        SortField.RANDOM_PLAY_COUNT,
    ],
}


def get_default_direction(field: SortField) -> SortDirection:
    """
    Get the default sort direction of a field.

    :param field: The sort field.
    :return: The field's default direction, ASC for fields without one.
    """
    return SORT_FIELD_DEFINITIONS[field].default_direction or SortDirection.ASC


def get_sort_options_for_media_type(media_type: MediaType) -> list[SortOptionInfo]:
    """
    Get the sort options the library listing of a media type offers.

    :param media_type: The media type of the listing.
    """
    return [SORT_FIELD_DEFINITIONS[field] for field in MEDIA_TYPE_SORT_FIELDS.get(media_type, [])]
