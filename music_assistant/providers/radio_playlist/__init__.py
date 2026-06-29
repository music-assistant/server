"""
Radio Playlists provider for Music Assistant.

Generates dynamic "radio" playlists from a seed media item (artist / album / track / genre /
playlist) — a mix of the seed's own tracks and similar tracks. A radio playlist is a normal dynamic
playlist (``is_dynamic=True``): the queue and the rest of Music Assistant treat it exactly like any
other provider's dynamic playlist (a station, a smart playlist). The playlist's ``item_id`` is the
seed item's own URI, so ``radio_playlist://playlist/<seed-uri>`` round-trips straight back to the
seed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import (
    BrowseFolder,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import DYNAMIC_PLAYLIST_SAMPLE_SIZE
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RadioPlaylistProvider(mass, manifest, config, set())


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider (none needed)."""
    return ()


def radio_playlist_uri(seed: MediaItemType) -> str:
    """
    Return the radio-playlist URI for a seed media item.

    :param seed: The media item (artist/album/track/genre/playlist) to base the radio playlist on.
    """
    return f"radio_playlist://playlist/{seed.uri}"


class RadioPlaylistProvider(PluginProvider):
    """Always-on provider that generates dynamic radio playlists from a seed media item."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return False; this provider only generates playlists from other providers' tracks."""
        return False

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """
        Return the (virtual, dynamic) radio playlist for the given seed.

        :param prov_playlist_id: The seed item's URI (raw or url-encoded).
        """
        seed = await self._resolve_seed(prov_playlist_id)
        playlist = Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=f"{seed.name} Radio",
            provider_mappings={
                ProviderMapping(
                    item_id=prov_playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=True,
                )
            },
        )
        playlist.is_dynamic = True
        if images := getattr(seed.metadata, "images", None):
            playlist.metadata = MediaItemMetadata(images=UniqueList(images))
        return playlist

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """
        Return a fresh sample of the radio playlist's tracks (the seed's own tracks + similar).

        :param prov_playlist_id: The seed item's URI (raw or url-encoded).
        :param page: Pagination; a dynamic playlist returns its full fresh batch on page 0 only.
        """
        if page > 0:
            return []
        seed = await self._resolve_seed(prov_playlist_id)
        try:
            return await self.mass.music.get_dynamic_radio_tracks(
                [seed], include_base_tracks=True, target_size=DYNAMIC_PLAYLIST_SAMPLE_SIZE
            )
        except MusicAssistantError:
            return []

    async def _resolve_seed(self, prov_playlist_id: str) -> MediaItemType:
        """Resolve a radio-playlist item id (the seed's URI, raw or url-encoded) to the seed item."""
        seed_uri = prov_playlist_id if "://" in prov_playlist_id else unquote(prov_playlist_id)
        try:
            seed = await self.mass.music.get_item_by_uri(seed_uri)
        except MusicAssistantError as err:
            raise MediaNotFoundError(f"Radio playlist seed not found: {seed_uri}") from err
        if isinstance(seed, BrowseFolder):
            raise MediaNotFoundError(f"Radio playlist seed is not a media item: {seed_uri}")
        return seed
