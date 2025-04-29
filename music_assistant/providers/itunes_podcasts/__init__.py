"""iTunes Podcast search support for MusicAssistant."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles
import orjson
import podcastparser
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    MediaItemImage,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.podcast_parsers import parse_podcast, parse_podcast_episode
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.itunes_podcasts.schema import (
    ITunesSearchResults,
    PodcastSearchResult,
    TopPodcastsHelper,
    TopPodcastsResponse,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


CONF_LOCALE = "locale"
CONF_EXPLICIT = "explicit"
CONF_NUM_EPISODES = "num_episodes"

CACHE_CATEGORY_PODCASTS = 0
CACHE_CATEGORY_RECOMMENDATIONS = 1
CACHE_KEY_TOP_PODCASTS = "top-podcasts"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return ITunesPodcastsProvider(mass, manifest, config)


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
    json_path = Path(__file__).parent / "itunes_country_codes.json"
    async with aiofiles.open(json_path) as f:
        country_codes = orjson.loads(await f.read())

    language_options = [ConfigValueOption(val, key.lower()) for key, val in country_codes.items()]
    return (
        ConfigEntry(
            key=CONF_LOCALE,
            type=ConfigEntryType.STRING,
            label="Country",
            required=True,
            options=language_options,
        ),
        ConfigEntry(
            key=CONF_NUM_EPISODES,
            type=ConfigEntryType.INTEGER,
            label="Maximum number of episodes. 0 for unlimited.",
            required=False,
            description="Maximum number of episodes. 0 for unlimited.",
            default_value=0,
        ),
        ConfigEntry(
            key=CONF_EXPLICIT,
            type=ConfigEntryType.BOOLEAN,
            label="Include explicit results",
            required=False,
            description="Whether or not to include explicit content results in search.",
            default_value=True,
        ),
    )


class ITunesPodcastsProvider(MusicProvider):
    """ITunesPodcastsProvider."""

    throttler: ThrottlerManager

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SEARCH, ProviderFeature.RECOMMENDATIONS}

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
                continue
            podcast = Podcast(
                name=result.track_name,
                item_id=result.feed_url,
                publisher=result.artist_name,
                provider=self.lookup_key,
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
                            type=ImageType.THUMB, path=artwork_url, provider=self.lookup_key
                        )
                    )
            podcast.metadata.images = UniqueList(image_list)
            podcast_list.append(podcast)
        return podcast_list

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get podcast."""
        parsed = await self._cache_get_podcast(prov_podcast_id)

        return parse_podcast(
            feed_url=prov_podcast_id,
            parsed_feed=parsed,
            lookup_key=self.lookup_key,
            domain=self.domain,
            instance_id=self.instance_id,
        )

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
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
                domain=self.domain,
                lookup_key=self.lookup_key,
                instance_id=self.instance_id,
            ):
                yield mass_episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get single podcast episode."""
        podcast_id, guid_or_stream_url = prov_episode_id.split(" ")
        async for mass_episode in self.get_podcast_episodes(podcast_id):
            _, _guid_or_stream_url = mass_episode.item_id.split(" ")
            # this is enough, as internal
            if guid_or_stream_url == _guid_or_stream_url:
                return mass_episode
        raise MediaNotFoundError("Episode not found")

    async def recommendations(self) -> list[RecommendationFolder]:
        """Get recommendations.

        This provider uses a list of top podcasts for the configured country.
        """
        search_results = await self._cache_get_top_podcasts()
        podcast_list = self._get_podcast_list(search_results)
        return [
            RecommendationFolder(
                item_id="itunes-top-podcasts",
                name="Trending podcasts",
                icon="mdi-trending-up",
                # translation_key=shelf.id_,
                items=UniqueList(podcast_list),
                provider=self.lookup_key,
            )
        ]

    async def _get_episode_stream_url(self, podcast_id: str, guid_or_stream_url: str) -> str | None:
        podcast = await self._cache_get_podcast(podcast_id)
        episodes = podcast.get("episodes", [])
        for cnt, episode in enumerate(episodes):
            episode_enclosures = episode.get("enclosures", [])
            if len(episode_enclosures) < 1:
                raise MediaNotFoundError
            stream_url: str | None = episode_enclosures[0].get("url", None)
            if guid_or_stream_url == episode.get("guid", stream_url):
                return stream_url
        return None

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for item."""
        podcast_id, guid_or_stream_url = item_id.split(" ")
        stream_url = await self._get_episode_stream_url(podcast_id, guid_or_stream_url)
        if stream_url is None:
            raise MediaNotFoundError
        return StreamDetails(
            provider=self.lookup_key,
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
            base_key=self.lookup_key,
            category=CACHE_CATEGORY_PODCASTS,
            default=None,
        )
        if parsed_podcast is None:
            # see music-assistant/server@6aae82e
            response = await self.mass.http_session.get(
                prov_podcast_id, headers={"User-Agent": "Mozilla/5.0"}
            )
            if response.status != 200:
                raise MediaNotFoundError
            feed_data = await response.read()
            feed_stream = BytesIO(feed_data)
            parsed_podcast = podcastparser.parse(
                prov_podcast_id, feed_stream, max_episodes=self.max_episodes
            )
            await self._cache_set_podcast(feed_url=prov_podcast_id, parsed_podcast=parsed_podcast)

        # this is a dictionary from podcastparser
        return parsed_podcast  # type: ignore[no-any-return]

    async def _cache_set_podcast(self, feed_url: str, parsed_podcast: dict[str, Any]) -> None:
        await self.mass.cache.set(
            key=feed_url,
            base_key=self.lookup_key,
            category=CACHE_CATEGORY_PODCASTS,
            data=parsed_podcast,
            expiration=60 * 60 * 24,  # 1 day
        )

    async def _cache_set_top_podcasts(self, top_podcast_helper: TopPodcastsHelper) -> None:
        await self.mass.cache.set(
            key=CACHE_KEY_TOP_PODCASTS,
            base_key=self.lookup_key,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
            data=top_podcast_helper.to_dict(),
            expiration=60 * 60 * 6,  # 6 hours
        )

    async def _cache_get_top_podcasts(self) -> list[PodcastSearchResult]:
        parsed_top_podcasts = await self.mass.cache.get(
            key=CACHE_KEY_TOP_PODCASTS,
            base_key=self.lookup_key,
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
