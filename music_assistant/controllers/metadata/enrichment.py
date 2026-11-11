"""
Rich metadata enrichment for the Metadata Controller.

Provides the MetadataEnrichmentMixin, mixed into the MetaDataController, with the
per-mediatype updaters that merge metadata from the music and metadata providers
into library items (artists, albums, tracks, playlists, audiobooks and podcasts).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from operator import itemgetter
from time import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.enums import (
    AlbumType,
    ExternalID,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.controllers.music.helpers import (
    discogs_external_id,
    fill_track_from_recording,
    merge_metadata,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.external_ids import is_valid_barcode, is_valid_isrc
from music_assistant.helpers.provider_access import visible_music_sources
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.musicbrainz.provider import MusicbrainzProvider, relation_urls

from .constants import (
    CONF_ENABLE_ONLINE_METADATA,
    CONF_PREFER_LOCAL_GENRES,
    REFRESH_INTERVAL,
    REFRESH_RETRY_INTERVAL,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterable, Sequence

    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.media_items import Audiobook, MediaItemType, Playlist, Podcast

    from music_assistant import MusicAssistant
    from music_assistant.models.media_capabilities import MusicDiscoveryMixin
    from music_assistant.models.metadata_provider import MetadataProvider
    from music_assistant.providers.musicbrainz.models import MusicBrainzRelease

# how many reference albums and tracks an artist is identified on MusicBrainz through
MAX_MUSICBRAINZ_REF_ITEMS = 3
# how many top track titles an artist is searched on MusicBrainz by, at most
MAX_TOP_TRACK_TITLES = 25
_MUSICBRAINZ_ID_TYPES = {
    ExternalID.MB_ALBUM,
    ExternalID.MB_RELEASEGROUP,
    ExternalID.MB_RECORDING,
}
# the errors a metadata provider is expected to recover from, which make the item's
# refresh due again after REFRESH_RETRY_INTERVAL
_TEMPORARY_ERRORS = (
    aiohttp.ClientError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
    TimeoutError,
)
# the sources of a refresh, in the order their values win on an update: the local metadata
# provider, the music folder, the music services, then the online metadata providers
_LOCAL_METADATA, _LOCAL_MUSIC, _MUSIC_SERVICE, _ONLINE_METADATA = range(4)
# the single-value metadata fields an update writes over the stored ones (besides the description)
_ARTIST_FIELDS = ("style", "mood", "life_span", "artist_entity_type", "label")
_ALBUM_FIELDS = ("review", "label", "release_date", "copyright", "explicit", "style", "mood")
_TRACK_FIELDS = ("copyright", "explicit", "grouping", "release_date", "style", "mood")


class MetadataEnrichmentMixin:
    """
    Rich metadata enrichment functionality for the MetaDataController.

    Expects to be mixed with a class providing ``mass``, ``logger``, ``config``
    and the ``providers``, ``preferred_language``, ``language_changed`` and
    ``link_providers_via_musicbrainz`` properties.
    """

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        config: CoreConfig

        @property
        def preferred_language(self) -> str: ...  # noqa: D102

        @property
        def language_changed(self) -> int: ...  # noqa: D102

        @property
        def providers(self) -> list[MetadataProvider]: ...  # noqa: D102

        @property
        def link_providers_via_musicbrainz(self) -> bool: ...  # noqa: D102

    async def _update_artist_metadata(  # noqa: PLR0915
        self, artist: Artist, force_refresh: bool = False
    ) -> None:
        """Get/update rich metadata for an artist."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        language_outdated = self._description_language_outdated(artist.metadata)
        needs_refresh = (
            time() - (artist.metadata.last_refresh or 0)
        ) > REFRESH_INTERVAL or language_outdated
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Artist %s", artist.name)
        unique_keys: set[str] = set()

        # The bio is re-derived from the providers on every refresh. Each source's
        # metadata is collected with its rank and its description excluded from the
        # field merge; _apply_fresh_values picks the bio and the values that win below.
        sources: list[tuple[int, MediaItemMetadata]] = []
        source_missing = False

        # collect (local) metadata from all local providers
        local_provs = get_global_cache_value("non_streaming_providers")
        if TYPE_CHECKING:
            local_provs = cast("set[str]", local_provs)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        for prov_mapping in sorted(
            artist.provider_mappings, key=lambda x: x.priority, reverse=True
        ):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                # a music source that is set up but unreachable has no say this time
                source_missing |= self._music_source_unreachable(prov_mapping.provider_instance)
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.artists.get_provider_item(
                    prov_mapping.item_id,
                    prov_mapping.provider_instance,
                    force_refresh=force_refresh,
                )
                # a provider miss hands back the stored library item, which is not fresh
                if prov_item.provider != "library":
                    sources.append((_music_source_rank(prov), prov_item.metadata))
                merge_metadata(artist.metadata, _without_description(prov_item.metadata))

        # The musicbrainz ID is mandatory for all metadata lookups
        merge_mbid: str | None = None
        reliable = bool(artist.mbid)
        retry_soon = False
        if not artist.mbid:
            try:
                found = await self._get_artist_mbid(artist)
            except _TEMPORARY_ERRORS as err:
                found = None
                retry_soon = True
                self.logger.debug(
                    "Artist %s could not be identified on MusicBrainz right now: %s",
                    artist.name,
                    err,
                )
            if found:
                artist.mbid, by_name = found
                merge_mbid = None if by_name else artist.mbid
                reliable = not by_name
        # a failed identification leaves the MusicBrainz lookup marker unset for a later retry
        if not retry_soon:
            own_mbid = artist.mbid
            retry_soon = await self._link_artist_to_musicbrainz(artist, follow_merged=reliable)
            if own_mbid and artist.mbid != own_mbid:
                # the id an artist merged on MusicBrainz led to is as reliable as the old one
                merge_mbid = artist.mbid

        # don't merge online genres on top of source-supplied ones; propagation-derived
        # genres also count as a local source so they survive metadata refreshes
        prefer_local_genres = self.config.get_value(CONF_PREFER_LOCAL_GENRES) and (
            bool(artist.metadata.genres)
            or await self.mass.music.genres.has_derived_genre_mappings(
                MediaType.ARTIST, artist.item_id
            )
        )

        # collect metadata from all [metadata] providers, the online ones only when enabled
        # TODO: Utilize a global (cloud) cache for metadata lookups to save on API calls
        online_enabled = bool(self.config.get_value(CONF_ENABLE_ONLINE_METADATA) and artist.mbid)
        # a local provider's images replace the stored ones instead of filling gaps
        local_images: dict[str, list[MediaItemImage]] = {}
        for provider in self.providers:
            if ProviderFeature.ARTIST_METADATA not in provider.supported_features:
                continue
            if not (online_enabled or provider.is_local):
                continue
            try:
                # a forced refresh asks the provider again, rather than reading a cached result
                async with self.mass.cache.handle_refresh(force_refresh):
                    metadata = await provider.get_artist_metadata(artist)
            except _TEMPORARY_ERRORS as err:
                retry_soon = True
                self.logger.debug(
                    "Metadata for Artist %s is not available from provider %s right now: %s",
                    artist.name,
                    provider.name,
                    err,
                )
                continue
            except Exception as err:
                self.logger.warning(
                    "Error fetching metadata for Artist %s from provider %s: %s",
                    artist.name,
                    provider.name,
                    err,
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
                continue
            if provider.is_local:
                _drop_provider_images(artist.metadata, provider.instance_id)
                local_images[provider.instance_id] = list(metadata.images or []) if metadata else []
            if metadata:
                if (
                    provider.is_local
                    and metadata.genres
                    and self.config.get_value(CONF_PREFER_LOCAL_GENRES)
                ):
                    prefer_local_genres = True
                if prefer_local_genres and not provider.is_local:
                    metadata = replace(metadata, genres=None)
                sources.append((_metadata_source_rank(provider), metadata))
                merge_metadata(artist.metadata, _without_description(metadata))
                self.logger.debug(
                    "Fetched metadata for Artist %s on provider %s",
                    artist.name,
                    provider.name,
                )
        replacements = self._apply_fresh_values(
            artist.metadata,
            sources,
            _ARTIST_FIELDS,
            self._replaces_stored_values(artist, force_refresh, retry_soon or source_missing),
            language_outdated,
        )
        if replacements or local_images:
            await self.mass.music.artists.replace_metadata(
                artist.item_id, replacements, local_images
            )

        # update final item in library database
        # set timestamp, used to determine when this function was last called
        artist.metadata.last_refresh = _refresh_timestamp(
            retry_soon, self.language_changed if language_outdated else 0
        )
        await self.mass.music.artists.update_item_in_library(artist.item_id, artist)
        # an id a name search found may belong to a namesake, so only a reliable one merges
        if merge_mbid:
            await self.mass.music.artists.merge_library_artists_by_mbid(
                int(artist.item_id), merge_mbid
            )

    def _select_description(
        self,
        candidates: Sequence[tuple[str | None, str]],
        prev_description: str | None,
        prev_description_language: str | None,
    ) -> tuple[str | None, str | None]:
        """
        Return the chosen ``(description, language)`` for the item this refresh.

        :param candidates: ``(language, text)`` tuples in source order (local sources first,
            then music services, then the online metadata providers).
        :param prev_description: Description stored before this refresh.
        :param prev_description_language: Language of the description stored before this refresh.
        """
        pref = self.preferred_language
        # 1. first candidate in the user's preferred language
        for lang, text in candidates:
            if lang == pref:
                return text, lang
        # 2. keep a stored preferred-language bio rather than downgrade
        if prev_description is not None and prev_description_language == pref:
            return prev_description, prev_description_language
        # 3. English fallback, same priority order
        for lang, text in candidates:
            if lang == "en":
                return text, lang
        # 4. last resort: highest-priority bio in any (incl. unknown) language
        if candidates:
            lang, text = candidates[0]
            return text, lang
        return prev_description, prev_description_language

    def _apply_fresh_values(
        self,
        metadata: MediaItemMetadata,
        sources: list[tuple[int, MediaItemMetadata]],
        field_names: Sequence[str],
        replace_stored: bool,
        language_outdated: bool = False,
    ) -> dict[str, Any]:
        """
        Set the description and the fresh values of a refresh on an item's metadata.

        Of each field, the first source in rank order with a value wins.

        :param metadata: The item's stored metadata, with the sources' values other than the
            description merged in.
        :param sources: ``(rank, metadata)`` of each source of the refresh.
        :param field_names: The single-value fields the refresh may write over the stored ones.
        :param replace_stored: Whether the values of every source replace the stored ones,
            else only those of a local metadata provider.
        :param language_outdated: Whether the stored description is in a language the user
            changed away from, so one in the preferred language may replace it.
        :return: The values to write over the stored ones.
        """
        sources = sorted(sources, key=itemgetter(0))
        # a local text without a language counts as written in the user's language
        candidates = [
            (
                rank,
                source.description_language
                or (self.preferred_language if rank < _MUSIC_SERVICE else None),
                source.description,
            )
            for rank, source in sources
            if source.description
        ]
        prev_description = (metadata.description, metadata.description_language)
        description, language = self._select_description(
            [(lang, text) for _, lang, text in candidates], *prev_description
        )
        winner = next(
            (rank for rank, lang, text in candidates if (lang, text) == (language, description)),
            None,
        )
        replacements: dict[str, Any] = {}
        # a run that only fills gaps keeps a stored description, unless a local metadata
        # provider supplies one or the stored one is in a language the user changed away from
        if winner is not None and (
            replace_stored
            or winner == _LOCAL_METADATA
            or metadata.description is None
            or (language_outdated and language == self.preferred_language)
        ):
            if (description, language) != prev_description:
                # a library update keeps a stored description, so a new one is written explicitly
                replacements |= {"description": description, "description_language": language}
            metadata.description, metadata.description_language = description, language
        fresh = MediaItemMetadata()
        for rank, source in sources:
            if replace_stored or rank == _LOCAL_METADATA:
                # an empty text is no value, so a later source can still supply one
                values = {name: getattr(source, name) for name in field_names}
                merge_metadata(
                    fresh,
                    MediaItemMetadata(**{k: v for k, v in values.items() if v != ""}),
                )
        # stored dates no source supplies complete a fresh life span
        merge_metadata(fresh, MediaItemMetadata(life_span=metadata.life_span))
        for name in field_names:
            value = getattr(fresh, name)
            if value is not None and value != getattr(metadata, name):
                setattr(metadata, name, value)
                replacements[name] = value
        return replacements

    def _replaces_stored_values(
        self, item: Artist | Album | Track, force_refresh: bool, source_failed: bool
    ) -> bool:
        """
        Return whether a refresh may write its values over the stored ones of an item.

        :param item: The item being refreshed.
        :param force_refresh: Whether the refresh is forced.
        :param source_failed: Whether a source could not be reached during the refresh.
        """
        if force_refresh and source_failed:
            self.logger.info(
                "Update metadata of %s only filled in missing information, as a source "
                "could not be reached; try again later",
                item.name,
            )
        return force_refresh and not source_failed

    def _description_language_outdated(self, metadata: MediaItemMetadata) -> bool:
        """Return whether an item's description is in a language the user changed away from."""
        return bool(
            metadata.description
            and metadata.description_language
            and metadata.description_language != self.preferred_language
            and (metadata.last_refresh or 0) < self.language_changed
        )

    def _music_source_unreachable(self, provider_instance: str) -> bool:
        """
        Return whether a music source that is not loaded is set up and switched on.

        :param provider_instance: The instance id of the music source.
        """
        return bool(
            self.mass.config.get_raw_provider_config_value(provider_instance, "enabled", False)
        )

    async def _update_album_metadata(  # noqa: PLR0915
        self, album: Album, force_refresh: bool = False
    ) -> None:
        """Get/update rich metadata for an album."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        language_outdated = self._description_language_outdated(album.metadata)
        needs_refresh = (
            time() - (album.metadata.last_refresh or 0)
        ) > REFRESH_INTERVAL or language_outdated
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Album %s", album.name)
        sources: list[tuple[int, MediaItemMetadata]] = []
        source_missing = False

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        for prov_mapping in sorted(album.provider_mappings, key=lambda x: x.priority, reverse=True):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                # a music source that is set up but unreachable has no say this time
                source_missing |= self._music_source_unreachable(prov_mapping.provider_instance)
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.albums.get_provider_item(
                    prov_mapping.item_id,
                    prov_mapping.provider_instance,
                    force_refresh=force_refresh,
                )
                # a provider miss hands back the stored library item, which is not fresh
                if prov_item.provider != "library":
                    sources.append((_music_source_rank(prov), prov_item.metadata))
                album.metadata.update(_without_description(prov_item.metadata))
                # backfill identifiers (e.g. barcode) missing on a sparse library add
                album.external_ids.update(prov_item.external_ids)
                if album.year is None and prov_item.year:
                    album.year = prov_item.year
                if album.album_type == AlbumType.UNKNOWN:
                    album.album_type = prov_item.album_type

        # identify the album on MusicBrainz before the metadata providers need its id
        retry_soon = await self._link_album_to_musicbrainz(album)

        # don't merge online genres on top of source-supplied ones; propagation-derived
        # genres also count as a local source so they survive metadata refreshes
        prefer_local_genres = self.config.get_value(CONF_PREFER_LOCAL_GENRES) and (
            bool(album.metadata.genres)
            or await self.mass.music.genres.has_derived_genre_mappings(
                MediaType.ALBUM, album.item_id
            )
        )

        # collect metadata from all (online) [metadata] providers
        # TODO: Utilize a global (cloud) cache for metadata lookups to save on API calls
        if self.config.get_value(CONF_ENABLE_ONLINE_METADATA):
            for provider in self.providers:
                if ProviderFeature.ALBUM_METADATA not in provider.supported_features:
                    continue
                try:
                    # a forced refresh asks the provider again, rather than reading a cached result
                    async with self.mass.cache.handle_refresh(force_refresh):
                        metadata = await provider.get_album_metadata(album)
                except _TEMPORARY_ERRORS as err:
                    retry_soon = True
                    self.logger.debug(
                        "Metadata for Album %s is not available from provider %s right now: %s",
                        album.name,
                        provider.name,
                        err,
                    )
                    continue
                except Exception as err:
                    self.logger.warning(
                        "Error fetching metadata for Album %s from provider %s: %s",
                        album.name,
                        provider.name,
                        err,
                        exc_info=err if self.logger.isEnabledFor(10) else None,
                    )
                    continue
                if metadata:
                    if prefer_local_genres:
                        metadata = replace(metadata, genres=None)
                    sources.append((_metadata_source_rank(provider), metadata))
                    album.metadata.update(_without_description(metadata))
                    self.logger.debug(
                        "Fetched metadata for Album %s on provider %s",
                        album.name,
                        provider.name,
                    )
        if replacements := self._apply_fresh_values(
            album.metadata,
            sources,
            _ALBUM_FIELDS,
            self._replaces_stored_values(album, force_refresh, retry_soon or source_missing),
            language_outdated,
        ):
            await self.mass.music.albums.replace_metadata(album.item_id, replacements)
        # update final item in library database
        # set timestamp, used to determine when this function was last called
        album.metadata.last_refresh = _refresh_timestamp(
            retry_soon, self.language_changed if language_outdated else 0
        )
        await self.mass.music.albums.update_item_in_library(album.item_id, album)

    async def _update_track_metadata(  # noqa: PLR0915
        self, track: Track, force_refresh: bool = False
    ) -> None:
        """Get/update rich metadata for a track."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        language_outdated = self._description_language_outdated(track.metadata)
        needs_refresh = (
            time() - (track.metadata.last_refresh or 0)
        ) > REFRESH_INTERVAL or language_outdated
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Track %s", track.name)
        sources: list[tuple[int, MediaItemMetadata]] = []
        source_missing = False

        prev_lyrics = (track.metadata.lyrics, track.metadata.lrc_lyrics)
        if force_refresh:
            # drop the stored lyrics so they are looked up again
            track.metadata.lyrics = None
            track.metadata.lrc_lyrics = None

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        for prov_mapping in sorted(track.provider_mappings, key=lambda x: x.priority, reverse=True):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                # a music source that is set up but unreachable has no say this time
                source_missing |= self._music_source_unreachable(prov_mapping.provider_instance)
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.tracks.get_provider_item(
                    prov_mapping.item_id,
                    prov_mapping.provider_instance,
                    force_refresh=force_refresh,
                )
                # a provider miss hands back the stored library item, which is not fresh
                if prov_item.provider != "library":
                    sources.append((_music_source_rank(prov), prov_item.metadata))
                track.metadata.update(_without_description(prov_item.metadata))
                # a provider's full track carries the ISRCs its album listing may leave out
                for id_type, value in prov_item.external_ids:
                    if id_type == ExternalID.ISRC and is_valid_isrc(value):
                        track.add_external_id(ExternalID.ISRC, value)

        # identify the track on MusicBrainz before the metadata providers need its id
        retry_soon = await self._link_track_to_musicbrainz(track)

        # don't merge online genres on top of source-supplied ones
        prefer_local_genres = self.config.get_value(CONF_PREFER_LOCAL_GENRES) and bool(
            track.metadata.genres
        )

        # collect metadata from all [metadata] providers
        # Only fetch metadata from these sources if force_refresh is set OR
        # if the track needs a refresh (based on REFRESH_INTERVAL) AND
        # online metadata is enabled.
        if (force_refresh or needs_refresh) and self.config.get_value(CONF_ENABLE_ONLINE_METADATA):
            for provider in self.providers:
                if ProviderFeature.TRACK_METADATA not in provider.supported_features:
                    continue

                try:
                    # a forced refresh asks the provider again, rather than reading a cached result
                    async with self.mass.cache.handle_refresh(force_refresh):
                        metadata = await provider.get_track_metadata(track)
                except _TEMPORARY_ERRORS as err:
                    retry_soon = True
                    self.logger.debug(
                        "Metadata for Track %s is not available from provider %s right now: %s",
                        track.name,
                        provider.name,
                        err,
                    )
                    continue
                except Exception as err:
                    self.logger.warning(
                        "Error fetching metadata for Track %s from provider %s: %s",
                        track.name,
                        provider.name,
                        err,
                        exc_info=err if self.logger.isEnabledFor(10) else None,
                    )
                    continue
                if metadata:
                    if prefer_local_genres:
                        metadata = replace(metadata, genres=None)
                    sources.append((_metadata_source_rank(provider), metadata))
                    track.metadata.update(_without_description(metadata))
                    self.logger.debug(
                        "Fetched metadata for Track %s on provider %s",
                        track.name,
                        provider.name,
                    )
        if force_refresh and not (track.metadata.lyrics or track.metadata.lrc_lyrics):
            # finding no lyrics says nothing about the stored ones, so they are kept
            track.metadata.lyrics, track.metadata.lrc_lyrics = prev_lyrics
        elif force_refresh:
            # the library keeps stored lyrics on an update, so the refreshed ones are written over
            await self.mass.music.tracks.replace_metadata(
                track.item_id,
                {"lyrics": track.metadata.lyrics, "lrc_lyrics": track.metadata.lrc_lyrics},
            )
        if replacements := self._apply_fresh_values(
            track.metadata,
            sources,
            _TRACK_FIELDS,
            self._replaces_stored_values(track, force_refresh, retry_soon or source_missing),
            language_outdated,
        ):
            await self.mass.music.tracks.replace_metadata(track.item_id, replacements)
        # set timestamp, used to determine when this function was last called
        track.metadata.last_refresh = _refresh_timestamp(
            retry_soon, self.language_changed if language_outdated else 0
        )
        # update final item in library database
        await self.mass.music.tracks.update_item_in_library(track.item_id, track)

    async def _update_playlist_metadata(
        self, playlist: Playlist, force_refresh: bool = False
    ) -> None:
        """Get/update rich metadata for a playlist."""
        # collect metadata from the playlist tracks and the metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (playlist.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return
        self.logger.debug("Updating metadata for Playlist %s", playlist.name)
        playlist.metadata.genres = set()
        playlist_genres: dict[str, int] = {}
        # retrieve metadata for the playlist from the tracks (such as genres etc.)
        # TODO: retrieve style/mood ?
        async for track in self.mass.music.playlists.iter_tracks(
            playlist.item_id, playlist.provider
        ):
            if track.metadata.genres:
                genres = track.metadata.genres
            elif (
                isinstance(track, Track)
                and track.album
                and isinstance(track.album, Album)
                and track.album.metadata.genres
            ):
                genres = track.album.metadata.genres
            else:
                genres = set()
            for genre in genres:
                if genre not in playlist_genres:
                    playlist_genres[genre] = 0
                playlist_genres[genre] += 1
            await asyncio.sleep(0)  # yield to eventloop

        playlist_genres_filtered = {genre for genre, count in playlist_genres.items() if count > 5}
        playlist_genres_filtered = set(list(playlist_genres_filtered)[:8])
        playlist.metadata.genres.update(playlist_genres_filtered)

        # Collect metadata from metadata providers (e.g. playlist_metadata)
        for provider in self.providers:
            if ProviderFeature.PLAYLIST_METADATA not in provider.supported_features:
                continue
            try:
                if prov_metadata := await provider.get_playlist_metadata(playlist):
                    playlist.metadata.update(prov_metadata)
                    self.logger.debug(
                        "Retrieved playlist metadata from provider %s for %s",
                        provider.name,
                        playlist.name,
                    )
            except MusicAssistantError as err:
                self.logger.warning(
                    "Error retrieving playlist metadata from provider %s for %s: %s",
                    provider.name,
                    playlist.name,
                    err,
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
        # set timestamp, used to determine when this function was last called
        playlist.metadata.last_refresh = int(time())
        # update final item in library database
        await self.mass.music.playlists.update_item_in_library(
            playlist.item_id, playlist, overwrite=True
        )

    async def _update_audiobook_metadata(
        self, audiobook: Audiobook, force_refresh: bool = False
    ) -> None:
        """Get/update rich metadata for an audiobook."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (audiobook.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Audiobook %s", audiobook.name)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        prov_images: UniqueList[MediaItemImage] | None = None
        for prov_mapping in sorted(
            audiobook.provider_mappings, key=lambda x: x.priority, reverse=True
        ):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.audiobooks.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                if prov_images is None and prov_item.metadata.images:
                    prov_images = prov_item.metadata.images
                audiobook.metadata.update(prov_item.metadata)
                if audiobook.publisher is None and prov_item.publisher:
                    audiobook.publisher = prov_item.publisher
                if not audiobook.authors and prov_item.authors:
                    audiobook.authors = prov_item.authors
                if not audiobook.narrators and prov_item.narrators:
                    audiobook.narrators = prov_item.narrators
                if not audiobook.duration and prov_item.duration:
                    audiobook.duration = prov_item.duration

        # no way to select a cover for audiobooks, so replace rather than merge the
        # images to keep it in sync with the provider; revisit if a picker is added
        if prov_images is not None:
            audiobook.metadata.images = prov_images

        # update final item in library database
        # set timestamp, used to determine when this function was last called
        audiobook.metadata.last_refresh = int(time())
        await self.mass.music.audiobooks.update_item_in_library(audiobook.item_id, audiobook)

    async def _update_podcast_metadata(self, podcast: Podcast, force_refresh: bool = False) -> None:
        """Get/update rich metadata for a podcast."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (podcast.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Podcast %s", podcast.name)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        prov_images: UniqueList[MediaItemImage] | None = None
        for prov_mapping in sorted(
            podcast.provider_mappings, key=lambda x: x.priority, reverse=True
        ):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.podcasts.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                if prov_images is None and prov_item.metadata.images:
                    prov_images = prov_item.metadata.images
                podcast.metadata.update(prov_item.metadata)
                if podcast.publisher is None and prov_item.publisher:
                    podcast.publisher = prov_item.publisher
                if not podcast.total_episodes and prov_item.total_episodes:
                    podcast.total_episodes = prov_item.total_episodes

        # no way to select a cover for podcasts, so replace rather than merge the
        # images to keep it in sync with the provider; revisit if a picker is added
        if prov_images is not None:
            podcast.metadata.images = prov_images

        # update final item in library database
        # set timestamp, used to determine when this function was last called
        podcast.metadata.last_refresh = int(time())
        await self.mass.music.podcasts.update_item_in_library(podcast.item_id, podcast)

    async def _link_artist_to_musicbrainz(self, artist: Artist, follow_merged: bool = True) -> bool:
        """
        Fill in an artist's Discogs id and link it to the music providers MusicBrainz knows.

        :param artist: The library artist to link.
        :param follow_merged: Give an artist MusicBrainz merged into another the other's id; not
            for an id only a name search found, which may belong to a namesake.
        :return: Whether the lookup failed temporarily, which makes the item due for a refresh
            again soon.
        """
        if not (musicbrainz := self._musicbrainz_provider()):
            return False
        # the Various Artists entity is nobody's discography, so it is not linked anywhere
        if artist.mbid and artist.mbid != VARIOUS_ARTISTS_MBID:
            try:
                details = await musicbrainz.get_artist_details(artist.mbid)
                if follow_merged:
                    # MusicBrainz answers a merged artist's old id with the artist it was merged into
                    await self._follow_merged_id(artist, details.id)
                urls = relation_urls(details.relations)
                if not artist.get_external_id(ExternalID.DISCOGS) and (
                    discogs := discogs_external_id(urls, MediaType.ARTIST)
                ):
                    artist.add_external_id(*discogs)
                if self.link_providers_via_musicbrainz:
                    await self.mass.music.artists.link_musicbrainz_mappings(artist, urls)
            except _TEMPORARY_ERRORS as err:
                self.logger.debug(
                    "Artist %s could not be looked up on MusicBrainz right now: %s",
                    artist.name,
                    err,
                )
                return True
            except MediaNotFoundError:
                # an item removed from the library meanwhile ends the whole update
                raise
            except Exception as err:
                self.logger.warning(
                    "Error linking Artist %s through MusicBrainz: %s",
                    artist.name,
                    err,
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
                return False
        artist.metadata.last_musicbrainz_lookup = int(time())
        return False

    async def _link_album_to_musicbrainz(self, album: Album) -> bool:
        """
        Identify an album on MusicBrainz, fill in what it knows and link it and its tracks.

        :param album: The library album to link.
        :return: Whether the lookup failed temporarily, which makes the item due for a refresh
            again soon.
        """
        if not (musicbrainz := self._musicbrainz_provider()):
            return False
        try:
            db_tracks = await self.mass.music.albums.get_library_album_tracks(album.item_id)
            release = await musicbrainz.resolve_release(
                album, library_track_count=len(db_tracks) or None
            )
            if release is None:
                self.logger.debug("Album %s was not found on MusicBrainz", album.name)
            else:
                if album.mbid and album.mbid.casefold() != release.id.casefold():
                    await self._follow_merged_release(musicbrainz, album, release.id)
                urls = relation_urls(release.relations)
                _fill_album_from_release(album, release, urls)
                link_providers = self.link_providers_via_musicbrainz
                if link_providers:
                    await self.mass.music.albums.link_musicbrainz_mappings(album, urls)
                await self.mass.music.albums.link_album_tracks(
                    album, db_tracks, release, link_providers=link_providers
                )
        except _TEMPORARY_ERRORS as err:
            self.logger.debug(
                "Album %s could not be looked up on MusicBrainz right now: %s", album.name, err
            )
            return True
        except Exception as err:
            # an album removed from the library meanwhile ends the whole update, a track of
            # it merged away meanwhile does not
            if isinstance(err, MediaNotFoundError) and not await self._in_library(album):
                raise
            self.logger.warning(
                "Error linking Album %s through MusicBrainz: %s",
                album.name,
                err,
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )
            return False
        album.metadata.last_musicbrainz_lookup = int(time())
        return False

    async def _link_track_to_musicbrainz(self, track: Track) -> bool:
        """
        Identify a track on MusicBrainz, fill in its recording id and ISRCs and link it.

        :param track: The library track to link.
        :return: Whether the lookup failed temporarily, which makes the item due for a refresh
            again soon.
        """
        if not (musicbrainz := self._musicbrainz_provider()):
            return False
        try:
            recording = await musicbrainz.resolve_recording(track)
            if recording is None:
                self.logger.debug("Track %s was not found on MusicBrainz", track.name)
            else:
                fill_track_from_recording(track, recording)
                if self.link_providers_via_musicbrainz:
                    await self.mass.music.tracks.link_musicbrainz_mappings(
                        track, relation_urls(recording.relations)
                    )
        except _TEMPORARY_ERRORS as err:
            self.logger.debug(
                "Track %s could not be looked up on MusicBrainz right now: %s", track.name, err
            )
            return True
        except MediaNotFoundError:
            # an item removed from the library meanwhile ends the whole update
            raise
        except Exception as err:
            self.logger.warning(
                "Error linking Track %s through MusicBrainz: %s",
                track.name,
                err,
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )
            return False
        track.metadata.last_musicbrainz_lookup = int(time())
        return False

    async def _follow_merged_release(
        self, musicbrainz: MusicbrainzProvider, album: Album, release_id: str
    ) -> None:
        """
        Give an album the release its stored release was merged into on MusicBrainz.

        A stored release MusicBrainz does not answer for is kept, as the MusicBrainz mirror may
        not have it yet.

        :param musicbrainz: The MusicBrainz provider.
        :param album: The library album, stored with the new id.
        :param release_id: The id of the release the album was identified as.
        """
        try:
            # the identify step looked the stored release up first, so this reads its answer again
            stored = await musicbrainz.get_release_details(album.mbid or "")
        except InvalidDataError:
            return
        await self._follow_merged_id(album, stored.id)

    async def _follow_merged_id(self, item: Album | Artist, mbid: str) -> None:
        """
        Give a library item the MusicBrainz id its own MusicBrainz id now leads to.

        MusicBrainz answers the id of an item merged into another with that other item.

        :param item: The library item, stored with the new id.
        :param mbid: The id of the item MusicBrainz answered with.
        """
        if not item.mbid or item.mbid.casefold() == mbid.casefold():
            return
        self.logger.debug(
            '%s "%s" was merged on MusicBrainz, %s now leads to %s',
            item.media_type.value,
            item.name,
            item.mbid,
            mbid,
        )
        await self._store_musicbrainz_id(item, mbid)

    async def _store_musicbrainz_id(self, item: Album | Artist, mbid: str) -> None:
        """Store a library item with a MusicBrainz id in place of the one it has."""
        previous = set(item.external_ids)
        item.mbid = mbid
        # a library update would add the new id next to the old one, so the ids stored now are
        # written with only the MusicBrainz id swapped, keeping any added meanwhile
        controller = self.mass.music.get_controller(item.media_type)
        stored = (await controller.get_library_item(item.item_id)).external_ids
        await controller.set_external_ids(
            item.item_id,
            (set(stored) - (previous - item.external_ids)) | (item.external_ids - previous),
        )

    async def _in_library(self, item: MediaItemType) -> bool:
        """Return whether the given library item still exists in the library."""
        try:
            await self.mass.music.get_controller(item.media_type).get_library_item(item.item_id)
        except MediaNotFoundError:
            return False
        return True

    def _musicbrainz_provider(self) -> MusicbrainzProvider | None:
        """Return the MusicBrainz provider, if it is loaded."""
        return cast("MusicbrainzProvider | None", self.mass.get_provider("musicbrainz"))

    async def _get_artist_mbid(self, artist: Artist) -> tuple[str, bool] | None:
        """Fetch the artist's MusicBrainz id and whether only its name identified it."""
        if artist.mbid:
            return artist.mbid, False
        if compare_strings(artist.name, VARIOUS_ARTISTS_NAME):
            return VARIOUS_ARTISTS_MBID, True

        if not (musicbrainz := self._musicbrainz_provider()):
            return None
        # the artist's own streaming service links cost neither a database read nor a
        # provider request, so they are tried on their own first
        if identified := await musicbrainz.identify_artist(artist, [], []):
            return identified.artist.id, identified.by_name
        # the library's own albums and tracks carry the MusicBrainz ids, barcodes and ISRCs
        # the lookup keys on, so those come before anything a provider has to be asked for
        ref_albums = _identifying_first(
            await self.mass.music.artists.albums(artist.item_id, artist.provider, limit=None)
        )
        library_tracks = await self.mass.music.artists.tracks(
            artist.item_id, artist.provider, limit=None
        )
        ref_tracks = _identifying_first(library_tracks)
        if identified := await musicbrainz.identify_artist(artist, ref_albums, ref_tracks):
            return identified.artist.id, identified.by_name
        # last resort for library items without identifiers: the artist's top tracks
        tried = {track.uri for track in ref_tracks}
        # only a music provider ranks its own catalog without a search per track, so the
        # listing is narrowed to them and never falls back to a metadata provider
        if ranking := self._ranking_music_providers(artist):
            top_tracks = _identifying_first(
                [
                    track
                    for track in (
                        await self.mass.music.artists.get_library_artist_toptracks(
                            artist.item_id, provider_filter=ranking
                        )
                    ).items
                    if track.uri not in tried
                ]
            )
            if top_tracks and (
                identified := await musicbrainz.identify_artist(artist, ref_albums, top_tracks)
            ):
                return identified.artist.id, identified.by_name
            tried.update(track.uri for track in top_tracks)
        if titles := await self._top_track_titles(artist):
            popular = _identifying_first(_tracks_named(library_tracks, titles, tried))
            if popular and (
                identified := await musicbrainz.identify_artist(artist, ref_albums, popular)
            ):
                return identified.artist.id, identified.by_name
            # a title search matches the artist by name only
            if mb_artist := await musicbrainz.get_artist_by_track_titles(artist.name, titles):
                return mb_artist.id, True

        # lookup failed
        ref_albums_str = "/".join(x.name for x in ref_albums) or "none"
        ref_tracks_str = "/".join(x.name for x in ref_tracks) or "none"
        self.logger.debug(
            "Unable to get musicbrainz ID for artist %s (albums: %s, tracks: %s)",
            artist.name,
            ref_albums_str,
            ref_tracks_str,
        )
        return None

    def _ranking_music_providers(self, artist: Artist) -> list[str]:
        """Return the visible instances of the artist's music providers that rank its top tracks."""
        # a refresh started by a user may only ask the sources that user can see
        user = get_current_user()
        visible = visible_music_sources(self.mass, user) if user else None
        ranking: list[str] = []
        for mapping in artist.provider_mappings:
            if visible is not None and mapping.provider_instance not in visible:
                continue
            provider = self.mass.get_provider(
                mapping.provider_instance, provider_type=MusicProvider
            )
            if (
                provider
                and ProviderFeature.ARTIST_TOPTRACKS in provider.supported_features
                and mapping.provider_instance not in ranking
            ):
                ranking.append(mapping.provider_instance)
        return ranking

    async def _top_track_titles(self, artist: Artist) -> list[str]:
        """Return the artist's top track titles from the first metadata or plugin provider with any."""
        for provider in self.mass.get_providers_supporting_feature(
            ProviderFeature.ARTIST_TOPTRACKS, priority=(ProviderType.METADATA, ProviderType.PLUGIN)
        ):
            try:
                titles = await cast("MusicDiscoveryMixin", provider).get_artist_toptrack_titles(
                    artist, MAX_TOP_TRACK_TITLES
                )
            except MusicAssistantError as err:
                self.logger.debug(
                    "Unable to get top track titles for artist %s from %s: %s",
                    artist.name,
                    provider.name,
                    err,
                )
                continue
            if titles:
                return titles
        return []


def _fill_album_from_release(
    album: Album, release: MusicBrainzRelease, urls: Iterable[str]
) -> None:
    """
    Fill an album's identifiers, year and type in from its MusicBrainz release.

    Values the album already carries are kept.

    :param album: The library album to fill in.
    :param release: The MusicBrainz release the album is.
    :param urls: The release's URLs on MusicBrainz (its URL relations).
    """
    if not album.mbid:
        album.mbid = release.id
    release_group = release.release_group
    if release_group and not album.get_external_id(ExternalID.MB_RELEASEGROUP):
        album.add_external_id(ExternalID.MB_RELEASEGROUP, release_group.id)
    if (
        release.barcode
        and is_valid_barcode(release.barcode)
        and not album.get_external_id(ExternalID.BARCODE)
    ):
        album.add_external_id(ExternalID.BARCODE, release.barcode)
    if release.asin and not album.get_external_id(ExternalID.ASIN):
        album.add_external_id(ExternalID.ASIN, release.asin)
    if not album.get_external_id(ExternalID.DISCOGS) and (
        discogs := discogs_external_id(urls, MediaType.ALBUM)
    ):
        album.add_external_id(*discogs)
    if album.year is None:
        # the release group dates the album, a reissue's own date only stands in for it
        date = (release_group.first_release_date if release_group else None) or release.date or ""
        if date[:4].isdigit():
            album.year = int(date[:4])
    if album.album_type == AlbumType.UNKNOWN and release_group:
        album.album_type = MusicbrainzProvider.album_type_from_release_group(release_group)


def _identifying_first[ItemT: Album | Track](items: Sequence[ItemT]) -> list[ItemT]:
    """
    Return the few reference items that identify an artist best on MusicBrainz.

    :param items: Albums or tracks of the artist.
    """

    # a MusicBrainz id resolves in one request, a barcode or ISRC in two; anything else
    # needs a name search
    def _rank(item: ItemT) -> int:
        id_types = {id_type for id_type, _ in item.external_ids}
        if id_types & _MUSICBRAINZ_ID_TYPES:
            return 0
        if id_types & {ExternalID.BARCODE, ExternalID.ISRC}:
            return 1
        return 2

    return sorted(items, key=_rank)[:MAX_MUSICBRAINZ_REF_ITEMS]


def _tracks_named(
    tracks: Sequence[Track], titles: Sequence[str], skip_uris: set[str | None]
) -> list[Track]:
    """
    Return the tracks whose name matches one of the titles, in the order of the titles.

    :param tracks: Tracks of the artist.
    :param titles: Track titles to look for.
    :param skip_uris: Uris of tracks to leave out.
    """
    named: list[Track] = []
    seen = set(skip_uris)
    for title in titles:
        for track in tracks:
            if track.uri in seen:
                continue
            if compare_strings(track.name, title, True) or compare_strings(
                track.name, title, False
            ):
                named.append(track)
                seen.add(track.uri)
    return named


def _music_source_rank(provider: MusicProvider) -> int:
    """Return the source rank of a music provider."""
    return _MUSIC_SERVICE if provider.is_streaming_provider else _LOCAL_MUSIC


def _metadata_source_rank(provider: MetadataProvider) -> int:
    """Return the source rank of a metadata provider."""
    return _LOCAL_METADATA if provider.is_local else _ONLINE_METADATA


def _without_description(metadata: MediaItemMetadata) -> MediaItemMetadata:
    """Return a copy of metadata without its description, which a refresh selects apart."""
    return replace(metadata, description=None, description_language=None)


def _drop_provider_images(metadata: MediaItemMetadata, provider_instance_id: str) -> None:
    """Remove the images of a provider from metadata (in-place)."""
    if metadata.images:
        metadata.images = (
            UniqueList(img for img in metadata.images if img.provider != provider_instance_id)
            or None
        )


def _refresh_timestamp(retry_soon: bool, not_before: int = 0) -> int:
    """
    Return the last_refresh stamp of a refresh that finishes now.

    :param retry_soon: Whether a metadata provider failed temporarily, which makes the
        item due again after REFRESH_RETRY_INTERVAL rather than REFRESH_INTERVAL.
    :param not_before: The earliest stamp to return, so a refresh after a language change
        counts as done for that change even when it is due again soon.
    """
    now = int(time())
    return max(now - REFRESH_INTERVAL + REFRESH_RETRY_INTERVAL if retry_soon else now, not_before)
