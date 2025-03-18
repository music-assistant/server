"""Allows scrobbling of tracks with the help of liblistenbrainz."""

# icon.svg from https://github.com/metabrainz/design-system/tree/master/brand/logos
# released under the Creative Commons Attribution-ShareAlike(BY-SA) 4.0 license.
# https://creativecommons.org/licenses/by-sa/4.0/

import logging
import time
from collections.abc import Callable
from typing import Any

from liblistenbrainz import Listen, ListenBrainz
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType, EventType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
from music_assistant_models.provider import ProviderManifest

from music_assistant.helpers.scrobbler import ScrobblerHelper
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider

CONF_USER_TOKEN = "_user_token"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    token = config.get_value(CONF_USER_TOKEN)
    if not token:
        raise SetupFailedError("User token needs to be set")

    assert token != SECURE_STRING_SUBSTITUTE

    client = ListenBrainz()
    client.set_auth_token(token)

    return ListenBrainzScrobbleProvider(mass, manifest, config, client)


class ListenBrainzScrobbleProvider(PluginProvider):
    """Plugin provider to support scrobbling of tracks."""

    _client: ListenBrainz = None
    _on_unload: list[Callable[[], None]] = []

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        client: ListenBrainz,
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self._client = client

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

        handler = ListenBrainzEventHandler(self._client, self.logger)

        # subscribe to internal event
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

    _client: ListenBrainz = None

    def __init__(self, client: ListenBrainz, logger: logging.Logger) -> None:
        """Initialize."""
        super().__init__(logger)
        self._client = client

    def _is_configured(self) -> bool:
        """Check that we are configured."""
        if self._client is None:
            self.logger.error("no client available during _on_mass_media_item_played")
            return False
        return True

    def _make_listen(self, report: Any) -> Listen:
        # album artist and track number are not available without an extra API call
        # so they won't be scrobbled

        # https://pylistenbrainz.readthedocs.io/en/latest/api_ref.html#class-listen
        return Listen(
            track_name=report.name,
            artist_name=report.artist,
            release_name=report.album,
            recording_mbid=report.mbid,
            listening_from="music-assistant",
        )

    def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        try:
            listen = self._make_listen(report)
            self._client.submit_playing_now(listen)
            self.logger.debug(f"track {report.uri} marked as 'now playing'")
            self._currently_playing = report.uri
        except Exception as err:
            self.logger.exception(err)

    def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        try:
            listen = self._make_listen(report)
            listen.listened_at = int(time.time())
            self._client.submit_single_listen(listen)
            self._last_scrobbled = report.uri
        except Exception as err:
            self.logger.exception(err)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_USER_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="User Token",
            required=True,
            value=values.get(CONF_USER_TOKEN) if values else None,
        ),
    )
