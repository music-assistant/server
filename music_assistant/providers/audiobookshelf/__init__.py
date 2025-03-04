"""Audiobookshelf (abs) provider for Music Assistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING

import aioaudiobookshelf as aioabs
from aioaudiobookshelf.client.items import LibraryItemExpandedBook as AbsLibraryItemExpandedBook
from aioaudiobookshelf.client.items import (
    LibraryItemExpandedPodcast as AbsLibraryItemExpandedPodcast,
)
from aioaudiobookshelf.exceptions import LoginError as AbsLoginError
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
    MediaItemType,
    MediaItemTypeOrItemMapping,
    PodcastEpisode,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.audiobookshelf.parsers import (
    parse_audiobook,
    parse_podcast,
    parse_podcast_episode,
)

from .constants import (
    ABSBROWSEITEMSTOPATH,
    CACHE_CATEGORY_LIBRARIES,
    CACHE_KEY_LIBRARIES,
    CONF_HIDE_EMPTY_PODCASTS,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    AbsBrowseItemsBook,
    AbsBrowseItemsPodcast,
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


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return Audiobookshelf(mass, manifest, config)


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
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The url of the Audiobookshelf server to connect to.",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
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


class Audiobookshelf(MusicProvider):
    """Audiobookshelf MusicProvider."""

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Features supported by this Provider."""
        return {
            ProviderFeature.LIBRARY_PODCASTS,
            ProviderFeature.LIBRARY_AUDIOBOOKS,
            ProviderFeature.BROWSE,
        }

    async def handle_async_init(self) -> None:
        """Pass config values to client and initialize."""
        base_url = str(self.config.get_value(CONF_URL))
        username = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD))
        verify_ssl = bool(self.config.get_value(CONF_VERIFY_SSL))
        session_config = aioabs.SessionConfiguration(
            session=self.mass.http_session,
            url=base_url,
            verify_ssl=verify_ssl,
            logger=self.logger,
            pagination_items_per_page=30,  # audible provider goes with 50 for pagination
        )
        try:
            self._client, self._client_socket = await aioabs.get_user_and_socket_client(
                session_config=session_config, username=username, password=password
            )
            await self._client_socket.init_client()
        except AbsLoginError as exc:
            raise LoginFailed(f"Login to abs instance at {base_url} failed.") from exc

        self.cache_base_key = self.instance_id

        cached_libraries = await self.mass.cache.get(
            key=CACHE_KEY_LIBRARIES,
            base_key=self.cache_base_key,
            category=CACHE_CATEGORY_LIBRARIES,
            default=None,
        )
        if cached_libraries is None:
            self.libraries = LibrariesHelper()
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

        # progress guard
        self.progress_guard = ProgressGuard()

        # update playlog information if just started
        user = await self._client.get_my_user()
        await self._set_playlog_from_user(user)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        await self._client.logout()
        await self._client_socket.logout()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return False

    async def sync_library(self, media_type: MediaType) -> None:
        """Obtain audiobook library ids and podcast library ids."""
        libraries = await self._client.get_all_libraries()
        for library in libraries:
            if library.media_type == AbsLibraryMediaType.BOOK and media_type == MediaType.AUDIOBOOK:
                self.libraries.audiobooks[library.id_] = LibraryHelper(name=library.name)
            elif (
                library.media_type == AbsLibraryMediaType.PODCAST
                and media_type == MediaType.PODCAST
            ):
                self.libraries.podcasts[library.id_] = LibraryHelper(name=library.name)
        await super().sync_library(media_type=media_type)
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
                        lookup_key=self.lookup_key,
                        domain=self.domain,
                        instance_id=self.instance_id,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    )
                    if (
                        bool(self.config.get_value(CONF_HIDE_EMPTY_PODCASTS))
                        and mass_podcast.total_episodes == 0
                    ):
                        continue
                    yield mass_podcast

    async def _get_abs_expanded_podcast(
        self, prov_podcast_id: str
    ) -> AbsLibraryItemExpandedPodcast:
        abs_podcast = await self._client.get_library_item_podcast(
            podcast_id=prov_podcast_id, expanded=True
        )
        assert isinstance(abs_podcast, AbsLibraryItemExpandedPodcast)

        return abs_podcast

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get single podcast."""
        abs_podcast = await self._get_abs_expanded_podcast(prov_podcast_id=prov_podcast_id)
        return parse_podcast(
            abs_podcast=abs_podcast,
            lookup_key=self.lookup_key,
            domain=self.domain,
            instance_id=self.instance_id,
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
                lookup_key=self.lookup_key,
                domain=self.domain,
                instance_id=self.instance_id,
                token=self._client.token,
                base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                media_progress=progress,
            )
            yield mass_episode
            episode_cnt += 1

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
                    lookup_key=self.lookup_key,
                    domain=self.domain,
                    instance_id=self.instance_id,
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
                    mass_audiobook = parse_audiobook(
                        abs_audiobook=book_expanded,
                        lookup_key=self.lookup_key,
                        domain=self.domain,
                        instance_id=self.instance_id,
                        token=self._client.token,
                        base_url=str(self.config.get_value(CONF_URL)).rstrip("/"),
                    )
                    yield mass_audiobook

    async def _get_abs_expanded_audiobook(
        self, prov_audiobook_id: str
    ) -> AbsLibraryItemExpandedBook:
        abs_audiobook = await self._client.get_library_item_book(
            book_id=prov_audiobook_id, expanded=True
        )
        assert isinstance(abs_audiobook, AbsLibraryItemExpandedBook)

        return abs_audiobook

    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get a single audiobook.

        Progress is added here.
        """
        progress = await self._client.get_my_media_progress(item_id=prov_audiobook_id)
        abs_audiobook = await self._get_abs_expanded_audiobook(prov_audiobook_id=prov_audiobook_id)
        return parse_audiobook(
            abs_audiobook=abs_audiobook,
            lookup_key=self.lookup_key,
            domain=self.domain,
            instance_id=self.instance_id,
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
        """Streamdetails audiobook."""
        tracks = abs_audiobook.media.tracks
        token = self._client.token
        base_url = str(self.config.get_value(CONF_URL))
        if len(tracks) == 0:
            raise MediaNotFoundError("Stream not found")
        if len(tracks) > 1:
            self.logger.debug("Using playback for multiple file audiobook.")
            multiple_files = []
            for track in tracks:
                media_url = track.content_url
                stream_url = f"{base_url}{media_url}?token={token}"
                content_type = ContentType.UNKNOWN
                if track.metadata is not None:
                    content_type = ContentType.try_parse(track.metadata.ext)
                multiple_files.append(
                    (AudioFormat(content_type=content_type), stream_url, track.duration)
                )

            return StreamDetails(
                provider=self.instance_id,
                item_id=abs_audiobook.id_,
                # for the concatanated stream, we need to use a pcm stream format
                audio_format=AudioFormat(
                    content_type=ContentType.PCM_S16LE,
                    sample_rate=44100,
                    bit_depth=16,
                    channels=2,
                ),
                media_type=MediaType.AUDIOBOOK,
                stream_type=StreamType.CUSTOM,
                duration=int(abs_audiobook.media.duration),
                data=multiple_files,
                allow_seek=True,
                can_seek=True,
            )

        self.logger.debug(
            f'Using direct playback for audiobook "{abs_audiobook.media.metadata.title}".'
        )

        track = abs_audiobook.media.tracks[0]
        media_url = track.content_url
        stream_url = f"{base_url}{media_url}?token={token}"
        content_type = ContentType.UNKNOWN
        if track.metadata is not None:
            content_type = ContentType.try_parse(track.metadata.ext)
        return StreamDetails(
            provider=self.lookup_key,
            item_id=abs_audiobook.id_,
            audio_format=AudioFormat(
                content_type=content_type,
            ),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            path=stream_url,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_stream_details_episode(self, podcast_id: str) -> StreamDetails:
        """Streamdetails of a podcast episode."""
        abs_podcast_id, abs_episode_id = podcast_id.split(" ")
        abs_episode = None

        abs_podcast = await self._get_abs_expanded_podcast(prov_podcast_id=abs_podcast_id)
        for abs_episode in abs_podcast.media.episodes:
            if abs_episode.id_ == abs_episode_id:
                break
        if abs_episode is None:
            raise MediaNotFoundError("Stream not found")
        self.logger.debug(f'Using direct playback for podcast episode "{abs_episode.title}".')
        token = self._client.token
        base_url = str(self.config.get_value(CONF_URL))
        media_url = abs_episode.audio_track.content_url
        full_url = f"{base_url}{media_url}?token={token}"
        content_type = ContentType.UNKNOWN
        if abs_episode.audio_track.metadata is not None:
            content_type = ContentType.try_parse(abs_episode.audio_track.metadata.ext)
        return StreamDetails(
            provider=self.lookup_key,
            item_id=podcast_id,
            audio_format=AudioFormat(
                content_type=content_type,
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=full_url,
            can_seek=True,
            allow_seek=True,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """
        Return the (custom) audio stream for the provider item.

        Only used for multi-file audiobooks.
        """
        stream_data: list[tuple[AudioFormat, str, float]] = streamdetails.data
        total_duration = 0.0
        for audio_format, chapter_file, chapter_duration in stream_data:
            total_duration += chapter_duration
            if total_duration < seek_position:
                continue
            seek_position_netto = round(
                max(0, seek_position - (total_duration - chapter_duration)), 2
            )
            self.logger.debug(chapter_file)
            async for chunk in get_ffmpeg_stream(
                chapter_file,
                input_format=audio_format,
                # output format is always pcm because we are sending
                # the result of multiple files as one big stream
                output_format=streamdetails.audio_format,
                extra_input_args=["-ss", str(seek_position_netto)] if seek_position_netto else [],
            ):
                yield chunk

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

    async def browse(self, path: str) -> Sequence[MediaItemTypeOrItemMapping]:
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

    def _browse_root(self) -> Sequence[MediaItemTypeOrItemMapping]:
        items = []

        def _get_folder(path: str, lib_id: str, lib_name: str) -> BrowseFolder:
            return BrowseFolder(
                item_id=lib_id,
                name=lib_name,
                provider=self.lookup_key,
                path=f"{self.instance_id}://{path}",
            )

        for lib_id, lib in self.libraries.audiobooks.items():
            path = f"{AbsBrowsePaths.LIBRARIES_BOOK} {lib_id}"
            name = f"{lib.name} ({AbsBrowseItemsBook.AUDIOBOOKS})"
            items.append(_get_folder(path, lib_id, name))
        for lib_id, lib in self.libraries.podcasts.items():
            path = f"{AbsBrowsePaths.LIBRARIES_PODCAST} {lib_id}"
            name = f"{lib.name} ({AbsBrowseItemsPodcast.PODCASTS})"
            items.append(_get_folder(path, lib_id, name))
        return items

    async def _browse_lib_podcasts(self, library_id: str) -> list[MediaItemTypeOrItemMapping]:
        """No sub categories for podcasts."""
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

    def _browse_lib_audiobooks(self, current_path: str) -> Sequence[MediaItemTypeOrItemMapping]:
        items = []
        for item_name in AbsBrowseItemsBook:
            path = current_path + "/" + ABSBROWSEITEMSTOPATH[item_name]
            items.append(
                BrowseFolder(
                    item_id=item_name.lower(),
                    name=item_name,
                    provider=self.lookup_key,
                    path=path,
                )
            )
        return items

    async def _browse_authors(
        self, current_path: str, library_id: str
    ) -> Sequence[MediaItemTypeOrItemMapping]:
        abs_authors = await self._client.get_library_authors(library_id=library_id)
        items = []
        for author in abs_authors:
            path = f"{current_path}/{author.id_}"
            items.append(
                BrowseFolder(
                    item_id=author.id_,
                    name=author.name,
                    provider=self.lookup_key,
                    path=path,
                )
            )

        return sorted(items, key=lambda x: x.name)

    async def _browse_narrators(
        self, current_path: str, library_id: str
    ) -> Sequence[MediaItemTypeOrItemMapping]:
        abs_narrators = await self._client.get_library_narrators(library_id=library_id)
        items = []
        for narrator in abs_narrators:
            path = f"{current_path}/{narrator.id_}"
            items.append(
                BrowseFolder(
                    item_id=narrator.id_,
                    name=narrator.name,
                    provider=self.lookup_key,
                    path=path,
                )
            )

        return sorted(items, key=lambda x: x.name)

    async def _browse_series(
        self, current_path: str, library_id: str
    ) -> Sequence[MediaItemTypeOrItemMapping]:
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
                        provider=self.lookup_key,
                        path=path,
                    )
                )

        return sorted(items, key=lambda x: x.name)

    async def _browse_collections(
        self, current_path: str, library_id: str
    ) -> Sequence[MediaItemTypeOrItemMapping]:
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
                        provider=self.lookup_key,
                        path=path,
                    )
                )
        return sorted(items, key=lambda x: x.name)

    async def _browse_books(self, library_id: str) -> Sequence[MediaItemTypeOrItemMapping]:
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
    ) -> Sequence[MediaItemTypeOrItemMapping]:
        items: list[MediaItemTypeOrItemMapping] = []

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
                    name=f"{series.name} ({AbsBrowseItemsBook.SERIES})",
                    provider=self.lookup_key,
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
    ) -> Sequence[MediaItemTypeOrItemMapping]:
        items: list[MediaItemTypeOrItemMapping] = []
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

    async def _browse_series_books(self, series_id: str) -> Sequence[MediaItemTypeOrItemMapping]:
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

    async def _browse_collection_books(
        self, collection_id: str
    ) -> Sequence[MediaItemTypeOrItemMapping]:
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
                self.logger.debug(
                    'Updated book "%s" via socket.', abs_item.media.metadata.title or ""
                )
                await self.mass.music.audiobooks.add_item_to_library(
                    parse_audiobook(
                        abs_audiobook=abs_item,
                        lookup_key=self.lookup_key,
                        domain=self.domain,
                        instance_id=self.instance_id,
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
                    lookup_key=self.lookup_key,
                    domain=self.domain,
                    instance_id=self.instance_id,
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

        for progress in progresses:
            # Guard. Also makes sure, that we don't write to db again if no state change happened.
            # This is achieved by adding a Helper Progress in the update playlog functions, which
            # then has the most recent timestamp. If a subsequent progress sent by abs has an older
            # timestamp, we do not update again.
            if not self.progress_guard.guard_ok_abs(progress):
                continue
            if progress.current_time is not None and not progress.current_time >= 30:
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
        await self.mass.music.mark_item_played(
            mass_episode,
            fully_played=progress.is_finished,
            seconds_played=int(progress.current_time),
        )

    async def _cache_set_helper_libraries(self) -> None:
        await self.mass.cache.set(
            key=CACHE_KEY_LIBRARIES,
            base_key=self.cache_base_key,
            category=CACHE_CATEGORY_LIBRARIES,
            data=self.libraries.to_dict(),
        )
