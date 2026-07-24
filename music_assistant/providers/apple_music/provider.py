"""Apple Music provider implementation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin

from .api_client import AppleMusicAPIClient
from .constants import (
    CONF_MUSIC_APP_TOKEN,
    CONF_MUSIC_USER_MANUAL_TOKEN,
    CONF_MUSIC_USER_TOKEN,
    SUPPORTED_FEATURES,
)
from .helpers import browse_playlists
from .library import AppleMusicLibraryManager
from .media import AppleMusicMediaManager
from .recommendations import AppleMusicRecommendationManager
from .streaming import AppleMusicStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import MediaType
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant import MusicAssistant


class AppleMusicProvider(RecommendationPayloadMixin, MusicProvider):
    """Implementation of an Apple Music MusicProvider."""

    _music_user_token: str | None = None
    _music_app_token: str | None = None
    _storefront: str | None = None

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize Apple Music provider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self.api_client = AppleMusicAPIClient(self)
        self.library_manager = AppleMusicLibraryManager(self)
        self.media_manager = AppleMusicMediaManager(self)
        self.recommendation_manager = AppleMusicRecommendationManager(self)
        self.streaming_manager = AppleMusicStreamingManager(self)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._music_user_token = cast(
            "str | None",
            self.config.get_value(CONF_MUSIC_USER_MANUAL_TOKEN)
            or self.config.get_value(CONF_MUSIC_USER_TOKEN),
        )
        self._music_app_token = cast("str | None", self.config.get_value(CONF_MUSIC_APP_TOKEN))
        self._storefront = await self.api_client.get_user_storefront()
        await self.streaming_manager.initialize()

    # ------------------------------------------------------------------
    # Browse / search / recommendations
    # ------------------------------------------------------------------

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse Apple Music with support for playlist folders and radio stations."""
        sub_path = path.split("://", 1)[1] if "://" in path else ""
        path_parts = [part for part in sub_path.split("/") if part]
        if not path_parts:
            items = list(await super().browse(path))
            items.append(
                BrowseFolder(
                    item_id="stations",
                    provider=self.instance_id,
                    path=f"{self.instance_id}://stations",
                    name="Radio Stations",
                    translation_key="radio_stations",
                )
            )
            return items
        if path_parts[0] == "playlists":
            return await browse_playlists(self, path, path_parts)
        if path_parts[0] == "stations":
            return await self.recommendation_manager.browse_stations()
        return await super().browse(path)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] | None,
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider."""
        return await self.media_manager.search(search_query, media_types, limit)

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        return await self._recommendation_rows_from_payload()

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        return await self._recommendation_items_from_payload(item_id)

    # ------------------------------------------------------------------
    # Media item getters
    # ------------------------------------------------------------------

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return await self.media_manager.get_artist(prov_artist_id)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return await self.media_manager.get_album(prov_album_id)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return await self.media_manager.get_track(prov_track_id)

    async def get_playlist(self, prov_playlist_id: str, is_favourite: bool = False) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id.startswith("ra."):
            return await self.recommendation_manager.get_station_playlist(prov_playlist_id)
        return await self.media_manager.get_playlist(prov_playlist_id, is_favourite)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        return await self.media_manager.get_album_tracks(prov_album_id)

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve an artwork token to a freshly signed artwork URL."""
        media_type, _, item_id = path.partition("/")
        return await self.media_manager.get_artwork_url(media_type, item_id) or ""

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        return await self.media_manager.get_playlist_tracks(prov_playlist_id, page)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        return await self.media_manager.get_artist_albums(prov_artist_id)

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        return await self.media_manager.get_artist_toptracks(prov_artist_id)

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        return await self.recommendation_manager.get_similar_tracks(prov_track_id, limit)

    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Retrieve a list of artists similar to the provided artist."""
        return await self.recommendation_manager.get_similar_artists(prov_artist_id, limit)

    # ------------------------------------------------------------------
    # Library generators
    # ------------------------------------------------------------------

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from the provider."""
        async for item in self.library_manager.get_library_artists():
            yield item

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        async for item in self.library_manager.get_library_albums():
            yield item

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from the provider."""
        async for item in self.library_manager.get_library_tracks():
            yield item

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve playlists from the provider."""
        async for item in self.library_manager.get_library_playlists():
            yield item

    # ------------------------------------------------------------------
    # Library mutations
    # ------------------------------------------------------------------

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to library."""
        await self.library_manager.library_add(item)
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from library."""
        await self.library_manager.library_remove(prov_item_id, media_type)
        return True

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        await self.library_manager.add_playlist_tracks(prov_playlist_id, prov_track_ids)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        await self.library_manager.remove_playlist_tracks(prov_playlist_id, positions_to_remove)

    async def set_favorite(self, prov_item_id: str, media_type: MediaType, favorite: bool) -> None:
        """Set the favorite status of an item."""
        await self.library_manager.set_favorite(prov_item_id, media_type, favorite)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        return await self.streaming_manager.get_stream_details(item_id)

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch and parse the full recommendations payload (folders with items)."""
        return await self.recommendation_manager.get_personal_recommendations()
