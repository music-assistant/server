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
from time import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import AlbumType, ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import Album, Artist, MediaItemImage, Track

from music_assistant.constants import VARIOUS_ARTISTS_MBID, VARIOUS_ARTISTS_NAME
from music_assistant.controllers.music.helpers import (
    discogs_external_id,
    fill_track_from_recording,
)
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.external_ids import is_valid_barcode
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.musicbrainz.provider import MusicbrainzProvider, relation_urls

from .constants import CONF_ENABLE_ONLINE_METADATA, CONF_PREFER_LOCAL_GENRES, REFRESH_INTERVAL

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterable, Sequence

    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.media_items import Audiobook, Playlist, Podcast
    from music_assistant_models.unique_list import UniqueList

    from music_assistant import MusicAssistant
    from music_assistant.models.metadata_provider import MetadataProvider
    from music_assistant.providers.musicbrainz.models import MusicBrainzRelease

# how many reference albums and tracks an artist is identified on MusicBrainz through
MAX_MUSICBRAINZ_REF_ITEMS = 3
_MUSICBRAINZ_ID_TYPES = {
    ExternalID.MB_ALBUM,
    ExternalID.MB_RELEASEGROUP,
    ExternalID.MB_RECORDING,
}


class MetadataEnrichmentMixin:
    """
    Rich metadata enrichment functionality for the MetaDataController.

    Expects to be mixed with a class providing ``mass``, ``logger``, ``config``
    and the ``providers``, ``preferred_language`` and
    ``link_providers_via_musicbrainz`` properties.
    """

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        config: CoreConfig

        @property
        def preferred_language(self) -> str: ...  # noqa: D102

        @property
        def providers(self) -> list[MetadataProvider]: ...  # noqa: D102

        @property
        def link_providers_via_musicbrainz(self) -> bool: ...  # noqa: D102

    async def _update_artist_metadata(self, artist: Artist, force_refresh: bool = False) -> None:
        """Get/update rich metadata for an artist."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (artist.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Artist %s", artist.name)
        unique_keys: set[str] = set()

        # The bio is re-derived from the providers on every refresh. Each provider's
        # description is collected as a (language, text) candidate and excluded from the
        # field merge; _select_description picks the winner below. Candidates are appended
        # in priority order: music providers first, then metadata providers (TADB, Wikipedia).
        prev_description = artist.metadata.description
        prev_description_language = artist.metadata.description_language
        description_candidates: list[tuple[str | None, str]] = []

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
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.artists.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                if prov_item.metadata.description:
                    description_candidates.append(
                        (prov_item.metadata.description_language, prov_item.metadata.description)
                    )
                artist.metadata.update(
                    replace(prov_item.metadata, description=None, description_language=None)
                )

        # The musicbrainz ID is mandatory for all metadata lookups
        if not artist.mbid:
            if mbid := await self._get_artist_mbid(artist):
                artist.mbid = mbid
        await self._link_artist_to_musicbrainz(artist)

        # don't merge online genres on top of source-supplied ones; propagation-derived
        # genres also count as a local source so they survive metadata refreshes
        prefer_local_genres = self.config.get_value(CONF_PREFER_LOCAL_GENRES) and (
            bool(artist.metadata.genres)
            or await self.mass.music.genres.has_derived_genre_mappings(
                MediaType.ARTIST, artist.item_id
            )
        )

        # collect metadata from all (online)[metadata] providers
        # TODO: Utilize a global (cloud) cache for metadata lookups to save on API calls
        if self.config.get_value(CONF_ENABLE_ONLINE_METADATA) and artist.mbid:
            for provider in self.providers:
                if ProviderFeature.ARTIST_METADATA not in provider.supported_features:
                    continue
                try:
                    metadata = await provider.get_artist_metadata(artist)
                except Exception as err:
                    self.logger.warning(
                        "Error fetching metadata for Artist %s from provider %s: %s",
                        artist.name,
                        provider.name,
                        err,
                        exc_info=err if self.logger.isEnabledFor(10) else None,
                    )
                    continue
                if metadata:
                    if prefer_local_genres:
                        metadata = replace(metadata, genres=None)
                    if metadata.description:
                        description_candidates.append(
                            (metadata.description_language, metadata.description)
                        )
                        metadata = replace(metadata, description=None, description_language=None)
                    artist.metadata.update(metadata)
                    self.logger.debug(
                        "Fetched metadata for Artist %s on provider %s",
                        artist.name,
                        provider.name,
                    )
        artist.metadata.description, artist.metadata.description_language = (
            self._select_description(
                description_candidates, prev_description, prev_description_language
            )
        )

        # update final item in library database
        # set timestamp, used to determine when this function was last called
        artist.metadata.last_refresh = int(time())
        await self.mass.music.artists.update_item_in_library(artist.item_id, artist)

    def _select_description(
        self,
        candidates: Sequence[tuple[str | None, str]],
        prev_description: str | None,
        prev_description_language: str | None,
    ) -> tuple[str | None, str | None]:
        """
        Return the chosen ``(description, language)`` for the artist this refresh.

        :param candidates: ``(language, text)`` tuples in provider-priority order
            (music providers first, then TADB, then Wikipedia).
        :param prev_description: Bio stored before this refresh.
        :param prev_description_language: Language of the bio stored before this refresh.
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

    async def _update_album_metadata(self, album: Album, force_refresh: bool = False) -> None:
        """Get/update rich metadata for an album."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (album.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Album %s", album.name)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        for prov_mapping in sorted(album.provider_mappings, key=lambda x: x.priority, reverse=True):
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
                prov_item = await self.mass.music.albums.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                album.metadata.update(prov_item.metadata)
                # backfill identifiers (e.g. barcode) missing on a sparse library add
                album.external_ids.update(prov_item.external_ids)
                if album.year is None and prov_item.year:
                    album.year = prov_item.year
                if album.album_type == AlbumType.UNKNOWN:
                    album.album_type = prov_item.album_type

        # identify the album on MusicBrainz before the metadata providers need its id
        await self._link_album_to_musicbrainz(album)

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
                    metadata = await provider.get_album_metadata(album)
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
                    album.metadata.update(metadata)
                    self.logger.debug(
                        "Fetched metadata for Album %s on provider %s",
                        album.name,
                        provider.name,
                    )
        # update final item in library database
        # set timestamp, used to determine when this function was last called
        album.metadata.last_refresh = int(time())
        await self.mass.music.albums.update_item_in_library(album.item_id, album)

    async def _update_track_metadata(self, track: Track, force_refresh: bool = False) -> None:
        """Get/update rich metadata for a track."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (track.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Track %s", track.name)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        for prov_mapping in sorted(track.provider_mappings, key=lambda x: x.priority, reverse=True):
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
                prov_item = await self.mass.music.tracks.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                track.metadata.update(prov_item.metadata)

        # identify the track on MusicBrainz before the metadata providers need its id
        await self._link_track_to_musicbrainz(track)

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
                    metadata = await provider.get_track_metadata(track)
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
                    track.metadata.update(metadata)
                    self.logger.debug(
                        "Fetched metadata for Track %s on provider %s",
                        track.name,
                        provider.name,
                    )
        # set timestamp, used to determine when this function was last called
        track.metadata.last_refresh = int(time())
        # update final item in library database
        await self.mass.music.tracks.update_item_in_library(track.item_id, track)

    async def _update_playlist_metadata(
        self, playlist: Playlist, force_refresh: bool = False
    ) -> None:
        """Get/update rich metadata for a playlist."""
        # collect metadata + create collage images
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (playlist.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return
        self.logger.debug("Updating metadata for Playlist %s", playlist.name)
        playlist.metadata.genres = set()
        playlist_genres: dict[str, int] = {}
        # retrieve metadata for the playlist from the tracks (such as genres etc.)
        # TODO: retrieve style/mood ?
        async for track in self.mass.music.playlists.tracks(playlist.item_id, playlist.provider):
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

    async def _link_artist_to_musicbrainz(self, artist: Artist) -> None:
        """Fill in an artist's Discogs id and link it to the music providers MusicBrainz knows."""
        if not (musicbrainz := self._musicbrainz_provider()):
            return
        # the Various Artists entity is nobody's discography, so it is not linked anywhere
        if artist.mbid and artist.mbid != VARIOUS_ARTISTS_MBID:
            try:
                details = await musicbrainz.get_artist_details(artist.mbid)
                urls = relation_urls(details.relations)
                if not artist.get_external_id(ExternalID.DISCOGS) and (
                    discogs := discogs_external_id(urls, MediaType.ARTIST)
                ):
                    artist.add_external_id(*discogs)
                if self.link_providers_via_musicbrainz:
                    await self.mass.music.artists.link_musicbrainz_mappings(artist, urls)
            except Exception as err:
                self.logger.warning(
                    "Error linking Artist %s through MusicBrainz: %s",
                    artist.name,
                    err,
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
                return
        artist.metadata.last_musicbrainz_lookup = int(time())

    async def _link_album_to_musicbrainz(self, album: Album) -> None:
        """Identify an album on MusicBrainz, fill in what it knows and link it and its tracks."""
        if not (musicbrainz := self._musicbrainz_provider()):
            return
        try:
            db_tracks = await self.mass.music.albums.get_library_album_tracks(album.item_id)
            release = await musicbrainz.resolve_release(
                album, library_track_count=len(db_tracks) or None
            )
            if release is None:
                self.logger.debug("Album %s was not found on MusicBrainz", album.name)
            else:
                urls = relation_urls(release.relations)
                _fill_album_from_release(album, release, urls)
                link_providers = self.link_providers_via_musicbrainz
                if link_providers:
                    await self.mass.music.albums.link_musicbrainz_mappings(album, urls)
                await self.mass.music.albums.link_album_tracks(
                    album, db_tracks, release, link_providers=link_providers
                )
        except Exception as err:
            self.logger.warning(
                "Error linking Album %s through MusicBrainz: %s",
                album.name,
                err,
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )
            return
        album.metadata.last_musicbrainz_lookup = int(time())

    async def _link_track_to_musicbrainz(self, track: Track) -> None:
        """Identify a track on MusicBrainz, fill in its recording id and ISRCs and link it."""
        if not (musicbrainz := self._musicbrainz_provider()):
            return
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
        except Exception as err:
            self.logger.warning(
                "Error linking Track %s through MusicBrainz: %s",
                track.name,
                err,
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )
            return
        track.metadata.last_musicbrainz_lookup = int(time())

    def _musicbrainz_provider(self) -> MusicbrainzProvider | None:
        """Return the MusicBrainz provider, if it is loaded."""
        return cast("MusicbrainzProvider | None", self.mass.get_provider("musicbrainz"))

    async def _get_artist_mbid(self, artist: Artist) -> str | None:
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        if artist.mbid:
            return artist.mbid
        if compare_strings(artist.name, VARIOUS_ARTISTS_NAME):
            return VARIOUS_ARTISTS_MBID

        if not (musicbrainz := self._musicbrainz_provider()):
            return None
        # the artist's own streaming service links cost neither a database read nor a
        # provider request, so they are tried on their own first
        if mb_artist := await musicbrainz.resolve_artist(artist, [], []):
            return mb_artist.id
        # the library's own albums and tracks carry the MusicBrainz ids, barcodes and ISRCs
        # the lookup keys on, so those come before anything a provider has to be asked for
        ref_albums = _identifying_first(
            await self.mass.music.artists.albums(artist.item_id, artist.provider)
        )
        ref_tracks = _identifying_first(
            await self.mass.music.artists.tracks(artist.item_id, artist.provider)
        )
        if mb_artist := await musicbrainz.resolve_artist(artist, ref_albums, ref_tracks):
            return mb_artist.id
        # last resort for library items without identifiers: the providers' top tracks
        tried = {track.uri for track in ref_tracks}
        top_tracks = _identifying_first(
            [
                track
                for track in await self.mass.music.artists.top_tracks(
                    artist.item_id, artist.provider
                )
                if track.uri not in tried
            ]
        )
        if top_tracks and (
            mb_artist := await musicbrainz.resolve_artist(artist, ref_albums, top_tracks)
        ):
            return mb_artist.id

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
