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

from aiohttp import ClientError
from deezer_python_gql import DeezerGQLClient, GraphQLClientAuthError, GraphQLClientError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.music.constants import (
    CACHE_CATEGORY_LISTINGS,
    CACHE_CATEGORY_SEARCH_RESULTS,
)
from music_assistant.helpers.app_vars import app_var
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin

from .browse import DeezerBrowseManager
from .constants import (
    BROWSE_AUDIOBOOKS,
    BROWSE_EXPLORE,
    BROWSE_MADE_FOR_YOU,
    BROWSE_SHAKER,
    DECRYPT_KEY_ERROR,
    DECRYPT_KEY_LENGTH,
    PERSONAL_SONGS_PLAYLIST_ID,
    USER_TOP_TRACKS_PLAYLIST_ID,
)
from .explicit_filter import (
    CONF_EXPLICIT_CONTENT,
    CONF_HIDE_EXPLICIT_ALBUMS,
    ExplicitContentSetting,
    ExplicitMode,
    fetch_account_explicit_mode,
    filtered_media_types,
    is_explicit,
    without_explicit,
)
from .gw_client import (
    DeezerGWAuthError,
    DeezerGWError,
    DeezerGWNoSubscriptionError,
    GWClient,
)
from .helpers import get_virtual_playlist_meta
from .media import DeezerMediaManager
from .rest_client import DeezerRESTClient
from .streaming import DeezerStreamingManager

if TYPE_CHECKING:
    from music_assistant_models.enums import ExternalID
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
    )
    from music_assistant_models.streamdetails import StreamDetails

SUPPORTED_FEATURES = {
    ProviderFeature.TRACK_BY_EXTERNAL_ID,
    ProviderFeature.ALBUM_BY_EXTERNAL_ID,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.FAVORITE_TRACKS_EDIT,
    ProviderFeature.FAVORITE_ARTISTS_EDIT,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_MOVE,
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

CONF_ARL_TOKEN = "arl_token"  # noqa: S105 - config key
CONF_FAMILY_PROFILE = "family_profile"

_EXPLICIT_FILTER_CACHE_KEY = "explicit_filter"
_EXPLICIT_FILTER_CACHE_EXPIRATION = 3600 * 24 * 365
# the browse folders whose content is chosen by Deezer's recommendations
_RECOMMENDATION_BROWSE_FOLDERS = frozenset(
    {BROWSE_MADE_FOR_YOU, BROWSE_EXPLORE, BROWSE_SHAKER, BROWSE_AUDIOBOOKS}
)


class DeezerProvider(RecommendationPayloadMixin, MusicProvider):
    """
    Deezer provider support.

    Delegates to specialized manager classes for clean separation of concerns.
    """

    gql_client: DeezerGQLClient
    gw_client: GWClient
    rest_client: DeezerRESTClient
    media_manager: DeezerMediaManager
    browse_manager: DeezerBrowseManager
    streaming_manager: DeezerStreamingManager
    user_id: str
    explicit_mode: ExplicitMode = ExplicitMode.SHOW
    explicit_media_types: frozenset[MediaType] = filtered_media_types(hide_albums=False)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_EXPLICIT_CONTENT,
                type=ConfigEntryType.STRING,
                options=[ConfigValueOption(setting.value) for setting in ExplicitContentSetting],
                default_value=ExplicitContentSetting.ACCOUNT.value,
            ),
            ConfigEntry(
                key=CONF_HIDE_EXPLICIT_ALBUMS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                depends_on=CONF_EXPLICIT_CONTENT,
                depends_on_value_not=ExplicitContentSetting.SHOW.value,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async init of the Deezer provider."""
        arl_token = str(self.get_setup_value(CONF_ARL_TOKEN))
        # a Family profile has no ARL of its own, it is reached through the admin's ARL
        family_profile = str(self.get_setup_value(CONF_FAMILY_PROFILE) or "")

        try:
            self.gql_client = DeezerGQLClient(
                arl=arl_token, session=self.mass.http_session, account_id=family_profile
            )
            logging.getLogger("deezer_python_gql").setLevel(self.logger.level + 10)
            me = await self.gql_client.get_me()
            if not me:
                msg = "Authentication returned no user data"
                raise GraphQLClientError(msg)  # noqa: TRY301 - converted to LoginFailed below
            self.user_id = me.id
            self.gw_client = GWClient(self.mass.http_session, arl_token, family_profile)
            await self.gw_client.setup()
        except DeezerGWNoSubscriptionError as err:
            self.logger.error("Deezer account has no streamable subscription: %s", err)
            raise LoginFailed(
                "This Deezer account has no subscription that Music Assistant can stream from.",
                translation_key="no_subscription",
                translation_owner=self.translation_owner,
            ) from err
        except DeezerGWAuthError as err:
            self.logger.error("Deezer GW authentication failed: %s", err)
            raise LoginFailed(
                "Deezer could not establish a playback session for this account.",
                translation_key="gw_no_session",
                translation_owner=self.translation_owner,
            ) from err
        except GraphQLClientAuthError as err:
            self.logger.error("Deezer ARL authentication failed: %s", err)
            raise LoginFailed(
                "Deezer could not authenticate with the supplied ARL token.",
                translation_key="arl_rejected",
                translation_owner=self.translation_owner,
            ) from err
        except (GraphQLClientError, DeezerGWError) as err:
            self.logger.error("Deezer authentication failed: %s", err)
            raise LoginFailed(
                "Deezer authentication failed. See the Music Assistant log for the reason.",
                translation_key="auth_failed",
                translation_owner=self.translation_owner,
            ) from err

        if len(app_var("deezer_decrypt_key")) != DECRYPT_KEY_LENGTH:
            self.logger.warning(DECRYPT_KEY_ERROR)

        self.explicit_mode = await self._resolve_explicit_mode()
        self.explicit_media_types = filtered_media_types(
            hide_albums=bool(self.config.get_value(CONF_HIDE_EXPLICIT_ALBUMS, False))
        )
        if self.explicit_mode != ExplicitMode.SHOW:
            self.logger.info("Explicit content filter active: %s", self._explicit_filter_state())
        await self._drop_results_cached_under_other_filter()

        self.media_manager = DeezerMediaManager(self)
        self.rest_client = DeezerRESTClient(self.mass)
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
            if not self._hides(item):
                yield item

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve all library playlists from Deezer."""
        async for item in self.media_manager.get_library_playlists():
            yield item

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve all library tracks from Deezer."""
        async for item in self.media_manager.get_library_tracks():
            if not self._hides(item):
                yield item

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve library/subscribed podcasts from Deezer."""
        async for item in self.media_manager.get_library_podcasts():
            if not self._hides(item):
                yield item

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Retrieve library/subscribed audiobooks from Deezer."""
        async for item in self.media_manager.get_library_audiobooks():
            if not self._hides(item):
                yield item

    # -- Search --

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Perform search on music provider."""
        results = await self.media_manager.search(search_query, media_types, limit)
        results.tracks = self._visible(results.tracks)
        results.albums = self._visible(results.albums)
        results.podcasts = self._visible(results.podcasts)
        results.audiobooks = self._visible(results.audiobooks)
        return results

    # -- Item getters --

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return await self.media_manager.get_artist(prov_artist_id)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return self._unless_hidden(await self.media_manager.get_album(prov_album_id))

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return self._unless_hidden(await self.media_manager.get_track(prov_track_id))

    async def get_track_by_external_id(
        self, external_id: str, external_id_type: ExternalID
    ) -> Track | None:
        """Retrieve a track by ISRC."""
        track = await self.media_manager.get_track_by_external_id(external_id, external_id_type)
        return None if track is None or self._hides(track) else track

    async def get_album_by_external_id(
        self, external_id: str, external_id_type: ExternalID
    ) -> Album | None:
        """Retrieve an album by barcode (UPC/EAN)."""
        album = await self.media_manager.get_album_by_external_id(external_id, external_id_type)
        return None if album is None or self._hides(album) else album

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        return await self.media_manager.get_playlist(prov_playlist_id)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio/livestream details by id."""
        return await self.media_manager.get_radio(prov_radio_id)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        return self._unless_hidden(await self.media_manager.get_podcast(prov_podcast_id))

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        return self._unless_hidden(await self.media_manager.get_podcast_episode(prov_episode_id))

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        return self._unless_hidden(await self.media_manager.get_audiobook(prov_audiobook_id))

    # -- Content getters --

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all tracks in an album."""
        return self._visible(await self.media_manager.get_album_tracks(prov_album_id))

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        tracks = await self.browse_manager.get_playlist_tracks(prov_playlist_id, page)
        return self._visible(tracks, recommended=self._is_recommendation_playlist(prov_playlist_id))

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        return self._visible(await self.media_manager.get_artist_albums(prov_artist_id))

    async def get_artist_topalbums(self, prov_artist_id: str) -> list[Album]:
        """Get top albums of an artist."""
        return self._visible(await self.media_manager.get_artist_topalbums(prov_artist_id))

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks of an artist."""
        return self._visible(await self.media_manager.get_artist_toptracks(prov_artist_id))

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all episodes for a given podcast."""
        async for ep in self.media_manager.get_podcast_episodes(prov_podcast_id):
            if not self._hides(ep):
                yield ep

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        tracks = await self.media_manager.get_similar_tracks(prov_track_id, limit)
        return self._visible(tracks, recommended=True)

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

    async def set_favorite(
        self, prov_item_id: str, media_type: MediaType, favorite: bool | None
    ) -> None:
        """Ban or unban a track or artist from the user's Deezer recommendations."""
        await self.media_manager.set_favorite(prov_item_id, media_type, favorite)

    # -- Playlist CRUD --

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        await self.media_manager.add_playlist_tracks(prov_playlist_id, prov_track_ids)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        await self.media_manager.remove_playlist_tracks(prov_playlist_id, positions_to_remove)

    async def move_playlist_track(
        self, prov_playlist_id: str, from_position: int, to_position: int
    ) -> None:
        """Move one track of a playlist to another position (both 1-based)."""
        await self.media_manager.move_playlist_track(prov_playlist_id, from_position, to_position)

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        return await self.media_manager.create_playlist(name, media_types)

    # -- Browse & Recommendations --

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse Deezer content."""
        items = await self.browse_manager.browse(path, super().browse)
        return self._visible(items, recommended=self._is_recommendation_browse_path(path))

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get Deezer's available recommendation rows, without items."""
        folders = await self.browse_manager.get_recommendations()
        if self._hides_recommended():
            for folder in folders:
                folder.items = UniqueList(without_explicit(folder.items, self.explicit_media_types))
        return folders

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        items = await self.browse_manager.get_recommendation_items(item_id)
        if not self._hides_recommended():
            return items
        return UniqueList(without_explicit(items, self.explicit_media_types))

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

    # -- Explicit content --

    async def _resolve_explicit_mode(self) -> ExplicitMode:
        """Return how explicit content is handled, following the account when configured."""
        setting = str(self.config.get_value(CONF_EXPLICIT_CONTENT, ExplicitContentSetting.ACCOUNT))
        if setting == ExplicitContentSetting.HIDE:
            return ExplicitMode.HIDE
        if setting == ExplicitContentSetting.NO_RECOMMENDATIONS:
            return ExplicitMode.NO_RECOMMENDATIONS
        if setting == ExplicitContentSetting.SHOW:
            return ExplicitMode.SHOW
        try:
            mode = await fetch_account_explicit_mode(self.gql_client)
        except (GraphQLClientError, ClientError, TimeoutError) as err:
            self.logger.warning(
                "Could not read the explicit content setting of this Deezer account, "
                "explicit content is shown. Choose 'Hide explicit content' to filter it "
                "regardless: %s",
                err,
            )
            return ExplicitMode.SHOW
        return mode or ExplicitMode.SHOW

    async def _drop_results_cached_under_other_filter(self) -> None:
        """Drop cached searches and listings that were filtered under other explicit settings."""
        filter_state = self._explicit_filter_state()
        previous = await self.mass.cache.get(_EXPLICIT_FILTER_CACHE_KEY, provider=self.instance_id)
        if previous == filter_state:
            return
        # the music controller caches what this source returned after filtering: its search
        # results, the merged global ones, and the listings (artist tracks, album tracks...)
        # it assembles per item uri, for this source and for library items that draw on it
        for provider in (self.instance_id, self.mass.music.domain):
            await self.mass.cache.delete(
                None, category=CACHE_CATEGORY_SEARCH_RESULTS, provider=provider
            )
        for uri_prefix in (f"{self.instance_id}://", "library://"):
            await self.mass.cache.clear(
                category_filter=CACHE_CATEGORY_LISTINGS, provider_filter=uri_prefix
            )
        await self.mass.cache.set(
            _EXPLICIT_FILTER_CACHE_KEY,
            filter_state,
            expiration=_EXPLICIT_FILTER_CACHE_EXPIRATION,
            provider=self.instance_id,
            persistent=True,
        )

    def _explicit_filter_state(self) -> str:
        """Return what the explicit content filter hides, to tell when that changes."""
        if self.explicit_mode != ExplicitMode.SHOW and MediaType.ALBUM in self.explicit_media_types:
            return f"{self.explicit_mode.value}+albums"
        return self.explicit_mode.value

    def _hides(self, item: object, recommended: bool = False) -> bool:
        """
        Return whether the explicit content filter hides this item.

        :param item: The media item to check.
        :param recommended: Whether the item comes from a Deezer recommendation.
        """
        return self._hides_recommended(recommended) and is_explicit(item, self.explicit_media_types)

    def _hides_recommended(self, recommended: bool = True) -> bool:
        """
        Return whether explicit items are hidden from this kind of content.

        :param recommended: Whether the content is a Deezer recommendation.
        """
        if self.explicit_mode == ExplicitMode.HIDE:
            return True
        return recommended and self.explicit_mode == ExplicitMode.NO_RECOMMENDATIONS

    def _visible[T](self, items: Sequence[T], recommended: bool = False) -> list[T]:
        """
        Return the items the explicit content filter leaves visible.

        :param items: The items to filter.
        :param recommended: Whether the items come from a Deezer recommendation.
        """
        if not self._hides_recommended(recommended):
            return list(items)
        return without_explicit(items, self.explicit_media_types)

    def _unless_hidden[T](self, item: T) -> T:
        """
        Return the item, or raise when the explicit content filter hides it.

        :param item: The media item to check.
        """
        if self._hides(item):
            raise MediaNotFoundError("This item is explicit and hidden by the content filter")
        return item

    @staticmethod
    def _is_recommendation_browse_path(path: str) -> bool:
        """
        Return whether a browse path lies in one of Deezer's recommendation folders.

        :param path: The browse path, e.g. "deezer--abc://Made For You/Flow".
        """
        folders = path.split("://", 1)[1].split("/") if "://" in path else []
        return bool(folders) and folders[0] in _RECOMMENDATION_BROWSE_FOLDERS

    @staticmethod
    def _is_recommendation_playlist(prov_playlist_id: str) -> bool:
        """
        Return whether a playlist is generated by Deezer's recommendations, like Flow.

        :param prov_playlist_id: The Deezer playlist id.
        """
        if prov_playlist_id in (PERSONAL_SONGS_PLAYLIST_ID, USER_TOP_TRACKS_PLAYLIST_ID):
            return False
        return get_virtual_playlist_meta(prov_playlist_id) is not None
