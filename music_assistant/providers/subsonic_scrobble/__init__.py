"""Allows scrobbling of tracks back to the Subsonic media server."""

import asyncio
import logging
import time
from collections.abc import Callable

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import Audiobook, PodcastEpisode, Track
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
from music_assistant_models.provider import ProviderManifest

from music_assistant.helpers.scrobbler import ScrobblerHelper
from music_assistant.helpers.uri import parse_uri
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.opensubsonic.parsers import EP_CHAN_SEP
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
    """Handles the scrobbling event handling."""

    def __init__(self, mass: MusicAssistant, logger: logging.Logger) -> None:
        """Initialize."""
        super().__init__(logger)
        self.mass = mass

    def _is_scrobblable_media_type(self, media_type: MediaType) -> bool:
        """Return true if the given OpenSubsonic media type can be scrobbled, false otherwise."""
        return media_type in (
            MediaType.TRACK,
            MediaType.AUDIOBOOK,
            MediaType.PODCAST_EPISODE,
        )

    async def _get_subsonic_provider_and_item_id(
        self, media_type: MediaType, provider_instance_id_or_domain: str, item_id: str
    ) -> tuple[None | OpenSonicProvider, str]:
        """Return a OpenSonicProvider or None if no subsonic provider, and the Subsonic item_id.

        Returns:
            Tuple[OpenSonicProvider | None, str]: The provider or None, and the Subsonic item_id.
        """
        if provider_instance_id_or_domain == "library":
            # unwrap library item to check if we have a subsonic mapping...
            library_item = await self.mass.music.get_library_item_by_prov_id(
                media_type, item_id, provider_instance_id_or_domain
            )
            if library_item is None:
                return None, item_id
            assert isinstance(library_item, Track | Audiobook | PodcastEpisode)
            for mapping in library_item.provider_mappings:
                if mapping.provider_domain.startswith("opensubsonic"):
                    # found a subsonic mapping, proceed...
                    prov = self.mass.get_provider(mapping.provider_instance)
                    assert isinstance(prov, OpenSonicProvider)
                    # Because there is no way to retrieve a single podcast episode in vanilla
                    # subsonic, we have to carry around the channel id as well. See
                    # opensubsonic.parsers.parse_episode.
                    if isinstance(library_item, PodcastEpisode) and EP_CHAN_SEP in mapping.item_id:
                        _, ret_id = mapping.item_id.split(EP_CHAN_SEP)
                    else:
                        ret_id = mapping.item_id
                    return prov, ret_id
            # no subsonic mapping has been found in library item, ignore...
            return None, item_id
        elif provider_instance_id_or_domain.startswith("opensubsonic"):
            # found a subsonic mapping, proceed...
            prov = self.mass.get_provider(provider_instance_id_or_domain)
            assert isinstance(prov, OpenSonicProvider)
            if media_type == MediaType.PODCAST_EPISODE and EP_CHAN_SEP in item_id:
                _, ret_id = item_id.split(EP_CHAN_SEP)
                return prov, ret_id
            return prov, item_id
        # not an item from subsonic provider, ignore...
        return None, item_id

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        def handler(prov: OpenSonicProvider, item_id: str, uri: str) -> None:
            try:
                self.logger.info("scrobble play now event")
                prov.conn.scrobble(item_id, submission=False)
                self.logger.debug("track %s marked as 'now playing'", uri)
                self.currently_playing = uri
            except Exception as err:
                self.logger.exception(err)

        media_type, provider_instance_id_or_domain, item_id = await parse_uri(report.uri)
        if not self._is_scrobblable_media_type(media_type):
            return
        prov, item_id = await self._get_subsonic_provider_and_item_id(
            media_type, provider_instance_id_or_domain, item_id
        )
        if not prov:
            return

        # the opensubsonic library is not async friendly,
        # so we need to run it in a executor thread
        await asyncio.to_thread(handler, prov, item_id, report.uri)

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        def handler(prov: OpenSonicProvider, item_id: str, uri: str) -> None:
            try:
                prov.conn.scrobble(item_id, submission=True, listen_time=int(time.time()))
                self.logger.debug("track %s marked as 'played'", uri)
                self.last_scrobbled = uri
            except Exception as err:
                self.logger.exception(err)

        media_type, provider_instance_id_or_domain, item_id = await parse_uri(report.uri)
        if not self._is_scrobblable_media_type(media_type):
            return
        prov, item_id = await self._get_subsonic_provider_and_item_id(
            media_type, provider_instance_id_or_domain, item_id
        )
        if not prov:
            return

        # the opensubsonic library is not async friendly,
        # so we need to run it in a executor thread
        await asyncio.to_thread(handler, prov, item_id, report.uri)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # ruff: noqa: ARG001
    return ()
