"""Apple Music provider implementation."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import jwt
from aiohttp import ClientError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.tasks.context import get_current_task
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.external_ids import normalize_external_id
from music_assistant.models.music_provider import MusicProvider, NothingToSync
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin

from .api_client import AppleMusicAPIClient
from .constants import (
    CONF_MUSIC_APP_TOKEN,
    CONF_MUSIC_USER_MANUAL_TOKEN,
    CONF_MUSIC_USER_TOKEN,
    LIBRARY_FULL_SYNC_INTERVAL,
    LIBRARY_SYNC_INTERVAL_HOURS,
    MUSIC_APP_TOKEN,
    SUPPORTED_FEATURES,
)
from .helpers import browse_playlists
from .helpers.utils import is_apple_id
from .library import AppleMusicLibraryManager
from .media import AppleMusicMediaManager
from .recommendations import AppleMusicRecommendationManager
from .streaming import AppleMusicStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.background_task import TaskSchedule
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.enums import ExternalID
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant import MusicAssistant
    from music_assistant.helpers.json import SerializableType


class AppleMusicProvider(RecommendationPayloadMixin, MusicProvider):
    """Implementation of an Apple Music MusicProvider."""

    recommendation_payload_version = "1"  # stations are radios
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
        self._last_library_listings: dict[str, dict[str, Any]] = {}

    @property
    def max_concurrent_streams(self) -> int:
        """Apple Music accounts allow a single active playback session."""
        return 1

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to configure this provider.

        Authentication is handled by the setup flow (see setup_flow.py); only the
        informational note is surfaced here.
        """
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        return {
            "requests_since": self.api_client.request_counter.started_at,
            "requests": self.api_client.request_counter.as_dict(),
            "last_library_listings": self._last_library_listings,
        }

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._music_user_token = cast(
            "str | None",
            self.get_setup_value(CONF_MUSIC_USER_MANUAL_TOKEN)
            or self.get_setup_value(CONF_MUSIC_USER_TOKEN),
        )
        self._music_app_token = _pick_app_token(
            cast("str | None", self.get_setup_value(CONF_MUSIC_APP_TOKEN)), MUSIC_APP_TOKEN
        )
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
        _require_apple_id(prov_artist_id)
        return await self.media_manager.get_artist(prov_artist_id)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        _require_apple_id(prov_album_id)
        return await self.media_manager.get_album(prov_album_id)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        _require_apple_id(prov_track_id)
        return await self.media_manager.get_track(prov_track_id)

    async def get_playlist(
        self, prov_playlist_id: str, is_favourite: bool | None = None
    ) -> Playlist:
        """Get full playlist details by id."""
        _require_apple_id(prov_playlist_id)
        if prov_playlist_id.startswith("ra."):
            raise MediaNotFoundError(f"{prov_playlist_id} is a station, not a playlist")
        return await self.media_manager.get_playlist(prov_playlist_id, is_favourite)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full station details by id."""
        _require_apple_id(prov_radio_id)
        return await self.recommendation_manager.get_station(prov_radio_id)

    async def get_dynamic_radio_tracks(self, prov_radio_id: str) -> list[Track]:
        """
        Get the next batch of tracks for the given station.

        :param prov_radio_id: The Apple Music station id.
        """
        if not is_apple_id(prov_radio_id):
            return []
        return await self.media_manager.get_station_tracks(prov_radio_id)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        if not is_apple_id(prov_album_id):
            return []
        return await self.media_manager.get_album_tracks(prov_album_id)

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve an artwork token to a freshly signed artwork URL."""
        media_type, _, item_id = path.partition("/")
        # a station token from before stations were radios cannot resolve as a playlist
        if not is_apple_id(item_id) or (
            media_type == MediaType.PLAYLIST.value and item_id.startswith("ra.")
        ):
            return ""
        return await self.media_manager.get_artwork_url(media_type, item_id) or ""

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        if not is_apple_id(prov_playlist_id) or prov_playlist_id.startswith("ra."):
            return []
        return await self.media_manager.get_playlist_tracks(prov_playlist_id, page)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        if not is_apple_id(prov_artist_id):
            return []
        return await self.media_manager.get_artist_albums(prov_artist_id)

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        if not is_apple_id(prov_artist_id):
            return []
        return await self.media_manager.get_artist_toptracks(prov_artist_id)

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        if not is_apple_id(prov_track_id):
            return []
        return await self.recommendation_manager.get_similar_tracks(prov_track_id, limit)

    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Retrieve a list of artists similar to the provided artist."""
        if not is_apple_id(prov_artist_id):
            return []
        return await self.recommendation_manager.get_similar_artists(prov_artist_id, limit)

    # ------------------------------------------------------------------
    # External ID lookups
    # ------------------------------------------------------------------

    async def get_track_by_external_id(
        self, external_id: str, external_id_type: ExternalID
    ) -> Track | None:
        """Retrieve track by external ID (ISRC)."""
        normalized_external_id = normalize_external_id(external_id_type, external_id)
        return await self.media_manager.get_track_by_external_id(
            normalized_external_id, external_id_type
        )

    async def get_album_by_external_id(
        self, external_id: str, external_id_type: ExternalID
    ) -> Album | None:
        """Retrieve album by external ID (UPC/Barcode)."""
        normalized_external_id = normalize_external_id(external_id_type, external_id)
        return await self.media_manager.get_album_by_external_id(
            normalized_external_id, external_id_type
        )

    # ------------------------------------------------------------------
    # Library generators
    # ------------------------------------------------------------------

    def get_default_library_sync_schedule(self, media_type: MediaType) -> TaskSchedule:
        """Return the default recurring schedule for library sync tasks of this provider."""
        schedule = super().get_default_library_sync_schedule(media_type)
        schedule.every = LIBRARY_SYNC_INTERVAL_HOURS
        return schedule

    async def sync_library(self, media_type: MediaType) -> None:
        """
        Run library sync for this provider.

        A scheduled artist, album or track run is skipped by raising NothingToSync when the
        library did not change since the last full sync. A skipped album run, and a skipped
        track run after a song rating changed, still record the current Apple Music ratings.
        A manual run always syncs.

        :param media_type: The media type to sync.
        """
        if media_type == MediaType.PLAYLIST:
            # costs about as much as a marker, and a marker would miss playlist content changes
            await super().sync_library(media_type)
            return
        favourites: str | None = None
        try:
            marker: str | None = await self.library_manager.get_sync_marker(media_type)
            if media_type == MediaType.TRACK:
                favourites = await self.library_manager.get_favourites_marker()
        except (MusicAssistantError, ClientError, KeyError) as err:
            # a failed check must never become a skip
            self.logger.warning(
                "Could not check %s library for changes, running a full sync: %s",
                media_type.value,
                err,
            )
            marker = None
        key = f"library_sync_state.{media_type.value}"
        task = get_current_task()
        manual_run = task is not None and task.last_run_user_id is not None
        if marker is not None and not manual_run:
            state = await self.mass.cache.get(key, provider=self.instance_id)
            if (
                state
                and state["marker"] == marker
                and time.time() - state["full_sync_at"] < LIBRARY_FULL_SYNC_INTERVAL
            ):
                # ratings made in the Apple Music app change no marker
                if media_type == MediaType.ALBUM or (
                    media_type == MediaType.TRACK and state.get("favourites") != favourites
                ):
                    await self._refresh_favourites(
                        media_type, key, {**state, "favourites": favourites}
                    )
                raise NothingToSync
        await super().sync_library(media_type)
        if marker is not None:
            await self.mass.cache.set(
                key=key,
                data={"marker": marker, "full_sync_at": time.time(), "favourites": favourites},
                expiration=LIBRARY_FULL_SYNC_INTERVAL,
                provider=self.instance_id,
            )

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from the provider."""
        async for item in self._counted_listing(
            MediaType.ARTIST, self.library_manager.get_library_artists()
        ):
            yield item

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        async for item in self._counted_listing(
            MediaType.ALBUM, self.library_manager.get_library_albums()
        ):
            yield item

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from the provider."""
        async for item in self._counted_listing(
            MediaType.TRACK, self.library_manager.get_library_tracks()
        ):
            yield item

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve playlists from the provider."""
        async for item in self._counted_listing(
            MediaType.PLAYLIST, self.library_manager.get_library_playlists()
        ):
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

    async def set_favorite(
        self, prov_item_id: str, media_type: MediaType, favorite: bool | None
    ) -> None:
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

    async def _refresh_favourites(
        self, media_type: MediaType, key: str, state: dict[str, Any]
    ) -> None:
        """Record the current ratings on a skipped sync, then store its sync state."""
        self.logger.debug("Refreshing %s favourites on a skipped sync", media_type.value)
        try:
            await self.library_manager.refresh_favourites(media_type)
        except (MusicAssistantError, ClientError) as err:
            # the skip stands; leaving the state unstored retries the refresh on the next check
            self.logger.warning("Could not refresh %s favourites: %s", media_type.value, err)
            return
        await self.mass.cache.set(
            key=key, data=state, expiration=LIBRARY_FULL_SYNC_INTERVAL, provider=self.instance_id
        )

    async def _counted_listing[T](
        self, media_type: MediaType, items: AsyncGenerator[T]
    ) -> AsyncGenerator[T]:
        """Yield the items of a full library listing and keep the requests made meanwhile for diagnostics."""
        counter = self.api_client.request_counter
        before = counter.as_dict()
        started = time.monotonic()
        count = 0
        completed = False
        try:
            async for item in items:
                count += 1
                yield item
            completed = True
        finally:
            self._last_library_listings[media_type.value] = {
                "finished_at": utc().isoformat(timespec="seconds"),
                "duration_sec": round(time.monotonic() - started, 1),
                "items": count,
                "completed": completed,
                "requests": counter.since(before),
            }


def _require_apple_id(item_id: str) -> None:
    """
    Raise MediaNotFoundError for an id that cannot exist on Apple Music, without a request.

    :param item_id: The provider item id to check.
    """
    if not is_apple_id(item_id):
        raise MediaNotFoundError(f"{item_id} is not an Apple Music id")


def _pick_app_token(stored: str | None, bundled: str) -> str | None:
    """Return whichever of the stored and bundled app tokens expires last."""
    # installs set up before 2.10 store the token bundled back then, while a token typed
    # into the setup flow (because the bundled one was rejected) is newer than the bundle
    if not stored or not bundled:
        return stored or bundled
    return stored if _token_expiry(stored) > _token_expiry(bundled) else bundled


def _token_expiry(token: str) -> int:
    """Return the expiry (unix timestamp) of an app token."""
    return int(jwt.decode(token, options={"verify_signature": False})["exp"])
