"""Recommendation logic for Last.fm."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    RecommendationFolder,
    Track,
    UniqueList,
)

from music_assistant.constants import CONF_USERNAME, VARIOUS_ARTISTS_NAME
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.datetime import utc, utc_timestamp
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.providers.lastfm_recommendations.constants import (
    CACHE_CATEGORY_RESOLVED_ITEMS,
    CACHE_CATEGORY_TOP_GENRES,
    CACHE_EXPIRATION_SECONDS,
    CONF_ENABLE_GENRE,
    CONF_ENABLE_GEO,
    CONF_ENABLE_GLOBAL_CHARTS,
    CONF_ENABLE_PERSONALIZED,
    CONF_GEO_COUNTRY,
    GENRE_ARTISTS_LIMIT,
    GENRE_ARTISTS_PERIOD,
    LIBRARY_MATCH_SCAN_LIMIT,
    RECENT_PLAYS_SCAN_LIMIT,
    RECENT_PLAYS_WINDOW_DAYS,
    RESOLUTION_BUFFER_LARGE,
    RESOLUTION_BUFFER_SMALL,
    SIMILAR_ITEMS_BUFFER,
    SIMILAR_ITEMS_PER_SEED,
    SIMILAR_TRACKS_BUFFER,
    TAGS_PER_ARTIST,
    TARGET_ITEM_COUNT,
    TOP_ARTISTS_LIMIT,
    TOP_GENRES_CACHE_EXPIRATION_SECONDS,
    TOP_GENRES_LIMIT,
    TOP_ITEMS_TO_TAKE,
    TOP_TRACKS_LIMIT,
)
from music_assistant.providers.lastfm_recommendations.parsers import (
    parse_album,
    parse_artist,
    parse_track,
)

if TYPE_CHECKING:
    import logging

    from music_assistant.providers.lastfm_recommendations import LastFMRecommendationsProvider

_MediaItemT = TypeVar("_MediaItemT", Artist, Album, Track)


class _SeedTrack(NamedTuple):
    """A recently played track used to seed similar-artist/track lookups."""

    artist: str
    name: str


class LastFMRecommendationManager:
    """Manages Last.fm recommendations."""

    def __init__(self, provider: LastFMRecommendationsProvider) -> None:
        """
        Initialize recommendation manager.

        :param provider: The Last.fm recommendations provider instance.
        """
        self.provider = provider
        self.api = provider.api
        self.mass = provider.mass

        # Resolved items keyed by MBID (preferred) or name to avoid re-resolving.
        self._resolved_cache: dict[str, Artist | Album | Track] = {}

    @property
    def logger(self) -> logging.Logger:
        """Return the provider's active logger."""
        return self.provider.logger

    async def clear_cache(self) -> None:
        """Clear in-memory and persistent recommendation caches."""
        self._resolved_cache.clear()

        await self.mass.cache.clear(
            category_filter=CACHE_CATEGORY_RESOLVED_ITEMS,
            provider_filter=self.provider.instance_id,
        )
        await self.mass.cache.clear(
            category_filter=CACHE_CATEGORY_TOP_GENRES,
            provider_filter=self.provider.instance_id,
        )

        self.provider._recommendation_folders.clear()

        self.logger.info("Cleared all recommendation caches (in-memory and persistent)")

    async def build_recommendation_folders(self) -> AsyncIterator[RecommendationFolder]:
        """Yield recommendation folders across all enabled categories."""
        async for folder in self._yield_and_count(
            self._get_personalized_recommendations(), "personalized"
        ):
            yield folder

        async for folder in self._yield_and_count(self._get_global_recommendations(), "global"):
            yield folder

        async for folder in self._yield_and_count(
            self._get_genre_based_recommendations(), "genre-based"
        ):
            yield folder

        async for folder in self._yield_and_count(
            self._get_geo_based_recommendations(), "geography-based"
        ):
            yield folder

    async def _yield_and_count(
        self, source: AsyncIterator[RecommendationFolder], category_label: str
    ) -> AsyncIterator[RecommendationFolder]:
        """Yield folders from a single category."""
        count = 0
        async for folder in source:
            count += 1
            yield folder
        if count:
            self.logger.debug("Added %d %s recommendation folder(s)", count, category_label)

    async def _is_in_library(self, item_data: dict[str, Any], media_type: MediaType) -> bool:
        """
        Return True if the Last.fm item already exists in the MA library.

        :param item_data: Raw Last.fm item data (artist, album, or track dict).
        :param media_type: Type of media item to check.
        """
        name = item_data.get("name", "")
        # MBID lookup is the most reliable; fall back to name search for items without MBID.
        mbid = item_data.get("mbid")
        if mbid:
            if media_type == MediaType.ARTIST:
                if await self.mass.music.artists.get_library_item_by_external_id(
                    mbid, ExternalID.MB_ARTIST
                ):
                    self.logger.debug("Filtered artist '%s' (MBID match: %s)", name, mbid)
                    return True
            elif media_type == MediaType.ALBUM:
                if await self.mass.music.albums.get_library_item_by_external_id(
                    mbid, ExternalID.MB_ALBUM
                ):
                    self.logger.debug("Filtered album '%s' (MBID match: %s)", name, mbid)
                    return True
            elif media_type == MediaType.TRACK:
                if await self.mass.music.tracks.get_library_item_by_external_id(
                    mbid, ExternalID.MB_RECORDING
                ):
                    self.logger.debug("Filtered track '%s' (MBID match: %s)", name, mbid)
                    return True

        # Library search is fuzzy and always returns a best-effort row, so verify the hit's
        # name actually matches before treating the item as owned. Lenient (strict=False) so
        # tagging variants still count, erring towards filtering over showing owned items.
        if media_type == MediaType.ARTIST:
            if name:
                artist_results = await self.mass.music.artists.library_items(
                    search=name, limit=LIBRARY_MATCH_SCAN_LIMIT, summary=False
                )
                artist_match = next(
                    (a for a in artist_results if compare_strings(name, a.name, strict=False)),
                    None,
                )
                if artist_match:
                    self.logger.debug(
                        "Filtered artist '%s' (name match: '%s')", name, artist_match.name
                    )
                    return True

        elif media_type == MediaType.ALBUM:
            if name:
                album_results = await self.mass.music.albums.library_items(
                    search=name, limit=LIBRARY_MATCH_SCAN_LIMIT, summary=False
                )
                album_match = next(
                    (a for a in album_results if compare_strings(name, a.name, strict=False)),
                    None,
                )
                if album_match:
                    self.logger.debug(
                        "Filtered album '%s' (name match: '%s')", name, album_match.name
                    )
                    return True

        elif media_type == MediaType.TRACK:
            artist_info = item_data.get("artist", {})
            artist_name = (
                artist_info if isinstance(artist_info, str) else artist_info.get("name", "")
            )
            if name and artist_name:
                # Last.fm track names carry scrobbled version suffixes ("- 2006 Remaster");
                # strip those so variants of an owned track still match
                clean_name, _ = parse_title_and_version(name, strip_for_search=True)
                # "Artist - Title" format so the tracks controller searches both fields
                search_query = f"{artist_name} - {clean_name}"
                track_results = await self.mass.music.tracks.library_items(
                    search=search_query, limit=LIBRARY_MATCH_SCAN_LIMIT, summary=False
                )
                # Match both title and artist; a title-only check would treat a same-named
                # track by a different artist as owned. Differing recording MBIDs identify
                # genuinely different tracks, so those never count as a match.
                track_match = next(
                    (
                        track
                        for track in track_results
                        if compare_strings(clean_name, track.name, strict=False)
                        and any(
                            compare_strings(artist_name, track_artist.name, strict=False)
                            for track_artist in track.artists
                        )
                        and not (mbid and track.mbid and track.mbid != mbid)
                    ),
                    None,
                )
                if track_match:
                    self.logger.debug(
                        "Filtered track '%s - %s' (name match: '%s')",
                        artist_name,
                        name,
                        track_match.name,
                    )
                    return True

        return False

    @staticmethod
    def _exclude_owned(items: list[_MediaItemT]) -> list[_MediaItemT]:
        """
        Drop items that resolved to the user's own library copy from a discovery row.

        :param items: Resolved discovery items to filter.
        """
        # Resolution returns the library copy when a recommendation matches on ISRC/MBID,
        # which the cheaper pre-filter can miss; exclude those so Discover rows stay discovery.
        return [item for item in items if item.provider != "library"]

    def _sample_items(
        self, items: list[dict[str, Any]], seed_suffix: str, target_count: int = TARGET_ITEM_COUNT
    ) -> list[dict[str, Any]]:
        """
        Sample items using a 'top N + random remainder' strategy with an hourly seed.

        :param items: List of items to sample from (already filtered).
        :param seed_suffix: Unique suffix for random seed (to vary between recommendation types).
        :param target_count: Target number of items to return.
        """
        if len(items) <= target_count:
            return items

        top_items = items[:TOP_ITEMS_TO_TAKE]

        remaining = items[TOP_ITEMS_TO_TAKE:]
        random_count = target_count - TOP_ITEMS_TO_TAKE

        # Hourly seed keeps the sampled remainder stable within the hour and rotates it each hour.
        now = utc()
        seed = f"{now.date().isoformat()}_{now.hour}_{seed_suffix}"
        rng = random.Random(seed)
        random_items = rng.sample(remaining, min(random_count, len(remaining)))

        return top_items + random_items

    async def get_or_resolve_artist(self, lastfm_artist: dict[str, Any]) -> Artist | None:
        """
        Return an Artist from cache (in-memory or persistent) or resolve and cache it.

        :param lastfm_artist: Raw Last.fm artist dict.
        """
        cache_key = lastfm_artist.get("mbid") or lastfm_artist.get("name", "")
        if not cache_key:
            return None

        if cache_key in self._resolved_cache:
            cached = self._resolved_cache[cache_key]
            if isinstance(cached, Artist):
                return cached

        persistent_cache_key = f"artist_{cache_key}"
        cached_artist = await self.mass.cache.get(
            key=persistent_cache_key,
            category=CACHE_CATEGORY_RESOLVED_ITEMS,
            provider=self.provider.instance_id,
            base_class=Artist,
        )
        if isinstance(cached_artist, Artist):
            self._resolved_cache[cache_key] = cached_artist
            return cached_artist

        artist = await parse_artist(lastfm_artist, self.mass, self.provider.instance_id)
        if artist:
            self._resolved_cache[cache_key] = artist
            await self.mass.cache.set(
                persistent_cache_key,
                artist.to_dict(),
                category=CACHE_CATEGORY_RESOLVED_ITEMS,
                provider=self.provider.instance_id,
                expiration=CACHE_EXPIRATION_SECONDS,
            )
        return artist

    async def get_or_resolve_track(self, lastfm_track: dict[str, Any]) -> Track | None:
        """
        Return a Track from cache (in-memory or persistent) or resolve and cache it.

        :param lastfm_track: Raw Last.fm track dict.
        """
        cache_key = lastfm_track.get("mbid")
        if not cache_key:
            artist_data = lastfm_track.get("artist", {})
            artist_name = (
                artist_data if isinstance(artist_data, str) else artist_data.get("name", "")
            )
            track_name = lastfm_track.get("name", "")
            cache_key = f"{artist_name}_{track_name}" if artist_name and track_name else ""

        if not cache_key:
            return None

        if cache_key in self._resolved_cache:
            cached = self._resolved_cache[cache_key]
            if isinstance(cached, Track):
                return cached

        persistent_cache_key = f"track_{cache_key}"
        cached_track = await self.mass.cache.get(
            key=persistent_cache_key,
            category=CACHE_CATEGORY_RESOLVED_ITEMS,
            provider=self.provider.instance_id,
            base_class=Track,
        )
        if isinstance(cached_track, Track):
            self._resolved_cache[cache_key] = cached_track
            return cached_track

        track = await parse_track(lastfm_track, self.mass, self.provider.instance_id)
        if track:
            self._resolved_cache[cache_key] = track
            await self.mass.cache.set(
                persistent_cache_key,
                track.to_dict(),
                category=CACHE_CATEGORY_RESOLVED_ITEMS,
                provider=self.provider.instance_id,
                expiration=CACHE_EXPIRATION_SECONDS,
            )
        return track

    async def _get_or_resolve_album(self, lastfm_album: dict[str, Any]) -> Album | None:
        """
        Return an Album from cache (in-memory or persistent) or resolve and cache it.

        :param lastfm_album: Raw Last.fm album dict.
        """
        cache_key = lastfm_album.get("mbid")
        if not cache_key:
            artist_data = lastfm_album.get("artist", {})
            artist_name = (
                artist_data if isinstance(artist_data, str) else artist_data.get("name", "")
            )
            album_name = lastfm_album.get("name", "")
            cache_key = f"{artist_name}_{album_name}" if artist_name and album_name else ""

        if not cache_key:
            return None

        if cache_key in self._resolved_cache:
            cached = self._resolved_cache[cache_key]
            if isinstance(cached, Album):
                return cached

        persistent_cache_key = f"album_{cache_key}"
        cached_album = await self.mass.cache.get(
            key=persistent_cache_key,
            category=CACHE_CATEGORY_RESOLVED_ITEMS,
            provider=self.provider.instance_id,
            base_class=Album,
        )
        if isinstance(cached_album, Album):
            self._resolved_cache[cache_key] = cached_album
            return cached_album

        album = await parse_album(lastfm_album, self.mass, self.provider.instance_id)
        if album:
            self._resolved_cache[cache_key] = album
            await self.mass.cache.set(
                persistent_cache_key,
                album.to_dict(),
                category=CACHE_CATEGORY_RESOLVED_ITEMS,
                provider=self.provider.instance_id,
                expiration=CACHE_EXPIRATION_SECONDS,
            )
        return album

    async def _get_personalized_recommendations(self) -> AsyncIterator[RecommendationFolder]:
        """Yield personalized recommendation folders based on the user's listening history."""
        if not self.provider.config.get_value(CONF_ENABLE_PERSONALIZED):
            return

        # Both seed rows derive from the same recent play events, so scan the playlog once.
        recent_plays = await self._get_recent_play_seeds()

        top_artists = self._rank_seed_artists(recent_plays)

        if top_artists:
            similar_artists = await self._get_similar_artists_from_seeds(top_artists)

            if similar_artists:
                yield RecommendationFolder(
                    item_id=f"{self.provider.instance_id}_similar_artists",
                    name="Discover Similar Artists",
                    translation_key="discover_similar_artists",
                    translation_params=[str(len(top_artists))],
                    provider=self.provider.instance_id,
                    items=UniqueList(similar_artists[:TARGET_ITEM_COUNT]),
                    subtitle=f"Based on your top {len(top_artists)} artists",
                    icon="mdi-account-music-outline",
                )

        top_tracks = self._rank_seed_tracks(recent_plays)[:TOP_TRACKS_LIMIT]

        if top_tracks:
            similar_tracks = await self._get_similar_tracks_from_seeds(top_tracks)

            if similar_tracks:
                yield RecommendationFolder(
                    item_id=f"{self.provider.instance_id}_similar_tracks",
                    name="Discover Similar Tracks",
                    translation_key="discover_similar_tracks",
                    translation_params=[str(len(top_tracks))],
                    provider=self.provider.instance_id,
                    items=UniqueList(similar_tracks[:TARGET_ITEM_COUNT]),
                    subtitle=f"Based on your top {len(top_tracks)} tracks",
                    icon="mdi-music-note-outline",
                )

    async def _get_global_recommendations(self) -> AsyncIterator[RecommendationFolder]:
        """Yield global chart recommendation folders (worldwide top artists and tracks)."""
        if not self.provider.config.get_value(CONF_ENABLE_GLOBAL_CHARTS):
            return

        # Over-fetch so deduplication and resolution failures still leave TARGET_ITEM_COUNT.
        top_artists_raw = await self.api.get_chart_top_artists(limit=RESOLUTION_BUFFER_SMALL)
        if top_artists_raw:
            resolved_artists = await asyncio.gather(
                *[self.get_or_resolve_artist(artist_data) for artist_data in top_artists_raw]
            )
            all_resolved = [a for a in resolved_artists if a is not None]
            top_artists = list(UniqueList(all_resolved))[:TARGET_ITEM_COUNT]

            if top_artists:
                yield RecommendationFolder(
                    item_id=f"{self.provider.instance_id}_chart_top_artists",
                    name="Global Top Artists",
                    translation_key="global_top_artists",
                    provider=self.provider.instance_id,
                    items=UniqueList(top_artists),
                    subtitle="Most popular artists worldwide",
                    icon="mdi-chart-line",
                )

        top_tracks_raw = await self.api.get_chart_top_tracks(limit=RESOLUTION_BUFFER_SMALL)
        if top_tracks_raw:
            resolved_tracks = await asyncio.gather(
                *[self.get_or_resolve_track(track_data) for track_data in top_tracks_raw]
            )
            all_resolved_tracks = [t for t in resolved_tracks if t is not None]
            top_tracks = list(UniqueList(all_resolved_tracks))[:TARGET_ITEM_COUNT]

            if top_tracks:
                yield RecommendationFolder(
                    item_id=f"{self.provider.instance_id}_chart_top_tracks",
                    name="Global Top Tracks",
                    translation_key="global_top_tracks",
                    provider=self.provider.instance_id,
                    items=UniqueList(top_tracks),
                    subtitle="Most popular tracks worldwide",
                    icon="mdi-chart-box",
                )

    async def _get_genre_based_recommendations(self) -> AsyncIterator[RecommendationFolder]:
        """
        Yield genre-based recommendation folders derived from the user's top genres.

        Requires a username to be configured.
        """
        if not self.provider.config.get_value(CONF_ENABLE_GENRE):
            return

        top_genres = await self._get_top_genres()
        if not top_genres:
            return

        # cycle through the user's top genres day by day so the genre rows vary
        day_index = utc().date().toordinal()
        tag_name = top_genres[day_index % len(top_genres)]

        # Over-fetch so there's enough left after library filtering and resolution failures.
        genre_artists_raw = await self.api.get_tag_top_artists(
            tag_name, limit=RESOLUTION_BUFFER_LARGE
        )
        if genre_artists_raw:
            # Drop items already in the library using a cheap DB lookup, before the
            # expensive provider resolution step.
            non_library_artists_raw = [
                artist_data
                for artist_data in genre_artists_raw
                if not await self._is_in_library(artist_data, MediaType.ARTIST)
            ]

            sampled_artists_raw = self._sample_items(
                non_library_artists_raw,
                seed_suffix="genre_artists",
                target_count=RESOLUTION_BUFFER_SMALL,
            )

            resolved_artists = await asyncio.gather(
                *[self.get_or_resolve_artist(artist_data) for artist_data in sampled_artists_raw]
            )
            all_resolved = self._exclude_owned([a for a in resolved_artists if a is not None])
            genre_artists = list(UniqueList(all_resolved))[:TARGET_ITEM_COUNT]

            if genre_artists:
                yield RecommendationFolder(
                    item_id=f"{self.provider.instance_id}_genre_artists",
                    name=f"Discover {tag_name.title()} Artists",
                    translation_key="genre_artists",
                    translation_params=[tag_name.title()],
                    provider=self.provider.instance_id,
                    items=UniqueList(genre_artists),
                    subtitle="Top artists in your top genres",
                    icon="mdi-account-music",
                )

        genre_albums_raw = await self.api.get_tag_top_albums(
            tag_name, limit=RESOLUTION_BUFFER_LARGE
        )
        if genre_albums_raw:
            non_library_albums_raw = [
                album_data
                for album_data in genre_albums_raw
                if not await self._is_in_library(album_data, MediaType.ALBUM)
            ]

            sampled_albums_raw = self._sample_items(
                non_library_albums_raw,
                seed_suffix="genre_albums",
                target_count=RESOLUTION_BUFFER_SMALL,
            )

            resolved_albums = await asyncio.gather(
                *[self._get_or_resolve_album(album_data) for album_data in sampled_albums_raw]
            )
            all_resolved_albums = self._exclude_owned(
                [album for album in resolved_albums if album is not None]
            )
            genre_albums = list(UniqueList(all_resolved_albums))[:TARGET_ITEM_COUNT]

            if genre_albums:
                yield RecommendationFolder(
                    item_id=f"{self.provider.instance_id}_genre_albums",
                    name=f"Discover {tag_name.title()} Albums",
                    translation_key="genre_albums",
                    translation_params=[tag_name.title()],
                    provider=self.provider.instance_id,
                    items=UniqueList(genre_albums),
                    subtitle="Top albums in your top genres",
                    icon="mdi-album",
                )

        genre_tracks_raw = await self.api.get_tag_top_tracks(
            tag_name, limit=RESOLUTION_BUFFER_LARGE
        )
        if genre_tracks_raw:
            non_library_tracks_raw = [
                track_data
                for track_data in genre_tracks_raw
                if not await self._is_in_library(track_data, MediaType.TRACK)
            ]

            sampled_tracks_raw = self._sample_items(
                non_library_tracks_raw,
                seed_suffix="genre_tracks",
                target_count=RESOLUTION_BUFFER_SMALL,
            )

            resolved_tracks = await asyncio.gather(
                *[self.get_or_resolve_track(track_data) for track_data in sampled_tracks_raw]
            )
            all_resolved_genre_tracks = self._exclude_owned(
                [track for track in resolved_tracks if track is not None]
            )
            genre_tracks = list(UniqueList(all_resolved_genre_tracks))[:TARGET_ITEM_COUNT]

            if genre_tracks:
                yield RecommendationFolder(
                    item_id=f"{self.provider.instance_id}_genre_tracks",
                    name=f"Discover {tag_name.title()} Tracks",
                    translation_key="genre_tracks",
                    translation_params=[tag_name.title()],
                    provider=self.provider.instance_id,
                    items=UniqueList(genre_tracks),
                    subtitle="Top tracks in your top genres",
                    icon="mdi-music",
                )

    async def _get_top_genres(self) -> list[str]:
        """Return the user's top genres, most prominent first, derived from their listening."""
        username = self.provider.config.get_value(CONF_USERNAME)
        if not username or not isinstance(username, str):
            return []

        # Deriving genres costs a request per top artist, so cache the result.
        cached = await self.mass.cache.get(
            key="top_genres",
            category=CACHE_CATEGORY_TOP_GENRES,
            provider=self.provider.instance_id,
        )
        if isinstance(cached, list):
            return cached

        top_artists = await self.api.get_user_top_artists(
            username, period=GENRE_ARTISTS_PERIOD, limit=GENRE_ARTISTS_LIMIT
        )
        if not top_artists:
            return []

        # Last.fm has no genre data, so an artist's top community tags stand in for its genre.
        # The user's genres are those tags aggregated across their most played artists, weighted
        # by how much they play each (playcount) and how strongly each tag applies (count 0-100).
        tag_lists = await asyncio.gather(
            *[
                self.api.get_artist_top_tags(
                    artist.get("name", ""), artist.get("mbid"), limit=TAGS_PER_ARTIST
                )
                for artist in top_artists
            ]
        )

        scores: dict[str, float] = {}
        display_names: dict[str, str] = {}
        for artist, tags in zip(top_artists, tag_lists, strict=True):
            try:
                artist_weight = float(artist.get("playcount", 0))
            except TypeError, ValueError:
                continue
            if artist_weight <= 0:
                continue
            for tag in tags:
                name = tag.get("name", "")
                if not name:
                    continue
                try:
                    tag_count = float(tag.get("count", 0))
                except TypeError, ValueError:
                    continue
                key = name.lower()
                scores[key] = scores.get(key, 0.0) + artist_weight * tag_count / 100
                display_names.setdefault(key, name)

        ranked = sorted(scores, key=lambda key: scores[key], reverse=True)
        top_genres = [display_names[key] for key in ranked[:TOP_GENRES_LIMIT]]

        if top_genres:
            await self.mass.cache.set(
                "top_genres",
                top_genres,
                category=CACHE_CATEGORY_TOP_GENRES,
                provider=self.provider.instance_id,
                expiration=TOP_GENRES_CACHE_EXPIRATION_SECONDS,
            )
        return top_genres

    async def _get_geo_based_recommendations(self) -> AsyncIterator[RecommendationFolder]:
        """Yield geography-based recommendation folders for the configured country."""
        if not self.provider.config.get_value(CONF_ENABLE_GEO):
            return

        country = self.provider.config.get_value(CONF_GEO_COUNTRY)
        if not country or not isinstance(country, str):
            return

        geo_artists_raw = await self.api.get_geo_top_artists(country, limit=RESOLUTION_BUFFER_SMALL)
        if geo_artists_raw:
            resolved_artists = await asyncio.gather(
                *[self.get_or_resolve_artist(artist_data) for artist_data in geo_artists_raw]
            )
            all_resolved = [artist for artist in resolved_artists if artist is not None]
            geo_artists = list(UniqueList(all_resolved))[:TARGET_ITEM_COUNT]

            if geo_artists:
                yield RecommendationFolder(
                    item_id=f"{self.provider.instance_id}_geo_artists",
                    name=f"Top artists for {country}",
                    translation_key="geo_artists",
                    translation_params=[country],
                    provider=self.provider.instance_id,
                    items=UniqueList(geo_artists),
                    subtitle=f"Most popular artists in {country}",
                    icon="mdi-earth",
                )

        geo_tracks_raw = await self.api.get_geo_top_tracks(country, limit=RESOLUTION_BUFFER_SMALL)
        if geo_tracks_raw:
            resolved_tracks = await asyncio.gather(
                *[self.get_or_resolve_track(track_data) for track_data in geo_tracks_raw]
            )
            all_resolved_geo_tracks = [track for track in resolved_tracks if track is not None]
            geo_tracks = list(UniqueList(all_resolved_geo_tracks))[:TARGET_ITEM_COUNT]

            if geo_tracks:
                yield RecommendationFolder(
                    item_id=f"{self.provider.instance_id}_geo_tracks",
                    name=f"Top tracks for {country}",
                    translation_key="geo_tracks",
                    translation_params=[country],
                    provider=self.provider.instance_id,
                    items=UniqueList(geo_tracks),
                    subtitle=f"Most popular tracks in {country}",
                    icon="mdi-earth",
                )

    async def _get_recent_play_seeds(self) -> list[_SeedTrack]:
        """
        Return artist/track pairs for recently played tracks, most recent first.

        Plays without a recorded artist are omitted.
        """
        # Rank by recent plays, not lifetime play_count, so seeds follow current listening.
        # Artists are stored on the playlog at play time, so no provider lookup is needed.
        cutoff = int(utc_timestamp()) - RECENT_PLAYS_WINDOW_DAYS * 24 * 60 * 60
        plays = await self.mass.music.recently_played_tracks(
            limit=RECENT_PLAYS_SCAN_LIMIT,
            played_after_timestamp=cutoff,
        )
        # Skip rows predating the playlog artists column; they regain an artist on the next play.
        # Various Artists is a compilation placeholder, not a usable seed.
        return [
            _SeedTrack(artist=play.artists[0].name, name=play.name)
            for play in plays
            if play.artists and play.artists[0].name != VARIOUS_ARTISTS_NAME
        ]

    def _rank_seed_tracks(self, recent_plays: list[_SeedTrack]) -> list[_SeedTrack]:
        """
        Return distinct recently played tracks, most recent first.

        :param recent_plays: Recently played artist/track pairs, most recent first.
        """
        seen: set[tuple[str, str]] = set()
        unique: list[_SeedTrack] = []
        for seed in recent_plays:
            key = (seed.artist.lower(), seed.name.lower())
            if key not in seen:
                seen.add(key)
                unique.append(seed)
        return unique

    def _rank_seed_artists(self, recent_plays: list[_SeedTrack]) -> list[str]:
        """
        Rank artists by how many of their tracks were recently played, most first.

        :param recent_plays: Recently played artist/track pairs, most recent first.
        """
        # The playlog has no per-play counts, so rank by distinct recently played tracks per
        # artist; ties fall to the more recently played artist via the most-recent-first order.
        counts: dict[str, int] = {}
        names_by_key: dict[str, str] = {}
        order: list[str] = []
        for seed in self._rank_seed_tracks(recent_plays):
            key = seed.artist.lower()
            if key not in counts:
                order.append(key)
                names_by_key[key] = seed.artist
            counts[key] = counts[key] + 1 if key in counts else 1

        order.sort(key=lambda key: counts[key], reverse=True)
        return [names_by_key[key] for key in order[:TOP_ARTISTS_LIMIT]]

    async def _get_similar_artists_from_seeds(self, seed_artists: list[str]) -> list[Artist]:
        """
        Return resolved artists similar to the given seed artists.

        :param seed_artists: Names of the user's recently played artists.
        """
        all_similar: list[dict[str, Any]] = []

        # Seed names are tracked so seeds don't appear in their own recommendations.
        seed_names = {name.lower() for name in seed_artists}

        similar_lists = await asyncio.gather(
            *[
                self.api.get_similar_artists(
                    artist_name=name,
                    artist_mbid=None,
                    limit=SIMILAR_ITEMS_PER_SEED,
                )
                for name in seed_artists
            ]
        )
        for similar in similar_lists:
            all_similar.extend(similar)

        # Deduplicate by MBID and by name: Last.fm sometimes returns the same artist twice,
        # once with an MBID and once without.
        seen_mbids = set()
        seen_names = set()
        unique_similar: list[dict[str, Any]] = []
        for artist_data in all_similar:
            mbid = artist_data.get("mbid")
            name = artist_data.get("name", "").lower()

            if name and name in seed_names:
                continue

            if mbid and mbid in seen_mbids:
                continue
            if name and name in seen_names:
                continue

            unique_similar.append(artist_data)
            if mbid:
                seen_mbids.add(mbid)
            if name:
                seen_names.add(name)

        unique_similar.sort(key=lambda x: float(x.get("match", 0)), reverse=True)

        resolved_artists = await asyncio.gather(
            *[
                self.get_or_resolve_artist(artist_data)
                for artist_data in unique_similar[:SIMILAR_ITEMS_BUFFER]
            ]
        )
        return self._exclude_owned([artist for artist in resolved_artists if artist is not None])

    async def _get_similar_tracks_from_seeds(self, seed_tracks: list[_SeedTrack]) -> list[Track]:
        """
        Return resolved tracks similar to the given seed tracks.

        :param seed_tracks: The user's recently played artist/track pairs.
        """
        all_similar: list[dict[str, Any]] = []

        # Seed names are tracked so seeds don't appear in their own recommendations.
        seed_name_keys = {f"{seed.artist}_{seed.name}".lower() for seed in seed_tracks}

        similar_lists = await asyncio.gather(
            *[
                self.api.get_similar_tracks(
                    artist_name=seed.artist,
                    track_name=seed.name,
                    track_mbid=None,
                    limit=SIMILAR_ITEMS_PER_SEED,
                )
                for seed in seed_tracks
            ]
        )
        for similar in similar_lists:
            all_similar.extend(similar)

        # Deduplicate by MBID and by artist+name: Last.fm sometimes returns the same track
        # twice, once with a MBID and once without.
        seen_mbids = set()
        seen_names = set()
        unique_similar: list[dict[str, Any]] = []
        for track_data in all_similar:
            mbid = track_data.get("mbid")

            artist_info = track_data.get("artist", {})
            if isinstance(artist_info, str):
                artist_name = artist_info
            else:
                artist_name = artist_info.get("name", "")
            track_name = track_data.get("name", "")
            name_key = f"{artist_name}_{track_name}".lower() if artist_name and track_name else ""

            if name_key and name_key in seed_name_keys:
                continue

            if mbid and mbid in seen_mbids:
                continue
            if name_key and name_key in seen_names:
                continue

            unique_similar.append(track_data)
            if mbid:
                seen_mbids.add(mbid)
            if name_key:
                seen_names.add(name_key)

        unique_similar.sort(key=lambda x: float(x.get("match", 0)), reverse=True)

        # Resolve a small buffer beyond the target so resolution failures and owned-copy
        # exclusion still leave enough to fill the row, while capping provider searches.
        top_tracks_data = unique_similar[:SIMILAR_TRACKS_BUFFER]

        resolved_tracks = await asyncio.gather(
            *[self.get_or_resolve_track(track_data) for track_data in top_tracks_data]
        )
        return self._exclude_owned([track for track in resolved_tracks if track is not None])
