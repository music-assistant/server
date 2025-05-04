"""Allows scrobbling of tracks back to the Subsonic media server."""

import asyncio
import logging
import time
from collections.abc import Callable

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
from music_assistant_models.provider import ProviderManifest

from music_assistant.helpers.scrobbler import ScrobblerHelper
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    sonic_prov = mass.get_provider("opensubsonic")
    if not sonic_prov or not isinstance(sonic_prov, OpenSonicProvider):
        raise SetupFailedError("A Open Subsonic Music provider must be configured first.")

    return SubsonicScrobbleProvider(mass, manifest, config)


class SubsonicScrobbleProvider(PluginProvider):
    """Plugin provider to support scrobbling of tracks."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self._on_unload: list[Callable[[], None]] = []

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

        handler = SubsonicScrobbleEventHandler(self.mass, self.logger)

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


class SubsonicScrobbleEventHandler(ScrobblerHelper):
    """Handles the event handling."""

    def __init__(self, mass: MusicAssistant, logger: logging.Logger) -> None:
        """Initialize."""
        super().__init__(logger)
        self._mass = mass

    def _get_sonic_prov_of_played_media(
        self, report: MediaItemPlaybackProgressReport
    ) -> OpenSonicProvider | None:
        """
        Return the corresponding Subsonic music provider, or None, if the media isn't
        a playable single item, or the source isn't a Subsonic music provider.
        """
        if report.media_type not in (
            MediaType.TRACK,
            MediaType.AUDIOBOOK,
            MediaType.PODCAST_EPISODE,
        ):
            return None
        prov = self._mass.get_provider(report.provider)
        if not isinstance(prov, OpenSonicProvider):
            return None
        return prov

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        def handler() -> None:
            try:
                prov = self._get_sonic_prov_of_played_media(report)
                if not prov:
                    return
                prov._conn.scrobble(report.item_id, submission=False)
                self.logger.debug(f"track {report.uri} marked as 'now playing'")
                self.currently_playing = report.uri
            except Exception as err:
                self.logger.exception(err)

        # the opensubsonic library is not async friendly,
        # so we need to run it in a executor thread
        await asyncio.to_thread(handler)

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        def handler() -> None:
            try:
                prov = self._get_sonic_prov_of_played_media(report)
                if not prov:
                    return
                prov._conn.scrobble(report.item_id, submission=True, listenTime=int(time.time()))
                self.logger.debug(f"track {report.uri} marked as 'played'")
                self.last_scrobbled = report.uri
            except Exception as err:
                self.logger.exception(err)

        # the opensubsonic library is not async friendly,
        # so we need to run it in a executor thread
        await asyncio.to_thread(handler)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # ruff: noqa: ARG001
    return ()
