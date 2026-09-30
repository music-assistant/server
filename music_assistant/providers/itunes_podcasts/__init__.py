"""iTunes Podcast search support for MusicAssistant."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator
from contextlib import suppress
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
    MediaItemTranscriptCue,
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
    find_episode_stream_url,
    find_episode_transcripts,
    get_cached_podcast,
    get_episode_positions,
    get_episode_transcript,
    parse_podcast,
    parse_podcast_episode,
    refresh_cached_podcast,
)
from music_assistant.helpers.throttle_retry import (
    RequestPriority,
    ThrottlerManager,
    set_request_priority,
    throttle_with_retries,
)
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

# category 0 holds the parsed podcast feeds, see CACHE_CATEGORY_PODCAST_FEED
CACHE_CATEGORY_RECOMMENDATIONS = 1
CACHE_CATEGORY_FEED_LOOKUP = 2
CACHE_KEY_TOP_PODCASTS = "top-podcasts-full"
CACHE_KEY_LIBRARY_RECOMMENDATIONS = "library-recommendations"
RECOMMENDATION_ROW_TOP_PODCASTS = "itunes-top-podcasts"
RECOMMENDATION_ROW_FOR_YOU = "itunes-library-recommendations"
RECOMMENDATION_ROW_SIZE = 15

# iTunes root genre "Podcasts", present on every show and useless for similarity
ROOT_GENRE_ID = "26"
# one request per genre
MAX_SEED_GENRES = 4
GENRE_TOP_PODCASTS_LIMIT = 100
# resolving a library podcast costs one search request (no lookup by feed url exists).
# Only this many run while the row is requested, the rest fill the cache in a
# background task: a large library would otherwise hit the throttle and the row's
# timeout. Only matters when the resolve cache is empty (upgrade, cache clear).
MAX_INLINE_RESOLVES = 5
# short, so podcasts resolved in the background are picked up soon
LIBRARY_RECOMMENDATIONS_CACHE_EXPIRATION = 60 * 60
# the v2 feed returns at most 100 entries
TOP_PODCASTS_LIMIT = 100
TOP_PODCASTS_CACHE_EXPIRATION = 60 * 60 * 24
# the trending row shows every n-th top podcast (1, 8, 15, ...) and moves to the next
# offset every rotation, so all of them are shown once per cache lifetime
TOP_PODCASTS_NUM_PAGES = math.ceil(TOP_PODCASTS_LIMIT / RECOMMENDATION_ROW_SIZE)
TOP_PODCASTS_ROTATION = TOP_PODCASTS_CACHE_EXPIRATION // TOP_PODCASTS_NUM_PAGES

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
    _resolve_task: asyncio.Task[None] | None = None

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

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

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._resolve_task and not self._resolve_task.done():
            self._resolve_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._resolve_task

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
        results = await self._perform_search(url, params) or []
        result.podcasts = self._get_podcast_list(results)

        return result

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        return [
            RecommendationFolder(
                item_id=RECOMMENDATION_ROW_TOP_PODCASTS,
                name="Trending Podcasts",
                icon="mdi-trending-up",
                translation_key="trending_podcasts",
                provider=self.instance_id,
            ),
            RecommendationFolder(
                item_id=RECOMMENDATION_ROW_FOR_YOU,
                name="Podcasts you might like",
                icon="mdi-podcast",
                translation_key="podcasts_you_might_like",
                provider=self.instance_id,
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        if item_id == RECOMMENDATION_ROW_TOP_PODCASTS:
            search_results = await self._get_top_podcasts_page()
        elif item_id == RECOMMENDATION_ROW_FOR_YOU:
            search_results = await self._get_library_recommendations()
        else:
            return UniqueList()
        return UniqueList(self._get_podcast_list(search_results))

    async def get_podcast_episode_transcript(
        self, prov_episode_id: str
    ) -> tuple[str | None, list[MediaItemTranscriptCue] | None]:
        """Get the transcript for a podcast episode."""
        podcast_id, guid_or_stream_url = prov_episode_id.split(" ")
        podcast = await self._cache_get_podcast(podcast_id)
        return await get_episode_transcript(
            mass=self.mass,
            provider_instance_id=self.instance_id,
            transcripts=find_episode_transcripts(
                parsed_feed=podcast, guid_or_stream_url=guid_or_stream_url
            ),
        )

    @throttle_with_retries
    async def _perform_search(
        self, url: str, params: dict[str, str | int]
    ) -> list[PodcastSearchResult] | None:
        """Run an iTunes search/lookup request, None on failure (so it is not cached)."""
        async with self.mass.http_session.get(url, params=params) as response:
            if response.status != 200:
                self.logger.debug("iTunes request failed with status %s", response.status)
                return None
            json_response = await response.read()
        if not json_response:
            return None
        return ITunesSearchResults.from_json(json_response).results

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
                parsed_podcast = await refresh_cached_podcast(
                    mass=self.mass,
                    provider_instance_id=self.instance_id,
                    feed_url=feed_url,
                    max_episodes=self.max_episodes,
                    cache_expiration=self._get_cache_expiration(),
                )
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
        positions = get_episode_positions(episodes)
        for position, episode in zip(positions, episodes, strict=True):
            if mass_episode := parse_podcast_episode(
                episode=episode,
                prov_podcast_id=prov_podcast_id,
                position=position,
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
        episodes = podcast.get("episodes", [])
        positions = get_episode_positions(episodes)
        for position, episode in zip(positions, episodes, strict=True):
            mass_episode = parse_podcast_episode(
                episode=episode,
                prov_podcast_id=podcast_id,
                position=position,
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
        parsed_podcast = await self._cache_get_podcast(podcast_id)
        return find_episode_stream_url(
            parsed_feed=parsed_podcast, guid_or_stream_url=guid_or_stream_url
        )

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

    async def _cache_get_podcast(self, prov_podcast_id: str) -> dict[str, Any]:
        # raises MediaNotFoundError if the feed is gone or invalid
        return await get_cached_podcast(
            mass=self.mass,
            provider_instance_id=self.instance_id,
            feed_url=prov_podcast_id,
            max_episodes=self.max_episodes,
            cache_expiration=self._get_cache_expiration(),
        )

    def _get_cache_expiration(self) -> int:
        # Cache slightly longer than the effective sync interval to avoid fetching
        # the same podcast feed repeatedly during recurring library sync.
        schedule = self.mass.music.get_provider_sync_schedule(self.instance_id, MediaType.PODCAST)
        library_sync_enabled = bool(self.config.get_value("library_sync_podcasts"))
        if not library_sync_enabled or schedule is None or not schedule.enabled:
            return 60 * 60 * 12  # 12h
        if schedule.type == TaskScheduleType.HOURLY and schedule.every is not None:
            return schedule.every * 60 * 60 + 600  # 10 minutes extra cache
        if schedule.type == TaskScheduleType.DAILY and schedule.every is not None:
            return schedule.every * 24 * 60 * 60 + 600
        return 60 * 60 * 12  # 12h

    async def _cache_set_top_podcasts(self, top_podcast_helper: TopPodcastsHelper) -> None:
        await self.mass.cache.set(
            key=f"{CACHE_KEY_TOP_PODCASTS}-{self.config.get_value(CONF_LOCALE)}",
            provider=self.instance_id,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
            data=top_podcast_helper.to_dict(),
            expiration=TOP_PODCASTS_CACHE_EXPIRATION,
        )

    async def _cache_get_top_podcasts(self) -> list[PodcastSearchResult]:
        # cached unfiltered, so the library and explicit filters take effect right away
        parsed_top_podcasts = await self.mass.cache.get(
            key=f"{CACHE_KEY_TOP_PODCASTS}-{self.config.get_value(CONF_LOCALE)}",
            provider=self.instance_id,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
        )
        if parsed_top_podcasts is not None:
            helper = TopPodcastsHelper.from_dict(parsed_top_podcasts)
            return helper.top_podcasts

        country = str(self.config.get_value(CONF_LOCALE))
        itunes_ids = await self._get_top_podcast_ids(country)
        if itunes_ids is None:
            return []
        top_podcasts = await self._get_podcast_search_results_from_itunes_ids(itunes_ids)
        if top_podcasts is None:
            # failed, not cached so the next request retries
            return []
        helper = TopPodcastsHelper(top_podcasts=top_podcasts)
        await self._cache_set_top_podcasts(top_podcast_helper=helper)
        return helper.top_podcasts

    @throttle_with_retries
    async def _get_top_podcast_ids(self, country: str) -> list[int] | None:
        """Get the iTunes ids of the top podcasts in rank order, None on failure."""
        # see https://rss.marketingtools.apple.com/
        url = (
            f"https://rss.marketingtools.apple.com/api/v2/{country}/podcasts/top/"
            f"{TOP_PODCASTS_LIMIT}/podcasts.json"
        )
        async with self.mass.http_session.get(url) as response:
            if response.status != 200:
                return None
            json_response = await response.read()
        if not json_response:
            return None
        top_podcasts_response = TopPodcastsResponse.from_json(json_response)
        if top_podcasts_response.feed is None:
            return None
        itunes_ids: list[int] = []
        for top_podcast in top_podcasts_response.feed.results:
            try:
                itunes_ids.append(int(top_podcast.id_))
            except ValueError:
                continue
        return itunes_ids

    async def _get_top_podcasts_page(self) -> list[PodcastSearchResult]:
        """Get the current page of the top podcasts, without podcasts of the library."""
        library_feeds = set(await self._get_library_feeds())
        include_explicit = bool(self.config.get_value(CONF_EXPLICIT))
        top_podcasts = [
            podcast
            for podcast in await self._cache_get_top_podcasts()
            if self._is_recommendable(podcast, library_feeds, include_explicit)
        ]
        # clock based, so every client shows the same page and a restart does not reset it
        page = int(time.time() // TOP_PODCASTS_ROTATION) % TOP_PODCASTS_NUM_PAGES
        return top_podcasts[page::TOP_PODCASTS_NUM_PAGES]

    async def _get_library_feeds(self) -> dict[str, str]:
        """Return feed url -> name of this provider's library podcasts."""
        feeds: dict[str, str] = {}
        async for podcast in self.mass.music.podcasts.iter_library_items_by_prov_id(
            self.instance_id
        ):
            for mapping in podcast.provider_mappings:
                if mapping.provider_instance == self.instance_id:
                    feeds[self._normalize_feed_url(mapping.item_id)] = podcast.name
        return feeds

    @staticmethod
    def _normalize_feed_url(url: str) -> str:
        url = url.strip().lower()
        for prefix in ("https://", "http://"):
            url = url.removeprefix(prefix)
        url = url.removeprefix("www.")
        return url.rstrip("/")

    def _is_recommendable(
        self, podcast: PodcastSearchResult, library_feeds: set[str], include_explicit: bool
    ) -> bool:
        if podcast.feed_url is None:
            return False
        if self._normalize_feed_url(podcast.feed_url) in library_feeds:
            return False
        return include_explicit or not podcast.is_explicit

    async def _resolve_library_podcast(
        self, feed_url: str, name: str, cached_only: bool = False
    ) -> tuple[bool, PodcastSearchResult | None]:
        """
        Find the iTunes entry of a library podcast.

        :param cached_only: Only look at the cache, do not send a request.
        :return: (resolved, entry). Not resolved: not cached yet or the request failed.
            No entry: not listed in iTunes.
        """
        cached = await self.mass.cache.get(
            key=feed_url, provider=self.instance_id, category=CACHE_CATEGORY_FEED_LOOKUP
        )
        if cached is not None:
            # an empty dict marks a known miss
            return True, PodcastSearchResult.from_dict(cached) if cached else None
        if cached_only:
            return False, None

        # Apple has no lookup by feed url: search the title, then match the feed url
        params: dict[str, str | int] = {
            "media": "podcast",
            "entity": "podcast",
            "country": str(self.config.get_value(CONF_LOCALE)),
            "limit": 25,
            "term": name,
        }
        results = await self._perform_search("https://itunes.apple.com/search?", params)
        if results is None:
            return False, None
        match = next(
            (r for r in results if r.feed_url and self._normalize_feed_url(r.feed_url) == feed_url),
            None,
        )
        await self.mass.cache.set(
            key=feed_url,
            provider=self.instance_id,
            category=CACHE_CATEGORY_FEED_LOOKUP,
            data=match.to_dict() if match else {},
            # retry misses sooner, the show may get listed later on
            expiration=60 * 60 * 24 * (30 if match else 7),
        )
        return True, match

    async def _resolve_library_podcasts(self, feeds: dict[str, str]) -> None:
        # background task, only fills the cache. low priority keeps the throttle
        # free for the row request and searches
        set_request_priority(RequestPriority.LOW)
        for feed_url, name in feeds.items():
            await self._resolve_library_podcast(feed_url, name)

    @use_cache(3600 * 12, cache_none=False)
    @throttle_with_retries
    async def _get_genre_top_podcast_ids(self, country: str, genre_id: str) -> list[int] | None:
        """Get the iTunes ids of the top podcasts of a genre in rank order, None on failure."""
        # legacy feed, the v2 feed has no genre filter. country is an argument (not
        # read from the config) so it is part of the cache key
        url = (
            f"https://itunes.apple.com/{country}/rss/toppodcasts/"
            f"limit={GENRE_TOP_PODCASTS_LIMIT}/genre={genre_id}/json"
        )
        async with self.mass.http_session.get(url) as response:
            if response.status != 200:
                return None
            body = await response.read()
        try:
            # served as text/javascript, so parse the body ourselves
            data = json.loads(body)
        except ValueError:
            return None
        entries = data.get("feed", {}).get("entry", [])
        if isinstance(entries, dict):
            # a single entry is not wrapped in a list
            entries = [entries]
        ids: list[int] = []
        for entry in entries:
            try:
                ids.append(int(entry["id"]["attributes"]["im:id"]))
            except KeyError, TypeError, ValueError:
                continue
        return ids

    async def _get_podcast_search_results_from_itunes_ids(
        self, itunes_ids: list[int]
    ) -> list[PodcastSearchResult] | None:
        """Lookup up to 100 iTunes ids in one request, keeping their order. None on failure."""
        params: dict[str, str | int] = {
            "id": ",".join(str(i) for i in itunes_ids),
            "entity": "podcast",
            "country": str(self.config.get_value(CONF_LOCALE)),
        }
        results = await self._perform_search("https://itunes.apple.com/lookup?", params)
        if results is None:
            return None
        by_id = {r.collection_id: r for r in results}
        return [by_id[i] for i in itunes_ids if i in by_id]

    async def _get_library_recommendations(self) -> list[PodcastSearchResult]:
        """Recommend podcasts of the genres dominating the library."""
        feeds = await self._get_library_feeds()
        if not feeds:
            return []
        country = str(self.config.get_value(CONF_LOCALE))
        include_explicit = bool(self.config.get_value(CONF_EXPLICIT))

        # recompute whenever the library or the relevant config changes
        fingerprint = hashlib.sha1(
            "\n".join([country, str(include_explicit), *sorted(feeds)]).encode()
        ).hexdigest()
        cached = await self.mass.cache.get(
            key=CACHE_KEY_LIBRARY_RECOMMENDATIONS,
            provider=self.instance_id,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
            checksum=fingerprint,
        )
        if cached is not None:
            return TopPodcastsHelper.from_dict(cached).top_podcasts

        seeds: list[PodcastSearchResult] = []
        unresolved: dict[str, str] = {}
        for feed_url, name in feeds.items():
            resolved, seed = await self._resolve_library_podcast(feed_url, name, cached_only=True)
            if not resolved:
                unresolved[feed_url] = name
            elif seed is not None:
                seeds.append(seed)
        # a few inline, the rest in the background: the throttle would make a large
        # library block the row for minutes
        for feed_url, name in list(unresolved.items())[:MAX_INLINE_RESOLVES]:
            _, seed = await self._resolve_library_podcast(feed_url, name)
            if seed is not None:
                seeds.append(seed)
        if len(unresolved) > MAX_INLINE_RESOLVES:
            self._resolve_task = self.mass.create_task(
                self._resolve_library_podcasts(unresolved),
                task_id=f"itunes_podcasts_resolve_library_{self.instance_id}",
                task_name="itunes_podcasts_resolve_library",
            )
        if not seeds:
            return []

        # only the primary (first) genre of a show counts: parent genres like "News"
        # would otherwise double count and crowd out other interests
        genre_weights: Counter[str] = Counter()
        for seed in seeds:
            specific = [g for g in seed.genre_ids if g != ROOT_GENRE_ID]
            if specific:
                genre_weights[specific[0]] += 1

        # score by genre weight and rank, shows ranking in several genres win
        scores: dict[int, float] = defaultdict(float)
        for genre_id, weight in genre_weights.most_common(MAX_SEED_GENRES):
            for rank, itunes_id in enumerate(
                await self._get_genre_top_podcast_ids(country, genre_id) or []
            ):
                scores[itunes_id] += weight * (1 - rank / GENRE_TOP_PODCASTS_LIMIT)
        if not scores:
            return []

        # some headroom for items dropped by the filters below
        ranked = sorted(scores, key=scores.__getitem__, reverse=True)
        candidates = await self._get_podcast_search_results_from_itunes_ids(
            ranked[: RECOMMENDATION_ROW_SIZE * 2]
        )
        if candidates is None:
            return []
        recommendations = [
            candidate
            for candidate in candidates
            if self._is_recommendable(candidate, set(feeds), include_explicit)
        ][:RECOMMENDATION_ROW_SIZE]
        await self.mass.cache.set(
            key=CACHE_KEY_LIBRARY_RECOMMENDATIONS,
            provider=self.instance_id,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
            data=TopPodcastsHelper(top_podcasts=recommendations).to_dict(),
            checksum=fingerprint,
            expiration=LIBRARY_RECOMMENDATIONS_CACHE_EXPIRATION,
        )
        return recommendations
