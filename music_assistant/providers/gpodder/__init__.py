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

import time
from collections.abc import AsyncGenerator
from datetime import datetime
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
from music_assistant_models.media_items import AudioFormat, MediaItemType, Podcast, PodcastEpisode
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.datetime import from_utc_timestamp
from music_assistant.helpers.podcast_parsers import (
    enrich_episode_chapters,
    find_episode_stream_url,
    get_cached_podcast,
    get_stream_url_and_guid_from_episode,
    parse_podcast,
    parse_podcast_episode,
    refresh_cached_podcast,
)
from music_assistant.models.music_provider import MusicProvider

from .client import EpisodeActionDelete, EpisodeActionNew, EpisodeActionPlay, GPodderClient

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
CACHE_KEY_TIMESTAMP = (
    "timestamp"  # tuple of two ints, timestamp_subscriptions and timestamp_actions
)
CACHE_KEY_FEEDS = "feeds"  # list[str] : all available rss feed urls

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

        for feed_url in subscriptions.add:
            self.feeds.add(feed_url)
        for feed_url in subscriptions.remove:
            try:
                self.feeds.remove(feed_url)
            except KeyError:
                # a podcast might have been added and removed in our absence...
                continue

        episode_actions, timestamp_action = await self._client.get_episode_actions()
        for feed_url in self.feeds:
            self.logger.debug("Adding podcast with feed %s to library", feed_url)
            # parse podcast
            try:
                parsed_podcast = await refresh_cached_podcast(
                    mass=self.mass,
                    provider_instance_id=self.instance_id,
                    feed_url=feed_url,
                    max_episodes=self.max_episodes,
                )
            except MediaNotFoundError:
                self.logger.warning(f"Was unable to obtain podcast with feed {feed_url}")
                continue

            # playlog
            # be safe, if there should be multiple episodeactions. client already sorts
            # progresses in descending order.
            _already_processed = set()
            _episode_actions = [x for x in episode_actions if x.podcast == feed_url]
            for _action in _episode_actions:
                if _action.episode not in _already_processed:
                    _already_processed.add(_action.episode)
                    # we do not have to add the progress, these would make calls twice,
                    # and we only use the object to propagate to playlog
                    self.progress_guard_timestamp = time.time()
                    _episode_ids: list[str] = []
                    if _action.guid is not None:
                        _episode_ids.append(f"{feed_url} {_action.guid}")
                    _episode_ids.append(f"{feed_url} {_action.episode}")
                    mass_episode: PodcastEpisode | None = None
                    for _episode_id in _episode_ids:
                        try:
                            mass_episode = await self.get_podcast_episode(
                                _episode_id, add_progress=False
                            )
                            break
                        except MediaNotFoundError:
                            continue
                    if mass_episode is None:
                        self.logger.debug(
                            f"Was unable to use progress for episode {_action.episode}."
                        )
                        continue
                    match _action:
                        case EpisodeActionNew():
                            await self.mass.music.mark_item_unplayed(mass_episode)
                        case EpisodeActionPlay():
                            await self.mass.music.mark_item_played(
                                mass_episode,
                                fully_played=_action.position >= _action.total,
                                seconds_played=_action.position,
                            )

            # cache
            yield parse_podcast(
                feed_url=feed_url,
                parsed_feed=parsed_podcast,
                instance_id=self.instance_id,
                domain=self.domain,
            )

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

    async def get_podcast_episodes(
        self, prov_podcast_id: str, add_progress: bool = True
    ) -> AsyncGenerator[PodcastEpisode]:
        """Get Podcast episodes. Add progress information."""
        if add_progress:
            episode_actions, timestamp = await self._client.get_episode_actions()
        else:
            episode_actions, timestamp = [], None

        podcast = await self._cache_get_podcast(prov_podcast_id)
        podcast_cover = podcast.get("cover_url")
        parsed_episodes = podcast.get("episodes", [])

        if timestamp is not None:
            self.timestamp_actions = timestamp
            await self._cache_set_timestamps()

        for cnt, parsed_episode in enumerate(parsed_episodes):
            mass_episode = parse_podcast_episode(
                episode=parsed_episode,
                prov_podcast_id=prov_podcast_id,
                episode_cnt=cnt,
                podcast_cover=podcast_cover,
                podcast_name=podcast.get("title"),
                domain=self.domain,
                instance_id=self.instance_id,
            )
            if mass_episode is None:
                # faulty episode
                continue
            try:
                stream_url, guid = get_stream_url_and_guid_from_episode(episode=parsed_episode)
            except ValueError:
                # episode enclosure or stream url missing
                continue

            for action in episode_actions:
                # we have to test both, as we are comparing to external input.
                _test = [action.guid, action.episode]
                if prov_podcast_id == action.podcast and (guid in _test or stream_url in _test):
                    self.progress_guard_timestamp = time.time()
                    if isinstance(action, EpisodeActionNew):
                        mass_episode.resume_position_ms = 0
                        mass_episode.fully_played = False

                        # propagate to playlog
                        await self.mass.music.mark_item_unplayed(
                            mass_episode,
                        )
                    elif isinstance(action, EpisodeActionPlay):
                        fully_played = action.position >= action.total
                        resume_position_s = action.position
                        mass_episode.resume_position_ms = resume_position_s * 1000
                        mass_episode.fully_played = fully_played

                        # propagate progress to playlog
                        await self.mass.music.mark_item_played(
                            mass_episode,
                            fully_played=fully_played,
                            seconds_played=resume_position_s,
                        )
                    elif isinstance(action, EpisodeActionDelete):
                        for mapping in mass_episode.provider_mappings:
                            mapping.available = False
                    break
            yield mass_episode

    async def get_podcast_episode(
        self, prov_episode_id: str, add_progress: bool = True
    ) -> PodcastEpisode:
        """Get Podcast Episode. Add progress information."""
        podcast_id, guid_or_stream_url = prov_episode_id.split(" ")
        async for mass_episode in self.get_podcast_episodes(podcast_id, add_progress=add_progress):
            _, _guid_or_stream_url = mass_episode.item_id.split(" ")
            # this is enough, as internal
            if guid_or_stream_url == _guid_or_stream_url:
                await self._enrich_episode_chapters(podcast_id, guid_or_stream_url, mass_episode)
                return mass_episode
        raise MediaNotFoundError("Did not find episode.")

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Return: finished, position_ms."""
        assert media_type == MediaType.PODCAST_EPISODE
        podcast_id, guid_or_stream_url = item_id.split(" ")
        stream_url = await self._get_episode_stream_url(podcast_id, guid_or_stream_url)
        try:
            progresses, timestamp = await self._client.get_episode_actions(
                since=self.timestamp_actions
            )
        except RuntimeError:
            self.logger.warning("Was unable to obtain progresses.")
            raise NotImplementedError  # fallback to internal position.
        for action in progresses:
            _test = [action.guid, action.episode]
            # progress is external, compare guid and stream_url
            if action.podcast == podcast_id and (
                guid_or_stream_url in _test or stream_url in _test
            ):
                dt_timestamp: datetime | None = None
                if timestamp is not None:
                    self.timestamp_actions = timestamp
                    await self._cache_set_timestamps()
                    dt_timestamp = from_utc_timestamp(timestamp)
                if isinstance(action, EpisodeActionNew | EpisodeActionDelete):
                    # no progress, it might have been actively reset
                    # in case of delete, we start from start.
                    return False, 0, None
                _progress = (action.position >= action.total, max(action.position * 1000, 0))
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
        stream_url = await self._get_episode_stream_url(podcast_id, guid_or_stream_url)
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
            self.logger.debug(f"Updated progress to {position / duration * 100:.2f}%")
        except RuntimeError as exc:
            self.logger.debug(exc)
            self.logger.debug("Failed to update progress.")

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

    async def _enrich_episode_chapters(
        self, prov_podcast_id: str, guid_or_stream_url: str, mass_episode: PodcastEpisode
    ) -> None:
        """
        Attach external ``podcast:chapters`` JSON to a resolved single episode, if any.

        :param prov_podcast_id: Provider podcast id the episode belongs to.
        :param guid_or_stream_url: Episode identifier used to locate the raw parsed episode.
        :param mass_episode: The episode to enrich in place; left untouched on any failure.
        """
        if mass_episode.metadata.chapters:
            return
        podcast = await self._cache_get_podcast(prov_podcast_id)
        for episode in podcast.get("episodes", []):
            try:
                stream_url, guid = get_stream_url_and_guid_from_episode(episode=episode)
            except ValueError:
                continue
            if guid_or_stream_url in (guid, stream_url):
                await enrich_episode_chapters(
                    session=self.mass.http_session,
                    chapters_json_url=episode.get("chapters_json_url"),
                    mass_episode=mass_episode,
                )
                return

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
