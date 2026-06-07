"""
Storytel provider helper utilities.

Lightweight async client helpers used by the Storytel provider for
interacting with Storytel API endpoints.
"""

from __future__ import annotations

import logging
from asyncio import Task, TaskGroup
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from aiohttp.client_exceptions import ClientError, ContentTypeError
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Audiobook,
    MediaItemChapter,
    MediaItemImage,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    UniqueList,
)

from .constants import (
    API_DEFAULT_RESOURCE_VERSION,
    API_ENCRYPTION_IV,
    API_ENCRYPTION_KEY,
    API_HEADER_CONTENT_TYPE_BOOK_DETAILS,
    API_HEADER_CONTENT_TYPE_EXPLORE,
    API_HEADER_CONTENT_TYPE_LIBRARY_DELTA,
    API_HEADER_CONTENT_TYPE_SEARCH,
    URL_BOOKMARK_GET,
    URL_BOOKMARK_SET,
    URL_CONSUMABLE_DETAILS,
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


# -------------------------------
# Lightweight API client (async)
# -------------------------------


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
        self.logger = logger or logging.getLogger("storytel_helper")

    @property
    def authorized(self) -> bool:
        """Return whether the provider is currently authorized."""
        return bool(self._auth and self._auth.jwt and self._auth.single_sign_token)

    @property
    def languages_query(self) -> str:
        """Return comma-separated ISO language codes for API queries."""
        if not self._languages:
            return "en"
        iso_values = sorted(self._languages.values())
        return ",".join(iso_values)

    @property
    def resource_version(self) -> str:
        """Return the current Storytel resource version or the default fallback."""
        if self._resource_version:
            return self._resource_version
        return API_DEFAULT_RESOURCE_VERSION

    @resource_version.setter
    def resource_version(self, value: str) -> None:
        self._resource_version = value

    def _headers_api(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "MusicAssistant-Storytel/1.0",
        }
        if self._auth:
            headers["authorization"] = f"bearer {self._auth.jwt}"
        return headers

    def _encrypt_password_hex(self, password: str) -> str:
        """Encrypt password for Storytel API."""
        # AES-128-CBC encrypt password, hex encoded (PKCS#7 padding).
        # Source for key and IV: https://github.com/MauritsWilke/storytel-api/blob/v1_archive/src/utils/encryptPassword.ts
        cipher = AES.new(self._KEY, AES.MODE_CBC, self._IV)
        enc = cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))
        return enc.hex()

    async def _raise_for_status(self, resp: ClientResponse) -> None:
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
        async with self._session.get(url) as resp:
            await self._raise_for_status(resp)
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
        try:
            async with self._session.post(
                URL_REVALIDATE, json={"token": self._auth.single_sign_token}
            ) as resp:
                await self._raise_for_status(resp)
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

    async def get_library(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Fetch the user's library, including both bookshelf items and followed items (e.g. podcasts).

        Returns a tuple of (library_items, following_items) where each is a dict keyed by consumableId.
        """
        url = URL_LIBRARY_MANAGEMENT
        headers = self._headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_LIBRARY_DELTA

        async with self._session.post(url, headers=headers, json={}) as resp:
            await self._raise_for_status(resp)
            data: dict[str, Any] = await resp.json()

        library_items = data.get("items") or {}
        following_items = data.get("followingItems") or {}

        # Pre-process library_items such that they only include items where model.resultType is "book". This is needed as the library endpoint returns both books and consuming podcasts, and we want to keep them separate for now.
        library_items = {
            k: v
            for k, v in library_items.items()
            if self._is_audiobook(v.get("model", {}))
            and self._abook_is_released((v.get("model") or {}).get("formats") or [{}])
        }

        return library_items, following_items

    async def get_consumable_details(self, consumable_id: str) -> dict[str, Any]:
        """
        Fetch consumable details from the Storytel API.

        :param consumable_id: the consumable id.
        """
        url = URL_CONSUMABLE_DETAILS.replace("{CONSUMABLE_ID}", consumable_id)
        headers = self._headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_BOOK_DETAILS
        async with self._session.get(url, headers=headers) as resp:
            await self._raise_for_status(resp)
            return cast("dict[str, Any]", await resp.json())

    async def get_bookmark(self, consumable_id: str) -> dict[str, Any] | None:
        """
        Fetch the bookmark for a consumable from the Storytel API.

        :param consumable_id: the consumable id.
        """
        url = URL_BOOKMARK_GET.replace("{CONSUMABLE_ID}", consumable_id)
        async with self._session.get(url, headers=self._headers_api()) as resp:
            await self._raise_for_status(resp)
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
            "kidsMode": bool(kids_mode),
            # MA provides seconds; Storytel expects milliseconds
            "position": int(position * 1000),
            "secondsSinceCreated": 0,
            "type": "abook",
        }
        async with self._session.post(
            URL_BOOKMARK_SET,
            headers={**self._headers_api(), "content-type": "application/json"},
            json=payload,
        ) as resp:
            await self._raise_for_status(resp)
            return cast("dict[str, Any]", await resp.json())

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
            headers = self._headers_api()
            async with self._session.get(url, headers=headers) as resp:
                await self._raise_for_status(resp)
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

    async def fetch_resource_version(self) -> None:
        """Fetch and cache the resource version for the Storytel API."""
        url = URL_LIBRARY_MANAGEMENT
        headers = self._headers_api()
        headers["Accept"] = "application/vnd.storytel.library-delta+json;v=1.4"
        request_data: dict[str, Any] = {
            "resourceVersion": self.resource_version,
            "followingItems": {},
            "items": {},
        }

        async with self._session.post(url, headers=headers, json=request_data) as resp:
            await self._raise_for_status(resp)
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
        headers = self._headers_api()
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

        async with self._session.post(url, headers=headers, json=request_data) as resp:
            await self._raise_for_status(resp)
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
        headers = self._headers_api()
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

        async with self._session.post(url, headers=headers, json=request_data) as resp:
            await self._raise_for_status(resp)
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

    async def _parse_podcast(self, podcast_data: dict[str, Any]) -> Podcast:
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
        items = podcast_data.get("items") or []
        first_episode_id = items[0].get("id") if items else ""
        publisher = ""
        if not first_episode_id:
            self.logger.debug(
                "Podcast %s has no episodes, cannot determine publisher. Skipping.",
                consumable_id,
            )
        else:
            mass_first_episode = await self.get_consumable_details(first_episode_id)
            publisher = ((mass_first_episode.get("formats") or [{}])[0].get("publisher") or {}).get(
                "name"
            ) or ""

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
            publisher=publisher,
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
        self, prov_podcast_id: str, total_episodes: int | None = None
    ) -> list[dict[str, Any]]:
        """
        Get all podcast episodes for a specific podcast.

        :param prov_podcast_id: the provider podcast id.
        :param total_episodes: the total number of episodes for the podcast.
        """

        async def fetch_page(token: str = "") -> dict[str, Any]:
            url = URL_PODCAST_DETAILS.replace("{CONSUMABLE_ID}", prov_podcast_id)
            url += (
                "?configVariant=voice-switcher-enabled"
                "&includeFormats=ebook%2Cabook%2Cpodcast"
                f"&includeLanguages={quote(self.languages_query, safe='')}"
                f"&kidsMode={quote(str(self._kids_mode), safe='')}"
                "&orderBy=default"
            )
            if token != "":
                url += f"&nextPageToken={quote(token, safe='')}"
            headers = self._headers_api()
            headers["Accept"] = API_HEADER_CONTENT_TYPE_EXPLORE
            async with self._session.get(url, headers=headers) as resp:
                await self._raise_for_status(resp)
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
        bookmark: dict[str, Any] | None,
    ) -> Audiobook | PodcastEpisode:
        release_date = (item_data.get("formats") or [{}])[0].get("releaseDate") or ""
        description = item_data.get("description") or ""
        cover_url = (item_data.get("cover") or {}).get("url")
        language = item_data.get("language") or ""
        category_name = (item_data.get("category") or {}).get("name") or ""
        languages = UniqueList([language]) if language else UniqueList()
        genres = {category_name} if category_name else set()

        if bookmark:
            media_item.resume_position_ms = int(bookmark.get("position") or 0)
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

    async def _parse_media_item(self, item_data: dict[str, Any]) -> MediaItemType:
        """Parse a media item from Storytel API to the appropriate Music Assistant media item type."""
        consumable_id = item_data.get("consumableId") or ""
        bookmark = await self.get_bookmark(consumable_id=consumable_id)
        item_type = item_data.get("type") or ""
        media_item: Audiobook | PodcastEpisode

        if item_type == "detailedPodcastEpisode":
            media_item = await self._parse_podcast_episode_item(item_data)
        elif item_type == "detailedBook":
            media_item = await self._parse_audiobook_item(item_data)
        else:
            self.logger.warning("Unsupported media item type for parsing: %s", item_type)
            raise UnsupportedFeaturedException(f"Unsupported media item type: {item_type}")

        return await self._apply_media_item_metadata(media_item, item_data, bookmark)

    async def get_podcast_details(self, consumable_id: str) -> dict[str, Any]:
        """
        Get details of a podcast from the Storytel API.

        :param consumable_id: the consumable id.
        """
        url = URL_PODCAST_DETAILS.replace("{CONSUMABLE_ID}", consumable_id)
        headers = self._headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_EXPLORE
        async with self._session.get(url, headers=headers) as resp:
            await self._raise_for_status(resp)
            return cast("dict[str, Any]", await resp.json())

    async def _fetch_search_page(
        self,
        query: str,
        search_for: str,
        page_token: str,
        filter_func: Callable[[list[dict[str, Any]]], list[str]],
        fetch_func: Callable[[str], Any],
    ) -> tuple[list[Any], int, int]:
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
            f"&kidsMode={quote(str(self._kids_mode), safe='')}"
            f"&query={quote(query, safe='')}"
            "&v2=true"
        )
        if page_token != "":
            url += f"&page={quote(page_token, safe='')}"
        headers = self._headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_SEARCH
        async with self._session.get(url, headers=headers) as resp:
            await self._raise_for_status(resp)
            data: dict[str, Any] = await resp.json()

        results = data.get("items") or []
        item_ids = filter_func(results)
        self.logger.debug(
            "Filtered away %d results, %d candidates on page %s for query '%s'",
            len(results) - len(item_ids),
            len(item_ids),
            page_token or "0",
            query,
        )

        page_items: list[Any] = []
        if item_ids:
            task_results: list[Task[Any]] = []
            async with TaskGroup() as tg:
                for item_id in item_ids:
                    task_results.append(tg.create_task(fetch_func(item_id)))
            for task in task_results:
                page_items.append(task.result())

        total_results = int(data.get("totalCount", 0) or 0)
        next_page_offset = int(data.get("nextPageToken", 0) or 0)
        return page_items, total_results, next_page_offset

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

        def filter_podcasts(results: list[dict[str, Any]]) -> list[str]:
            """Filter results to extract podcast IDs."""
            return [
                result_id
                for result in results
                if result.get("resultType") == "podcast" and (result_id := result.get("id"))
            ]

        async def fetch_page(search_page_token: str) -> tuple[list[Podcast], int, int]:
            return await self._fetch_search_page(
                query,
                "podcast_shows",
                search_page_token,
                filter_podcasts,
                self.provider_instance.get_podcast,
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

        while current_results_count < limit and next_page_offset < total_results:
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

        def filter_audiobooks(results: list[dict[str, Any]]) -> list[str]:
            """Filter results to extract audiobook IDs."""
            return [
                result_id
                for result in results
                if result.get("resultType") == "book"
                and any(
                    book_format.get("type") == "abook" for book_format in result.get("formats", [])
                )
                and (result_id := result.get("id"))
            ]

        async def fetch_page(search_page_token: str) -> tuple[list[Audiobook], int, int]:
            return await self._fetch_search_page(
                query,
                "books",
                search_page_token,
                filter_audiobooks,
                self.provider_instance.get_audiobook,
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

        while current_results_count < limit and next_page_offset < total_results:
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

            next_page_offset = int(batch_tokens[-1]) + page_size

        return audiobooks

    async def get_recommendations(self) -> RecommendationFolder | None:
        """Get audiobook recommendations for the user."""
        folder = RecommendationFolder(
            item_id=f"{self.provider_id}_recommendations",
            provider=self.provider_id,
            icon="mdi-star-circle-outline",
            name="Storytel Recommendations",
        )

        chip_url = URL_FRONTPAGE

        headers = self._headers_api()
        headers["Accept"] = API_HEADER_CONTENT_TYPE_EXPLORE
        chip_url += (
            "?includeFormats=abook%2Cpodcast"
            f"&includeLanguages={quote(self.languages_query, safe='')}"
            f"&kidsMode={quote(str(self._kids_mode), safe='')}"
            "&onboarding=false&version=2"
        )

        async with self._session.get(chip_url, headers=headers) as resp:
            await self._raise_for_status(resp)
            chip_response: dict[str, Any] = await resp.json()
        chips = chip_response.get("chips") or []

        frontpage_chip = next(
            (chip for chip in chips if chip.get("id", "").startswith("frontpage")),
            None,
        )
        if frontpage_chip:
            frontpage_url = frontpage_chip.get("url", "")
        else:
            return None

        frontpage_url += (
            "?categoryIds="
            "&configVariant=voice-switcher-enabled"
            "&includeFormats=abook%2Cpodcast"
            f"&includeLanguages={quote(self.languages_query, safe='')}"
            f"&kidsMode={quote(str(self._kids_mode), safe='')}"
            "&onboarding=false&version=2"
        )

        async with self._session.get(frontpage_url, headers=headers) as resp:
            await self._raise_for_status(resp)
            frontpage_response: dict[str, Any] = await resp.json()
        content_blocks = frontpage_response.get("contentBlocks") or []

        personal_recommendations_block = next(
            (
                block
                for block in content_blocks
                if block.get("id", "").startswith("personal-recommendations_")
            ),
            None,
        )
        if personal_recommendations_block:
            personal_recommendations_url = personal_recommendations_block.get("itemsUrl", "")
        else:
            return None

        async with self._session.get(personal_recommendations_url, headers=headers) as resp:
            await self._raise_for_status(resp)
            recommendations_response: dict[str, Any] = await resp.json()
        items = recommendations_response.get("items") or []

        task_results: list[Task[Audiobook]] = []

        # TODO: Currently doesn't handle podcast recommendations
        async with TaskGroup() as tg:
            for item in items:
                task_results.append(
                    tg.create_task(self.provider_instance.get_audiobook(item.get("id")))
                )

        for task in task_results:
            folder.items.append(task.result())

        # for item in items:
        #     # TODO: Currently doesn't handle podcast recommendations
        #     if item.get("resultType") == "book":
        #         folder.items.append(await self.provider_instance.get_audiobook(item.get("id")))
        #     else:
        #         self.logger.debug("Skipping non-audiobook recommendation item: %s", item.get("id"))
        #         self.logger.debug("Item type: %s", item.get("resultType"))

        if len(folder.items) == 0:
            return None

        return folder


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
