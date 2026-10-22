"""
The shared pipeline of the listings assembled in memory.

A container listing (the tracks of an album or playlist, the albums of an artist, the
versions of a track) is assembled from the library and the providers as a whole, and this
pipeline then searches, sorts and pages it the way SQL does for the library listings. The
sort fields a listing offers come from the sort-option table; the fields only the database
holds (play counts, the modification and favorite moments) have no in-memory sort.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from typing import Any, Final

from music_assistant_models.enums import ListingType, SortDirection, SortField
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.helpers import create_safe_string, create_sort_name
from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItem

from music_assistant.controllers.music.constants import LEGACY_SORT_KEYS
from music_assistant.controllers.music.sorting import LISTING_SORT_OPTIONS

# anything a listing holds: a media item, a bare reference to one or a browse folder
type ListingItem = MediaItem | ItemMapping | BrowseFolder


def legacy_sort(sort_key: str) -> tuple[SortField, SortDirection | None]:
    """
    Return the sort field and direction a deprecated sort key stands for.

    :param sort_key: The deprecated sort key an outdated client sends.
    :return: The sort field and its direction, None for a field without a direction.
    :raises InvalidDataError: When the key is unknown.
    """
    if (sort := LEGACY_SORT_KEYS.get(sort_key)) is None:
        raise InvalidDataError(f"Unknown sort key: {sort_key}")
    return sort


def resolve_listing_sort(
    listing: ListingType,
    sort_field: SortField | None = None,
    sort_direction: SortDirection | None = None,
    order_by: str | None = None,
) -> tuple[SortField, SortDirection | None]:
    """
    Resolve the sort requested for a listing into the field and the direction to sort on.

    The deprecated ``order_by`` key only counts when no ``sort_field`` is given.

    :param listing: The listing being sorted.
    :param sort_field: The requested sort field, the listing's default when omitted.
    :param sort_direction: The requested direction, the field's default for this listing
        when omitted.
    :param order_by: The deprecated sort key an outdated client sends.
    :return: The sort field and its direction, None for a field without a direction.
    :raises InvalidDataError: When the listing does not offer the field or the key is unknown.
    """
    if sort_field is None and order_by:
        sort_field, sort_direction = legacy_sort(order_by)
    # without a requested field the first option, the listing's default, matches
    options = LISTING_SORT_OPTIONS[listing]
    option = next((x for x in options if sort_field in (None, x.field)), None)
    if option is None:
        raise InvalidDataError(f"Sort field {sort_field} is not supported for {listing.value}")
    if not option.supports_direction:
        return option.field, None
    return option.field, sort_direction or option.default_direction


def apply_listing[ItemT: ListingItem](
    items: Sequence[ItemT],
    listing: ListingType,
    *,
    search: str | None = None,
    sort_field: SortField | None = None,
    sort_direction: SortDirection | None = None,
    limit: int | None = None,
    offset: int = 0,
    random_seed: int | None = None,
) -> list[ItemT]:
    """
    Search, sort and page a listing assembled in memory.

    Items that sort the same keep the order the source lists them in, the tracks of one album
    their disc and track order.

    :param items: The whole listing, in the order the source lists it.
    :param listing: The listing the items belong to, which decides the sort options.
    :param search: Only keep the items whose name, album name or artist name contains this
        text; case and accents do not matter.
    :param sort_field: Sort field, the listing's default when omitted.
    :param sort_direction: Sort direction, the field's default for this listing when omitted.
    :param limit: Maximum number of items to return; None (or 0) returns them all.
    :param offset: Number of items to skip.
    :param random_seed: Keep random ordering stable across pages with the same seed.
    :raises InvalidDataError: When the listing does not offer the sort field.
    """
    field, direction = resolve_listing_sort(listing, sort_field, sort_direction)
    term = _normalize(search) if search else ""
    result = [item for item in items if _matches(item, term)] if term else list(items)
    if field == SortField.RANDOM:
        if random_seed is None:
            random.shuffle(result)
        else:
            # A cache refill can reorder identical contents; anchor the shuffle to identities
            # without deduplicating repeated playlist entries.
            result.sort(
                key=lambda item: (
                    item.media_type.value,
                    item.provider,
                    item.path if isinstance(item, BrowseFolder) else item.item_id,
                )
            )
            random.Random(random_seed).shuffle(result)  # noqa: S311 - reproducible order, not security
    elif field != SortField.ORIGINAL:
        if (sort_key := _SORT_KEYS.get(field)) is None:
            raise InvalidDataError(f"Sort field {field.value} cannot be sorted in memory")
        if tie_key := _TIE_KEYS.get(field):
            result.sort(key=tie_key)
        result.sort(key=sort_key, reverse=direction == SortDirection.DESC)
    return result[offset : offset + limit] if limit else result[offset:]


def _normalize(name: str) -> str:
    """Return a name the way the library's search columns hold it."""
    return create_safe_string(name, True, True)


def _matches(item: ListingItem, term: str) -> bool:
    """Return whether the name of the item, of its album or of one of its artists holds the term."""
    names = [_name(item)]
    if album := getattr(item, "album", None):
        names.append(album.name)
    names.extend(artist.name for artist in getattr(item, "artists", ()))
    return any(term in _normalize(name) for name in names)


def _name(item: ListingItem) -> str:
    # a folder can go by its translation key alone, translated only when sent to a client
    return item.name or getattr(item, "translation_key", None) or ""


def _sort_name(item: ListingItem) -> str:
    return _normalize(item.sort_name or create_sort_name(item.name))


def _date_added(item: ListingItem) -> float:
    # an item without a date sorts as the oldest
    date_added = getattr(item, "date_added", None)
    return date_added.timestamp() if date_added else 0.0


def _artist_name(item: ListingItem) -> str:
    artists = getattr(item, "artists", None)
    return _normalize(artists[0].name) if artists else ""


def _album_name(item: ListingItem) -> tuple[str, str]:
    # albums that share a name stay apart
    album = getattr(item, "album", None)
    return (_normalize(album.name), album.uri or "") if album else ("", "")


def _track_number(item: ListingItem) -> tuple[int, int]:
    # a digital release stores its single disc as disc 0 or 1
    return (getattr(item, "disc_number", 0) or 1, getattr(item, "track_number", 0) or 0)


# the sort key of every field an item carries itself; ORIGINAL and RANDOM need none
_SORT_KEYS: Final[dict[SortField, Callable[[ListingItem], Any]]] = {
    SortField.NAME: lambda item: _normalize(_name(item)),
    SortField.SORT_NAME: _sort_name,
    SortField.TIMESTAMP_ADDED: _date_added,
    SortField.LAST_PLAYED: lambda item: getattr(item, "last_played", 0) or 0,
    SortField.DURATION: lambda item: getattr(item, "duration", 0) or 0,
    SortField.YEAR: lambda item: getattr(item, "year", 0) or 0,
    SortField.POSITION: lambda item: getattr(item, "position", 0) or 0,
    SortField.ARTIST_NAME: _artist_name,
    SortField.TRACK_NUMBER: _track_number,
    SortField.ALBUM_NAME: _album_name,
    SortField.PROVIDER: lambda item: item.provider,
}

# the order among the items a sort field ties, whichever the direction
_TIE_KEYS: Final[dict[SortField, Callable[[ListingItem], Any]]] = {
    # the tracks of one album keep their disc and track order
    SortField.ALBUM_NAME: _track_number,
}
