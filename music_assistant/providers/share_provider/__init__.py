"""
Share Links plugin for Music Assistant.

Generates and resolves provider-agnostic share links for tracks, albums and
artists. Links encode compressed, versioned JSON metadata in the URL
fragment — no server-side storage required.

Link format:
    https://<share_base_url>/#1<base64url-encoded-raw-deflate-json>
"""

from __future__ import annotations

import binascii
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ExternalID, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, BrowseFolder, Track

from music_assistant.helpers.playlists import PlaylistItem
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.share_provider.links import (
    build_provider_links,
    is_valid_artwork_url,
)
from music_assistant.providers.share_provider.payload import (
    SUPPORTED_SHARE_MEDIA_TYPES,
    SharePayload,
)
from music_assistant.providers.share_provider.resolver import ShareLinkResolver

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_SHARE_BASE_URL = "share_base_url"
DEFAULT_SHARE_BASE_URL = "https://app.music-assistant.io/share"
LOGGER = logging.getLogger(__name__)
__all__ = ["SharePayload", "ShareProvider", "setup"]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return ShareProvider(mass, manifest, config)


class ShareProvider(PluginProvider):
    """Share Links plugin provider."""

    _unregister_api_commands: list[Callable[[], None]]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the configurable share-link base URL."""
        return (
            ConfigEntry(
                key=CONF_SHARE_BASE_URL,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_SHARE_BASE_URL,
                advanced=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Register authenticated share commands on startup."""
        self._unregister_api_commands = []
        try:
            self._unregister_api_commands.append(
                self.mass.register_api_command(
                    "share/generate",
                    self.generate_share_link,
                    required_scope=Scope.LIBRARY_READ,
                )
            )
            self._unregister_api_commands.append(
                self.mass.register_api_command(
                    "share/resolve",
                    self.resolve_share_link,
                    required_scope=Scope.LIBRARY_READ,
                )
            )
        except Exception:
            await self.unload()
            raise

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands on unload."""
        for unregister in getattr(self, "_unregister_api_commands", []):
            unregister()
        self._unregister_api_commands = []

    async def generate_share_link(self, uri: str) -> str:
        """
        Generate a shareable link for a given MA item URI.

        :param uri: The MA item URI.
        :return: A fully-qualified share URL with the payload in the fragment.
        """
        item = await self.mass.music.get_item_by_uri(uri)
        if isinstance(item, BrowseFolder):
            raise InvalidDataError("Sharing browse folders is not supported")
        payload = await self._build_payload(item)
        try:
            encoded_payload = payload.to_base64()
        except ValueError as err:
            raise InvalidDataError(str(err)) from err
        base_url = self.config.get_value(CONF_SHARE_BASE_URL) or DEFAULT_SHARE_BASE_URL
        return f"{base_url}/#{encoded_payload}"

    async def resolve_share_link(
        self, encoded: str, allow_search: bool = True
    ) -> MediaItemType | None:
        """
        Resolve a share payload to a local MA item.

        :param encoded: Versioned, compressed base64url share payload.
        :param allow_search: Allow text search when no external ID matches.
        :return: The resolved MA item, or None if nothing matched.
        """
        try:
            payload = SharePayload.from_base64(encoded)
        except (binascii.Error, TypeError, ValueError) as err:
            LOGGER.debug("Could not decode share payload: %s", err)
            return None

        return await self._resolve_media_item(payload, allow_search=allow_search)

    async def _build_payload(self, item: MediaItemType) -> SharePayload:
        """Build a SharePayload from a MediaItem."""
        media_type = item.media_type.value
        artist_str: str | None = None
        artist_credits: list[str] | None = None
        version_str: str | None = None
        album_str: str | None = None
        isrc: str | None = None
        mbid: str | None = None
        mb_track: str | None = None
        barcode: str | None = None
        art_url: str | None = None
        duration: int | None = None
        provider_urls: dict[str, str] | None = None

        if item.media_type not in SUPPORTED_SHARE_MEDIA_TYPES:
            raise InvalidDataError(f"Sharing {media_type} items is not supported")

        if item.image and item.image.remotely_accessible and is_valid_artwork_url(item.image.path):
            art_url = item.image.path

        if item.media_type == MediaType.TRACK:
            version_str = getattr(item, "version", None) or None
            if isinstance(item, Track):
                duration = item.duration
                artist_credits = [artist.name for artist in item.artists] or None
                artist_str = ", ".join(artist_credits) if artist_credits else None
            isrc = item.get_external_id(ExternalID.ISRC)
            mbid = item.get_external_id(ExternalID.MB_RECORDING)
            mb_track = item.get_external_id(ExternalID.MB_TRACK)
            if hasattr(item, "album") and item.album:
                album_str = item.album.name
            provider_urls = build_provider_links(item)

        elif item.media_type == MediaType.ARTIST:
            mbid = item.get_external_id(ExternalID.MB_ARTIST)
            provider_urls = build_provider_links(item)

        elif item.media_type == MediaType.ALBUM:
            version_str = getattr(item, "version", None) or None
            mbid = item.get_external_id(ExternalID.MB_ALBUM)
            barcode = item.get_external_id(ExternalID.BARCODE)
            if isinstance(item, Album):
                artist_credits = [artist.name for artist in item.artists] or None
                artist_str = ", ".join(artist_credits) if artist_credits else None
            provider_urls = build_provider_links(item)

        return SharePayload(
            type=cast("Literal['track', 'album', 'artist']", media_type),
            title=item.name,
            artist=artist_str,
            artists=artist_credits,
            version=version_str,
            album=album_str,
            isrc=isrc,
            mbid=mbid,
            mb_track=mb_track,
            barcode=barcode,
            art_url=art_url,
            provider_urls=provider_urls or None,
            duration=duration,
        )

    async def _resolve_media_item(
        self,
        payload: SharePayload,
        allow_search: bool = True,
    ) -> MediaItemType | None:
        """Resolve a payload using the dedicated share-link resolver."""
        return await ShareLinkResolver(self.mass).resolve_media_item(
            payload,
            allow_search=allow_search,
        )

    async def _searchable_music_provider_instances(self) -> set[str]:
        return await ShareLinkResolver(self.mass)._searchable_music_provider_instances()

    @staticmethod
    def _playlist_item_from_payload(payload: SharePayload) -> PlaylistItem:
        return ShareLinkResolver._playlist_item_from_payload(payload)
