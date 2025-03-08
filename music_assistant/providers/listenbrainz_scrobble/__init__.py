"""Allows scrobbling of tracks with the help of liblistenbrainz."""

# icon.svg from https://github.com/metabrainz/design-system/tree/master/brand/logos
# released under the Creative Commons Attribution-ShareAlike(BY-SA) 4.0 license.
# https://creativecommons.org/licenses/by-sa/4.0/

import asyncio
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
from music_assistant_models.event import MassEvent
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
from music_assistant_models.provider import ProviderManifest

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
    _currently_playing: str | None = None
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

        # subscribe to internal event
        self._on_unload.append(
            self.mass.subscribe(self._on_mass_media_item_played, EventType.MEDIA_ITEM_PLAYED)
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        for unload_cb in self._on_unload:
            unload_cb()

    async def _on_mass_media_item_played(self, event: MassEvent) -> None:
        """Media item has finished playing, we'll scrobble the track."""
        if self._client is None:
            self.logger.error("no client available during _on_mass_media_item_played")
            return

        report = event.data

        def make_listen(report: Any) -> Listen:
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

        def update_now_playing() -> None:
            try:
                listen = make_listen(report)
                self._client.submit_playing_now(listen)
                self.logger.debug(f"track {report.uri} marked as 'now playing'")
                self._currently_playing = report.uri
            except Exception as err:
                self.logger.exception(err)

        def scrobble() -> None:
            try:
                listen = make_listen(report)
                listen.listened_at = int(time.time())
                self._client.submit_single_listen(listen)
            except Exception as err:
                self.logger.exception(err)

        # update now playing if needed
        if self._currently_playing is None or self._currently_playing != report.uri:
            await asyncio.to_thread(update_now_playing)

        if self.should_scrobble(report):
            await asyncio.to_thread(scrobble)

        if report.fully_played:
            # reset currently playing to avoid it expiring when looping songs
            self._currently_playing = None

    def should_scrobble(self, report: MediaItemPlaybackProgressReport) -> bool:
        """Determine if a track should be scrobbled, to be extended later."""
        # ideally we want more precise control
        # but because the event is triggered every 30s
        # and we don't have full queue details to determine
        # the exact context in which the event was fired
        # we can only rely on fully_played for now
        return bool(report.fully_played)


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
