"""Field catalog, condition matching and sorting for Rotation playlist steps."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from mashumaro import DataClassDictMixin
from music_assistant_models.enums import ContentType
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import Track
from music_assistant_models.translations import resolve_translation

from music_assistant.helpers.datetime import utc

from .constants import TRANSLATION_OWNER
from .models import ItemKind

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType

    from .models import Condition, SortKey

RANDOM = "random"
STEP_TYPES = ("source", "filter", "sort", "limit", "expand", "flatten")


class FieldType(StrEnum):
    """The type of a field, which decides the operators it supports."""

    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    BOOL = "bool"
    REF = "ref"


OPERATORS: dict[FieldType, tuple[str, ...]] = {
    FieldType.TEXT: ("is", "is_not", "contains", "not_contains", "starts_with", "ends_with"),
    FieldType.NUMBER: ("is", "less_than", "more_than", "between"),
    FieldType.DATE: ("is", "less_than", "more_than", "between", "in_last_days"),
    FieldType.BOOL: ("is",),
    FieldType.REF: ("is", "is_not", "one_of"),
}
NAME_OPERATORS = ("contains", "not_contains", "starts_with", "ends_with")
NEGATIVE_OPERATORS = ("is_not", "not_contains")
AUDIO_QUALITIES = ("lossy", "lossless", "hi_res")
# the moment a never played item sorts at, like the 0 the library stores for it
NEVER_PLAYED = datetime.fromtimestamp(0, UTC)


@dataclass(frozen=True)
class FieldDef:
    """One field of a media item that steps can filter or sort by."""

    name: str
    type: FieldType
    label: str
    sortable: bool
    getter: Callable[[MediaItemType], Any]
    named: bool = False
    # a text field with several values: one must match, or none for a negative operator
    multi: bool = False
    # the media item models do not carry the value, so the engine loads it for library items;
    # the getter gives the value of the other items
    loaded: bool = False
    # the value an item without one sorts by, instead of after all others
    sort_missing: Any = None
    # the only values a reference field takes
    values: tuple[str, ...] = ()
    # the value a sort orders by, when it is not the value of the field itself
    sort_getter: Callable[[MediaItemType], Any] | None = None


@dataclass
class FieldDescription(DataClassDictMixin):
    """What a client needs to know of one field to build conditions and sort keys."""

    name: str
    type: str
    # in English; an API result carries it in the language of the client
    label: str
    operators: list[str]
    sortable: bool
    # the only values a reference field takes; not sent when the field takes any value
    values: list[str] | None = None

    def __post_serialize__(self, d: dict[str, Any]) -> dict[str, Any]:
        """Localize the label when a resolver for the language of the client is active."""
        if (label := field_label(self.name)) is not None:
            d["label"] = label
        if self.values is None:
            d.pop("values", None)
        return d


@dataclass
class MediaTypeDescription(DataClassDictMixin):
    """The fields and sort keys of the items of one media type."""

    fields: list[FieldDescription]
    sort_keys: list[str]


@dataclass
class StepsDescription(DataClassDictMixin):
    """What steps can do, for editors and other clients."""

    step_types: list[str]
    media_types: dict[str, MediaTypeDescription]
    providers: list[dict[str, str]]
    # the JSON Schema of the steps list; not sent when it is not set
    schema: dict[str, Any] | None = None

    def __post_serialize__(self, d: dict[str, Any]) -> dict[str, Any]:
        """Leave out the schema when it is not set."""
        if self.schema is None:
            d.pop("schema", None)
        return d


def field_label(name: str) -> str | None:
    """
    Return the label of a field in the language of the current API client.

    Returns None when no API client is being answered, so callers keep the English label.

    :param name: The name of the field.
    """
    return resolve_translation(f"fields.{name}", owner=TRANSLATION_OWNER)


def operators_for(definition: FieldDef) -> tuple[str, ...]:
    """
    Return the operators a field supports.

    :param definition: The field to look up.
    """
    operators = OPERATORS[definition.type]
    return (*operators, *NAME_OPERATORS) if definition.named else operators


def matches(
    item: MediaItemType,
    kind: ItemKind,
    condition: Condition,
    loaded: Mapping[str, Any] | None = None,
) -> bool:
    """
    Check whether an item satisfies one condition.

    A missing value never matches, whatever the operator; only a text field with several
    values (such as the languages) matches a negative operator when it has no value.

    :param item: The media item to check.
    :param kind: The kind of the item.
    :param condition: The condition to check.
    :param loaded: The values the engine loaded for the item, by field name.
    """
    definition = FIELDS[kind].get(condition.field)
    if definition is None or condition.field == RANDOM:
        return False
    value = _field_value(definition, item, loaded)
    if definition.multi:
        return _match_names(value, condition)
    if value is None or value == []:
        # an item without names has none that matches, like a text field with several values
        return definition.named and condition.op in NEGATIVE_OPERATORS
    try:
        match definition.type:
            case FieldType.TEXT:
                return _match_text(str(value), condition)
            case FieldType.NUMBER:
                return _match_ordered(value, condition, float)
            case FieldType.DATE:
                return _match_date(value, condition)
            case FieldType.BOOL:
                return bool(value) is bool(condition.value) if condition.op == "is" else False
            case FieldType.REF:
                return _match_ref(value, condition)
    except TypeError, ValueError:
        return False


def sort_value(
    item: MediaItemType, kind: ItemKind, field: str, loaded: Mapping[str, Any] | None = None
) -> tuple[Any, ...]:
    """
    Return a sortable key for an item; items without a value sort after all others.

    Sorting by this key with ``reverse=True`` puts the items without a value first,
    so use :func:`sort_items` to order a list in both directions.

    :param item: The media item.
    :param kind: The kind of the item.
    :param field: The field name to sort by.
    :param loaded: The values the engine loaded for the item, by field name.
    """
    definition = FIELDS[kind].get(field)
    if definition is None:
        return (1,)
    if definition.sort_getter is not None:
        value = definition.sort_getter(item)
    else:
        value = _field_value(definition, item, loaded)
    if definition.type == FieldType.REF and definition.sort_getter is None:
        value = value[0][-1] if value else None
    if value is None:
        value = definition.sort_missing
    if value is None:
        return (1,)
    if isinstance(value, str):
        value = value.casefold()
    elif isinstance(value, bool):
        value = int(value)
    return (0, value)


def sort_items[T: MediaItemType](
    items: Sequence[T],
    kind: ItemKind,
    keys: Sequence[SortKey],
    loaded: Callable[[T], Mapping[str, Any]] | None = None,
) -> list[T]:
    """
    Order items by sort keys; items without a value always come last.

    A random key is left out, as the order it gives depends on the seed of the run.

    :param items: The items to order.
    :param kind: The kind of the items.
    :param keys: The sort keys, most important first.
    :param loaded: Returns the values the engine loaded for an item, by field name.
    """
    result = list(items)
    for key in reversed(keys):
        if key.field == RANDOM:
            continue
        values = [
            (sort_value(item, kind, key.field, loaded(item) if loaded else None), item)
            for item in result
        ]
        present = [pair for pair in values if pair[0][0] == 0]
        missing = [pair[1] for pair in values if pair[0][0] != 0]
        present.sort(key=lambda pair: pair[0], reverse=key.descending)
        result = [pair[1] for pair in present] + missing
    return result


def describe(kind: ItemKind | None, providers: list[dict[str, str]]) -> StepsDescription:
    """
    Describe what steps can do, for editors and other clients.

    :param kind: Only describe this item kind, or all kinds when None.
    :param providers: The providers a client may choose from.
    """
    kinds = [kind] if kind is not None else list(ItemKind)
    media_types: dict[str, MediaTypeDescription] = {}
    for item_kind in kinds:
        catalog = FIELDS[item_kind]
        media_types[item_kind.value] = MediaTypeDescription(
            fields=[
                _describe_field(definition)
                for definition in catalog.values()
                if definition.name != RANDOM
            ],
            sort_keys=[definition.name for definition in catalog.values() if definition.sortable],
        )
    return StepsDescription(
        step_types=list(STEP_TYPES), media_types=media_types, providers=providers
    )


def parse_date(value: Any) -> datetime:
    """
    Parse a date or date-time given as ISO text; naive values count as UTC.

    :param value: The value to parse.
    :raises ValueError: When the value is not a date.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise TypeError("not a date")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def quality_rank(
    content_type: ContentType, codec_type: ContentType, bit_depth: int, sample_rate: int
) -> int | None:
    """
    Return the index of an audio format in AUDIO_QUALITIES, or None when it is not known.

    :param content_type: The content type of the format.
    :param codec_type: The codec type of the format.
    :param bit_depth: The bit depth of the format.
    :param sample_rate: The sample rate of the format.
    """
    if content_type == ContentType.UNKNOWN and codec_type == ContentType.UNKNOWN:
        return None
    if not (content_type.is_lossless() or codec_type.is_lossless()):
        return 0
    return 2 if bit_depth > 16 or sample_rate > 48000 else 1


def _describe_field(definition: FieldDef) -> FieldDescription:
    """Return the description of one field."""
    return FieldDescription(
        name=definition.name,
        type=definition.type.value,
        label=definition.label,
        operators=list(operators_for(definition)),
        sortable=definition.sortable,
        values=list(definition.values) if definition.values else None,
    )


def _field_value(
    definition: FieldDef, item: MediaItemType, loaded: Mapping[str, Any] | None
) -> Any:
    """Return the value of a field for an item, from the loaded values when the engine loads it."""
    if definition.loaded and loaded is not None and definition.name in loaded:
        return loaded[definition.name]
    return definition.getter(item)


def _text(item: Any, attr: str = "name") -> str | None:
    """Return a text attribute or None when empty."""
    return getattr(item, attr, None) or None


def _entity(item: Any) -> tuple[str, ...]:
    """Return the identifiers of one referenced item: its uris and, last, its name."""
    uris = [item.uri] if item.uri else []
    uris.extend(
        create_uri(item.media_type, mapping.provider_instance, mapping.item_id)
        for mapping in getattr(item, "provider_mappings", None) or ()
    )
    return (*dict.fromkeys(uris), item.name)


def _artists(item: MediaItemType) -> list[tuple[str, ...]]:
    return [_entity(artist) for artist in getattr(item, "artists", None) or ()]


def _album(item: MediaItemType) -> list[tuple[str, ...]]:
    album = getattr(item, "album", None)
    return [_entity(album)] if album is not None else []


def _album_type(item: MediaItemType) -> list[tuple[str, ...]]:
    album_type = getattr(item, "album_type", None)
    return [(album_type.value,)] if album_type is not None else []


def _genres(item: MediaItemType) -> list[tuple[str, ...]]:
    genres = item.metadata.genres if item.metadata else None
    return [(genre,) for genre in sorted(genres or ())]


def _providers(item: MediaItemType) -> list[tuple[str, ...]]:
    """Return the providers of an item: for a library item those that have it in their library."""
    # like the provider filter of the library, which needs a mapping in the provider's library
    in_library = item.provider == "library"
    result: set[tuple[str, ...]] = {
        (mapping.provider_instance, mapping.provider_domain)
        for mapping in item.provider_mappings
        if mapping.in_library or not in_library
    }
    if not in_library:
        result.add((item.provider,))
    return sorted(result)


def _metadata_texts(attr: str) -> Callable[[MediaItemType], list[str]]:
    """Return a getter of a metadata attribute that holds one text or a list of texts."""

    def getter(item: MediaItemType) -> list[str]:
        value = getattr(item.metadata, attr, None) if item.metadata else None
        if not value:
            return []
        return [value] if isinstance(value, str) else [str(part) for part in value if part]

    return getter


def _popularity(item: MediaItemType) -> int | None:
    return item.metadata.popularity if item.metadata else None


def _audio_quality(item: MediaItemType) -> list[tuple[str, ...]]:
    """Return the quality of the best available mapping whose format is known."""
    ranks = [
        rank
        for mapping in getattr(item, "provider_mappings", None) or ()
        if mapping.available
        and (
            rank := quality_rank(
                mapping.audio_format.content_type,
                mapping.audio_format.codec_type,
                mapping.audio_format.bit_depth,
                mapping.audio_format.sample_rate,
            )
        )
        is not None
    ]
    return [(AUDIO_QUALITIES[max(ranks)],)] if ranks else []


def _year(item: MediaItemType) -> int | None:
    if isinstance(item, Track):
        year = item.album.year if item.album is not None else None
    else:
        year = getattr(item, "year", None)
    return year or None


def _explicit(item: MediaItemType) -> bool | None:
    return item.metadata.explicit if item.metadata else None


def _favorite(item: MediaItemType) -> bool:
    return bool(item.favorite)


def _last_played(item: MediaItemType) -> datetime | None:
    stamp = getattr(item, "last_played", 0)
    return datetime.fromtimestamp(stamp, UTC) if stamp else None


def _track_number(item: MediaItemType) -> int | None:
    return getattr(item, "track_number", 0) or None


def _track_position(item: MediaItemType) -> tuple[int, int] | None:
    # like the track number sort of the music controllers: the disc first, a digital release
    # stores its single disc as disc 0 or 1
    track_number = getattr(item, "track_number", 0)
    if not track_number:
        return None
    return (getattr(item, "disc_number", 0) or 1, track_number)


def _disc_number(item: MediaItemType) -> int | None:
    return getattr(item, "disc_number", 0) or None


def _duration(item: MediaItemType) -> int | None:
    return getattr(item, "duration", 0) or None


def _name(label: str, field_name: str = "name") -> FieldDef:
    return FieldDef(field_name, FieldType.TEXT, label, True, _text)


def _version(item: MediaItemType) -> str:
    return getattr(item, "version", None) or ""


def _no_value(_item: MediaItemType) -> None:
    return None


def _never_played(_item: MediaItemType) -> int:
    return 0


def _match_text(value: str, condition: Condition) -> bool:
    text = value.casefold()
    wanted = str(condition.value).casefold()
    match condition.op:
        case "is":
            return text == wanted
        case "is_not":
            return text != wanted
        case "contains":
            return wanted in text
        case "not_contains":
            return wanted not in text
        case "starts_with":
            return text.startswith(wanted)
        case "ends_with":
            return text.endswith(wanted)
    return False


def _match_ordered(value: Any, condition: Condition, convert: Callable[[Any], Any]) -> bool:
    current = convert(value)
    match condition.op:
        case "is":
            return bool(current == convert(condition.value))
        case "less_than":
            return bool(current < convert(condition.value))
        case "more_than":
            return bool(current > convert(condition.value))
        case "between":
            low, high = (convert(part) for part in condition.value)
            return bool(low <= current <= high)
    return False


def _match_date(value: datetime, condition: Condition) -> bool:
    # a date-only value covers its whole UTC day; a value with a time is one moment
    current = _as_utc(value)
    match condition.op:
        case "in_last_days":
            return current >= utc() - timedelta(days=int(condition.value))
        case "is":
            return current.date() == _as_utc(parse_date(condition.value)).date()
        case "less_than":
            return current < _start(condition.value)
        case "more_than":
            return _after(current, condition.value)
        case "between":
            low, high = condition.value
            return _start(low) <= current and not _after(current, high)
    return False


def _as_utc(value: datetime) -> datetime:
    """Return a moment in UTC; a naive moment counts as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _day_only(value: Any) -> date | None:
    """Return the day of a date-only value such as 2024-05-31, or None for any other value."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _start(value: Any) -> datetime:
    """Return the first moment of a date value: midnight UTC of a date-only value."""
    if (day := _day_only(value)) is not None:
        return datetime.combine(day, time(), UTC)
    return _as_utc(parse_date(value))


def _after(current: datetime, value: Any) -> bool:
    """Return whether a moment is after a date value; a date-only value ends with its day."""
    if (day := _day_only(value)) is not None:
        return current.date() > day
    return current > _as_utc(parse_date(value))


def _match_ref(value: list[tuple[str, ...]], condition: Condition) -> bool:
    if condition.op in NAME_OPERATORS:
        return _match_names([entity[-1] for entity in value], condition)
    known = {identifier.casefold() for entity in value for identifier in entity}
    if condition.op == "one_of":
        return any(str(wanted).casefold() in known for wanted in condition.value)
    found = str(condition.value).casefold() in known
    if condition.op == "is":
        return found
    return not found if condition.op == "is_not" else False


def _match_names(names: list[str], condition: Condition) -> bool:
    if condition.op in NEGATIVE_OPERATORS:
        return all(_match_text(name, condition) for name in names)
    return any(_match_text(name, condition) for name in names)


def _catalog(*defs: FieldDef) -> dict[str, FieldDef]:
    return {definition.name: definition for definition in defs}


# the catalog is built last, from the getters above
_FAVORITE = FieldDef("favorite", FieldType.BOOL, "Favorite", True, _favorite)
_GENRE = FieldDef("genre", FieldType.REF, "Genre", False, _genres, named=True)
_PROVIDER = FieldDef("provider", FieldType.REF, "Provider", False, _providers)
_DATE_ADDED = FieldDef("date_added", FieldType.DATE, "Date added", True, lambda i: i.date_added)
_VERSION = FieldDef("version", FieldType.TEXT, "Version", False, _version)
_RANDOM = FieldDef(RANDOM, FieldType.NUMBER, "Random", True, lambda _item: 0)
_METADATA = (
    FieldDef("mood", FieldType.TEXT, "Mood", False, _metadata_texts("mood"), multi=True),
    FieldDef("style", FieldType.TEXT, "Style", False, _metadata_texts("style"), multi=True),
    FieldDef("label", FieldType.TEXT, "Label", False, _metadata_texts("label"), multi=True),
    FieldDef(
        "language", FieldType.TEXT, "Language", False, _metadata_texts("languages"), multi=True
    ),
    FieldDef("popularity", FieldType.NUMBER, "Popularity", True, _popularity),
)
_AUDIO_QUALITY = FieldDef(
    "audio_quality",
    FieldType.REF,
    "Audio quality",
    False,
    _audio_quality,
    values=AUDIO_QUALITIES,
)
# the media item models do not carry these, so the engine loads them for library items
# a library album also counts the formats of its tracks
_ALBUM_AUDIO_QUALITY = replace(_AUDIO_QUALITY, loaded=True)
_PLAY_COUNT = FieldDef(
    "play_count", FieldType.NUMBER, "Play count (all users)", True, _never_played, loaded=True
)
_LAST_PLAYED_LOADED = FieldDef(
    "last_played",
    FieldType.DATE,
    "Last played (all users)",
    True,
    _no_value,
    loaded=True,
    sort_missing=NEVER_PLAYED,
)
_TRACK_COUNT = FieldDef(
    "track_count", FieldType.NUMBER, "Number of tracks", True, _no_value, loaded=True
)
_DATE_FAVORITED = FieldDef(
    "date_favorited", FieldType.DATE, "Date favorited", True, _no_value, loaded=True
)


FIELDS: dict[ItemKind, dict[str, FieldDef]] = {
    ItemKind.ARTIST: _catalog(
        _name("Name"),
        _FAVORITE,
        _GENRE,
        _PROVIDER,
        _DATE_ADDED,
        _DATE_FAVORITED,
        *_METADATA,
        _PLAY_COUNT,
        _LAST_PLAYED_LOADED,
        _RANDOM,
    ),
    ItemKind.ALBUM: _catalog(
        _name("Name"),
        _VERSION,
        FieldDef("artist", FieldType.REF, "Artist", True, _artists, named=True),
        FieldDef("year", FieldType.NUMBER, "Year", True, _year),
        FieldDef("album_type", FieldType.REF, "Album type", True, _album_type),
        _TRACK_COUNT,
        _GENRE,
        _FAVORITE,
        _PROVIDER,
        _ALBUM_AUDIO_QUALITY,
        _DATE_ADDED,
        _DATE_FAVORITED,
        *_METADATA,
        _PLAY_COUNT,
        _LAST_PLAYED_LOADED,
        _RANDOM,
    ),
    ItemKind.TRACK: _catalog(
        _name("Title", "title"),
        _VERSION,
        FieldDef("artist", FieldType.REF, "Artist", True, _artists, named=True),
        FieldDef("album", FieldType.REF, "Album", True, _album, named=True),
        FieldDef(
            "track_number",
            FieldType.NUMBER,
            "Track number",
            True,
            _track_number,
            sort_getter=_track_position,
        ),
        FieldDef("disc_number", FieldType.NUMBER, "Disc number", True, _disc_number),
        FieldDef("duration", FieldType.NUMBER, "Duration (seconds)", True, _duration),
        FieldDef("year", FieldType.NUMBER, "Year", True, _year),
        FieldDef("explicit", FieldType.BOOL, "Explicit", True, _explicit),
        _GENRE,
        _FAVORITE,
        _PROVIDER,
        _AUDIO_QUALITY,
        _DATE_FAVORITED,
        *_METADATA,
        FieldDef(
            "last_played",
            FieldType.DATE,
            "Last played (all users)",
            True,
            _last_played,
            sort_missing=NEVER_PLAYED,
        ),
        _PLAY_COUNT,
        _RANDOM,
    ),
}
