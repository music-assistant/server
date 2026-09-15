"""Model/base for a Music Provider implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, cast

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import ArtistType, MediaType, ProviderFeature
from music_assistant_models.errors import (
    AudioError,
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    UnsupportedFeaturedException,
)
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
    SoundEffect,
    Track,
    UniqueList,
)

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS,
    CONF_ENTRY_LIBRARY_SYNC_DELETIONS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS,
    PlaylistPlayableItem,
)
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress_text,
)

from .provider import Provider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.music.media.base import (
        AudiobookSyncDetails,
        LibraryItemSyncDetails,
        TrackSyncDetails,
    )
    from music_assistant.mass import MusicAssistant

CACHE_CATEGORY_PREV_LIBRARY_IDS: Final[int] = 1
DEFAULT_MAX_CONCURRENT_STREAMS: Final[int] = 5
# a provider-wide payload change fails every single item, so only the first failures
# of a sync run are logged in full to keep the (rotating) log file usable
MAX_LOGGED_SYNC_FAILURES: Final[int] = 25
MAX_SYNC_ERROR_DETAIL: Final[int] = 200
# skipped id's are resolved back to library id's in batches of this size
SKIPPED_ITEM_QUERY_LIMIT: Final[int] = 500

LIBRARY_FEATURE_BY_MEDIA_TYPE: Final[dict[MediaType, ProviderFeature]] = {
    MediaType.ARTIST: ProviderFeature.LIBRARY_ARTISTS,
    MediaType.ALBUM: ProviderFeature.LIBRARY_ALBUMS,
    MediaType.TRACK: ProviderFeature.LIBRARY_TRACKS,
    MediaType.PLAYLIST: ProviderFeature.LIBRARY_PLAYLISTS,
    MediaType.RADIO: ProviderFeature.LIBRARY_RADIOS,
    MediaType.AUDIOBOOK: ProviderFeature.LIBRARY_AUDIOBOOKS,
    MediaType.PODCAST: ProviderFeature.LIBRARY_PODCASTS,
}


@dataclass
class SyncRunState:
    """
    Failure state of one library sync run.

    :param incomplete_media_types: Media types the run failed to collect an item for, which
        makes their result set an unsafe basis for deleting anything from the library.
    :param failures: Number of item failures reported by the run so far.
    :param skipped_item_ids: Provider item id's the provider dropped while listing its
        library, per media type.
    """

    incomplete_media_types: set[MediaType] = field(default_factory=set)
    failures: int = 0
    skipped_item_ids: dict[MediaType, set[str]] = field(default_factory=dict)


# scoped per run rather than per provider: a standalone import_album_tracks() is
# launched as its own task, so it must not consume or inflate a running sync's state
SYNC_RUN_STATE: Final[ContextVar[SyncRunState | None]] = ContextVar(
    "music_provider_sync_run", default=None
)


def sync_run_state() -> SyncRunState:
    """Return the state of the sync run in progress, starting one if there is none."""
    if (state := SYNC_RUN_STATE.get()) is None:
        state = SyncRunState()
        SYNC_RUN_STATE.set(state)
    return state


class ProviderStreamLimitError(AudioError):
    """Raised when a music provider has no source-stream slot available."""

    translation_key = "provider_stream_limit"

    def __init__(self, provider: MusicProvider, wait_timeout: float | None) -> None:
        """
        Initialize the provider stream limit error.

        :param provider: Provider instance whose source-stream limit was reached.
        :param wait_timeout: Seconds spent waiting for a slot, or None for an unbounded wait.
        """
        limit = provider.max_concurrent_streams
        assert limit is not None
        wait_text = f" after waiting {wait_timeout:g} seconds" if wait_timeout is not None else ""
        super().__init__(
            f"{provider.name} has reached its limit of {limit} "
            f"concurrent source streams{wait_text}.",
            translation_args=[provider.name, limit],
        )
        self.provider_instance = provider.instance_id
        self.limit = limit


def describe_sync_error(err: Exception) -> str:
    """Return a short description of a sync failure, safe to log and to report to clients."""
    if isinstance(err, MusicAssistantError):
        return str(err)
    # an unexpected error can carry an entire api response as its message, which would end
    # up in the log and - through the task failure list - in every connected client. report
    # it by type with a clipped detail and leave the full payload to the debug traceback
    detail = str(err)
    if not detail:
        return type(err).__name__
    if len(detail) > MAX_SYNC_ERROR_DETAIL:
        detail = f"{detail[:MAX_SYNC_ERROR_DETAIL]}..."
    return f"{type(err).__name__}: {detail}"


class MusicProvider(Provider):
    """
    Base representation of a Music Provider (controller).

    Music Provider implementations should inherit from this base model.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, supported_features)
        max_concurrent_streams = self.max_concurrent_streams
        if max_concurrent_streams is not None and max_concurrent_streams < 1:
            raise ValueError("max_concurrent_streams must be at least 1 or None")
        self._stream_semaphore = (
            asyncio.BoundedSemaphore(max_concurrent_streams)
            if max_concurrent_streams is not None
            else None
        )

    def delivers_normalized_audio(self, streamdetails: StreamDetails) -> bool:
        """
        Return whether this provider hands over audio it has already normalized.

        True means the source applies a loudness target of its own, so Music
        Assistant leaves the level alone instead of measuring and correcting it
        a second time. Only say so when the audio really is normalized on the
        way out: nothing downstream double-checks it.

        :param streamdetails: Stream details of the item being asked about. A
            provider that normalizes per playback session answers for the queue
            these details belong to, not for whatever it happens to serve
            elsewhere.
        """
        return False

    @property
    def max_concurrent_streams(self) -> int | None:
        """
        Return the number of source streams Music Assistant may run against this provider.

        None means no limit is imposed, which is the correct answer for local and
        self-hosted sources. Streaming providers get a conservative default of five;
        override with a lower, evidence-backed value where the service enforces one.
        Plugin providers (exclusive audio sources) manage their own session exclusivity
        and are not covered by this limit.
        """
        return DEFAULT_MAX_CONCURRENT_STREAMS if self.is_streaming_provider else None

    @property
    def has_available_stream_slot(self) -> bool:
        """Return whether a source stream can start without waiting."""
        return self._stream_semaphore is None or not self._stream_semaphore.locked()

    @asynccontextmanager
    async def acquire_stream_slot(self, wait_timeout: float | None) -> AsyncGenerator[None]:
        """
        Acquire one source-stream slot for the duration of the context.

        :param wait_timeout: Maximum seconds to wait, or None to wait without a timeout.
        :raises ProviderStreamLimitError: If no slot becomes available before the timeout.
        """
        semaphore = self._stream_semaphore
        if semaphore is None:
            yield
            return
        try:
            if wait_timeout is None:
                await semaphore.acquire()
            else:
                async with asyncio.timeout(wait_timeout):
                    await semaphore.acquire()
        except TimeoutError as err:
            raise ProviderStreamLimitError(self, wait_timeout) from err
        try:
            yield
        finally:
            semaphore.release()

    @property
    def is_streaming_provider(self) -> bool:
        """
        Return True if the provider is a streaming provider.

        This literally means that the catalog is not the same as the library contents.
        For local based providers (files, plex), the catalog is the same as the library content.
        It also means that data is if this provider is NOT a streaming provider,
        data cross instances is unique, the catalog and library differs per instance.

        Setting this to True will only query one instance of the provider for search and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        return True

    @property
    def supported_media_types(self) -> set[MediaType]:
        """
        Return the media types this provider can serve.

        Defaults to the media types the provider declares library support for.
        Override for providers that can serve (search/stream) media types they
        cannot list as library items, so they are eligible for search-based
        lookups such as cross-provider matching and versions.
        """
        return {
            media_type
            for media_type, feature in LIBRARY_FEATURE_BY_MEDIA_TYPE.items()
            if feature in self.supported_features
        }

    @property
    def unskippable_sync_errors(self) -> tuple[type[Exception], ...]:
        """
        Return the errors a library sync must never treat as a skippable item failure.

        Declare the errors this provider raises to signal something a wrapper around its
        own methods has to act on, such as an expired token that triggers a reauthenticate
        and a retry. Anything listed here is re-raised instead of skipping the item.
        """
        return ()

    @property
    def supported_artist_types(self) -> set[ArtistType]:
        """
        Return all supported artist types by this provider.

        Note, that this property currently is only used, to verify support of artists with
        ArtistType.AUTHOR or ArtistType.NARRATOR.
        """
        return {ArtistType.SINGER}

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        if ProviderFeature.SEARCH in self.supported_features:
            raise NotImplementedError
        return SearchResults()

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve library/subscribed playlists from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Retrieve library/subscribed radio stations from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Retrieve library/subscribed audiobooks from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve library/subscribed podcasts from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_library_genres(self) -> AsyncGenerator[str]:
        """Retrieve library genres from the provider."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        raise NotImplementedError

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """
        Get a list of all albums for the given artist.

        Only called if provider supports ProviderFeature.ARTIST_ALBUMS.
        """
        raise NotImplementedError

    async def get_artist_tracks(self, prov_artist_id: str) -> list[Track]:
        """
        Get a list of all tracks for the given artist.

        Only called if provider supports ProviderFeature.ARTIST_TRACKS.
        """
        raise NotImplementedError

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """
        Get a list of most popular tracks for the given artist.

        Only called if provider supports ProviderFeature.ARTIST_TOPTRACKS.
        """
        raise NotImplementedError

    async def get_artist_topalbums(self, prov_artist_id: str) -> list[Album]:
        """
        Get a list of most popular albums for the given artist.

        Only called if provider supports ProviderFeature.ARTIST_TOPALBUMS.
        """
        raise NotImplementedError

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        raise NotImplementedError

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        raise NotImplementedError

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        raise NotImplementedError

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        raise NotImplementedError

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details by id."""
        raise NotImplementedError

    async def get_author_audiobooks(self, prov_artist_id: str) -> list[Audiobook]:
        """
        Get a list of all audiobooks for the given author.

        Only called if provider supports ProviderFeature.AUTHOR_AUDIOBOOKS.
        """
        raise NotImplementedError

    async def get_narrator_audiobooks(self, prov_artist_id: str) -> list[Audiobook]:
        """
        Get a list of all audiobooks for the given narrator.

        Only called if provider supports ProviderFeature.NARRATOR_AUDIOBOOKS.
        """
        raise NotImplementedError

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        raise NotImplementedError

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        raise NotImplementedError

    async def get_sound_effect(self, prov_sound_effect_id: str) -> SoundEffect:
        """Get full sound effect details by id."""
        raise NotImplementedError

    async def get_sound_effects(self) -> AsyncGenerator[SoundEffect]:
        """
        Get all sound effect items this provider offers.

        Sound effects are not library-backed; they are fetched live from the provider.
        Only called if provider supports ProviderFeature.SOUND_EFFECTS.
        """
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def get_item_genre_names(self, media_type: MediaType, item_id: str) -> set[str]:
        """Return genre names for a single item."""
        raise NotImplementedError

    async def get_album_tracks(
        self,
        prov_album_id: str,
    ) -> list[Track]:
        """Get album tracks for given album id."""
        raise NotImplementedError

    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> Sequence[PlaylistPlayableItem]:
        """Get all playlist tracks for given playlist id."""
        raise NotImplementedError

    async def get_dynamic_radio_tracks(
        self, prov_radio_id: str, *, sample: bool = False
    ) -> list[Track]:
        """
        Return a fresh batch of tracks for a dynamic radio station.

        Only called for a Radio with `is_dynamic` set. Every call returns a new batch;
        there is no stable listing and no pagination.

        :param prov_radio_id: The provider's ID of the radio station.
        :param sample: True returns a preview batch that must not mutate any
            playback state.
        """
        raise NotImplementedError

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode]:
        """Get all PodcastEpisodes for given podcast id."""
        yield  # type: ignore[misc]
        raise NotImplementedError

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        if (
            item.media_type == MediaType.ARTIST
            and ProviderFeature.LIBRARY_ARTISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.ALBUM
            and ProviderFeature.LIBRARY_ALBUMS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.TRACK
            and ProviderFeature.LIBRARY_TRACKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.PLAYLIST
            and ProviderFeature.LIBRARY_PLAYLISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.RADIO
            and ProviderFeature.LIBRARY_RADIOS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.AUDIOBOOK
            and ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            item.media_type == MediaType.PODCAST
            and ProviderFeature.LIBRARY_PODCASTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        self.logger.info(
            "Provider %s does not support library edit, "
            "the action will only be performed in the local database.",
            self.name,
        )
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        if (
            media_type == MediaType.ARTIST
            and ProviderFeature.LIBRARY_ARTISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.ALBUM
            and ProviderFeature.LIBRARY_ALBUMS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.TRACK
            and ProviderFeature.LIBRARY_TRACKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PLAYLIST
            and ProviderFeature.LIBRARY_PLAYLISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.RADIO
            and ProviderFeature.LIBRARY_RADIOS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.AUDIOBOOK
            and ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PODCAST
            and ProviderFeature.LIBRARY_PODCASTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        self.logger.info(
            "Provider %s does not support library edit, "
            "the action will only be performed in the local database.",
            self.name,
        )
        return True

    async def set_favorite(self, prov_item_id: str, media_type: MediaType, favorite: bool) -> None:
        """
        Set favorite status for item in provider's library.

        Only called if provider supports ProviderFeature.FAVORITE_*_EDIT.

        Note that this should only be implemented by a provider implementation if
        the provider differentiates between 'in library' and 'favorited' items.
        """
        if (
            media_type == MediaType.ARTIST
            and ProviderFeature.FAVORITE_ARTISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.ALBUM
            and ProviderFeature.FAVORITE_ALBUMS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.TRACK
            and ProviderFeature.FAVORITE_TRACKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PLAYLIST
            and ProviderFeature.FAVORITE_PLAYLISTS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.RADIO
            and ProviderFeature.FAVORITE_RADIOS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.AUDIOBOOK
            and ProviderFeature.FAVORITE_AUDIOBOOKS_EDIT in self.supported_features
        ):
            raise NotImplementedError
        if (
            media_type == MediaType.PODCAST
            and ProviderFeature.FAVORITE_PODCASTS_EDIT in self.supported_features
        ):
            raise NotImplementedError

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """
        Add track(s) to playlist.

        Only called if provider supports ProviderFeature.PLAYLIST_TRACKS_EDIT.
        """
        raise NotImplementedError

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """
        Remove track(s) from playlist.

        Only called if provider supports ProviderFeature.PLAYLIST_TRACKS_EDIT.
        """
        raise NotImplementedError

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """
        Create a new playlist on provider with given name and targeting media_types.

        Only called if provider supports ProviderFeature.PLAYLIST_CREATE.
        """
        raise NotImplementedError

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """
        Retrieve a dynamic list of similar tracks based on the provided track.

        Only called if provider supports ProviderFeature.SIMILAR_TRACKS.
        """
        raise NotImplementedError

    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """
        Retrieve a dynamic list of similar artists based on the provided artist.

        Only called if provider supports ProviderFeature.SIMILAR_ARTISTS.
        """
        raise NotImplementedError

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """
        Get progress (resume point) details for the given Audiobook or Podcast episode.

        This is a separate call from the regular get_item call to ensure the resume position
        is always up-to-date and because a lot providers have this info present on a dedicated
        endpoint.

        Will be called right before playback starts to ensure the resume position is correct.

        Returns a boolean with the fully_played status
        an integer with the resume position in ms,
        and an optional timestamp as datetime giving when this resume position was set
        """
        raise NotImplementedError

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track/radio/chapter/episode."""
        raise NotImplementedError

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the (custom) audio stream for the provider item.

        Will only be called when the stream_type is set to CUSTOM.
        """
        yield b""
        raise NotImplementedError

    async def on_streamed(
        self,
        streamdetails: StreamDetails,
    ) -> None:
        """
        Handle callback when given streamdetails completed streaming.

        To get the number of seconds streamed, see streamdetails.seconds_streamed.
        To get the number of seconds seeked/skipped, see streamdetails.seek_position.
        Note that seconds_streamed is the total streamed seconds, so without seeked time.

        NOTE: Due to internal and player buffering,
        this may be called in advance of the actual completion.
        """

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Handle callback when a (playable) media item has been played.

        This is called by the Queue controller when;
            - a track has been fully played
            - a track has been stopped (or skipped) after being played
            - every 30s when a track is playing

        Fully played is True when the track has been played to the end.

        Position is the last known position of the track in seconds, to sync resume state.
        When fully_played is set to false and position is 0,
        the user marked the item as unplayed in the UI.

        media_item is the full media item details of the played/playing track.

        is_playing is True when the track is currently playing.
        """

    async def on_item_updated(self, item: MediaItemType) -> None:
        """
        Handle callback when a library item's metadata has been updated.

        Providers can implement this to sync changes to their own storage
        (e.g. config entries, file tags).

        :param item: The updated library item.
        """

    async def resolve_image(self, path: str) -> str | bytes:
        """
        Resolve an image from an image path.

        This either returns (a generator to get) raw bytes of the image or
        a string with an http(s) URL or local path that is accessible from the server.
        """
        return path

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:  # noqa: PLR0911
        """
        Browse this provider's items.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        if ProviderFeature.BROWSE not in self.supported_features:
            # we may NOT use the default implementation if the provider does not support browse
            raise NotImplementedError

        path_parts = path.split("://")[1].split("/")
        subpath = path_parts[0] if len(path_parts) > 0 else None
        sub_subpath = path_parts[1] if len(path_parts) > 1 else None
        # this reference implementation can be overridden with a provider specific approach
        if subpath == "artists":
            if artists := await self.mass.music.artists.library_items(
                provider=self.instance_id,
                summary=False,
            ):
                return artists
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_artists()]
        if subpath == "albums":
            if albums := await self.mass.music.albums.library_items(
                provider=self.instance_id,
                summary=False,
            ):
                return albums
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_albums()]
        if subpath == "tracks":
            if tracks := await self.mass.music.tracks.library_items(
                provider=self.instance_id,
                summary=False,
            ):
                return tracks
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_tracks()]
        if subpath == "radios":
            if radios := await self.mass.music.radio.library_items(
                provider=self.instance_id,
                summary=False,
            ):
                return radios
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_radios()]
        if subpath == "playlists":
            if playlists := await self.mass.music.playlists.library_items(
                provider=self.instance_id,
                summary=False,
            ):
                return playlists
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_playlists()]
        if subpath == "audiobooks":
            if audiobooks := await self.mass.music.audiobooks.library_items(
                provider=self.instance_id,
                summary=False,
            ):
                return audiobooks
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_audiobooks()]
        if subpath == "podcasts":
            if podcasts := await self.mass.music.podcasts.library_items(
                provider=self.instance_id,
                summary=False,
            ):
                return podcasts
            # library items not (yet) synced, fallback to direct retrieval
            return [x async for x in self.get_library_podcasts()]
        if subpath == "sound_effects":
            # sound effects are not library-backed, always retrieve them live
            return [x async for x in self.get_sound_effects()]
        if subpath == "recommendations" and sub_subpath:
            # recommendations contents listing
            return await self.get_recommendation_items(sub_subpath)
        if subpath == "recommendations":
            # Main recommendations listing
            result: list[BrowseFolder] = []
            recommendations = await self.get_recommendations()
            for rec in recommendations:
                result.append(
                    BrowseFolder(
                        item_id=rec.item_id,
                        provider=self.instance_id,
                        name=rec.name,
                        is_playable=rec.is_playable,
                        image=rec.image,
                        path=f"{path}/{rec.item_id}",
                    )
                )
            return result

        if subpath:
            # unknown path
            msg = "Invalid subpath"
            raise KeyError(msg)

        # no subpath: return main listing
        folders: list[BrowseFolder] = []
        if ProviderFeature.LIBRARY_ARTISTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="artists",
                    provider=self.instance_id,
                    path=path + "artists",
                    name="",
                    translation_key="artists",
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_ALBUMS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="albums",
                    provider=self.instance_id,
                    path=path + "albums",
                    name="",
                    translation_key="albums",
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_TRACKS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="tracks",
                    provider=self.domain,
                    path=path + "tracks",
                    name="",
                    translation_key="tracks",
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_PLAYLISTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="playlists",
                    provider=self.instance_id,
                    path=path + "playlists",
                    name="",
                    translation_key="playlists",
                    is_playable=True,
                )
            )
        if ProviderFeature.LIBRARY_RADIOS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="radios",
                    provider=self.instance_id,
                    path=path + "radios",
                    name="",
                    translation_key="radios",
                )
            )
        if ProviderFeature.LIBRARY_AUDIOBOOKS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="audiobooks",
                    provider=self.instance_id,
                    path=path + "audiobooks",
                    name="",
                    translation_key="audiobooks",
                )
            )
        if ProviderFeature.LIBRARY_PODCASTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="podcasts",
                    provider=self.instance_id,
                    path=path + "podcasts",
                    name="",
                    translation_key="podcasts",
                )
            )
        if ProviderFeature.SOUND_EFFECTS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="sound_effects",
                    provider=self.instance_id,
                    path=path + "sound_effects",
                    name="",
                    translation_key="sound_effects",
                )
            )
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            folders.append(
                BrowseFolder(
                    item_id="recommendations",
                    provider=self.instance_id,
                    path=path + "recommendations",
                    name="",
                    translation_key="recommendations",
                )
            )
        if len(folders) == 1:
            # only one level, return the items directly
            return await self.browse(folders[0].path)
        return folders

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's available recommendation rows, without items.

        Must be fast: return static or cached row descriptors only, without
        live backend calls. The items for a row are fetched separately
        through get_recommendation_items.

        Will only be called if ProviderFeature.RECOMMENDATIONS is declared.
        """
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            raise NotImplementedError
        return []

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        Live backend fetches belong here. Will only be called if
        ProviderFeature.RECOMMENDATIONS is declared.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        if ProviderFeature.RECOMMENDATIONS in self.supported_features:
            raise NotImplementedError
        return UniqueList()

    async def sync_library(self, media_type: MediaType) -> None:
        """Run library sync for this provider."""
        token = SYNC_RUN_STATE.set(SyncRunState())
        try:
            await self._run_library_sync(media_type)
        finally:
            SYNC_RUN_STATE.reset(token)

    def report_skipped_sync_item(
        self, media_type: MediaType, item_id: str | None, err: Exception
    ) -> None:
        """
        Report a library item that was dropped while listing this provider's library.

        Call this from a get_library_*() generator whenever it swallows an error instead of
        yielding the item, so the failure is reported on the sync task rather than the item
        looking like it was removed at the provider.

        :param media_type: Media type of the skipped item.
        :param item_id: The provider item id of the skipped item, which keeps that single item
            out of this sync's deletion pass. Pass None if the item cannot be identified, which
            holds back the deletion pass for the entire run instead.
        :param err: The error that made the item unusable.
        :raises Exception: If this provider declared the error unskippable, so that its own
            error handling can act on it instead of the item being skipped.
        """
        self._handle_sync_item_failure(media_type, item_id, err)
        state = sync_run_state()
        if item_id:
            state.skipped_item_ids.setdefault(media_type, set()).add(item_id)
        else:
            state.incomplete_media_types.add(media_type)

    async def _run_library_sync(self, media_type: MediaType) -> None:
        """Sync the given media type into the library and process its deletions."""
        # this reference implementation may be overridden
        # with a provider specific approach if needed

        if not self.mass.music.library_supported(self, media_type):
            raise UnsupportedFeaturedException("Library sync not supported for this media type")

        sync_state = sync_run_state()
        if media_type == MediaType.ARTIST:
            cur_db_ids = await self._sync_library_artists()
        elif media_type == MediaType.ALBUM:
            cur_db_ids = await self._sync_library_albums()
        elif media_type == MediaType.TRACK:
            cur_db_ids = await self._sync_library_tracks()
        elif media_type == MediaType.PLAYLIST:
            cur_db_ids = await self._sync_library_playlists()
        elif media_type == MediaType.PODCAST:
            cur_db_ids = await self._sync_library_podcasts()
        elif media_type == MediaType.RADIO:
            cur_db_ids = await self._sync_library_radios()
        elif media_type == MediaType.AUDIOBOOK:
            cur_db_ids = await self._sync_library_audiobooks()
        else:
            # this should not happen but catch it anyways
            raise UnsupportedFeaturedException(f"Unexpected media type to sync: {media_type}")

        # process deletions (= no longer in library)
        update_current_task_progress_text("Checking library deletions")
        controller = self.mass.music.get_controller(media_type)
        await self._keep_skipped_items(media_type, cur_db_ids)
        prev_library_items: list[int] | None
        if media_type in sync_state.incomplete_media_types:
            # a skipped item is missing from cur_db_ids just like a deleted one, but it is
            # still in the provider's library, so deleting it would throw away valid content
            if self.library_sync_deletions_enabled():
                summary = f"{sync_state.failures} item(s) could not be synced"
                self.logger.warning("Skipping deletions for %s: %s", self.name, summary)
                report_current_task_failure(f"Deletions skipped: {summary}")
            # merge this run's id's into the stored ones instead of replacing them: that
            # keeps both the deletions this run could not tell apart from its own failures
            # and the items it saw for the first time, so a later complete run finds either
            if prev_library_items := await self.mass.cache.get(
                key=media_type.value,
                provider=self.instance_id,
                category=CACHE_CATEGORY_PREV_LIBRARY_IDS,
            ):
                cur_db_ids.update(prev_library_items)
        elif self.library_sync_deletions_enabled():
            if prev_library_items := await self.mass.cache.get(
                key=media_type.value,
                provider=self.instance_id,
                category=CACHE_CATEGORY_PREV_LIBRARY_IDS,
            ):
                for db_id in prev_library_items:
                    if db_id not in cur_db_ids:
                        try:
                            library_item = await controller.get_library_item(db_id)
                        except MediaNotFoundError:
                            # edge case: the item is (already) removed from MA library as well
                            continue
                        # check if we have other provider-mappings (marked as in-library)
                        remaining_providers_in_library = {
                            x.provider_instance
                            for x in library_item.provider_mappings
                            if x.provider_instance != self.instance_id and x.in_library
                        }
                        if not remaining_providers_in_library and not self.is_streaming_provider:
                            # for non-streaming providers (local files, library-middlemen
                            # like subsonic/jellyfin/plex) an item removed from the provider
                            # is actually gone; fully remove it to avoid dangling records
                            # that stay visible in artist/album views where in_library is
                            # not filtered on
                            await controller.remove_item_from_library(db_id)
                        else:
                            if not remaining_providers_in_library and library_item.favorite:
                                # unmark as favorite since no providers have it in library
                                await controller.set_favorite(db_id, False)
                            # unmark this provider mapping as in_library = False
                            # we keep it in the library database so we can keep the metadata
                            for prov_map in library_item.provider_mappings:
                                if prov_map.provider_instance == self.instance_id:
                                    prov_map.in_library = False
                            await controller.set_provider_mappings(
                                db_id, library_item.provider_mappings
                            )
                        await asyncio.sleep(0)  # yield to eventloop
        # store current list of id's in cache so we can track changes
        await self.mass.cache.set(
            key=media_type.value,
            data=list(cur_db_ids),
            provider=self.instance_id,
            category=CACHE_CATEGORY_PREV_LIBRARY_IDS,
        )
        update_current_task_progress_text("Finalizing library sync")

    def _update_sync_task_item_status(
        self, media_type: MediaType, processed_items: int, item_name: str | None = None
    ) -> None:
        """Update task text for the item currently being synced."""
        message = f"Processed {processed_items} {media_type.value}s"
        if item_name:
            message = f"{message}: {item_name}"
        update_current_task_progress_text(message)

    def _handle_sync_item_failure(
        self, media_type: MediaType, item_ref: str | None, err: Exception
    ) -> None:
        """
        Log a non-fatal sync failure and record it on the active background task.

        :raises Exception: If the provider declared this error unskippable, so that its own
            error handling can act on it instead of the item being skipped.
        """
        if isinstance(err, self.unskippable_sync_errors):
            raise err
        state = sync_run_state()
        state.failures += 1
        error_detail = describe_sync_error(err)
        if state.failures <= MAX_LOGGED_SYNC_FAILURES:
            if isinstance(err, MusicAssistantError):
                self.logger.warning(
                    "Skipping sync of %s %s - error details: %s",
                    media_type.value,
                    item_ref,
                    error_detail,
                )
            else:
                # not one of our own errors: usually a provider choking on its own api
                # payload, but the per-item library writes raise the same way, so log the
                # traceback (on debug) to make the actual origin traceable
                self.logger.error(
                    "Skipping sync of %s %s - unexpected error: %s",
                    media_type.value,
                    item_ref,
                    error_detail,
                    exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
                )
        report_current_task_failure(
            f"Failed to sync {media_type.value} {item_ref or '<unknown>'}: {error_detail}"
        )

    async def _keep_skipped_items(self, media_type: MediaType, cur_db_ids: set[int]) -> None:
        """
        Add the library id's of the items the provider skipped to this run's result set.

        A skipped item is still in the provider's library, so leaving it out would let the
        deletion pass read it as removed.
        """
        if not (skipped_item_ids := sorted(sync_run_state().skipped_item_ids.get(media_type, ()))):
            return
        controller = self.mass.music.get_controller(media_type)
        for index in range(0, len(skipped_item_ids), SKIPPED_ITEM_QUERY_LIMIT):
            for library_item in await controller.get_library_items_by_prov_id(
                provider_instance=self.instance_id,
                provider_item_ids=skipped_item_ids[index : index + SKIPPED_ITEM_QUERY_LIMIT],
                limit=SKIPPED_ITEM_QUERY_LIMIT,
            ):
                cur_db_ids.add(int(library_item.item_id))

    def _protect_failed_sync_item(
        self,
        media_type: MediaType,
        provider_item_id: str | None,
        library_item_id: int | None,
        cur_db_ids: set[int],
    ) -> None:
        """Keep a failed item out of this run's deletion pass."""
        if library_item_id is not None:
            cur_db_ids.add(library_item_id)
        elif provider_item_id:
            sync_run_state().skipped_item_ids.setdefault(media_type, set()).add(provider_item_id)
        else:
            sync_run_state().incomplete_media_types.add(media_type)

    async def _sync_item_genres(
        self,
        media_type: MediaType,
        provider_item_id: str,
        library_item_id: int,
        fallback_genres: set[str] | None = None,
    ) -> None:
        try:
            genre_names = await self.get_item_genre_names(media_type, provider_item_id)
        except NotImplementedError:
            if fallback_genres is None:
                return
            genre_names = fallback_genres

        await self.mass.music.genres.sync_media_item_genres(
            media_type, library_item_id, set(genre_names)
        )

    async def _sync_library_artists(self) -> set[int]:
        """Sync Library Artists to Music Assistant library."""
        self.logger.debug("Start sync of Artists to Music Assistant library.")
        cur_db_ids: set[int] = set()
        item_count = 0
        async for prov_item in self.get_library_artists():
            item_count += 1
            self._update_sync_task_item_status(MediaType.ARTIST, item_count, prov_item.name)
            db_id: int | None = None
            try:
                sync_details = await self.mass.music.artists.get_library_item_sync_details(
                    prov_item.provider_mappings,
                )
                db_id = sync_details.item_id if sync_details else None
                # batch all writes for this item into a single commit
                async with self.mass.music.database.deferred_commit():
                    if not sync_details:
                        # add item to the library
                        for prov_map in prov_item.provider_mappings:
                            prov_map.in_library = True
                        library_item = await self.mass.music.artists.add_item_to_library(prov_item)
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                    elif self._library_item_needs_update(sync_details, prov_item):
                        library_item = await self.mass.music.artists.update_item_in_library(
                            sync_details.item_id, prov_item
                        )
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                    else:
                        db_id = sync_details.item_id
                        favorite = sync_details.favorite
                    cur_db_ids.add(db_id)
                    if not favorite and prov_item.favorite:
                        # existing library item not favorite but should be
                        await self.mass.music.artists.set_favorite(db_id, True)
                    fallback_genres = (
                        set(prov_item.metadata.genres)
                        if prov_item.metadata and prov_item.metadata.genres
                        else None
                    )
                    await self._sync_item_genres(
                        MediaType.ARTIST,
                        prov_item.item_id,
                        db_id,
                        fallback_genres,
                    )
                await asyncio.sleep(0)  # yield to eventloop
            except Exception as err:
                self._handle_sync_item_failure(MediaType.ARTIST, prov_item.uri, err)
                self._protect_failed_sync_item(
                    MediaType.ARTIST, prov_item.item_id, db_id, cur_db_ids
                )
        return cur_db_ids

    def library_sync_album_tracks_enabled(self) -> bool:
        """Return whether all tracks of an album should be imported into the library."""
        return bool(
            self.config.get_value(
                CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS.key,
                CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS.default_value,
            )
        )

    async def _sync_library_albums(self) -> set[int]:
        """Sync Library Albums to Music Assistant library."""
        self.logger.debug("Start sync of Albums to Music Assistant library.")
        cur_db_ids: set[int] = set()
        sync_album_tracks = self.library_sync_album_tracks_enabled()
        item_count = 0
        async for prov_item in self.get_library_albums():
            item_count += 1
            self._update_sync_task_item_status(MediaType.ALBUM, item_count, prov_item.name)
            db_id: int | None = None
            try:
                sync_details = await self.mass.music.albums.get_library_item_sync_details(
                    prov_item.provider_mappings,
                )
                db_id = sync_details.item_id if sync_details else None
                # batch all writes for this item into a single commit
                async with self.mass.music.database.deferred_commit():
                    if not sync_details:
                        # add item to the library
                        for prov_map in prov_item.provider_mappings:
                            prov_map.in_library = True
                        library_item = await self.mass.music.albums.add_item_to_library(prov_item)
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                    elif self._library_item_needs_update(sync_details, prov_item):
                        library_item = await self.mass.music.albums.update_item_in_library(
                            sync_details.item_id, prov_item
                        )
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                    else:
                        db_id = sync_details.item_id
                        favorite = sync_details.favorite
                    cur_db_ids.add(db_id)
                    if not favorite and prov_item.favorite:
                        # existing library item not favorite but should be
                        await self.mass.music.albums.set_favorite(db_id, True)
                    fallback_genres = (
                        set(prov_item.metadata.genres)
                        if prov_item.metadata and prov_item.metadata.genres
                        else None
                    )
                    await self._sync_item_genres(
                        MediaType.ALBUM,
                        prov_item.item_id,
                        db_id,
                        fallback_genres,
                    )
                await asyncio.sleep(0)  # yield to eventloop
            except Exception as err:
                self._handle_sync_item_failure(MediaType.ALBUM, prov_item.uri, err)
                self._protect_failed_sync_item(
                    MediaType.ALBUM, prov_item.item_id, db_id, cur_db_ids
                )
                continue
            # optionally add album tracks to library. the album is already collected here,
            # so failing to import its tracks does not make the album result set incomplete
            if sync_album_tracks:
                try:
                    await self.import_album_tracks(prov_item.item_id, prov_item)
                except Exception as err:
                    self._handle_sync_item_failure(MediaType.ALBUM, prov_item.uri, err)
        return cur_db_ids

    async def import_album_tracks(self, prov_album_id: str, album: Album | None = None) -> None:
        """
        Import all tracks of the given (provider) album into the Music Assistant library.

        :param prov_album_id: The provider item id of the album.
        :param album: The album the tracks belong to.
            Fetched from the provider when not given.
        """
        self.logger.debug(
            "Importing Album Tracks into the Music Assistant library for album %s.",
            album.name if album else prov_album_id,
        )
        prov_tracks = await self.get_album_tracks(prov_album_id)
        # some providers leave the (redundant) album off the tracks in an album listing.
        # without it the track is stored unfiled, so resolve it once for the whole import.
        if album is None and any(prov_track.album is None for prov_track in prov_tracks):
            with suppress(MusicAssistantError, NotImplementedError):
                album = await self.get_album(prov_album_id)
        album_mapping = ItemMapping.from_item(album) if album else None
        for item_count, prov_track in enumerate(prov_tracks, start=1):
            self._update_sync_task_item_status(MediaType.TRACK, item_count, prov_track.name)
            try:
                if prov_track.album is None and album_mapping is not None:
                    prov_track.album = album_mapping
                sync_details = cast(
                    "TrackSyncDetails | None",
                    await self.mass.music.tracks.get_library_item_sync_details(
                        prov_track.provider_mappings,
                    ),
                )
                # batch all writes for this item into a single commit
                async with self.mass.music.database.deferred_commit():
                    if not sync_details:
                        # add item to the library
                        for prov_map in prov_track.provider_mappings:
                            prov_map.in_library = True
                        library_track = await self.mass.music.tracks.add_item_to_library(prov_track)
                        db_id = int(library_track.item_id)
                    elif (
                        not self._check_provider_mappings(sync_details, prov_track, True)
                        # existing library track but provider mapping doesn't match
                        # or backfill a missing album(_tracks) link for existing tracks
                        or (prov_track.album and not sync_details.has_album)
                    ):
                        library_track = await self.mass.music.tracks.update_item_in_library(
                            sync_details.item_id, prov_track
                        )
                        db_id = int(library_track.item_id)
                    else:
                        db_id = sync_details.item_id
                    fallback_genres = (
                        set(prov_track.metadata.genres)
                        if prov_track.metadata and prov_track.metadata.genres
                        else None
                    )
                    await self._sync_item_genres(
                        MediaType.TRACK,
                        prov_track.item_id,
                        db_id,
                        fallback_genres,
                    )
                await asyncio.sleep(0)  # yield to eventloop
            except Exception as err:
                self._handle_sync_item_failure(MediaType.TRACK, prov_track.uri, err)

    def _validate_audiobook_author_narrator_types(self, prov_item: Audiobook) -> None:
        """
        Validate of correct artist and artist types.

        If a provider supports artists of type Author or Narrator, they have to be part of an audiobook instance.
        Otherwise only strings are allowed.
        """
        if ArtistType.AUTHOR in self.supported_artist_types and not all(
            (isinstance(author, Artist) and author.artist_type == ArtistType.AUTHOR)
            for author in prov_item.authors
        ):
            raise InvalidDataError(
                f"Provider {self.name} supports ArtistType.AUTHOR, but"
                f" item {prov_item.name} does not exclusively provide Artist instances "
                "with ArtistType.AUTHOR set."
            )
        if ArtistType.NARRATOR in self.supported_artist_types and not all(
            (isinstance(narrator, Artist) and narrator.artist_type == ArtistType.NARRATOR)
            for narrator in prov_item.narrators
        ):
            raise InvalidDataError(
                f"Provider {self.name} supports ArtistType.NARRATOR, but"
                f" item {prov_item.name} does not exclusively provide Artist instances "
                "with ArtistType.NARRATOR set."
            )
        if ArtistType.AUTHOR not in self.supported_artist_types and not all(
            isinstance(author, str) for author in prov_item.authors
        ):
            raise InvalidDataError(
                f"Provider {self.name} does not support artists of type author, but"
                f" item {prov_item.name} does not exclusively provide strings."
            )
        if ArtistType.NARRATOR not in self.supported_artist_types and not all(
            isinstance(narrator, str) for narrator in prov_item.narrators
        ):
            raise InvalidDataError(
                f"Provider {self.name} does not support artists of type narrator, but"
                f" item {prov_item.name} does not exclusively provide strings."
            )

    async def _sync_library_audiobooks(self) -> set[int]:
        """Sync Library Audiobooks to Music Assistant library."""
        self.logger.debug("Start sync of Audiobooks to Music Assistant library.")
        cur_db_ids: set[int] = set()
        item_count = 0
        async for prov_item in self.get_library_audiobooks():
            item_count += 1
            self._update_sync_task_item_status(MediaType.AUDIOBOOK, item_count, prov_item.name)
            db_id: int | None = None
            try:
                sync_details = cast(
                    "AudiobookSyncDetails | None",
                    await self.mass.music.audiobooks.get_library_item_sync_details(
                        prov_item.provider_mappings,
                    ),
                )
                db_id = sync_details.item_id if sync_details else None
                self._validate_audiobook_author_narrator_types(prov_item)
                # batch all writes for this item into a single commit
                async with self.mass.music.database.deferred_commit():
                    if not sync_details:
                        # add item to the library
                        for prov_map in prov_item.provider_mappings:
                            prov_map.in_library = True
                        library_item = await self.mass.music.audiobooks.add_item_to_library(
                            prov_item
                        )
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                        lib_fully_played = library_item.fully_played
                        lib_resume_position_ms = library_item.resume_position_ms
                    elif self._library_item_needs_update(sync_details, prov_item):
                        library_item = await self.mass.music.audiobooks.update_item_in_library(
                            sync_details.item_id, prov_item
                        )
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                        lib_fully_played = library_item.fully_played
                        lib_resume_position_ms = library_item.resume_position_ms
                    else:
                        # Detect, if stored authors/narrators are plain strings but the provider
                        # now supplies full Artist objects, i.e. artist support changed.
                        prov_author = prov_item.authors[0] if prov_item.authors else None
                        prov_narrator = prov_item.narrators[0] if prov_item.narrators else None
                        if (sync_details.author_is_str and not isinstance(prov_author, str)) or (
                            sync_details.narrator_is_str and not isinstance(prov_narrator, str)
                        ):
                            library_item = await self.mass.music.audiobooks.update_item_in_library(
                                sync_details.item_id, prov_item
                            )
                            db_id = int(library_item.item_id)
                            favorite = library_item.favorite
                            lib_fully_played = library_item.fully_played
                            lib_resume_position_ms = library_item.resume_position_ms
                        else:
                            db_id = sync_details.item_id
                            favorite = sync_details.favorite
                            lib_fully_played = sync_details.fully_played
                            lib_resume_position_ms = sync_details.resume_position_ms

                    cur_db_ids.add(db_id)
                    if not favorite and prov_item.favorite:
                        # existing library item not favorite but should be
                        await self.mass.music.audiobooks.set_favorite(db_id, True)
                    # check if resume_position_ms or fully_played changed
                    if (
                        prov_item.resume_position_ms is not None
                        and prov_item.fully_played is not None
                        and (
                            lib_resume_position_ms != prov_item.resume_position_ms
                            or lib_fully_played != prov_item.fully_played
                        )
                    ):
                        await self.mass.music.audiobooks.update_item_in_library(db_id, prov_item)

                    fallback_genres = (
                        set(prov_item.metadata.genres)
                        if prov_item.metadata and prov_item.metadata.genres
                        else None
                    )
                    await self._sync_item_genres(
                        MediaType.AUDIOBOOK,
                        prov_item.item_id,
                        db_id,
                        fallback_genres,
                    )

                await asyncio.sleep(0)  # yield to eventloop
            except Exception as err:
                self._handle_sync_item_failure(MediaType.AUDIOBOOK, prov_item.uri, err)
                self._protect_failed_sync_item(
                    MediaType.AUDIOBOOK, prov_item.item_id, db_id, cur_db_ids
                )
        return cur_db_ids

    async def _sync_library_playlists(self) -> set[int]:
        """Sync Library Playlists to Music Assistant library."""
        self.logger.debug("Start sync of Playlists to Music Assistant library.")
        conf_sync_playlist_tracks = self.config.get_value(
            CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS.key,
            CONF_ENTRY_LIBRARY_SYNC_PLAYLIST_TRACKS.default_value,
        )
        conf_sync_playlist_tracks = cast("list[str]", conf_sync_playlist_tracks)
        cur_db_ids: set[int] = set()
        item_count = 0
        async for prov_item in self.get_library_playlists():
            item_count += 1
            self._update_sync_task_item_status(MediaType.PLAYLIST, item_count, prov_item.name)
            db_id: int | None = None
            try:
                library_item = await self.mass.music.playlists.get_library_item_by_prov_mappings(
                    prov_item.provider_mappings,
                )
                db_id = int(library_item.item_id) if library_item else None
                # batch all writes for this item into a single commit
                async with self.mass.music.database.deferred_commit():
                    if not library_item:
                        # add item to the library
                        for prov_map in prov_item.provider_mappings:
                            prov_map.in_library = True
                        library_item = await self.mass.music.playlists.add_item_to_library(
                            prov_item
                        )
                    elif (
                        self._library_item_needs_update(library_item, prov_item)
                        # or the supported mediatypes changed
                        or prov_item.supported_mediatypes != library_item.supported_mediatypes
                    ):
                        library_item = await self.mass.music.playlists.update_item_in_library(
                            library_item.item_id, prov_item
                        )
                    elif (
                        prov_item.is_dynamic
                        and not library_item.is_editable
                        and (
                            prov_item.name != library_item.name
                            or prov_item.metadata.images != library_item.metadata.images
                        )
                    ):
                        # the provider is the sole source of truth for non-editable dynamic
                        # playlists (e.g. Pandora/personalized-radio stations): overwrite=True
                        # replaces the full stored record (not just name/images), which is fine
                        # here since there's no local customization on these to lose. Restricted
                        # to is_dynamic so static non-editable playlists (e.g. provider
                        # "favorites") keep their locally-enriched metadata/images.
                        library_item = await self.mass.music.playlists.update_item_in_library(
                            library_item.item_id, prov_item, overwrite=True
                        )
                    db_id = int(library_item.item_id)
                    cur_db_ids.add(db_id)
                    if not library_item.favorite and prov_item.favorite:
                        # existing library item not favorite but should be
                        await self.mass.music.playlists.set_favorite(library_item.item_id, True)
                await asyncio.sleep(0)  # yield to eventloop
            except Exception as err:
                self._handle_sync_item_failure(MediaType.PLAYLIST, prov_item.uri, err)
                self._protect_failed_sync_item(
                    MediaType.PLAYLIST, prov_item.item_id, db_id, cur_db_ids
                )
                continue
            # optionally sync playlist tracks. the playlist is already collected here, so
            # failing on its tracks does not make the playlist result set incomplete
            if (
                prov_item.name in conf_sync_playlist_tracks
                or prov_item.uri in conf_sync_playlist_tracks
            ):
                try:
                    await self._sync_playlist_tracks(prov_item)
                except Exception as err:
                    self._handle_sync_item_failure(MediaType.PLAYLIST, prov_item.uri, err)
        return cur_db_ids

    async def _sync_playlist_tracks(self, provider_playlist: Playlist) -> None:
        """Sync Playlist Tracks to Music Assistant library."""
        self.logger.debug(
            "Start sync of Playlist Tracks to Music Assistant library for playlist %s.",
            provider_playlist.name,
        )
        item_count = 0
        async for _prov_track in self.iter_playlist_tracks(provider_playlist.item_id):
            prov_track: PlaylistPlayableItem | Podcast = _prov_track
            item_count += 1
            try:
                if isinstance(_prov_track, PodcastEpisode):
                    # In MA, only full podcasts can be synced to the library
                    prov_track = await self.get_podcast(_prov_track.podcast.item_id)
                self._update_sync_task_item_status(MediaType.TRACK, item_count, prov_track.name)
                controller = self.mass.music.get_controller(prov_track.media_type)
                sync_details = await controller.get_library_item_sync_details(
                    prov_track.provider_mappings,
                )
                # batch all writes for this item into a single commit
                async with self.mass.music.database.deferred_commit():
                    if not sync_details:
                        # add item to the library
                        for prov_map in prov_track.provider_mappings:
                            prov_map.in_library = True
                        library_track = await controller.add_item_to_library(prov_track)  # type: ignore[arg-type]
                        db_id = int(library_track.item_id)
                    elif not self._check_provider_mappings(sync_details, prov_track, True):
                        # existing library track but provider mapping doesn't match
                        library_track = await controller.update_item_in_library(
                            sync_details.item_id,
                            prov_track,  # type: ignore[arg-type]
                        )
                        db_id = int(library_track.item_id)
                    else:
                        db_id = sync_details.item_id
                    fallback_genres = (
                        set(prov_track.metadata.genres)
                        if prov_track.metadata and prov_track.metadata.genres
                        else None
                    )
                    await self._sync_item_genres(
                        MediaType.TRACK,
                        prov_track.item_id,
                        db_id,
                        fallback_genres,
                    )
                await asyncio.sleep(0)  # yield to eventloop
            except Exception as err:
                self._handle_sync_item_failure(MediaType.TRACK, prov_track.uri, err)

    async def _sync_library_tracks(self) -> set[int]:
        """Sync Library Tracks to Music Assistant library."""
        self.logger.debug("Start sync of Tracks to Music Assistant library.")
        cur_db_ids: set[int] = set()
        item_count = 0
        async for prov_item in self.get_library_tracks():
            item_count += 1
            self._update_sync_task_item_status(MediaType.TRACK, item_count, prov_item.name)
            db_id: int | None = None
            try:
                sync_details = cast(
                    "TrackSyncDetails | None",
                    await self.mass.music.tracks.get_library_item_sync_details(
                        prov_item.provider_mappings,
                    ),
                )
                db_id = sync_details.item_id if sync_details else None
                if not sync_details and not prov_item.available:
                    # skip unavailable tracks
                    # TODO: do we want to search for substitutes at this point ?
                    self.logger.debug(
                        "Skipping sync of track %s because it is unavailable",
                        prov_item.uri,
                    )
                    continue
                # batch all writes for this item into a single commit
                async with self.mass.music.database.deferred_commit():
                    if not sync_details:
                        # add item to the library
                        for prov_map in prov_item.provider_mappings:
                            prov_map.in_library = True
                        library_item = await self.mass.music.tracks.add_item_to_library(prov_item)
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                    elif (
                        self._library_item_needs_update(sync_details, prov_item)
                        # or backfill a missing album(_tracks) link for existing tracks
                        or (prov_item.album and not sync_details.has_album)
                        # or backfill missing track_artists link(s) for existing tracks
                        or (prov_item.artists and not sync_details.has_artists)
                    ):
                        library_item = await self.mass.music.tracks.update_item_in_library(
                            sync_details.item_id, prov_item
                        )
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                    else:
                        db_id = sync_details.item_id
                        favorite = sync_details.favorite
                    cur_db_ids.add(db_id)
                    if not favorite and prov_item.favorite:
                        # existing library item not favorite but should be
                        await self.mass.music.tracks.set_favorite(db_id, True)
                    fallback_genres = (
                        set(prov_item.metadata.genres)
                        if prov_item.metadata and prov_item.metadata.genres
                        else None
                    )
                    await self._sync_item_genres(
                        MediaType.TRACK,
                        prov_item.item_id,
                        db_id,
                        fallback_genres,
                    )
                await asyncio.sleep(0)  # yield to eventloop
            except Exception as err:
                self._handle_sync_item_failure(MediaType.TRACK, prov_item.uri, err)
                self._protect_failed_sync_item(
                    MediaType.TRACK, prov_item.item_id, db_id, cur_db_ids
                )
        return cur_db_ids

    async def _sync_library_podcasts(self) -> set[int]:
        """Sync Library Podcasts to Music Assistant library."""
        self.logger.debug("Start sync of Podcasts to Music Assistant library.")
        cur_db_ids: set[int] = set()
        item_count = 0
        async for prov_item in self.get_library_podcasts():
            item_count += 1
            self._update_sync_task_item_status(MediaType.PODCAST, item_count, prov_item.name)
            db_id: int | None = None
            try:
                sync_details = await self.mass.music.podcasts.get_library_item_sync_details(
                    prov_item.provider_mappings,
                )
                db_id = sync_details.item_id if sync_details else None
                # batch all writes for this item into a single commit
                async with self.mass.music.database.deferred_commit():
                    if not sync_details:
                        # add item to the library
                        for prov_map in prov_item.provider_mappings:
                            prov_map.in_library = True
                        library_item = await self.mass.music.podcasts.add_item_to_library(prov_item)
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                    elif self._library_item_needs_update(sync_details, prov_item):
                        library_item = await self.mass.music.podcasts.update_item_in_library(
                            sync_details.item_id, prov_item
                        )
                        db_id = int(library_item.item_id)
                        favorite = library_item.favorite
                    else:
                        db_id = sync_details.item_id
                        favorite = sync_details.favorite
                    cur_db_ids.add(db_id)
                    if not favorite and prov_item.favorite:
                        # existing library item not favorite but should be
                        await self.mass.music.podcasts.set_favorite(db_id, True)
                    fallback_genres = (
                        set(prov_item.metadata.genres)
                        if prov_item.metadata and prov_item.metadata.genres
                        else None
                    )
                    await self._sync_item_genres(
                        MediaType.PODCAST,
                        prov_item.item_id,
                        db_id,
                        fallback_genres,
                    )
                await asyncio.sleep(0)  # yield to eventloop
            except Exception as err:
                self._handle_sync_item_failure(MediaType.PODCAST, prov_item.uri, err)
                self._protect_failed_sync_item(
                    MediaType.PODCAST, prov_item.item_id, db_id, cur_db_ids
                )
                continue
            # the podcast is already collected here, so a feed that fails to deliver its
            # episodes does not make the podcast result set incomplete
            try:
                # precache podcast episodes
                async for _ in self.mass.music.podcasts.episodes(str(db_id), "library"):
                    await asyncio.sleep(0)  # yield to eventloop
            except Exception as err:
                self._handle_sync_item_failure(MediaType.PODCAST, prov_item.uri, err)
        return cur_db_ids

    async def _sync_library_radios(self) -> set[int]:
        """Sync Library Radios to Music Assistant library."""
        self.logger.debug("Start sync of Radios to Music Assistant library.")
        cur_db_ids: set[int] = set()
        item_count = 0
        async for prov_item in self.get_library_radios():
            item_count += 1
            self._update_sync_task_item_status(MediaType.RADIO, item_count, prov_item.name)
            db_id: int | None = None
            try:
                library_item = await self.mass.music.radio.get_library_item_by_prov_mappings(
                    prov_item.provider_mappings,
                )
                db_id = int(library_item.item_id) if library_item else None
                # batch all writes for this item into a single commit
                async with self.mass.music.database.deferred_commit():
                    if not library_item:
                        # add item to the library
                        for prov_map in prov_item.provider_mappings:
                            prov_map.in_library = True
                        library_item = await self.mass.music.radio.add_item_to_library(prov_item)
                    elif prov_item.is_dynamic and (
                        not library_item.is_dynamic
                        or prov_item.name != library_item.name
                        or prov_item.metadata.images != library_item.metadata.images
                    ):
                        # must overwrite: merging keeps mappings that serve the wrong tracks
                        for prov_map in prov_item.provider_mappings:
                            prov_map.in_library = True  # overwrite re-inserts the rows
                        library_item = await self.mass.music.radio.update_item_in_library(
                            library_item.item_id, prov_item, overwrite=True
                        )
                    elif self._library_item_needs_update(library_item, prov_item) or (
                        library_item.is_dynamic and not prov_item.is_dynamic
                    ):
                        # a station leaving dynamic mode is no longer provider-owned, so merge
                        library_item = await self.mass.music.radio.update_item_in_library(
                            library_item.item_id, prov_item
                        )
                    db_id = int(library_item.item_id)
                    cur_db_ids.add(db_id)
                    if not library_item.favorite and prov_item.favorite:
                        # existing library item not favorite but should be
                        await self.mass.music.radio.set_favorite(library_item.item_id, True)
                await asyncio.sleep(0)  # yield to eventloop

            except Exception as err:
                self._handle_sync_item_failure(MediaType.RADIO, prov_item.uri, err)
                self._protect_failed_sync_item(
                    MediaType.RADIO, prov_item.item_id, db_id, cur_db_ids
                )
        return cur_db_ids

    # DO NOT OVERRIDE BELOW

    def get_default_library_sync_schedule(self, media_type: MediaType) -> TaskSchedule:
        """Return the default recurring schedule for library sync tasks of this provider."""
        if not self.mass.music.library_supported(self, media_type):
            raise UnsupportedFeaturedException(
                f"Library sync is not supported for {media_type} on {self.instance_id}"
            )
        return TaskSchedule.hourly(every=12)

    def library_sync_deletions_enabled(self) -> bool:
        """Return if Library sync deletions is enabled for this provider."""
        conf_value = self.config.get_value(
            CONF_ENTRY_LIBRARY_SYNC_DELETIONS.key, CONF_ENTRY_LIBRARY_SYNC_DELETIONS.default_value
        )
        return bool(conf_value)

    async def iter_playlist_tracks(
        self,
        prov_playlist_id: str,
    ) -> AsyncGenerator[PlaylistPlayableItem]:
        """Iterate playlist tracks for the given provider playlist id."""
        page = 0
        while True:
            tracks = await self.get_playlist_tracks(
                prov_playlist_id,
                page=page,
            )
            if not tracks:
                break
            for track in tracks:
                yield track
            page += 1

    def _get_library_gen(self, media_type: MediaType) -> AsyncGenerator[MediaItemType]:
        """Return library generator for given media_type."""
        if media_type == MediaType.ARTIST:
            return self.get_library_artists()
        if media_type == MediaType.ALBUM:
            return self.get_library_albums()
        if media_type == MediaType.TRACK:
            return self.get_library_tracks()
        if media_type == MediaType.PLAYLIST:
            return self.get_library_playlists()
        if media_type == MediaType.RADIO:
            return self.get_library_radios()
        if media_type == MediaType.AUDIOBOOK:
            return self.get_library_audiobooks()
        if media_type == MediaType.PODCAST:
            return self.get_library_podcasts()
        raise NotImplementedError

    def _library_item_needs_update(
        self, library_item: MediaItemType | LibraryItemSyncDetails, prov_item: MediaItemType
    ) -> bool:
        """Return True if the library item needs an update from the given provider item."""
        if not self._check_provider_mappings(library_item, prov_item, True):
            # provider mapping doesn't match the library item
            return True
        # the item's date_added changed on the provider
        return bool(prov_item.date_added and library_item.date_added != prov_item.date_added)

    def _check_provider_mappings(
        self,
        library_item: MediaItemType | LibraryItemSyncDetails,
        provider_item: MediaItemType,
        in_library: bool,
    ) -> bool:
        """Check if provider mapping(s) are consistent between library and provider items."""
        for provider_mapping in provider_item.provider_mappings:
            if provider_mapping.item_id != provider_item.item_id:
                # this should never happen, but guard against it
                raise MusicAssistantError("Inconsistent provider mapping item_id found")
            if provider_mapping.provider_instance != self.instance_id:
                # this should never happen, but guard against it
                raise MusicAssistantError("Inconsistent provider mapping instance_id found")
            # check if the provider mapping matches the library item
            provider_mapping.in_library = in_library
            library_mapping = next(
                (
                    x
                    for x in library_item.provider_mappings
                    if x.provider_instance == provider_mapping.provider_instance
                    and x.item_id == provider_mapping.item_id
                ),
                None,
            )
            if not library_mapping:
                return False
            if provider_mapping.in_library != library_mapping.in_library:
                # in-library status doesn't match
                return False
            if provider_mapping.is_unique != library_mapping.is_unique:
                # unique status doesn't match
                return False
            # check if the library item has all provider instances mappings
            is_unique = provider_mapping.is_unique or (not self.is_streaming_provider)
            if not is_unique:
                # for streaming providers we need to make sure all provider instances
                # for this domain are represented in the provider mappings
                prov_instances = self.mass.music.get_provider_instances(
                    domain=provider_mapping.provider_domain,
                    return_unavailable=True,
                )
                if len(prov_instances) > 1:
                    # multiple provider instances for this domain exist
                    # make sure the library item has all provider mappings
                    for prov_instance in prov_instances:
                        if not any(
                            x.provider_instance == prov_instance.instance_id
                            and x.item_id == provider_mapping.item_id
                            for x in library_item.provider_mappings
                        ):
                            # missing provider mapping for another instance
                            # the rest of the core logic will take care of adding it
                            # just return False here to trigger that logic
                            return False

            # final check: availability
            return provider_mapping.available == library_mapping.available
        return False
