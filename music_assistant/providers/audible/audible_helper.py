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
from datetime import UTC, datetime
from os import PathLike
from typing import Any
from urllib.parse import parse_qs, urlparse

import audible
import audible.register
from audible import AsyncClient
from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    AudioFormat,
    MediaItemChapter,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.mass import MusicAssistant

CACHE_DOMAIN = "audible"
CACHE_CATEGORY_API = 0
CACHE_CATEGORY_AUDIOBOOK = 1
CACHE_CATEGORY_CHAPTERS = 2

_AUTH_CACHE: dict[str, audible.Authenticator] = {}


async def cached_authenticator_from_file(path: str) -> audible.Authenticator:
    """Get an authenticator from file with caching to avoid repeated file reads."""
    logger = logging.getLogger("audible_helper")
    if path in _AUTH_CACHE:
        return _AUTH_CACHE[path]

    logger.debug("Loading authenticator from file %s and caching it", path)
    auth = await asyncio.to_thread(audible.Authenticator.from_file, path)
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

    async def _process_audiobook_item(
        self, audiobook_data: dict[str, Any], total_processed: int
    ) -> tuple[Audiobook | None, int]:
        """Process a single audiobook item from the library."""
        content_type = audiobook_data.get("content_delivery_type", "")
        if content_type not in ("SinglePartBook",):
            self.logger.debug(
                "Skipping non-audiobook item: %s (%s)",
                audiobook_data.get("title", "Unknown"),
                content_type,
            )
            return None, total_processed + 1

        # Ensure asin is a valid string
        asin = str(audiobook_data.get("asin", ""))
        cached_book = None
        if asin:
            cached_book = await self.mass.cache.get(
                key=asin, base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_AUDIOBOOK, default=None
            )

        try:
            if cached_book is not None:
                album = await self._parse_audiobook(cached_book)
            else:
                album = await self._parse_audiobook(audiobook_data)
            return album, total_processed + 1
        except MediaNotFoundError as exc:
            self.logger.warning(f"Skipping invalid audiobook: {exc}")
            return None, total_processed + 1
        except Exception as exc:
            self.logger.warning(
                f"Error processing audiobook {audiobook_data.get('asin', 'unknown')}: {exc}"
            )
            return None, total_processed + 1

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

        page = 1
        page_size = 50
        total_processed = 0
        max_iterations = 100
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            self.logger.debug(
                "Audible: Fetching library page %s with page_size %s (processed so far: %s)",
                page,
                page_size,
                total_processed,
            )

            library = await self._call_api(
                "library",
                use_cache=False,
                response_groups=",".join(response_groups),
                page=page,
                num_results=page_size,
            )

            items = library.get("items", [])
            total_items = library.get("total_results", 0)
            self.logger.debug(
                "Audible: Got %s items (total reported by API: %s)", len(items), total_items
            )

            if not items:
                self.logger.debug(
                    "Audible: No more items returned, ending pagination (processed %s items)",
                    total_processed,
                )
                break

            items_processed_this_page = 0
            for audiobook_data in items:
                album, total_processed = await self._process_audiobook_item(
                    audiobook_data, total_processed
                )
                if album:
                    yield album
                    items_processed_this_page += 1

            self.logger.debug(
                "Audible: Processed %s valid audiobooks on page %s", items_processed_this_page, page
            )

            page += 1
            self.logger.debug(
                "Audible: Moving to page %s (processed: %s, total reported: %s)",
                page,
                total_processed,
                total_items,
            )

            if len(items) < page_size:
                self.logger.debug(
                    "Audible: Fewer than page size returned, ending pagination "
                    "(processed %s items)",
                    total_processed,
                )
                break

        if iteration >= max_iterations:
            self.logger.warning(
                "Audible: Reached maximum iteration limit (%s) with %s items processed",
                max_iterations,
                total_processed,
            )
        else:
            self.logger.info(
                "Audible: Successfully retrieved %s audiobooks from library", total_processed
            )

    async def get_audiobook(self, asin: str, use_cache: bool = True) -> Audiobook:
        """Fetch the audiobook by asin."""
        if use_cache:
            cached_book = await self.mass.cache.get(
                key=asin, base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_AUDIOBOOK, default=None
            )
            if cached_book is not None:
                return await self._parse_audiobook(cached_book)
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
            base_key=CACHE_DOMAIN,
            category=CACHE_CATEGORY_AUDIOBOOK,
            data=item_data,
        )
        return await self._parse_audiobook(item_data)

    async def get_stream(self, asin: str) -> StreamDetails:
        """Get stream details for a track (audiobook chapter)."""
        if not asin:
            self.logger.error("Invalid ASIN provided to get_stream")
            raise ValueError("Invalid ASIN provided to get_stream")

        chapters = await self._fetch_chapters(asin=asin)
        if not chapters:
            self.logger.warning(f"No chapters found for ASIN {asin}, using default duration")
            duration = 0
        else:
            try:
                duration = sum(chapter.get("length_ms", 0) for chapter in chapters) / 1000
            except Exception as exc:
                self.logger.warning(f"Error calculating duration for ASIN {asin}: {exc}")
                duration = 0

        try:
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

            m3u8_url = content_license.get("license_response")
            if not m3u8_url:
                self.logger.error(f"No license_response (stream URL) for ASIN {asin}")
                raise ValueError(f"Missing stream URL for ASIN {asin}")

            acr = content_license.get("acr", "")
        except Exception as exc:
            self.logger.error(f"Error getting stream details for ASIN {asin}: {exc}")
            raise ValueError(f"Failed to get stream details: {exc}") from exc
        return StreamDetails(
            provider=self.provider_instance,
            size=size,
            item_id=f"{asin}",
            audio_format=AudioFormat(content_type=ContentType.AAC),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.HTTP,
            path=m3u8_url,
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
            base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_CHAPTERS, key=asin, default=[]
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
                    base_key=CACHE_DOMAIN,
                    category=CACHE_CATEGORY_CHAPTERS,
                    key=asin,
                    data=chapters_data,
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

    async def set_last_position(self, asin: str, pos: int) -> None:
        """Report last position to Audible.

        Args:
            asin: The audiobook ID
            pos: Position in seconds
        """
        if not asin or asin == "error" or pos <= 0:
            return

        try:
            position_ms = pos * 1000

            stream_details = await self.get_stream(asin=asin)
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
                key=cache_key_with_params, base_key=CACHE_DOMAIN, category=CACHE_CATEGORY_API
            )
        if not response:
            response = await self.client.get(path, **kwargs)
            await self.mass.cache.set(
                key=cache_key_with_params, base_key=CACHE_DOMAIN, data=response
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

    async def _parse_audiobook(self, audiobook_data: dict[str, Any] | None) -> Audiobook:
        """Parse audiobook data from API response."""
        if audiobook_data is None:
            self.logger.error("Received None audiobook_data in _parse_audiobook")
            raise MediaNotFoundError("Audiobook data not found")

        asin = audiobook_data.get("asin", "")
        title = audiobook_data.get("title", "")

        # Parse authors and narrators
        narrators = self._parse_contributors(audiobook_data.get("narrators"), "Unknown Narrator")
        authors = self._parse_contributors(audiobook_data.get("authors"), "Unknown Author")

        # Get chapters and calculate duration
        chapters_data = await self._fetch_chapters(asin=asin)
        try:
            duration = sum(chapter.get("length_ms", 0) for chapter in chapters_data) / 1000
        except Exception as exc:
            self.logger.warning(f"Error calculating duration for audiobook {asin}: {exc}")
            duration = 0

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

        # Parse chapters
        chapters: list[MediaItemChapter] = [
            self._parse_chapter_data(chapter, idx) for idx, chapter in enumerate(chapters_data)
        ]
        book.metadata.chapters = chapters

        # Get resume position
        book.resume_position_ms = await self.get_last_postion(asin=asin)
        return book

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
    """
    Generate the login URL and auth info for Audible OAuth flow asynchronously.

    Args:
        locale: The locale string (e.g., 'us', 'uk', 'de') to determine region settings
    Returns:
        A tuple containing:
        - code_verifier (str): The OAuth code verifier string
        - oauth_url (str): The complete OAuth URL for login
        - serial (str): The generated device serial number
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
    """
    Complete the authentication using the code_verifier, response_url, and serial asynchronously.

    Args:
        code_verifier: The code verifier string used in OAuth flow
        response_url: The response URL containing the authorization code
        serial: The device serial number
        locale: The locale string
    Returns:
        Audible Authenticator object
    Raises:
        LoginFailed: If authorization code is not found in the URL
    """
    auth = audible.Authenticator()
    auth.locale = audible.localization.Locale(locale)

    response_url_parsed = urlparse(response_url)
    parsed_qs = parse_qs(response_url_parsed.query)

    authorization_codes = parsed_qs.get("openid.oa2.authorization_code")
    if not authorization_codes:
        raise LoginFailed("Authorization code not found in the provided URL.")

    authorization_code = authorization_codes[0]
    registration_data = await asyncio.to_thread(
        audible.register.register,
        authorization_code=authorization_code,
        code_verifier=code_verifier.encode(),
        domain=auth.locale.domain,
        serial=serial,
    )
    auth._update_attrs(**registration_data)
    return auth


async def check_file_exists(path: str | PathLike[str]) -> bool:
    """Async file exists check."""
    return await asyncio.to_thread(os.path.exists, path)


async def remove_file(path: str | PathLike[str]) -> None:
    """Async file delete."""
    await asyncio.to_thread(os.remove, path)
