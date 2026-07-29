"""iTunes Podcast search support for MusicAssistant."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
    TaskScheduleType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_ENTRY_LIBRARY_SYNC_PODCASTS
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.countries import get_country_codes
from music_assistant.helpers.podcast_parsers import (
    enrich_episode_chapters,
    get_podcastparser_dict,
    parse_podcast,
    parse_podcast_episode,
)
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.itunes_podcasts.schema import (
    ITunesSearchResults,
    PodcastSearchResult,
    TopPodcastsHelper,
    TopPodcastsResponse,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


CONF_LOCALE = "locale"
CONF_EXPLICIT = "explicit"
CONF_NUM_EPISODES = "num_episodes"

# store to search when the server's language has no matching iTunes storefront
DEFAULT_LOCALE = "us"

CACHE_CATEGORY_PODCASTS = 0
CACHE_CATEGORY_RECOMMENDATIONS = 1
CACHE_KEY_TOP_PODCASTS = "top-podcasts"
RECOMMENDATION_ROW_TOP_PODCASTS = "itunes-top-podcasts"

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.RECOMMENDATIONS,
    # This provider does not have a "real" library. Refer to method comment
    # in get_library_podcasts
    ProviderFeature.LIBRARY_PODCASTS,
}

CONF_ENTRY_LIBRARY_SYNC_PODCASTS_HIDDEN = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_LIBRARY_SYNC_PODCASTS.to_dict(),
        "hidden": True,
        "default_value": True,
    }
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ITunesPodcastsProvider(mass, manifest, config, SUPPORTED_FEATURES)


class ITunesPodcastsProvider(MusicProvider):
    """ITunesPodcastsProvider."""

    throttler: ThrottlerManager

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        country_codes = await asyncio.to_thread(get_country_codes)

        language_options = [
            ConfigValueOption(key.lower(), title=val) for key, val in country_codes.items()
        ]
        # the store country decides which catalog is searched; default to the region of the
        # server's language so the provider can be added without picking one first
        region = self.mass.metadata.locale.split("_")[-1].upper()
        return (
            CONF_ENTRY_LIBRARY_SYNC_PODCASTS_HIDDEN,
            ConfigEntry(
                key=CONF_LOCALE,
                type=ConfigEntryType.STRING,
                required=True,
                options=language_options,
                default_value=region.lower() if region in country_codes else DEFAULT_LOCALE,
            ),
            ConfigEntry(
                key=CONF_NUM_EPISODES,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=0,
            ),
            ConfigEntry(
                key=CONF_EXPLICIT,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=True,
            ),
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        return True

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.max_episodes = int(str(self.config.get_value(CONF_NUM_EPISODES)))
        # 20 requests per minute, be a bit below
        self.throttler = ThrottlerManager(rate_limit=18, period=60)

    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """Perform search on musicprovider."""
        result = SearchResults()
        if MediaType.PODCAST not in media_types:
            return result

        if limit < 1:
            limit = 1
        elif limit > 200:
            limit = 200
        country = str(self.config.get_value(CONF_LOCALE))
        explicit = "Yes" if bool(self.config.get_value(CONF_EXPLICIT)) else "No"
        params: dict[str, str | int] = {
            "media": "podcast",
            "entity": "podcast",
            "country": country,
            "attribute": "titleTerm",
            "explicit": explicit,
            "limit": limit,
            "term": search_query,
        }
        url = "https://itunes.apple.com/search?"
        result.podcasts = await self._perform_search(url, params)

        return result

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's available recommendation rows, without items.

        A single row with the top podcasts for the configured country.
        """
        return [
            RecommendationFolder(
                item_id=RECOMMENDATION_ROW_TOP_PODCASTS,
                name="Trending Podcasts",
                icon="mdi-trending-up",
                translation_key="trending_podcasts",
                provider=self.instance_id,
            )
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        if item_id != RECOMMENDATION_ROW_TOP_PODCASTS:
            return UniqueList()
        search_results = await self._cache_get_top_podcasts()
        return UniqueList(self._get_podcast_list(search_results))

    @throttle_with_retries
    async def _perform_search(self, url: str, params: dict[str, str | int]) -> list[Podcast]:
        response = await self.mass.http_session.get(url, params=params)
        json_response = b""
        if response.status == 200:
            json_response = await response.read()
        if not json_response:
            return []
        results = ITunesSearchResults.from_json(json_response).results
        return self._get_podcast_list(results)

    def _get_podcast_list(self, results: list[PodcastSearchResult]) -> list[Podcast]:
        podcast_list: list[Podcast] = []
        for result in results:
            if result.feed_url is None or result.track_name is None:
                self.logger.info(
                    "The podcast '%s' does not have a feed url. Please see the docs for more info.",
                    result.track_name,
                )
                continue
            podcast = Podcast(
                name=result.track_name,
                item_id=result.feed_url,
                publisher=result.artist_name,
                provider=self.instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=result.feed_url,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            image_list = []
            for artwork_url in [
                result.artwork_url_600,
                result.artwork_url_100,
                result.artwork_url_60,
                result.artwork_url_30,
            ]:
                if artwork_url is not None:
                    image_list.append(
                        MediaItemImage(
                            type=ImageType.THUMB, path=artwork_url, provider=self.instance_id
                        )
                    )
            podcast.metadata.images = UniqueList(image_list)
            podcast_list.append(podcast)
        return podcast_list

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """
        Get library podcasts.

        We use get_library_podcasts to sync all feeds which have been added to the MA library
        by the user via the search function. The provider itself does not offer a real library.

        The item_id corresponds to the feed_url.
        """
        podcasts = await self.mass.music.podcasts.get_library_items_by_prov_id(
            provider_instance=self.instance_id
        )
        for podcast in podcasts:
            our_provider_mapping: ProviderMapping | None = None
            for provider_mapping in podcast.provider_mappings:
                if provider_mapping.provider_instance == self.instance_id:
                    our_provider_mapping = provider_mapping
                    break
            if our_provider_mapping is None:
                # We should never end up here.
                self.logger.error("Podcast %s lacks a provider mapping.", podcast.name)
                continue
            feed_url = our_provider_mapping.item_id
            parsed_podcast: dict[str, Any] | None = None
            try:
                parsed_podcast = await get_podcastparser_dict(
                    session=self.mass.http_session,
                    feed_url=feed_url,
                    max_episodes=self.max_episodes,
                )
                await self._cache_set_podcast(feed_url=feed_url, parsed_podcast=parsed_podcast)
                self.logger.debug("Synced podcast %s.", podcast.name)
            except MediaNotFoundError:
                # If we are not able to refresh the podcast, we must prevent the sync
                # from deleting the podcast from the library - that is both a breaking change
                # (pre March 2026) and certainly not desired just because of some downtime.
                self.logger.warning("Was unable to sync podcast %s (%s).", podcast.name, feed_url)
                podcast.item_id = feed_url
                podcast.provider_mappings = {our_provider_mapping}
                yield podcast
                continue

            yield parse_podcast(
                feed_url=feed_url,
                parsed_feed=parsed_podcast,
                instance_id=self.instance_id,
                domain=self.domain,
            )

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get podcast."""
        parsed = await self._cache_get_podcast(prov_podcast_id)

        return parse_podcast(
            feed_url=prov_podcast_id,
            parsed_feed=parsed,
            instance_id=self.instance_id,
            domain=self.domain,
        )

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get podcast episodes."""
        podcast = await self._cache_get_podcast(prov_podcast_id)
        podcast_cover = podcast.get("cover_url")
        episodes = podcast.get("episodes", [])
        for cnt, episode in enumerate(episodes):
            if mass_episode := parse_podcast_episode(
                episode=episode,
                prov_podcast_id=prov_podcast_id,
                episode_cnt=cnt,
                podcast_cover=podcast_cover,
                podcast_name=podcast.get("title"),
                domain=self.domain,
                instance_id=self.instance_id,
            ):
                yield mass_episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode."""
        podcast_id, guid_or_stream_url = prov_episode_id.split(" ")
        podcast = await self._cache_get_podcast(podcast_id)
        podcast_cover = podcast.get("cover_url")
        for cnt, episode in enumerate(podcast.get("episodes", [])):
            mass_episode = parse_podcast_episode(
                episode=episode,
                prov_podcast_id=podcast_id,
                episode_cnt=cnt,
                podcast_cover=podcast_cover,
                podcast_name=podcast.get("title"),
                domain=self.domain,
                instance_id=self.instance_id,
            )
            if mass_episode is None:
                continue
            _, _guid_or_stream_url = mass_episode.item_id.split(" ")
            # this is enough, as internal
            if guid_or_stream_url == _guid_or_stream_url:
                await enrich_episode_chapters(
                    session=self.mass.http_session,
                    chapters_json_url=episode.get("chapters_json_url"),
                    mass_episode=mass_episode,
                )
                return mass_episode
        raise MediaNotFoundError("Episode not found")

    async def _get_episode_stream_url(self, podcast_id: str, guid_or_stream_url: str) -> str | None:
        podcast = await self._cache_get_podcast(podcast_id)
        episodes = podcast.get("episodes", [])
        for episode in episodes:
            episode_enclosures = episode.get("enclosures", [])
            if len(episode_enclosures) < 1:
                # episode without an enclosure carries no stream; skip it instead of
                # aborting the lookup for the (potentially later) requested episode
                continue
            stream_url: str | None = episode_enclosures[0].get("url", None)
            guid = episode.get("guid")
            if guid is not None and len(guid.split(" ")) == 1:
                _guid_or_stream_url_compare = guid
            else:
                _guid_or_stream_url_compare = stream_url
            if guid_or_stream_url == _guid_or_stream_url_compare:
                return stream_url
        return None

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for item."""
        podcast_id, guid_or_stream_url = item_id.split(" ")
        stream_url = await self._get_episode_stream_url(podcast_id, guid_or_stream_url)
        if stream_url is None:
            raise MediaNotFoundError
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(stream_url),
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=stream_url,
            can_seek=True,
            allow_seek=True,
        )

    @throttle_with_retries
    async def _get_podcast_search_result_from_itunes_id(
        self, itunes_id: int
    ) -> PodcastSearchResult:
        params = {"id": itunes_id}
        url = "https://itunes.apple.com/lookup?"
        response = await self.mass.http_session.get(url, params=params)
        json_response = b""
        if response.status == 200:
            json_response = await response.read()
        if not json_response:
            raise MediaNotFoundError
        search_results = ITunesSearchResults.from_json(json_response)
        if search_results.result_count == 0:
            raise MediaNotFoundError
        if search_results.result_count > 1:
            self.logger.warning("More than a single result for podcast.")
        return search_results.results[0]

    async def _cache_get_podcast(self, prov_podcast_id: str) -> dict[str, Any]:
        parsed_podcast = await self.mass.cache.get(
            key=prov_podcast_id,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PODCASTS,
            default=None,
        )
        if parsed_podcast is None:
            # get_podcastparser_dict raises MediaNotFoundError if data is invalid
            parsed_podcast = await get_podcastparser_dict(
                session=self.mass.http_session,
                feed_url=prov_podcast_id,
                max_episodes=self.max_episodes,
            )
            await self._cache_set_podcast(feed_url=prov_podcast_id, parsed_podcast=parsed_podcast)

        # this is a dictionary from podcastparser
        return parsed_podcast  # type: ignore[no-any-return]

    async def _cache_set_podcast(self, feed_url: str, parsed_podcast: dict[str, Any]) -> None:
        # Cache slightly longer than the effective sync interval to avoid fetching
        # the same podcast feed repeatedly during recurring library sync.
        schedule = self.mass.music.get_provider_sync_schedule(self.instance_id, MediaType.PODCAST)
        library_sync_enabled = bool(self.config.get_value("library_sync_podcasts"))
        if not library_sync_enabled or schedule is None or not schedule.enabled:
            cache_time = 60 * 60 * 12  # 12h
        elif schedule.type == TaskScheduleType.HOURLY and schedule.every is not None:
            cache_time = schedule.every * 60 * 60 + 600  # 10 minutes extra cache
        elif schedule.type == TaskScheduleType.DAILY and schedule.every is not None:
            cache_time = schedule.every * 24 * 60 * 60 + 600
        else:
            cache_time = 60 * 60 * 12  # 12h
        await self.mass.cache.set(
            key=feed_url,
            provider=self.instance_id,
            category=CACHE_CATEGORY_PODCASTS,
            data=parsed_podcast,
            expiration=cache_time,
        )

    async def _cache_set_top_podcasts(self, top_podcast_helper: TopPodcastsHelper) -> None:
        await self.mass.cache.set(
            key=CACHE_KEY_TOP_PODCASTS,
            provider=self.instance_id,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
            data=top_podcast_helper.to_dict(),
            expiration=60 * 60 * 6,  # 6 hours
        )

    async def _cache_get_top_podcasts(self) -> list[PodcastSearchResult]:
        parsed_top_podcasts = await self.mass.cache.get(
            key=CACHE_KEY_TOP_PODCASTS,
            provider=self.instance_id,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
        )
        if parsed_top_podcasts is not None:
            helper = TopPodcastsHelper.from_dict(parsed_top_podcasts)
            return helper.top_podcasts

        # 15 results
        # keep 20 requests max per minute in mind
        # https://rss.marketingtools.apple.com/
        country = str(self.config.get_value(CONF_LOCALE))
        url = f"https://rss.marketingtools.apple.com/api/v2/{country}/podcasts/top/15/podcasts.json"
        response = await self.mass.http_session.get(url)
        json_response = b""
        if response.status == 200:
            json_response = await response.read()
        if not json_response:
            return []

        top_podcasts_response = TopPodcastsResponse.from_json(json_response)

        if top_podcasts_response.feed is None:
            return []

        include_explicit = bool(self.config.get_value(CONF_EXPLICIT))

        helper = TopPodcastsHelper()
        for top_podcast in top_podcasts_response.feed.results:
            if not include_explicit and top_podcast.content_advisory_rating is not None:
                # the spelling within the API is wrong.
                if top_podcast.content_advisory_rating in [
                    "explicit",
                    "Explicit",
                    "Explict",
                    "explict",
                ]:
                    continue
            try:
                podcast_search_result = await self._get_podcast_search_result_from_itunes_id(
                    int(top_podcast.id_)
                )
            except MediaNotFoundError:
                continue
            helper.top_podcasts.append(podcast_search_result)

        await self._cache_set_top_podcasts(top_podcast_helper=helper)
        return helper.top_podcasts
