"""Helper for parsing and using audible api."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from os import PathLike
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import audible
import audible.register
from audible import AsyncClient

if TYPE_CHECKING:
    from aiohttp import ClientSession
from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.mass import MusicAssistant

CACHE_DOMAIN = "audible"
CACHE_CATEGORY_API = 0
CACHE_CATEGORY_AUDIOBOOK = 1
CACHE_CATEGORY_CHAPTERS = 2
CACHE_CATEGORY_PODCAST = 3
CACHE_CATEGORY_PODCAST_EPISODES = 4

# Content delivery types
AUDIOBOOK_CONTENT_TYPES = ("SinglePartBook", "MultiPartBook")
PODCAST_CONTENT_TYPES = ("PodcastParent",)

_AUTH_CACHE: dict[str, audible.Authenticator] = {}


async def refresh_access_token_compat(
    refresh_token: str, domain: str, http_session: ClientSession, with_username: bool = False
) -> dict[str, Any]:
    """Refresh tokens with compatibility for new Audible API format.

    The Audible API changed from returning 'access_token' to 'actor_access_token'.
    This function handles both formats for backward compatibility.

    :param refresh_token: The refresh token obtained after device registration.
    :param domain: The top level domain (e.g., com, de).
    :param http_session: The HTTP client session to use for requests.
    :param with_username: If True, use audible domain instead of amazon.
    :return: Dict with access_token and expires timestamp.
    """
    logger = logging.getLogger("audible_helper")

    body = {
        "app_name": "Audible",
        "app_version": "3.56.2",
        "source_token": refresh_token,
        "requested_token_type": "access_token",
        "source_token_type": "refresh_token",
    }

    target_domain = "audible" if with_username else "amazon"
    url = f"https://api.{target_domain}.{domain}/auth/token"

    async with http_session.post(url, data=body) as resp:
        resp.raise_for_status()
        resp_dict = await resp.json()

    expires_in_sec = int(resp_dict.get("expires_in", 3600))
    expires = (datetime.now(UTC) + timedelta(seconds=expires_in_sec)).timestamp()

    # Handle new format (actor_access_token) or fall back to legacy (access_token)
    access_token = resp_dict.get("actor_access_token") or resp_dict.get("access_token")

    if not access_token:
        logger.error("Token refresh response missing both actor_access_token and access_token")
        raise LoginFailed("Token refresh failed: no access token in response")

    logger.debug(
        "Token refreshed successfully using %s format",
        "new (actor)" if "actor_access_token" in resp_dict else "legacy",
    )

    return {"access_token": access_token, "expires": expires}


async def cached_authenticator_from_file(path: str) -> audible.Authenticator:
    """Get an authenticator from file with caching and signing auth validation.

    :param path: Path to the authenticator JSON file.
    :return: The cached or loaded Authenticator instance.
    """
    logger = logging.getLogger("audible_helper")
    if path in _AUTH_CACHE:
        return _AUTH_CACHE[path]

    logger.debug("Loading authenticator from file %s and caching it", path)
    auth = await asyncio.to_thread(audible.Authenticator.from_file, path)

    # Verify signing auth is available (not affected by API changes)
    if auth.adp_token and auth.device_private_key:
        logger.debug("Signing auth available - using stable RSA-signed requests")
    else:
        logger.warning(
            "Signing auth not available - only bearer auth will work. "
            "Consider re-authenticating for more stable auth."
        )

    _AUTH_CACHE[path] = auth
    return auth


class AudibleHelper:
    """Helper for parsing and using audible api."""

    def __init__(
        self,
        mass: MusicAssistant,
        client: AsyncClient,
        provider_domain: str,
        provider_instance: str,
        logger: logging.Logger | None = None,
    ):
        """Initialize the Audible Helper."""
        self.mass = mass
        self.client = client
        self.provider_domain = provider_domain
        self.provider_instance = provider_instance
        self.logger = logger or logging.getLogger("audible_helper")
        self._acr_cache: dict[tuple[str, MediaType], str] = {}

    async def _fetch_library_items(
        self,
        response_groups: str,
        content_types: tuple[str, ...],
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Fetch items from the library with pagination."""
        page = 1
        page_size = 50
        total_processed = 0
        max_iterations = 100
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            self.logger.debug(
                "Audible: Fetching library page %s (processed so far: %s)",
                page,
                total_processed,
            )

            library = await self._call_api(
                "library",
                use_cache=False,
                response_groups=response_groups,
                page=page,
                num_results=page_size,
            )

            items = library.get("items", [])

            if not items:
                break

            items_processed_this_page = 0
            for item in items:
                # Filter by content type if specified
                if content_types and item.get("content_delivery_type") not in content_types:
                    continue

                yield item
                items_processed_this_page += 1
                total_processed += 1

            self.logger.debug(
                "Audible: Processed %s items on page %s", items_processed_this_page, page
            )

            page += 1
            if len(items) < page_size:
                break

        if iteration >= max_iterations:
            self.logger.warning(
                "Audible: Reached maximum iteration limit (%s) with %s items processed",
                max_iterations,
                total_processed,
            )

    async def _process_audiobook_item(self, audiobook_data: dict[str, Any]) -> Audiobook | None:
        """Process a single audiobook item from the library."""
        # Ensure asin is a valid string
        asin = str(audiobook_data.get("asin", ""))
        cached_book = None
        if asin:
            cached_book = await self.mass.cache.get(
                key=asin,
                provider=self.provider_instance,
                category=CACHE_CATEGORY_AUDIOBOOK,
                default=None,
            )

        try:
            if cached_book is not None:
                return self._parse_audiobook(cached_book)
            return self._parse_audiobook(audiobook_data)
        except MediaNotFoundError as exc:
            self.logger.warning(f"Skipping invalid audiobook: {exc}")
            return None
        except Exception as exc:
            self.logger.warning(
                f"Error processing audiobook {audiobook_data.get('asin', 'unknown')}: {exc}"
            )
            return None

    async def get_library(self) -> AsyncGenerator[Audiobook, None]:
        """Fetch the user's library with pagination."""
        response_groups = [
            "contributors",
            "media",
            "product_attrs",
            "product_desc",
            "product_details",
            "product_extended_attrs",
        ]

        async for item in self._fetch_library_items(
            ",".join(response_groups), AUDIOBOOK_CONTENT_TYPES
        ):
            if album := await self._process_audiobook_item(item):
                yield album

    async def get_audiobook(self, asin: str, use_cache: bool = True) -> Audiobook:
        """Fetch the full audiobook by asin with all details including chapters.

        This method fetches complete audiobook details including chapters and resume position.
        Use this when the user requests full details for a specific audiobook.
        """
        if use_cache:
            cached_book = await self.mass.cache.get(
                key=asin,
                provider=self.provider_instance,
                category=CACHE_CATEGORY_AUDIOBOOK,
                default=None,
            )
            if cached_book is not None:
                book = self._parse_audiobook(cached_book)
                # Enrich with chapters and resume position
                await self._enrich_audiobook(book, asin)
                return book
        response = await self._call_api(
            f"library/{asin}",
            response_groups="""
                contributors, media, price, product_attrs, product_desc, product_details,
                product_extended_attrs,is_finished
                """,
        )

        if response is None:
            raise MediaNotFoundError(f"Audiobook with ASIN {asin} not found")

        item_data = response.get("item")
        if item_data is None:
            raise MediaNotFoundError(f"Audiobook data for ASIN {asin} is empty")

        await self.mass.cache.set(
            key=asin,
            provider=self.provider_instance,
            category=CACHE_CATEGORY_AUDIOBOOK,
            data=item_data,
        )
        book = self._parse_audiobook(item_data)
        # Enrich with chapters and resume position
        await self._enrich_audiobook(book, asin)
        return book

    async def _enrich_audiobook(self, book: Audiobook, asin: str) -> None:
        """Enrich audiobook with chapters and resume position.

        This makes additional API calls and should only be used for full audiobook details,
        not during library sync.
        """
        # Fetch chapters
        chapters_data = await self._fetch_chapters(asin=asin)
        if chapters_data:
            chapters: list[MediaItemChapter] = [
                self._parse_chapter_data(chapter, idx) for idx, chapter in enumerate(chapters_data)
            ]
            book.metadata.chapters = chapters
            # Update duration from chapters if available (more accurate)
            try:
                duration = sum(chapter.get("length_ms", 0) for chapter in chapters_data) / 1000
                if duration > 0:
                    book.duration = duration
            except Exception as exc:
                self.logger.warning(f"Error calculating duration from chapters for {asin}: {exc}")

        # Fetch resume position
        book.resume_position_ms = await self.get_last_postion(asin=asin)

    async def get_stream(
        self, asin: str, media_type: MediaType = MediaType.AUDIOBOOK
    ) -> StreamDetails:
        """Get stream details for an audiobook or podcast episode.

        :param asin: The ASIN of the content.
        :param media_type: The type of media (audiobook or podcast episode).
        """
        if not asin:
            self.logger.error("Invalid ASIN provided to get_stream")
            raise ValueError("Invalid ASIN provided to get_stream")

        duration = 0
        # For audiobooks, try to get duration from chapters
        if media_type == MediaType.AUDIOBOOK:
            chapters = await self._fetch_chapters(asin=asin)
            if chapters:
                try:
                    duration = sum(chapter.get("length_ms", 0) for chapter in chapters) / 1000
                except Exception as exc:
                    self.logger.warning(f"Error calculating duration for ASIN {asin}: {exc}")

        try:
            # Podcasts use Mpeg (non-DRM MP3), audiobooks use HLS
            if media_type == MediaType.PODCAST_EPISODE:
                playback_info = await self.client.post(
                    f"content/{asin}/licenserequest",
                    body={
                        "consumption_type": "Streaming",
                        "drm_type": "Mpeg",
                        "quality": "High",
                    },
                )
            else:
                playback_info = await self.client.post(
                    f"content/{asin}/licenserequest",
                    body={
                        "quality": "High",
                        "response_groups": "content_reference,certificate",
                        "consumption_type": "Streaming",
                        "supported_media_features": {
                            "codecs": ["mp4a.40.2", "mp4a.40.42"],
                            "drm_types": [
                                "Hls",
                            ],
                        },
                        "spatial": False,
                    },
                )

            content_license = playback_info.get("content_license", {})
            if not content_license:
                self.logger.error(f"No content_license in playback_info for ASIN {asin}")
                raise ValueError(f"Missing content_license for ASIN {asin}")

            content_metadata = content_license.get("content_metadata", {})
            content_reference = content_metadata.get("content_reference", {})
            size = content_reference.get("content_size_in_bytes", 0)

            stream_url = content_license.get("license_response")
            if not stream_url:
                self.logger.error(f"No license_response (stream URL) for ASIN {asin}")
                raise ValueError(f"Missing stream URL for ASIN {asin}")

            acr = content_license.get("acr", "")
            if acr:
                self._acr_cache[(asin, media_type)] = acr

            content_type = (
                ContentType.MP3 if media_type == MediaType.PODCAST_EPISODE else ContentType.AAC
            )
        except Exception as exc:
            self.logger.error(f"Error getting stream details for ASIN {asin}: {exc}")
            raise ValueError(f"Failed to get stream details: {exc}") from exc

        return StreamDetails(
            provider=self.provider_instance,
            size=size,
            item_id=f"{asin}",
            audio_format=AudioFormat(content_type=content_type),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=stream_url,
            can_seek=True,
            allow_seek=True,
            duration=duration,
            data={"acr": acr},
        )

    async def _fetch_chapters(self, asin: str) -> list[dict[str, Any]]:
        """Fetch chapter data for an audiobook."""
        if not asin or asin == "error":
            self.logger.warning(
                "Invalid ASIN provided to _fetch_chapters, returning empty chapter list"
            )
            return []

        chapters_data: list[Any] = await self.mass.cache.get(
            key=asin, provider=self.provider_instance, category=CACHE_CATEGORY_CHAPTERS, default=[]
        )

        if not chapters_data:
            try:
                response = await self._call_api(
                    f"content/{asin}/metadata",
                    response_groups="chapter_info, always-returned, content_reference, content_url",
                    chapter_titles_type="Flat",
                )

                if not response:
                    self.logger.warning(f"Failed to get metadata for ASIN {asin}")
                    return []

                content_metadata = response.get("content_metadata")
                if not content_metadata:
                    self.logger.warning(f"No content_metadata for ASIN {asin}")
                    return []

                chapter_info = content_metadata.get("chapter_info")
                if not chapter_info:
                    self.logger.warning(f"No chapter_info for ASIN {asin}")
                    return []

                chapters_data = chapter_info.get("chapters") or []

                await self.mass.cache.set(
                    key=asin,
                    data=chapters_data,
                    provider=self.provider_instance,
                    category=CACHE_CATEGORY_CHAPTERS,
                )
            except Exception as exc:
                self.logger.error(f"Error fetching chapters for ASIN {asin}: {exc}")
                chapters_data = []

        return chapters_data

    async def get_last_postion(self, asin: str) -> int:
        """Fetch last position of asin."""
        if not asin or asin == "error":
            return 0

        try:
            response = await self._call_api("annotations/lastpositions", asins=asin)

            if not response:
                self.logger.debug(f"No last position data available for ASIN {asin}")
                return 0

            annotations = response.get("asin_last_position_heard_annots")
            if not annotations or not isinstance(annotations, list) or len(annotations) == 0:
                self.logger.debug(f"No annotations found for ASIN {asin}")
                return 0

            annotation = annotations[0]
            if not annotation or not isinstance(annotation, dict):
                self.logger.debug(f"Invalid annotation for ASIN {asin}")
                return 0

            last_position = annotation.get("last_position_heard")
            if not last_position or not isinstance(last_position, dict):
                self.logger.debug(f"Invalid last_position for ASIN {asin}")
                return 0

            position_ms = last_position.get("position_ms", 0)
            return int(position_ms)

        except Exception as exc:
            self.logger.error(f"Error getting last position for ASIN {asin}: {exc}")
            return 0

    async def set_last_position(
        self, asin: str, pos: int, media_type: MediaType = MediaType.AUDIOBOOK
    ) -> None:
        """Report last position to Audible.

        :param asin: The content ID (audiobook or podcast episode).
        :param pos: Position in seconds.
        :param media_type: The type of media (audiobook or podcast episode).
        """
        if not asin or asin == "error" or pos <= 0:
            return

        try:
            position_ms = pos * 1000

            # Try to get ACR from cache first
            acr = self._acr_cache.get((asin, media_type))
            if not acr:
                stream_details = await self.get_stream(asin=asin, media_type=media_type)
                acr = stream_details.data.get("acr")

            if not acr:
                self.logger.warning(f"No ACR available for ASIN {asin}, cannot report position")
                return

            await self.client.put(
                f"lastpositions/{asin}", body={"acr": acr, "asin": asin, "position_ms": position_ms}
            )

            self.logger.debug(f"Successfully reported position {position_ms}ms for ASIN {asin}")

        except (KeyError, TypeError) as exc:
            self.logger.error(
                f"Error accessing data while reporting position for ASIN {asin}: {exc}"
            )
        except TimeoutError as exc:
            self.logger.error(f"Timeout while reporting position for ASIN {asin}: {exc}")
        except ConnectionError as exc:
            self.logger.error(f"Connection error while reporting position for ASIN {asin}: {exc}")
        except Exception as exc:
            self.logger.error(f"Unexpected error reporting position for ASIN {asin}: {exc}")

    async def _call_api(self, path: str, **kwargs: Any) -> Any:
        response = None
        use_cache = kwargs.pop("use_cache", False)
        params_str = json.dumps(kwargs, sort_keys=True)
        params_hash = hashlib.md5(params_str.encode()).hexdigest()
        cache_key_with_params = f"{path}:{params_hash}"
        if use_cache:
            response = await self.mass.cache.get(
                key=cache_key_with_params,
                provider=self.provider_instance,
                category=CACHE_CATEGORY_API,
            )
        if not response:
            response = await self.client.get(path, **kwargs)
            await self.mass.cache.set(
                key=cache_key_with_params, provider=self.provider_instance, data=response
            )
        return response

    def _parse_contributors(
        self, contributors_list: list[dict[str, Any]] | None, default_name: str
    ) -> list[str]:
        """Parse contributors (authors, narrators) from API response."""
        result: list[str] = []
        contributors: list[dict[str, Any]] = contributors_list or []
        if isinstance(contributors, list):
            for contributor in contributors:
                if contributor and isinstance(contributor, dict):
                    result.append(contributor.get("name", default_name))
        return result

    def _create_images(self, image_path: str | None) -> list[MediaItemImage]:
        """Create image objects if image path exists."""
        images: list[MediaItemImage] = []
        if image_path:
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_path,
                    provider=self.provider_instance,
                    remotely_accessible=True,
                )
            )
            images.append(
                MediaItemImage(
                    type=ImageType.CLEARART,
                    path=image_path,
                    provider=self.provider_instance,
                    remotely_accessible=True,
                )
            )
        return images

    def _parse_chapter_data(self, chapter_data: dict[str, Any], index: int) -> MediaItemChapter:
        """Parse chapter data into MediaItemChapter object."""
        try:
            start = int(chapter_data.get("start_offset_sec", 0))
        except (TypeError, ValueError):
            start = 0

        try:
            length = int(chapter_data.get("length_ms", 0)) / 1000
        except (TypeError, ValueError):
            length = 0

        raw_title = chapter_data.get("title")
        chapter_title: str
        if raw_title is None:
            chapter_title = f"Chapter {index + 1}"
        elif isinstance(raw_title, str):
            chapter_title = raw_title
        else:
            chapter_title = str(raw_title)

        return MediaItemChapter(position=index, name=chapter_title, start=start, end=start + length)

    def _parse_audiobook(self, audiobook_data: dict[str, Any] | None) -> Audiobook:
        """Parse audiobook data from API response.

        NOTE: This is a pure parser - no API calls allowed here.
        Chapters and resume position are fetched lazily when needed.
        """
        if audiobook_data is None:
            self.logger.error("Received None audiobook_data in _parse_audiobook")
            raise MediaNotFoundError("Audiobook data not found")

        asin = audiobook_data.get("asin", "")
        title = audiobook_data.get("title", "")

        # Parse authors and narrators
        narrators = self._parse_contributors(audiobook_data.get("narrators"), "Unknown Narrator")
        authors = self._parse_contributors(audiobook_data.get("authors"), "Unknown Author")

        # Get duration from runtime_length_min (provided by 'media' response group)
        # Chapters are fetched lazily when streaming, not during library sync
        runtime_minutes = audiobook_data.get("runtime_length_min", 0)
        duration = runtime_minutes * 60 if runtime_minutes else 0

        # Create audiobook object
        book = Audiobook(
            item_id=asin,
            provider=self.provider_instance,
            name=title,
            duration=duration,
            provider_mappings={
                ProviderMapping(
                    item_id=asin,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_instance,
                )
            },
            publisher=audiobook_data.get("publisher_name"),
            authors=UniqueList(authors),
            narrators=UniqueList(narrators),
        )

        # Set metadata
        book.metadata.copyright = audiobook_data.get("copyright")
        book.metadata.description = _html_to_txt(
            str(audiobook_data.get("extended_product_description", ""))
        )
        book.metadata.languages = UniqueList([audiobook_data.get("language") or ""])
        if release_date := audiobook_data.get("release_date"):
            with suppress(ValueError):
                datetime.strptime(release_date, "%Y-%m-%d").astimezone(UTC)

        # Set review if available
        reviews = audiobook_data.get("editorial_reviews", [])
        if reviews and reviews[0]:
            book.metadata.review = _html_to_txt(str(reviews[0]))

        # Set genres
        book.metadata.genres = {
            genre.replace("_", " ") for genre in (audiobook_data.get("platinum_keywords") or [])
        }

        # Add images
        image_path = audiobook_data.get("product_images", {}).get("500")
        book.metadata.images = UniqueList(self._create_images(image_path))

        # Chapters are not fetched during parsing - they are fetched lazily when streaming
        # This avoids N+1 API calls during library sync

        return book

    async def _process_podcast_item(self, podcast_data: dict[str, Any]) -> Podcast | None:
        """Process a single podcast item from the library."""
        asin = str(podcast_data.get("asin", ""))
        cached_podcast = None
        if asin:
            cached_podcast = await self.mass.cache.get(
                key=asin,
                provider=self.provider_instance,
                category=CACHE_CATEGORY_PODCAST,
                default=None,
            )

        try:
            if cached_podcast is not None:
                return self._parse_podcast(cached_podcast)
            return self._parse_podcast(podcast_data)
        except MediaNotFoundError as exc:
            self.logger.warning(f"Skipping invalid podcast: {exc}")
            return None
        except Exception as exc:
            self.logger.warning(
                f"Error processing podcast {podcast_data.get('asin', 'unknown')}: {exc}"
            )
            return None

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Fetch podcasts from the user's library with pagination."""
        response_groups = [
            "contributors",
            "media",
            "product_attrs",
            "product_desc",
            "product_details",
            "product_extended_attrs",
        ]

        async for item in self._fetch_library_items(
            ",".join(response_groups), PODCAST_CONTENT_TYPES
        ):
            if podcast := await self._process_podcast_item(item):
                yield podcast

    async def get_podcast(self, asin: str, use_cache: bool = True) -> Podcast:
        """Fetch full podcast details by ASIN.

        :param asin: The ASIN of the podcast.
        :param use_cache: Whether to use cached data if available.
        """
        if use_cache:
            cached_podcast = await self.mass.cache.get(
                key=asin,
                provider=self.provider_instance,
                category=CACHE_CATEGORY_PODCAST,
                default=None,
            )
            if cached_podcast is not None:
                return self._parse_podcast(cached_podcast)

        response = await self._call_api(
            f"library/{asin}",
            response_groups="""
                contributors, media, price, product_attrs, product_desc, product_details,
                product_extended_attrs, relationships
                """,
        )

        if response is None:
            raise MediaNotFoundError(f"Podcast with ASIN {asin} not found")

        item_data = response.get("item")
        if item_data is None:
            raise MediaNotFoundError(f"Podcast data for ASIN {asin} is empty")

        await self.mass.cache.set(
            key=asin,
            provider=self.provider_instance,
            category=CACHE_CATEGORY_PODCAST,
            data=item_data,
        )
        return self._parse_podcast(item_data)

    async def get_podcast_episodes(self, podcast_asin: str) -> AsyncGenerator[PodcastEpisode, None]:
        """Fetch all episodes for a podcast.

        :param podcast_asin: The ASIN of the parent podcast.
        """
        podcast = await self.get_podcast(podcast_asin)

        # Fetch episodes - they're typically in relationships or we need to query children
        response_groups = [
            "contributors",
            "media",
            "product_attrs",
            "product_desc",
            "product_details",
            "relationships",
        ]

        page = 1
        page_size = 50
        position = 0

        while True:
            # Query for children of the podcast parent
            response = await self._call_api(
                "library",
                use_cache=False,
                response_groups=",".join(response_groups),
                parent_asin=podcast_asin,
                page=page,
                num_results=page_size,
            )

            items = response.get("items", [])
            if not items:
                break

            for episode_data in items:
                try:
                    episode = self._parse_podcast_episode(episode_data, podcast, position)
                    position += 1
                    yield episode
                except Exception as exc:
                    asin = episode_data.get("asin", "unknown")
                    self.logger.warning(f"Error parsing podcast episode {asin}: {exc}")

            page += 1
            if len(items) < page_size:
                break

    async def get_podcast_episode(self, episode_asin: str) -> PodcastEpisode:
        """Fetch full podcast episode details by ASIN.

        :param episode_asin: The ASIN of the podcast episode.
        """
        response = await self._call_api(
            f"library/{episode_asin}",
            response_groups="""
                contributors, media, price, product_attrs, product_desc, product_details,
                product_extended_attrs, relationships
                """,
        )

        if response is None:
            raise MediaNotFoundError(f"Podcast episode with ASIN {episode_asin} not found")

        item_data = response.get("item")
        if item_data is None:
            raise MediaNotFoundError(f"Podcast episode data for ASIN {episode_asin} is empty")

        # Try to get parent podcast info from relationships
        podcast: Podcast | None = None
        relationships = item_data.get("relationships", [])
        for rel in relationships:
            if rel.get("relationship_type") == "parent":
                parent_asin = rel.get("asin")
                if parent_asin:
                    with suppress(MediaNotFoundError):
                        podcast = await self.get_podcast(parent_asin)
                break

        return self._parse_podcast_episode(item_data, podcast, 0)

    def _parse_podcast(self, podcast_data: dict[str, Any] | None) -> Podcast:
        """Parse podcast data from API response.

        :param podcast_data: Raw podcast data from the Audible API.
        """
        if podcast_data is None:
            self.logger.error("Received None podcast_data in _parse_podcast")
            raise MediaNotFoundError("Podcast data not found")

        asin = podcast_data.get("asin", "")
        title = podcast_data.get("title", "")
        publisher = podcast_data.get("publisher_name", "")

        # Create podcast object
        podcast = Podcast(
            item_id=asin,
            provider=self.provider_instance,
            name=title,
            publisher=publisher,
            provider_mappings={
                ProviderMapping(
                    item_id=asin,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_instance,
                )
            },
        )

        # Set metadata
        podcast.metadata.description = _html_to_txt(
            str(
                podcast_data.get("publisher_summary", "")
                or podcast_data.get("extended_product_description", "")
            )
        )
        podcast.metadata.languages = UniqueList([podcast_data.get("language") or ""])

        # Set genres
        podcast.metadata.genres = {
            genre.replace("_", " ") for genre in (podcast_data.get("platinum_keywords") or [])
        }

        # Add images
        image_path = podcast_data.get("product_images", {}).get("500")
        podcast.metadata.images = UniqueList(self._create_images(image_path))

        return podcast

    def _parse_podcast_episode(
        self,
        episode_data: dict[str, Any] | None,
        podcast: Podcast | None,
        position: int,
    ) -> PodcastEpisode:
        """Parse podcast episode data from API response.

        :param episode_data: Raw episode data from the Audible API.
        :param podcast: Parent podcast object (optional).
        :param position: Position/index of the episode in the podcast.
        """
        if episode_data is None:
            self.logger.error("Received None episode_data in _parse_podcast_episode")
            raise MediaNotFoundError("Podcast episode data not found")

        asin = episode_data.get("asin", "")
        title = episode_data.get("title", "")

        # Get duration from runtime_length_min
        runtime_minutes = episode_data.get("runtime_length_min", 0)
        duration = runtime_minutes * 60 if runtime_minutes else 0

        # Create podcast reference - use Podcast object or create ItemMapping
        podcast_ref: Podcast | ItemMapping
        if podcast is not None:
            podcast_ref = podcast
        else:
            # Try to get parent_asin from relationships for ItemMapping
            parent_asin = ""
            relationships = episode_data.get("relationships", [])
            for rel in relationships:
                if rel.get("relationship_type") == "parent":
                    parent_asin = rel.get("asin", "")
                    break

            if not parent_asin:
                self.logger.warning(
                    "No parent_asin found for podcast episode %s; parent podcast is unknown",
                    asin,
                )

            podcast_ref = ItemMapping(
                item_id=parent_asin or "",
                provider=self.provider_instance,
                name="Unknown Podcast",
                media_type=MediaType.PODCAST,
            )

        # Create episode object
        episode = PodcastEpisode(
            item_id=asin,
            provider=self.provider_instance,
            name=title,
            duration=duration,
            position=position,
            podcast=podcast_ref,
            provider_mappings={
                ProviderMapping(
                    item_id=asin,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_instance,
                )
            },
        )

        # Set metadata
        episode.metadata.description = _html_to_txt(
            str(
                episode_data.get("publisher_summary", "")
                or episode_data.get("extended_product_description", "")
            )
        )

        # Add images
        image_path = episode_data.get("product_images", {}).get("500")
        episode.metadata.images = UniqueList(self._create_images(image_path))

        return episode

    async def get_authors(self) -> dict[str, str]:
        """Get all unique authors from the library.

        Returns dict mapping author ASIN to author name.
        """
        authors: dict[str, str] = {}
        async for item in self._fetch_library_items(
            "contributors,product_attrs", AUDIOBOOK_CONTENT_TYPES
        ):
            for author in item.get("authors") or []:
                asin = author.get("asin")
                name = author.get("name")
                if asin and name:
                    authors[asin] = name
        return authors

    async def get_series(self) -> dict[str, str]:
        """Get all unique series from the library.

        Returns dict mapping series ASIN to series title.
        """
        series: dict[str, str] = {}
        async for item in self._fetch_library_items(
            "series,product_attrs", AUDIOBOOK_CONTENT_TYPES
        ):
            for s in item.get("series") or []:
                asin = s.get("asin")
                title = s.get("title")
                if asin and title:
                    series[asin] = title
        return series

    async def get_narrators(self) -> dict[str, str]:
        """Get all unique narrators from the library.

        Returns dict mapping narrator ASIN to narrator name.
        """
        narrators: dict[str, str] = {}
        async for item in self._fetch_library_items(
            "contributors,product_attrs", AUDIOBOOK_CONTENT_TYPES
        ):
            for narrator in item.get("narrators") or []:
                asin = narrator.get("asin")
                name = narrator.get("name")
                if asin and name:
                    narrators[asin] = name
        return narrators

    async def get_genres(self) -> set[str]:
        """Get all unique genres from the library."""
        genres: set[str] = set()
        async for item in self._fetch_library_items("product_attrs", AUDIOBOOK_CONTENT_TYPES):
            for keyword in item.get("thesaurus_subject_keywords") or []:
                genres.add(keyword.replace("_", " ").replace("-", " ").title())
        return genres

    async def get_publishers(self) -> set[str]:
        """Get all unique publishers from the library."""
        publishers: set[str] = set()
        async for item in self._fetch_library_items("product_attrs", AUDIOBOOK_CONTENT_TYPES):
            publisher = item.get("publisher_name")
            if publisher:
                publishers.add(publisher)
        return publishers

    async def get_audiobooks_by_author(self, author_asin: str) -> list[Audiobook]:
        """Get all audiobooks by a specific author, sorted by release date."""
        audiobooks: list[tuple[str, Audiobook]] = []
        async for item in self._fetch_library_items(
            "contributors,media,product_attrs,product_desc,series", AUDIOBOOK_CONTENT_TYPES
        ):
            for author in item.get("authors") or []:
                if author.get("asin") == author_asin:
                    release_date = item.get("release_date") or "0000-00-00"
                    audiobooks.append((release_date, self._parse_audiobook(item)))
                    break
        audiobooks.sort(key=lambda x: x[0], reverse=True)
        return [book for _, book in audiobooks]

    async def get_audiobooks_by_narrator(self, narrator_asin: str) -> list[Audiobook]:
        """Get all audiobooks by a specific narrator, sorted by release date."""
        audiobooks: list[tuple[str, Audiobook]] = []
        async for item in self._fetch_library_items(
            "contributors,media,product_attrs,product_desc,series", AUDIOBOOK_CONTENT_TYPES
        ):
            for narrator in item.get("narrators") or []:
                if narrator.get("asin") == narrator_asin:
                    release_date = item.get("release_date") or "0000-00-00"
                    audiobooks.append((release_date, self._parse_audiobook(item)))
                    break
        audiobooks.sort(key=lambda x: x[0], reverse=True)
        return [book for _, book in audiobooks]

    async def get_audiobooks_by_genre(self, genre: str) -> list[Audiobook]:
        """Get all audiobooks matching a genre, sorted by release date."""
        audiobooks: list[tuple[str, Audiobook]] = []
        genre_key = genre.lower().replace(" ", "_")
        genre_key_alt = genre.lower().replace(" ", "-")
        async for item in self._fetch_library_items(
            "contributors,media,product_attrs,product_desc,series", AUDIOBOOK_CONTENT_TYPES
        ):
            keywords = item.get("thesaurus_subject_keywords") or []
            if genre_key in keywords or genre_key_alt in keywords:
                release_date = item.get("release_date") or "0000-00-00"
                audiobooks.append((release_date, self._parse_audiobook(item)))
        audiobooks.sort(key=lambda x: x[0], reverse=True)
        return [book for _, book in audiobooks]

    async def get_audiobooks_by_publisher(self, publisher: str) -> list[Audiobook]:
        """Get all audiobooks from a specific publisher, sorted by release date."""
        audiobooks: list[tuple[str, Audiobook]] = []
        async for item in self._fetch_library_items(
            "contributors,media,product_attrs,product_desc,series", AUDIOBOOK_CONTENT_TYPES
        ):
            if item.get("publisher_name") == publisher:
                release_date = item.get("release_date") or "0000-00-00"
                audiobooks.append((release_date, self._parse_audiobook(item)))
        audiobooks.sort(key=lambda x: x[0], reverse=True)
        return [book for _, book in audiobooks]

    async def get_audiobooks_by_series(self, series_asin: str) -> list[Audiobook]:
        """Get all audiobooks in a specific series, ordered by sequence."""
        audiobooks: list[tuple[float, Audiobook]] = []
        async for item in self._fetch_library_items(
            "contributors,media,product_attrs,product_desc,series", AUDIOBOOK_CONTENT_TYPES
        ):
            for s in item.get("series") or []:
                if s.get("asin") == series_asin:
                    sequence = s.get("sequence")
                    try:
                        seq_num = float(sequence) if sequence else 999
                    except (ValueError, TypeError):
                        seq_num = 999
                    audiobooks.append((seq_num, self._parse_audiobook(item)))
                    break
        audiobooks.sort(key=lambda x: x[0])
        return [book for _, book in audiobooks]

    async def deregister(self) -> None:
        """Deregister this provider from Audible."""
        await asyncio.to_thread(self.client.auth.deregister_device)


def _html_to_txt(html_text: str) -> str:
    txt = html.unescape(html_text)
    tags = re.findall("<[^>]+>", txt)
    for tag in tags:
        txt = txt.replace(tag, "")
    return txt


async def audible_get_auth_info(locale: str) -> tuple[str, str, str]:
    """Generate the login URL and auth info for Audible OAuth flow.

    :param locale: The locale string (e.g., 'us', 'uk', 'de').
    :return: Tuple of (code_verifier, oauth_url, serial).
    """
    locale_obj = audible.localization.Locale(locale)
    code_verifier = await asyncio.to_thread(audible.login.create_code_verifier)
    oauth_url, serial = await asyncio.to_thread(
        audible.login.build_oauth_url,
        country_code=locale_obj.country_code,
        domain=locale_obj.domain,
        market_place_id=locale_obj.market_place_id,
        code_verifier=code_verifier,
        with_username=False,
    )

    return code_verifier.decode(), oauth_url, serial


async def audible_custom_login(
    code_verifier: str, response_url: str, serial: str, locale: str
) -> audible.Authenticator:
    """Complete the authentication using the code_verifier, response_url, and serial.

    :param code_verifier: The code verifier string used in OAuth flow.
    :param response_url: The response URL containing the authorization code.
    :param serial: The device serial number.
    :param locale: The locale string.
    :return: Audible Authenticator object.
    :raises LoginFailed: If authorization code is not found in the URL.
    """
    logger = logging.getLogger("audible_helper")
    auth = audible.Authenticator()
    auth.locale = audible.localization.Locale(locale)

    response_url_parsed = urlparse(response_url)
    parsed_qs = parse_qs(response_url_parsed.query)

    # Try multiple parameter names for authorization code
    # Audible may use different parameter names depending on the flow
    authorization_code = None
    for param_name in ["openid.oa2.authorization_code", "authorization_code", "code"]:
        if codes := parsed_qs.get(param_name):
            authorization_code = codes[0]
            logger.debug("Found authorization code in parameter: %s", param_name)
            break

    if not authorization_code:
        available_params = list(parsed_qs.keys())
        raise LoginFailed(
            f"Authorization code not found in URL. "
            f"Expected 'openid.oa2.authorization_code' but found parameters: {available_params}"
        )

    registration_data = await asyncio.to_thread(
        audible.register.register,
        authorization_code=authorization_code,
        code_verifier=code_verifier.encode(),
        domain=auth.locale.domain,
        serial=serial,
    )
    auth._update_attrs(**registration_data)

    # Log what auth methods are available after registration
    if auth.adp_token and auth.device_private_key:
        logger.info("Registration successful with signing auth (stable)")
    else:
        logger.warning("Registration successful but signing auth not available")

    return auth


async def check_file_exists(path: str | PathLike[str]) -> bool:
    """Async file exists check."""
    return await asyncio.to_thread(os.path.exists, path)


async def remove_file(path: str | PathLike[str]) -> None:
    """Async file delete."""
    await asyncio.to_thread(os.remove, path)
