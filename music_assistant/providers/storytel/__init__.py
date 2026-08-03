"""
Storytel provider integration.

Provides the MusicProvider implementation and glue to the StorytelHelper
lightweight client used to interact with Storytel APIs.
"""

from __future__ import annotations

import functools
from asyncio import Semaphore, Task, TaskGroup
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

from aiohttp import ClientError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant_models.media_items import (
    Audiobook,
    BrowseFolder,
    ItemMapping,
    Podcast,
    PodcastEpisode,
    RecommendationFolder,
    SearchResults,
    UniqueList,
)

from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.recommendation_payload import RecommendationPayloadMixin

from .constants import (
    ALL_LANGUAGES,
    CACHE_CATEGORY_AUDIOBOOK,
    CACHE_CATEGORY_PODCAST,
    CACHE_CATEGORY_PODCAST_EPISODE,
    CACHE_CATEGORY_PODCAST_EPISODES,
    CONF_KIDS_MODE,
    CONF_LANGUAGES,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_LANGUAGES,
)
from .storytel_helper import StorytelHelper

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

F = TypeVar("F", bound=Callable[..., Any])


# -------------------------------
# Setup and configuration entries
# -------------------------------


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """
    Set up the Storytel provider.

    :param mass: the MusicAssistant instance.
    :param manifest: the provider manifest.
    :param config: the provider config.
    """
    return Storytel(mass=mass, manifest=manifest, config=config)


# -------------------------------
# Provider implementation
# -------------------------------


class Storytel(RecommendationPayloadMixin, MusicProvider):
    """Storytel provider."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the config entries for the Storytel provider.

        Credentials are collected in the setup flow and persisted in setup_data.
        The options page only exposes persisted discovery settings.
        """
        setup_languages = self.get_setup_value(CONF_LANGUAGES)
        if isinstance(setup_languages, list) and setup_languages:
            default_languages = [name for name in setup_languages if isinstance(name, str)]
        else:
            default_languages = list(DEFAULT_LANGUAGES)
        return (
            ConfigEntry(
                key=CONF_LANGUAGES,
                type=ConfigEntryType.STRING,
                multi_value=True,
                required=False,
                default_value=default_languages,
                options=[ConfigValueOption(name, name) for name in sorted(ALL_LANGUAGES.keys())],
            ),
            ConfigEntry(
                key=CONF_KIDS_MODE,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
            ),
        )

    @staticmethod
    def handle_login_failed(
        method: F,
    ) -> F:
        """
        Decorate a method to retry once after a login failure.

        :param method: the method to decorate.
        """

        @functools.wraps(method)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            self = cast("Storytel", args[0])
            try:
                return await method(*args, **kwargs)
            except LoginFailed:
                await self.api.revalidate_account()
                return await method(*args, **kwargs)
            except ExceptionGroup as err:
                if err.subgroup(LoginFailed):
                    await self.api.revalidate_account()
                    return await method(*args, **kwargs)
                raise

        return cast("F", wrapper)

    @staticmethod
    def handle_login_failed_generator(
        method: F,
    ) -> F:
        """
        Decorate an async generator method to retry once after login failure.

        :param method: the async generator method to decorate.
        """

        @functools.wraps(method)
        async def wrapper(*args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            self = cast("Storytel", args[0])
            try:
                async for item in method(*args, **kwargs):
                    yield item
            except LoginFailed:
                await self.api.revalidate_account()
                async for item in method(*args, **kwargs):
                    yield item
            except ExceptionGroup as err:
                if err.subgroup(LoginFailed):
                    await self.api.revalidate_account()
                    async for item in method(*args, **kwargs):
                        yield item
                    return
                raise

        return cast("F", wrapper)

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """
        Initialize the Storytel provider instance.

        Accepts the same args/kwargs as the base MusicProvider constructor.
        """
        super().__init__(mass=mass, manifest=manifest, config=config)
        self._api: StorytelHelper | None = None
        # Whether to use Kids mode for bookmarks
        self._kids_mode: bool = bool(self.get_setup_value(CONF_KIDS_MODE))
        # Selected languages for discovery features (recommendations, search)
        self._languages: dict[str, str] = DEFAULT_LANGUAGES.copy()

    @property
    def api(self) -> StorytelHelper:
        """Return the initialized Storytel helper API instance."""
        if self._api is None:
            raise SetupFailedError("Storytel provider API not initialized")
        return self._api

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the supported features by this provider."""
        return {
            ProviderFeature.LIBRARY_AUDIOBOOKS,
            ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT,
            ProviderFeature.LIBRARY_PODCASTS,
            ProviderFeature.LIBRARY_PODCASTS_EDIT,
            ProviderFeature.RECOMMENDATIONS,
            ProviderFeature.SEARCH,
        }

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        username = str(self.get_setup_value(CONF_USERNAME) or "")
        password = str(self.get_setup_value(CONF_PASSWORD) or "")
        if not username or not password:
            raise LoginFailed("Username/password required")

        languages_list = self.get_setup_value(CONF_LANGUAGES)
        if isinstance(languages_list, list) and languages_list:
            self._languages = {
                name: ALL_LANGUAGES[name]
                for name in languages_list
                if isinstance(name, str) and name in ALL_LANGUAGES
            }
        if not self._languages:
            self._languages = DEFAULT_LANGUAGES.copy()

        self._kids_mode = bool(self.get_setup_value(CONF_KIDS_MODE))

        self._api = StorytelHelper(
            session=self.mass.http_session,
            provider_instance=self,
            provider_id=self.instance_id,
            provider_domain=self.domain,
            kids_mode=self._kids_mode,
            languages=self._languages,
        )
        try:
            await self._api.login(username, password)
        except LoginFailed:
            raise SetupFailedError("Invalid Storytel username or password")
        except (ClientError, ConnectionError) as err:
            raise SetupFailedError(f"Storytel login failed: {err}") from err
        try:
            await self._api.fetch_resource_version()
        except (ClientError, ConnectionError, ProviderUnavailableError) as err:
            self.logger.warning(
                "Storytel resource version refresh failed during setup; using default: %s",
                err,
            )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    # -------------------
    # Library: Audiobooks
    # -------------------

    @handle_login_failed_generator
    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Yield audiobooks from the user's Storytel bookshelf."""
        books, _ = await self.api.get_library()
        failed_provider_error: ProviderUnavailableError | None = None
        yielded_any = False
        for consumable_id in books:
            try:
                audiobook = await self.get_audiobook(consumable_id)
                yielded_any = True
                yield audiobook
            except (MediaNotFoundError, ProviderUnavailableError, InvalidDataError) as err:
                if isinstance(err, ProviderUnavailableError):
                    failed_provider_error = failed_provider_error or err
                self.logger.debug("Skipping Storytel book %s: %s", consumable_id, err)

        if not yielded_any and failed_provider_error is not None:
            raise failed_provider_error

    @handle_login_failed
    async def get_audiobook(self, prov_audiobook_id: str, use_cache: bool = True) -> Audiobook:
        """
        Fetch a single audiobook by our provider id.

        :param prov_audiobook_id: the provider specific ID of the audiobook.
        :param use_cache: boolean to indicate if a cache lookup is allowed.
        """
        if use_cache:
            cached_book = await self.mass.cache.get(
                key=prov_audiobook_id,
                provider=self.instance_id,
                category=CACHE_CATEGORY_AUDIOBOOK,
                default=None,
            )
            if cached_book is not None:
                return Audiobook.from_dict(cached_book)
        item_data = await self.api.get_consumable_details(prov_audiobook_id)

        if item_data is None:
            raise MediaNotFoundError(f"Storytel book not found: {prov_audiobook_id}")

        media_item = await self.api.parse_media_item(item_data)

        await self.mass.cache.set(
            key=prov_audiobook_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_AUDIOBOOK,
            data=media_item.to_dict(),
        )
        return cast("Audiobook", media_item)

    # -------------------
    # Streaming
    # -------------------

    @handle_login_failed
    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Fetch stream details for a given item id and media type.

        :param item_id: the provider item id of the media.
        :param media_type: the media type to stream (Audiobook or PodcastEpisode).
        """
        if media_type not in {MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE}:
            self.logger.error("Unsupported media type for streaming: %s", media_type)
            raise InvalidDataError("Only audiobooks and podcasts are supported")
        return await self.api.get_stream_details(item_id, media_type)

    @handle_login_failed
    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """
        Get the resume position for a media item.

        :param item_id: the provider item id.
        :param media_type: the media type of the item.
        """
        if media_type not in {MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE}:
            self.logger.error("Unsupported media type for resume position: %s", media_type)
            raise InvalidDataError("Only audiobooks and podcasts are supported for resume position")
        bm = await self.api.get_bookmark(item_id)
        if not bm:
            self.logger.debug("No bookmark found for %s", item_id)
            return False, 0, None
        # Storytel returns the position in milliseconds.
        pos = int(bm.get("position") or 0)
        updated_ts = bm.get("updatedTime")
        bookmark_updated_dt: datetime | None = None
        if updated_ts:
            try:
                bookmark_updated_dt = datetime.fromisoformat(str(updated_ts)).astimezone(UTC)
            except ValueError:
                bookmark_updated_dt = None

        return False, pos, bookmark_updated_dt

    @handle_login_failed
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
        Handle played event of a media item.

        :param media_type: the media type of the item.
        :param prov_item_id: the provider item id.
        :param fully_played: True if the item was fully played.
        :param position: the resume position in the item.
        :param media_item: the media item object.
        :param is_playing: True if the item is currently playing.
        """
        consumable_id = prov_item_id
        if media_type not in {MediaType.AUDIOBOOK, MediaType.PODCAST_EPISODE}:
            self.logger.error("Unsupported media type for bookmark update: %s", media_type)
            return
        # Set bookmark position to the media duration if the item was fully played and the duration is known. Otherwise, use the provided position.
        bookmark_position = position
        if fully_played:
            media_duration = getattr(media_item, "duration", None)
            if isinstance(media_duration, int) and media_duration > 0:
                bookmark_position = media_duration
        try:
            await self.api.set_bookmark(
                consumable_id,
                bookmark_position,
                kids_mode=self._kids_mode,
            )
        except (
            ClientError,
            ConnectionError,
            TimeoutError,
            LoginFailed,
            ProviderUnavailableError,
        ) as err:
            if isinstance(err, (LoginFailed, ProviderUnavailableError)):
                raise
            self.logger.debug("Failed to update Storytel bookmark: %s", err)

    @handle_login_failed
    async def get_podcast(self, prov_podcast_id: str, use_cache: bool = True) -> Podcast:
        """
        Fetch a podcast by our provider id.

        :param prov_podcast_id: the provider specific ID of the podcast.
        :param use_cache: boolean to indicate if a cache lookup is allowed.
        """
        if use_cache:
            cached_podcast = await self.mass.cache.get(
                key=prov_podcast_id,
                provider=self.instance_id,
                category=CACHE_CATEGORY_PODCAST,
                default=None,
            )
            if cached_podcast is not None:
                return Podcast.from_dict(cached_podcast)
        item_data = await self.api.get_podcast_details(prov_podcast_id)
        if item_data is None:
            raise MediaNotFoundError(f"Storytel podcast not found: {prov_podcast_id}")

        podcast = await self.api.parse_podcast(item_data)

        await self.mass.cache.set(
            key=prov_podcast_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PODCAST,
            data=podcast.to_dict(),
        )
        return podcast

    @handle_login_failed
    async def get_podcast_episode(
        self, prov_episode_id: str, use_cache: bool = True
    ) -> PodcastEpisode:
        """
        Fetch a podcast episode by our provider id.

        :param prov_episode_id: the provider specific ID of the podcast episode.
        :param use_cache: boolean to indicate if a cache lookup is allowed.
        """
        if use_cache:
            cached_episode = await self.mass.cache.get(
                key=prov_episode_id,
                provider=self.instance_id,
                category=CACHE_CATEGORY_PODCAST_EPISODE,
                default=None,
            )
            if cached_episode is not None:
                return PodcastEpisode.from_dict(cached_episode)
        item_data = await self.api.get_consumable_details(prov_episode_id)

        if item_data is None:
            raise MediaNotFoundError(f"Storytel podcast episode not found: {prov_episode_id}")

        podcast_episode = await self.api.parse_media_item(item_data)

        await self.mass.cache.set(
            key=prov_episode_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PODCAST_EPISODE,
            data=podcast_episode.to_dict(),
        )
        return cast("PodcastEpisode", podcast_episode)

    @handle_login_failed_generator
    async def get_podcast_episodes(
        self, prov_podcast_id: str, use_cache: bool = True
    ) -> AsyncGenerator[PodcastEpisode]:
        """
        Fetch all episodes for a given podcast by our provider id.

        :param prov_podcast_id: the provider specific ID of the podcast.
        :param use_cache: boolean to indicate if a cache lookup is allowed.
        """
        failed_provider_error: ProviderUnavailableError | None = None
        episode_semaphore = Semaphore(10)

        async def _fetch_episode(episode: dict[str, Any]) -> PodcastEpisode | None:
            nonlocal failed_provider_error
            consumable_id = str(episode.get("id") or "").strip()
            if not consumable_id:
                return None
            async with episode_semaphore:
                try:
                    return await self.get_podcast_episode(consumable_id, use_cache=True)
                except (MediaNotFoundError, ProviderUnavailableError, InvalidDataError) as err:
                    if isinstance(err, ProviderUnavailableError):
                        failed_provider_error = failed_provider_error or err
                    self.logger.debug(
                        "Skipping Storytel podcast episode %s: %s", consumable_id, err
                    )
                    return None

        if use_cache:
            cached_episodes = await self.mass.cache.get(
                key=prov_podcast_id,
                provider=self.instance_id,
                category=CACHE_CATEGORY_PODCAST_EPISODES,
                default=None,
            )
            if cached_episodes is not None:
                async with TaskGroup() as tg:
                    cached_tasks: list[Task[PodcastEpisode | None]] = []
                    for episode in cached_episodes:
                        cached_tasks.append(tg.create_task(_fetch_episode(episode)))
                yielded_any = False
                for task in cached_tasks:
                    if episode := task.result():
                        yielded_any = True
                        yield episode
                if not yielded_any and failed_provider_error is not None:
                    raise failed_provider_error
                return
        podcast = await self.get_podcast(prov_podcast_id, use_cache=use_cache)
        episode_languages = {lang for lang in self.api.languages_query.split(",") if lang}
        podcast_languages = getattr(getattr(podcast, "metadata", None), "languages", None)
        if podcast_languages:
            episode_languages.update(
                str(language).strip() for language in podcast_languages if str(language).strip()
            )
        podcast_episodes = await self.api.get_podcast_episodes(
            prov_podcast_id,
            total_episodes=podcast.total_episodes or 0,
            include_languages=",".join(sorted(episode_languages)) or None,
        )

        if podcast_episodes is not None:
            if use_cache:
                await self.mass.cache.set(
                    key=prov_podcast_id,
                    provider=self.instance_id,
                    category=CACHE_CATEGORY_PODCAST_EPISODES,
                    data=podcast_episodes,
                )
            async with TaskGroup() as tg:
                live_tasks: list[Task[PodcastEpisode | None]] = []
                for episode in podcast_episodes:
                    live_tasks.append(tg.create_task(_fetch_episode(episode)))
            yielded_any = False
            for task in live_tasks:
                if episode := task.result():
                    yielded_any = True
                    yield episode
            if not yielded_any and failed_provider_error is not None:
                raise failed_provider_error

    @handle_login_failed_generator
    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Yield podcasts from the user's library."""
        _, podcasts = await self.api.get_library()
        failed_provider_error: ProviderUnavailableError | None = None
        yielded_any = False
        for podcast_data in podcasts.values():
            model = podcast_data.get("model") or {}
            podcast_id = model.get("id") or ""
            result_type = str(model.get("resultType") or podcast_data.get("resultType") or "")
            if result_type and result_type.lower() != "podcast":
                self.logger.debug(
                    "Skipping Storytel followed item %s with result type %s",
                    podcast_id,
                    result_type,
                )
                continue
            try:
                podcast = await self.get_podcast(podcast_id)
                yielded_any = True
                yield podcast
            except (MediaNotFoundError, ProviderUnavailableError, InvalidDataError) as err:
                if isinstance(err, ProviderUnavailableError):
                    failed_provider_error = failed_provider_error or err
                self.logger.debug("Skipping Storytel podcast %s: %s", podcast_id, err)

        if not yielded_any and failed_provider_error is not None:
            raise failed_provider_error

    @handle_login_failed
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """
        Perform a search on Storytel.

        :param search_query: the search query.
        :param media_types: the media types to search for.
        :param limit: the maximum number of results per media type.
        """
        result = SearchResults()
        task_audiobooks: Task[list[Audiobook]] | None = None
        task_podcasts: Task[list[Podcast]] | None = None

        async with TaskGroup() as tg:
            if MediaType.AUDIOBOOK in media_types:
                task_audiobooks = tg.create_task(
                    self.api.search_audiobooks(search_query, limit=limit)
                )
            if MediaType.PODCAST in media_types:
                task_podcasts = tg.create_task(self.api.search_podcasts(search_query, limit=limit))

        if MediaType.AUDIOBOOK in media_types and task_audiobooks:
            result.audiobooks = task_audiobooks.result()

        if MediaType.PODCAST in media_types and task_podcasts:
            result.podcasts = task_podcasts.result()

        return result

    @handle_login_failed
    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return personalized recommendation folders without items."""
        return await self._recommendation_rows_from_payload()

    @handle_login_failed
    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return the items for a single recommendation folder.

        :param item_id: the item id of the recommendation row.
        """
        return await self._recommendation_items_from_payload(item_id)

    @handle_login_failed
    async def library_add(self, item: MediaItemType) -> bool:
        """
        Add an item to the provider library.

        :param item: the media item to add.
        :return: True if successful, False otherwise.
        """
        if item.media_type not in (MediaType.AUDIOBOOK, MediaType.PODCAST):
            self.logger.error("Unsupported media type for library add: %s", item.media_type)
            raise InvalidDataError(
                "Only audiobooks and podcasts are supported for library management"
            )
        return await self.api.add_to_bookshelf(item.item_id, item)

    @handle_login_failed
    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """
        Remove an item from the provider library.

        :param prov_item_id: the provider item id to remove.
        :param media_type: the media type of the item.
        :return: True if successful, False otherwise.
        """
        if media_type not in (MediaType.AUDIOBOOK, MediaType.PODCAST):
            self.logger.error("Unsupported media type for library remove: %s", media_type)
            raise InvalidDataError(
                "Only audiobooks and podcasts are supported for library management"
            )
        return await self.api.remove_from_bookshelf(prov_item_id, media_type)

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch the full Storytel recommendation payload with items."""
        recommendations = await self.api.get_recommendations()
        return recommendations or []
