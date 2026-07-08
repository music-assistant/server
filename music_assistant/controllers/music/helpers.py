"""Helper functions for the music controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.media_items import Artist, ItemMapping, MediaItemType, SearchResults
from music_assistant_models.unique_list import UniqueList

from music_assistant.helpers.compare import create_safe_string

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.enums import MediaType


def sort_search_result[SortItemT: MediaItemType | ItemMapping](
    search_query: str,
    items: Sequence[SortItemT],
) -> UniqueList[SortItemT]:
    """Sort search results on priority/preference."""
    scored_items: list[tuple[int, SortItemT]] = []
    # search results are already sorted by (streaming) providers on relevance
    # but we prefer exact name matches and library items so we simply put those
    # on top of the list.
    safe_title_str = create_safe_string(search_query)
    if " - " in search_query:
        artist_name, title_alt = search_query.split(" - ", 1)
        safe_title_alt = create_safe_string(title_alt)
        safe_artist_str = create_safe_string(artist_name)
    else:
        safe_artist_str = None
        safe_title_alt = None
    for item in items:
        score = 0
        if create_safe_string(item.name) not in (safe_title_str, safe_title_alt):
            # literal name match is mandatory to get a score at all
            continue
        # bonus point if artist provided and exact match
        if safe_artist_str:
            artist: Artist | ItemMapping
            for artist in getattr(item, "artists", []):
                if create_safe_string(artist.name) == safe_artist_str:
                    score += 1
        # bonus point for library items
        if item.provider == "library":
            score += 1
        scored_items.append((score, item))
    scored_items.sort(key=lambda x: x[0], reverse=True)
    # combine it all with uniquelist, so this will deduplicated by default
    # note that streaming provider results are already (most likely) sorted on relevance
    # so we add all remaining items in their original order. We just prioritize
    # exact name matches and library items.
    return UniqueList([*[x[1] for x in scored_items], *items])


def filter_search_results(
    results: SearchResults,
    provider_domain: str,
    skip_item_ids: set[tuple[MediaType, str, str]] | None,
) -> SearchResults:
    """
    Return a copy of the given search results without the items in skip_item_ids.

    :param results: The search results to filter.
    :param provider_domain: Domain of the provider the results originate from.
    :param skip_item_ids: Set of (media_type, provider_domain, item_id) tuples to filter out.
    """
    if not skip_item_ids:
        return results

    def _keep(item: MediaItemType | ItemMapping) -> bool:
        return (item.media_type, provider_domain, item.item_id) not in skip_item_ids

    # build a new SearchResults object as the original may be a (shared) cached object
    return SearchResults(
        artists=[x for x in results.artists if _keep(x)],
        albums=[x for x in results.albums if _keep(x)],
        genres=results.genres,
        tracks=[x for x in results.tracks if _keep(x)],
        playlists=[x for x in results.playlists if _keep(x)],
        radio=[x for x in results.radio if _keep(x)],
        audiobooks=[x for x in results.audiobooks if _keep(x)],
        podcasts=[x for x in results.podcasts if _keep(x)],
        sound_effects=results.sound_effects,
    )
