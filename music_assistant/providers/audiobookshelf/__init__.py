"""Audiobookshelf (abs) provider for Music Assistant."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING

import aioaudiobookshelf as aioabs
from aioaudiobookshelf.client.session_configuration import (
    SessionConfiguration as AbsSessionConfiguration,
)
from aioaudiobookshelf.exceptions import AbsError
from aioaudiobookshelf.exceptions import (
    LoginError as AbsLoginError,
)
from aioaudiobookshelf.schema.library import LibraryMediaType as AbsLibraryMediaType
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    ProviderFeature,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError

from music_assistant.constants import PLAYBACK_REPORT_INTERVAL_SECONDS
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.audiobookshelf.mixins import (
    ArtistsMixin,
    AudiobooksMixin,
    BrowseMixin,
    PlaylistMixin,
    PodcastsMixin,
    RecommendationsMixin,
    SocketMixin,
    StreamsMixin,
)

from .constants import (
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
)
from .helpers import (
    LibrariesHelper,
    LibraryHelper,
    ProgressGuard,
    SessionHelper,
    handle_refresh_token,
)

if TYPE_CHECKING:
    from aioaudiobookshelf.schema.media_progress import MediaProgress
    from aioaudiobookshelf.schema.user import User
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ARTISTS,  # authors/ narrators
    ProviderFeature.AUTHOR_AUDIOBOOKS,
    ProviderFeature.NARRATOR_AUDIOBOOKS,
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
        ),
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            required=True,
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            required=False,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
        ),
        ConfigEntry(
            key=CONF_API_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
        ),
        ConfigEntry(
            key=CONF_OLD_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
            hidden=True,
        ),
        ConfigEntry(
            key=CONF_VERIFY_SSL,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            advanced=True,
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_HIDE_EMPTY_PODCASTS,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            advanced=True,
            default_value=False,
        ),
    )


# BaseMixin class overrides domain & instance_id only when type checking.
# Overriding @final is not allowed, and handled as misc error in mypy.
class Audiobookshelf(  # type: ignore[misc]
    ArtistsMixin,
    AudiobooksMixin,
    BrowseMixin,
    PlaylistMixin,
    PodcastsMixin,
    RecommendationsMixin,
    SocketMixin,
    StreamsMixin,
    MusicProvider,
):
    """Audiobookshelf MusicProvider."""

    _on_unload_callbacks: list[Callable[[], None]]

    async def handle_async_init(self) -> None:
        """Pass config values to client and initialize."""
        self._on_unload_callbacks: list[Callable[[], None]] = []
        self.sessions: dict[str, SessionHelper] = {}  # key is the mass_item_id
        self.create_session_lock = asyncio.Lock()
        base_url = str(self.config.get_value(CONF_URL))
        username = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD))
        token_old = self.config.get_value(CONF_OLD_TOKEN)
        token_api = self.config.get_value(CONF_API_TOKEN)
        verify_ssl = bool(self.config.get_value(CONF_VERIFY_SSL))
        session_config = AbsSessionConfiguration(
            session=self.mass.http_session,
            url=base_url,
            verify_ssl=verify_ssl,
            logger=self.logger,
            pagination_items_per_page=30,  # audible provider goes with 50 for pagination
            timeout=AIOHTTP_TIMEOUT,
        )
        # If we are configured with a non-expiring API key or not.
        self.is_token_user = False
        try:
            if token_api is not None or token_old is not None:
                _token = token_api if token_api is not None else token_old
                session_config.token = str(_token)
                (
                    self._client,
                    self._client_socket,
                ) = await aioabs.get_user_and_socket_client_by_token(session_config=session_config)
                self.is_token_user = True
            else:
                self._client, self._client_socket = await aioabs.get_user_and_socket_client(
                    session_config=session_config, username=username, password=password
                )
            await self._client_socket.init_client()
        except AbsLoginError as exc:
            raise LoginFailed(
                f"Login to abs instance at {base_url} failed.",
                translation_key="login_failed",
                translation_owner=self.translation_owner,
                translation_args=[base_url],
            ) from exc

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

        # cache username
        self.abs_username = (await self._client.get_my_user()).username

        # set socket callbacks
        self.set_socket_callbacks()

        # progress guard
        self.progress_guard = ProgressGuard()

        # safe guard reauthentication
        self.reauthenticate_lock = asyncio.Lock()
        self.reauthenticate_last = 0.0

        # safe guard playlist updates
        self.playlist_lock = asyncio.Lock()
        self.playlist_last = 0.0

        # register dynamic stream route for audiobook parts
        self._on_unload_callbacks.append(
            self.mass.streams.register_dynamic_route(
                f"/{self.instance_id}_part_stream", self._handle_session_part_request
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        try:
            await self._client.logout()
            await self._client_socket.logout()
        except AbsError as err:
            self.logger.debug("Ignoring error during logout: %s", err)
        for callback in self._on_unload_callbacks:
            callback()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return False

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """
        Get supported features.

        ABS supports multiple libraries, but they must be of the same media type. If we only
        have a single library of a media type, mapping the playlist creation is unambiguous.
        """
        features = SUPPORTED_FEATURES.copy()
        if len(self.libraries.audiobooks) > 1 or len(self.libraries.podcasts) > 1:
            return features
        features.add(ProviderFeature.PLAYLIST_TRACKS_EDIT)
        features.add(ProviderFeature.LIBRARY_PLAYLISTS_EDIT)
        if len(self.libraries.audiobooks) == 1:
            features.add(ProviderFeature.PLAYLIST_CREATE_AUDIOBOOKS)
        if len(self.libraries.podcasts) == 1:
            features.add(ProviderFeature.PLAYLIST_CREATE_PODCAST_EPISODES)
        return features

    @handle_refresh_token
    async def sync_library(self, media_type: MediaType) -> None:
        """Obtain audiobook library ids and podcast library ids."""
        libraries = await self._client.get_all_libraries()
        if len(libraries) == 0:
            self._log_no_libraries()
        for library in libraries:
            if library.media_type == AbsLibraryMediaType.BOOK and media_type == MediaType.AUDIOBOOK:
                self.libraries.audiobooks[library.id_] = LibraryHelper(name=library.name)
                await self._update_book_narrators(library.id_)
            elif (
                library.media_type == AbsLibraryMediaType.PODCAST
                and media_type == MediaType.PODCAST
            ):
                self.libraries.podcasts[library.id_] = LibraryHelper(name=library.name)
            elif media_type == MediaType.PLAYLIST:
                if library.media_type == AbsLibraryMediaType.PODCAST:
                    self.libraries.playlists_podcasts[library.id_] = set()
                if library.media_type == AbsLibraryMediaType.BOOK:
                    self.libraries.playlists_audiobooks[library.id_] = set()
            elif library.media_type == AbsLibraryMediaType.BOOK and media_type == MediaType.ARTIST:
                self.libraries.narrators[library.id_] = set()
                self.libraries.authors[library.id_] = set()

        await super().sync_library(media_type)
        await self._cache_set_helper_libraries()

        # update playlog
        user = await self._client.get_my_user()
        await self._set_playlog_from_user(user)

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
        """
        Update on user callback.

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
                if (
                    int(progress.current_time) != 0
                    and not progress.current_time >= PLAYBACK_REPORT_INTERVAL_SECONDS
                ):
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
