"""Scrobble tracks played through the NetEase Cloud Music provider back to NetEase."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, Final

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    ResourceTemporarilyUnavailable,
)

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
from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.media_items import ProviderMapping
    from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport
    from music_assistant_models.provider import ProviderManifest


SUPPORTED_FEATURES: Final[set[ProviderFeature]] = {ProviderFeature.SCROBBLE}
SUPPORTED_SCROBBLE_MEDIA_TYPES: Final[frozenset[MediaType]] = frozenset({MediaType.TRACK})

NETEASE_DOMAIN: Final[str] = "neteasecloudmusic"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return NeteaseScrobbleProvider(mass, manifest, config, SUPPORTED_FEATURES)


class NeteaseScrobbleProvider(PluginProvider):
    """Plugin provider to scrobble NetEase Cloud Music plays (listening check-in)."""

    _handler: NeteaseScrobbleHandler | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the configuration entries for this plugin."""
        return (*await ScrobblerConfig.get_shared_config_entries(self.mass, None),)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        self._handler = NeteaseScrobbleHandler(self.mass, self.logger, self.config)

    async def on_media_item_played(self, report: MediaItemPlaybackProgressReport) -> None:
        """Forward a playback progress report to the NetEase account that served the play."""
        if self._handler is not None:
            await self._handler.on_media_item_played(report)


class NeteaseScrobbleHandler(ScrobblerHelper):
    """Submit listening check-ins for NetEase Cloud Music plays."""

    # the NCM api client converts network/timeout errors to these
    scrobble_exceptions: ClassVar[tuple[type[Exception], ...]] = (
        InvalidDataError,
        ResourceTemporarilyUnavailable,
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

    async def _get_ncm_provider_and_track_id(
        self,
        media_type: MediaType,
        provider_instance_id_or_domain: str,
        item_id: str,
        user_id: str | None = None,
    ) -> tuple[NeteaseCloudMusicProvider | None, str]:
        """
        Return the NetEase provider to report to and the NetEase track id.

        The provider is None when the play did not stream from a loaded NetEase source.

        :param media_type: Media type of the played item.
        :param provider_instance_id_or_domain: Provider part of the played item's uri.
        :param item_id: Item id part of the played item's uri.
        :param user_id: MA user that initiated playback, used to prefer the account it owns.
        """
        if provider_instance_id_or_domain == "library":
            library_item = await self.mass.music.get_library_item_by_prov_id(
                media_type, item_id, provider_instance_id_or_domain
            )
            if library_item is None:
                return None, item_id
            ncm_mappings = [
                mapping
                for mapping in library_item.provider_mappings
                if mapping.provider_domain == NETEASE_DOMAIN
            ]
            if not ncm_mappings:
                # the library item is not linked to NetEase, nothing to report
                return None, item_id
            # a library item can map to several NetEase instances (one per account); prefer
            # the account the playing user owns so the play is not credited to another one
            for mapping in await self._preferred_mappings(ncm_mappings, user_id):
                prov = exact_provider(self.mass, mapping.provider_instance)
                if isinstance(prov, NeteaseCloudMusicProvider):
                    return prov, mapping.item_id
            return None, item_id
        # not a library item: only the exact instance that played it may be reported to
        prov = exact_provider(self.mass, provider_instance_id_or_domain)
        if isinstance(prov, NeteaseCloudMusicProvider):
            return prov, item_id
        return None, item_id

    async def _preferred_mappings(
        self, mappings: list[ProviderMapping], user_id: str | None
    ) -> list[ProviderMapping]:
        """
        Return the mappings the given MA user may scrobble to, the ones they own first.

        :param mappings: The played item's NetEase provider mappings.
        :param user_id: MA user id, or None when playback was not initiated by a user.
        """
        user = await self.mass.webserver.auth.get_user(user_id) if user_id else None
        allowed = visible_playback_sources(self.mass, user)
        owned = set(own_music_sources(self.mass, user))
        return sorted(
            (m for m in mappings if allowed is None or m.provider_instance in allowed),
            key=lambda m: m.provider_instance not in owned,
        )

    async def _scrobble(self, report: MediaItemPlaybackProgressReport) -> None:
        media_type, provider_instance_id_or_domain, item_id = await parse_uri(report.uri)
        prov, track_id = await self._get_ncm_provider_and_track_id(
            media_type, provider_instance_id_or_domain, item_id, report.userid
        )
        if not prov:
            return
        await prov.scrobble(track_id, report.seconds_played)
