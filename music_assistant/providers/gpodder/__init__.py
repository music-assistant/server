"""
gPodder provider for Music Assistant.

Tested against opodsync, https://github.com/kd2org/opodsync
and nextcloud-gpodder, https://github.com/thrillfall/nextcloud-gpodder
gpodder.net is not supported due to responsiveness/ frequent downtimes of domain.

Note:
    - it can happen, that we have the guid and use that for identification, but the sync state
      provider, eg. opodsync might use only the stream url. So always make sure, to compare both
      when relying on an external service
    - The service calls have a timestamp (int, unix epoch s), which give the changes since then.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator
from datetime import datetime
from itertools import islice
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    AudioFormat,
    MediaItemTranscriptCue,
    MediaItemType,
    Podcast,
    PodcastEpisode,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.datetime import from_utc_timestamp
from music_assistant.helpers.podcast_parsers import (
    enrich_episode_chapters,
    find_episode_stream_url,
    find_episode_transcripts,
    get_cached_podcast,
    get_episode_transcript,
    parse_podcast,
    parse_podcast_episode,
    refresh_cached_podcast,
)
from music_assistant.models.music_provider import MusicProvider

from .client import (
    EpisodeAction,
    EpisodeActionDelete,
    EpisodeActionNew,
    EpisodeActionPlay,
    GPodderClient,
)
from .helpers import (
    ActionIndex,
    action_time,
    apply_action,
    find_action,
    index_actions,
    iter_episodes,
)

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Config for "classic" gpodder api
CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEVICE_ID = "device_id"

# Config for nextcloud
CONF_TOKEN_NC = "token"
CONF_URL_NC = "url_nc"

# General config
CONF_VERIFY_SSL = "verify_ssl"
CONF_MAX_NUM_EPISODES = "max_num_episodes"


# category 0 holds the individual parsed podcasts, see CACHE_CATEGORY_PODCAST_FEED
CACHE_CATEGORY_OTHER = 1
# tuple of two ints, timestamp_subscriptions and timestamp_actions; the actions timestamp marks
# what the sync wrote to the playlog, the previous key ("timestamp") was also moved by listings
CACHE_KEY_TIMESTAMP = "sync_timestamps"
CACHE_KEY_FEEDS = "feeds"  # list[str] : all available rss feed urls

# feeds refreshed at the same time during a library sync
FEED_REFRESH_CONCURRENCY = 5

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.BROWSE,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return GPodder(mass, manifest, config, SUPPORTED_FEATURES)


class GPodder(MusicProvider):
    """gPodder MusicProvider."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for the gPodder provider.

        The server/account connection (gpodder API or Nextcloud) is set up by the interactive
        setup flow (see ``setup_flow.py``); only the max-episodes limit is configured here.
        """
        return (
            ConfigEntry(
                key=CONF_MAX_NUM_EPISODES,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=0,
            ),
        )

    async def handle_async_init(self) -> None:
        """Pass config values to client and initialize."""
        base_url = str(self.get_setup_value(CONF_URL))
        _username = self.get_setup_value(CONF_USERNAME)
        _password = self.get_setup_value(CONF_PASSWORD)
        _device_id = self.get_setup_value(CONF_DEVICE_ID)
        nc_url = str(self.get_setup_value(CONF_URL_NC))
        nc_token = self.get_setup_value(CONF_TOKEN_NC)
        verify_ssl = bool(self.get_setup_value(CONF_VERIFY_SSL, True))

        self.max_episodes = cast("int", self.config.get_value(CONF_MAX_NUM_EPISODES, 0))

        self._client = GPodderClient(
            session=self.mass.http_session, logger=self.logger, verify_ssl=verify_ssl
        )

        if nc_token is not None:
            assert nc_url is not None
            self._client.init_nc(base_url=nc_url, nc_token=str(nc_token))
        else:
            if _username is None or _password is None or _device_id is None:
                raise LoginFailed("Must provide username, password and device_id.")
            username = str(_username)
            password = str(_password)
            device_id = str(_device_id)

            if base_url.rstrip("/") == "https://gpodder.net":
                raise LoginFailed("Do not use gpodder.net. See docs for explanation.")
            try:
                await self._client.init_gpodder(
                    username=username, password=password, base_url=base_url, device=device_id
                )
            except RuntimeError as exc:
                raise LoginFailed("Login failed.") from exc

        timestamps = await self.mass.cache.get(
            key=CACHE_KEY_TIMESTAMP,
            provider=self.instance_id,
            category=CACHE_CATEGORY_OTHER,
            default=None,
        )
        if timestamps is None:
            self.timestamp_subscriptions: int = 0
            self.timestamp_actions: int = 0
        else:
            self.timestamp_subscriptions, self.timestamp_actions = timestamps

        self.logger.debug(
            "Our timestamps are (subscriptions, actions)  (%s, %s)",
            self.timestamp_subscriptions,
            self.timestamp_actions,
        )

        feeds = await self.mass.cache.get(
            key=CACHE_KEY_FEEDS,
            provider=self.instance_id,
            category=CACHE_CATEGORY_OTHER,
            default=None,
        )
        if feeds is None:
            self.feeds: set[str] = set()
        else:
            self.feeds = set(feeds)  # feeds is a list here

        # we are syncing the playlog, but not event based. A simple check in on_played,
        # should be sufficient
        self.progress_guard_timestamp = 0.0

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        # While the streams are remote, the user controls what is added.
        return False

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve library/subscribed podcasts from the provider."""
        try:
            subscriptions = await self._client.get_subscriptions()
        except RuntimeError:
            raise ResourceTemporarilyUnavailable(backoff_time=30)
        if subscriptions is None:
            return

        feeds = self.feeds | set(subscriptions.add)
        # a podcast might have been added and removed in our absence...
        feeds.difference_update(subscriptions.remove)
        episode_actions, timestamp_action = await self._client.get_episode_actions(
            since=self.timestamp_actions
        )
        if self.timestamp_actions and (new_feeds := feeds - self.feeds):
            # a feed not in the last completed sync needs its whole history
            history, _ = await self._client.get_episode_actions()
            episode_actions = episode_actions + [x for x in history if x.podcast in new_feeds]
        actions_by_podcast = index_actions(episode_actions)
        async for feed_url, parsed_podcast in self._refresh_feeds(list(feeds)):
            self.logger.debug("Adding podcast with feed %s to library", feed_url)

            # playlog
            actions = actions_by_podcast.get(feed_url, {})
            for position, parsed_episode, stream_url, guid in iter_episodes(parsed_podcast):
                action = find_action(actions, guid, stream_url)
                if not isinstance(action, EpisodeActionNew | EpisodeActionPlay):
                    continue
                mass_episode = parse_podcast_episode(
                    episode=parsed_episode,
                    prov_podcast_id=feed_url,
                    position=position,
                    podcast_cover=parsed_podcast.get("cover_url"),
                    podcast_name=parsed_podcast.get("title"),
                    domain=self.domain,
                    instance_id=self.instance_id,
                )
                if mass_episode is not None:
                    await self._write_playlog(mass_episode, action)

            yield parse_podcast(
                feed_url=feed_url,
                parsed_feed=parsed_podcast,
                instance_id=self.instance_id,
                domain=self.domain,
            )

        self.feeds = feeds
        self.timestamp_subscriptions = subscriptions.timestamp
        if timestamp_action is not None:
            self.timestamp_actions = timestamp_action
        await self._cache_set_timestamps()
        await self._cache_set_feeds()

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get Podcast."""
        parsed_podcast = await self._cache_get_podcast(prov_podcast_id)

        return parse_podcast(
            feed_url=prov_podcast_id,
            parsed_feed=parsed_podcast,
            instance_id=self.instance_id,
            domain=self.domain,
        )

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get Podcast episodes, with the progress gPodder got since the last sync."""
        actions, synced = await self._get_unsynced_actions(prov_podcast_id)
        podcast = await self._cache_get_podcast(prov_podcast_id)
        for position, parsed_episode, stream_url, guid in iter_episodes(podcast):
            mass_episode = parse_podcast_episode(
                episode=parsed_episode,
                prov_podcast_id=prov_podcast_id,
                position=position,
                podcast_cover=podcast.get("cover_url"),
                podcast_name=podcast.get("title"),
                domain=self.domain,
                instance_id=self.instance_id,
            )
            if mass_episode is None:
                continue
            if action := find_action(actions, guid, stream_url):
                apply_action(mass_episode, action)
                if synced:
                    await self._write_playlog(mass_episode, action)
            yield mass_episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get Podcast Episode, with the progress gPodder got since the last sync."""
        podcast_id, guid_or_stream_url = prov_episode_id.split(" ")
        podcast = await self._cache_get_podcast(podcast_id)
        for position, parsed_episode, stream_url, guid in iter_episodes(podcast):
            # the episode part of the item id, see parse_podcast_episode
            if guid_or_stream_url != (guid if guid is not None else stream_url):
                continue
            mass_episode = parse_podcast_episode(
                episode=parsed_episode,
                prov_podcast_id=podcast_id,
                position=position,
                podcast_cover=podcast.get("cover_url"),
                podcast_name=podcast.get("title"),
                domain=self.domain,
                instance_id=self.instance_id,
            )
            if mass_episode is None:
                break
            actions, synced = await self._get_unsynced_actions(podcast_id)
            if action := find_action(actions, guid, stream_url):
                apply_action(mass_episode, action)
                if synced:
                    await self._write_playlog(mass_episode, action)
            await enrich_episode_chapters(
                session=self.mass.http_session,
                chapters_json_url=parsed_episode.get("chapters_json_url"),
                mass_episode=mass_episode,
            )
            return mass_episode
        raise MediaNotFoundError("Did not find episode.")

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Return: finished, position_ms."""
        assert media_type == MediaType.PODCAST_EPISODE
        podcast_id, guid_or_stream_url = item_id.split(" ")
        try:
            # only the library sync moves this timestamp, as it writes the actions to the playlog
            progresses, _ = await self._client.get_episode_actions(since=self.timestamp_actions)
        except RuntimeError:
            self.logger.warning("Was unable to obtain progresses.")
            raise NotImplementedError  # fallback to internal position.
        action: EpisodeAction | None = None
        if actions := index_actions(progresses).get(podcast_id):
            # progress is external, compare guid and stream_url
            stream_url = await self._get_episode_stream_url(podcast_id, guid_or_stream_url)
            action = find_action(actions, guid_or_stream_url, stream_url)
        # the action's own time, so core still prefers a newer playlog entry
        dt_timestamp = (
            from_utc_timestamp(seconds) if action and (seconds := action_time(action)) else None
        )
        if isinstance(action, EpisodeActionNew):
            # actively reset in another client, which wins over an older playlog entry
            return False, 0, dt_timestamp
        if isinstance(action, EpisodeActionDelete):
            # a deleted download says nothing about progress, the playlog still decides
            return False, 0, None
        if isinstance(action, EpisodeActionPlay):
            self.logger.debug("Found an updated external resume position.")
            return action.position >= action.total, max(action.position * 1000, 0), dt_timestamp
        self.logger.debug("Did not find an updated resume position, falling back to stored.")
        # If we did not find a resume position, nothing changed since our last timestamp
        # we raise NotImplementedError, such that MA falls back to the already stored
        # resume_position in its playlog.
        raise NotImplementedError

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Update progress."""
        if media_item is None or not isinstance(media_item, PodcastEpisode):
            return
        if media_type != MediaType.PODCAST_EPISODE:
            return
        if time.time() - self.progress_guard_timestamp <= 5:
            return
        podcast_id, guid_or_stream_url = prov_item_id.split(" ")
        stream_url = next(
            (x.url for x in media_item.provider_mappings if x.item_id == prov_item_id and x.url),
            None,
        ) or await self._get_episode_stream_url(podcast_id, guid_or_stream_url)
        assert stream_url is not None
        duration = media_item.duration
        try:
            await self._client.update_progress(
                podcast_id=podcast_id,
                episode_id=stream_url,
                guid=guid_or_stream_url,
                position_s=position,
                duration_s=duration,
            )
            self.logger.debug("Updated progress to %s of %s s", position, duration)
        except RuntimeError as exc:
            self.logger.debug(exc)
            self.logger.debug("Failed to update progress.")

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

    async def _refresh_feeds(
        self, feed_urls: list[str]
    ) -> AsyncGenerator[tuple[str, dict[str, Any]]]:
        """Refresh the cached feeds a few at a time, yielding them in the given order."""

        def refresh(feed_url: str) -> tuple[str, asyncio.Task[dict[str, Any]]]:
            # tracked by mass, so a shutdown cancels it; its errors are handled below
            return feed_url, self.mass.create_task(
                refresh_cached_podcast(
                    mass=self.mass,
                    provider_instance_id=self.instance_id,
                    feed_url=feed_url,
                    max_episodes=self.max_episodes,
                ),
                task_name="gpodder_feed_refresh",
                log_exceptions=False,
            )

        # only a few refreshed feeds are held at a time, however large the library
        pending = iter(feed_urls)
        refreshing = deque(refresh(x) for x in islice(pending, FEED_REFRESH_CONCURRENCY))
        try:
            while refreshing:
                feed_url, task = refreshing.popleft()
                parsed_podcast: dict[str, Any] | None = None
                try:
                    parsed_podcast = await task
                except MediaNotFoundError as err:
                    self.report_skipped_sync_item(MediaType.PODCAST, feed_url, err)
                if (next_feed_url := next(pending, None)) is not None:
                    refreshing.append(refresh(next_feed_url))
                if parsed_podcast is not None:
                    yield feed_url, parsed_podcast
        finally:
            for _, task in refreshing:
                task.cancel()

    async def _get_unsynced_actions(self, podcast_id: str) -> tuple[ActionIndex, bool]:
        """Return the podcast's actions the playlog lacks, and whether the sync wrote the rest."""
        # without a completed sync of this feed the whole history is needed, but it is too
        # large to write to the playlog outside of the sync
        synced = bool(self.timestamp_actions) and podcast_id in self.feeds
        episode_actions, _ = await self._client.get_episode_actions(
            since=self.timestamp_actions if synced else 0
        )
        return index_actions(episode_actions).get(podcast_id, {}), synced

    async def _write_playlog(self, mass_episode: PodcastEpisode, action: EpisodeAction) -> None:
        # the playlog writes must not be reported back to gPodder
        self.progress_guard_timestamp = time.time()
        if isinstance(action, EpisodeActionNew):
            await self.mass.music.mark_item_unplayed(
                mass_episode, provider_instance_id=self.instance_id
            )
        elif isinstance(action, EpisodeActionPlay):
            await self.mass.music.mark_item_played(
                mass_episode,
                fully_played=action.position >= action.total,
                seconds_played=action.position,
                user_initiated=False,
                provider_instance_id=self.instance_id,
            )

    async def _get_episode_stream_url(self, podcast_id: str, guid_or_stream_url: str) -> str | None:
        parsed_podcast = await self._cache_get_podcast(podcast_id)
        return find_episode_stream_url(
            parsed_feed=parsed_podcast, guid_or_stream_url=guid_or_stream_url
        )

    async def _cache_get_podcast(self, prov_podcast_id: str) -> dict[str, Any]:
        # raises MediaNotFoundError when the feed is gone
        return await get_cached_podcast(
            mass=self.mass,
            provider_instance_id=self.instance_id,
            feed_url=prov_podcast_id,
            max_episodes=self.max_episodes,
        )

    async def _cache_set_timestamps(self) -> None:
        # seven days default
        await self.mass.cache.set(
            key=CACHE_KEY_TIMESTAMP,
            provider=self.instance_id,
            category=CACHE_CATEGORY_OTHER,
            data=[self.timestamp_subscriptions, self.timestamp_actions],
        )

    async def _cache_set_feeds(self) -> None:
        # seven days default
        await self.mass.cache.set(
            key=CACHE_KEY_FEEDS,
            provider=self.instance_id,
            category=CACHE_CATEGORY_OTHER,
            data=list(self.feeds),
        )
