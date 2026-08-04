"""
Overcast provider for Music Assistant.

Imports podcast subscriptions and playback progress from an Overcast account
using the account's extended OPML export. Synchronization is strictly one-way
(Overcast -> Music Assistant): Overcast has no ingest API, so ``on_played`` is
deliberately not implemented and no state is ever written back.

The OPML export endpoint is rate limited by Overcast, therefore the export is
cached aggressively and refreshed in the background, while the RSS feeds it
refers to are fetched directly from the podcasts' own servers.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import AudioFormat, Podcast, PodcastEpisode
from music_assistant_models.streamdetails import StreamDetails
from yarl import URL

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER, CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.helpers.datetime import from_iso_string
from music_assistant.helpers.podcast_parsers import (
    enrich_episode_chapters,
    find_episode_stream_url,
    get_cached_podcast,
    get_stream_url_and_guid_from_episode,
    parse_podcast,
    parse_podcast_episode,
    refresh_cached_podcast,
)
from music_assistant.helpers.throttle_retry import parse_retry_after
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    AUTH_REJECT_STATUSES,
    BASE_URL,
    CACHE_CATEGORY_OPML,
    CACHE_KEY_LAST_APPLIED,
    CONF_MAX_NUM_EPISODES,
    CONF_SESSION_COOKIE,
    LOGIN_URL,
    OPML_CACHE_EXPIRATION,
    OPML_EXPORT_URL,
    PODCASTS_URL,
    RATE_LIMIT_FALLBACK_BACKOFF,
    SESSION_COOKIE_NAME,
)
from .helpers import OvercastSubscription, match_episode_state, parse_extended_opml

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from datetime import datetime


class OvercastProvider(MusicProvider):
    """Provider that imports podcast subscriptions from an Overcast account."""

    http_session: aiohttp.ClientSession
    max_episodes: int
    # newest playback state applied per feed url: a feed that could not be retrieved
    # keeps its own watermark, so its states are still applied once it recovers
    _feed_watermarks: dict[str, datetime]
    # the parsed export, kept alongside the raw text it was parsed from so a
    # refreshed export is re-parsed while repeated lookups are not
    _opml_cache: tuple[str, dict[str, OvercastSubscription]] | None = None
    # monotonic deadline set from a 429's Retry-After, see _rate_limit_remaining
    _rate_limited_until: float | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for the Overcast provider.

        The account credentials are collected by the interactive setup flow
        (see ``setup_flow.py``); only the max-episodes limit is configured here.
        """
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_MAX_NUM_EPISODES,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=0,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.max_episodes = cast("int", self.config.get_value(CONF_MAX_NUM_EPISODES, 0))
        # Dedicated session with its own cookie jar to support multi-instance
        # (each instance has its own Overcast login cookie)
        self.http_session = create_clientsession(self.mass, cookie_jar=aiohttp.CookieJar())
        stored_cookie = self.get_setup_value(CONF_SESSION_COOKIE)
        cookie_valid = False
        if isinstance(stored_cookie, str) and stored_cookie:
            self.http_session.cookie_jar.update_cookies(
                {SESSION_COOKIE_NAME: stored_cookie}, response_url=URL(BASE_URL)
            )
            cookie_valid = await self._session_cookie_valid()
        if not cookie_valid:
            await self._login()

        raw_watermarks = await self.mass.cache.get(
            key=CACHE_KEY_LAST_APPLIED,
            provider=self.instance_id,
            category=CACHE_CATEGORY_OPML,
            default={},
        )
        self._feed_watermarks = {
            feed_url: from_iso_string(raw) for feed_url, raw in raw_watermarks.items()
        }

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if not self.http_session.closed:
            await self.http_session.close()

    @property
    def is_streaming_provider(self) -> bool:
        """Return False: the library mirrors the user's own Overcast subscriptions."""
        return False

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve the subscribed podcasts from the Overcast account."""
        subscriptions = await self._get_opml_subscriptions()
        for feed_url, subscription in subscriptions.items():
            self.logger.debug("Adding podcast with feed %s to library", feed_url)
            try:
                parsed_podcast = await refresh_cached_podcast(
                    mass=self.mass,
                    provider_instance_id=self.instance_id,
                    feed_url=feed_url,
                    max_episodes=self.max_episodes,
                )
            except MediaNotFoundError:
                self.logger.warning("Was unable to obtain podcast with feed %s", feed_url)
                continue
            applied = await self._apply_playback_states(feed_url, subscription, parsed_podcast)
            if applied is not None:
                # stored right away: the caller may stop consuming this generator at
                # any point, which would otherwise re-apply these states on the next sync
                self._feed_watermarks[feed_url] = applied
                await self._store_watermarks()
            yield parse_podcast(
                feed_url=feed_url,
                parsed_feed=parsed_podcast,
                instance_id=self.instance_id,
                domain=self.domain,
            )

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get the podcast for the given feed url."""
        parsed_podcast = await self._cache_get_podcast(prov_podcast_id)
        return parse_podcast(
            feed_url=prov_podcast_id,
            parsed_feed=parsed_podcast,
            instance_id=self.instance_id,
            domain=self.domain,
        )

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all episodes of a podcast, including their Overcast playback state."""
        podcast = await self._cache_get_podcast(prov_podcast_id)
        subscription = await self._get_subscription(prov_podcast_id)
        podcast_cover = podcast.get("cover_url")
        podcast_name = podcast.get("title")
        for cnt, parsed_episode in enumerate(podcast.get("episodes", [])):
            mass_episode = parse_podcast_episode(
                episode=parsed_episode,
                prov_podcast_id=prov_podcast_id,
                episode_cnt=cnt,
                podcast_cover=podcast_cover,
                podcast_name=podcast_name,
                instance_id=self.instance_id,
                domain=self.domain,
            )
            if mass_episode is None:
                # faulty episode
                continue
            try:
                stream_url, _ = get_stream_url_and_guid_from_episode(episode=parsed_episode)
            except ValueError:
                # episode enclosure or stream url missing
                continue
            if subscription is not None:
                state = match_episode_state(subscription, stream_url)
                if state is not None and (state.played or state.progress_s):
                    mass_episode.resume_position_ms = (state.progress_s or 0) * 1000
                    mass_episode.fully_played = state.played
            yield mass_episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get a single podcast episode."""
        podcast_id, guid_or_stream_url = prov_episode_id.split(" ", 1)
        async for mass_episode in self.get_podcast_episodes(podcast_id):
            _, episode_key = mass_episode.item_id.split(" ", 1)
            if episode_key == guid_or_stream_url:
                await self._enrich_episode_chapters(podcast_id, guid_or_stream_url, mass_episode)
                return mass_episode
        raise MediaNotFoundError("Did not find episode.")

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """Return fully_played, resume position (ms) and its timestamp from Overcast."""
        if media_type != MediaType.PODCAST_EPISODE:
            raise NotImplementedError
        podcast_id, guid_or_stream_url = item_id.split(" ", 1)
        stream_url = await self._get_episode_stream_url(podcast_id, guid_or_stream_url)
        if stream_url is None:
            raise NotImplementedError
        subscription = await self._get_subscription(podcast_id)
        state = match_episode_state(subscription, stream_url) if subscription else None
        if state is None or (not state.played and state.progress_s is None):
            # No known Overcast progress; raise NotImplementedError such that MA
            # falls back to the resume position stored in its own playlog.
            raise NotImplementedError
        return state.played, max((state.progress_s or 0) * 1000, 0), state.user_updated_at

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for item."""
        podcast_id, guid_or_stream_url = item_id.split(" ", 1)
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

    async def _login(self) -> None:
        """Authenticate with Overcast and persist the session cookie."""
        email = str(self.get_setup_value(CONF_USERNAME))
        password = str(self.get_setup_value(CONF_PASSWORD))
        try:
            async with self.http_session.post(
                LOGIN_URL,
                data={"email": email, "password": password},
                allow_redirects=False,
            ) as response:
                status = response.status
                location = response.headers.get("Location", "")
                morsel = response.cookies.get(SESSION_COOKIE_NAME)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise ResourceTemporarilyUnavailable("Overcast is unreachable") from err
        if status != 302 or "/podcasts" not in location or morsel is None:
            raise LoginFailed("Overcast login failed, check your email and password")
        self._update_setup_data(CONF_SESSION_COOKIE, morsel.value)

    async def _session_cookie_valid(self) -> bool:
        """Check whether the restored session cookie is still accepted by Overcast."""
        try:
            async with self.http_session.get(PODCASTS_URL, allow_redirects=False) as response:
                return response.status == 200
        except (TimeoutError, aiohttp.ClientError) as err:
            raise ResourceTemporarilyUnavailable("Overcast is unreachable") from err

    @use_cache(
        expiration=OPML_CACHE_EXPIRATION,
        category=CACHE_CATEGORY_OPML,
        allow_expired_cache=True,
        cache_none=False,
    )
    async def _fetch_opml_text(self) -> str:
        """Fetch the account's extended OPML export."""
        opml_text = await self._request_opml()
        if opml_text is None:
            # the session expired, log in again and retry once
            await self._login()
            opml_text = await self._request_opml()
            if opml_text is None:
                raise LoginFailed("Overcast rejected the session right after a fresh login")
        return opml_text

    async def _request_opml(self) -> str | None:
        """Return the raw OPML document, or None if the session cookie was rejected."""
        if remaining := self._rate_limit_remaining():
            # spend no request while Overcast is still refusing them: the export allows
            # only ~10 per day and every episode listing would otherwise cost one
            raise ResourceTemporarilyUnavailable(
                "Overcast OPML export is rate limited", backoff_time=remaining
            )
        try:
            async with self.http_session.get(OPML_EXPORT_URL, allow_redirects=False) as response:
                if response.status == 200:
                    return await response.text()
                if response.status == 429:
                    backoff = (
                        parse_retry_after(response.headers.get("Retry-After"))
                        or RATE_LIMIT_FALLBACK_BACKOFF
                    )
                    self._rate_limited_until = time.monotonic() + backoff
                    raise ResourceTemporarilyUnavailable(
                        "Overcast OPML export is rate limited", backoff_time=backoff
                    )
                if response.status in AUTH_REJECT_STATUSES:
                    return None
                raise ResourceTemporarilyUnavailable(
                    f"Overcast OPML export failed with HTTP {response.status}"
                )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise ResourceTemporarilyUnavailable("Overcast is unreachable") from err

    def _rate_limit_remaining(self) -> int:
        """Return the seconds left of a known Overcast rate limit window, 0 if none."""
        if self._rate_limited_until is None:
            return 0
        remaining = self._rate_limited_until - time.monotonic()
        if remaining <= 0:
            self._rate_limited_until = None
            return 0
        return math.ceil(remaining)

    async def _get_opml_subscriptions(self) -> dict[str, OvercastSubscription]:
        opml_text = await self._fetch_opml_text()
        if self._opml_cache is None or self._opml_cache[0] != opml_text:
            parsed = await asyncio.to_thread(parse_extended_opml, opml_text)
            self._opml_cache = (opml_text, parsed)
        return self._opml_cache[1]

    async def _get_subscription(self, feed_url: str) -> OvercastSubscription | None:
        """Return the Overcast subscription for a feed, or None if unavailable."""
        try:
            subscriptions = await self._get_opml_subscriptions()
        except (ResourceTemporarilyUnavailable, LoginFailed) as err:
            # episodes can still be listed without playback state
            self.logger.debug("Could not obtain Overcast playback states: %s", err)
            return None
        return subscriptions.get(feed_url)

    async def _apply_playback_states(
        self,
        feed_url: str,
        subscription: OvercastSubscription,
        parsed_podcast: dict[str, Any],
    ) -> datetime | None:
        """
        Push a feed's new Overcast playback states to the playlog.

        :param feed_url: The podcast's feed url (also the provider item id).
        :param subscription: The Overcast subscription holding the episode states.
        :param parsed_podcast: The podcastparser dict of the feed.
        :return: The newest state timestamp that was applied, or None if none were.
        """
        watermark = self._feed_watermarks.get(feed_url)
        newest_applied: datetime | None = None
        podcast_cover = parsed_podcast.get("cover_url")
        podcast_name = parsed_podcast.get("title")
        for cnt, parsed_episode in enumerate(parsed_podcast.get("episodes", [])):
            try:
                stream_url, _ = get_stream_url_and_guid_from_episode(episode=parsed_episode)
            except ValueError:
                continue
            state = match_episode_state(subscription, stream_url)
            if state is None or state.user_updated_at is None:
                continue
            if not state.played and not state.progress_s:
                # never mark items unplayed: an absent state cannot be told apart
                # from an episode that simply was never touched in Overcast
                continue
            if watermark is not None and state.user_updated_at <= watermark:
                # already applied in a previous sync; skipping it also makes sure
                # local progress made since then is not overwritten
                continue
            mass_episode = parse_podcast_episode(
                episode=parsed_episode,
                prov_podcast_id=feed_url,
                episode_cnt=cnt,
                podcast_cover=podcast_cover,
                podcast_name=podcast_name,
                instance_id=self.instance_id,
                domain=self.domain,
            )
            if mass_episode is None:
                continue
            if not state.played:
                # never move the user backwards: the playlog write replaces whatever MA
                # recorded itself, which may be a position further into the episode
                _, local_position_ms = await self.mass.music.get_resume_position(mass_episode)
                if local_position_ms > (state.progress_s or 0) * 1000:
                    continue
            await self.mass.music.mark_item_played(
                mass_episode,
                fully_played=state.played,
                seconds_played=state.progress_s or 0,
                user_initiated=False,
            )
            if newest_applied is None or state.user_updated_at > newest_applied:
                newest_applied = state.user_updated_at
        return newest_applied

    async def _store_watermarks(self) -> None:
        """Persist the per-feed watermarks of the applied playback states."""
        # watermarks of feeds that are no longer subscribed are kept on purpose,
        # so re-subscribing does not re-apply the feed's entire playback history
        await self.mass.cache.set(
            key=CACHE_KEY_LAST_APPLIED,
            provider=self.instance_id,
            category=CACHE_CATEGORY_OPML,
            data={feed_url: ts.isoformat() for feed_url, ts in self._feed_watermarks.items()},
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
