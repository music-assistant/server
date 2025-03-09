"""gPodder provider for Music Assistant.

Tested against opodsync, https://github.com/kd2org/opodsync
and nextcloud-gpodder, https://github.com/thrillfall/nextcloud-gpodder
gpodder.net is not supported due to responsiveness of domain.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    MediaType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    LoginFailed,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    MediaItemType,
    Podcast,
    PodcastEpisode,
)

from music_assistant.providers.gpodder.client import GPodderClient
from music_assistant.providers.itunes_podcasts import ITunesPodcastsProvider
from music_assistant.providers.itunes_podcasts.parsers import parse_podcast

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Config for "classic" gpodder api
CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEVICE_ID = "device_id"
CONF_USING_GPODDER = "using_gpodder"  # hidden, bool, true if not nextcloud used

# Config for nextcloud
CONF_ACTION_AUTH_NC = "authenticate_nc"
CONF_TOKEN_NC = "token"
CONF_URL_NC = "url_nc"

# General config
CONF_VERIFY_SSL = "verify_ssl"
CONF_MAX_NUM_EPISODES = "max_num_episodes"
CACHE_CATEGORY_PODCASTS = 0


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return GPodder(mass, manifest, config)


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
    if values is None:
        values = {}

    if action == CONF_ACTION_AUTH_NC:
        session = mass.http_session
        response = await session.post(
            str(values[CONF_URL_NC]).rstrip("/") + "/index.php/login/v2",
            headers={"User-Agent": "Music Assistant"},
        )
        data = await response.json()
        poll_endpoint = data["poll"]["endpoint"]
        poll_token = data["poll"]["token"]
        login_url = data["login"]
        session_id = str(values["session_id"])
        mass.signal_event(EventType.AUTH_SESSION, session_id, login_url)
        while True:
            response = await session.post(poll_endpoint, data={"token": poll_token})
            if response.status not in [200, 404]:
                raise LoginFailed("The specified url seems not to belong to a nextcloud instance.")
            if response.status == 200:
                data = await response.json()
                values[CONF_TOKEN_NC] = data["appPassword"]
                break
            await asyncio.sleep(1)

    authenticated_nc = True
    if values.get(CONF_TOKEN_NC, None) is None:
        authenticated_nc = False

    using_gpodder = bool(values.get(CONF_USING_GPODDER, False))

    return (
        ConfigEntry(
            key="label_text",
            type=ConfigEntryType.LABEL,
            label="Authentication did succeed! Please press save to continue.",
            hidden=not authenticated_nc,
        ),
        ConfigEntry(
            key="label_gpodder",
            type=ConfigEntryType.LABEL,
            label="Authentication with gPodder compatible web service, e.g. opodsync:",
            hidden=authenticated_nc,
        ),
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="URL",
            required=False,
            description="URL of gPodder instance.",
            value=values.get(CONF_URL),
            hidden=authenticated_nc,
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=False,
            description="Username of gPodder instance.",
            hidden=authenticated_nc,
            value=values.get(CONF_USERNAME),
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            description="Password for gPodder instance.",
            hidden=authenticated_nc,
            value=values.get(CONF_PASSWORD),
        ),
        ConfigEntry(
            key=CONF_DEVICE_ID,
            type=ConfigEntryType.STRING,
            label="Device ID",
            required=False,
            description="Device ID of user.",
            hidden=authenticated_nc,
            value=values.get(CONF_DEVICE_ID),
        ),
        ConfigEntry(
            key="label_nextcloud",
            type=ConfigEntryType.LABEL,
            label="Authentication with Nextcloud with GPodder Sync (nextcloud-gpodder) installed:",
            hidden=authenticated_nc or using_gpodder,
        ),
        ConfigEntry(
            key=CONF_URL_NC,
            type=ConfigEntryType.STRING,
            label="URL",
            required=False,
            description="URL of Nextcloud instance.",
            value=values.get(CONF_URL),
            hidden=using_gpodder,
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH_NC,
            type=ConfigEntryType.ACTION,
            label="(Re)Authenticate with Nextcloud",
            description="This button will redirect you to your Nextcloud instance to authenticate.",
            action=CONF_ACTION_AUTH_NC,
            required=False,
            hidden=using_gpodder,
        ),
        ConfigEntry(
            key="label_general",
            type=ConfigEntryType.LABEL,
            label="General config:",
        ),
        ConfigEntry(
            key=CONF_MAX_NUM_EPISODES,
            type=ConfigEntryType.INTEGER,
            label="Maximum amount of episodes (0 for unlimited)",
            required=False,
            description="Maximum amount of episodes to sync per feed. Use 0 for unlimited",
            default_value=0,
            value=values.get(CONF_MAX_NUM_EPISODES),
        ),
        ConfigEntry(
            key=CONF_VERIFY_SSL,
            type=ConfigEntryType.BOOLEAN,
            label="Verify SSL",
            required=False,
            description="Whether or not to verify the certificate of SSL/TLS connections.",
            category="advanced",
            default_value=True,
            value=values.get(CONF_VERIFY_SSL),
        ),
        ConfigEntry(
            key=CONF_TOKEN_NC,
            type=ConfigEntryType.SECURE_STRING,
            label="token",
            hidden=True,
            required=False,
            value=values.get(CONF_TOKEN_NC),
        ),
        ConfigEntry(
            key=CONF_USING_GPODDER,
            type=ConfigEntryType.BOOLEAN,
            label="using_gpodder",
            hidden=True,
            required=False,
            value=values.get(CONF_USING_GPODDER),
        ),
    )


class GPodder(ITunesPodcastsProvider):
    """gPodder MusicProvider.

    We can inherit from the ITunesPodcastsProvider here, as gpodder only stores stream URLs and
    user progresses. So we need to add progress information and reporting, and implement library
    sync.
    """

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Features supported by this Provider."""
        return {
            ProviderFeature.LIBRARY_PODCASTS,
            ProviderFeature.BROWSE,
        }

    async def handle_async_init(self) -> None:
        """Pass config values to client and initialize."""
        base_url = str(self.config.get_value(CONF_URL))
        _username = self.config.get_value(CONF_USERNAME)
        _password = self.config.get_value(CONF_PASSWORD)
        _device_id = self.config.get_value(CONF_DEVICE_ID)
        nc_url = str(self.config.get_value(CONF_URL_NC))
        nc_token = self.config.get_value(CONF_TOKEN_NC)

        # int float str - can this be easier?
        self.max_episodes = int(float(str(self.config.get_value(CONF_MAX_NUM_EPISODES))))

        self._client = GPodderClient(session=self.mass.http_session, logger=self.logger)

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

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        # For streaming providers return True here but for local file based providers return False.
        # While the streams are remote, the user controls what is added.
        return False

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Retrieve library/subscribed podcasts from the provider."""
        try:
            subscriptions = await self._client.get_subscriptions()
        except RuntimeError:
            raise ResourceTemporarilyUnavailable(backoff_time=30)
        if subscriptions is None:
            return
        for feed_url in subscriptions.add:
            parsed_podcast = await self._cache_get_podcast(feed_url, use_cache=False)
            await self._cache_set_podcast(feed_url, parsed_podcast)
            yield parse_podcast(
                feed_url=feed_url,
                parsed_feed=parsed_podcast,
                lookup_key=self.lookup_key,
                domain=self.domain,
                instance_id=self.instance_id,
            )

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get Podcast episodes. Add progress information."""
        progresses = await self._client.get_progresses(podcast_id=prov_podcast_id)
        async for episode in super().get_podcast_episodes(prov_podcast_id=prov_podcast_id):
            podcast_id, guid_or_stream_url, stream_url = episode.item_id.split(" ")
            for progress in progresses:
                _test = [progress.guid, progress.episode]
                if guid_or_stream_url in _test or stream_url in _test:
                    episode.resume_position_ms = progress.position * 1000
                    episode.fully_played = progress.position >= progress.total
                    break

            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get Podcast Episode. Add progress information."""
        episode = await super().get_podcast_episode(prov_episode_id=prov_episode_id)
        podcast_id, guid_or_stream_url, stream_url = episode.item_id.split(" ")
        progresses = await self._client.get_progresses(podcast_id=podcast_id)
        for progress in progresses:
            _test = [progress.guid, progress.episode]
            if guid_or_stream_url in _test or stream_url in _test:
                episode.resume_position_ms = progress.position * 1000
                episode.fully_played = progress.position >= progress.total
                break
        return episode

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Return: finished, position_ms."""
        assert media_type == MediaType.PODCAST_EPISODE
        podcast_id, guid_or_stream_url, stream_url = item_id.split(" ")
        try:
            progresses = await self._client.get_progresses(podcast_id=podcast_id)
        except RuntimeError:
            self.logger.warning("Was unable to obtain progresses.")
            return False, 0
        for action in progresses:
            _test = [action.guid, action.episode]
            if action.podcast == podcast_id and (
                guid_or_stream_url in _test or stream_url in _test
            ):
                return action.position >= action.total, max(action.position * 1000, 0)
        return False, 0

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
        podcast_id, guid_or_stream_url, stream_url = prov_item_id.split(" ")
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
        except RuntimeError:
            self.logger.debug("Failed to update progress.")
