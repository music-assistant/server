"""Audiobookshelf (abs) provider for Music Assistant."""

from __future__ import annotations

import asyncio
import functools
import itertools
import time
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

import aioaudiobookshelf as aioabs
from aioaudiobookshelf.client.items import LibraryItemExpandedBook as AbsLibraryItemExpandedBook
from aioaudiobookshelf.client.items import (
    LibraryItemExpandedPodcast as AbsLibraryItemExpandedPodcast,
)
from aioaudiobookshelf.exceptions import LoginError as AbsLoginError
from aioaudiobookshelf.exceptions import RefreshTokenExpiredError
from aioaudiobookshelf.schema.author import AuthorExpanded
from aioaudiobookshelf.schema.calls_authors import (
    AuthorWithItemsAndSeries as AbsAuthorWithItemsAndSeries,
)
from aioaudiobookshelf.schema.calls_series import SeriesWithProgress as AbsSeriesWithProgress
from aioaudiobookshelf.schema.library import (
    LibraryItemExpanded,
    LibraryItemExpandedBook,
    LibraryItemExpandedPodcast,
    LibraryItemMinifiedPodcast,
)
from aioaudiobookshelf.schema.library import LibraryMediaType as AbsLibraryMediaType
from aioaudiobookshelf.schema.shelf import (
    SeriesShelf,
    ShelfAuthors,
    ShelfBook,
    ShelfEpisode,
    ShelfLibraryItemMinified,
    ShelfPodcast,
    ShelfSeries,
)
from aioaudiobookshelf.schema.shelf import ShelfId as AbsShelfId
from aioaudiobookshelf.schema.shelf import ShelfType as AbsShelfType
from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    PodcastEpisode,
    UniqueList,
)
from music_assistant_models.media_items.media_item import RecommendationFolder
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.audiobookshelf.parsers import (
    parse_audiobook,
    parse_podcast,
    parse_podcast_episode,
)

from .constants import (
    ABS_BROWSE_ITEMS_TO_PATH,
    ABS_SHELF_ID_ICONS,
    ABS_SHELF_ID_TRANSLATION_KEY,
    AIOHTTP_TIMEOUT,
    CACHE_CATEGORY_LIBRARIES,
    CACHE_KEY_LIBRARIES,
    CONF_API_TOKEN,
    CONF_HIDE_EMPTY_PODCASTS,
    CONF_OLD_TOKEN,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    AbsBrowseItemsBookTranslationKey,
    AbsBrowseItemsPodcastTranslationKey,
    AbsBrowsePaths,
)
from .helpers import LibrariesHelper, LibraryHelper, ProgressGuard

if TYPE_CHECKING:
    from aioaudiobookshelf.schema.events_socket import LibraryItemRemoved
    from aioaudiobookshelf.schema.media_progress import MediaProgress
    from aioaudiobookshelf.schema.user import User
    from music_assistant_models.media_items import Podcast
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.BROWSE,
    ProviderFeature.RECOMMENDATIONS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return Audiobookshelf(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key="label",
            type=ConfigEntryType.LABEL,
            label="Please provide the address of your Audiobookshelf instance. To authenticate "
            "you have two options: "
            "a) Provide username AND password. Leave the API key empty. "
            "b) Provide ONLY an API key.",
        ),
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The URL of the Audiobookshelf server to connect to. For example "
            "https://abs.domain.tld/ or http://192.168.1.4:13378/",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=False,
            description="The username to authenticate to the remote server.",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            description="The password to authenticate to the remote server.",
        ),
        ConfigEntry(
            key=CONF_API_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="API key _instead_ of user/ password. (ABS version >= 2.26)",
            required=False,
            description="Instead of using a username and password, "
            "you may provide an API key (ABS version >= 2.26). "
            "Please consult the docs.",
        ),
        ConfigEntry(
            key=CONF_OLD_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="old token",
            required=False,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_VERIFY_SSL,
            type=ConfigEntryType.BOOLEAN,
            label="Verify SSL",
            required=False,
            description="Whether or not to verify the certificate of SSL/TLS connections.",
            category="advanced",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_HIDE_EMPTY_PODCASTS,
            type=ConfigEntryType.BOOLEAN,
            label="Hide empty podcasts.",
            required=False,
            description="This will skip podcasts with no episodes associated.",
            category="advanced",
            default_value=False,
        ),
    )


R = TypeVar("R")
P = ParamSpec("P")


class Audiobookshelf(MusicProvider):
    """Audiobookshelf MusicProvider."""

    _on_unload_callbacks: list[Callable[[], None]]

    @staticmethod
    def handle_refresh_token(
        method: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        """Decorate a method to handle an expired refresh token by relogin."""

        @functools.wraps(method)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            self = cast("Audiobookshelf", args[0])
            try:
                return await method(*args, **kwargs)
            except RefreshTokenExpiredError:
                self.logger.debug("Refresh token expired. Trying to renew.")
                await self.reauthenticate()
                return await method(*args, **kwargs)

        return wrapper

    async def handle_async_init(self) -> None:
        """Pass config values to client and initialize."""
        self._on_unload_callbacks: list[Callable[[], None]] = []
        base_url = str(self.config.get_value(CONF_URL))
        username = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD))
        token_old = self.config.get_value(CONF_OLD_TOKEN)
        token_api = self.config.get_value(CONF_API_TOKEN)
        verify_ssl = bool(self.config.get_value(CONF_VERIFY_SSL))
        session_config = aioabs.SessionConfiguration(
            session=self.mass.http_session,
            url=base_url,
            verify_ssl=verify_ssl,
            logger=self.logger,
            pagination_items_per_page=30,  # audible provider goes with 50 for pagination
            timeout=AIOHTTP_TIMEOUT,
        )
        try:
            if token_api is not None or token_old is not None:
                _token = token_api if token_api is not None else token_old
                session_config.token = str(_token)
                (
                    self._client,
                    self._client_socket,
                ) = await aioabs.get_user_and_socket_client_by_token(session_config=session_config)
            else:
                self._client, self._client_socket = await aioabs.get_user_and_socket_client(
                    session_config=session_config, username=username, password=password
                )
            await self._client_socket.init_client()
        except AbsLoginError as exc:
            raise LoginFailed(f"Login to abs instance at {base_url} failed.") from exc

        if token_old is not None and token_api is None:
            # Log Message that the old token won't work
            _version = self._client.server_settings.version.split(".")
            if len(_version) >= 2:
                try:
                    major, minor = int(_version[0]), int(_version[1])
                except ValueError:
                    major = minor = 0
                if major >= 2 and minor >= 26:
                    self.logger.warning(
                        """

######## Audiobookshelf API key change #############################################################

Audiobookshelf introduced a new API key system in version 2.26 (JWT).
You are still using a token configured with a previous version of Audiobookshelf,
but you are running version %s. This will stop working in a future Audiobookshelf release.
Please create a non-expiring API Key instead, and update your configuration accordingly.
Refer to the documentation of Audiobookshelf, https://www.audiobookshelf.org/guides/api-keys/
and of Music Assistant https://www.music-assistant.io/music-providers/audiobookshelf/
for more details.

""",
                        self._client.server_settings.version,
                    )

        cached_libraries = await self.mass.cache.get(
            key=CACHE_KEY_LIBRARIES,
            provider=self.instance_id,
            category=CACHE_CATEGORY_LIBRARIES,
            default=None,
        )
        if cached_libraries is None:
            self.libraries = LibrariesHelper()
            # We need the library ids for recommendations. If the cache got cleared e.g. by a db
            # migration, we might end up with empty library helpers on a configured provider. Note,
            # that the lib item ids are not synced, still only on full provider sync, instead the
            # sets are empty. Full sync is expensive.
            # See warning in browse_lib_podcasts / _browse_books
            libraries = await self._client.get_all_libraries()
            for library in libraries:
                if library.media_type == AbsLibraryMediaType.BOOK:
                    self.libraries.audiobooks[library.id_] = LibraryHelper(name=library.name)
                elif library.media_type == AbsLibraryMediaType.PODCAST:
                    self.libraries.podcasts[library.id_] = LibraryHelper(name=library.name)
        else:
            self.libraries = LibrariesHelper.from_dict(cached_libraries)

        # set socket callbacks
        self._client_socket.set_item_callbacks(
            on_item_added=self._socket_abs_item_changed,
            on_item_updated=self._socket_abs_item_changed,
            on_item_removed=self._socket_abs_item_removed,
            on_items_added=self._socket_abs_item_changed,
            on_items_updated=self._socket_abs_item_changed,
        )

        self._client_socket.set_user_callbacks(
            on_user_item_progress_updated=self._socket_abs_user_item_progress_updated,
        )

        self._client_socket.set_refresh_token_expired_callback(
            on_refresh_token_expired=self._socket_abs_refresh_token_expired
        )

        # progress guard
        self.progress_guard = ProgressGuard()

        # safe guard reauthentication
        self.reauthenticate_lock = asyncio.Lock()
        self.reauthenticate_last = 0.0

        # register dynamic stream route for audiobook parts
        self._on_unload_callbacks.append(
            self.mass.streams.register_dynamic_route(
                f"/{self.instance_id}_part_stream", self._handle_audiobook_part_request
            )
        )

    @handle_refresh_token
    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        await self._client.logout()
        await self._client_socket.logout()
        for callback in self._on_unload_callbacks:
            callback()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return False

    @handle_refresh_token
    async def sync_library(self, media_type: MediaType) -> None:
        """Obtain audiobook library ids and podcast library ids."""
        libraries = await self._client.get_all_libraries()
        if len(libraries) == 0:
            self._log_no_libraries()
        for library in libraries:
            if library.media_type == AbsLibraryMediaType.BOOK and media_type == MediaType.AUDIOBOOK:
                self.libraries.audiobooks[library.id_] = LibraryHelper(name=library.name)
            elif (
                library.media_type == AbsLibraryMediaType.PODCAST
                and media_type == MediaType.PODCAST
            ):
                self.libraries.podcasts[library.id_] = LibraryHelper(name=library.name)
        await super().sync_library(media_type)
        await self._cache_set_helper_libraries()

        # update playlog
        user = await self._client.get_my_user()
        await self._set_playlog_from_user(user)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider.

        Minified podcast information is enough.
        """
        for pod_lib_id in self.libraries.podcasts:
            async for response in self._client.get_library_items(library_id=pod_lib_id):
                if not response.results:
                    break
                podcast_ids = [x.id_ for x in response.results]
                # store uuids
                self.libraries.podcasts[pod_lib_id].item_ids.update(podcast_ids)
                for podcast_minified in response.results:
                    assert isinstance(podcast_minified, LibraryItemMinifiedPodcast)
                    mass_podcast = parse_podcast(
                        abs_podcast=podcast_minified,
                        instance_id=self.instance_id,
                        domain=self.domain,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    )
                    if (
                        bool(self.config.get_value(CONF_HIDE_EMPTY_PODCASTS))
                        and mass_podcast.total_episodes == 0
                    ):
                        continue
                    yield mass_podcast

    @handle_refresh_token
    async def _get_abs_expanded_podcast(
        self, prov_podcast_id: str
    ) -> AbsLibraryItemExpandedPodcast:
        abs_podcast = await self._client.get_library_item_podcast(
            podcast_id=prov_podcast_id, expanded=True
        )
        assert isinstance(abs_podcast, AbsLibraryItemExpandedPodcast)

        return abs_podcast

    @handle_refresh_token
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get single podcast."""
        abs_podcast = await self._get_abs_expanded_podcast(prov_podcast_id=prov_podcast_id)
        return parse_podcast(
            abs_podcast=abs_podcast,
            instance_id=self.instance_id,
            domain=self.domain,
            token=self._client.token,
            base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
        )

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all podcast episodes of podcast.

        Adds progress information.
        """
        abs_podcast = await self._get_abs_expanded_podcast(prov_podcast_id=prov_podcast_id)
        episode_cnt = 1
        # the user has the progress of all media items
        # so we use a single api call here to obtain possibly many
        # progresses for episodes
        user = await self._client.get_my_user()
        abs_progresses = {
            x.episode_id: x
            for x in user.media_progress
            if x.episode_id is not None and x.library_item_id == prov_podcast_id
        }
        for abs_episode in abs_podcast.media.episodes:
            progress = abs_progresses.get(abs_episode.id_, None)
            mass_episode = parse_podcast_episode(
                episode=abs_episode,
                prov_podcast_id=prov_podcast_id,
                fallback_episode_cnt=episode_cnt,
                instance_id=self.instance_id,
                domain=self.domain,
                token=self._client.token,
                base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                media_progress=progress,
            )
            yield mass_episode
            episode_cnt += 1

    @handle_refresh_token
    async def get_podcast_episode(
        self, prov_episode_id: str, add_progress: bool = True
    ) -> PodcastEpisode:
        """Get single podcast episode."""
        prov_podcast_id, e_id = prov_episode_id.split(" ")
        abs_podcast = await self._get_abs_expanded_podcast(prov_podcast_id=prov_podcast_id)
        episode_cnt = 1
        for abs_episode in abs_podcast.media.episodes:
            if abs_episode.id_ == e_id:
                progress = None
                if add_progress:
                    progress = await self._client.get_my_media_progress(
                        item_id=prov_podcast_id, episode_id=abs_episode.id_
                    )
                return parse_podcast_episode(
                    episode=abs_episode,
                    prov_podcast_id=prov_podcast_id,
                    fallback_episode_cnt=episode_cnt,
                    instance_id=self.instance_id,
                    domain=self.domain,
                    token=self._client.token,
                    base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    media_progress=progress,
                )

            episode_cnt += 1
        raise MediaNotFoundError("Episode not found")

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook, None]:
        """Get Audiobook libraries.

        Need expanded version for chapters.
        """
        for book_lib_id in self.libraries.audiobooks:
            async for response in self._client.get_library_items(library_id=book_lib_id):
                if not response.results:
                    break
                book_ids = [x.id_ for x in response.results]
                # store uuids
                self.libraries.audiobooks[book_lib_id].item_ids.update(book_ids)
                # use expanded version for chapters/ caching.
                books_expanded = await self._client.get_library_item_batch_book(item_ids=book_ids)
                for book_expanded in books_expanded:
                    # If the book has no audiofiles, we skip -> ebook only.
                    if len(book_expanded.media.tracks) == 0:
                        continue
                    mass_audiobook = parse_audiobook(
                        abs_audiobook=book_expanded,
                        instance_id=self.instance_id,
                        domain=self.domain,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    )
                    yield mass_audiobook

    @handle_refresh_token
    async def _get_abs_expanded_audiobook(
        self, prov_audiobook_id: str
    ) -> AbsLibraryItemExpandedBook:
        abs_audiobook = await self._client.get_library_item_book(
            book_id=prov_audiobook_id, expanded=True
        )
        assert isinstance(abs_audiobook, AbsLibraryItemExpandedBook)

        return abs_audiobook

    @handle_refresh_token
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get a single audiobook.

        Progress is added here.
        """
        progress = await self._client.get_my_media_progress(item_id=prov_audiobook_id)
        abs_audiobook = await self._get_abs_expanded_audiobook(prov_audiobook_id=prov_audiobook_id)
        return parse_audiobook(
            abs_audiobook=abs_audiobook,
            instance_id=self.instance_id,
            domain=self.domain,
            token=self._client.token,
            base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
            media_progress=progress,
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream of item."""
        if media_type == MediaType.PODCAST_EPISODE:
            return await self._get_stream_details_episode(item_id)
        elif media_type == MediaType.AUDIOBOOK:
            abs_audiobook = await self._get_abs_expanded_audiobook(prov_audiobook_id=item_id)
            return await self._get_stream_details_audiobook(abs_audiobook)
        raise MediaNotFoundError("Stream unknown")

    async def _get_stream_details_audiobook(
        self, abs_audiobook: AbsLibraryItemExpandedBook
    ) -> StreamDetails:
        """Streamdetails audiobook.

        We always use a custom stream type, also for single file, such
        that we can handle an ffmpeg error and refresh our tokens.
        """
        tracks = abs_audiobook.media.tracks
        if len(tracks) == 0:
            raise MediaNotFoundError("Stream not found")

        content_type = ContentType.UNKNOWN
        if abs_audiobook.media.tracks[0].metadata is not None:
            content_type = ContentType.try_parse(abs_audiobook.media.tracks[0].metadata.ext)

        file_parts: list[MultiPartPath] = []
        for idx, track in enumerate(tracks):
            # to ensure token is always valid, we create a dynamic url
            # this ensures that we always get a fresh token on each part
            # without having to deal with a custom stream etc.
            # we also use this for the first part, otherwise we can't seek
            stream_url = (
                f"{self.mass.streams.base_url}/{self.instance_id}_part_stream?"
                f"audiobook_id={abs_audiobook.id_}&part_id={idx}"
            )
            file_parts.append(MultiPartPath(path=stream_url, duration=track.duration))

        return StreamDetails(
            provider=self.instance_id,
            item_id=abs_audiobook.id_,
            audio_format=AudioFormat(content_type=content_type),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            duration=int(abs_audiobook.media.duration),
            path=file_parts,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_stream_details_episode(self, podcast_id: str) -> StreamDetails:
        """Streamdetails of a podcast episode.

        There are no multi-file podcasts in abs, but we use a custom
        stream to handle possible ffmpeg errors.
        """
        abs_podcast_id, abs_episode_id = podcast_id.split(" ")
        abs_episode = None

        abs_podcast = await self._get_abs_expanded_podcast(prov_podcast_id=abs_podcast_id)
        for abs_episode in abs_podcast.media.episodes:
            if abs_episode.id_ == abs_episode_id:
                break
        if abs_episode is None:
            raise MediaNotFoundError("Stream not found")
        self.logger.debug(f'Using direct playback for podcast episode "{abs_episode.title}".')
        content_type = ContentType.UNKNOWN
        if abs_episode.audio_track.metadata is not None:
            content_type = ContentType.try_parse(abs_episode.audio_track.metadata.ext)
        base_url = str(self.config.get_value(CONF_URL))
        stream_url = f"{base_url}{abs_episode.audio_track.content_url}?token={self._client.token}"
        return StreamDetails(
            provider=self.instance_id,
            item_id=podcast_id,
            audio_format=AudioFormat(
                content_type=content_type,
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            can_seek=True,
            allow_seek=True,
            path=stream_url,
        )

    async def _handle_audiobook_part_request(self, request: web.Request) -> web.Response:
        """
        Handle dynamic audiobook part stream request.

        We redirect to the actual stream url with token.
        This is done because the token might expire, so we need to
        generate a fresh url on each part.
        """
        if not (audiobook_id := request.query.get("audiobook_id")):
            return web.Response(status=400, text="Missing audiobook_id")
        if not (part_id := request.query.get("part_id")):
            return web.Response(status=400, text="Missing part_id")
        abs_audiobook = await self._get_abs_expanded_audiobook(prov_audiobook_id=audiobook_id)
        part_id = int(part_id)  # type: ignore[assignment]
        try:
            part_track = abs_audiobook.media.tracks[part_id]
        except IndexError:
            return web.Response(status=404, text="Part not found")

        base_url = str(self.config.get_value(CONF_URL))
        stream_url = f"{base_url}{part_track.content_url}?token={self._client.token}"
        # redirect to the actual stream url
        raise web.HTTPFound(location=stream_url)

    @handle_refresh_token
    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Return finished:bool, position_ms: int."""
        progress: None | MediaProgress = None
        if media_type == MediaType.PODCAST_EPISODE:
            abs_podcast_id, abs_episode_id = item_id.split(" ")
            progress = await self._client.get_my_media_progress(
                item_id=abs_podcast_id, episode_id=abs_episode_id
            )

        if media_type == MediaType.AUDIOBOOK:
            progress = await self._client.get_my_media_progress(item_id=item_id)

        if progress is not None and progress.current_time is not None:
            self.logger.debug("Resume position: obtained.")
            return progress.is_finished, int(progress.current_time * 1000)

        return False, 0

    @handle_refresh_token
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get recommendations."""
        # We have to avoid "flooding" the home page, which becomes especially troublesome if users
        # have multiple libraries. Instead we collect per ShelfId, and make sure, that we always get
        # roughly the same amount of items per row, no matter the amount of libraries
        # List of list (one list per lib) here, such that we can pick the items per lib later.
        items_by_shelf_id: dict[AbsShelfId, list[list[MediaItemType | BrowseFolder]]] = {}

        all_libraries = {**self.libraries.audiobooks, **self.libraries.podcasts}
        max_items_per_row = 20
        num_libraries = len(all_libraries)

        if num_libraries == 0:
            self._log_no_libraries()
            return []

        limit_items_per_lib = max_items_per_row // num_libraries
        limit_items_per_lib = 1 if limit_items_per_lib == 0 else limit_items_per_lib

        for library_id in all_libraries:
            shelves = await self._client.get_library_personalized_view(
                library_id=library_id, limit=limit_items_per_lib
            )
            await self._recommendations_iter_shelves(shelves, library_id, items_by_shelf_id)

        folders: list[RecommendationFolder] = []
        for shelf_id, item_lists in items_by_shelf_id.items():
            # we have something like [[A, B], [C, D, E], [F]]
            # and want [A, C, F, B, D, E]
            recommendation_items = [
                x
                for x in itertools.chain.from_iterable(itertools.zip_longest(*item_lists))
                if x is not None
            ][:max_items_per_row]

            # shelf ids follow pattern:
            # recently-added
            # newest-episodes
            # etc
            name = f"{shelf_id.capitalize().replace('-', ' ')}"
            if ABS_SHELF_ID_TRANSLATION_KEY.get(shelf_id):
                name = ""  # use translation key if available
            folders.append(
                RecommendationFolder(
                    item_id=f"{shelf_id}",
                    name=name,
                    icon=ABS_SHELF_ID_ICONS.get(shelf_id),
                    translation_key=ABS_SHELF_ID_TRANSLATION_KEY.get(shelf_id),
                    items=UniqueList(recommendation_items),
                    provider=self.instance_id,
                )
            )

        # Browse "recommendation" for convenience. If the user has
        # multiple audiobook libraries, we return a listing of them.
        # If there is only a single audiobook library, we add the folders
        # from _browse_lib_audiobooks, i.e. Authors, Narrators etc.
        # Podcast libs do not have filter folders, so always the root folders.
        browse_items: list[MediaItemType | BrowseFolder] = []
        translation_key = "libraries"
        if len(self.libraries.audiobooks) <= 1:
            if len(self.libraries.podcasts) == 0:
                translation_key = "library"

            # audiobooklibs are first, and we have at max 1 audiobook lib
            _browse_root = self._browse_root(append_mediatype_suffix=False)
            if len(self.libraries.audiobooks) == 0:
                browse_items.extend(_browse_root)
            else:
                assert isinstance(_browse_root[0], BrowseFolder)
                _path = _browse_root[0].path
                browse_items.extend(self._browse_lib_audiobooks(current_path=_path))
                # add podcast roots
                browse_items.extend(_browse_root[1:])
        else:
            browse_items = list(self._browse_root())

        folders.append(
            RecommendationFolder(
                item_id="browse",
                name="",  # use translation key
                icon="mdi-bookshelf",
                translation_key=translation_key,
                items=UniqueList(browse_items),
                provider=self.instance_id,
            )
        )

        return folders

    async def _recommendations_iter_shelves(
        self,
        shelves: list[ShelfBook | ShelfPodcast | ShelfAuthors | ShelfEpisode | ShelfSeries],
        library_id: str,
        items_by_shelf_id: dict[AbsShelfId, list[list[MediaItemType | BrowseFolder]]],
    ) -> None:
        for shelf in shelves:
            media_type: MediaType
            match shelf.type_:
                case AbsShelfType.PODCAST:
                    media_type = MediaType.PODCAST
                case AbsShelfType.EPISODE:
                    media_type = MediaType.PODCAST_EPISODE
                case AbsShelfType.BOOK:
                    media_type = MediaType.AUDIOBOOK
                case AbsShelfType.SERIES | AbsShelfType.AUTHORS:
                    media_type = MediaType.FOLDER
                case _:
                    # this would be authors, currently
                    continue

            items: list[MediaItemType | BrowseFolder] = []
            # Recently added is the _only_ case, where we get a full podcast
            # We have a podcast object with only the episodes matching the
            # shelf.id_ otherwise.
            match shelf.id_:
                case (
                    AbsShelfId.RECENTLY_ADDED
                    | AbsShelfId.LISTEN_AGAIN
                    | AbsShelfId.DISCOVER
                    | AbsShelfId.NEWEST_EPISODES
                    | AbsShelfId.CONTINUE_LISTENING
                ):
                    for entity in shelf.entities:
                        assert isinstance(entity, ShelfLibraryItemMinified)
                        item: MediaItemType | None = None
                        if media_type in [MediaType.PODCAST, MediaType.AUDIOBOOK]:
                            item = await self.mass.music.get_library_item_by_prov_id(
                                media_type=media_type,
                                provider_instance_id_or_domain=self.instance_id,
                                item_id=entity.id_,
                            )
                        elif media_type == MediaType.PODCAST_EPISODE:
                            podcast_id = entity.id_
                            if entity.recent_episode is None:
                                continue
                            # we only have a PodcastEpisode here, with limited information
                            item = parse_podcast_episode(
                                episode=entity.recent_episode,
                                prov_podcast_id=podcast_id,
                                instance_id=self.instance_id,
                                domain=self.domain,
                                token=self._client.token,
                                base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                            )
                        if item is not None:
                            items.append(item)
                case AbsShelfId.RECENT_SERIES | AbsShelfId.CONTINUE_SERIES:
                    # We jump into a browse folder here if we have SeriesShelf, set path up as if
                    # browse function used.
                    if isinstance(shelf, ShelfSeries):
                        for entity in shelf.entities:
                            assert isinstance(entity, SeriesShelf)
                            if len(entity.books) == 0:
                                continue
                            path = (
                                f"{self.instance_id}://"
                                f"{AbsBrowsePaths.LIBRARIES_BOOK} {library_id}/"
                                f"{AbsBrowsePaths.SERIES}/{entity.id_}"
                            )
                            items.append(
                                BrowseFolder(
                                    item_id=entity.id_,
                                    name=entity.name,
                                    provider=self.instance_id,
                                    path=path,
                                )
                            )
                    elif isinstance(shelf, ShelfBook) and media_type == MediaType.AUDIOBOOK:
                        # Single books, must be audiobooks
                        for entity in shelf.entities:
                            item = await self.mass.music.get_library_item_by_prov_id(
                                media_type=media_type,
                                provider_instance_id_or_domain=self.instance_id,
                                item_id=entity.id_,
                            )
                            if item is not None:
                                items.append(item)
                case AbsShelfId.NEWEST_AUTHORS:
                    # same as for series, use a folder
                    for entity in shelf.entities:
                        assert isinstance(entity, AuthorExpanded)
                        if entity.num_books == 0:
                            continue
                        path = (
                            f"{self.instance_id}://"
                            f"{AbsBrowsePaths.LIBRARIES_BOOK} {library_id}/"
                            f"{AbsBrowsePaths.AUTHORS}/{entity.id_}"
                        )
                        items.append(
                            BrowseFolder(
                                item_id=entity.id_,
                                name=entity.name,
                                provider=self.instance_id,
                                path=path,
                            )
                        )
            if not items:
                continue

            # add collected items
            assert isinstance(shelf.id_, AbsShelfId)
            items_collected = items_by_shelf_id.get(shelf.id_, [])
            items_collected.append(items)
            items_by_shelf_id[shelf.id_] = items_collected

    @handle_refresh_token
    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Update progress in Audiobookshelf.

        In our case media_type may have 3 values:
            - PODCAST
            - PODCAST_EPISODE
            - AUDIOBOOK
        We ignore PODCAST (function is called on adding a podcast with position=None)

        """
        if media_type == MediaType.PODCAST_EPISODE:
            abs_podcast_id, abs_episode_id = prov_item_id.split(" ")

            # guard, see progress guard class docstrings for explanation
            if not self.progress_guard.guard_ok_mass(
                item_id=abs_podcast_id, episode_id=abs_episode_id
            ):
                return
            self.progress_guard.add_progress(item_id=abs_podcast_id, episode_id=abs_episode_id)

            if media_item is None or not isinstance(media_item, PodcastEpisode):
                return

            if position == 0 and not fully_played:
                # marked unplayed
                mp = await self._client.get_my_media_progress(
                    item_id=abs_podcast_id, episode_id=abs_episode_id
                )
                if mp is not None:
                    await self._client.remove_my_media_progress(media_progress_id=mp.id_)
                    self.logger.debug(f"Removed media progress of {media_type.value}.")
                    return

            duration = media_item.duration
            self.logger.debug(
                f"Updating media progress of {media_type.value}, title {media_item.name}."
            )
            await self._client.update_my_media_progress(
                item_id=abs_podcast_id,
                episode_id=abs_episode_id,
                duration_seconds=duration,
                progress_seconds=position,
                is_finished=fully_played,
            )

        if media_type == MediaType.AUDIOBOOK:
            # guard, see progress guard class docstrings for explanation
            if not self.progress_guard.guard_ok_mass(item_id=prov_item_id):
                return
            self.progress_guard.add_progress(item_id=prov_item_id)

            if media_item is None or not isinstance(media_item, Audiobook):
                return

            if position == 0 and not fully_played:
                # marked unplayed
                mp = await self._client.get_my_media_progress(item_id=prov_item_id)
                if mp is not None:
                    await self._client.remove_my_media_progress(media_progress_id=mp.id_)
                    self.logger.debug(f"Removed media progress of {media_type.value}.")
                return

            duration = media_item.duration
            self.logger.debug(f"Updating {media_type.value} named {media_item.name} progress")
            await self._client.update_my_media_progress(
                item_id=prov_item_id,
                duration_seconds=duration,
                progress_seconds=position,
                is_finished=fully_played,
            )

    @handle_refresh_token
    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse for audiobookshelf.

        Generates this view:
        Library_Name_A (Audiobooks)
            Audiobooks
                Audiobook_1
                Audiobook_2
            Series
                Series_1
                    Audiobook_1
                    Audiobook_2
                Series_2
                    Audiobook_3
                    Audiobook_4
            Collections
                Collection_1
                    Audiobook_1
                    Audiobook_2
                Collection_2
                    Audiobook_3
                    Audiobook_4
            Authors
                Author_1
                    Series_1
                    Audiobook_1
                    Audiobook_2
                Author_2
                    Audiobook_3
        Library_Name_B (Podcasts)
            Podcast_1
            Podcast_2
        """
        item_path = path.split("://", 1)[1]
        if not item_path:
            return self._browse_root()
        sub_path = item_path.split("/")
        lib_key, lib_id = sub_path[0].split(" ")
        if len(sub_path) == 1:
            if lib_key == AbsBrowsePaths.LIBRARIES_PODCAST:
                return await self._browse_lib_podcasts(library_id=lib_id)
            else:
                return self._browse_lib_audiobooks(current_path=path)
        elif len(sub_path) == 2:
            item_key = sub_path[1]
            match item_key:
                case AbsBrowsePaths.AUTHORS:
                    return await self._browse_authors(current_path=path, library_id=lib_id)
                case AbsBrowsePaths.NARRATORS:
                    return await self._browse_narrators(current_path=path, library_id=lib_id)
                case AbsBrowsePaths.SERIES:
                    return await self._browse_series(current_path=path, library_id=lib_id)
                case AbsBrowsePaths.COLLECTIONS:
                    return await self._browse_collections(current_path=path, library_id=lib_id)
                case AbsBrowsePaths.AUDIOBOOKS:
                    return await self._browse_books(library_id=lib_id)
        elif len(sub_path) == 3:
            item_key, item_id = sub_path[1:3]
            match item_key:
                case AbsBrowsePaths.AUTHORS:
                    return await self._browse_author_books(current_path=path, author_id=item_id)
                case AbsBrowsePaths.NARRATORS:
                    return await self._browse_narrator_books(
                        library_id=lib_id, narrator_filter_str=item_id
                    )
                case AbsBrowsePaths.SERIES:
                    return await self._browse_series_books(series_id=item_id)
                case AbsBrowsePaths.COLLECTIONS:
                    return await self._browse_collection_books(collection_id=item_id)
        elif len(sub_path) == 4:
            # series within author
            series_id = sub_path[3]
            return await self._browse_series_books(series_id=series_id)
        return []

    def _browse_root(self, append_mediatype_suffix: bool = True) -> Sequence[BrowseFolder]:
        items = []

        def _get_folder(
            path: str, lib_id: str, lib_name: str, translation_key: str | None = None
        ) -> BrowseFolder:
            return BrowseFolder(
                item_id=lib_id,
                name=lib_name,
                translation_key=translation_key,  # if given, <name>: <translation> in frontend
                provider=self.instance_id,
                path=f"{self.instance_id}://{path}",
            )

        if len(self.libraries.audiobooks) == 0 and len(self.libraries.podcasts) == 0:
            self._log_no_libraries()
            return []

        translation_key: str | None
        for lib_id, lib in self.libraries.audiobooks.items():
            path = f"{AbsBrowsePaths.LIBRARIES_BOOK} {lib_id}"
            translation_key = None
            if append_mediatype_suffix:
                translation_key = AbsBrowseItemsBookTranslationKey.AUDIOBOOKS
            items.append(
                _get_folder(path, lib_id, lib_name=lib.name, translation_key=translation_key)
            )
        for lib_id, lib in self.libraries.podcasts.items():
            path = f"{AbsBrowsePaths.LIBRARIES_PODCAST} {lib_id}"
            translation_key = None
            if append_mediatype_suffix:
                translation_key = AbsBrowseItemsPodcastTranslationKey.PODCASTS
            items.append(
                _get_folder(path, lib_id, lib_name=lib.name, translation_key=translation_key)
            )
        return items

    async def _browse_lib_podcasts(self, library_id: str) -> list[MediaItemType]:
        """No sub categories for podcasts."""
        if len(self.libraries.podcasts[library_id].item_ids) == 0:
            self._log_no_helper_item_ids()
        items = []
        for podcast_id in self.libraries.podcasts[library_id].item_ids:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.PODCAST,
                item_id=podcast_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)
        return sorted(items, key=lambda x: x.name)

    def _browse_lib_audiobooks(self, current_path: str) -> Sequence[BrowseFolder]:
        items = []
        for translation_key in AbsBrowseItemsBookTranslationKey:
            path = current_path + "/" + ABS_BROWSE_ITEMS_TO_PATH[translation_key]
            items.append(
                BrowseFolder(
                    item_id=translation_key.lower(),
                    name="",  # use translation key
                    translation_key=translation_key,
                    provider=self.instance_id,
                    path=path,
                )
            )
        return items

    async def _browse_authors(self, current_path: str, library_id: str) -> Sequence[BrowseFolder]:
        abs_authors = await self._client.get_library_authors(library_id=library_id)
        items = []
        for author in abs_authors:
            path = f"{current_path}/{author.id_}"
            items.append(
                BrowseFolder(
                    item_id=author.id_,
                    name=author.name,
                    provider=self.instance_id,
                    path=path,
                )
            )

        return sorted(items, key=lambda x: x.name)

    async def _browse_narrators(self, current_path: str, library_id: str) -> Sequence[BrowseFolder]:
        abs_narrators = await self._client.get_library_narrators(library_id=library_id)
        items = []
        for narrator in abs_narrators:
            path = f"{current_path}/{narrator.id_}"
            items.append(
                BrowseFolder(
                    item_id=narrator.id_,
                    name=narrator.name,
                    provider=self.instance_id,
                    path=path,
                )
            )

        return sorted(items, key=lambda x: x.name)

    async def _browse_series(self, current_path: str, library_id: str) -> Sequence[BrowseFolder]:
        items = []
        async for response in self._client.get_library_series(library_id=library_id):
            if not response.results:
                break
            for abs_series in response.results:
                path = f"{current_path}/{abs_series.id_}"
                items.append(
                    BrowseFolder(
                        item_id=abs_series.id_,
                        name=abs_series.name,
                        provider=self.instance_id,
                        path=path,
                    )
                )

        return sorted(items, key=lambda x: x.name)

    async def _browse_collections(
        self, current_path: str, library_id: str
    ) -> Sequence[BrowseFolder]:
        items = []
        async for response in self._client.get_library_collections(library_id=library_id):
            if not response.results:
                break
            for abs_collection in response.results:
                path = f"{current_path}/{abs_collection.id_}"
                items.append(
                    BrowseFolder(
                        item_id=abs_collection.id_,
                        name=abs_collection.name,
                        provider=self.instance_id,
                        path=path,
                    )
                )
        return sorted(items, key=lambda x: x.name)

    async def _browse_books(self, library_id: str) -> Sequence[MediaItemType]:
        if len(self.libraries.audiobooks[library_id].item_ids) == 0:
            self._log_no_helper_item_ids()
        items = []
        for book_id in self.libraries.audiobooks[library_id].item_ids:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)
        return sorted(items, key=lambda x: x.name)

    async def _browse_author_books(
        self, current_path: str, author_id: str
    ) -> Sequence[MediaItemType | BrowseFolder]:
        items: list[MediaItemType | BrowseFolder] = []

        abs_author = await self._client.get_author(
            author_id=author_id, include_items=True, include_series=True
        )
        if not isinstance(abs_author, AbsAuthorWithItemsAndSeries):
            raise TypeError("Unexpected type of author.")

        book_ids = {x.id_ for x in abs_author.library_items}
        series_book_ids = set()

        for series in abs_author.series:
            series_book_ids.update([x.id_ for x in series.items])
            path = f"{current_path}/{series.id_}"
            items.append(
                BrowseFolder(
                    item_id=series.id_,
                    # frontend does <name>: <translation>
                    name=series.name,
                    translation_key="series_singular",
                    provider=self.instance_id,
                    path=path,
                )
            )
        book_ids = book_ids.difference(series_book_ids)
        for book_id in book_ids:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)

        return items

    async def _browse_narrator_books(
        self, library_id: str, narrator_filter_str: str
    ) -> Sequence[MediaItemType]:
        items: list[MediaItemType] = []
        async for response in self._client.get_library_items(
            library_id=library_id, filter_str=f"narrators.{narrator_filter_str}"
        ):
            if not response.results:
                break
            for item in response.results:
                mass_item = await self.mass.music.get_library_item_by_prov_id(
                    media_type=MediaType.AUDIOBOOK,
                    item_id=item.id_,
                    provider_instance_id_or_domain=self.instance_id,
                )
                if mass_item is not None:
                    items.append(mass_item)

        return sorted(items, key=lambda x: x.name)

    async def _browse_series_books(self, series_id: str) -> Sequence[MediaItemType]:
        items = []

        abs_series = await self._client.get_series(series_id=series_id, include_progress=True)
        if not isinstance(abs_series, AbsSeriesWithProgress):
            raise TypeError("Unexpected series type.")

        for book_id in abs_series.progress.library_item_ids:
            # these are sorted in abs by sequence
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book_id,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)

        return items

    async def _browse_collection_books(self, collection_id: str) -> Sequence[MediaItemType]:
        items = []
        abs_collection = await self._client.get_collection(collection_id=collection_id)
        for book in abs_collection.books:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=MediaType.AUDIOBOOK,
                item_id=book.id_,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                items.append(mass_item)
        return items

    async def _socket_abs_item_changed(
        self, items: LibraryItemExpanded | list[LibraryItemExpanded]
    ) -> None:
        """For added and updated."""
        abs_items = [items] if isinstance(items, LibraryItemExpanded) else items
        for abs_item in abs_items:
            if isinstance(abs_item, LibraryItemExpandedBook):
                # If the book has no audiofiles, we skip -> ebook only.
                if len(abs_item.media.tracks) == 0:
                    continue
                self.logger.debug(
                    'Updated book "%s" via socket.', abs_item.media.metadata.title or ""
                )
                await self.mass.music.audiobooks.add_item_to_library(
                    parse_audiobook(
                        abs_audiobook=abs_item,
                        instance_id=self.instance_id,
                        domain=self.domain,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    ),
                    overwrite_existing=True,
                )
                lib = self.libraries.audiobooks.get(abs_item.library_id, None)
                if lib is not None:
                    lib.item_ids.add(abs_item.id_)
            elif isinstance(abs_item, LibraryItemExpandedPodcast):
                self.logger.debug(
                    'Updated podcast "%s" via socket.', abs_item.media.metadata.title or ""
                )
                mass_podcast = parse_podcast(
                    abs_podcast=abs_item,
                    instance_id=self.instance_id,
                    domain=self.domain,
                    token=self._client.token,
                    base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                )
                if not (
                    bool(self.config.get_value(CONF_HIDE_EMPTY_PODCASTS))
                    and mass_podcast.total_episodes == 0
                ):
                    await self.mass.music.podcasts.add_item_to_library(
                        mass_podcast,
                        overwrite_existing=True,
                    )
                    lib = self.libraries.podcasts.get(abs_item.library_id, None)
                    if lib is not None:
                        lib.item_ids.add(abs_item.id_)
        await self._cache_set_helper_libraries()

    async def _socket_abs_item_removed(self, item: LibraryItemRemoved) -> None:
        """Item removed."""
        media_type: MediaType | None = None
        for lib in self.libraries.audiobooks.values():
            if item.id_ in lib.item_ids:
                media_type = MediaType.AUDIOBOOK
                lib.item_ids.remove(item.id_)
                break
        for lib in self.libraries.podcasts.values():
            if item.id_ in lib.item_ids:
                media_type = MediaType.PODCAST
                lib.item_ids.remove(item.id_)
                break

        if media_type is not None:
            mass_item = await self.mass.music.get_library_item_by_prov_id(
                media_type=media_type,
                item_id=item.id_,
                provider_instance_id_or_domain=self.instance_id,
            )
            if mass_item is not None:
                await self.mass.music.remove_item_from_library(
                    media_type=media_type, library_item_id=mass_item.item_id
                )
                self.logger.debug('Removed %s "%s" via socket.', media_type.value, mass_item.name)

        await self._cache_set_helper_libraries()

    async def _socket_abs_user_item_progress_updated(
        self, id_: str, progress: MediaProgress
    ) -> None:
        """To update continue listening.

        ABS reports every 15s and immediately on play state change.
        This callback is called per item if a progress is changed:
            - a change in position
            - the item is finished
        But it is _not_called, if a progress is reset/ discarded.
        """
        # guard, see progress guard class docstrings for explanation
        if not self.progress_guard.guard_ok_abs(abs_progress=progress):
            return

        known_ids = self._get_all_known_item_ids()
        if progress.library_item_id not in known_ids:
            return

        self.logger.debug(f"Updated progress of item {progress.library_item_id} via socket.")

        if progress.episode_id is None:
            await self._update_playlog_book(progress)
            return
        await self._update_playlog_episode(progress)

    async def _socket_abs_refresh_token_expired(self) -> None:
        await self.reauthenticate()

    async def reauthenticate(self) -> None:
        """Reauthorize the abs session config if refresh token expired."""
        # some safe guarding should that function be called simultaneously
        if self.reauthenticate_lock.locked() or time.time() - self.reauthenticate_last < 5:
            while True:
                if not self.reauthenticate_lock.locked():
                    return
                await asyncio.sleep(0.5)
        async with self.reauthenticate_lock:
            await self._client.session_config.authenticate(
                username=str(self.config.get_value(CONF_USERNAME)),
                password=str(self.config.get_value(CONF_PASSWORD)),
            )
            self.reauthenticate_last = time.time()

    def _get_all_known_item_ids(self) -> set[str]:
        known_ids = set()
        for lib in self.libraries.podcasts.values():
            known_ids.update(lib.item_ids)
        for lib in self.libraries.audiobooks.values():
            known_ids.update(lib.item_ids)

        return known_ids

    async def _set_playlog_from_user(self, user: User) -> None:
        """Update on user callback.

        User holds also all media progresses specific to that user.

        The function 'guard_ok_abs' uses the timestamp of the last update in abs, thus after an
        initial progress update, an unchanged update will not trigger a (useless) playlog update.

        We do not sync removed progresses for the sake of simplicity.
        """
        await self._set_playlog_from_user_sync(user.media_progress)

    async def _set_playlog_from_user_sync(self, progresses: list[MediaProgress]) -> None:
        # for debugging
        __updated_items = 0

        known_ids = self._get_all_known_item_ids()
        abs_ids_with_progress = set()

        for progress in progresses:
            # save progress ids for later
            ma_item_id = (
                progress.library_item_id
                if progress.episode_id is None
                else f"{progress.library_item_id} {progress.episode_id}"
            )
            abs_ids_with_progress.add(ma_item_id)

            # Guard. Also makes sure, that we don't write to db again if no state change happened.
            # This is achieved by adding a Helper Progress in the update playlog functions, which
            # then has the most recent timestamp. If a subsequent progress sent by abs has an older
            # timestamp, we do not update again.
            if not self.progress_guard.guard_ok_abs(progress):
                continue
            if progress.current_time is not None:
                if int(progress.current_time) != 0 and not progress.current_time >= 30:
                    # same as mass default, only > 30s
                    continue
            if progress.library_item_id not in known_ids:
                continue
            __updated_items += 1
            if progress.episode_id is None:
                await self._update_playlog_book(progress)
            else:
                await self._update_playlog_episode(progress)
        self.logger.debug(f"Updated {__updated_items} from full playlog.")

        # Get MA's known progresses of ABS.
        # In ABS the user may discard a progress, which removes the progress completely.
        # There is no socket notification for this event.
        ma_playlog_state = await self.mass.music.get_playlog_provider_item_ids(
            provider_instance_id=self.instance_id
        )
        ma_ids_with_progress = {x for _, x in ma_playlog_state}
        discarded_progress_ids = ma_ids_with_progress.difference(abs_ids_with_progress)
        for discarded_progress_id in discarded_progress_ids:
            if len(discarded_progress_id.split(" ")) == 1:
                if discarded_item := await self.mass.music.get_library_item_by_prov_id(
                    media_type=MediaType.AUDIOBOOK,
                    item_id=discarded_progress_id,
                    provider_instance_id_or_domain=self.instance_id,
                ):
                    self.progress_guard.add_progress(discarded_progress_id)
                    await self.mass.music.mark_item_unplayed(discarded_item)
            else:
                with suppress(MediaNotFoundError):
                    discarded_item = await self.get_podcast_episode(
                        prov_episode_id=discarded_progress_id, add_progress=False
                    )
                    self.progress_guard.add_progress(*discarded_progress_id.split(" "))
                    await self.mass.music.mark_item_unplayed(discarded_item)
            self.logger.debug("Discarded item %s ", discarded_progress_id)

    async def _update_playlog_book(self, progress: MediaProgress) -> None:
        # helper progress also ensures no useless progress updates,
        # see comment above
        self.progress_guard.add_progress(progress.library_item_id)
        if progress.current_time is None:
            return
        mass_audiobook = await self.mass.music.get_library_item_by_prov_id(
            media_type=MediaType.AUDIOBOOK,
            item_id=progress.library_item_id,
            provider_instance_id_or_domain=self.instance_id,
        )
        if mass_audiobook is None:
            return
        if int(progress.current_time) == 0:
            await self.mass.music.mark_item_unplayed(mass_audiobook)
        else:
            await self.mass.music.mark_item_played(
                mass_audiobook,
                fully_played=progress.is_finished,
                seconds_played=int(progress.current_time),
            )

    async def _update_playlog_episode(self, progress: MediaProgress) -> None:
        # helper progress also ensures no useless progress updates,
        # see comment above
        self.progress_guard.add_progress(progress.library_item_id, progress.episode_id)
        if progress.current_time is None:
            return
        _episode_id = f"{progress.library_item_id} {progress.episode_id}"
        try:
            # need to obtain full podcast, and then search for episode
            mass_episode = await self.get_podcast_episode(_episode_id, add_progress=False)
        except MediaNotFoundError:
            return
        if int(progress.current_time) == 0:
            await self.mass.music.mark_item_unplayed(mass_episode)
        else:
            await self.mass.music.mark_item_played(
                mass_episode,
                fully_played=progress.is_finished,
                seconds_played=int(progress.current_time),
            )

    async def _cache_set_helper_libraries(self) -> None:
        await self.mass.cache.set(
            key=CACHE_KEY_LIBRARIES,
            provider=self.instance_id,
            category=CACHE_CATEGORY_LIBRARIES,
            data=self.libraries.to_dict(),
        )

    def _log_no_libraries(self) -> None:
        self.logger.error("There are no libraries visible to the Audiobookshelf provider.")

    def _log_no_helper_item_ids(self) -> None:
        self.logger.warning(
            "Cached item ids are missing. "
            "Please trigger a full resync of the Audiobookshelf provider manually."
        )
