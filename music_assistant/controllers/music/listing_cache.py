"""
The cache of the listings assembled from the providers.

A container's listing is assembled once and kept as a whole for a while, so paging, sorting
and searching it never ask the providers again. Entries are grouped by the container's uri,
which is what an edit or refresh of the container drops.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import MediaItem

from music_assistant.controllers.music.constants import (
    CACHE_CATEGORY_LISTINGS,
    LISTING_CACHE_EXPIRATION,
)
from music_assistant.controllers.music.favorites import with_user_favorites
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import parse_value

if TYPE_CHECKING:
    from music_assistant_models.enums import ListingType

    from music_assistant import MusicAssistant
    from music_assistant.helpers.json import SerializableType

# the fills in flight, keyed on the entry they fill, so concurrent misses assemble it once
_fills: dict[str, asyncio.Future[tuple[Listing[Any], bool]]] = {}
# how often the listings of a container were dropped: a fill only keeps what it assembled
# while that number stood still
_generations: dict[str, int] = {}


@dataclass(slots=True)
class Listing[ItemT: MediaItem]:
    """
    A listing as assembled from the library and the providers.

    :param items: The items, in the order the sources list them.
    :param complete: Whether every source answered; an incomplete listing is served but
        not kept, so the next request asks the sources again.
    """

    items: list[ItemT]
    complete: bool = True


async def cached_listing[ItemT: MediaItem](
    mass: MusicAssistant,
    listing: ListingType,
    uri: str,
    assemble: Callable[[], Awaitable[Listing[ItemT]]],
    *,
    item_type: Any,
    narrowed_to: Sequence[str] | None = None,
) -> list[ItemT]:
    """
    Return a container's listing, assembled once and served from the cache for a while.

    A cached listing carries no user's favorite state: the calling user's is stamped on
    it. A refresh request (``cache.handle_refresh``) assembles the listing anew and
    replaces the cached one; an assembly overtaken by an invalidation of the container is
    served but not kept.

    :param mass: The MusicAssistant instance.
    :param listing: The listing being served.
    :param uri: The uri of the container (the album, playlist, artist, ...) the listing
        belongs to.
    :param assemble: Assembles the whole listing from the library and the providers.
    :param item_type: The type of the items (a class or a union of classes), to rebuild
        them from the cache.
    :param narrowed_to: The provider instances the calling user's music sources narrow
        the assembly to, when they do.
    """
    key = listing.value
    if narrowed_to is not None:
        key += f".{','.join(sorted(narrowed_to))}"
    entry = f"{uri}|{key}"
    while (items := await _read(mass, uri, key, item_type)) is None:
        if (fill := _fills.get(entry)) is None:
            return cast("list[ItemT]", await _assemble(mass, entry, uri, key, assemble))
        # a concurrent request is assembling the listing: take what it keeps, with the
        # calling user's favorites, or share its items when it keeps nothing
        try:
            assembled, kept = await asyncio.shield(fill)
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) and task.cancelling():
                raise
            # the assembling request went away, so the next round assembles it here
            continue
        if not kept:
            return cast("list[ItemT]", assembled.items)
    return cast("list[ItemT]", items)


async def invalidate_listings(mass: MusicAssistant, item: MediaItem) -> None:
    """
    Drop the cached listings of a container whose contents changed, under every id it has.

    :param mass: The MusicAssistant instance.
    :param item: The container (an album, a playlist, ...).
    """
    uris = {
        create_uri(item.media_type, mapping.provider_instance, mapping.item_id)
        for mapping in item.provider_mappings
    }
    if item.uri:
        uris.add(item.uri)
    for uri in uris:
        _generations[uri] = _generations.get(uri, 0) + 1
        await mass.cache.delete(None, category=CACHE_CATEGORY_LISTINGS, provider=uri)


async def _read(mass: MusicAssistant, uri: str, key: str, item_type: Any) -> list[Any] | None:
    """Return a cached listing carrying the calling user's favorites, None when there is none."""
    cached = await mass.cache.get(
        key, provider=uri, category=CACHE_CATEGORY_LISTINGS, allow_bypass=True
    )
    if cached is None:
        return None
    # rebuilt off the event loop, a listing can hold thousands of items
    items = await asyncio.to_thread(_rebuild, cached, item_type)
    return await with_user_favorites(mass, get_current_user(), items)


async def _assemble(
    mass: MusicAssistant,
    entry: str,
    uri: str,
    key: str,
    assemble: Callable[[], Awaitable[Listing[Any]]],
) -> list[Any]:
    """Assemble a listing in the calling task and keep it, for the requests waiting on it too."""
    fill = _fills[entry] = asyncio.get_running_loop().create_future()
    try:
        assembled, kept = await _fill(mass, uri, key, assemble)
    except asyncio.CancelledError:
        fill.cancel()
        raise
    except Exception as err:
        fill.set_exception(err)
        raise
    else:
        fill.set_result((assembled, kept))
        return assembled.items
    finally:
        _fills.pop(entry, None)


async def _fill(
    mass: MusicAssistant,
    uri: str,
    key: str,
    assemble: Callable[[], Awaitable[Listing[Any]]],
) -> tuple[Listing[Any], bool]:
    """Assemble a listing and keep it, returning the listing and whether it was kept."""
    generation = _generations.get(uri, 0)
    assembled = await assemble()
    if not assembled.complete or _generations.get(uri, 0) != generation:
        return assembled, False
    # the items are serialized off the event loop, as the cache does for provider results
    await mass.cache.set(
        key,
        cast("SerializableType", assembled.items),
        expiration=LISTING_CACHE_EXPIRATION,
        provider=uri,
        category=CACHE_CATEGORY_LISTINGS,
    )
    return assembled, True


def _rebuild(raw_items: list[dict[str, Any]], item_type: Any) -> list[Any]:
    """Rebuild the items of a cached listing from their stored form."""
    return [parse_value("item", raw, item_type) for raw in raw_items]
