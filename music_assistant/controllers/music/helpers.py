"""Helper functions for the music controller."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.enums import ExternalID, ImageType
from music_assistant_models.errors import InvalidProviderID, InvalidProviderURI
from music_assistant_models.helpers import create_safe_string, get_global_cache_value
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    MediaItemMetadata,
    MediaItemType,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.helpers.external_ids import is_valid_isrc
from music_assistant.helpers.uri import canonical_provider_url, discogs_id_from_url, parse_uri

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from collections.abc import Set as AbstractSet

    from music_assistant_models.enums import MediaType

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.musicbrainz.models import MusicBrainzRecording

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


def update_moves_single_source_item(
    stored: Iterable[ProviderMapping], update: Iterable[ProviderMapping]
) -> bool:
    """
    Return True when an update gives a library item a new id on the only provider it is from.

    This is what a renamed or moved file looks like: the stored data came from that one
    provider alone, so the update may replace what the provider reports (such as the
    artists) instead of being merged with it.

    :param stored: Provider mappings currently stored for the library item.
    :param update: Provider mappings of the item as delivered by the provider.
    """
    stored = list(stored)
    update = list(update)
    if not stored or not update:
        return False
    if len({mapping.provider_instance for mapping in (*stored, *update)}) != 1:
        return False
    stored_ids = {mapping.item_id for mapping in stored}
    return any(mapping.item_id not in stored_ids for mapping in update)


def preferred_thumb(
    images: Iterable[dict[str, Any]] | None, hidden_sources: AbstractSet[str]
) -> dict[str, Any] | None:
    """
    Return the thumb to show of a library item's stored (raw) images.

    A library item can carry the artwork of several music sources, not all of which the
    viewer can be shown. The first thumb that can be shown is preferred, falling back to
    the first thumb.

    :param images: The stored (raw) images of the item.
    :param hidden_sources: Music sources hidden from the viewer.
    """
    thumbs = [image for image in images or () if image["type"] == ImageType.THUMB.value]
    # same semantics as the MediaItem.available property: an empty cache means unknown
    available_providers: AbstractSet[str] = get_global_cache_value("available_providers") or set()
    return next(
        (
            image
            for image in thumbs
            if image.get("remotely_accessible")
            or (
                image["provider"] not in hidden_sources
                and (not available_providers or image["provider"] in available_providers)
            )
        ),
        thumbs[0] if thumbs else None,
    )


def sibling_instance_mappings(
    mass: MusicAssistant, mappings: Iterable[ProviderMapping], mapped: Iterable[ProviderMapping]
) -> list[ProviderMapping]:
    """
    Return copies of the mappings for the other instances of the same streaming provider.

    :param mappings: Provider mappings to copy.
    :param mapped: Provider mappings the item already has; their instances get no copy.
    """
    mappings = list(mappings)
    mapped_instances = {x.provider_instance for x in (*mapped, *mappings)}
    copies: list[ProviderMapping] = []
    for mapping in mappings:
        if mapping.is_unique:
            continue
        # unavailable instances count too: a mapping they hold must not be taken over
        # once they are back
        for instance in mass.music.get_provider_instances(
            mapping.provider_domain, return_unavailable=True
        ):
            if instance.instance_id in mapped_instances or not instance.is_streaming_provider:
                continue
            # whether the other instance holds the item in its library is unknown
            copies.append(replace(mapping, provider_instance=instance.instance_id, in_library=None))
            mapped_instances.add(instance.instance_id)
    return copies


async def provider_mappings_from_urls(
    mass: MusicAssistant,
    urls: Iterable[str],
    media_type: MediaType,
    exclude_domains: AbstractSet[str],
) -> list[ProviderMapping]:
    """
    Return provider mappings for the streaming service links of a media item.

    A link only becomes a mapping when it names an item on a loaded music provider
    unambiguously: a provider linked to several different items is left out, as is one
    that is not loaded.

    :param mass: MusicAssistant instance.
    :param urls: Public URLs of the item on streaming services (e.g. MusicBrainz URL relations).
    :param media_type: Media type the URLs must point to.
    :param exclude_domains: Provider domains to leave out, e.g. those the item is already
        mapped to.
    """
    # the ids a provider is linked with, each with the URL it came from
    ids_by_domain: dict[str, dict[str, str]] = {}
    for url in urls:
        if not url.startswith(("http://", "https://")):
            continue
        try:
            url_media_type, domain, item_id = await parse_uri(url, validate_id=True)
        except InvalidProviderURI, InvalidProviderID:
            continue
        if domain == "builtin" or url_media_type != media_type or domain in exclude_domains:
            continue
        ids_by_domain.setdefault(domain, {}).setdefault(item_id, url)
    mappings: list[ProviderMapping] = []
    for domain, item_ids in sorted(ids_by_domain.items()):
        if len(item_ids) != 1:
            continue
        instances = mass.music.get_provider_instances(domain, return_unavailable=True)
        if not instances:
            continue
        # an available instance is preferred, an unavailable one still maps the item
        available = [provider for provider in instances if provider.available]
        ((item_id, url),) = item_ids.items()
        mappings.append(
            ProviderMapping(
                item_id=item_id,
                provider_domain=domain,
                provider_instance=min(provider.instance_id for provider in available or instances),
                available=True,
                in_library=False,
                url=canonical_provider_url(domain, media_type, item_id) or url,
            )
        )
    return mappings


def discogs_external_id(
    urls: Iterable[str], media_type: MediaType
) -> tuple[ExternalID, str] | None:
    """
    Return the Discogs external id among the links of a media item, if any.

    :param urls: Public URLs of the item (e.g. MusicBrainz URL relations).
    :param media_type: Media type of the item: ARTIST or ALBUM.
    """
    discogs_ids = {
        discogs_id for url in urls if (discogs_id := discogs_id_from_url(url, media_type))
    }
    # links to several Discogs entries identify none of them
    if len(discogs_ids) != 1:
        return None
    return (ExternalID.DISCOGS, discogs_ids.pop())


def fill_track_from_recording(track: Track, recording: MusicBrainzRecording) -> bool:
    """
    Fill a track's MusicBrainz recording id and ISRCs in from its recording.

    A recording id the track already carries is kept.

    :param track: The track to fill in.
    :param recording: The MusicBrainz recording the track is.
    :return: Whether the track gained its recording id or an ISRC.
    """
    known_ids = set(track.external_ids)
    if not track.mbid:
        track.mbid = recording.id
    for isrc in recording.isrcs or ():
        if is_valid_isrc(isrc):
            track.add_external_id(ExternalID.ISRC, isrc)
    return track.external_ids != known_ids
