"""
Deezer provider - provider facade.

Thin facade that delegates to specialized manager classes:
- DeezerMediaManager: library, search, item getters, mutations
- DeezerBrowseManager: browse tree, recommendations, virtual playlists
- DeezerStreamingManager: streaming, decryption, playback callbacks
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from deezer_python_gql import DeezerGQLClient, GraphQLClientAuthError, GraphQLClientError
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin

from .browse import DeezerBrowseManager
from .gw_client import (
    DeezerGWAuthError,
    DeezerGWError,
    DeezerGWNoSubscriptionError,
    GWClient,
)
from .media import DeezerMediaManager
from .streaming import DeezerStreamingManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry
    from music_assistant_models.media_items import (
        Album,
        Artist,
        Audiobook,
        BrowseFolder,
        ItemMapping,
        MediaItemType,
        Playlist,
        Podcast,
        PodcastEpisode,
        Radio,
        RecommendationFolder,
        SearchResults,
        Track,
        UniqueList,
    )
    from music_assistant_models.streamdetails import StreamDetails

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
    ProviderFeature.ARTIST_TOPALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.LYRICS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
}

CONF_ARL_TOKEN = "arl_token"


class DeezerProvider(RecommendationPayloadMixin, MusicProvider):
    """
    Deezer provider support.

    Delegates to specialized manager classes for clean separation of concerns.
    """

    gql_client: DeezerGQLClient
    gw_client: GWClient
    media_manager: DeezerMediaManager
    browse_manager: DeezerBrowseManager
    streaming_manager: DeezerStreamingManager
    user_id: str

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def handle_async_init(self) -> None:
        """Handle async init of the Deezer provider."""
        arl_token = str(self.get_setup_value(CONF_ARL_TOKEN))

        try:
            self.gql_client = DeezerGQLClient(arl=arl_token, session=self.mass.http_session)
            logging.getLogger("deezer_python_gql").setLevel(self.logger.level + 10)
            me = await self.gql_client.get_me()
            if not me:
                msg = "Authentication returned no user data"
                raise GraphQLClientError(msg)
            self.user_id = me.id
            self.gw_client = GWClient(self.mass.http_session, arl_token)
            await self.gw_client.setup()
        # Order matters: the specific errors below subclass the generic ones caught last.
        # Every branch logs the cause, otherwise the reason is lost -- clients only ever
        # see the translated message, never the exception text.
        except DeezerGWNoSubscriptionError as err:
            self.logger.error("Deezer account has no streamable subscription: %s", err)
            raise LoginFailed(
                "This Deezer account has no subscription that Music Assistant can stream from.",
                translation_key="no_subscription",
                translation_owner=self.translation_owner,
            ) from err
        except DeezerGWAuthError as err:
            self.logger.error("Deezer GW API rejected the ARL token: %s", err)
            raise LoginFailed(
                "Deezer did not accept the ARL token.",
                translation_key="arl_rejected",
                translation_owner=self.translation_owner,
            ) from err
        except GraphQLClientAuthError as err:
            self.logger.error("Deezer auth service rejected the ARL token: %s", err)
            raise LoginFailed(
                "Deezer did not accept the ARL token.",
                translation_key="arl_rejected",
                translation_owner=self.translation_owner,
            ) from err
        except (GraphQLClientError, DeezerGWError) as err:
            self.logger.error("Deezer authentication failed: %s", err)
            raise LoginFailed(
                "Deezer authentication failed. Please check your ARL token.",
                translation_key="auth_failed",
                translation_owner=self.translation_owner,
            ) from err

        self.media_manager = DeezerMediaManager(self)
        self.browse_manager = DeezerBrowseManager(self)
        self.streaming_manager = DeezerStreamingManager(self)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await super().unload(is_removed)

    # -- Library retrieval --

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve all library artists from Deezer."""
        async for item in self.media_manager.get_library_artists():
            yield item

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve all library albums from Deezer."""
        async for item in self.media_manager.get_library_albums():
            yield item

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve all library playlists from Deezer."""
        async for item in self.media_manager.get_library_playlists():
            yield item

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve all library tracks from Deezer."""
        async for item in self.media_manager.get_library_tracks():
            yield item

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve library/subscribed podcasts from Deezer."""
        async for item in self.media_manager.get_library_podcasts():
            yield item

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Retrieve library/subscribed audiobooks from Deezer."""
        async for item in self.media_manager.get_library_audiobooks():
            yield item

    # -- Search --

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on music provider."""
        return await self.media_manager.search(search_query, media_types, limit)

    # -- Item getters --

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return await self.media_manager.get_artist(prov_artist_id)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return await self.media_manager.get_album(prov_album_id)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return await self.media_manager.get_track(prov_track_id)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        return await self.media_manager.get_playlist(prov_playlist_id)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio/livestream details by id."""
        return await self.media_manager.get_radio(prov_radio_id)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        return await self.media_manager.get_podcast(prov_podcast_id)

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        return await self.media_manager.get_podcast_episode(prov_episode_id)

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        return await self.media_manager.get_audiobook(prov_audiobook_id)

    # -- Content getters --

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks in an album."""
        return await self.media_manager.get_album_tracks(prov_album_id)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        return await self.browse_manager.get_playlist_tracks(prov_playlist_id, page)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        return await self.media_manager.get_artist_albums(prov_artist_id)

    async def get_artist_topalbums(self, prov_artist_id: str) -> list[Album]:
        """Get top albums of an artist."""
        return await self.media_manager.get_artist_topalbums(prov_artist_id)

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks of an artist."""
        return await self.media_manager.get_artist_toptracks(prov_artist_id)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all episodes for a given podcast."""
        async for ep in self.media_manager.get_podcast_episodes(prov_podcast_id):
            yield ep

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        return await self.media_manager.get_similar_tracks(prov_track_id, limit)

    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Retrieve a list of artists similar to the provided artist."""
        return await self.media_manager.get_similar_artists(prov_artist_id, limit)

    # -- Library mutations --

    async def library_add(self, item: MediaItemType) -> bool:
        """Add an item to the provider's library/favorites."""
        return await self.media_manager.library_add(item)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item from the provider's library/favorites."""
        return await self.media_manager.library_remove(prov_item_id, media_type)

    # -- Playlist CRUD --

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        await self.media_manager.add_playlist_tracks(prov_playlist_id, prov_track_ids)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        await self.media_manager.remove_playlist_tracks(prov_playlist_id, positions_to_remove)

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        return await self.media_manager.create_playlist(name, media_types)

    # -- Browse & Recommendations --

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse Deezer content."""
        return await self.browse_manager.browse(path, super().browse)

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get Deezer's available recommendation rows, without items."""
        return await self.browse_manager.get_recommendations()

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        return await self.browse_manager.get_recommendation_items(item_id)

    # -- Streaming --

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Get the resume position for a podcast episode."""
        return await self.streaming_manager.get_resume_position(item_id, media_type)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Handle callback when a podcast episode has been played or is playing."""
        await self.streaming_manager.on_played(
            media_type, prov_item_id, fully_played, position, media_item, is_playing
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        return await self.streaming_manager.get_stream_details(item_id, media_type)

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Return the audio stream for the provider item."""
        async for chunk in self.streaming_manager.get_audio_stream(streamdetails, seek_position):
            yield chunk

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Handle callback when an item completed streaming."""
        await self.streaming_manager.on_streamed(streamdetails)

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch the recommendation folders fed by the shared gql recommendations payload."""
        return await self.browse_manager._fetch_recommendation_payload()
