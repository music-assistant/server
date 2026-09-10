"""
Media operations manager for the Deezer provider.

Handles library retrieval, search, item getters, content getters,
library mutations, and playlist CRUD operations.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from deezer_python_gql import GraphQLClientGraphQLMultiError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, UnsupportedFeaturedException
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    ItemMapping,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date

from .constants import (
    AUDIOBOOK_CHAPTERS_PAGE_SIZE,
    FAVORITES_PAGE_SIZE,
    PERSONAL_ALBUM_PREFIX,
    PERSONAL_ARTIST_PREFIX,
)
from .helpers import fetch_all_audiobook_chapter_edges, fetch_all_bookmarks
from .parsers import (
    apply_web_url,
    parse_album,
    parse_artist,
    parse_audiobook,
    parse_audiobook_chapters,
    parse_audiobook_from_album,
    parse_date,
    parse_gw_track,
    parse_playlist,
    parse_podcast,
    parse_podcast_episode,
    parse_radio,
    parse_track,
)

if TYPE_CHECKING:
    from .provider import DeezerProvider


# -- Protocols for typed pagination --


class _PageInfo(Protocol):
    @property
    def has_next_page(self) -> bool: ...

    @property
    def end_cursor(self) -> str | None: ...


class _Connection(Protocol):
    @property
    def edges(self) -> list[Any]: ...

    @property
    def page_info(self) -> _PageInfo: ...


def _is_complexity_error(err: GraphQLClientGraphQLMultiError) -> bool:
    """Check if a GraphQL error is a query complexity limit violation."""
    return any("complexity" in e.message.lower() for e in err.errors)


class DeezerMediaManager:
    """Handles library sync, search, item getters, and mutations."""

    def __init__(self, provider: DeezerProvider) -> None:
        """Initialize media manager."""
        self.provider = provider
        self.mass = provider.mass
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        self.logger = provider.logger
        self._audiobook_ids_in_favorites: set[str] | None = None

    # -- Pagination helper --

    async def _iter_paged(
        self,
        fetch: Callable[..., Awaitable[Any]],
        extract: Callable[..., _Connection | None],
    ) -> AsyncGenerator[Any]:
        """Iterate a cursor-paginated connection, yielding edges with non-null nodes."""
        cursor: str | None = None
        while True:
            result = await fetch(first=FAVORITES_PAGE_SIZE, after=cursor)
            if result is None:
                break
            connection = extract(result)
            if connection is None:
                break
            for edge in connection.edges:
                if edge.node is not None:
                    yield edge
            if not connection.page_info.has_next_page:
                break
            cursor = connection.page_info.end_cursor

    # -- Personal songs cache --

    @use_cache(3600 * 24)
    async def _get_personal_songs(self) -> list[dict[str, Any]]:
        """Fetch all user-uploaded personal songs via the GW API (cached 24h)."""
        all_songs: list[dict[str, Any]] = []
        start = 0
        page_size = 500
        while True:
            results = await self.provider.gw_client.get_personal_songs(start=start, nb=page_size)
            data: list[dict[str, Any]] = results.get("data", [])
            all_songs.extend(data)
            if len(data) < page_size:
                break
            start += page_size
        return all_songs

    # -- Library retrieval --

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve all library artists from Deezer."""
        async for edge in self._iter_paged(
            self.provider.gql_client.get_favorite_artists,
            lambda r: r.user_favorites.artists,
        ):
            item = parse_artist(self.provider, edge.node)
            if edge.favorited_at:
                item.date_added = parse_date(edge.favorited_at)
            yield item
        # Also include artists from user-uploaded personal songs
        personal_songs = await self._get_personal_songs()
        seen_artist_names: set[str] = set()
        for song in personal_songs:
            track = parse_gw_track(self.provider, song)
            for artist in track.artists:
                if isinstance(artist, Artist) and artist.name not in seen_artist_names:
                    seen_artist_names.add(artist.name)
                    yield artist

    async def _get_audiobook_ids_in_albums(self) -> set[str]:
        """Identify which favorite album IDs are actually audiobooks."""
        # Deezer stores audiobook favorites in the albums list, not in the
        # dedicated (deprecated) audiobook favorites endpoint. We use
        # check_audiobook_ids to tell them apart. Result is cached for the
        # lifetime of this manager instance so both get_library_albums and
        # get_library_audiobooks can share it without extra API calls.
        if self._audiobook_ids_in_favorites is not None:
            return self._audiobook_ids_in_favorites
        album_ids: list[str] = []
        async for edge in self._iter_paged(
            self.provider.gql_client.get_favorite_albums,
            lambda r: r.user_favorites.albums,
        ):
            album_ids.append(edge.node.id)
        if not album_ids:
            self._audiobook_ids_in_favorites = set()
        else:
            self._audiobook_ids_in_favorites = await self.provider.gql_client.check_audiobook_ids(
                album_ids
            )
        return self._audiobook_ids_in_favorites

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve all library albums from Deezer."""
        # Collect all favorite album edges in a single pass, then determine
        # which are audiobooks via check_audiobook_ids, and yield the rest.
        all_edges: list[Any] = []
        async for edge in self._iter_paged(
            self.provider.gql_client.get_favorite_albums,
            lambda r: r.user_favorites.albums,
        ):
            all_edges.append(edge)
        # Populate the favorites-audiobook cache (shared with get_library_audiobooks)
        if self._audiobook_ids_in_favorites is None:
            album_ids = [edge.node.id for edge in all_edges]
            self._audiobook_ids_in_favorites = (
                await self.provider.gql_client.check_audiobook_ids(album_ids)
                if album_ids
                else set()
            )
        for edge in all_edges:
            if edge.node.id in self._audiobook_ids_in_favorites:
                continue
            item = parse_album(self.provider, edge.node)
            if edge.favorited_at:
                item.date_added = parse_date(edge.favorited_at)
            yield item
        # Also include albums from user-uploaded personal songs
        personal_songs = await self._get_personal_songs()
        seen_album_names: set[str] = set()
        for song in personal_songs:
            track = parse_gw_track(self.provider, song)
            if isinstance(track.album, Album) and track.album.name not in seen_album_names:
                seen_album_names.add(track.album.name)
                yield track.album

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve all library playlists from Deezer."""
        # User-owned playlists first
        seen_ids: set[str] = set()
        async for edge in self._iter_paged(
            self.provider.gql_client.get_user_playlists,
            lambda r: r.playlists,
        ):
            seen_ids.add(edge.node.id)
            yield parse_playlist(self.provider, edge.node, is_editable=True)
        # Favorited playlists (other users' playlists)
        async for edge in self._iter_paged(
            self.provider.gql_client.get_favorite_playlists,
            lambda r: r.user_favorites.playlists,
        ):
            if edge.node.id in seen_ids:
                continue
            item = parse_playlist(self.provider, edge.node)
            if edge.favorited_at:
                item.date_added = parse_date(edge.favorited_at)
            yield item

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve all library tracks from Deezer (favorites + personal uploads)."""
        async for edge in self._iter_paged(
            self.provider.gql_client.get_favorite_tracks,
            lambda r: r.user_favorites.tracks,
        ):
            item = parse_track(self.provider, edge.node)
            if edge.favorited_at:
                item.date_added = parse_date(edge.favorited_at)
            yield item
        # Also include user-uploaded personal songs
        personal_songs = await self._get_personal_songs()
        for idx, song in enumerate(personal_songs, 1):
            yield parse_gw_track(self.provider, song, position=idx)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve library/subscribed podcasts from Deezer."""
        async for edge in self._iter_paged(
            self.provider.gql_client.get_favorite_podcasts,
            lambda r: r.user_favorites.podcasts,
        ):
            item = parse_podcast(self.provider, edge.node)
            if edge.favorited_at:
                item.date_added = parse_date(edge.favorited_at)
            yield item

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """
        Retrieve library/subscribed audiobooks from Deezer.

        Checks both the dedicated (deprecated) audiobook favorites endpoint
        and the regular favorite albums list, since Deezer stores audiobook
        favorites in the albums list.
        """
        seen_ids: set[str] = set()
        # 1. Dedicated audiobook favorites (deprecated but may still have entries)
        result = await self.provider.gql_client.get_favorite_audiobooks()
        if result is not None and result.favorites.raw_audiobooks is not None:
            for raw in result.favorites.raw_audiobooks:
                try:
                    item = await self.get_audiobook(raw.id)
                except MediaNotFoundError as err:
                    self.provider.report_skipped_sync_item(MediaType.AUDIOBOOK, raw.id, err)
                    continue
                seen_ids.add(raw.id)
                if raw.favorited_at:
                    item.date_added = parse_date(raw.favorited_at)
                yield item
        # 2. Audiobooks stored as favorite albums
        audiobook_ids = await self._get_audiobook_ids_in_albums()
        for ab_id in audiobook_ids:
            if ab_id in seen_ids:
                continue
            try:
                yield await self.get_audiobook(ab_id)
            except MediaNotFoundError as err:
                self.provider.report_skipped_sync_item(MediaType.AUDIOBOOK, ab_id, err)
                continue

    # -- Search --

    @use_cache(60 * 15)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on music provider."""
        self.logger.debug("search called with media_types=%s", media_types)
        need_albums = MediaType.ALBUM in media_types
        need_audiobooks = MediaType.AUDIOBOOK in media_types

        # Try with full limit first; on complexity error, retry with reduced limits.
        attempts = [limit, max(limit // 2, 5), 5]
        result = None
        for idx, attempt_limit in enumerate(attempts):
            try:
                result = await self.provider.gql_client.search(
                    query=search_query,
                    tracks_first=attempt_limit if MediaType.TRACK in media_types else 0,
                    albums_first=attempt_limit if (need_albums or need_audiobooks) else 0,
                    artists_first=attempt_limit if MediaType.ARTIST in media_types else 0,
                    playlists_first=attempt_limit if MediaType.PLAYLIST in media_types else 0,
                    livestreams_first=attempt_limit if MediaType.RADIO in media_types else 0,
                    podcasts_first=attempt_limit if MediaType.PODCAST in media_types else 0,
                )
                break
            except GraphQLClientGraphQLMultiError as err:
                if not _is_complexity_error(err):
                    raise
                if idx == len(attempts) - 1:
                    self.logger.warning("Search complexity exceeded even at minimum limit")
                    raise
                self.logger.debug(
                    "Search complexity exceeded at limit=%d, retrying with %d",
                    attempt_limit,
                    attempts[idx + 1],
                )
        search_results = SearchResults()
        if result is None:
            return search_results
        if MediaType.TRACK in media_types:
            search_results.tracks = [
                parse_track(self.provider, edge.node)
                for edge in result.results.tracks.edges
                if edge.node is not None
            ]
        if need_albums or need_audiobooks:
            album_nodes = [e.node for e in result.results.albums.edges if e.node is not None]
            if album_nodes and need_audiobooks:
                album_ids = [n.id for n in album_nodes]
                audiobook_ids = await self.provider.gql_client.check_audiobook_ids(album_ids)
                if need_albums:
                    search_results.albums = [
                        parse_album(self.provider, n)
                        for n in album_nodes
                        if n.id not in audiobook_ids
                    ]
                search_results.audiobooks = [
                    parse_audiobook_from_album(self.provider, n)
                    for n in album_nodes
                    if n.id in audiobook_ids
                ]
            elif need_albums:
                search_results.albums = [parse_album(self.provider, n) for n in album_nodes]
        if MediaType.ARTIST in media_types:
            search_results.artists = [
                parse_artist(self.provider, edge.node)
                for edge in result.results.artists.edges
                if edge.node is not None
            ]
        if MediaType.PLAYLIST in media_types:
            search_results.playlists = [
                parse_playlist(self.provider, edge.node)
                for edge in result.results.playlists.edges
                if edge.node is not None
            ]
        if MediaType.RADIO in media_types:
            search_results.radio = [
                parse_radio(self.provider, edge.node)
                for edge in result.results.livestreams.edges
                if edge.node is not None
            ]
        if MediaType.PODCAST in media_types:
            search_results.podcasts = [
                parse_podcast(self.provider, edge.node)
                for edge in result.results.podcasts.edges
                if edge.node is not None
            ]
        return search_results

    # -- Item getters --

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.startswith(PERSONAL_ARTIST_PREFIX):
            # Personal track artist — reconstruct from GW data
            song_id = prov_artist_id.removeprefix(PERSONAL_ARTIST_PREFIX)
            personal_songs = await self._get_personal_songs()
            for song in personal_songs:
                if str(song["SNG_ID"]) == song_id:
                    return Artist(
                        item_id=prov_artist_id,
                        provider=self.instance_id,
                        name=song.get("ART_NAME", ""),
                        provider_mappings={
                            ProviderMapping(
                                item_id=prov_artist_id,
                                provider_domain=self.domain,
                                provider_instance=self.instance_id,
                            )
                        },
                    )
            raise MediaNotFoundError(f"Personal artist {prov_artist_id} not found")
        result = await self.provider.gql_client.get_artist(artist_id=prov_artist_id)
        if result is None:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found on Deezer")
        item = parse_artist(self.provider, result)
        apply_web_url(item, result)
        return item

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        if prov_album_id.startswith(PERSONAL_ALBUM_PREFIX):
            # Personal track album — reconstruct from GW data
            song_id = prov_album_id.removeprefix(PERSONAL_ALBUM_PREFIX)
            personal_songs = await self._get_personal_songs()
            for song in personal_songs:
                if str(song["SNG_ID"]) == song_id:
                    art_name = song.get("ART_NAME", "")
                    personal_art_id = f"{PERSONAL_ARTIST_PREFIX}{song_id}"
                    artists: UniqueList[Artist | ItemMapping] = UniqueList()
                    if art_name:
                        artists.append(
                            ItemMapping(
                                media_type=MediaType.ARTIST,
                                item_id=personal_art_id,
                                provider=self.instance_id,
                                name=art_name,
                            )
                        )
                    return Album(
                        item_id=prov_album_id,
                        provider=self.instance_id,
                        name=song.get("ALB_TITLE", ""),
                        artists=artists,
                        provider_mappings={
                            ProviderMapping(
                                item_id=prov_album_id,
                                provider_domain=self.domain,
                                provider_instance=self.instance_id,
                            )
                        },
                    )
            raise MediaNotFoundError(f"Personal album {prov_album_id} not found")
        result = await self.provider.gql_client.get_album(album_id=prov_album_id)
        if result is None:
            raise MediaNotFoundError(f"Album {prov_album_id} not found on Deezer")
        item = parse_album(self.provider, result)
        apply_web_url(item, result)
        return item

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        try:
            track_id_int = int(prov_track_id)
        except ValueError as err:
            raise MediaNotFoundError(f"Invalid Deezer track ID: {prov_track_id}") from err
        # Personal tracks (negative IDs) don't exist in the GQL API
        if track_id_int < 0:
            personal_songs = await self._get_personal_songs()
            for song in personal_songs:
                if str(song["SNG_ID"]) == prov_track_id:
                    return parse_gw_track(self.provider, song)
            raise MediaNotFoundError(f"Personal track {prov_track_id} not found")
        result = await self.provider.gql_client.get_track(track_id=prov_track_id)
        if result is None:
            raise MediaNotFoundError(f"Track {prov_track_id} not found on Deezer")
        return parse_track(self.provider, result)

    @use_cache(3600 * 24 * 30)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if virtual := await self.provider.browse_manager.get_virtual_playlist(prov_playlist_id):
            return virtual
        result = await self.provider.gql_client.get_playlist(playlist_id=prov_playlist_id)
        if result is None:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found on Deezer")
        is_editable = result.owner is not None and result.owner.id == self.provider.user_id
        return parse_playlist(self.provider, result, is_editable=is_editable)

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio/livestream details by id."""
        result = await self.provider.gql_client.get_livestream(livestream_id=prov_radio_id)
        if result is None:
            raise MediaNotFoundError(f"Radio {prov_radio_id} not found on Deezer")
        return parse_radio(self.provider, result)

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        result = await self.provider.gql_client.get_podcast(podcast_id=prov_podcast_id)
        if result is None:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found on Deezer")
        podcast = parse_podcast(self.provider, result)
        podcast.total_episodes = len(result.raw_episodes)
        return podcast

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        result = await self.provider.gql_client.get_podcast_episode(
            podcast_episode_id=prov_episode_id,
        )
        if result is None:
            raise MediaNotFoundError(f"Podcast episode {prov_episode_id} not found on Deezer")
        podcast_mapping = ItemMapping(
            media_type=MediaType.PODCAST,
            item_id=result.podcast.id,
            provider=self.instance_id,
            name=result.podcast.display_title,
        )
        podcast_image_url = (
            result.podcast.cover.urls[0]
            if result.podcast.cover and result.podcast.cover.urls
            else None
        )
        return parse_podcast_episode(self.provider, result, podcast_mapping, 0, podcast_image_url)

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        result = await self.provider.gql_client.get_audiobook(
            audiobook_id=prov_audiobook_id, chapters_first=AUDIOBOOK_CHAPTERS_PAGE_SIZE
        )
        if result is None:
            raise MediaNotFoundError(f"Audiobook {prov_audiobook_id} not found on Deezer")
        item = parse_audiobook(self.provider, result)
        if result.chapters.page_info.has_next_page:
            all_edges = await fetch_all_audiobook_chapter_edges(
                self.provider.gql_client,
                prov_audiobook_id,
                initial_edges=result.chapters.edges,
                initial_page_info=result.chapters.page_info,
            )
        else:
            all_edges = result.chapters.edges
        item.metadata.chapters = parse_audiobook_chapters(all_edges)
        return item

    # -- Content getters --

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks in an album."""
        if prov_album_id.startswith(PERSONAL_ALBUM_PREFIX):
            # Personal album has no real Deezer album page
            return []
        result = await self.provider.gql_client.get_album(album_id=prov_album_id)
        if result is None:
            return []
        all_edges = list(result.tracks.edges)
        while result.tracks.page_info.has_next_page:
            result = await self.provider.gql_client.get_album(
                album_id=prov_album_id,
                tracks_after=result.tracks.page_info.end_cursor,
            )
            if result is None:
                break
            all_edges.extend(result.tracks.edges)
        return [
            parse_track(self.provider, edge.node, position=idx)
            for idx, edge in enumerate(all_edges, 1)
            if edge.node is not None
        ]

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all episodes for a given podcast with current resume state."""
        episodes = await self._fetch_podcast_episodes(prov_podcast_id)
        if not episodes:
            return
        bookmarks = await fetch_all_bookmarks(self.provider.gql_client)
        for ep in episodes:
            ep.fully_played = False
            ep.resume_position_ms = 0
            if ep.item_id in bookmarks:
                ep.fully_played, ep.resume_position_ms = bookmarks[ep.item_id]
            yield ep

    @use_cache(3600)
    async def _fetch_podcast_episodes(self, prov_podcast_id: str) -> list[PodcastEpisode]:
        """Fetch all episodes for a podcast (cached 1h)."""
        # Two-layer caching strategy:
        # - Outer (this decorator, 1h): avoids repeated cache lookups during
        #   rapid navigation (e.g., user browsing back and forth between podcasts).
        # - Inner (per-episode, 30 days): prevents re-fetching episode details
        #   that rarely change. When the outer cache expires, only genuinely new
        #   episodes require an API call.
        result = await self.provider.gql_client.get_podcast(
            podcast_id=prov_podcast_id, episodes_first=0
        )
        if result is None:
            return []
        podcast_mapping = ItemMapping(
            media_type=MediaType.PODCAST,
            item_id=result.id,
            provider=self.instance_id,
            name=result.display_title,
        )
        podcast_image_url: str | None = None
        if result.cover and result.cover.urls:
            podcast_image_url = result.cover.urls[0]
        episode_ids = result.raw_episodes
        if not episode_ids:
            return []

        cache = self.mass.cache
        episode_cache_ttl = 3600 * 24 * 30  # 30 days

        # Resolve cached vs uncached episode IDs
        cached_episodes: dict[str, PodcastEpisode] = {}
        uncached_ids: list[str] = []
        for eid in episode_ids:
            cache_key = f"podcast_episode.{eid}"
            cached = await cache.get(cache_key, provider=self.instance_id)
            if cached is not None:
                cached_episodes[eid] = PodcastEpisode.from_dict(cached)
            else:
                uncached_ids.append(eid)

        # Batch-fetch only uncached episodes
        batch_size = 50
        for i in range(0, len(uncached_ids), batch_size):
            batch = uncached_ids[i : i + batch_size]
            fetched = await self.provider.gql_client.get_podcast_episodes_by_ids(ids=batch)
            for ep in fetched:
                if ep is not None:
                    parsed = parse_podcast_episode(
                        self.provider, ep, podcast_mapping, 0, podcast_image_url
                    )
                    cached_episodes[ep.id] = parsed
                    self.mass.create_task(
                        cache.set(
                            key=f"podcast_episode.{ep.id}",
                            data=parsed.to_dict(),
                            expiration=episode_cache_ttl,
                            provider=self.instance_id,
                        )
                    )

        # rank on the publication date, so the order the API returns the episodes in does
        # not decide the ordering
        episodes = [cached_episodes[eid] for eid in episode_ids if eid in cached_episodes]
        positions = rank_episodes_by_date([ep.metadata.release_date for ep in episodes])
        for episode, position in zip(episodes, positions, strict=True):
            episode.position = position
        return episodes

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        if prov_artist_id.startswith(PERSONAL_ARTIST_PREFIX):
            # Personal artist has no real Deezer artist page
            return []
        result = await self.provider.gql_client.get_artist(artist_id=prov_artist_id)
        if result is None:
            return []
        all_edges = list(result.albums.edges)
        while result.albums.page_info.has_next_page:
            result = await self.provider.gql_client.get_artist(
                artist_id=prov_artist_id,
                albums_after=result.albums.page_info.end_cursor,
            )
            if result is None:
                break
            all_edges.extend(result.albums.edges)
        return [
            parse_album(self.provider, edge.node) for edge in all_edges if edge.node is not None
        ]

    async def get_artist_topalbums(self, prov_artist_id: str) -> list[Album]:
        """Get top albums of an artist, ranked by popularity."""
        albums = await self.get_artist_albums(prov_artist_id)
        return sorted(albums, key=lambda album: album.metadata.popularity or 0, reverse=True)

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks of an artist."""
        if prov_artist_id.startswith(PERSONAL_ARTIST_PREFIX):
            # Personal artist has no real Deezer artist page
            return []
        result = await self.provider.gql_client.get_artist(artist_id=prov_artist_id)
        if result is None or result.top_tracks is None:
            return []
        all_edges = list(result.top_tracks.edges)
        while result.top_tracks is not None and result.top_tracks.page_info.has_next_page:
            result = await self.provider.gql_client.get_artist(
                artist_id=prov_artist_id,
                top_tracks_after=result.top_tracks.page_info.end_cursor,
            )
            if result is None or result.top_tracks is None:
                break
            all_edges.extend(result.top_tracks.edges)
        return [
            parse_track(self.provider, edge.node) for edge in all_edges if edge.node is not None
        ]

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        result = await self.provider.gql_client.get_similar_tracks(track_id=prov_track_id, nb=limit)
        if result is None:
            return []
        return [parse_track(self.provider, t) for t in result.recommended_tracks if t is not None]

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Retrieve a list of artists similar to the provided artist."""
        if prov_artist_id.startswith(PERSONAL_ARTIST_PREFIX):
            return []
        result = await self.provider.gql_client.get_similar_artists(
            artist_id=prov_artist_id, first=limit
        )
        if result is None or result.related_artist is None:
            return []
        return [
            parse_artist(self.provider, edge.node)
            for edge in result.related_artist.edges
            if edge.node is not None
        ]

    # -- Library mutations --

    async def library_add(self, item: MediaItemType) -> bool:
        """Add an item to the provider's library/favorites."""
        if item.media_type == MediaType.ARTIST:
            await self.provider.gql_client.add_artist_to_favorite(artist_id=item.item_id)
        elif item.media_type == MediaType.ALBUM:
            await self.provider.gql_client.add_album_to_favorite(album_id=item.item_id)
        elif item.media_type == MediaType.TRACK:
            await self.provider.gql_client.add_track_to_favorite(track_id=item.item_id)
        elif item.media_type == MediaType.PLAYLIST:
            await self.provider.gql_client.add_playlist_to_favorite(playlist_id=item.item_id)
        elif item.media_type == MediaType.PODCAST:
            await self.provider.gql_client.add_podcast_to_favorite(podcast_id=item.item_id)
        elif item.media_type == MediaType.AUDIOBOOK:
            await self.provider.gql_client.add_album_to_favorite(album_id=item.item_id)
        else:
            raise UnsupportedFeaturedException(
                f"Unsupported media type for library_add: {item.media_type}"
            )
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item from the provider's library/favorites."""
        if media_type == MediaType.ARTIST:
            await self.provider.gql_client.remove_artist_from_favorite(artist_id=prov_item_id)
        elif media_type == MediaType.ALBUM:
            await self.provider.gql_client.remove_album_from_favorite(album_id=prov_item_id)
        elif media_type == MediaType.TRACK:
            await self.provider.gql_client.remove_track_from_favorite(track_id=prov_item_id)
        elif media_type == MediaType.PLAYLIST:
            await self.provider.gql_client.remove_playlist_from_favorite(playlist_id=prov_item_id)
        elif media_type == MediaType.PODCAST:
            await self.provider.gql_client.remove_podcast_from_favorite(podcast_id=prov_item_id)
        elif media_type == MediaType.AUDIOBOOK:
            await self.provider.gql_client.remove_album_from_favorite(album_id=prov_item_id)
        else:
            raise UnsupportedFeaturedException(
                f"Unsupported media type for library_remove: {media_type}"
            )
        return True

    # -- Playlist CRUD --

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        await self.provider.gql_client.add_tracks_to_playlist(
            playlist_id=prov_playlist_id, track_ids=prov_track_ids
        )
        await self.provider.browse_manager.invalidate_playlist_cache(prov_playlist_id)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        playlist_tracks = await self.provider.browse_manager.get_playlist_tracks(
            prov_playlist_id, 0
        )
        track_ids = [
            track.item_id for track in playlist_tracks if track.position in positions_to_remove
        ]
        if track_ids:
            await self.provider.gql_client.remove_tracks_from_playlist(
                playlist_id=prov_playlist_id, track_ids=track_ids
            )
            await self.provider.browse_manager.invalidate_playlist_cache(prov_playlist_id)

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        result = await self.provider.gql_client.create_playlist(
            title=name, is_private=False, is_collaborative=False
        )
        if result.playlist is None:
            msg = f"Failed to create playlist '{name}' on Deezer"
            raise MediaNotFoundError(msg)
        playlist = await self.provider.gql_client.get_playlist(playlist_id=result.playlist.id)
        if playlist is None:
            msg = f"Created playlist {result.playlist.id} not found on Deezer"
            raise MediaNotFoundError(msg)
        return parse_playlist(self.provider, playlist, is_editable=True)
