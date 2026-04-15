"""Recommendation logic for Last.fm."""

from __future__ import annotations

import asyncio
import datetime
import random
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    RecommendationFolder,
    Track,
    UniqueList,
)

from music_assistant.providers.lastfm_recommendations.constants import (
    CACHE_CATEGORY_RESOLVED_ITEMS,
    CACHE_EXPIRATION_SECONDS,
    RESOLUTION_BUFFER_LARGE,
    RESOLUTION_BUFFER_SMALL,
    SIMILAR_ITEMS_BUFFER,
    SIMILAR_ITEMS_PER_SEED,
    TARGET_ITEM_COUNT,
    TOP_ARTISTS_LIMIT,
    TOP_ITEMS_TO_TAKE,
    TOP_TAGS_LIMIT,
    TOP_TRACKS_LIMIT,
)
from music_assistant.providers.lastfm_recommendations.parsers import (
    parse_album,
    parse_artist,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant.providers.lastfm_recommendations import LastFMRecommendationsProvider


class LastFMRecommendationManager:
    """Manages Last.fm recommendations."""

    def __init__(self, provider: LastFMRecommendationsProvider) -> None:
        """Initialize recommendation manager.

        :param provider: The Last.fm recommendations provider instance.
        """
        self.provider = provider
        self.api = provider.api
        self.mbid_resolver = provider.mbid_resolver
        self.logger = provider.logger
        self.mass = provider.mass

        # Resolved items keyed by MBID (preferred) or name to avoid re-resolving.
        self._resolved_cache: dict[str, Artist | Album | Track] = {}

    async def clear_cache(self) -> None:
        """Clear in-memory and persistent recommendation caches."""
        self._resolved_cache.clear()

        await self.mass.cache.clear(category_filter=CACHE_CATEGORY_RESOLVED_ITEMS)

        self.provider._recommendation_folders.clear()
        self.provider._recommendations_populated = False

        self.logger.info("Cleared all recommendation caches (in-memory and persistent)")

    async def _is_in_library(self, item_data: dict[str, Any], media_type: MediaType) -> bool:
        """Return True if the Last.fm item already exists in the MA library.

        :param item_data: Raw Last.fm item data (artist, album, or track dict).
        :param media_type: Type of media item to check.
        """
        # MBID lookup is the most reliable; fall back to name search for items without MBID.
        mbid = item_data.get("mbid")
        if mbid:
            if media_type == MediaType.ARTIST:
                if await self.mass.music.artists.get_library_item_by_external_id(
                    mbid, ExternalID.MB_ARTIST
                ):
                    return True
            elif media_type == MediaType.ALBUM:
                if await self.mass.music.albums.get_library_item_by_external_id(
                    mbid, ExternalID.MB_ALBUM
                ):
                    return True
            elif media_type == MediaType.TRACK:
                if await self.mass.music.tracks.get_library_item_by_external_id(
                    mbid, ExternalID.MB_RECORDING
                ):
                    return True

        if media_type == MediaType.ARTIST:
            name = item_data.get("name", "")
            if name:
                artist_results = await self.mass.music.artists.library_items(search=name, limit=1)
                return len(artist_results) > 0

        elif media_type == MediaType.ALBUM:
            name = item_data.get("name", "")
            if name:
                album_results = await self.mass.music.albums.library_items(search=name, limit=1)
                return len(album_results) > 0

        elif media_type == MediaType.TRACK:
            artist_info = item_data.get("artist", {})
            artist_name = (
                artist_info if isinstance(artist_info, str) else artist_info.get("name", "")
            )
            track_name = item_data.get("name", "")
            if track_name and artist_name:
                search_query = f"{artist_name} {track_name}"
                track_results = await self.mass.music.tracks.library_items(
                    search=search_query, limit=1
                )
                return len(track_results) > 0

        return False

    def _sample_items(
        self, items: list[dict[str, Any]], seed_suffix: str, target_count: int = TARGET_ITEM_COUNT
    ) -> list[dict[str, Any]]:
        """Sample items using a 'top N + random remainder' strategy with a daily seed.

        :param items: List of items to sample from (already filtered).
        :param seed_suffix: Unique suffix for random seed (to vary between recommendation types).
        :param target_count: Target number of items to return.
        """
        if len(items) <= target_count:
            return items

        top_items = items[:TOP_ITEMS_TO_TAKE]

        remaining = items[TOP_ITEMS_TO_TAKE:]
        random_count = target_count - TOP_ITEMS_TO_TAKE

        # Daily seed keeps recommendations stable within the day and rotates them overnight.
        seed = f"{datetime.datetime.now(tz=datetime.UTC).date().isoformat()}_{seed_suffix}"
        random.seed(seed)
        random_items = random.sample(remaining, min(random_count, len(remaining)))

        return top_items + random_items

    async def _get_or_resolve_artist(self, lastfm_artist: dict[str, Any]) -> Artist | None:
        """Return an Artist from cache (in-memory or persistent) or resolve and cache it.

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
                expiration=CACHE_EXPIRATION_SECONDS,
            )
        return artist

    async def _get_or_resolve_track(self, lastfm_track: dict[str, Any]) -> Track | None:
        """Return a Track from cache (in-memory or persistent) or resolve and cache it.

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
            base_class=Track,
        )
        if isinstance(cached_track, Track):
            self._resolved_cache[cache_key] = cached_track
            return cached_track

        track = await parse_track(
            lastfm_track, self.mbid_resolver, self.mass, self.provider.instance_id
        )
        if track:
            self._resolved_cache[cache_key] = track
            await self.mass.cache.set(
                persistent_cache_key,
                track.to_dict(),
                category=CACHE_CATEGORY_RESOLVED_ITEMS,
                expiration=CACHE_EXPIRATION_SECONDS,
            )
        return track

    async def _get_or_resolve_album(self, lastfm_album: dict[str, Any]) -> Album | None:
        """Return an Album from cache (in-memory or persistent) or resolve and cache it.

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
                expiration=CACHE_EXPIRATION_SECONDS,
            )
        return album

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return all recommendation folders for this provider.

        Individual category methods return empty lists on failure, so errors should not
        bubble up here; if they do it indicates a bug.
        """
        folders: list[RecommendationFolder] = []

        folders.extend(await self._get_personalized_recommendations())
        folders.extend(await self._get_global_recommendations())
        folders.extend(await self._get_genre_based_recommendations())
        folders.extend(await self._get_geo_based_recommendations())

        return folders

    async def _get_personalized_recommendations(self) -> list[RecommendationFolder]:
        """Return personalized recommendation folders based on the user's listening history."""
        folders: list[RecommendationFolder] = []

        if not self.provider.config.get_value("enable_personalized"):
            return folders

        # TODO: evaluate recent play history (e.g. last_played, last 7 days) instead of all-time
        # play_count, possibly weighted. Needs user feedback.

        top_artists = await self.mass.music.artists.library_items(
            limit=TOP_ARTISTS_LIMIT, order_by="play_count_desc"
        )

        if top_artists:
            similar_artists = await self._get_similar_artists_from_seeds(top_artists)

            if similar_artists:
                folders.append(
                    RecommendationFolder(
                        item_id=f"{self.provider.instance_id}_similar_artists",
                        name="Discover Similar Artists",
                        provider=self.provider.instance_id,
                        items=UniqueList(similar_artists[:TARGET_ITEM_COUNT]),
                        subtitle=f"Based on your top {len(top_artists)} artists",
                        icon="mdi-account-music-outline",
                    )
                )

        top_tracks = await self.mass.music.tracks.library_items(
            limit=TOP_TRACKS_LIMIT, order_by="play_count_desc"
        )

        if top_tracks:
            similar_tracks = await self._get_similar_tracks_from_seeds(top_tracks)

            if similar_tracks:
                folders.append(
                    RecommendationFolder(
                        item_id=f"{self.provider.instance_id}_similar_tracks",
                        name="Discover Similar Tracks",
                        provider=self.provider.instance_id,
                        items=UniqueList(similar_tracks[:TARGET_ITEM_COUNT]),
                        subtitle=f"Based on your top {len(top_tracks)} tracks",
                        icon="mdi-music-note-outline",
                    )
                )

        return folders

    async def _get_global_recommendations(self) -> list[RecommendationFolder]:
        """Return global chart recommendation folders (worldwide top artists and tracks)."""
        folders: list[RecommendationFolder] = []

        if not self.provider.config.get_value("enable_global_charts"):
            return folders

        # Over-fetch so deduplication and resolution failures still leave TARGET_ITEM_COUNT.
        top_artists_raw = await self.api.get_chart_top_artists(limit=RESOLUTION_BUFFER_SMALL)
        if top_artists_raw:
            resolved_artists = await asyncio.gather(
                *[self._get_or_resolve_artist(artist_data) for artist_data in top_artists_raw]
            )
            all_resolved = [artist for artist in resolved_artists if artist is not None]
            deduplicated = list(UniqueList(all_resolved))[:TARGET_ITEM_COUNT]
            top_artists = UniqueList(deduplicated)

            if len(top_artists) < TARGET_ITEM_COUNT:
                self.logger.debug(
                    "Global Top Artists: only %d/%d items after resolution "
                    "(requested %d, resolved %d)",
                    len(top_artists),
                    TARGET_ITEM_COUNT,
                    RESOLUTION_BUFFER_SMALL,
                    len([a for a in resolved_artists if a is not None]),
                )

            if top_artists:
                folders.append(
                    RecommendationFolder(
                        item_id=f"{self.provider.instance_id}_chart_top_artists",
                        name="Global Top Artists",
                        provider=self.provider.instance_id,
                        items=UniqueList(top_artists),
                        subtitle="Most popular artists worldwide",
                        icon="mdi-chart-line",
                    )
                )

        top_tracks_raw = await self.api.get_chart_top_tracks(limit=RESOLUTION_BUFFER_SMALL)
        if top_tracks_raw:
            resolved_tracks = await asyncio.gather(
                *[self._get_or_resolve_track(track_data) for track_data in top_tracks_raw]
            )
            all_resolved_tracks = [track for track in resolved_tracks if track is not None]
            deduplicated_tracks = list(UniqueList(all_resolved_tracks))[:TARGET_ITEM_COUNT]
            top_tracks = UniqueList(deduplicated_tracks)

            if len(top_tracks) < TARGET_ITEM_COUNT:
                self.logger.debug(
                    "Global Top Tracks: only %d/%d items after resolution "
                    "(requested %d, resolved %d)",
                    len(top_tracks),
                    TARGET_ITEM_COUNT,
                    RESOLUTION_BUFFER_SMALL,
                    len(all_resolved_tracks),
                )

            if top_tracks:
                folders.append(
                    RecommendationFolder(
                        item_id=f"{self.provider.instance_id}_chart_top_tracks",
                        name="Global Top Tracks",
                        provider=self.provider.instance_id,
                        items=UniqueList(top_tracks),
                        subtitle="Most popular tracks worldwide",
                        icon="mdi-chart-box",
                    )
                )

        return folders

    async def _get_genre_based_recommendations(self) -> list[RecommendationFolder]:
        """Return genre-based recommendation folders derived from the user's top Last.fm tag.

        Requires a username to be configured.
        """
        folders: list[RecommendationFolder] = []

        if not self.provider.config.get_value("enable_genre"):
            return folders

        username = self.provider.config.get_value("username")
        if not username or not isinstance(username, str):
            return folders

        top_tags = await self.api.get_user_top_tags(username, limit=TOP_TAGS_LIMIT)
        if not top_tags:
            return folders

        tag_name = top_tags[0].get("name")
        if not tag_name:
            return folders

        # Over-fetch so there's enough left after library filtering and resolution failures.
        genre_artists_raw = await self.api.get_tag_top_artists(
            tag_name, limit=RESOLUTION_BUFFER_LARGE
        )
        if genre_artists_raw:
            # Drop items already in the library using a cheap DB lookup, before the
            # expensive MusicBrainz + provider resolution step.
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
                *[self._get_or_resolve_artist(artist_data) for artist_data in sampled_artists_raw]
            )
            all_resolved = [artist for artist in resolved_artists if artist is not None]
            deduplicated = list(UniqueList(all_resolved))[:TARGET_ITEM_COUNT]
            genre_artists = UniqueList(deduplicated)

            if len(genre_artists) < TARGET_ITEM_COUNT:
                self.logger.debug(
                    "Genre Artists (%s): only %d/%d items after resolution "
                    "(requested %d, resolved %d)",
                    tag_name,
                    len(genre_artists),
                    TARGET_ITEM_COUNT,
                    RESOLUTION_BUFFER_SMALL,
                    len([a for a in resolved_artists if a is not None]),
                )

            if genre_artists:
                folders.append(
                    RecommendationFolder(
                        item_id=f"{self.provider.instance_id}_genre_artists",
                        name=f"Discover {tag_name.title()} Artists",
                        provider=self.provider.instance_id,
                        items=UniqueList(genre_artists),
                        subtitle="Top artists in your most played genre",
                        icon="mdi-account-music",
                    )
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
            all_resolved_albums = [album for album in resolved_albums if album is not None]
            genre_albums = list(UniqueList(all_resolved_albums))[:TARGET_ITEM_COUNT]

            if len(genre_albums) < TARGET_ITEM_COUNT:
                self.logger.debug(
                    "Genre Albums (%s): only %d/%d items after resolution "
                    "(requested %d, resolved %d)",
                    tag_name,
                    len(genre_albums),
                    TARGET_ITEM_COUNT,
                    RESOLUTION_BUFFER_SMALL,
                    len(all_resolved_albums),
                )

            if genre_albums:
                folders.append(
                    RecommendationFolder(
                        item_id=f"{self.provider.instance_id}_genre_albums",
                        name=f"Discover {tag_name.title()} Albums",
                        provider=self.provider.instance_id,
                        items=UniqueList(genre_albums),
                        subtitle="Top albums in your most played genre",
                        icon="mdi-album",
                    )
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
                *[self._get_or_resolve_track(track_data) for track_data in sampled_tracks_raw]
            )
            all_resolved_genre_tracks = [track for track in resolved_tracks if track is not None]
            genre_tracks = list(UniqueList(all_resolved_genre_tracks))[:TARGET_ITEM_COUNT]

            if len(genre_tracks) < TARGET_ITEM_COUNT:
                self.logger.debug(
                    "Genre Tracks (%s): only %d/%d items after resolution "
                    "(requested %d, resolved %d)",
                    tag_name,
                    len(genre_tracks),
                    TARGET_ITEM_COUNT,
                    RESOLUTION_BUFFER_SMALL,
                    len(all_resolved_genre_tracks),
                )

            if genre_tracks:
                folders.append(
                    RecommendationFolder(
                        item_id=f"{self.provider.instance_id}_genre_tracks",
                        name=f"Discover {tag_name.title()} Tracks",
                        provider=self.provider.instance_id,
                        items=UniqueList(genre_tracks),
                        subtitle="Top tracks in your most played genre",
                        icon="mdi-music",
                    )
                )

        return folders

    async def _get_geo_based_recommendations(self) -> list[RecommendationFolder]:
        """Return geography-based recommendation folders for the configured country."""
        folders: list[RecommendationFolder] = []

        if not self.provider.config.get_value("enable_geo"):
            return folders

        country = self.provider.config.get_value("geo_country")
        if not country or not isinstance(country, str):
            return folders

        geo_artists_raw = await self.api.get_geo_top_artists(country, limit=RESOLUTION_BUFFER_SMALL)
        if geo_artists_raw:
            resolved_artists = await asyncio.gather(
                *[self._get_or_resolve_artist(artist_data) for artist_data in geo_artists_raw]
            )
            all_resolved = [artist for artist in resolved_artists if artist is not None]
            geo_artists = list(UniqueList(all_resolved))[:TARGET_ITEM_COUNT]

            if len(geo_artists) < TARGET_ITEM_COUNT:
                self.logger.debug(
                    "Geo Top Artists (%s): only %d/%d items after resolution "
                    "(requested %d, resolved %d)",
                    country,
                    len(geo_artists),
                    TARGET_ITEM_COUNT,
                    RESOLUTION_BUFFER_SMALL,
                    len([a for a in resolved_artists if a is not None]),
                )

            if geo_artists:
                folders.append(
                    RecommendationFolder(
                        item_id=f"{self.provider.instance_id}_geo_artists",
                        name=f"Top artists for {country}",
                        provider=self.provider.instance_id,
                        items=UniqueList(geo_artists),
                        subtitle=f"Most popular artists in {country}",
                        icon="mdi-earth",
                    )
                )

        geo_tracks_raw = await self.api.get_geo_top_tracks(country, limit=RESOLUTION_BUFFER_SMALL)
        if geo_tracks_raw:
            resolved_tracks = await asyncio.gather(
                *[self._get_or_resolve_track(track_data) for track_data in geo_tracks_raw]
            )
            all_resolved_geo_tracks = [track for track in resolved_tracks if track is not None]
            geo_tracks = list(UniqueList(all_resolved_geo_tracks))[:TARGET_ITEM_COUNT]

            if len(geo_tracks) < TARGET_ITEM_COUNT:
                self.logger.debug(
                    "Geo Top Tracks (%s): only %d/%d items after resolution "
                    "(requested %d, resolved %d)",
                    country,
                    len(geo_tracks),
                    TARGET_ITEM_COUNT,
                    RESOLUTION_BUFFER_SMALL,
                    len(all_resolved_geo_tracks),
                )

            if geo_tracks:
                folders.append(
                    RecommendationFolder(
                        item_id=f"{self.provider.instance_id}_geo_tracks",
                        name=f"Top tracks for {country}",
                        provider=self.provider.instance_id,
                        items=UniqueList(geo_tracks),
                        subtitle=f"Most popular tracks in {country}",
                        icon="mdi-earth",
                    )
                )

        return folders

    async def _get_similar_artists_from_seeds(self, seed_artists: list[Artist]) -> list[Artist]:
        """Return resolved artists similar to the given seed artists.

        :param seed_artists: Seed artists from the user's library.
        """
        all_similar: list[dict[str, Any]] = []

        # Seed identifiers are tracked so seeds don't appear in their own recommendations.
        seed_mbids = {
            seed_artist.get_external_id(ExternalID.MB_ARTIST)
            for seed_artist in seed_artists
            if seed_artist.get_external_id(ExternalID.MB_ARTIST)
        }
        seed_names = {seed_artist.name.lower() for seed_artist in seed_artists}

        for seed_artist in seed_artists:
            mbid = seed_artist.get_external_id(ExternalID.MB_ARTIST)

            similar = await self.api.get_similar_artists(
                artist_name=seed_artist.name, artist_mbid=mbid, limit=SIMILAR_ITEMS_PER_SEED
            )
            all_similar.extend(similar)

        # Deduplicate by MBID and by name: Last.fm sometimes returns the same artist twice,
        # once with an MBID and once without.
        seen_mbids = set()
        seen_names = set()
        unique_similar: list[dict[str, Any]] = []
        for artist_data in all_similar:
            mbid = artist_data.get("mbid")
            name = artist_data.get("name", "").lower()

            if mbid and mbid in seed_mbids:
                continue
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
                self._get_or_resolve_artist(artist_data)
                for artist_data in unique_similar[:SIMILAR_ITEMS_BUFFER]
            ]
        )
        result = [artist for artist in resolved_artists if artist is not None]

        if len(result) < len(resolved_artists):
            self.logger.debug(
                "Similar artists: %d/%d resolved successfully (%d failed)",
                len(result),
                len(resolved_artists),
                len(resolved_artists) - len(result),
            )

        return result

    async def _get_similar_tracks_from_seeds(self, seed_tracks: list[Track]) -> list[Track]:
        """Return resolved tracks similar to the given seed tracks.

        :param seed_tracks: Seed tracks from the user's library.
        """
        all_similar: list[dict[str, Any]] = []

        # Seed identifiers are tracked so seeds don't appear in their own recommendations.
        seed_mbids = {
            seed_track.get_external_id(ExternalID.MB_RECORDING)
            for seed_track in seed_tracks
            if seed_track.get_external_id(ExternalID.MB_RECORDING)
        }
        seed_name_keys = {
            f"{seed_track.artists[0].name if seed_track.artists else ''}_{seed_track.name}".lower()
            for seed_track in seed_tracks
        }

        for seed_track in seed_tracks:
            mbid = seed_track.get_external_id(ExternalID.MB_RECORDING)

            artist_name = seed_track.artists[0].name if seed_track.artists else "Unknown Artist"

            similar = await self.api.get_similar_tracks(
                artist_name=artist_name,
                track_name=seed_track.name,
                track_mbid=mbid,
                limit=SIMILAR_ITEMS_PER_SEED,
            )
            all_similar.extend(similar)

        # Deduplicate by MBID and by artist+name: Last.fm sometimes returns the same track
        # twice, once with an MBID and once without.
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

            if mbid and mbid in seed_mbids:
                continue
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

        # Only resolve ISRCs for the top results to avoid unnecessary MusicBrainz lookups.
        top_tracks_data = unique_similar[:TARGET_ITEM_COUNT]

        resolved_tracks = await asyncio.gather(
            *[self._get_or_resolve_track(track_data) for track_data in top_tracks_data]
        )
        return [track for track in resolved_tracks if track is not None]
