"""Audiobook functionality for Audible provider."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
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
from music_assistant.providers.audible.api import AudibleAPI, html_to_txt

CACHE_DOMAIN = "audible"
CACHE_CATEGORY_AUDIOBOOK = 1
CACHE_CATEGORY_CHAPTERS = 2

AUDIOBOOK_RESPONSE_GROUPS = (
    "contributors,"
    "customer_rights,"
    "media,"
    "product_attrs,"
    "product_desc,"
    "product_details,"
    "product_extended_attrs"
)

AUDIOBOOK_DETAIL_RESPONSE_GROUPS = (
    "contributors,"
    "customer_rights,"
    "media,"
    "product_attrs,"
    "product_desc,"
    "product_details,"
    "product_extended_attrs,"
    "is_finished"
)

CHAPTER_RESPONSE_GROUPS = "chapter_info,content_reference,content_url"


class AudiobookHelper:
    """Helper for Audible audiobook functionality."""

    def __init__(
        self,
        api: AudibleAPI,
        mass: MusicAssistant,
        provider_domain: str,
        provider_instance: str,
        logger: logging.Logger | None = None,
    ):
        """Initialize the Audiobook Helper."""
        self.api = api
        self.mass = mass
        self.provider_domain = provider_domain
        self.provider_instance = provider_instance
        self.logger = logger or logging.getLogger("audible_audiobook")

    async def _get_from_cache(self, key: str, category: int, default: Any = None) -> Any:
        """Get item from cache with standard parameters."""
        return await self.mass.cache.get(
            key=key, base_key=CACHE_DOMAIN, category=category, default=default
        )

    async def _set_to_cache(self, key: str, category: int, data: Any) -> None:
        """Set item to cache with standard parameters."""
        await self.mass.cache.set(key=key, base_key=CACHE_DOMAIN, category=category, data=data)

    async def _create_media_item_image(
        self, image_url: str, image_type: ImageType
    ) -> MediaItemImage:
        """Create a MediaItemImage with standard parameters."""
        return MediaItemImage(
            type=image_type,
            path=image_url,
            provider=self.provider_instance,
            remotely_accessible=True,
        )

    async def get_library(self) -> AsyncGenerator[Audiobook, None]:
        """Fetch the user's audiobook library with pagination."""
        page = 1
        page_size = 50
        total_processed = 0

        while True:
            self.logger.debug(
                "Audible: Fetching library page %s with page_size %s",
                page,
                page_size,
            )

            library = await self.api.call_api(
                "library",
                use_cache=False,
                response_groups=AUDIOBOOK_RESPONSE_GROUPS,
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

            page_processed = 0
            for audiobook_data in items:
                content_type = audiobook_data.get("content_delivery_type", "")
                if content_type in ("PodcastParent", "NonAudio"):
                    self.logger.debug(
                        "Skipping non-audiobook item: %s (%s)",
                        audiobook_data.get("title", "Unknown"),
                        content_type,
                    )
                    continue
                customer_rights = audiobook_data.get(
                    "customer_rights", {"is_consumable_offline": False}
                )
                is_consumable_offline = customer_rights.get("is_consumable_offline", False)
                if not is_consumable_offline:
                    self.logger.info(
                        "Skipping audiobook: %s, no stream license.",
                        audiobook_data.get("title", "Unknown"),
                    )
                    continue

                asin = audiobook_data.get("asin")
                cached_book = await self._get_from_cache(asin, CACHE_CATEGORY_AUDIOBOOK)

                try:
                    if cached_book is not None:
                        album = await self._parse_audiobook(cached_book)
                        yield album
                    else:
                        album = await self._parse_audiobook(audiobook_data)
                        yield album
                    page_processed += 1
                    total_processed += 1

                except MediaNotFoundError as exc:
                    self.logger.warning(f"Skipping invalid audiobook: {exc}")
                    continue

            self.logger.debug(
                "Audible: Processed %s valid audiobooks on page %s", page_processed, page
            )

            # If we got fewer items than requested, we've reached the end
            if len(items) < page_size:
                self.logger.debug(
                    "Audible: Fewer items than page size returned, ending pagination "
                    f"(processed {total_processed} items total)",
                )
                break

            page += 1
            self.logger.debug(
                "Audible: Moving to page %s (processed: %s, total reported: %s)",
                page,
                total_processed,
                total_items,
            )

    async def get_audiobook(self, asin: str, use_cache: bool = True) -> Audiobook:
        """Fetch the audiobook by asin."""
        if use_cache:
            cached_book = await self._get_from_cache(asin, CACHE_CATEGORY_AUDIOBOOK)
            if cached_book is not None:
                return await self._parse_audiobook(cached_book)

        response = await self.api.call_api(
            f"library/{asin}",
            response_groups=AUDIOBOOK_DETAIL_RESPONSE_GROUPS,
        )

        if response is None:
            raise MediaNotFoundError(f"Audiobook with ASIN {asin} not found")

        item_data = response.get("item")
        if item_data is None:
            raise MediaNotFoundError(f"Audiobook data for ASIN {asin} is empty")

        await self._set_to_cache(asin, CACHE_CATEGORY_AUDIOBOOK, item_data)
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
            duration = sum(chapter["length_ms"] for chapter in chapters) / 1000

        try:
            playback_info = await self.api.client.post(
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

            acr = content_license.get("acr")
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

        chapters_data: list[Any] = await self._get_from_cache(asin, CACHE_CATEGORY_CHAPTERS, [])

        if not chapters_data:
            try:
                response = await self.api.call_api(
                    f"content/{asin}/metadata",
                    response_groups=CHAPTER_RESPONSE_GROUPS,
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

                chapters_data = chapter_info.get("chapters", [])

                await self._set_to_cache(asin, CACHE_CATEGORY_CHAPTERS, chapters_data)
            except Exception as exc:
                self.logger.error(f"Error fetching chapters for ASIN {asin}: {exc}")
                chapters_data = []

        return chapters_data

    async def get_last_postion(self, asin: str) -> int:
        """Fetch last position of asin."""
        if not asin or asin == "error":
            return 0

        try:
            response = await self.api.call_api("annotations/lastpositions", asins=asin)

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

            await self.api.client.put(
                f"lastpositions/{asin}",
                body={
                    "acr": acr,
                    "asin": asin,
                    "position_ms": position_ms,
                    "response_groups": "last_position",
                },
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

    async def _parse_audiobook(self, audiobook_data: dict[str, Any] | None) -> Audiobook:
        """Parse audiobook data from Audible API and convert to Music Assistant Audiobook object."""
        if audiobook_data is None:
            self.logger.error("Received None audiobook_data in _parse_audiobook")
            raise MediaNotFoundError("Audiobook data not found")

        asin = audiobook_data.get("asin", "")
        title = audiobook_data.get("title", "")
        authors = []
        narrators = []

        narrators_list = audiobook_data.get("narrators") or []
        if isinstance(narrators_list, list):
            for narrator in narrators_list:
                if narrator and isinstance(narrator, dict):
                    narrators.append(narrator.get("name", "Unknown Narrator"))

        authors_list = audiobook_data.get("authors") or []
        if isinstance(authors_list, list):
            for author in authors_list:
                if author and isinstance(author, dict):
                    authors.append(author.get("name", "Unknown Author"))
        chapters_data = await self._fetch_chapters(asin=asin)
        duration = sum(chapter["length_ms"] for chapter in chapters_data) / 1000
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
        book.metadata.copyright = audiobook_data.get("copyright")
        book.metadata.description = html_to_txt(
            str(audiobook_data.get("extended_product_description", ""))
        )
        book.metadata.languages = UniqueList([audiobook_data.get("language", "")])
        book.metadata.release_date = audiobook_data.get("release_date")
        reviews = audiobook_data.get("editorial_reviews", [])
        if reviews:
            book.metadata.review = html_to_txt(reviews[0])
        book.metadata.genres = {
            genre.replace("_", " ") for genre in audiobook_data.get("platinum_keywords", "")
        }
        image_url = audiobook_data.get("product_images", {}).get("500")
        if image_url:
            book.metadata.images = UniqueList(
                [
                    await self._create_media_item_image(image_url, ImageType.THUMB),
                    await self._create_media_item_image(image_url, ImageType.CLEARART),
                ]
            )

        chapters = []
        for index, chapter_data in enumerate(chapters_data):
            start = int(chapter_data.get("start_offset_sec", 0))
            length = int(chapter_data.get("length_ms", 0)) / 1000
            raw_title = chapter_data.get("title")
            chapter_title: str
            if raw_title is None:
                chapter_title = f"Chapter {index + 1}"
            elif isinstance(raw_title, str):
                chapter_title = raw_title
            else:
                chapter_title = str(raw_title)

            chapters.append(
                MediaItemChapter(
                    position=index, name=chapter_title, start=start, end=start + length
                )
            )
        book.metadata.chapters = chapters
        book.resume_position_ms = await self.get_last_postion(asin=asin)
        return book
