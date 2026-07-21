"""Allows scrobbling of tracks with the help of liblistenbrainz."""

# icon.svg from https://github.com/metabrainz/design-system/tree/master/brand/logos
# released under the Creative Commons Attribution-ShareAlike(BY-SA) 4.0 license.
# https://creativecommons.org/licenses/by-sa/4.0/

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar, Final

import requests.exceptions
from liblistenbrainz import Listen, ListenBrainz
from liblistenbrainz.errors import ListenBrainzException
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType, EventType, MediaType, ProviderFeature
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.helpers.scrobbler import ScrobblerConfig, ScrobblerHelper
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.provider import ProviderManifest

CONF_USER_TOKEN = "_user_token"
CONF_API_BASE_URL = "api_base_url"
LISTENBRAINZ_API_URL = "https://api.listenbrainz.org"
SUPPORTED_FEATURES: set[ProviderFeature] = (
    set()
)  # we don't have any special supported features (yet)
SUPPORTED_SCROBBLE_MEDIA_TYPES: Final[frozenset[MediaType]] = frozenset({MediaType.TRACK})


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    token = config.get_value(CONF_USER_TOKEN)
    api_base_url = config.get_value(CONF_API_BASE_URL) or LISTENBRAINZ_API_URL

    if not token:
        raise SetupFailedError("User token needs to be set")

    assert token != SECURE_STRING_SUBSTITUTE

    client = ListenBrainz(api_base_url=api_base_url)
    client.set_auth_token(token)

    return ListenBrainzScrobbleProvider(mass, manifest, config, client)


class ListenBrainzScrobbleProvider(PluginProvider):
    """Plugin provider to support scrobbling of tracks."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        client: ListenBrainz,
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self._client = client
        self._on_unload: list[Callable[[], None]] = []

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

        handler = ListenBrainzEventHandler(self._client, self.logger, self.config)

        # subscribe to media_item_played event
        self._on_unload.append(
            self.mass.subscribe(handler._on_mass_media_item_played, EventType.MEDIA_ITEM_PLAYED)
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        for unload_cb in self._on_unload:
            unload_cb()


class ListenBrainzEventHandler(ScrobblerHelper):
    """Handles the event handling."""

    # The client raises ListenBrainzException for API/payload errors and lets raw
    # requests network errors (RequestException) propagate.
    scrobble_exceptions: ClassVar[tuple[type[Exception], ...]] = (
        ListenBrainzException,
        requests.exceptions.RequestException,
    )

    def __init__(
        self, client: ListenBrainz, logger: logging.Logger, config: ProviderConfig
    ) -> None:
        """Initialize."""
        super().__init__(
            logger,
            ScrobblerConfig.create_from_config(config),
            SUPPORTED_SCROBBLE_MEDIA_TYPES,
        )
        self._client = client

    def _get_artist_name(self, report: MediaItemPlaybackProgressReport) -> str:
        """Return the best available artist name for the ListenBrainz payload."""
        if report.artists:
            return ", ".join(artist for artist in report.artists)
        return report.artist or UNKNOWN_ARTIST

    def _make_listen(self, report: MediaItemPlaybackProgressReport) -> Listen:
        # album artist and track number are not available without an extra API call
        # so they won't be scrobbled

        additional_info = {}

        if report.duration:
            additional_info["duration"] = report.duration

        if report.seconds_played:
            additional_info["duration_played"] = report.seconds_played

        # https://pylistenbrainz.readthedocs.io/en/latest/api_ref.html#class-listen
        return Listen(
            track_name=self.get_name(report),
            artist_name=self._get_artist_name(report),
            artist_mbids=report.artist_mbids,
            release_name=report.album,
            release_mbid=report.album_mbid,
            recording_mbid=report.mbid,
            listening_from="music-assistant",
            additional_info=additional_info or None,
        )

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        def handler() -> None:
            listen = self._make_listen(report)
            self._client.submit_playing_now(listen)

        # the listenbrainz client is not async friendly,
        # so we need to run it in a executor thread
        await asyncio.to_thread(handler)

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        def handler() -> None:
            listen = self._make_listen(report)
            listen.listened_at = int(time.time())
            self._client.submit_single_listen(listen)

        # the listenbrainz client is not async friendly,
        # so we need to run it in a executor thread
        await asyncio.to_thread(handler)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        *await ScrobblerConfig.get_shared_config_entries(mass, values),
        ConfigEntry(
            key=CONF_USER_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            required=True,
            value=values.get(CONF_USER_TOKEN) if values else None,
        ),
        ConfigEntry(
            key=CONF_API_BASE_URL,
            type=ConfigEntryType.STRING,
            required=False,
            value=values.get(CONF_API_BASE_URL) if values else None,
            advanced=True,
        ),
    )
