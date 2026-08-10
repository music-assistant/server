"""Sort field definitions and metadata for media listings."""

from __future__ import annotations

from dataclasses import dataclass

from music_assistant_models.enums import MediaType, SortDirection, SortField


@dataclass
class SortFieldDefinition:
    """
    Definition and metadata for a sort field.

    Used internally by the server to provide sort options to clients.
    """

    field: SortField
    supports_direction: bool
    default_direction: SortDirection | None = None
    label_key: str | None = None


@dataclass
class SortOptionInfo:
    """
    Sort option information returned by the API.

    Used by clients to build UI for sorting controls.
    """

    field: str
    supports_direction: bool
    default_direction: str | None = None
    label_key: str | None = None


# Complete definitions for all sort fields
SORT_FIELD_DEFINITIONS: dict[SortField, SortFieldDefinition] = {
    SortField.NAME: SortFieldDefinition(
        field=SortField.NAME,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="name",
    ),
    SortField.SORT_NAME: SortFieldDefinition(
        field=SortField.SORT_NAME,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="sort_name",
    ),
    SortField.TIMESTAMP_ADDED: SortFieldDefinition(
        field=SortField.TIMESTAMP_ADDED,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="timestamp_added",
    ),
    SortField.TIMESTAMP_MODIFIED: SortFieldDefinition(
        field=SortField.TIMESTAMP_MODIFIED,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="timestamp_modified",
    ),
    SortField.LAST_PLAYED: SortFieldDefinition(
        field=SortField.LAST_PLAYED,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="last_played",
    ),
    SortField.PLAY_COUNT: SortFieldDefinition(
        field=SortField.PLAY_COUNT,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="play_count",
    ),
    SortField.DURATION: SortFieldDefinition(
        field=SortField.DURATION,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="duration",
    ),
    SortField.YEAR: SortFieldDefinition(
        field=SortField.YEAR,
        supports_direction=True,
        default_direction=SortDirection.DESC,
        label_key="year",
    ),
    SortField.POSITION: SortFieldDefinition(
        field=SortField.POSITION,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="position",
    ),
    SortField.ARTIST_NAME: SortFieldDefinition(
        field=SortField.ARTIST_NAME,
        supports_direction=True,
        default_direction=SortDirection.ASC,
        label_key="artist_name",
    ),
    SortField.RANDOM: SortFieldDefinition(
        field=SortField.RANDOM,
        supports_direction=False,
        label_key="random",
    ),
    SortField.RANDOM_PLAY_COUNT: SortFieldDefinition(
        field=SortField.RANDOM_PLAY_COUNT,
        supports_direction=False,
        label_key="random_play_count",
    ),
}

# Maps each MediaType to its available sort fields
MEDIA_TYPE_SORT_FIELDS: dict[MediaType, list[SortField]] = {
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
        SortField.DURATION,
        SortField.RANDOM,
        SortField.RANDOM_PLAY_COUNT,
    ],
}


def get_sort_options_for_media_type(media_type: MediaType) -> list[SortOptionInfo]:
    """
    Get available sort options for a media type.

    :param media_type: The MediaType to get sort options for.
    :return: List of SortOptionInfo for the media type.
    """
    fields = MEDIA_TYPE_SORT_FIELDS.get(media_type, [])
    return [
        SortOptionInfo(
            field=definition.field.value,
            supports_direction=definition.supports_direction,
            default_direction=(
                definition.default_direction.value if definition.default_direction else None
            ),
            label_key=definition.label_key,
        )
        for field in fields
        if (definition := SORT_FIELD_DEFINITIONS.get(field))
    ]
