"""
Browse and recommendations manager for the Deezer provider.

Handles browse tree routing, recommendation folders, virtual playlist
infrastructure, and all track-fetching methods for virtual playlists.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from typing import TYPE_CHECKING

from deezer_python_gql.generated.enums import (
    MusicTogetherRefreshSuggestedTracklistMoodInput,
    MusicTogetherSuggestedTracklistMoodInput,
)
from deezer_python_gql.generated.get_made_for_me import (
    GetMadeForMeMeMadeForMeEdgesNodeSmartTracklist,
)
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    RecommendationFolder,
    Track,
    UniqueList,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.track_filter import filter_tracks

from .constants import (
    BROWSE_ALL_FLOWS,
    BROWSE_AUDIOBOOKS,
    BROWSE_EXPLORE,
    BROWSE_GENRES,
    BROWSE_MADE_FOR_YOU,
    BROWSE_MOODS,
    BROWSE_PERSONALIZED_PLAYLISTS,
    BROWSE_RECENTLY_PLAYED,
    BROWSE_RECOMMENDED_ARTIST_PLAYLISTS,
    BROWSE_RECOMMENDED_PLAYLISTS,
    BROWSE_SHAKER,
    BROWSE_TOP_ALBUMS,
    BROWSE_TOP_ARTISTS,
    BROWSE_TOP_PLAYLISTS,
    BROWSE_YOUR_TOP_ALBUMS,
    BROWSE_YOUR_TOP_ARTISTS,
    DEFAULT_FLOW_CONFIG_ID,
    FLOW_BATCH_COUNT,
    FLOW_CONFIG_PREFIX,
    FLOW_PLAYLIST_ID,
    PERSONAL_SONGS_PLAYLIST_ID,
    RECOMMENDED_TRACKS_PLAYLIST_ID,
    SHAKER_CURATED_PREFIX,
    SHAKER_MIX_COVER,
    SHAKER_PREFIX,
    SMART_TRACKLIST_PREFIX,
    TOP_CHARTS_PLAYLIST_ID,
    USER_TOP_TRACKS_PLAYLIST_ID,
)
from .helpers import (
    create_virtual_playlist,
)
from .parsers import (
    get_flow_config_image,
    get_gw_item_image,
    parse_album,
    parse_artist,
    parse_gw_item,
    parse_gw_track,
    parse_playlist,
    parse_recently_played_edges,
    parse_track,
)

if TYPE_CHECKING:
    from deezer_python_gql.generated.get_flow_configs import (
        GetFlowConfigsMeFlowConfigsGenresEdges,
        GetFlowConfigsMeFlowConfigsMoodsEdges,
    )
    from deezer_python_gql.generated.get_recommendations import GetRecommendationsMe
    from deezer_python_gql.generated.search_flows import (
        SearchFlowsSearchResultsFlowConfigsEdges,
    )

    from .provider import DeezerProvider

AUDIOBOOKS_CHANNEL = "channels/audiobooks"


class DeezerBrowseManager:
    """Handles browse tree, recommendations, and virtual playlist content."""

    def __init__(self, provider: DeezerProvider) -> None:
        """Initialize browse manager."""
        self.provider = provider
        self.mass = provider.mass
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        self.logger = provider.logger
        self._browse_slug_cache: dict[str, str] = {}

    # -- Browse routing --

    async def browse(
        self,
        path: str,
        base_browse: Callable[
            [str], Coroutine[None, None, Sequence[MediaItemType | ItemMapping | BrowseFolder]]
        ],
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse Deezer content.

        :param path: The browse path.
        :param base_browse: Coroutine for the base class browse method.
        """
        path_parts = path.split("://")[1].split("/") if "://" in path else []
        subpath = path_parts[0] if path_parts else None
        sub_subpath = path_parts[1] if len(path_parts) > 1 else None

        if subpath == BROWSE_MADE_FOR_YOU:
            return await self._browse_made_for_you(path, sub_subpath)

        if subpath == BROWSE_EXPLORE:
            if sub_subpath:
                return await self._browse_explore_category(sub_subpath)
            return await self._browse_explore_root(path)

        if subpath == BROWSE_RECENTLY_PLAYED:
            return await self._get_recently_played_items()

        if subpath == BROWSE_SHAKER:
            if sub_subpath:
                group_id = self._browse_slug_cache.get(f"shaker/{sub_subpath}", sub_subpath)
                return await self._browse_shaker_group(group_id)
            return await self._browse_shaker_root(path)

        if subpath == BROWSE_AUDIOBOOKS:
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
                for item in await base_browse(path)
                if not (isinstance(item, BrowseFolder) and item.item_id == "recommendations")
            ]
            base = path if path.endswith("//") else path.rstrip("/") + "/"
            base_items.extend(
                [
                    BrowseFolder(
                        item_id="made_for_me",
                        provider=self.instance_id,
                        path=f"{base}{BROWSE_MADE_FOR_YOU}",
                        name=BROWSE_MADE_FOR_YOU,
                        translation_key="made_for_me",
                    ),
                    BrowseFolder(
                        item_id="explore",
                        provider=self.instance_id,
                        path=f"{base}{BROWSE_EXPLORE}",
                        name=BROWSE_EXPLORE,
                        translation_key="explore",
                    ),
                    BrowseFolder(
                        item_id="recently_played",
                        provider=self.instance_id,
                        path=f"{base}{BROWSE_RECENTLY_PLAYED}",
                        name=BROWSE_RECENTLY_PLAYED,
                        translation_key="recently_played",
                    ),
                    BrowseFolder(
                        item_id="shaker",
                        provider=self.instance_id,
                        path=f"{base}{BROWSE_SHAKER}",
                        name=BROWSE_SHAKER,
                        translation_key="shaker",
                    ),
                    BrowseFolder(
                        item_id="discover_audiobooks",
                        provider=self.instance_id,
                        path=f"{base}{BROWSE_AUDIOBOOKS}",
                        name=BROWSE_AUDIOBOOKS,
                        translation_key="discover_audiobooks",
                    ),
                    create_virtual_playlist(
                        self.provider, PERSONAL_SONGS_PLAYLIST_ID, "My Uploads"
                    ),
                ]
            )
            return base_items

        # Standard paths handled by base class
        return list(await base_browse(path))

    # -- Made For You --

    async def _browse_made_for_you(
        self, path: str, sub_subpath: str | None
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Route Made For You sub-paths or return the root listing."""
        if sub_subpath in (BROWSE_MOODS, BROWSE_GENRES):
            return await self._browse_flow_configs(sub_subpath.lower())
        if sub_subpath == BROWSE_YOUR_TOP_ARTISTS:
            return await self._browse_user_charts_category("your_top_artists")
        if sub_subpath == BROWSE_YOUR_TOP_ALBUMS:
            return await self._browse_user_charts_category("your_top_albums")
        if sub_subpath == BROWSE_RECOMMENDED_PLAYLISTS:
            return await self._browse_editorial_playlists()
        if sub_subpath == BROWSE_RECOMMENDED_ARTIST_PLAYLISTS:
            return await self._browse_artist_playlists()
        if sub_subpath == BROWSE_PERSONALIZED_PLAYLISTS:
            return await self._get_smart_tracklist_playlists()
        return await self._browse_made_for_me(path)

    async def _browse_made_for_me(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Return Made For You sub-items: Moods, Genres, Top stats, Mixes, Playlists."""
        base = path if path.endswith("/") else path + "/"
        items: list[MediaItemType | BrowseFolder] = [
            BrowseFolder(
                item_id="moods",
                provider=self.instance_id,
                path=f"{base}{BROWSE_MOODS}",
                name=BROWSE_MOODS,
                translation_key="moods",
            ),
            BrowseFolder(
                item_id="genres",
                provider=self.instance_id,
                path=f"{base}{BROWSE_GENRES}",
                name=BROWSE_GENRES,
                translation_key="genres",
            ),
            create_virtual_playlist(self.provider, USER_TOP_TRACKS_PLAYLIST_ID, "Your Top Tracks"),
            create_virtual_playlist(self.provider, RECOMMENDED_TRACKS_PLAYLIST_ID, "Hot Tracks"),
            BrowseFolder(
                item_id="your_top_artists",
                provider=self.instance_id,
                path=f"{base}{BROWSE_YOUR_TOP_ARTISTS}",
                name=BROWSE_YOUR_TOP_ARTISTS,
                translation_key="your_top_artists",
            ),
            BrowseFolder(
                item_id="your_top_albums",
                provider=self.instance_id,
                path=f"{base}{BROWSE_YOUR_TOP_ALBUMS}",
                name=BROWSE_YOUR_TOP_ALBUMS,
                translation_key="your_top_albums",
            ),
            BrowseFolder(
                item_id="mixes",
                provider=self.instance_id,
                path=f"{base}{BROWSE_PERSONALIZED_PLAYLISTS}",
                name=BROWSE_PERSONALIZED_PLAYLISTS,
                translation_key="mixes",
            ),
            BrowseFolder(
                item_id="recommended_playlists",
                provider=self.instance_id,
                path=f"{base}{BROWSE_RECOMMENDED_PLAYLISTS}",
                name=BROWSE_RECOMMENDED_PLAYLISTS,
                translation_key="recommended_playlists",
            ),
            BrowseFolder(
                item_id="recommended_artist_playlists",
                provider=self.instance_id,
                path=f"{base}{BROWSE_RECOMMENDED_ARTIST_PLAYLISTS}",
                name=BROWSE_RECOMMENDED_ARTIST_PLAYLISTS,
                translation_key="recommended_artist_playlists",
            ),
        ]
        return items

    async def _browse_editorial_playlists(self) -> list[Playlist]:
        """Fetch personalized editorial playlists from Deezer recommendations."""
        recs = await self.provider.gql_client.get_recommendations(
            playlists_first=50,
            artist_playlists_first=0,
            new_releases_first=0,
            artists_first=0,
            hot_tracks_limit=0,
        )
        if not recs or not recs.recommendations:
            return []
        return [
            parse_playlist(self.provider, edge.node)
            for edge in recs.recommendations.playlists.edges
            if edge.node is not None
        ]

    async def _browse_artist_playlists(self) -> list[Playlist]:
        """Fetch personalized artist playlists from Deezer recommendations."""
        recs = await self.provider.gql_client.get_recommendations(
            playlists_first=0,
            artist_playlists_first=50,
            new_releases_first=0,
            artists_first=0,
            hot_tracks_limit=0,
        )
        if not recs or not recs.recommendations:
            return []
        return [
            parse_playlist(self.provider, edge.node)
            for edge in recs.recommendations.artist_playlists.edges
            if edge.node is not None
        ]

    # -- Flow configs --

    def _flow_configs_to_playlists(
        self,
        edges: Sequence[
            GetFlowConfigsMeFlowConfigsMoodsEdges
            | GetFlowConfigsMeFlowConfigsGenresEdges
            | SearchFlowsSearchResultsFlowConfigsEdges
        ],
    ) -> list[Playlist]:
        """Convert FlowConfig edges to virtual playlists."""
        return [
            create_virtual_playlist(
                self.provider,
                f"{FLOW_CONFIG_PREFIX}{edge.node.id}",
                f"Flow: {edge.node.title}",
                image_url=get_flow_config_image(edge.node),
            )
            for edge in edges
            if edge.node is not None
        ]

    async def _browse_flow_configs(self, category: str) -> list[Playlist]:
        """
        Fetch mood or genre flow configs and return as virtual playlists.

        :param category: Either "moods" or "genres".
        """
        is_moods = category == "moods"
        all_edges: list[
            GetFlowConfigsMeFlowConfigsMoodsEdges | GetFlowConfigsMeFlowConfigsGenresEdges
        ] = []
        cursor: str | None = None
        while True:
            flow_configs = await self.provider.gql_client.get_flow_configs(
                moods_first=50 if is_moods else 0,
                moods_after=cursor if is_moods else None,
                genres_first=0 if is_moods else 50,
                genres_after=None if is_moods else cursor,
            )
            if not flow_configs or not flow_configs.flow_configs:
                break
            connection = (
                flow_configs.flow_configs.moods if is_moods else flow_configs.flow_configs.genres
            )
            all_edges.extend(connection.edges)
            if not connection.page_info.has_next_page:
                break
            cursor = connection.page_info.end_cursor
        return self._flow_configs_to_playlists(all_edges)

    async def _browse_all_flows(self) -> list[Playlist]:
        """Fetch all available Deezer flows via search and return as virtual playlists."""
        all_edges: list[SearchFlowsSearchResultsFlowConfigsEdges] = []
        cursor: str | None = None
        while True:
            result = await self.provider.gql_client.search_flows(
                query="flow", first=100, after=cursor
            )
            if not result:
                break
            edges = result.results.flow_configs.edges
            all_edges.extend(edges)
            if not result.results.flow_configs.page_info.has_next_page:
                break
            cursor = result.results.flow_configs.page_info.end_cursor
        return self._flow_configs_to_playlists(all_edges)

    # -- Explore --

    async def _browse_explore_root(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Return Explore section: charts, top content, all flows."""
        base = path if path.endswith("/") else path + "/"
        charts_cover = None
        charts = await self.provider.gql_client.get_charts(tracks_first=1)
        if charts and charts.country and charts.country.tracks:
            for edge in charts.country.tracks.edges:
                if edge.node and edge.node.album and edge.node.album.cover:
                    if edge.node.album.cover.urls:
                        charts_cover = edge.node.album.cover.urls[0]
                    break
        return [
            create_virtual_playlist(
                self.provider, TOP_CHARTS_PLAYLIST_ID, "Top Charts", image_url=charts_cover
            ),
            BrowseFolder(
                item_id="top_albums",
                provider=self.instance_id,
                path=f"{base}{BROWSE_TOP_ALBUMS}",
                name=BROWSE_TOP_ALBUMS,
                translation_key="top_albums",
            ),
            BrowseFolder(
                item_id="top_artists",
                provider=self.instance_id,
                path=f"{base}{BROWSE_TOP_ARTISTS}",
                name=BROWSE_TOP_ARTISTS,
                translation_key="top_artists",
            ),
            BrowseFolder(
                item_id="top_playlists",
                provider=self.instance_id,
                path=f"{base}{BROWSE_TOP_PLAYLISTS}",
                name=BROWSE_TOP_PLAYLISTS,
                translation_key="top_playlists",
            ),
            BrowseFolder(
                item_id="all_flows",
                provider=self.instance_id,
                path=f"{base}{BROWSE_ALL_FLOWS}",
                name=BROWSE_ALL_FLOWS,
                translation_key="all_flows",
            ),
        ]

    async def _browse_explore_category(self, category: str) -> list[MediaItemType]:
        """Fetch items for an Explore sub-category."""
        if category == BROWSE_ALL_FLOWS:
            return list(await self._browse_all_flows())
        items: list[MediaItemType] = []
        if category in (BROWSE_TOP_ALBUMS, BROWSE_TOP_ARTISTS, BROWSE_TOP_PLAYLISTS):
            charts = await self.provider.gql_client.get_charts(tracks_first=0)
            if not charts or not charts.country:
                return []
            country = charts.country
            if category == BROWSE_TOP_ALBUMS and country.albums:
                for album_edge in country.albums.edges:
                    if album_edge.node is not None:
                        items.append(parse_album(self.provider, album_edge.node))
            elif category == BROWSE_TOP_ARTISTS and country.artists:
                for artist_edge in country.artists.edges:
                    if artist_edge.node is not None:
                        items.append(parse_artist(self.provider, artist_edge.node))
            elif category == BROWSE_TOP_PLAYLISTS and country.playlists:
                for playlist_edge in country.playlists.edges:
                    if playlist_edge.node is not None:
                        items.append(parse_playlist(self.provider, playlist_edge.node))
        return items

    async def _browse_user_charts_category(self, category: str) -> list[MediaItemType]:
        """Fetch user chart items (top artists/albums)."""
        result = await self.provider.gql_client.get_user_charts()
        if not result:
            return []
        charts = result.charts
        items: list[MediaItemType] = []
        if category == "your_top_artists" and charts.artists:
            for artist_edge in charts.artists.edges:
                if artist_edge.node is not None:
                    items.append(parse_artist(self.provider, artist_edge.node))
        elif category == "your_top_albums" and charts.albums:
            for album_edge in charts.albums.edges:
                if album_edge.node is not None:
                    items.append(parse_album(self.provider, album_edge.node))
        return items

    # -- Shaker (Music Together) --

    async def _browse_shaker_root(self, path: str) -> list[BrowseFolder]:
        """Return Shaker (Music Together) groups as browse folders."""
        base = path if path.endswith("/") else path + "/"
        folders: list[BrowseFolder] = []
        cursor: str | None = None
        while True:
            result = await self.provider.gql_client.get_music_together_groups(
                first=50, after=cursor
            )
            if not result:
                break
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
            if not result.music_together_groups.page_info.has_next_page:
                break
            cursor = result.music_together_groups.page_info.end_cursor
        return folders

    async def _browse_shaker_group(self, group_id: str) -> list[MediaItemType]:
        """Return playlists for a Shaker group: mix + curated tracklist."""
        group = await self.provider.gql_client.get_music_together_group(
            group_id=group_id,
            mood=MusicTogetherSuggestedTracklistMoodInput.NONE,
            tracks_first=1,
        )
        if group is None:
            return []
        items: list[MediaItemType] = []
        if group.suggested_tracklist and group.suggested_tracklist.tracklist:
            items.append(
                create_virtual_playlist(
                    self.provider,
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
                create_virtual_playlist(
                    self.provider,
                    f"{SHAKER_CURATED_PREFIX}{group_id}",
                    f"{group.name} - Playlist",
                    image_url=cover_url,
                )
            )
        return items

    # -- Audiobooks --

    async def _browse_audiobooks_root(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Return audiobook sections from the Deezer audiobooks channel page."""
        page_data = await self.provider.gw_client.get_page(AUDIOBOOKS_CHANNEL)
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
                    folder.image = get_gw_item_image(self.provider, item)
                    items.append(folder)
            else:
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
                folder.image = get_gw_item_image(self.provider, first_item)
                items.append(folder)

        return items

    async def _browse_audiobooks_page(self, page_path: str) -> list[MediaItemType | BrowseFolder]:
        """Fetch a Deezer channel page and return its items."""
        page_data = await self.provider.gw_client.get_page(page_path)
        sections = page_data.get("sections", [])
        items: list[MediaItemType | BrowseFolder] = []
        for section in sections:
            for item in section.get("items", []):
                if parsed := parse_gw_item(self.provider, item):
                    items.append(parsed)
        return items

    # -- Recommendations --

    @use_cache(3600)
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get Deezer's recommendations including Flow and personalized content."""
        result: list[RecommendationFolder] = []
        recs = await self.provider.gql_client.get_recommendations(
            playlists_first=50,
            artist_playlists_first=50,
            new_releases_first=10,
            artists_first=0,
            hot_tracks_limit=50,
        )
        await self._add_made_for_you(result, recs)
        self._add_recommended_playlists(result, recs)
        self._add_recommended_artist_playlists(result, recs)
        self._add_recommended_tracks(result, recs)
        self._add_new_releases(result, recs)
        await self._add_flow_configs(result)
        recently_played = await self._get_recently_played_items()
        if recently_played:
            result.append(
                RecommendationFolder(
                    item_id="recently_played",
                    provider=self.instance_id,
                    name=BROWSE_RECENTLY_PLAYED,
                    translation_key="recently_played",
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
        made_for_me_items: list[Playlist] = [
            create_virtual_playlist(
                self.provider, FLOW_PLAYLIST_ID, "Flow", image_url=await self._get_flow_cover()
            )
        ]
        made_for_me_items.extend(await self._get_smart_tracklist_playlists())
        if made_for_me_items:
            result.append(
                RecommendationFolder(
                    item_id="made_for_you",
                    provider=self.instance_id,
                    name=BROWSE_MADE_FOR_YOU,
                    translation_key="made_for_you",
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
        track_items = [parse_track(self.provider, ht) for ht in recs.recommendations.hot_tracks]
        if track_items:
            result.append(
                RecommendationFolder(
                    item_id="recommended_tracks",
                    provider=self.instance_id,
                    name="Hot Tracks",
                    translation_key="recommended_tracks",
                    items=UniqueList(track_items),
                )
            )

    def _add_recommended_playlists(
        self,
        result: list[RecommendationFolder],
        recs: GetRecommendationsMe | None,
    ) -> None:
        """Add Recommended Playlists section (editorial playlists)."""
        if not recs or not recs.recommendations:
            return
        items = [
            parse_playlist(self.provider, edge.node)
            for edge in recs.recommendations.playlists.edges
            if edge.node is not None
        ]
        if items:
            result.append(
                RecommendationFolder(
                    item_id="recommended_playlists",
                    provider=self.instance_id,
                    name=BROWSE_RECOMMENDED_PLAYLISTS,
                    translation_key="recommended_playlists",
                    items=UniqueList(items),
                )
            )

    def _add_recommended_artist_playlists(
        self,
        result: list[RecommendationFolder],
        recs: GetRecommendationsMe | None,
    ) -> None:
        """Add Recommended Artist Playlists section."""
        if not recs or not recs.recommendations:
            return
        items = [
            parse_playlist(self.provider, edge.node)
            for edge in recs.recommendations.artist_playlists.edges
            if edge.node is not None
        ]
        if items:
            result.append(
                RecommendationFolder(
                    item_id="recommended_artist_playlists",
                    provider=self.instance_id,
                    name=BROWSE_RECOMMENDED_ARTIST_PLAYLISTS,
                    translation_key="recommended_artist_playlists",
                    items=UniqueList(items),
                )
            )

    def _add_new_releases(
        self,
        result: list[RecommendationFolder],
        recs: GetRecommendationsMe | None,
    ) -> None:
        """Add New Releases section to recommendations."""
        if recs is None:
            return
        new_release_items = [
            parse_album(self.provider, edge.node)
            for edge in recs.recommendations.new_releases.edges
            if edge.node is not None
        ]
        if new_release_items:
            result.append(
                RecommendationFolder(
                    item_id="new_releases",
                    provider=self.instance_id,
                    name="New Releases",
                    translation_key="new_releases",
                    items=UniqueList(new_release_items),
                )
            )

    async def _add_flow_configs(self, result: list[RecommendationFolder]) -> None:
        """Add Mood and Genre Flow sections to recommendations."""
        flow_configs = await self.provider.gql_client.get_flow_configs(
            moods_first=20, genres_first=20
        )
        if not flow_configs or not flow_configs.flow_configs:
            return
        configs = flow_configs.flow_configs
        for folder_id, folder_name, edges in (
            ("mood_flows", "Deezer Mood Flows", configs.moods.edges),
            ("genre_flows", "Deezer Genre Flows", configs.genres.edges),
        ):
            playlists = self._flow_configs_to_playlists(list(edges))
            if playlists:
                result.append(
                    RecommendationFolder(
                        item_id=folder_id,
                        provider=self.instance_id,
                        name=folder_name,
                        translation_key=folder_id,
                        items=UniqueList(playlists),
                    )
                )

    # -- Recently played (shared by browse and recommendations) --

    @use_cache(3600)
    async def _get_recently_played_items(self) -> list[MediaItemType]:
        """Get recently played items (cached)."""
        result = await self.provider.gql_client.get_recently_played(first=50)
        if not result:
            return []
        return parse_recently_played_edges(self.provider, result.recently_played.edges)

    # -- Virtual playlist metadata --

    async def get_virtual_playlist(self, prov_playlist_id: str) -> Playlist | None:
        """Return a virtual playlist, or None if the ID is not virtual."""
        if prov_playlist_id == FLOW_PLAYLIST_ID:
            cover = await self._get_flow_cover()
            return create_virtual_playlist(self.provider, FLOW_PLAYLIST_ID, "Flow", image_url=cover)
        if prov_playlist_id == RECOMMENDED_TRACKS_PLAYLIST_ID:
            return create_virtual_playlist(
                self.provider, RECOMMENDED_TRACKS_PLAYLIST_ID, "Hot Tracks"
            )
        if prov_playlist_id == TOP_CHARTS_PLAYLIST_ID:
            return create_virtual_playlist(self.provider, TOP_CHARTS_PLAYLIST_ID, "Top Charts")
        if prov_playlist_id == USER_TOP_TRACKS_PLAYLIST_ID:
            return create_virtual_playlist(
                self.provider, USER_TOP_TRACKS_PLAYLIST_ID, "Your Top Tracks"
            )
        if prov_playlist_id == PERSONAL_SONGS_PLAYLIST_ID:
            return create_virtual_playlist(self.provider, PERSONAL_SONGS_PLAYLIST_ID, "My Uploads")
        if prov_playlist_id.startswith(FLOW_CONFIG_PREFIX):
            config_id = prov_playlist_id.removeprefix(FLOW_CONFIG_PREFIX)
            flow_config = await self.provider.gql_client.get_flow_config_tracks(
                flow_config_id=config_id
            )
            name = f"Flow: {flow_config.title}" if flow_config else f"Flow: {config_id}"
            cover = get_flow_config_image(flow_config) if flow_config else None
            return create_virtual_playlist(self.provider, prov_playlist_id, name, image_url=cover)
        if prov_playlist_id.startswith(SMART_TRACKLIST_PREFIX):
            tracklist_id = prov_playlist_id.removeprefix(SMART_TRACKLIST_PREFIX)
            tracklist = await self.provider.gql_client.get_smart_tracklist(
                smart_tracklist_id=tracklist_id, first=1
            )
            name = tracklist.title if tracklist else f"Mix {tracklist_id}"
            cover = (
                tracklist.cover.urls[0]
                if tracklist and tracklist.cover and tracklist.cover.urls
                else None
            )
            return create_virtual_playlist(
                self.provider,
                prov_playlist_id,
                name,
                image_url=cover,
            )
        if prov_playlist_id.startswith(SHAKER_CURATED_PREFIX):
            group_id = prov_playlist_id.removeprefix(SHAKER_CURATED_PREFIX)
            group = await self.provider.gql_client.get_music_together_group(
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
            return create_virtual_playlist(
                self.provider,
                prov_playlist_id,
                name,
                image_url=cover_url,
            )
        if prov_playlist_id.startswith(SHAKER_PREFIX):
            group_id = prov_playlist_id.removeprefix(SHAKER_PREFIX)
            group = await self.provider.gql_client.get_music_together_group(
                group_id=group_id,
                mood=MusicTogetherSuggestedTracklistMoodInput.NONE,
                tracks_first=1,
            )
            name = f"{group.name} - Mix" if group else f"Shaker {group_id}"
            return create_virtual_playlist(
                self.provider, prov_playlist_id, name, image_url=SHAKER_MIX_COVER
            )
        return None

    # -- Virtual playlist track fetchers --

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks, routing virtual playlist IDs to their fetchers."""
        if page > 0:
            return []
        if prov_playlist_id == FLOW_PLAYLIST_ID:
            return await self._get_flow_config_tracks(DEFAULT_FLOW_CONFIG_ID)
        if prov_playlist_id == RECOMMENDED_TRACKS_PLAYLIST_ID:
            return await self._get_recommended_tracks()
        if prov_playlist_id == TOP_CHARTS_PLAYLIST_ID:
            return await self._get_chart_tracks()
        if prov_playlist_id == USER_TOP_TRACKS_PLAYLIST_ID:
            return await self._get_user_chart_tracks()
        if prov_playlist_id == PERSONAL_SONGS_PLAYLIST_ID:
            return await self._get_personal_songs()
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

    @use_cache(3600)
    async def _get_smart_tracklist_playlists(self) -> list[Playlist]:
        """Get SmartTracklist items from Made For Me as virtual playlists."""
        made_for_me = await self.provider.gql_client.get_made_for_me(first=20)
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
                    create_virtual_playlist(
                        self.provider,
                        f"{SMART_TRACKLIST_PREFIX}{edge.node.id}",
                        edge.node.title,
                        image_url=cover,
                    )
                )
        return playlists

    @use_cache(3600)
    async def _get_recommended_tracks(self) -> list[Track]:
        """Get cached recommended tracks (hot tracks)."""
        recs = await self.provider.gql_client.get_recommendations(
            playlists_first=0,
            artist_playlists_first=0,
            new_releases_first=0,
            artists_first=0,
            hot_tracks_limit=50,
        )
        if recs is None or recs.recommendations.hot_tracks is None:
            return []
        return [parse_track(self.provider, ht) for ht in recs.recommendations.hot_tracks]

    @use_cache(3600)
    async def _get_chart_tracks(self) -> list[Track]:
        """Get cached chart tracks."""
        charts = await self.provider.gql_client.get_charts(
            country_code=self.provider.gw_client.user_country,
            tracks_first=100,
        )
        if charts is None or charts.country is None or charts.country.tracks is None:
            return []
        return [
            parse_track(self.provider, edge.node)
            for edge in charts.country.tracks.edges
            if edge.node is not None
        ]

    async def _get_flow_config_tracks(self, config_id: str) -> list[Track]:
        """
        Get fresh batches of tracks for a Flow config, merged deduplicated.

        :param config_id: The Flow config identifier
            (e.g. "default", "happy", "chill", "genre-rock").
        """
        seen: set[str] = set()
        tracks: list[Track] = []
        for _ in range(FLOW_BATCH_COUNT):
            result = await self.provider.gql_client.get_flow_config_tracks(flow_config_id=config_id)
            if result is None or not result.tracks:
                break
            for ft in result.tracks:
                if ft.track is not None and ft.track.id not in seen:
                    seen.add(ft.track.id)
                    tracks.append(parse_track(self.provider, ft.track))
        return filter_tracks(tracks)

    @use_cache(3600)
    async def _get_smart_tracklist_tracks(self, tracklist_id: str) -> list[Track]:
        """
        Get tracks for a SmartTracklist.

        :param tracklist_id: The SmartTracklist identifier.
        """
        all_tracks: list[Track] = []
        cursor: str | None = None
        while True:
            result = await self.provider.gql_client.get_smart_tracklist(
                smart_tracklist_id=tracklist_id, first=50, after=cursor
            )
            if result is None:
                break
            all_tracks.extend(
                parse_track(self.provider, edge.node)
                for edge in result.tracks.edges
                if edge.node is not None
            )
            if not result.tracks.page_info.has_next_page:
                break
            cursor = result.tracks.page_info.end_cursor
        return all_tracks

    async def _get_shaker_tracks(self, group_id: str) -> list[Track]:
        """
        Get suggested tracks for a Shaker (Music Together) group.

        :param group_id: The Music Together group identifier.
        """
        # Refresh the suggested tracklist to get a fresh set of tracks
        await self.provider.gql_client.music_together_refresh_suggested_tracklist(
            group_id=group_id,
            mood=MusicTogetherRefreshSuggestedTracklistMoodInput.NONE,
        )
        group = await self.provider.gql_client.get_music_together_group(
            group_id=group_id,
            mood=MusicTogetherSuggestedTracklistMoodInput.NONE,
            tracks_first=50,
        )
        if group is None or group.suggested_tracklist is None:
            return []
        tracklist = group.suggested_tracklist.tracklist
        if tracklist is None:
            return []
        return filter_tracks(
            [parse_track(self.provider, edge.node) for edge in tracklist.tracks.edges if edge.node]
        )

    async def _get_shaker_curated_tracks(self, group_id: str) -> list[Track]:
        """
        Get curated playlist tracks for a Shaker (Music Together) group.

        :param group_id: The Music Together group identifier.
        """
        all_tracks: list[Track] = []
        cursor: str | None = None
        while True:
            group = await self.provider.gql_client.get_music_together_group(
                group_id=group_id,
                mood=MusicTogetherSuggestedTracklistMoodInput.NONE,
                tracks_first=50,
                tracks_after=cursor,
            )
            if group is None or group.curated_tracklist is None:
                break
            tracks_conn = group.curated_tracklist.tracks
            all_tracks.extend(
                parse_track(self.provider, edge.node) for edge in tracks_conn.edges if edge.node
            )
            if not tracks_conn.page_info.has_next_page:
                break
            cursor = tracks_conn.page_info.end_cursor
        return all_tracks

    @use_cache(3600)
    async def _get_user_chart_tracks(self) -> list[Track]:
        """Get the user's most listened tracks."""
        result = await self.provider.gql_client.get_user_charts(tracks_first=50)
        if not result or not result.charts.tracks:
            return []
        return [
            parse_track(self.provider, edge.node)
            for edge in result.charts.tracks.edges
            if edge.node is not None
        ]

    async def _get_personal_songs(self) -> list[Track]:
        """Get user-uploaded personal songs via the GW API."""
        songs = await self.provider.media_manager._get_personal_songs()
        return [
            parse_gw_track(self.provider, song, position=idx) for idx, song in enumerate(songs, 1)
        ]

    async def invalidate_playlist_cache(self, prov_playlist_id: str) -> None:
        """Invalidate the cached playlist tracks after a mutation."""
        cache_key = f"_get_regular_playlist_tracks.{prov_playlist_id}"
        await self.mass.cache.delete(key=cache_key, provider=self.instance_id)

    @use_cache(3600 * 3)
    async def _get_regular_playlist_tracks(self, prov_playlist_id: str) -> list[Track]:
        """Get tracks for regular Deezer playlists (cached)."""
        result = await self.provider.gql_client.get_playlist(playlist_id=prov_playlist_id)
        if result is None:
            return []
        all_edges = list(result.tracks.edges)
        while result.tracks.page_info.has_next_page:
            result = await self.provider.gql_client.get_playlist(
                playlist_id=prov_playlist_id,
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

    @use_cache(3600)
    async def _get_flow_cover(self) -> str | None:
        """Get the cover URL for the user's default Flow."""
        result = await self.provider.gql_client.get_flow_config_tracks(
            flow_config_id=DEFAULT_FLOW_CONFIG_ID
        )
        return get_flow_config_image(result) if result else None
