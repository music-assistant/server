"""Podcast functionality for Audible provider."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.mass import MusicAssistant
from music_assistant.providers.audible.api import AudibleAPI, html_to_txt

CACHE_DOMAIN = "audible"
CACHE_CATEGORY_PODCAST = 3
CACHE_CATEGORY_PODCAST_EPISODE = 4

PODCAST_RESPONSE_GROUPS = (
    "contributors,media,product_attrs,product_desc,product_details,product_extended_attrs"
)

EPISODE_RESPONSE_GROUPS = (
    "contributors,media,product_attrs,product_desc,product_details,product_extended_attrs"
)


class PodcastHelper:
    """Helper for Audible podcast functionality."""

    def __init__(
        self,
        api: AudibleAPI,
        mass: MusicAssistant,
        provider_domain: str,
        provider_instance: str,
        logger: logging.Logger | None = None,
    ):
        """Initialize the Podcast Helper."""
        self.api = api
        self.mass = mass
        self.provider_domain = provider_domain
        self.provider_instance = provider_instance
        self.logger = logger or logging.getLogger("audible_podcast")

    async def _get_from_cache(self, key: str, category: int, default: Any = None) -> Any:
        """Get item from cache with standard parameters."""
        return await self.mass.cache.get(
            key=key, base_key=CACHE_DOMAIN, category=category, default=default
        )

    async def _set_to_cache(self, key: str, category: int, data: dict[str, Any]) -> None:
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

    async def get_podcast(self, asin: str, use_cache: bool = True) -> Podcast:
        """Fetch the podcast by asin."""
        if use_cache:
            cached_podcast = await self._get_from_cache(asin, CACHE_CATEGORY_PODCAST)
            if cached_podcast is not None:
                return await self._parse_podcast(cached_podcast)

        response = await self.api.call_api(
            f"library/{asin}",
            response_groups=PODCAST_RESPONSE_GROUPS,
        )

        if response is None:
            raise MediaNotFoundError(f"Podcast with ASIN {asin} not found")

        item_data = response.get("item")
        if item_data is None:
            raise MediaNotFoundError(f"Podcast data for ASIN {asin} is empty")

        await self._set_to_cache(asin, CACHE_CATEGORY_PODCAST, item_data)
        return await self._parse_podcast(item_data)

    async def get_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Fetch the user's podcast library with pagination."""
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
                response_groups=PODCAST_RESPONSE_GROUPS,
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
                    "Audible: No more items returned, ending pagination (processed %s podcasts)",
                    total_processed,
                )
                break

            page_processed = 0
            for podcast_data in items:
                content_type = podcast_data.get("content_delivery_type", "")
                if content_type != "PodcastParent":
                    continue

                asin = podcast_data.get("asin")
                cached_podcast = await self._get_from_cache(asin, CACHE_CATEGORY_PODCAST)

                try:
                    if cached_podcast is not None:
                        podcast = await self._parse_podcast(cached_podcast)
                        yield podcast
                    else:
                        podcast = await self._parse_podcast(podcast_data)
                        yield podcast

                    page_processed += 1
                    total_processed += 1

                except MediaNotFoundError as exc:
                    self.logger.warning(f"Skipping invalid podcast: {exc}")
                    continue

            self.logger.debug(
                "Audible: Processed %s valid podcasts on page %s", page_processed, page
            )

            # If we got fewer items than requested, we've reached the end
            if len(items) < page_size:
                self.logger.debug(
                    "Audible: Fewer items than page size returned, ending pagination "
                    f"(processed {total_processed} podcasts total)",
                )
                break

            page += 1
            self.logger.debug(
                "Audible: Moving to page %s (processed: %s, total reported: %s)",
                page,
                total_processed,
                total_items,
            )

        self.logger.info(
            "Audible: Successfully retrieved %s podcasts from library", total_processed
        )

    async def _parse_podcast(self, podcast_data: dict[str, Any] | None) -> Podcast:
        """Parse podcast data from Audible API.

        Convert Audible API podcast data to Music Assistant Podcast object.
        """
        if podcast_data is None:
            self.logger.error("Received None podcast_data in _parse_podcast")
            raise MediaNotFoundError("Podcast data not found")

        asin = podcast_data.get("asin", "")
        title = podcast_data.get("title", "")

        podcast = Podcast(
            item_id=asin,
            provider=self.provider_instance,
            name=title,
            publisher=podcast_data.get("publisher_name", ""),
            provider_mappings={
                ProviderMapping(
                    item_id=asin,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_instance,
                )
            },
        )

        podcast.metadata.description = html_to_txt(
            str(podcast_data.get("extended_product_description", ""))
        )
        podcast.metadata.languages = UniqueList([podcast_data.get("language", "")])
        podcast.metadata.release_date = podcast_data.get("release_date")

        podcast.metadata.genres = {
            genre.replace("_", " ") for genre in podcast_data.get("platinum_keywords", "")
        }

        image_url = podcast_data.get("product_images", {}).get("500")
        if image_url:
            podcast.metadata.images = UniqueList(
                [
                    await self._create_media_item_image(image_url, ImageType.THUMB),
                    await self._create_media_item_image(image_url, ImageType.CLEARART),
                ]
            )

        await self._set_to_cache(asin, CACHE_CATEGORY_PODCAST, podcast_data)

        return podcast

    async def get_podcast_episodes(self, podcast_asin: str) -> AsyncGenerator[PodcastEpisode, None]:
        """Fetch episodes for a podcast by its ASIN with pagination."""
        if not podcast_asin:
            self.logger.error("Invalid podcast ASIN provided to get_podcast_episodes")
            return

        page = 1
        page_size = 50
        total_processed = 0
        overall_position = 0

        while True:
            self.logger.debug(
                "Audible: Fetching episodes for podcast %s (page %s, page_size %s)",
                podcast_asin,
                page,
                page_size,
            )

            try:
                episodes_response = await self.api.call_api(
                    "library",
                    parent_asin=podcast_asin,
                    response_groups=EPISODE_RESPONSE_GROUPS,
                    use_cache=False,
                    page=page,
                    num_results=page_size,
                )

                items = episodes_response.get("items", [])
                total_items = episodes_response.get("total_results", 0)

                self.logger.debug(
                    "Audible: Got %s episodes for podcast %s (total reported by API: %s)",
                    len(items),
                    podcast_asin,
                    total_items,
                )

                if not items:
                    self.logger.debug(
                        "Audible: No more episodes returned, ending pagination "
                        f"(processed {total_processed} episodes)",
                    )
                    break

                page_processed = 0
                for episode_data in items:
                    try:
                        content_type = episode_data.get("content_delivery_type", "")
                        episode_asin = episode_data.get("asin")

                        self.logger.debug(
                            "Audible: Episode %s has content_delivery_type: %s",
                            episode_asin,
                            content_type,
                        )

                        cache_key = self._create_episode_id(podcast_asin, episode_asin)
                        cached_episode = await self._get_from_cache(
                            cache_key, CACHE_CATEGORY_PODCAST_EPISODE
                        )

                        if cached_episode is not None:
                            episode = await self._parse_podcast_episode(
                                cached_episode, podcast_asin, overall_position
                            )
                            yield episode
                        else:
                            episode = await self._parse_podcast_episode(
                                episode_data, podcast_asin, overall_position
                            )
                            yield episode

                        page_processed += 1
                        total_processed += 1
                        overall_position += 1

                    except MediaNotFoundError as exc:
                        self.logger.warning(f"Skipping invalid podcast episode: {exc}")
                        continue

                self.logger.debug(
                    "Audible: Processed %s valid episodes on page %s", page_processed, page
                )

                # If we got fewer items than requested, we've reached the end
                if len(items) < page_size:
                    self.logger.debug(
                        "Audible: Fewer episodes than page size returned, ending pagination "
                        f"(processed {total_processed} episodes total)",
                    )
                    break

                page += 1
                self.logger.debug(
                    "Audible: Moving to page %s (processed: %s, total reported: %s)",
                    page,
                    total_processed,
                    total_items,
                )

            except Exception as exc:
                self.logger.error(f"Error fetching episodes for podcast {podcast_asin}: {exc}")
                return

    async def _get_podcast_name(self, podcast_asin: str) -> str:
        """Get podcast name from cache."""
        try:
            podcast_data = await self._get_from_cache(podcast_asin, CACHE_CATEGORY_PODCAST)
            return (
                podcast_data.get("title", "Unknown Podcast") if podcast_data else "Unknown Podcast"
            )
        except Exception:
            return "Unknown Podcast"

    async def get_podcast_episode(self, episode_id: str) -> PodcastEpisode:
        """Get a single podcast episode by its ID (format: podcast_asin:episode_asin)."""
        try:
            podcast_asin, episode_asin = self._split_episode_id(episode_id)
        except ValueError:
            self.logger.error("Invalid episode ID format: %s", episode_id)
            raise MediaNotFoundError(f"Invalid episode ID format: {episode_id}")

        # First check cache
        cached_episode = await self._get_from_cache(episode_id, CACHE_CATEGORY_PODCAST_EPISODE)
        if cached_episode is not None:
            return await self._parse_podcast_episode(cached_episode, podcast_asin, 0)

        # If not in cache, fetch directly using episode ASIN
        try:
            response = await self.api.call_api(
                f"library/{episode_asin}",
                parent_asin=podcast_asin,
                response_groups=EPISODE_RESPONSE_GROUPS,
                use_cache=False,
            )

            if response is None:
                raise MediaNotFoundError(f"Episode {episode_id} not found")

            episode_data = response.get("item")
            if episode_data is None:
                raise MediaNotFoundError(f"Episode data for {episode_id} is empty")

            return await self._parse_podcast_episode(episode_data, podcast_asin, 0)

        except Exception as exc:
            self.logger.error(f"Error fetching episode {episode_id}: {exc}")
            raise MediaNotFoundError(f"Failed to fetch episode {episode_id}: {exc}")

    async def _parse_podcast_episode(
        self, episode_data: dict[str, Any], podcast_asin: str, position: int
    ) -> PodcastEpisode:
        """Parse podcast episode data from Audible API.

        Convert Audible API podcast episode data to Music Assistant PodcastEpisode object.
        """
        episode_asin = episode_data.get("asin", "")
        title = episode_data.get("title", "")
        episode_id = self._create_episode_id(podcast_asin, episode_asin)
        podcast_name = await self._get_podcast_name(podcast_asin)
        duration_min = episode_data.get("runtime_length_min", 0)
        duration = int(duration_min * 60) if duration_min else 0
        resume_position_ms = await self.get_last_position(podcast_asin, episode_asin)

        episode = PodcastEpisode(
            item_id=episode_id,
            provider=self.provider_instance,
            name=title,
            duration=duration,
            position=position,
            resume_position_ms=resume_position_ms,
            podcast=ItemMapping(
                item_id=podcast_asin,
                provider=self.provider_instance,
                name=podcast_name,
                media_type=MediaType.PODCAST,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=episode_id,
                    provider_domain=self.provider_domain,
                    provider_instance=self.provider_instance,
                    audio_format=AudioFormat(content_type=ContentType.AAC),
                )
            },
        )

        episode.metadata.description = html_to_txt(
            str(episode_data.get("extended_product_description", ""))
        )
        episode.metadata.release_date = episode_data.get("release_date")

        image_url = episode_data.get("product_images", {}).get("500")
        if image_url:
            episode.metadata.images = UniqueList(
                [await self._create_media_item_image(image_url, ImageType.THUMB)]
            )

        await self._set_to_cache(episode_id, CACHE_CATEGORY_PODCAST_EPISODE, episode_data)

        return episode

    async def _get_episode_data(self, item_id: str) -> dict[str, Any]:
        """Get episode data from cache or API."""
        try:
            podcast_asin, episode_asin = self._split_episode_id(item_id)
        except ValueError as exc:
            raise ValueError(str(exc))

        # First check cache
        cached_episode = await self._get_from_cache(item_id, CACHE_CATEGORY_PODCAST_EPISODE)
        if cached_episode is not None:
            return dict(cached_episode)

        # If not in cache, fetch directly using episode ASIN
        try:
            response = await self.api.call_api(
                f"library/{episode_asin}",
                parent_asin=podcast_asin,
                response_groups=EPISODE_RESPONSE_GROUPS,
                use_cache=False,
            )

            if response is None:
                raise ValueError(f"Episode {item_id} not found")

            episode_data = response.get("item")
            if episode_data is None:
                raise ValueError(f"Episode data for {item_id} is empty")

            return dict(episode_data)

        except Exception as exc:
            self.logger.error(f"Error fetching episode data for {item_id}: {exc}")
            raise ValueError(f"Failed to fetch episode data: {exc}")

    async def get_stream(self, item_id: str) -> StreamDetails:
        """Get stream details for a podcast episode."""
        try:
            podcast_asin, episode_asin = self._split_episode_id(item_id)
        except ValueError as exc:
            self.logger.error(f"Invalid item_id provided to get_stream: {exc}")
            raise ValueError(f"Invalid item_id provided to get_stream: {exc}")

        try:
            try:
                episode_data = await self._get_episode_data(item_id)
            except ValueError as exc:
                self.logger.error(f"Episode data not found for {item_id}: {exc}")
                raise ValueError(f"Episode data not found for {item_id}")

            duration_min = episode_data.get("runtime_length_min", 0)
            duration = int(duration_min * 60) if duration_min else 0

            content_type = episode_data.get("content_delivery_type", "")
            self.logger.debug(
                "Audible: Episode %s has content_delivery_type: %s for streaming",
                episode_asin,
                content_type,
            )

            playback_info = await self.api.client.post(
                f"content/{episode_asin}/licenserequest",
                body={
                    "quality": "High",
                    "consumption_type": "Streaming",
                    "supported_media_features": {
                        "codecs": ["mp4a.40.2", "mp4a.40.42"],
                        "drm_types": ["Mpeg", "Hls"],
                    },
                },
            )

            content_license = playback_info.get("content_license", {})
            if not content_license:
                self.logger.error(f"No content_license in playback_info for episode {item_id}")
                raise ValueError(f"Missing content_license for episode {item_id}")

            content_metadata = content_license.get("content_metadata", {})
            content_reference = content_metadata.get("content_reference", {})
            size = content_reference.get("content_size_in_bytes", 0)

            m3u8_url = content_license.get("license_response")
            if not m3u8_url:
                self.logger.error(f"No license_response (stream URL) for episode {item_id}")
                raise ValueError(f"Missing stream URL for episode {item_id}")

            acr = content_license.get("acr")

            return StreamDetails(
                provider=self.provider_instance,
                size=size,
                item_id=item_id,
                audio_format=AudioFormat(content_type=ContentType.AAC),
                media_type=MediaType.PODCAST_EPISODE,
                stream_type=StreamType.HTTP,
                path=m3u8_url,
                can_seek=True,
                allow_seek=True,
                duration=duration,
                data={"acr": acr},
            )

        except Exception as exc:
            self.logger.error(f"Error getting stream details for episode {item_id}: {exc}")
            raise ValueError(f"Failed to get stream details: {exc}") from exc

    async def get_last_position(self, podcast_asin: str, episode_asin: str) -> int:
        """Fetch last position of a podcast episode."""
        if not podcast_asin or not episode_asin:
            return 0

        try:
            response = await self.api.call_api("annotations/lastpositions", asins=episode_asin)

            if not response:
                self.logger.debug(f"No last position data available for episode {episode_asin}")
                return 0

            annotations = response.get("asin_last_position_heard_annots")
            if not annotations or not isinstance(annotations, list) or len(annotations) == 0:
                self.logger.debug(f"No annotations found for episode {episode_asin}")
                return 0

            annotation = annotations[0]
            if not annotation or not isinstance(annotation, dict):
                self.logger.debug(f"Invalid annotation for episode {episode_asin}")
                return 0

            last_position = annotation.get("last_position_heard")
            if not last_position or not isinstance(last_position, dict):
                self.logger.debug(f"Invalid last_position for episode {episode_asin}")
                return 0

            position_ms = last_position.get("position_ms", 0)
            return int(position_ms)

        except Exception as exc:
            self.logger.error(f"Error getting last position for episode {episode_asin}: {exc}")
            return 0

    def _create_episode_id(self, podcast_asin: str, episode_asin: str) -> str:
        """Create a unique ID for an episode using podcast_asin:episode_asin format."""
        return f"{podcast_asin}:{episode_asin}"

    def _split_episode_id(self, episode_id: str) -> tuple[str, str]:
        """Split an episode ID into podcast_asin and episode_asin."""
        if not episode_id or ":" not in episode_id:
            raise ValueError(f"Invalid episode ID format: {episode_id}")

        podcast_asin, episode_asin = episode_id.split(":", 1)

        if not podcast_asin or not episode_asin:
            raise ValueError(f"Invalid podcast_asin or episode_asin in {episode_id}")

        return podcast_asin, episode_asin

    async def set_last_position(self, item_id: str, pos: int) -> None:
        """Report last position to Audible for a podcast episode.

        Args:
            item_id: The podcast episode ID in format podcast_asin:episode_asin
            pos: Position in seconds
        """
        if not item_id or ":" not in item_id or pos <= 0:
            return

        try:
            _, episode_asin = self._split_episode_id(item_id)
            position_ms = pos * 1000

            try:
                stream_details = await self.get_stream(item_id=item_id)
                acr = stream_details.data.get("acr")
            except Exception as exc:
                self.logger.error(f"Error getting stream details for episode {item_id}: {exc}")
                raise ValueError(f"Failed to get ACR: {exc}") from exc

            if not acr:
                self.logger.warning(
                    f"No ACR available for episode {item_id}, cannot report position"
                )
                return

            await self.api.client.put(
                f"lastpositions/{episode_asin}",
                body={
                    "acr": acr,
                    "asin": episode_asin,
                    "position_ms": position_ms,
                    "response_groups": "last_position",
                },
            )

            self.logger.debug(
                f"Successfully reported position {position_ms}ms for episode {item_id}"
            )

        except Exception as exc:
            self.logger.error(f"Error reporting position for episode {item_id}: {exc}")
