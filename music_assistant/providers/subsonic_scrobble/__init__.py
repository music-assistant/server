"""Allows scrobbling of supported media items back to the Subsonic media server."""

import logging
import time
from typing import TYPE_CHECKING, ClassVar, Final

import aiohttp
from libopensonic.errors import SonicError
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import Audiobook, PodcastEpisode, Track

from music_assistant.helpers.provider_access import (
    exact_provider,
    own_music_sources,
    visible_playback_sources,
)
from music_assistant.helpers.scrobbler import ScrobblerConfig, ScrobblerHelper
from music_assistant.helpers.uri import parse_uri
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.opensubsonic.parsers import EP_CHAN_SEP
from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.media_items import ProviderMapping
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.provider import ProviderManifest

SUPPORTED_FEATURES: Final[set[ProviderFeature]] = {ProviderFeature.SCROBBLE}
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

    return SubsonicScrobbleProvider(mass, manifest, config, SUPPORTED_FEATURES)


class SubsonicScrobbleProvider(PluginProvider):
    """Plugin provider to support Subsonic scrobbling."""

    _handler: SubsonicScrobbleEventHandler | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (*await ScrobblerConfig.get_shared_config_entries(self.mass, None),)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()

        self._handler = SubsonicScrobbleEventHandler(self.mass, self.logger, self.config)

    async def on_media_item_played(self, report: MediaItemPlaybackProgressReport) -> None:
        """Forward a playback progress report to the Subsonic server of the playing user."""
        if self._handler is not None:
            await self._handler.on_media_item_played(report)


class SubsonicScrobbleEventHandler(ScrobblerHelper):
    """Submit now-playing updates and scrobbles to the Subsonic server of the playing user."""

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
            Subsonic provider instance, the one that user owns is used.
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
            if not sonic_mappings:
                # no subsonic mapping has been found in library item, ignore...
                return None, item_id
            # One library item can map to several instances of the same Subsonic server (one
            # instance per account of that server). provider_mappings is a set, so without a
            # preference the account that receives the scrobble is arbitrary; the instance the
            # playing user owns goes first, then the ones shared with them.
            # Reporting to a Subsonic account the user may not use would disclose and credit
            # their listening to that other member, so those instances are dropped entirely
            # and nothing is reported when none is left.
            sonic_mappings = await self._preferred_mappings(sonic_mappings, user_id)
            for mapping in sonic_mappings:
                prov = exact_provider(self.mass, mapping.provider_instance)
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
            # mappings exist, but none of the allowed instances is loaded: nothing to report to
            return None, item_id
        if provider_instance_id_or_domain.startswith("opensubsonic"):
            # the item was played from this exact account, so only that one may be reported
            # to; an unavailable account is never stood in for by another member's
            prov = exact_provider(self.mass, provider_instance_id_or_domain)
            if not isinstance(prov, OpenSonicProvider):
                return None, item_id
            if media_type == MediaType.PODCAST_EPISODE and EP_CHAN_SEP in item_id:
                _, ret_id = item_id.split(EP_CHAN_SEP)
                return prov, ret_id
            return prov, item_id
        # not an item from subsonic provider, ignore...
        return None, item_id

    async def _preferred_mappings(
        self, mappings: list[ProviderMapping], user_id: str | None
    ) -> list[ProviderMapping]:
        """
        Return the mappings the given MA user may scrobble to, the ones they own first.

        :param mappings: The played item's Subsonic provider mappings.
        :param user_id: MA user id, or None when playback was not initiated by a user.
        """
        user = await self.mass.webserver.auth.get_user(user_id) if user_id else None
        allowed = visible_playback_sources(self.mass, user)
        owned = set(own_music_sources(self.mass, user))
        return sorted(
            (m for m in mappings if allowed is None or m.provider_instance in allowed),
            key=lambda m: m.provider_instance not in owned,
        )

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
