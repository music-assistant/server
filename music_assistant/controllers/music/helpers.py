"""Helper functions for the music controller."""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    MediaItemMetadata,
    MediaItemType,
    ProviderMapping,
    SearchResults,
)
from music_assistant_models.unique_list import UniqueList

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from music_assistant_models.enums import MediaType

# the trigram tokenizer of the FTS5 search index cannot match
# search terms shorter than 3 characters
MIN_FTS_TERM_LENGTH: Final[int] = 3


def search_name_match_clause(
    db_table: str, search_term: str, param_name: str, query_params: dict[str, Any]
) -> str:
    """
    Return a SQL WHERE fragment matching ``search_term`` as substring of the search_name column.

    :param db_table: The media item table to match against.
    :param search_term: The (already normalized) search term to match.
    :param param_name: Name of the query parameter to bind the search term to.
    :param query_params: Query parameters dict the search term is bound into.
    """
    if len(search_term) < MIN_FTS_TERM_LENGTH:
        # terms too short for the trigram index fall back to a LIKE scan
        query_params[param_name] = f"%{search_term}%"
        return f"{db_table}.search_name LIKE :{param_name}"
    # quote the term so it is interpreted as a plain (sub)string instead of
    # FTS5 query syntax; normalized search terms are alphanumeric only so
    # they can never contain quotes themselves
    query_params[param_name] = f'"{search_term}"'
    return (
        f"{db_table}.item_id IN "
        f"(SELECT rowid FROM {db_table}_fts WHERE {db_table}_fts MATCH :{param_name})"
    )


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
        genres=[x for x in results.genres if _keep(x)],
        tracks=[x for x in results.tracks if _keep(x)],
        playlists=[x for x in results.playlists if _keep(x)],
        radio=[x for x in results.radio if _keep(x)],
        audiobooks=[x for x in results.audiobooks if _keep(x)],
        podcasts=[x for x in results.podcasts if _keep(x)],
        sound_effects=[x for x in results.sound_effects if _keep(x)],
    )


def metadata_for_update(
    stored: MediaItemMetadata, update: MediaItemMetadata, overwrite: bool
) -> MediaItemMetadata:
    """
    Return the metadata to store for a library item update.

    An overwrite replaces the stored metadata, unless the given item carries none at
    all: providers embed a bare stub of an album or artist in their track payloads.

    :param stored: Metadata currently stored for the library item.
    :param update: Metadata of the item as delivered by the provider.
    :param overwrite: Whether the given item replaces the stored one.
    """
    if overwrite and any(getattr(update, field.name) for field in fields(update)):
        return update
    return stored.update(update)


def provider_mappings_for_update(
    stored: Iterable[ProviderMapping], update: Iterable[ProviderMapping], overwrite: bool
) -> set[ProviderMapping]:
    """
    Return the provider mappings to store for a library item update.

    An overwrite replaces the mappings of the providers the given item comes from, so a
    changed item id (a moved file) drops its stale row, and keeps the mappings written
    by the other providers the item is linked to.

    :param stored: Provider mappings currently stored for the library item.
    :param update: Provider mappings of the item as delivered by the provider.
    :param overwrite: Whether the given item replaces the stored one.
    """
    if not overwrite:
        return {*update, *stored}
    updated_instances = {mapping.provider_instance for mapping in update}
    return {
        *update,
        *(mapping for mapping in stored if mapping.provider_instance not in updated_instances),
    }
