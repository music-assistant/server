"""Deezer music provider support for MusicAssistant."""

import contextlib
import hashlib
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from math import ceil
from typing import Any, Protocol

import httpx
from aiohttp import ClientTimeout
from Crypto.Cipher import Blowfish
from deezer_python_gql import DeezerGQLClient
from deezer_python_gql.generated.enums import AlbumType as DeezerAlbumType
from deezer_python_gql.generated.enums import (
    AudiobookContributorRoles,
    MusicTogetherSuggestedTracklistMoodInput,
)
from deezer_python_gql.generated.fragments import (
    AlbumFields,
    ArtistFields,
    AudiobookFields,
    LivestreamFields,
    PlaylistFields,
    PodcastEpisodeFields,
    PodcastFields,
    TrackFields,
)
from deezer_python_gql.generated.get_made_for_me import (
    GetMadeForMeMeMadeForMeEdgesNodeSmartTracklist,
)
from deezer_python_gql.generated.get_recently_played import (
    GetRecentlyPlayedMeRecentlyPlayedEdges,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeAlbum,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeArtist,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlow,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlowConfig,
    GetRecentlyPlayedMeRecentlyPlayedEdgesNodePlaylist,
)
from deezer_python_gql.generated.get_recommendations import GetRecommendationsMe
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.provider import ProviderManifest
from music_assistant_models.streamdetails import StreamDetails

from music_assistant import MusicAssistant
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.util import infer_album_type, parse_title_and_version
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider

from .gw_client import GWClient

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.LYRICS,
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
}

CONF_ARL_TOKEN = "arl_token"

# Virtual playlist IDs for dynamic Deezer content
FLOW_PLAYLIST_ID = "flow"
RECOMMENDED_TRACKS_PLAYLIST_ID = "recommended_tracks"
TOP_CHARTS_PLAYLIST_ID = "top_charts"
FLOW_CONFIG_PREFIX = "flow_config_"
SMART_TRACKLIST_PREFIX = "smart_tracklist_"
USER_TOP_TRACKS_PLAYLIST_ID = "user_top_tracks"
SHAKER_PREFIX = "shaker_"
SHAKER_CURATED_PREFIX = "shaker_curated_"
SHAKER_MIX_COVER = "https://cdn-assets.dzcdn.net/shaker/_next/static/media/group_mix.d986951b.svg"
# TODO: Re-enable mood mixes when Deezer's mood API is working again
# SHAKER_MOODS = {
#     "_chill": MusicTogetherSuggestedTracklistMoodInput.CHILL,
#     "_focus": MusicTogetherSuggestedTracklistMoodInput.FOCUS,
#     "_party": MusicTogetherSuggestedTracklistMoodInput.PARTY,
# }

# Page size for cursor-based pagination
FAVORITES_PAGE_SIZE = 50

# Deezer CDN image URL pattern
DEEZER_CDN_IMAGE = "https://e-cdns-images.dzcdn.net/images"
AUDIOBOOKS_CHANNEL = "channels/audiobooks"


class _FlowConfigIcon(Protocol):
    urls: list[str]


class _FlowConfigVisuals(Protocol):
    hardware_square_icon: _FlowConfigIcon | None


class _HasVisuals(Protocol):
    visuals: _FlowConfigVisuals


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_ARL_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="ARL token",
            required=True,
            description="Your Deezer ARL cookie token. "
            "See https://www.dumpmedia.com/deezplus/deezer-arl.html",
            value=values.get(CONF_ARL_TOKEN) if values else None,
        ),
    )


class DeezerProvider(MusicProvider):
    """Deezer provider support."""

    gql_client: DeezerGQLClient
    gw_client: GWClient
    _user_id: str
    _http_client: httpx.AsyncClient
    _browse_slug_cache: dict[str, str]  # slug → actual ID/path

    async def handle_async_init(self) -> None:
        """Handle async init of the Deezer provider."""
        arl_token = str(self.config.get_value(CONF_ARL_TOKEN))

        self._http_client = httpx.AsyncClient()
        self.gql_client = DeezerGQLClient(arl=arl_token, http_client=self._http_client)
        me = await self.gql_client.get_me()
        if me is None:
            msg = "Failed to authenticate with Deezer. Please check your ARL token."
            raise LoginFailed(msg)
        self._user_id = me.id

        self.gw_client = GWClient(self.mass.http_session, arl_token)
        await self.gw_client.setup()
        self._browse_slug_cache = {}

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self._http_client.aclose()

    # -- Library retrieval (cursor-paginated) --

    async def _iter_paged(
        self,
        fetch: Callable[..., Awaitable[Any]],
        extract: Callable[..., Any],
    ) -> AsyncGenerator[Any, None]:
        """Iterate a cursor-paginated connection, yielding edges."""
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

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Deezer."""
        async for edge in self._iter_paged(
            self.gql_client.get_favorite_artists,
            lambda r: r.user_favorites.artists,
        ):
            item = self._parse_artist(edge.node)
            if edge.favorited_at:
                item.date_added = self._parse_date(edge.favorited_at)
            yield item

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Deezer."""
        async for edge in self._iter_paged(
            self.gql_client.get_favorite_albums,
            lambda r: r.user_favorites.albums,
        ):
            item = self._parse_album(edge.node)
            if edge.favorited_at:
                item.date_added = self._parse_date(edge.favorited_at)
            yield item

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from Deezer."""
        # User-owned playlists first
        seen_ids: set[str] = set()
        async for edge in self._iter_paged(
            self.gql_client.get_user_playlists,
            lambda r: r.playlists,
        ):
            seen_ids.add(edge.node.id)
            yield self._parse_playlist(edge.node, is_editable=True)
        # Favorited playlists (other users' playlists)
        async for edge in self._iter_paged(
            self.gql_client.get_favorite_playlists,
            lambda r: r.user_favorites.playlists,
        ):
            if edge.node.id in seen_ids:
                continue
            item = self._parse_playlist(edge.node)
            if edge.favorited_at:
                item.date_added = self._parse_date(edge.favorited_at)
            yield item

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve all library tracks from Deezer."""
        async for edge in self._iter_paged(
            self.gql_client.get_favorite_tracks,
            lambda r: r.user_favorites.tracks,
        ):
            item = self._parse_track(edge.node)
            if edge.favorited_at:
                item.date_added = self._parse_date(edge.favorited_at)
            yield item

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library radio stations from Deezer.

        Deezer livestreams have no favorites list, so this is intentionally empty.
        Radio stations are discoverable via search.
        """
        yield  # type: ignore[misc]

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from Deezer."""
        async for edge in self._iter_paged(
            self.gql_client.get_favorite_podcasts,
            lambda r: r.user_favorites.podcasts,
        ):
            item = self._parse_podcast(edge.node)
            if edge.favorited_at:
                item.date_added = self._parse_date(edge.favorited_at)
            yield item

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Retrieve library/subscribed audiobooks from Deezer."""
        result = await self.gql_client.get_favorite_audiobooks()
        if result is None or result.favorites.raw_audiobooks is None:
            return
        for raw in result.favorites.raw_audiobooks:
            audiobook_data = await self.gql_client.get_audiobook(
                audiobook_id=raw.id, chapters_first=200
            )
            if audiobook_data is None:
                continue
            item = self._parse_audiobook(audiobook_data)
            item.metadata.chapters = self._parse_audiobook_chapters(audiobook_data.chapters.edges)
            if raw.favorited_at:
                item.date_added = self._parse_date(raw.favorited_at)
            yield item

    # -- Search --

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on music provider."""
        self.logger.debug("search called with media_types=%s", media_types)
        # When audiobooks are requested, also fetch albums so we can split them
        need_albums = MediaType.ALBUM in media_types
        need_audiobooks = MediaType.AUDIOBOOK in media_types
        albums_limit = limit if (need_albums or need_audiobooks) else 0

        result = await self.gql_client.search(
            query=search_query,
            tracks_first=limit if MediaType.TRACK in media_types else 0,
            albums_first=albums_limit,
            artists_first=limit if MediaType.ARTIST in media_types else 0,
            playlists_first=limit if MediaType.PLAYLIST in media_types else 0,
            livestreams_first=limit if MediaType.RADIO in media_types else 0,
            podcasts_first=limit if MediaType.PODCAST in media_types else 0,
        )
        search_results = SearchResults()
        if result is None:
            return search_results
        if MediaType.TRACK in media_types:
            search_results.tracks = [
                self._parse_track(edge.node)
                for edge in result.results.tracks.edges
                if edge.node is not None
            ]
        if need_albums or need_audiobooks:
            album_edges = [e for e in result.results.albums.edges if e.node is not None]
            if album_edges and need_audiobooks:
                album_ids = [e.node.id for e in album_edges]
                audiobook_ids = await self.gql_client.check_audiobook_ids(album_ids)
                if need_albums:
                    search_results.albums = [
                        self._parse_album(e.node)
                        for e in album_edges
                        if e.node.id not in audiobook_ids
                    ]
                search_results.audiobooks = [
                    self._parse_audiobook_from_album(e.node)
                    for e in album_edges
                    if e.node.id in audiobook_ids
                ]
            elif need_albums:
                search_results.albums = [self._parse_album(e.node) for e in album_edges]
        if MediaType.ARTIST in media_types:
            search_results.artists = [
                self._parse_artist(edge.node)
                for edge in result.results.artists.edges
                if edge.node is not None
            ]
        if MediaType.PLAYLIST in media_types:
            search_results.playlists = [
                self._parse_playlist(edge.node)
                for edge in result.results.playlists.edges
                if edge.node is not None
            ]
        if MediaType.RADIO in media_types:
            search_results.radio = [
                self._parse_radio(edge.node)
                for edge in result.results.livestreams.edges
                if edge.node is not None
            ]
        if MediaType.PODCAST in media_types:
            search_results.podcasts = [
                self._parse_podcast(edge.node)
                for edge in result.results.podcasts.edges
                if edge.node is not None
            ]
        return search_results

    # -- Item retrieval --

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        result = await self.gql_client.get_artist(artist_id=prov_artist_id)
        if result is None:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found on Deezer")
        item = self._parse_artist(result)
        self._apply_web_url(item, result)
        return item

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        result = await self.gql_client.get_album(album_id=prov_album_id)
        if result is None:
            raise MediaNotFoundError(f"Album {prov_album_id} not found on Deezer")
        item = self._parse_album(result)
        self._apply_web_url(item, result)
        return item

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        result = await self.gql_client.get_track(track_id=prov_track_id)
        if result is None:
            raise MediaNotFoundError(f"Track {prov_track_id} not found on Deezer")
        return self._parse_track(result)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if virtual := await self._get_virtual_playlist(prov_playlist_id):
            return virtual
        result = await self.gql_client.get_playlist(playlist_id=prov_playlist_id)
        if result is None:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found on Deezer")
        is_editable = result.owner is not None and result.owner.id == self._user_id
        return self._parse_playlist(result, is_editable=is_editable)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio/livestream details by id."""
        result = await self.gql_client.get_livestream(livestream_id=prov_radio_id)
        if result is None:
            raise MediaNotFoundError(f"Radio {prov_radio_id} not found on Deezer")
        return self._parse_radio(result)

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        result = await self.gql_client.get_podcast(podcast_id=prov_podcast_id)
        if result is None:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found on Deezer")
        podcast = self._parse_podcast(result)
        if hasattr(result, "raw_episodes"):
            podcast.total_episodes = len(result.raw_episodes)
        return podcast

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        result = await self.gql_client.get_podcast_episode(
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
        return self._parse_podcast_episode(result, podcast_mapping, 0, podcast_image_url)

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all episodes for a given podcast.

        Episode metadata is cached for 3 hours. Bookmark/resume state is
        applied on top of the cached data so it stays fresh.
        """
        episodes = await self._fetch_podcast_episodes(prov_podcast_id)
        if not episodes:
            return
        # Apply fresh bookmark/resume state on top of cached episodes
        bookmarks: dict[str, tuple[bool, int]] = {}
        bookmark_result = await self.gql_client.get_podcast_episode_bookmarks(first=100)
        if bookmark_result:
            for edge in bookmark_result.podcast_episode_bookmarks.edges:
                if edge.node is not None:
                    bookmarks[edge.node.episode.id] = (
                        edge.node.is_played,
                        edge.node.position * 1000,
                    )
        for ep in episodes:
            if ep.item_id in bookmarks:
                ep.fully_played, ep.resume_position_ms = bookmarks[ep.item_id]
            yield ep

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def _fetch_podcast_episodes(self, prov_podcast_id: str) -> list[PodcastEpisode]:
        """Fetch and cache all episodes for a podcast.

        Uses rawEpisodes (full ID list) + batch fetch because Deezer's cursor
        pagination on episodes is broken when using an order other than NONE.
        """
        result = await self.gql_client.get_podcast(podcast_id=prov_podcast_id, episodes_first=0)
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
        episodes: list[PodcastEpisode] = []
        batch_size = 50
        position = 0
        for i in range(0, len(episode_ids), batch_size):
            batch = episode_ids[i : i + batch_size]
            fetched = await self.gql_client.get_podcast_episodes_by_ids(ids=batch)
            for episode in fetched:
                if episode is not None:
                    position += 1
                    episodes.append(
                        self._parse_podcast_episode(
                            episode, podcast_mapping, position, podcast_image_url
                        )
                    )
        return episodes

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        result = await self.gql_client.get_audiobook(
            audiobook_id=prov_audiobook_id, chapters_first=200
        )
        if result is None:
            raise MediaNotFoundError(f"Audiobook {prov_audiobook_id} not found on Deezer")
        item = self._parse_audiobook(result)
        item.metadata.chapters = self._parse_audiobook_chapters(result.chapters.edges)
        return item

    @use_cache(3600 * 24 * 30)  # Cache for 30 days
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks in an album."""
        result = await self.gql_client.get_album(album_id=prov_album_id)
        if result is None:
            return []
        return [
            self._parse_track(edge.node, position=idx)
            for idx, edge in enumerate(result.tracks.edges, 1)
            if edge.node is not None
        ]

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        if page > 0:
            return []
        if prov_playlist_id == FLOW_PLAYLIST_ID:
            return await self._get_flow_tracks()
        if prov_playlist_id == RECOMMENDED_TRACKS_PLAYLIST_ID:
            return await self._get_recommended_tracks()
        if prov_playlist_id == TOP_CHARTS_PLAYLIST_ID:
            return await self._get_chart_tracks()
        if prov_playlist_id == USER_TOP_TRACKS_PLAYLIST_ID:
            return await self._get_user_chart_tracks()
        if prov_playlist_id.startswith(FLOW_CONFIG_PREFIX):
            return await self._get_flow_config_tracks(
                prov_playlist_id.removeprefix(FLOW_CONFIG_PREFIX)
            )
        if prov_playlist_id.startswith(SMART_TRACKLIST_PREFIX):
            tracklist_id = prov_playlist_id.removeprefix(SMART_TRACKLIST_PREFIX)
            return await self._get_smart_tracklist_tracks(tracklist_id)
        if prov_playlist_id.startswith(SHAKER_CURATED_PREFIX):
            group_id = prov_playlist_id.removeprefix(SHAKER_CURATED_PREFIX)
            return await self._get_shaker_curated_tracks(group_id)
        if prov_playlist_id.startswith(SHAKER_PREFIX):
            shaker_id = prov_playlist_id.removeprefix(SHAKER_PREFIX)
            return await self._get_shaker_tracks(shaker_id)
        return await self._get_regular_playlist_tracks(prov_playlist_id)

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        result = await self.gql_client.get_artist(artist_id=prov_artist_id)
        if result is None:
            return []
        return [
            self._parse_album(edge.node) for edge in result.albums.edges if edge.node is not None
        ]

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top 50 tracks of an artist."""
        result = await self.gql_client.get_artist(artist_id=prov_artist_id)
        if result is None or result.top_tracks is None:
            return []
        return [
            self._parse_track(edge.node)
            for edge in result.top_tracks.edges
            if edge.node is not None
        ]

    # -- Library add/remove (favorites) --

    async def library_add(self, item: MediaItemType) -> bool:
        """Add an item to the provider's library/favorites."""
        if item.media_type == MediaType.ARTIST:
            await self.gql_client.add_artist_to_favorite(artist_id=item.item_id)
        elif item.media_type == MediaType.ALBUM:
            await self.gql_client.add_album_to_favorite(album_id=item.item_id)
        elif item.media_type == MediaType.TRACK:
            await self.gql_client.add_track_to_favorite(track_id=item.item_id)
        elif item.media_type == MediaType.PLAYLIST:
            await self.gql_client.add_playlist_to_favorite(playlist_id=item.item_id)
        elif item.media_type == MediaType.PODCAST:
            await self.gql_client.add_podcast_to_favorite(podcast_id=item.item_id)
        elif item.media_type == MediaType.AUDIOBOOK:
            await self.gql_client.add_audiobook_to_favorite(audiobook_id=item.item_id)
        else:
            raise NotImplementedError
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item from the provider's library/favorites."""
        if media_type == MediaType.ARTIST:
            await self.gql_client.remove_artist_from_favorite(artist_id=prov_item_id)
        elif media_type == MediaType.ALBUM:
            await self.gql_client.remove_album_from_favorite(album_id=prov_item_id)
        elif media_type == MediaType.TRACK:
            await self.gql_client.remove_track_from_favorite(track_id=prov_item_id)
        elif media_type == MediaType.PLAYLIST:
            await self.gql_client.remove_playlist_from_favorite(playlist_id=prov_item_id)
        elif media_type == MediaType.PODCAST:
            await self.gql_client.remove_podcast_from_favorite(podcast_id=prov_item_id)
        elif media_type == MediaType.AUDIOBOOK:
            await self.gql_client.remove_audiobook_from_favorite(audiobook_id=prov_item_id)
        else:
            raise NotImplementedError
        return True

    # -- Playlist CRUD --

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        await self.gql_client.add_tracks_to_playlist(
            playlist_id=prov_playlist_id, track_ids=prov_track_ids
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        playlist_tracks = await self.get_playlist_tracks(prov_playlist_id, 0)
        track_ids = [
            track.item_id for track in playlist_tracks if track.position in positions_to_remove
        ]
        if track_ids:
            await self.gql_client.remove_tracks_from_playlist(
                playlist_id=prov_playlist_id, track_ids=track_ids
            )

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        result = await self.gql_client.create_playlist(
            title=name, is_private=False, is_collaborative=False
        )
        if result.playlist is None:
            msg = f"Failed to create playlist '{name}' on Deezer"
            raise MediaNotFoundError(msg)
        playlist = await self.gql_client.get_playlist(playlist_id=result.playlist.id)
        if playlist is None:
            msg = f"Created playlist {result.playlist.id} not found on Deezer"
            raise MediaNotFoundError(msg)
        return self._parse_playlist(playlist, is_editable=True)

    # -- Browse --

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse Deezer content.

        Custom folders: Made For You, Explore, Recently Played.
        Standard paths (artists, albums, tracks, playlists, recommendations) delegated to base.
        """
        path_parts = path.split("://")[1].split("/") if "://" in path else []
        subpath = path_parts[0] if path_parts else None
        sub_subpath = path_parts[1] if len(path_parts) > 1 else None

        if subpath == "Made For You":
            if sub_subpath in ("Moods", "Genres"):
                return await self._browse_flow_configs(sub_subpath.lower())
            if sub_subpath == "Your Top Artists":
                return await self._browse_user_charts_category("your_top_artists")
            if sub_subpath == "Your Top Albums":
                return await self._browse_user_charts_category("your_top_albums")
            return await self._browse_made_for_me(path)

        if subpath == "Explore":
            if sub_subpath:
                return await self._browse_explore_category(sub_subpath)
            return await self._browse_explore_root(path)

        if subpath == "Recently Played":
            return await self._get_recently_played_items()

        if subpath == "Shaker":
            if sub_subpath:
                group_id = self._browse_slug_cache.get(f"shaker/{sub_subpath}", sub_subpath)
                return await self._browse_shaker_group(group_id)
            return await self._browse_shaker_root(path)

        if subpath == "Discover Audiobooks":
            if sub_subpath:
                page_path = self._browse_slug_cache.get(
                    f"audiobooks/{sub_subpath}", f"channels/{sub_subpath}"
                )
                return await self._browse_audiobooks_page(page_path)
            return await self._browse_audiobooks_root(path)

        if not subpath:
            # Root: add custom folders alongside standard ones
            # Filter out the Recommendations folder — our custom folders cover that content
            base_items = [
                item
                for item in await super().browse(path)
                if not (isinstance(item, BrowseFolder) and item.item_id == "recommendations")
            ]
            base = path if path.endswith("//") else path.rstrip("/") + "/"
            base_items.extend(
                [
                    BrowseFolder(
                        item_id="made_for_me",
                        provider=self.instance_id,
                        path=f"{base}Made For You",
                        name="Made For You",
                    ),
                    BrowseFolder(
                        item_id="explore",
                        provider=self.instance_id,
                        path=f"{base}Explore",
                        name="Explore",
                    ),
                    BrowseFolder(
                        item_id="recently_played",
                        provider=self.instance_id,
                        path=f"{base}Recently Played",
                        name="Recently Played",
                    ),
                    BrowseFolder(
                        item_id="shaker",
                        provider=self.instance_id,
                        path=f"{base}Shaker",
                        name="Shaker",
                    ),
                    BrowseFolder(
                        item_id="discover_audiobooks",
                        provider=self.instance_id,
                        path=f"{base}Discover Audiobooks",
                        name="Discover Audiobooks",
                    ),
                ]
            )
            return base_items

        # Standard paths handled by base class
        return list(await super().browse(path))

    # -- Browse helpers --

    async def _browse_made_for_me(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Return Made For You sub-items: Moods, Genres, Top stats, SmartTracklists."""
        base = path if path.endswith("/") else path + "/"
        items: list[MediaItemType | BrowseFolder] = [
            BrowseFolder(
                item_id="moods",
                provider=self.instance_id,
                path=f"{base}Moods",
                name="Moods",
            ),
            BrowseFolder(
                item_id="genres",
                provider=self.instance_id,
                path=f"{base}Genres",
                name="Genres",
            ),
            self._create_virtual_playlist(USER_TOP_TRACKS_PLAYLIST_ID, "Your Top Tracks"),
            self._create_virtual_playlist(RECOMMENDED_TRACKS_PLAYLIST_ID, "Hot Tracks"),
            BrowseFolder(
                item_id="your_top_artists",
                provider=self.instance_id,
                path=f"{base}Your Top Artists",
                name="Your Top Artists",
            ),
            BrowseFolder(
                item_id="your_top_albums",
                provider=self.instance_id,
                path=f"{base}Your Top Albums",
                name="Your Top Albums",
            ),
        ]
        items.extend(await self._get_smart_tracklist_playlists())
        return items

    def _flow_configs_to_playlists(self, edges: Sequence[Any]) -> list[Playlist]:
        """Convert FlowConfig edges to virtual playlists."""
        return [
            self._create_virtual_playlist(
                f"{FLOW_CONFIG_PREFIX}{edge.node.id}",
                f"Flow: {edge.node.title}",
                image_url=self._get_flow_config_image(edge.node),
            )
            for edge in edges
            if edge.node is not None
        ]

    async def _browse_flow_configs(self, category: str) -> list[Playlist]:
        """Fetch mood or genre flow configs and return as virtual playlists.

        :param category: Either "moods" or "genres".
        """
        is_moods = category == "moods"
        flow_configs = await self.gql_client.get_flow_configs(
            moods_first=50 if is_moods else 0,
            genres_first=0 if is_moods else 50,
        )
        if not flow_configs or not flow_configs.flow_configs:
            return []
        edges = (
            flow_configs.flow_configs.moods.edges
            if is_moods
            else flow_configs.flow_configs.genres.edges
        )
        return self._flow_configs_to_playlists(edges)

    async def _browse_all_flows(self) -> list[Playlist]:
        """Fetch all available Deezer flows via search and return as virtual playlists."""
        result = await self.gql_client.search_flows(query="flow", first=100)
        if not result:
            return []
        return self._flow_configs_to_playlists(result.results.flow_configs.edges)

    async def _browse_explore_root(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Return Explore section: charts, top content, all flows."""
        base = path if path.endswith("/") else path + "/"
        charts_cover = None
        charts = await self.gql_client.get_charts(tracks_first=1)
        if charts and charts.country and charts.country.tracks:
            for edge in charts.country.tracks.edges:
                if edge.node and edge.node.album and edge.node.album.cover:
                    if edge.node.album.cover.urls:
                        charts_cover = edge.node.album.cover.urls[0]
                    break
        return [
            self._create_virtual_playlist(
                TOP_CHARTS_PLAYLIST_ID, "Top Tracks", image_url=charts_cover
            ),
            BrowseFolder(
                item_id="top_albums",
                provider=self.instance_id,
                path=f"{base}Top Albums",
                name="Top Albums",
            ),
            BrowseFolder(
                item_id="top_artists",
                provider=self.instance_id,
                path=f"{base}Top Artists",
                name="Top Artists",
            ),
            BrowseFolder(
                item_id="top_playlists",
                provider=self.instance_id,
                path=f"{base}Top Playlists",
                name="Top Playlists",
            ),
            BrowseFolder(
                item_id="all_flows",
                provider=self.instance_id,
                path=f"{base}All Flows",
                name="All Flows",
            ),
        ]

    async def _browse_explore_category(self, category: str) -> list[MediaItemType]:
        """Fetch items for an Explore sub-category."""
        if category == "All Flows":
            return list(await self._browse_all_flows())
        items: list[MediaItemType] = []
        if category in ("Top Albums", "Top Artists", "Top Playlists"):
            charts = await self.gql_client.get_charts(tracks_first=0)
            if not charts or not charts.country:
                return []
            country = charts.country
            if category == "Top Albums" and country.albums:
                for edge in country.albums.edges:
                    if edge.node is not None:
                        items.append(self._parse_album(edge.node))
            elif category == "Top Artists" and country.artists:
                for edge in country.artists.edges:
                    if edge.node is not None:
                        items.append(self._parse_artist(edge.node))
            elif category == "Top Playlists" and country.playlists:
                for edge in country.playlists.edges:
                    if edge.node is not None:
                        items.append(self._parse_playlist(edge.node))
        return items

    async def _browse_user_charts_category(self, category: str) -> list[MediaItemType]:
        """Fetch user chart items (top artists/albums)."""
        result = await self.gql_client.get_user_charts()
        if not result:
            return []
        charts = result.charts
        items: list[MediaItemType] = []
        if category == "your_top_artists" and charts.artists:
            for edge in charts.artists.edges:
                if edge.node is not None:
                    items.append(self._parse_artist(edge.node))
        elif category == "your_top_albums" and charts.albums:
            for edge in charts.albums.edges:
                if edge.node is not None:
                    items.append(self._parse_album(edge.node))
        return items

    async def _browse_shaker_root(self, path: str) -> list[BrowseFolder]:
        """Return Shaker (Music Together) groups as browse folders."""
        result = await self.gql_client.get_music_together_groups(first=50)
        if not result:
            return []
        base = path if path.endswith("/") else path + "/"
        folders: list[BrowseFolder] = []
        for edge in result.music_together_groups.edges:
            if edge.node is None:
                continue
            group = edge.node
            members = group.estimated_members_count
            name = f"{group.name} ({members} member{'s' if members != 1 else ''})"
            path_name = group.name.replace("/", "-")
            self._browse_slug_cache[f"shaker/{path_name}"] = group.id
            folders.append(
                BrowseFolder(
                    item_id=f"shaker_{group.id}",
                    provider=self.instance_id,
                    path=f"{base}{path_name}",
                    name=name,
                )
            )
        return folders

    async def _browse_shaker_group(self, group_id: str) -> list[MediaItemType]:
        """Return playlists for a Shaker group: mix + curated tracklist."""
        group = await self.gql_client.get_music_together_group(
            group_id=group_id,
            mood=MusicTogetherSuggestedTracklistMoodInput.NONE,
            tracks_first=1,
        )
        if group is None:
            return []
        items: list[MediaItemType] = []
        if group.suggested_tracklist and group.suggested_tracklist.tracklist:
            items.append(
                self._create_virtual_playlist(
                    f"{SHAKER_PREFIX}{group_id}",
                    f"{group.name} - Mix",
                    image_url=SHAKER_MIX_COVER,
                )
            )
        if group.curated_tracklist:
            cover_url = (
                group.curated_tracklist.picture.urls[0]
                if group.curated_tracklist.picture and group.curated_tracklist.picture.urls
                else None
            )
            items.append(
                self._create_virtual_playlist(
                    f"{SHAKER_CURATED_PREFIX}{group_id}",
                    f"{group.name} - Playlist",
                    image_url=cover_url,
                )
            )
        return items

    # -- Audiobook browse helpers --

    async def _browse_audiobooks_root(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Return audiobook sections from the Deezer audiobooks channel page."""
        page_data = await self.gw_client.get_page(AUDIOBOOKS_CHANNEL)
        sections = page_data.get("sections", [])
        base = path if path.endswith("/") else path + "/"
        items: list[MediaItemType | BrowseFolder] = []

        for section in sections:
            title = section.get("title", "")
            if not title:
                continue
            section_items = section.get("items", [])
            if not section_items:
                continue

            first_item = section_items[0]
            if first_item.get("type") == "channel":
                # Genre channels → one BrowseFolder per channel
                for item in section_items:
                    data = item.get("data", {})
                    target = data.get("target", "")
                    if not target:
                        continue
                    slug = target.removeprefix("/channels/")
                    channel_name = data.get("name", slug)
                    path_name = channel_name.replace("/", "-")
                    self._browse_slug_cache[f"audiobooks/{path_name}"] = f"channels/{slug}"
                    folder = BrowseFolder(
                        item_id=f"audiobooks_{slug}",
                        provider=self.instance_id,
                        path=f"{base}{path_name}",
                        name=channel_name,
                    )
                    folder.image = self._get_gw_item_image(item)
                    items.append(folder)
            else:
                # Album/playlist/artist section → BrowseFolder for the section
                module_id = section.get("module_id", "")
                if not module_id:
                    continue
                path_name = title.replace("/", "-")
                self._browse_slug_cache[f"audiobooks/{path_name}"] = f"channels/module/{module_id}"
                folder = BrowseFolder(
                    item_id=f"audiobooks_section_{module_id}",
                    provider=self.instance_id,
                    path=f"{base}{path_name}",
                    name=title,
                )
                folder.image = self._get_gw_item_image(first_item)
                items.append(folder)

        return items

    async def _browse_audiobooks_page(self, page_path: str) -> list[MediaItemType | BrowseFolder]:
        """Fetch a Deezer channel page and return its items."""
        page_data = await self.gw_client.get_page(page_path)
        sections = page_data.get("sections", [])
        items: list[MediaItemType | BrowseFolder] = []
        for section in sections:
            for item in section.get("items", []):
                if parsed := self._parse_gw_item(item):
                    items.append(parsed)
        return items

    def _parse_gw_item(self, item: dict[str, Any]) -> MediaItemType | None:
        """Parse a GW page item to a Music Assistant media item."""
        item_type = item.get("type")
        data = item.get("data", {})
        if item_type == "album" and data.get("ALB_ID"):
            return self._parse_gw_audiobook(data)
        if item_type == "playlist" and data.get("PLAYLIST_ID"):
            return self._parse_gw_playlist(data)
        if item_type == "artist" and data.get("ART_ID"):
            return self._parse_gw_artist(data)
        return None

    def _parse_gw_audiobook(self, data: dict[str, Any]) -> Audiobook:
        """Parse a GW page album item to Music Assistant Audiobook."""
        album_id = str(data["ALB_ID"])
        title = data.get("ALB_TITLE", "")

        images: UniqueList[MediaItemImage] = UniqueList()
        if md5 := data.get("ALB_PICTURE"):
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._deezer_cover_url(md5, "cover"),
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )

        authors: UniqueList[str] = UniqueList()
        if art_name := data.get("ART_NAME"):
            authors.append(art_name)

        return Audiobook(
            item_id=album_id,
            provider=self.instance_id,
            name=title,
            authors=authors,
            media_type=MediaType.AUDIOBOOK,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(images=images),
        )

    def _parse_gw_playlist(self, data: dict[str, Any]) -> Playlist:
        """Parse a GW page playlist item to Music Assistant Playlist."""
        playlist_id = str(data["PLAYLIST_ID"])

        images: UniqueList[MediaItemImage] = UniqueList()
        if md5 := data.get("PLAYLIST_PICTURE"):
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._deezer_cover_url(md5, "playlist"),
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )

        metadata = MediaItemMetadata(images=images) if images else MediaItemMetadata()
        if desc := data.get("DESCRIPTION"):
            metadata.description = desc

        return Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=data.get("TITLE", ""),
            media_type=MediaType.PLAYLIST,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=metadata,
            owner=data.get("PARENT_USERNAME", "Deezer"),
        )

    def _parse_gw_artist(self, data: dict[str, Any]) -> Artist:
        """Parse a GW page artist item to Music Assistant Artist."""
        artist_id = str(data["ART_ID"])

        images: UniqueList[MediaItemImage] = UniqueList()
        if md5 := data.get("ART_PICTURE"):
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._deezer_cover_url(md5, "artist"),
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )

        return Artist(
            item_id=artist_id,
            provider=self.instance_id,
            name=data.get("ART_NAME", ""),
            media_type=MediaType.ARTIST,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(images=images),
        )

    @staticmethod
    def _deezer_cover_url(md5: str, image_type: str = "cover", size: int = 500) -> str:
        """Construct a Deezer CDN image URL from an MD5 hash."""
        return f"{DEEZER_CDN_IMAGE}/{image_type}/{md5}/{size}x{size}-000000-80-0-0.jpg"

    def _get_gw_item_image(self, item: dict[str, Any]) -> MediaItemImage | None:
        """Extract a cover image from a GW page item."""
        data = item.get("data", {})
        item_type = item.get("type")
        md5: str | None = None
        img_type = "cover"

        if item_type == "album":
            md5 = data.get("ALB_PICTURE")
        elif item_type == "playlist":
            md5 = data.get("PLAYLIST_PICTURE")
            img_type = "playlist"
        elif item_type == "artist":
            md5 = data.get("ART_PICTURE")
            img_type = "artist"
        elif item_type == "channel":
            pictures = item.get("pictures", [])
            if pictures:
                md5 = pictures[0].get("md5")
                img_type = pictures[0].get("type", "misc")

        if md5:
            return MediaItemImage(
                type=ImageType.THUMB,
                path=self._deezer_cover_url(md5, img_type),
                provider=self.instance_id,
                remotely_accessible=True,
            )
        return None

    @use_cache(3600)
    async def _get_recently_played_items(self) -> list[MediaItemType]:
        """Get recently played items (cached, shared by browse and recommendations)."""
        result = await self.gql_client.get_recently_played(first=50)
        if not result:
            return []
        return self._parse_recently_played_edges(result.recently_played.edges)

    # -- Recommendations --

    @use_cache(3600)  # Cache for 1 hour
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get Deezer's recommendations including Flow and personalized content."""
        result: list[RecommendationFolder] = []
        recs = await self.gql_client.get_recommendations(
            playlists_first=10,
            artist_playlists_first=0,
            new_releases_first=10,
            artists_first=10,
            hot_tracks_limit=50,
        )
        await self._add_made_for_you(result, recs)
        self._add_recommended_tracks(result, recs)
        self._add_new_releases_and_artists(result, recs)
        await self._add_flow_configs(result)
        recently_played = await self._get_recently_played_items()
        if recently_played:
            result.append(
                RecommendationFolder(
                    item_id="recently_played",
                    provider=self.instance_id,
                    name="Recently Played",
                    items=UniqueList(recently_played),
                )
            )
        return result

    async def _add_made_for_you(
        self,
        result: list[RecommendationFolder],
        recs: GetRecommendationsMe | None,
    ) -> None:
        """Add Made For You section to recommendations."""
        made_for_me_items: list[Playlist] = []
        flow_me = await self.gql_client.get_flow()
        if flow_me and flow_me.flow:
            cover = (
                flow_me.flow.cover.urls[0]
                if flow_me.flow.cover and flow_me.flow.cover.urls
                else None
            )
            made_for_me_items.append(
                self._create_virtual_playlist(FLOW_PLAYLIST_ID, "Flow", image_url=cover)
            )
        made_for_me_items.extend(await self._get_smart_tracklist_playlists())
        if recs and recs.recommendations:
            for edge in recs.recommendations.playlists.edges:
                if edge.node is not None:
                    made_for_me_items.append(self._parse_playlist(edge.node))
        if made_for_me_items:
            result.append(
                RecommendationFolder(
                    item_id="made_for_you",
                    provider=self.instance_id,
                    name="Made for you",
                    items=UniqueList(made_for_me_items),
                )
            )

    def _add_recommended_tracks(
        self,
        result: list[RecommendationFolder],
        recs: GetRecommendationsMe | None,
    ) -> None:
        """Add Hot Tracks section with tracks rendered directly."""
        if not recs or not recs.recommendations.hot_tracks:
            return
        track_items = [self._parse_track(ht) for ht in recs.recommendations.hot_tracks]
        if track_items:
            result.append(
                RecommendationFolder(
                    item_id="recommended_tracks",
                    provider=self.instance_id,
                    name="Hot Tracks",
                    items=UniqueList(track_items),
                )
            )

    def _add_new_releases_and_artists(
        self,
        result: list[RecommendationFolder],
        recs: GetRecommendationsMe | None,
    ) -> None:
        """Add New Releases and Recommended Artists sections to recommendations."""
        if recs is None:
            return
        new_release_items = [
            self._parse_album(edge.node)
            for edge in recs.recommendations.new_releases.edges
            if edge.node is not None
        ]
        if new_release_items:
            result.append(
                RecommendationFolder(
                    item_id="new_releases",
                    provider=self.instance_id,
                    name="New Releases",
                    items=UniqueList(new_release_items),
                )
            )
        rec_artist_items = [
            self._parse_artist(edge.node)
            for edge in recs.recommendations.artists.edges
            if edge.node is not None
        ]
        if rec_artist_items:
            result.append(
                RecommendationFolder(
                    item_id="recommended_artists",
                    provider=self.instance_id,
                    name="Recommended Artists",
                    items=UniqueList(rec_artist_items),
                )
            )

    async def _add_flow_configs(self, result: list[RecommendationFolder]) -> None:
        """Add Mood and Genre Flow sections to recommendations."""
        flow_configs = await self.gql_client.get_flow_configs(moods_first=20, genres_first=20)
        if not flow_configs or not flow_configs.flow_configs:
            return
        for folder_id, folder_name, edges in [
            ("mood_flows", "Deezer Mood Flows", flow_configs.flow_configs.moods.edges),
            ("genre_flows", "Deezer Genre Flows", flow_configs.flow_configs.genres.edges),
        ]:
            playlists = self._flow_configs_to_playlists(edges)
            if playlists:
                result.append(
                    RecommendationFolder(
                        item_id=folder_id,
                        provider=self.instance_id,
                        name=folder_name,
                        items=UniqueList(playlists),
                    )
                )

    # -- Similar tracks (via GW API) --

    @use_cache(3600 * 24)  # Cache for 24 hours
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        result = await self.gql_client.get_similar_tracks(track_id=prov_track_id, nb=limit)
        if result is None or result.track is None:
            return []
        return [self._parse_track(t) for t in result.track.recommended_tracks if t is not None]

    # -- Streaming (tracks via GW API, radio/podcasts via GQL stream URLs) --

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Get the resume position for a podcast episode.

        :param item_id: The provider-specific episode ID.
        :param media_type: The media type (only PODCAST_EPISODE is supported).
        :returns: Tuple of (fully_played, resume_position_ms).
        """
        if media_type != MediaType.PODCAST_EPISODE:
            return (False, 0)
        result = await self.gql_client.get_podcast_episode_bookmarks(first=100)
        if not result:
            return (False, 0)
        for edge in result.podcast_episode_bookmarks.edges:
            if edge.node is None:
                continue
            if edge.node.episode.id == item_id:
                return (edge.node.is_played, edge.node.position * 1000)
        return (False, 0)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        if media_type == MediaType.RADIO:
            return await self._get_radio_stream_details(item_id)
        if media_type == MediaType.PODCAST_EPISODE:
            return await self._get_podcast_episode_stream_details(item_id)
        url_details, song_data = await self.gw_client.get_deezer_track_urls(item_id)
        url = url_details["sources"][0]["url"]
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(url_details["format"].split("_")[0])
            ),
            stream_type=StreamType.CUSTOM,
            duration=int(song_data["DURATION"]),
            data={"url": url, "format": url_details["format"], "track_id": song_data["SNG_ID"]},
            size=int(song_data[f"FILESIZE_{url_details['format']}"]),
            can_seek=True,
            allow_seek=True,
        )

    async def _get_radio_stream_details(self, item_id: str) -> StreamDetails:
        """Return stream details for a Deezer livestream (radio station)."""
        result = await self.gql_client.get_livestream(livestream_id=item_id)
        if result is None or not result.media:
            raise MediaNotFoundError(f"Radio {item_id} has no stream URL")
        # Prefer HLS, then AAC, then MP3
        best_media = result.media[0]
        for media in result.media:
            if media.codec and media.codec.type_ == "hls":
                best_media = media
                break
        content_type = ContentType.UNKNOWN
        if best_media.codec:
            content_type = ContentType.try_parse(best_media.codec.type_)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=best_media.codec.bitrate if best_media.codec else None,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=best_media.url,
            can_seek=False,
            allow_seek=False,
        )

    async def _get_podcast_episode_stream_details(self, item_id: str) -> StreamDetails:
        """Return stream details for a Deezer podcast episode."""
        result = await self.gql_client.get_podcast_episode(podcast_episode_id=item_id)
        if result is None or not result.media:
            raise MediaNotFoundError(f"Podcast episode {item_id} has no stream URL")
        content_type = ContentType.UNKNOWN
        if result.media.codec:
            content_type = ContentType.try_parse(result.media.codec.type_)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=result.media.codec.bitrate if result.media.codec else None,
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=result.media.url,
            duration=result.duration,
            can_seek=True,
            allow_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        blowfish_key = self._get_blowfish_key(streamdetails.data["track_id"])
        chunk_index = 0
        timeout = ClientTimeout(total=None, connect=30, sock_read=600)
        headers: dict[str, str] = {}

        # Seek by skipping chunks (Range header causes malformed audio)
        if seek_position and streamdetails.size and streamdetails.duration:
            chunk_count = ceil(streamdetails.size / 2048)
            skip_chunks = int(chunk_count / streamdetails.duration) * seek_position
        else:
            skip_chunks = 0

        buffer = bytearray()
        streamdetails.data["start_ts"] = utc_timestamp()
        streamdetails.data["stream_id"] = uuid.uuid1()
        self.mass.create_task(self.gw_client.log_listen(next_track=streamdetails.item_id))
        async with self.mass.http_session.get(
            streamdetails.data["url"], headers=headers, timeout=timeout
        ) as resp:
            async for chunk in resp.content.iter_chunked(2048):
                buffer += chunk
                if len(buffer) >= 2048:
                    if chunk_index >= skip_chunks or chunk_index == 0:
                        if chunk_index % 3 > 0:
                            yield bytes(buffer[:2048])
                        else:
                            yield self._decrypt_chunk(bytes(buffer[:2048]), blowfish_key)

                    chunk_index += 1
                    del buffer[:2048]
        yield bytes(buffer)

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Handle callback when an item completed streaming."""
        await self.gw_client.log_listen(last_track=streamdetails)

    # -- Cached virtual playlist data --

    @use_cache(3600)
    async def _get_smart_tracklist_playlists(self) -> list[Playlist]:
        """Get SmartTracklist items from Made For Me as virtual playlists (cached).

        Shared by browse (Made For You) and recommendations.
        """
        made_for_me = await self.gql_client.get_made_for_me(first=20)
        if not made_for_me or not made_for_me.made_for_me:
            return []
        playlists: list[Playlist] = []
        for edge in made_for_me.made_for_me.edges:
            if edge.node is None:
                continue
            if isinstance(edge.node, GetMadeForMeMeMadeForMeEdgesNodeSmartTracklist):
                cover = (
                    edge.node.cover.urls[0] if edge.node.cover and edge.node.cover.urls else None
                )
                playlists.append(
                    self._create_virtual_playlist(
                        f"{SMART_TRACKLIST_PREFIX}{edge.node.id}",
                        edge.node.title,
                        image_url=cover,
                    )
                )
        return playlists

    @use_cache(3600)
    async def _get_flow_tracks(self) -> list[Track]:
        """Get Flow tracks by fetching 4 batches in a single request.

        Uses the batched flow query to get 4 fresh random batches in one API call,
        then deduplicates by track ID to build a larger playlist (~80-100 unique tracks).
        """
        result = await self.gql_client.get_flow_batch()
        if result is None or result.flow is None:
            return []
        seen: set[str] = set()
        tracks: list[Track] = []
        for batch in (
            result.flow.batch_1,
            result.flow.batch_2,
            result.flow.batch_3,
            result.flow.batch_4,
        ):
            for ft in batch:
                if ft.track is not None and ft.track.id not in seen:
                    seen.add(ft.track.id)
                    tracks.append(self._parse_track(ft.track))
        return tracks

    @use_cache(3600)
    async def _get_recommended_tracks(self) -> list[Track]:
        """Get cached recommended tracks (hot tracks)."""
        recs = await self.gql_client.get_recommendations(
            playlists_first=0,
            artist_playlists_first=0,
            new_releases_first=0,
            artists_first=0,
            hot_tracks_limit=50,
        )
        if recs is None or recs.recommendations.hot_tracks is None:
            return []
        return [self._parse_track(ht) for ht in recs.recommendations.hot_tracks]

    @use_cache(3600)
    async def _get_chart_tracks(self) -> list[Track]:
        """Get cached chart tracks."""
        charts = await self.gql_client.get_charts(
            country_code=self.gw_client.user_country,
            tracks_first=100,
        )
        if charts is None or charts.country is None or charts.country.tracks is None:
            return []
        return [
            self._parse_track(edge.node)
            for edge in charts.country.tracks.edges
            if edge.node is not None
        ]

    @use_cache(3600)
    async def _get_flow_config_tracks(self, config_id: str) -> list[Track]:
        """Get tracks for a mood/genre Flow config.

        Fetches 4 batches from the API and deduplicates by track ID
        to build a richer playlist. Each API call returns a fresh random batch.

        :param config_id: The Flow config identifier (e.g. "happy", "chill", "genre-rock").
        """
        seen: set[str] = set()
        tracks: list[Track] = []
        for _ in range(4):
            result = await self.gql_client.get_flow_config_tracks(flow_config_id=config_id)
            if result is None:
                break
            for ft in result.tracks:
                if ft.track is not None and ft.track.id not in seen:
                    seen.add(ft.track.id)
                    tracks.append(self._parse_track(ft.track))
        return tracks

    @use_cache(3600)
    async def _get_smart_tracklist_tracks(self, tracklist_id: str) -> list[Track]:
        """Get tracks for a SmartTracklist.

        :param tracklist_id: The SmartTracklist identifier.
        """
        result = await self.gql_client.get_smart_tracklist(
            smart_tracklist_id=tracklist_id, first=50
        )
        if result is None:
            return []
        return [
            self._parse_track(edge.node) for edge in result.tracks.edges if edge.node is not None
        ]

    async def _get_shaker_tracks(self, group_id: str) -> list[Track]:
        """Get suggested tracks for a Shaker (Music Together) group.

        :param group_id: The Music Together group identifier.
        """
        group = await self.gql_client.get_music_together_group(
            group_id=group_id,
            mood=MusicTogetherSuggestedTracklistMoodInput.NONE,
            tracks_first=50,
        )
        if group is None or group.suggested_tracklist is None:
            return []
        tracklist = group.suggested_tracklist.tracklist
        if tracklist is None:
            return []
        return [self._parse_track(edge.node) for edge in tracklist.tracks.edges if edge.node]

    async def _get_shaker_curated_tracks(self, group_id: str) -> list[Track]:
        """Get curated playlist tracks for a Shaker (Music Together) group.

        :param group_id: The Music Together group identifier.
        """
        group = await self.gql_client.get_music_together_group(
            group_id=group_id,
            mood=MusicTogetherSuggestedTracklistMoodInput.NONE,
            tracks_first=50,
        )
        if group is None or group.curated_tracklist is None:
            return []
        return [
            self._parse_track(edge.node)
            for edge in group.curated_tracklist.tracks.edges
            if edge.node
        ]

    @use_cache(3600)
    async def _get_user_chart_tracks(self) -> list[Track]:
        """Get the user's most listened tracks."""
        result = await self.gql_client.get_user_charts(tracks_first=50)
        if not result or not result.charts.tracks:
            return []
        return [
            self._parse_track(edge.node)
            for edge in result.charts.tracks.edges
            if edge.node is not None
        ]

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def _get_regular_playlist_tracks(self, prov_playlist_id: str) -> list[Track]:
        """Get tracks for regular Deezer playlists (cached)."""
        result = await self.gql_client.get_playlist(playlist_id=prov_playlist_id)
        if result is None:
            return []
        return [
            self._parse_track(edge.node, position=idx)
            for idx, edge in enumerate(result.tracks.edges, 1)
            if edge.node is not None
        ]

    # -- Virtual playlist helpers --

    async def _get_virtual_playlist(self, prov_playlist_id: str) -> Playlist | None:
        """Return a virtual playlist, or None if the ID is not virtual."""
        if prov_playlist_id == FLOW_PLAYLIST_ID:
            cover = await self._get_flow_cover()
            return self._create_virtual_playlist(FLOW_PLAYLIST_ID, "Flow", image_url=cover)
        if prov_playlist_id == RECOMMENDED_TRACKS_PLAYLIST_ID:
            return self._create_virtual_playlist(
                RECOMMENDED_TRACKS_PLAYLIST_ID, "Recommended Tracks"
            )
        if prov_playlist_id == TOP_CHARTS_PLAYLIST_ID:
            return self._create_virtual_playlist(TOP_CHARTS_PLAYLIST_ID, "Top Charts")
        if prov_playlist_id == USER_TOP_TRACKS_PLAYLIST_ID:
            return self._create_virtual_playlist(USER_TOP_TRACKS_PLAYLIST_ID, "Your Top Tracks")
        if prov_playlist_id.startswith(FLOW_CONFIG_PREFIX):
            config_id = prov_playlist_id.removeprefix(FLOW_CONFIG_PREFIX)
            result = await self.gql_client.get_flow_config_tracks(flow_config_id=config_id)
            name = f"Flow: {result.title}" if result else f"Flow: {config_id}"
            cover = self._get_flow_config_image(result) if result else None
            return self._create_virtual_playlist(prov_playlist_id, name, image_url=cover)
        if prov_playlist_id.startswith(SMART_TRACKLIST_PREFIX):
            tracklist_id = prov_playlist_id.removeprefix(SMART_TRACKLIST_PREFIX)
            result = await self.gql_client.get_smart_tracklist(
                smart_tracklist_id=tracklist_id, first=1
            )
            name = result.title if result else f"Mix {tracklist_id}"
            cover = result.cover.urls[0] if result and result.cover and result.cover.urls else None
            return self._create_virtual_playlist(prov_playlist_id, name, image_url=cover)
        if prov_playlist_id.startswith(SHAKER_CURATED_PREFIX):
            group_id = prov_playlist_id.removeprefix(SHAKER_CURATED_PREFIX)
            group = await self.gql_client.get_music_together_group(
                group_id=group_id,
                mood=MusicTogetherSuggestedTracklistMoodInput.NONE,
                tracks_first=1,
            )
            name = f"{group.name} - Playlist" if group else f"Shaker {group_id}"
            cover_url: str | None = None
            if (
                group
                and group.curated_tracklist
                and group.curated_tracklist.picture
                and group.curated_tracklist.picture.urls
            ):
                cover_url = group.curated_tracklist.picture.urls[0]
            return self._create_virtual_playlist(prov_playlist_id, name, image_url=cover_url)
        if prov_playlist_id.startswith(SHAKER_PREFIX):
            group_id = prov_playlist_id.removeprefix(SHAKER_PREFIX)
            group = await self.gql_client.get_music_together_group(
                group_id=group_id,
                mood=MusicTogetherSuggestedTracklistMoodInput.NONE,
                tracks_first=1,
            )
            name = f"{group.name} - Mix" if group else f"Shaker {group_id}"
            return self._create_virtual_playlist(prov_playlist_id, name, image_url=SHAKER_MIX_COVER)
        return None

    @use_cache(3600)
    async def _get_flow_cover(self) -> str | None:
        """Get the cover URL for the user's Flow."""
        result = await self.gql_client.get_flow()
        if result and result.flow and result.flow.cover and result.flow.cover.urls:
            return str(result.flow.cover.urls[0])
        return None

    def _create_virtual_playlist(
        self,
        item_id: str,
        name: str,
        image_url: str | None = None,
    ) -> Playlist:
        """Create a virtual playlist for Flow, recommended content, etc.

        :param item_id: The unique identifier (e.g., "flow", "smart_tracklist_123").
        :param name: Display name for the playlist.
        :param image_url: Optional cover image URL.
        """
        images: UniqueList[MediaItemImage] = UniqueList()
        if image_url:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            media_type=MediaType.PLAYLIST,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(images=images) if images else MediaItemMetadata(),
            is_editable=False,
            owner="Deezer",
        )

    # -- Parse methods (GQL Pydantic models → MA models) --

    def _parse_track(self, track: TrackFields, position: int = 0) -> Track:
        """Parse a GQL track model to Music Assistant Track.

        :param track: A GQL track model inheriting from TrackFields.
        :param position: Position in a track list (for playlist ordering).
        """
        artists: UniqueList[Artist | ItemMapping] = UniqueList()
        for edge in track.contributors.edges:
            if edge.node is not None:
                artists.append(
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=edge.node.id,
                        provider=self.instance_id,
                        name=edge.node.name,
                    )
                )

        album_mapping: ItemMapping | None = None
        if track.album is not None:
            album_mapping = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=track.album.id,
                provider=self.instance_id,
                name=track.album.display_title,
            )

        name, version = parse_title_and_version(track.title)
        disc_number = 0
        track_number = position
        if track.disk_info is not None:
            disc_number = track.disk_info.disk_number or 0
            track_number = track.disk_info.track_number or position

        item = Track(
            item_id=track.id,
            provider=self.instance_id,
            name=name,
            version=version,
            duration=track.duration,
            artists=artists,
            album=album_mapping,
            provider_mappings={
                ProviderMapping(
                    item_id=track.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                )
            },
            metadata=self._parse_track_metadata(track),
            track_number=track_number,
            position=position,
            disc_number=disc_number,
        )
        if track.isrc:
            item.external_ids.add((ExternalID.ISRC, track.isrc))
        if track.is_favorite:
            item.favorite = True
        if track.popularity is not None:
            item.metadata.popularity = int(track.popularity)
        return item

    def _parse_track_metadata(self, track: TrackFields) -> MediaItemMetadata:
        """Parse track metadata (images, explicit flag, lyrics) from a GQL track model."""
        metadata = MediaItemMetadata(explicit=track.is_explicit)
        if track.album is not None and track.album.cover and track.album.cover.urls:
            metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=track.album.cover.urls[0],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        # Lyrics (only present on GetTrackTrack, not on fragment-based models)
        if hasattr(track, "lyrics") and track.lyrics is not None:
            if track.lyrics.text:
                metadata.lyrics = track.lyrics.text
            if track.lyrics.synchronized_lines:
                lrc_lines = [
                    f"{line.lrc_timestamp} {line.line}"
                    for line in track.lyrics.synchronized_lines
                    if line.lrc_timestamp
                ]
                if lrc_lines:
                    metadata.lrc_lyrics = "\n".join(lrc_lines)
        return metadata

    def _parse_artist(self, artist: ArtistFields) -> Artist:
        """Parse a GQL artist model to Music Assistant Artist."""
        images: UniqueList[MediaItemImage] = UniqueList()
        if artist.picture and artist.picture.urls:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artist.picture.urls[0],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        metadata = MediaItemMetadata(images=images) if images else MediaItemMetadata()
        if artist.bio:
            metadata.description = artist.bio.summary or artist.bio.full
        if artist.fans_count is not None:
            metadata.popularity = int(artist.fans_count)
        item = Artist(
            item_id=artist.id,
            provider=self.instance_id,
            name=artist.name,
            media_type=MediaType.ARTIST,
            provider_mappings={
                ProviderMapping(
                    item_id=artist.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=metadata,
        )
        if artist.is_favorite:
            item.favorite = True
        return item

    def _parse_album(self, album: AlbumFields) -> Album:
        """Parse a GQL album model to Music Assistant Album."""
        name, version = parse_title_and_version(album.display_title)
        artists: UniqueList[Artist | ItemMapping] = UniqueList()
        for edge in album.contributors.edges:
            if edge.node is not None:
                artists.append(
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=edge.node.id,
                        provider=self.instance_id,
                        name=edge.node.name,
                    )
                )

        images: UniqueList[MediaItemImage] = UniqueList()
        if album.cover and album.cover.urls:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=album.cover.urls[0],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )

        deezer_type = album.type_
        album_type = self._map_album_type(deezer_type, name)

        year: int | None = None
        if album.release_date:
            with contextlib.suppress(ValueError, TypeError):
                year = datetime.fromisoformat(str(album.release_date)).year

        item = Album(
            album_type=album_type,
            item_id=album.id,
            provider=self.instance_id,
            name=name,
            version=version,
            year=year,
            artists=artists,
            media_type=MediaType.ALBUM,
            provider_mappings={
                ProviderMapping(
                    item_id=album.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                explicit=album.is_explicit,
                images=images,
                label=album.label,
                copyright=album.copyright,
            ),
        )
        if album.fans_count is not None:
            item.metadata.popularity = int(album.fans_count)
        if album.is_favorite:
            item.favorite = True
        return item

    def _parse_playlist(self, playlist: PlaylistFields, is_editable: bool = False) -> Playlist:
        """Parse a GQL playlist model to Music Assistant Playlist.

        :param playlist: A GQL playlist model inheriting from PlaylistFields.
        :param is_editable: Whether the current user owns this playlist.
        """
        images: UniqueList[MediaItemImage] = UniqueList()
        if playlist.picture and playlist.picture.urls:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=playlist.picture.urls[0],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        owner_name = "Unknown"
        if playlist.owner:
            owner_name = playlist.owner.name
            if not is_editable and playlist.owner.id == self._user_id:
                is_editable = True

        metadata = MediaItemMetadata(images=images) if images else MediaItemMetadata()
        if playlist.fans_count is not None:
            metadata.popularity = int(playlist.fans_count)
        if playlist.description:
            metadata.description = playlist.description
        item = Playlist(
            item_id=playlist.id,
            provider=self.instance_id,
            name=playlist.title,
            media_type=MediaType.PLAYLIST,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=is_editable,
                )
            },
            metadata=metadata,
            is_editable=is_editable,
            owner=owner_name,
        )
        if playlist.is_favorite or is_editable:
            item.favorite = True
        return item

    def _parse_radio(self, livestream: LivestreamFields) -> Radio:
        """Parse a GQL Livestream model to Music Assistant Radio.

        :param livestream: A GQL livestream model inheriting from LivestreamFields.
        """
        images: UniqueList[MediaItemImage] = UniqueList()
        if livestream.cover and livestream.cover.urls:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=livestream.cover.urls[0],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        metadata = MediaItemMetadata(images=images) if images else MediaItemMetadata()
        if livestream.description:
            metadata.description = livestream.description
        return Radio(
            item_id=livestream.id,
            provider=self.instance_id,
            name=livestream.name,
            media_type=MediaType.RADIO,
            provider_mappings={
                ProviderMapping(
                    item_id=livestream.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=metadata,
        )

    def _parse_podcast(self, podcast: PodcastFields) -> Podcast:
        """Parse a GQL podcast model to Music Assistant Podcast.

        :param podcast: A GQL podcast model inheriting from PodcastFields.
        """
        images: UniqueList[MediaItemImage] = UniqueList()
        if podcast.cover and podcast.cover.urls:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=podcast.cover.urls[0],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        metadata = MediaItemMetadata(
            images=images,
            explicit=podcast.is_explicit,
        )
        if podcast.description:
            metadata.description = podcast.description
        item = Podcast(
            item_id=podcast.id,
            provider=self.instance_id,
            name=podcast.display_title,
            media_type=MediaType.PODCAST,
            provider_mappings={
                ProviderMapping(
                    item_id=podcast.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=metadata,
        )
        if podcast.is_favorite:
            item.favorite = True
        return item

    def _parse_podcast_episode(
        self,
        episode: PodcastEpisodeFields,
        podcast: Podcast | ItemMapping,
        position: int = 0,
        podcast_image_url: str | None = None,
    ) -> PodcastEpisode:
        """Parse a GQL podcast episode model to Music Assistant PodcastEpisode.

        :param episode: A GQL podcast episode model inheriting from PodcastEpisodeFields.
        :param podcast: Parent podcast reference.
        :param position: Sort position / episode number.
        :param podcast_image_url: Fallback image from parent podcast if episode has none.
        """
        images: UniqueList[MediaItemImage] = UniqueList()
        thumb_url = None
        if episode.cover and episode.cover.urls:
            thumb_url = episode.cover.urls[0]
        elif podcast_image_url:
            thumb_url = podcast_image_url
        if thumb_url:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=thumb_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        metadata = MediaItemMetadata(images=images)
        if episode.description:
            metadata.description = episode.description
        episode_name = episode.title
        if episode.publication_date:
            with contextlib.suppress(ValueError, TypeError):
                pub_date = datetime.fromisoformat(str(episode.publication_date))
                metadata.release_date = pub_date
                episode_name = f"{pub_date.strftime('%Y-%m-%d')} - {episode.title}"
        return PodcastEpisode(
            item_id=episode.id,
            provider=self.instance_id,
            name=episode_name,
            duration=episode.duration,
            podcast=podcast,
            position=position,
            media_type=MediaType.PODCAST_EPISODE,
            provider_mappings={
                ProviderMapping(
                    item_id=episode.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=metadata,
        )

    def _parse_audiobook(self, audiobook: AudiobookFields) -> Audiobook:
        """Parse a GQL audiobook model to Music Assistant Audiobook.

        :param audiobook: A GQL audiobook model inheriting from AudiobookFields.
        """
        images: UniqueList[MediaItemImage] = UniqueList()
        if audiobook.cover and audiobook.cover.urls:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=audiobook.cover.urls[0],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        metadata = MediaItemMetadata(
            images=images,
            explicit=audiobook.is_explicit,
        )
        if audiobook.description:
            metadata.description = audiobook.description
        if audiobook.fans_count:
            metadata.popularity = int(audiobook.fans_count)

        authors: UniqueList[str] = UniqueList()
        narrators: UniqueList[str] = UniqueList()
        for edge in audiobook.contributors.edges:
            if edge.node is None:
                continue
            if AudiobookContributorRoles.NARRATOR in edge.roles:
                narrators.append(edge.node.name)
            else:
                authors.append(edge.node.name)

        item = Audiobook(
            item_id=audiobook.id,
            provider=self.instance_id,
            name=audiobook.display_title or audiobook.id,
            duration=audiobook.duration,
            publisher=audiobook.publisher,
            authors=authors,
            narrators=narrators,
            media_type=MediaType.AUDIOBOOK,
            provider_mappings={
                ProviderMapping(
                    item_id=audiobook.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=metadata,
        )
        if audiobook.is_favorite:
            item.favorite = True
        return item

    def _parse_audiobook_from_album(self, album: AlbumFields) -> Audiobook:
        """Create an Audiobook from an AlbumFields result (search context).

        Used when an album search result is identified as an audiobook via
        check_audiobook_ids. Provides basic metadata from the album fields.

        :param album: A GQL album model inheriting from AlbumFields.
        """
        images: UniqueList[MediaItemImage] = UniqueList()
        if album.cover and album.cover.urls:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=album.cover.urls[0],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )

        authors: UniqueList[str] = UniqueList()
        for edge in album.contributors.edges:
            if edge.node is not None:
                authors.append(edge.node.name)

        return Audiobook(
            item_id=album.id,
            provider=self.instance_id,
            name=album.display_title,
            authors=authors,
            media_type=MediaType.AUDIOBOOK,
            provider_mappings={
                ProviderMapping(
                    item_id=album.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                images=images,
                explicit=album.is_explicit,
            ),
        )

    @staticmethod
    def _parse_audiobook_chapters(
        chapter_edges: Sequence[Any],
    ) -> list[MediaItemChapter]:
        """Build MediaItemChapter list from audiobook chapter edges.

        Computes cumulative start/end positions from individual chapter durations.

        :param chapter_edges: List of chapter edge objects with `.node` attribute.
        """
        chapters: list[MediaItemChapter] = []
        cumulative_seconds = 0.0
        for idx, edge in enumerate(chapter_edges):
            if edge.node is None:
                continue
            duration = float(edge.node.duration)
            chapters.append(
                MediaItemChapter(
                    position=idx + 1,
                    name=edge.node.display_title,
                    start=cumulative_seconds,
                    end=cumulative_seconds + duration,
                )
            )
            cumulative_seconds += duration
        return chapters

    def _parse_recently_played_edges(
        self, edges: list[GetRecentlyPlayedMeRecentlyPlayedEdges]
    ) -> list[MediaItemType]:
        """Parse recently played edges into MediaItemType list."""
        items: list[MediaItemType] = []
        for edge in edges:
            node = edge.node
            if node is None:
                continue
            if isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodeAlbum):
                items.append(self._parse_album(node))
            elif isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodePlaylist):
                items.append(self._parse_playlist(node))
            elif isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodeArtist):
                items.append(self._parse_artist(node))
            elif isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlow):
                cover = node.cover.urls[0] if node.cover and node.cover.urls else None
                items.append(
                    self._create_virtual_playlist(FLOW_PLAYLIST_ID, node.title, image_url=cover)
                )
            elif isinstance(node, GetRecentlyPlayedMeRecentlyPlayedEdgesNodeFlowConfig):
                cover = self._get_flow_config_image(node)
                playlist_id = f"{FLOW_CONFIG_PREFIX}{node.id}"
                items.append(
                    self._create_virtual_playlist(
                        playlist_id, f"Flow: {node.title}", image_url=cover
                    )
                )
        return items

    # -- Helpers --

    @staticmethod
    def _apply_web_url(item: Artist | Album, gql_result: object) -> None:
        """Set web URL on provider mappings if available in the GQL result."""
        if hasattr(gql_result, "url") and hasattr(gql_result.url, "web_url"):
            for pm in item.provider_mappings:
                pm.url = gql_result.url.web_url

    @staticmethod
    def _get_flow_config_image(node: _HasVisuals) -> str | None:
        """Extract the square icon URL from a FlowConfig node's visuals."""
        icon = node.visuals.hardware_square_icon
        if icon and icon.urls:
            return icon.urls[0]
        return None

    @staticmethod
    def _map_album_type(deezer_type: DeezerAlbumType | None, title: str) -> AlbumType:
        """Map Deezer album type to Music Assistant AlbumType."""
        inferred = infer_album_type(title, "")
        if inferred in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
            return inferred
        if deezer_type is None:
            return AlbumType.UNKNOWN
        if isinstance(deezer_type, DeezerAlbumType):
            match deezer_type:
                case DeezerAlbumType.ALBUM:
                    return AlbumType.ALBUM
                case DeezerAlbumType.SINGLES:
                    return AlbumType.SINGLE
                case DeezerAlbumType.EP:
                    return AlbumType.EP
                case DeezerAlbumType.COMPILATIONS:
                    return AlbumType.COMPILATION
                case _:
                    return AlbumType.UNKNOWN
        return AlbumType.UNKNOWN

    @staticmethod
    def _parse_date(date_value: str | None) -> datetime:
        """Parse a date value from the GQL API to a timezone-aware datetime."""
        try:
            return datetime.fromisoformat(str(date_value))
        except (ValueError, TypeError):
            return datetime.now(tz=UTC)

    @staticmethod
    def _md5(data: str, data_type: str = "ascii") -> str:
        md5sum = hashlib.md5()
        md5sum.update(data.encode(data_type))
        return md5sum.hexdigest()

    def _get_blowfish_key(self, track_id: str) -> str:
        """Get blowfish key to decrypt a chunk of a track."""
        secret = app_var(5)
        id_md5 = self._md5(track_id)
        return "".join(
            chr(ord(id_md5[i]) ^ ord(id_md5[i + 16]) ^ ord(secret[i])) for i in range(16)
        )

    @staticmethod
    def _decrypt_chunk(chunk: bytes, blowfish_key: str) -> bytes:
        """Decrypt a given chunk using the blow fish key."""
        cipher = Blowfish.new(
            blowfish_key.encode("ascii"),
            Blowfish.MODE_CBC,
            b"\x00\x01\x02\x03\x04\x05\x06\x07",
        )
        return cipher.decrypt(chunk)  # type: ignore[no-any-return,unused-ignore]
