"""
Storytel provider helper utilities.

Lightweight async client helpers used by the Storytel provider for
interacting with Storytel API endpoints.
"""

from __future__ import annotations

import logging
from asyncio import Lock, Task, TaskGroup
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast
from urllib.parse import quote

from aiohttp.client_exceptions import ClientError, ContentTypeError
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    UnplayableMediaError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    MediaItemChapter,
    MediaItemImage,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from yarl import URL

from .constants import (
    API_DEFAULT_RESOURCE_VERSION,
    API_ENCRYPTION_IV,
    API_ENCRYPTION_KEY,
    API_HEADER_CONTENT_TYPE_BOOK_DETAILS,
    API_HEADER_CONTENT_TYPE_EXPLORE,
    API_HEADER_CONTENT_TYPE_LIBRARY_DELTA,
    API_HEADER_CONTENT_TYPE_SEARCH,
    API_HEADER_STORYTEL_MEDIA_ACCEPT,
    API_HEADER_STORYTEL_MEDIA_FORMATS,
    URL_BOOKMARK_GET,
    URL_BOOKMARK_SET,
    URL_CONSUMABLE_DETAILS,
    URL_CONSUMABLE_DOWNLOAD_NO_RANGE,
    URL_FRONTPAGE,
    URL_LIBRARY_MANAGEMENT,
    URL_LOGIN,
    URL_PLAYBACK_BOOK_DETAILS,
    URL_PODCAST_DETAILS,
    URL_REVALIDATE,
    URL_SEARCH,
)

if TYPE_CHECKING:
    from aiohttp import ClientResponse, ClientSession

    from music_assistant.providers.storytel import Storytel


# Generic type for async fetch functions used by _fetch_search_page
T = TypeVar("T")


@dataclass
class StorytelAuth:
    """
    Authentication tokens container for Storytel API.

    Holds the JWT and the single-sign token used for Storytel requests.
    """

    jwt: str
    single_sign_token: str


class StorytelHelper:
    """Async client for the Storytel API, for endpoints needed by the provider."""

    _KEY = API_ENCRYPTION_KEY
    _IV = API_ENCRYPTION_IV

    def __init__(
        self,
        session: ClientSession,
        provider_instance: Storytel,
        provider_id: str,
        provider_domain: str,
        kids_mode: bool = False,
        languages: dict[str, str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize the StorytelHelper.

        :param session: aiohttp ClientSession used for requests.
        :param provider_instance: parent provider instance.
        :param provider_id: provider instance id.
        :param provider_domain: provider domain string.
        :param kids_mode: whether to request kids-mode content.
        :param languages: optional language mapping for queries.
        :param logger: optional logger to use.
        """
        self._session = session
        self._auth: StorytelAuth | None = None
        self.provider_instance = provider_instance
        self.provider_id = provider_id
        self.provider_domain = provider_domain
        self._kids_mode = kids_mode
        self._languages = languages or {}
        self._resource_version: str | None = None
        self._revalidate_lock = Lock()
        self.logger = logger or logging.getLogger("storytel_helper")

    @property
    def authorized(self) -> bool:
        """Return whether the provider is currently authorized."""
        return bool(self._auth and self._auth.jwt and self._auth.single_sign_token)

    @property
    def session(self) -> ClientSession:
        """Return the aiohttp session used for Storytel requests."""
        return self._session

    @property
    def languages_query(self) -> str:
        """Return comma-separated ISO language codes for API queries."""
        if not self._languages:
            return "en"
        iso_values = sorted(self._languages.values())
        return ",".join(iso_values)

    @property
    def kids_mode_query(self) -> str:
        """Return the Storytel kids-mode query value."""
        return "true" if self._kids_mode else "false"

    @property
    def resource_version(self) -> str:
        """Return the current Storytel resource version or the default fallback."""
        if self._resource_version:
            return self._resource_version
        return API_DEFAULT_RESOURCE_VERSION

    @resource_version.setter
    def resource_version(self, value: str) -> None:
        self._resource_version = value

    def headers_api(self) -> dict[str, str]:
        """Build the standard API headers used for Storytel requests."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "MusicAssistant-Storytel/1.0",
        }
        if self._auth:
            headers["authorization"] = f"bearer {self._auth.jwt}"
        return headers

    async def raise_for_status(self, resp: ClientResponse) -> None:
        """Raise a provider exception for non-success Storytel responses."""
        if 200 <= resp.status < 300:
            return
        try:
            resp_json = await resp.json()
            resp_message = resp_json.get("message") or ""
        except ContentTypeError:
            resp_message = await resp.text() or "<no response>"
        if resp.status in (401, 403):
            raise LoginFailed(f"Unauthorized ({resp.status}): {resp_message}")
        self.logger.warning(
            "Failed Storytel API request with status %s: %s", resp.status, resp_message
        )
        raise ProviderUnavailableError(f"Storytel HTTP {resp.status}: {resp_message}")

    async def login(self, username: str, password: str) -> StorytelAuth:
        """
        Authenticate with the Storytel API.

        :param username: the username.
        :param password: the password.
        """
        enc = self._encrypt_password_hex(password)
        url = URL_LOGIN.replace("{UID}", quote(username, safe="")).replace(
            "{PASSWORD}", quote(enc, safe="")
        )
        async with self.session.get(url) as resp:
            await self.raise_for_status(resp)
            data: dict[str, Any] = await resp.json()
        acc = data.get("accountInfo") or {}
        jwt = acc.get("jwt")
        sst = acc.get("singleSignToken")
        if not jwt or not sst:
            # API returns a message on login errors
            msg = data.get("message") or "Invalid credentials"
            raise LoginFailed(msg)
        self._auth = StorytelAuth(jwt=jwt, single_sign_token=sst)
        return self._auth

    async def revalidate_account(self) -> StorytelAuth:
        """Revalidate the Storytel account using the single sign token."""
        if not self._auth or not self._auth.single_sign_token:
            raise LoginFailed("No single sign token")
        current_token = self._auth.single_sign_token
        async with self._revalidate_lock:
            if not self._auth or self._auth.single_sign_token != current_token:
                return self._auth
            try:
                async with self.session.post(URL_REVALIDATE, json={"token": current_token}) as resp:
                    await self.raise_for_status(resp)
                    data: dict[str, Any] = await resp.json()
            except LoginFailed:
                raise ProviderUnavailableError(
                    "Storytel account revalidation failed, token may be expired. Please login again."
                )
        acc = data.get("accountInfo") or {}
        jwt = acc.get("jwt")
        sst = acc.get("singleSignToken")
        if not jwt or not sst:
            msg = data.get("message") or "Revalidation failed"
            raise LoginFailed(msg)
        self._auth = StorytelAuth(jwt=jwt, single_sign_token=sst)
        return self._auth

    async def get_library(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Fetch the user's library, including both bookshelf items and followed items (e.g. podcasts).

        Returns a tuple of (library_items, following_items) where each is a dict keyed by consumableId.
        """
        url = URL_LIBRARY_MANAGEMENT
        headers = self.headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_LIBRARY_DELTA

        async with self.session.post(url, headers=headers, json={}) as resp:
            await self.raise_for_status(resp)
            data: dict[str, Any] = await resp.json()

        library_items = data.get("items") or {}
        following_items = data.get("followingItems") or {}

        # Pre-process library_items such that they only include items where model.resultType is "book". This is needed as the library endpoint returns both books and consuming podcasts, and we want to keep them separate for now.
        library_items = {
            k: v
            for k, v in library_items.items()
            if (model := v.get("model") or {})
            and self._is_audiobook(model)
            and self._abook_is_released(model.get("formats") or [{}])
        }

        return library_items, following_items

    async def get_consumable_details(self, consumable_id: str) -> dict[str, Any]:
        """
        Fetch consumable details from the Storytel API.

        :param consumable_id: the consumable id.
        """
        url = URL_CONSUMABLE_DETAILS.replace("{CONSUMABLE_ID}", consumable_id)
        headers = self.headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_BOOK_DETAILS
        async with self.session.get(url, headers=headers) as resp:
            await self.raise_for_status(resp)
            return cast("dict[str, Any]", await resp.json())

    async def get_bookmark(self, consumable_id: str) -> dict[str, Any] | None:
        """
        Fetch the bookmark for a consumable from the Storytel API.

        :param consumable_id: the consumable id.
        """
        url = URL_BOOKMARK_GET.replace("{CONSUMABLE_ID}", consumable_id)
        async with self.session.get(url, headers=self.headers_api()) as resp:
            await self.raise_for_status(resp)
            data: dict[str, Any] = await resp.json()
        bookmarks = data.get("bookmarks") or []
        # Only abook bookmark
        for bm in bookmarks:
            if bm.get("type") == "abook":
                return cast("dict[str, Any]", bm)
        return None

    async def set_bookmark(
        self, consumable_id: str, position: int, kids_mode: bool = False
    ) -> dict[str, Any]:
        """
        Set the bookmark for a consumable.

        :param consumable_id: the consumable id.
        :param position: the position in seconds.
        :param kids_mode: True if kids mode is enabled.
        """
        payload = {
            "consumableId": consumable_id,
            "kidsMode": "true" if kids_mode else "false",
            # MA provides seconds; Storytel expects milliseconds
            "position": int(position * 1000),
            "secondsSinceCreated": 0,
            "type": "abook",
        }
        async with self.session.post(
            URL_BOOKMARK_SET,
            headers={**self.headers_api(), "content-type": "application/json"},
            json=payload,
        ) as resp:
            await self.raise_for_status(resp)
            return cast("dict[str, Any]", await resp.json())

    async def fetch_resource_version(self) -> None:
        """Fetch and cache the resource version for the Storytel API."""
        url = URL_LIBRARY_MANAGEMENT
        headers = self.headers_api()
        headers["Accept"] = "application/vnd.storytel.library-delta+json;v=1.4"
        request_data: dict[str, Any] = {
            "resourceVersion": self.resource_version,
            "followingItems": {},
            "items": {},
        }

        async with self.session.post(url, headers=headers, json=request_data) as resp:
            await self.raise_for_status(resp)
            response_data: dict[str, Any] = await resp.json()

        resource_version = response_data.get("resourceVersion", "")

        if resource_version != "":
            self.logger.debug("Fetched Storytel resource version: %s", resource_version)
            self.resource_version = resource_version
        else:
            self.logger.debug("No resource version found in Storytel response.")
            self.resource_version = API_DEFAULT_RESOURCE_VERSION

    async def add_to_bookshelf(self, consumable_id: str, item: MediaItemType) -> bool:
        """
        Add an audiobook or podcast to the user's bookshelf.

        :param consumable_id: the consumable id.
        :param item: the media item to add.
        """
        url = URL_LIBRARY_MANAGEMENT
        headers = self.headers_api()
        headers["Accept"] = "application/vnd.storytel.library-delta+json;v=1.4"
        request_data: dict[str, Any] = {
            "resourceVersion": self.resource_version,
            "followingItems": {},
            "items": {},
        }
        if item.media_type == MediaType.PODCAST:
            cast("dict[str, Any]", request_data["followingItems"])["podcast-" + consumable_id] = {
                "id": consumable_id,
                "action": "SET",
                "resultType": "podcast",
                "state": "DO_NOT_NOTIFY",
                "millisecondsSinceEvent": 10,
            }
        elif item.media_type == MediaType.AUDIOBOOK:
            cast("dict[str, Any]", request_data["items"])[consumable_id] = {
                "action": "SET",
                "state": "WILL_CONSUME",
                "millisecondsSinceEvent": 10,
            }

        async with self.session.post(url, headers=headers, json=request_data) as resp:
            await self.raise_for_status(resp)
            response_data: dict[str, Any] = await resp.json()

        following_items = response_data.get("followingItems") or {}
        library_items = response_data.get("items") or {}
        success = False
        if item.media_type == MediaType.PODCAST:
            podcast_item = following_items.get("podcast-" + consumable_id) or {}
            success = (
                podcast_item.get("action") == "SET"
                and (podcast_item.get("model") or {}).get("state") == "DO_NOT_NOTIFY"
            )
        elif item.media_type == MediaType.AUDIOBOOK:
            audiobook_item = library_items.get(consumable_id) or {}
            success = (
                audiobook_item.get("action") == "SET"
                and (audiobook_item.get("model") or {}).get("state") == "WILL_CONSUME"
            )

        if success:
            self.logger.debug("Added %s %s to bookshelf.", item.media_type.value, consumable_id)
            self.resource_version = response_data.get("resourceVersion", self.resource_version)
        else:
            self.logger.debug(
                "Failed to add %s %s to bookshelf.", item.media_type.value, consumable_id
            )

        return success

    async def remove_from_bookshelf(self, consumable_id: str, media_type: MediaType) -> bool:
        """
        Remove an audiobook or podcast from the user's bookshelf.

        :param consumable_id: the consumable id.
        :param media_type: the media type.
        """
        url = URL_LIBRARY_MANAGEMENT
        headers = self.headers_api()
        headers["Accept"] = "application/vnd.storytel.library-delta+json;v=1.4"
        request_data: dict[str, Any] = {
            "resourceVersion": self.resource_version,
            "followingItems": {},
            "items": {},
        }
        if media_type == MediaType.PODCAST:
            cast("dict[str, Any]", request_data["followingItems"])["podcast-" + consumable_id] = {
                "id": consumable_id,
                "millisecondsSinceEvent": 10,
                "resultType": "podcast",
                "action": "DELETE",
            }
        elif media_type == MediaType.AUDIOBOOK:
            cast("dict[str, Any]", request_data["items"])[consumable_id] = {
                "action": "DELETE",
                "millisecondsSinceEvent": 10,
            }

        async with self.session.post(url, headers=headers, json=request_data) as resp:
            await self.raise_for_status(resp)
            response_data: dict[str, Any] = await resp.json()

        following_items = response_data.get("followingItems") or {}
        library_items = response_data.get("items") or {}
        success = False
        if media_type == MediaType.PODCAST:
            podcast_item = following_items.get("podcast-" + consumable_id) or {}
            success = podcast_item.get("action") == "DELETE"
        elif media_type == MediaType.AUDIOBOOK:
            audiobook_item = library_items.get(consumable_id) or {}
            success = audiobook_item.get("action") == "DELETE"

        if success:
            self.logger.debug("Removed %s %s from bookshelf.", media_type.value, consumable_id)
            self.resource_version = response_data.get("resourceVersion", self.resource_version)
        else:
            self.logger.debug(
                "Failed to remove %s %s from bookshelf.", media_type.value, consumable_id
            )

        return success

    async def parse_podcast(self, podcast_data: dict[str, Any]) -> Podcast:
        """Parse Storytel podcast data to Music Assistant Podcast."""
        list_metadata = podcast_data.get("listMetadata") or {}
        media_type = list_metadata.get("type") or ""
        if media_type != "podcast":
            self.logger.debug(
                "Skipping non-podcast item of type %s: %s", media_type, podcast_data.get("id")
            )
            raise InvalidDataError(f"Item {podcast_data.get('id')} is not a podcast")

        consumable_id = podcast_data.get("id") or ""
        title = podcast_data.get("title") or "Unknown"
        episode_count = podcast_data.get("totalCount") or 0
        hosts = parse_podcast_hosts(list_metadata)

        podcast = Podcast(
            item_id=consumable_id,
            provider=self.provider_id,
            name=title,
            provider_mappings={
                ProviderMapping(
                    item_id=consumable_id,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_id,
                )
            },
            favorite=False,
            total_episodes=episode_count,
        )

        cover_url = (list_metadata.get("imageUrl") or {}).get("url") or ""
        if cover_url:
            podcast.metadata.images = UniqueList(
                [MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=self.provider_id)]
            )
        podcast.metadata.description = list_metadata.get("description") or ""
        podcast.metadata.languages = UniqueList([list_metadata.get("language") or ""])
        podcast.metadata.genres = {list_metadata.get("genre", "")}
        podcast.metadata.performers = set(hosts)
        latest_episode_date_text = (list_metadata.get("followingInfo") or {}).get(
            "newestItemReleaseDate"
        ) or ""
        if latest_episode_date_text:
            podcast.metadata.release_date = datetime.fromisoformat(
                latest_episode_date_text
            ).astimezone(UTC)

        return podcast

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
        total_episodes: int | None = None,
        include_languages: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get all podcast episodes for a specific podcast.

        :param prov_podcast_id: the provider podcast id.
        :param total_episodes: the total number of episodes for the podcast.
        :param include_languages: optional comma-separated language filter for the episode list.
        """
        languages_query = include_languages or self.languages_query

        async def fetch_page(token: str = "") -> dict[str, Any]:
            url = URL_PODCAST_DETAILS.replace("{CONSUMABLE_ID}", prov_podcast_id)
            url += (
                "?configVariant=voice-switcher-enabled"
                "&includeFormats=ebook%2Cabook%2Cpodcast"
                f"&includeLanguages={quote(languages_query, safe='')}"
                f"&kidsMode={self.kids_mode_query}"
                "&orderBy=default"
            )
            if token != "":
                url += f"&nextPageToken={quote(token, safe='')}"
            headers = self.headers_api()
            headers["Accept"] = API_HEADER_CONTENT_TYPE_EXPLORE
            async with self.session.get(url, headers=headers) as resp:
                await self.raise_for_status(resp)
                return cast("dict[str, Any]", await resp.json())

        async def fetch_items(token: str) -> list[dict[str, Any]]:
            page_data = await fetch_page(token)
            return page_data.get("items") or []

        async def fetch_pages(page_tokens: list[str]) -> list[dict[str, Any]]:
            page_results: list[Task[list[dict[str, Any]]]] = []

            async with TaskGroup() as tg:
                for token in page_tokens:
                    page_results.append(tg.create_task(fetch_items(token)))

            results: list[dict[str, Any]] = []
            for task in page_results:
                results.extend(task.result())
            return results

        if not total_episodes or total_episodes <= 0:
            return []

        page_size = 10
        page_tokens = [str(page_offset) for page_offset in range(0, total_episodes, page_size)]
        if not page_tokens:
            return []
        return await fetch_pages(page_tokens)

    async def parse_media_item(self, item_data: dict[str, Any]) -> MediaItemType:
        """Parse a media item from Storytel API to the appropriate Music Assistant media item type."""
        item_type = item_data.get("type") or ""
        media_item: Audiobook | PodcastEpisode

        if item_type == "detailedPodcastEpisode":
            media_item = await self._parse_podcast_episode_item(item_data)
        elif item_type == "detailedBook":
            media_item = await self._parse_audiobook_item(item_data)
        else:
            self.logger.warning("Unsupported media item type for parsing: %s", item_type)
            raise UnsupportedFeaturedException(f"Unsupported media item type: {item_type}")

        return await self._apply_media_item_metadata(media_item, item_data)

    async def get_podcast_details(self, consumable_id: str) -> dict[str, Any]:
        """
        Get details of a podcast from the Storytel API.

        :param consumable_id: the consumable id.
        """
        url = URL_PODCAST_DETAILS.replace("{CONSUMABLE_ID}", consumable_id)
        headers = self.headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_EXPLORE
        async with self.session.get(url, headers=headers) as resp:
            await self.raise_for_status(resp)
            return cast("dict[str, Any]", await resp.json())

    async def search_podcasts(
        self, query: str, limit: int = 10, page_token: str = "", results_count: int = 0
    ) -> list[Podcast]:
        """
        Search for podcasts matching the query.

        :param query: the search query.
        :param limit: the maximum number of results.
        :param page_token: the page token for pagination.
        :param results_count: the current count of results.
        """
        max_pages = 10

        def filter_podcasts(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Filter results to extract podcast result payloads."""
            return [result for result in results if result.get("resultType") == "podcast"]

        async def fetch_page(search_page_token: str) -> tuple[list[Podcast], int, int]:
            return await self._fetch_search_page(
                query,
                "podcast_shows",
                search_page_token,
                filter_podcasts,
                self._parse_search_podcast_item,
            )

        podcasts: list[Podcast] = []
        current_page_token = page_token
        current_results_count = max(results_count, 0)

        page_podcasts, total_results, next_page_offset = await fetch_page(current_page_token)
        podcasts.extend(page_podcasts)
        current_results_count += len(page_podcasts)

        if next_page_offset <= 0 or next_page_offset >= total_results:
            return podcasts

        page_size = max(
            next_page_offset - (int(current_page_token) if current_page_token else 0), 1
        )
        page_batch_size = 5
        pages_fetched = 1

        while (
            current_results_count < limit
            and next_page_offset < total_results
            and pages_fetched < max_pages
        ):
            batch_tokens = [
                str(page_offset)
                for page_offset in range(
                    next_page_offset,
                    total_results,
                    page_size,
                )
            ][:page_batch_size]
            if not batch_tokens:
                break

            batch_results: list[list[Podcast]] = []
            async with TaskGroup() as tg:
                batch_tasks = [tg.create_task(fetch_page(token)) for token in batch_tokens]
            for task in batch_tasks:
                page_podcasts, _, _ = task.result()
                batch_results.append(page_podcasts)

            for page_podcasts in batch_results:
                podcasts.extend(page_podcasts)
                current_results_count += len(page_podcasts)

            pages_fetched += len(batch_tokens)
            next_page_offset = int(batch_tokens[-1]) + page_size

        return podcasts

    async def search_audiobooks(
        self, query: str, limit: int = 10, page_token: str = "", results_count: int = 0
    ) -> list[Audiobook]:
        """
        Search for audiobooks matching the query.

        :param query: the search query.
        :param limit: the maximum number of results.
        :param page_token: the page token for pagination.
        :param results_count: the current count of results.
        """
        max_pages = 10

        def filter_audiobooks(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Filter results to extract audiobook result payloads."""
            return [
                result
                for result in results
                if result.get("resultType") == "book"
                and any(
                    isinstance(book_format, dict) and book_format.get("type") == "abook"
                    for book_format in result.get("formats", [])
                )
            ]

        async def fetch_page(search_page_token: str) -> tuple[list[Audiobook], int, int]:
            return await self._fetch_search_page(
                query,
                "books",
                search_page_token,
                filter_audiobooks,
                self._parse_search_audiobook_item,
            )

        audiobooks: list[Audiobook] = []
        current_page_token = page_token
        current_results_count = max(results_count, 0)

        page_audiobooks, total_results, next_page_offset = await fetch_page(current_page_token)
        audiobooks.extend(page_audiobooks)
        current_results_count += len(page_audiobooks)

        if next_page_offset <= 0 or next_page_offset >= total_results:
            return audiobooks

        page_size = max(
            next_page_offset - (int(current_page_token) if current_page_token else 0), 1
        )
        page_batch_size = 5
        pages_fetched = 1

        while (
            current_results_count < limit
            and next_page_offset < total_results
            and pages_fetched < max_pages
        ):
            batch_tokens = [
                str(page_offset)
                for page_offset in range(
                    next_page_offset,
                    total_results,
                    page_size,
                )
            ][:page_batch_size]
            if not batch_tokens:
                break

            batch_results: list[list[Audiobook]] = []
            async with TaskGroup() as tg:
                batch_tasks = [tg.create_task(fetch_page(token)) for token in batch_tokens]
            for task in batch_tasks:
                page_audiobooks, _, _ = task.result()
                batch_results.append(page_audiobooks)

            for page_audiobooks in batch_results:
                audiobooks.extend(page_audiobooks)
                current_results_count += len(page_audiobooks)

            pages_fetched += len(batch_tokens)
            next_page_offset = int(batch_tokens[-1]) + page_size

        return audiobooks

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get audiobook and podcast recommendations for the user."""
        chip_url = URL_FRONTPAGE
        chip_url += (
            "?includeFormats=abook%2Cpodcast"
            f"&includeLanguages={quote(self.languages_query, safe='')}"
            f"&kidsMode={self.kids_mode_query}"
            "&onboarding=false&version=2"
        )

        chip_response = await self._fetch_explore_page(chip_url)
        chips = chip_response.get("chips") or []

        folders: list[RecommendationFolder] = []

        frontpage_chip = next(
            (chip for chip in chips if str(chip.get("id") or "").startswith("frontpage")),
            None,
        )
        if frontpage_chip and (frontpage_url := str(frontpage_chip.get("url") or "").strip()):
            frontpage_url += (
                "?categoryIds="
                "&configVariant=voice-switcher-enabled"
                "&includeFormats=abook%2Cpodcast"
                f"&includeLanguages={quote(self.languages_query, safe='')}"
                f"&kidsMode={self.kids_mode_query}"
                "&onboarding=false&version=2"
            )
            if folder := await self._recommendation_folder_from_block_url(
                frontpage_url,
                block_id_prefixes=("personal-recommendations_",),
                folder_item_id=f"{self.provider_id}_recommendations",
                folder_name="Recommended for You",
                translation_key="storytel.recommendations.recommended_for_you.name",
            ):
                folders.append(folder)

        podcast_chip = next(
            (
                chip
                for chip in chips
                if "podcast" in str(chip.get("id") or "").lower()
                and not str(chip.get("id") or "").startswith("frontpage")
            ),
            None,
        )
        if podcast_chip and (podcast_url := str(podcast_chip.get("url") or "").strip()):
            if folder := await self._recommendation_folder_from_block_url(
                podcast_url,
                block_id_prefixes=("algorithmic-podcasts-for-you",),
                folder_item_id=f"{self.provider_id}_podcast_recommendations",
                folder_name="Recommended Podcasts",
                translation_key="storytel.recommendations.recommended_podcasts.name",
            ):
                folders.append(folder)

        self.logger.debug(
            "Storytel recommendation folders resolved: %d (chips=%d)", len(folders), len(chips)
        )

        return folders

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve Storytel stream information for an audiobook or podcast episode."""
        try:
            stream_info_url = URL_CONSUMABLE_DOWNLOAD_NO_RANGE.replace("{CONSUMABLE_ID}", item_id)
            headers = self.headers_api()
            headers[API_HEADER_STORYTEL_MEDIA_ACCEPT] = API_HEADER_STORYTEL_MEDIA_FORMATS

            async with self.session.get(stream_info_url, headers=headers) as stream_info_response:
                await self.raise_for_status(stream_info_response)
                response = await stream_info_response.json()
                stream_url = (response.get("result") or {}).get("signedUrl") or ""
                if stream_url == "":
                    self.logger.error("No signed URL returned for %s", item_id)
                    raise MediaNotFoundError("No signed URL returned")
                headers.pop("authorization", None)
                headers["range"] = "bytes=0-1"
                current_stream_url = URL(stream_url, encoded=True)
                resolved_stream_url = stream_url
                stream_headers: dict[str, str] = {}
                for _redirect_count in range(5):
                    async with self.session.get(
                        current_stream_url, headers=headers, allow_redirects=False
                    ) as stream_response:
                        if 300 <= stream_response.status < 400:
                            location = stream_response.headers.get("Location") or ""
                            if location == "":
                                await self.raise_for_status(stream_response)
                            location_url = URL(location, encoded=True)
                            current_stream_url = (
                                location_url
                                if location.startswith("http")
                                else current_stream_url.join(location_url)
                            )
                            resolved_stream_url = str(current_stream_url)
                            continue

                        await self.raise_for_status(stream_response)
                        stream_headers = parse_raw_headers(stream_response.raw_headers)
                        content_type, content_full_size = parse_partial_content_probe(
                            status_code=stream_response.status,
                            stream_headers=stream_headers,
                        )
                        resolved_stream_url = str(current_stream_url)
                        break
                else:
                    raise UnplayableMediaError("Too many redirects while resolving Storytel stream")
        except Exception as err:
            if isinstance(err, LoginFailed):
                raise
            self.logger.error("Failed to fetch stream details for %s: %s", item_id, err)
            raise UnplayableMediaError(f"Failed to fetch stream details: {err}") from err

        mass_content_type: ContentType | None = parse_content_type(content_type)
        content_duration_seconds: int = int(
            float(stream_headers.get("x-amz-meta-x-durationseconds", 0))
        )
        if content_duration_seconds == 0:
            if media_type == MediaType.PODCAST_EPISODE:
                item_details: (
                    Audiobook | PodcastEpisode
                ) = await self.provider_instance.get_podcast_episode(item_id)
            else:
                item_details = await self.provider_instance.get_audiobook(item_id)
            content_duration_seconds = item_details.duration or 0

        return StreamDetails(
            provider=self.provider_id,
            size=content_full_size,
            item_id=item_id,
            audio_format=AudioFormat(content_type=mass_content_type or ContentType.UNKNOWN),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=resolved_stream_url,
            can_seek=True,
            allow_seek=True,
            duration=content_duration_seconds,
        )

    async def _fetch_explore_page(self, page_url: str) -> dict[str, Any]:
        """Fetch a Storytel explore page or list-resource payload."""
        headers = self.headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_EXPLORE
        async with self.session.get(page_url, headers=headers) as resp:
            await self.raise_for_status(resp)
            return cast("dict[str, Any]", await resp.json())

    async def _recommendation_folder_from_block_url(
        self,
        block_url: str,
        *,
        block_id_prefixes: tuple[str, ...],
        folder_item_id: str,
        folder_name: str | None = None,
        translation_key: str | None = None,
    ) -> RecommendationFolder | None:
        """Build a recommendation folder from a Storytel explore page URL."""
        page_response = await self._fetch_explore_page(block_url)
        content_blocks = page_response.get("contentBlocks") or []

        block = next(
            (
                content_block
                for content_block in content_blocks
                if any(
                    str(content_block.get("id") or "").startswith(prefix)
                    for prefix in block_id_prefixes
                )
            ),
            None,
        )
        if not block:
            self.logger.debug(
                "No Storytel recommendation block matched prefixes %s",
                ",".join(block_id_prefixes),
            )
            return None

        items = block.get("items") or []
        if not items:
            items_url = str(block.get("itemsUrl") or "").strip()
            if items_url:
                self.logger.debug(
                    "Storytel recommendation block %s has no inline items; fetching itemsUrl",
                    block.get("id") or "<missing>",
                )
                items_response = await self._fetch_explore_page(items_url)
                items = items_response.get("items") or []
            else:
                self.logger.debug(
                    "Storytel recommendation block %s did not include items or itemsUrl",
                    block.get("id") or "<missing>",
                )
        parsed_items = await self._parse_recommendation_items(items)
        if not parsed_items:
            self.logger.debug(
                "Storytel recommendation block %s produced no parsable items",
                block.get("id") or "<missing>",
            )
            return None

        folder = RecommendationFolder(
            item_id=folder_item_id,
            provider=self.provider_id,
            icon="mdi-star-circle-outline",
            name=(folder_name or str(block.get("title") or "Recommendations")).strip(),
            translation_key=translation_key,
        )
        folder.items.extend(parsed_items)
        return folder

    async def _parse_recommendation_items(
        self, items: list[dict[str, Any]]
    ) -> list[Audiobook | Podcast]:
        """Parse Storytel recommendation payload items into media items."""

        async def _parse_item(item: dict[str, Any]) -> Audiobook | Podcast | None:
            item_type = str(item.get("resultType") or "").strip().lower()
            if item_type not in {"book", "podcast"}:
                return None
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                return None
            try:
                if item_type == "book":
                    return await self._parse_search_audiobook_item(item)
                return await self._parse_search_podcast_item(item)
            except (InvalidDataError, MediaNotFoundError, ProviderUnavailableError) as err:
                self.logger.debug("Skipping Storytel recommendation item %s: %s", item_id, err)
                return None

        task_results: list[Task[Audiobook | Podcast | None]] = []

        async with TaskGroup() as tg:
            for item in items:
                task_results.append(tg.create_task(_parse_item(item)))

        parsed_items: list[Audiobook | Podcast] = []
        for task in task_results:
            parsed_item = task.result()
            if parsed_item is not None:
                parsed_items.append(parsed_item)
        return parsed_items

    def _encrypt_password_hex(self, password: str) -> str:
        """Encrypt password for Storytel API."""
        # AES-128-CBC encrypt password, hex encoded (PKCS#7 padding).
        # Source for key and IV: https://github.com/MauritsWilke/storytel-api/blob/v1_archive/src/utils/encryptPassword.ts
        cipher = AES.new(self._KEY, AES.MODE_CBC, self._IV)
        enc: bytes = cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))
        return enc.hex()

    def _abook_is_released(self, formats_data: list[dict[str, Any]]) -> bool:
        for format_data in formats_data:
            if format_data.get("type") != "abook":
                continue
            is_released: bool = format_data.get("isReleased") or False
            return is_released
        return False

    def _is_audiobook(self, model_data: dict[str, Any]) -> bool:
        result_type: str = model_data.get("resultType") or ""
        return result_type == "book"

    def _parse_duration(self, duration_data: dict[str, int]) -> int:
        """Parse duration data to seconds."""
        if not duration_data:
            return 0
        hours = int(duration_data.get("hours") or 0)
        minutes = int(duration_data.get("minutes") or 0)
        seconds = int(duration_data.get("seconds") or 0)
        return hours * 3600 + minutes * 60 + seconds

    async def _fetch_chapters(self, consumable_id: str) -> list[dict[str, Any]]:
        """Fetch chapters for a given consumable_id."""
        chapters: list[dict[str, Any]] = []

        url = URL_PLAYBACK_BOOK_DETAILS.replace("{CONSUMABLE_ID}", consumable_id)
        try:
            headers = self.headers_api()
            async with self.session.get(url, headers=headers) as resp:
                await self.raise_for_status(resp)
                data: dict[str, Any] = await resp.json()
            formats = data.get("formats") or []
            audiobook_format = next((f for f in formats if f.get("type") == "abook"), None)
            chapters = audiobook_format.get("chapters") or [] if audiobook_format else []
        except (ClientError, KeyError, TypeError, ValueError) as err:
            self.logger.debug("Failed to fetch chapters for %s: %s", consumable_id, err)

        return list(chapters)

    async def _parse_chapters(self, chapters_data: list[dict[str, Any]]) -> list[MediaItemChapter]:
        """Parse raw chapter data into MediaChapter objects."""
        chapters: list[MediaItemChapter] = []
        chapters_data = await self._compute_chapter_start(chapters_data)
        for chap in chapters_data:
            chapter_number = int(chap.get("number") or 0)
            title = chap.get("title") or f"Chapter {chapter_number}"
            start = chap.get("startPosition") or 0
            end = chap.get("endPosition") or 0
            chapter = MediaItemChapter(
                position=chapter_number,
                name=title,
                start=start,
                end=end,
            )
            chapters.append(chapter)
        return chapters

    async def _compute_chapter_start(
        self, chapters_data: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Augment the chapters data with computed start positions."""
        for i, chap in enumerate(chapters_data):
            if i == 0:
                chap["startPosition"] = 0
                chap["endPosition"] = int(chap.get("durationInSeconds", 0))
            else:
                prev_chap = chapters_data[i - 1]
                chap["startPosition"] = prev_chap.get("endPosition", 0)
                chap["endPosition"] = chap["startPosition"] + int(chap.get("durationInSeconds", 0))
        return chapters_data

    async def _parse_podcast_episode_item(self, item_data: dict[str, Any]) -> PodcastEpisode:
        consumable_id = item_data.get("consumableId") or ""
        title = item_data.get("title") or "Unknown"
        duration_seconds = self._parse_duration(item_data.get("duration") or {})
        podcast_info = item_data.get("seriesInfo") or {}
        mass_podcast = await self.provider_instance.get_podcast(podcast_info.get("id") or "")
        hosts = parse_podcast_hosts(item_data)
        episode_number = item_data.get("seriesInfo", {}).get("orderInSeries") or 0
        media_item = PodcastEpisode(
            item_id=consumable_id,
            provider=self.provider_id,
            podcast=mass_podcast,
            name=title,
            duration=duration_seconds,
            position=0,
            provider_mappings={
                ProviderMapping(
                    item_id=consumable_id,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_id,
                )
            },
        )
        if hosts:
            media_item.metadata.performers = set(hosts)
        if episode_number:
            media_item.position = episode_number
        return media_item

    async def _parse_audiobook_item(self, item_data: dict[str, Any]) -> Audiobook:
        consumable_id = item_data.get("consumableId") or ""
        title = item_data.get("title") or "Unknown"
        duration_seconds = self._parse_duration(item_data.get("duration") or {})
        authors = [a.get("name") for a in item_data.get("authors", []) if a.get("name")]
        narrators = [n.get("name") for n in item_data.get("narrators", []) if n.get("name")]
        publisher = ((item_data.get("formats") or [{}])[0].get("publisher") or {}).get("name") or ""
        media_item = Audiobook(
            item_id=consumable_id,
            provider=self.provider_id,
            name=title,
            duration=duration_seconds,
            provider_mappings={
                ProviderMapping(
                    item_id=consumable_id,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_id,
                )
            },
            publisher=publisher,
            favorite=False,
        )
        chapters = await self._fetch_chapters(consumable_id=consumable_id)
        chapters_list = await self._parse_chapters(chapters)
        if authors:
            media_item.authors.set(authors)
        if narrators:
            media_item.narrators.set(narrators)
        if chapters_list:
            media_item.metadata.chapters = chapters_list
        return media_item

    async def _apply_media_item_metadata(
        self,
        media_item: Audiobook | PodcastEpisode,
        item_data: dict[str, Any],
    ) -> Audiobook | PodcastEpisode:
        release_date = (item_data.get("formats") or [{}])[0].get("releaseDate") or ""
        description = item_data.get("description") or ""
        cover_url = (item_data.get("cover") or {}).get("url")
        language = item_data.get("language") or ""
        category_name = (item_data.get("category") or {}).get("name") or ""
        languages = UniqueList([language]) if language else UniqueList()
        genres = {category_name} if category_name else set()

        if release_date:
            media_item.metadata.release_date = datetime.fromisoformat(release_date).astimezone(UTC)
        if description:
            media_item.metadata.description = description
        if cover_url:
            media_item.metadata.images = UniqueList(
                [MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=self.provider_id)]
            )
        if languages:
            media_item.metadata.languages = languages
        if genres:
            media_item.metadata.genres = genres

        return media_item

    async def _parse_search_audiobook_item(self, item_data: dict[str, Any]) -> Audiobook:
        """Build a lightweight audiobook item from a search result payload."""
        consumable_id = item_data.get("id") or ""
        title = item_data.get("title") or item_data.get("name") or "Unknown"
        authors_data = item_data.get("authors") or []
        authors = [
            str(author.get("name"))
            for author in authors_data
            if isinstance(author, dict) and author.get("name")
        ]
        media_item = Audiobook(
            item_id=consumable_id,
            provider=self.provider_id,
            name=title,
            provider_mappings={
                ProviderMapping(
                    item_id=consumable_id,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_id,
                )
            },
            favorite=False,
        )
        if authors:
            media_item.authors.set(authors)

        audiobook_formats_data = [
            format_data
            for format_data in (item_data.get("formats") or [])
            if isinstance(format_data, dict) and format_data.get("type") == "abook"
        ]
        for format_data in audiobook_formats_data:
            cover_url = (
                (format_data.get("cover") or {}).get("url")
                if isinstance(format_data.get("cover"), dict)
                else None
            )
        if cover_url:
            media_item.metadata.images = UniqueList(
                [MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=self.provider_id)]
            )
        return media_item

    async def _parse_search_podcast_item(self, item_data: dict[str, Any]) -> Podcast:
        """Build a lightweight podcast item from a search result payload."""
        podcast_id = item_data.get("id") or ""
        title = item_data.get("title") or item_data.get("name") or "Unknown"
        total_episodes = int(item_data.get("numberOfEpisodes") or 0)
        podcast = Podcast(
            item_id=podcast_id,
            provider=self.provider_id,
            name=title,
            total_episodes=total_episodes,
            provider_mappings={
                ProviderMapping(
                    item_id=podcast_id,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_id,
                )
            },
        )
        cover_url = (
            (item_data.get("cover") or {}).get("url")
            if isinstance(item_data.get("cover"), dict)
            else None
        )
        if cover_url:
            podcast.metadata.images = UniqueList(
                [MediaItemImage(type=ImageType.THUMB, path=cover_url, provider=self.provider_id)]
            )
        return podcast

    async def _fetch_search_page(
        self,
        query: str,
        search_for: str,
        page_token: str,
        filter_func: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        fetch_func: Callable[[dict[str, Any]], Coroutine[Any, Any, T]],
    ) -> tuple[list[T], int, int]:
        """
        Fetch a single search page and prepare items for the given search type.

        :param query: The search query string.
        :param search_for: The search type (e.g., "podcast_shows" or "books").
        :param page_token: The page token for this request.
        :param filter_func: Callable to filter item IDs from results.
        :param fetch_func: Async callable to fetch full item details by ID.
        """
        url = URL_SEARCH
        url += (
            "?configVariant=baseline"
            f"&searchFor={quote(search_for, safe='')}"
            "&includeFormats=abook"
            f"&includeLanguages={quote(self.languages_query, safe='')}"
            f"&kidsMode={self.kids_mode_query}"
            f"&query={quote(query, safe='')}"
            "&v2=true"
        )
        if page_token != "":
            url += f"&page={quote(page_token, safe='')}"
        headers = self.headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_SEARCH
        async with self.session.get(url, headers=headers) as resp:
            await self.raise_for_status(resp)
            data: dict[str, Any] = await resp.json()

        if data is None:
            raise MediaNotFoundError(f"No search results found for query '{query}'")
        results = data.get("items") or []
        item_results = filter_func(results)
        self.logger.debug(
            "Filtered away %d results, %d candidates on page %s for query '%s'",
            len(results) - len(item_results),
            len(item_results),
            page_token or "0",
            query,
        )

        page_items: list[T] = []
        if item_results:
            task_results: list[Task[T]] = []
            async with TaskGroup() as tg:
                for item_data in item_results:
                    task_results.append(tg.create_task(fetch_func(item_data)))
            for task in task_results:
                page_items.append(task.result())

        total_results = int(data.get("totalCount", 0) or 0)
        next_page_offset = int(data.get("nextPageToken", 0) or 0)
        return page_items, total_results, next_page_offset


def parse_raw_headers(raw_headers: tuple[tuple[bytes, bytes], ...]) -> dict[str, str]:
    """
    Parse raw bytes headers into a dictionary of string key-pairs.

    :param raw_headers: the raw headers to parse.
    """
    headers: dict[str, str] = {}
    for key_bytes, value_bytes in raw_headers:
        key = key_bytes.decode("utf-8").lower()
        value = value_bytes.decode("utf-8")
        headers[key] = value
    return headers


def parse_content_type(content_type: str) -> ContentType | None:
    """
    Parse the content type string to a MA ContentType.

    :param content_type: the content type string.
    """
    # Split on ';' to separate media type from codec params (e.g. "audio/mp4;codecs=mp4a.40.2")
    parts = content_type.split(";", 1) if content_type else []
    content_type = parts[0].strip() if parts else ""
    codec = parts[1].strip() if len(parts) > 1 else ""
    if codec == "codecs=ec-3":
        return ContentType.EAC3
    if codec == "codecs=mp4a.40.2":
        return ContentType.AAC
    if content_type == "audio/mp4":
        return ContentType.MP4
    if content_type == "audio/mpeg":
        return ContentType.MP3
    return None


def parse_partial_content_probe(
    status_code: int, stream_headers: dict[str, str]
) -> tuple[str, int]:
    """
    Validate a range probe response and extract stream metadata.

    :param status_code: the HTTP status code from the probe response.
    :param stream_headers: the parsed response headers.
    """
    if status_code != 206:
        raise UnplayableMediaError(
            "Storytel stream endpoint did not honor range request (expected HTTP 206)"
        )

    content_type = stream_headers.get("content-type", "")
    content_content_range = stream_headers.get("content-range", "")
    if content_content_range == "":
        raise UnplayableMediaError("Storytel stream response missing Content-Range header")

    try:
        content_full_size = int(content_content_range.rsplit("/", maxsplit=1)[-1])
    except ValueError as err:
        raise UnplayableMediaError(
            "Storytel stream response has invalid Content-Range header"
        ) from err
    return content_type, content_full_size


def parse_podcast_hosts(podcast_metadata: dict[str, Any]) -> list[str]:
    """
    Parse podcast hosts from metadata.

    :param podcast_metadata: the podcast metadata dictionary.
    """
    hosts_list = []
    hosts_metadata = podcast_metadata.get("hosts") or []
    for host in hosts_metadata:
        if host.get("name"):
            hosts_list.append(host.get("name"))

    return hosts_list
