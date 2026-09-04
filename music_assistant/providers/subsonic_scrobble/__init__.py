"""Allows scrobbling of supported media items back to the Subsonic media server."""

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar, Final

import aiohttp
from libopensonic.errors import SonicError
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import Audiobook, PodcastEpisode, Track

from music_assistant.helpers.scrobbler import ScrobblerConfig, ScrobblerHelper
from music_assistant.helpers.uri import parse_uri
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.opensubsonic.parsers import EP_CHAN_SEP
from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.provider import ProviderManifest

SUPPORTED_SCROBBLE_MEDIA_TYPES: Final[frozenset[MediaType]] = frozenset(
    {
        MediaType.TRACK,
        MediaType.AUDIOBOOK,
        MediaType.PODCAST_EPISODE,
    }
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    sonic_prov = mass.get_provider("opensubsonic")
    if not sonic_prov or not isinstance(sonic_prov, OpenSonicProvider):
        raise SetupFailedError("A Open Subsonic Music provider must be configured first.")

    return SubsonicScrobbleProvider(mass, manifest, config)


class SubsonicScrobbleProvider(PluginProvider):
    """Plugin provider to support Subsonic scrobbling."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self._on_unload: list[Callable[[], None]] = []

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (*await ScrobblerConfig.get_shared_config_entries(self.mass, None),)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

        handler = SubsonicScrobbleEventHandler(self.mass, self.logger, self.config)

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

    # SonicError covers Subsonic API failures; aiohttp.ClientError and TimeoutError
    # cover the underlying transport the libopensonic connection uses.
    scrobble_exceptions: ClassVar[tuple[type[Exception], ...]] = (
        SonicError,
        aiohttp.ClientError,
        TimeoutError,
    )

    def __init__(
        self, mass: MusicAssistant, logger: logging.Logger, config: ProviderConfig
    ) -> None:
        """Initialize."""
        super().__init__(
            logger,
            ScrobblerConfig.create_from_config(config),
            SUPPORTED_SCROBBLE_MEDIA_TYPES,
        )
        self.mass = mass

    async def _get_subsonic_provider_and_item_id(
        self,
        media_type: MediaType,
        provider_instance_id_or_domain: str,
        item_id: str,
        user_id: str | None = None,
    ) -> tuple[OpenSonicProvider | None, str]:
        """
        Return a OpenSonicProvider or None if no subsonic provider, and the Subsonic item_id.

        :param media_type: Media type of the played item.
        :param provider_instance_id_or_domain: Provider part of the played item's uri.
        :param item_id: Item id part of the played item's uri.
        :param user_id: MA user that initiated playback. When the item maps to more than one
            Subsonic provider instance, the instance in that user's provider filter is used.
        """
        if provider_instance_id_or_domain == "library":
            # unwrap library item to check if we have a subsonic mapping...
            library_item = await self.mass.music.get_library_item_by_prov_id(
                media_type, item_id, provider_instance_id_or_domain
            )
            if library_item is None:
                return None, item_id
            assert isinstance(library_item, Track | Audiobook | PodcastEpisode)
            sonic_mappings = [
                mapping
                for mapping in library_item.provider_mappings
                if mapping.provider_domain.startswith("opensubsonic")
            ]
            # One library item can map to several instances of the same Subsonic server (one
            # instance per account of that server). provider_mappings is a set, so without a
            # preference the account that receives the scrobble is arbitrary; the instance in
            # the playing user's provider filter goes first, the others keep their order.
            preferred = await self._get_user_provider_filter(user_id)
            sonic_mappings.sort(key=lambda mapping: mapping.provider_instance not in preferred)
            for mapping in sonic_mappings:
                prov = self.mass.get_provider(mapping.provider_instance)
                if not isinstance(prov, OpenSonicProvider):
                    continue
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
        if provider_instance_id_or_domain.startswith("opensubsonic"):
            # found a subsonic mapping, proceed...
            prov = self.mass.get_provider(provider_instance_id_or_domain)
            assert isinstance(prov, OpenSonicProvider)
            if media_type == MediaType.PODCAST_EPISODE and EP_CHAN_SEP in item_id:
                _, ret_id = item_id.split(EP_CHAN_SEP)
                return prov, ret_id
            return prov, item_id
        # not an item from subsonic provider, ignore...
        return None, item_id

    async def _get_user_provider_filter(self, user_id: str | None) -> set[str]:
        """
        Return the provider instance ids the given MA user is restricted to.

        :param user_id: MA user id, or None when playback was not initiated by a user.
        """
        if not user_id:
            return set()
        user = await self.mass.webserver.auth.get_user(user_id)
        if user is None or not user.provider_filter:
            return set()
        return set(user.provider_filter)

    async def _update_now_playing(self, report: MediaItemPlaybackProgressReport) -> None:
        media_type, provider_instance_id_or_domain, item_id = await parse_uri(report.uri)
        prov, item_id = await self._get_subsonic_provider_and_item_id(
            media_type, provider_instance_id_or_domain, item_id, report.userid
        )
        if not prov:
            return

        await prov.conn.scrobble(item_id, submission=False)

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        media_type, provider_instance_id_or_domain, item_id = await parse_uri(report.uri)
        prov, item_id = await self._get_subsonic_provider_and_item_id(
            media_type, provider_instance_id_or_domain, item_id, report.userid
        )
        if not prov:
            return

        await prov.conn.scrobble(item_id, submission=True, listen_time=int(time.time()))
