"""
Share Links plugin for Music Assistant.

Generates and resolves provider-agnostic share links for tracks, artists and
playlists. Links encode item metadata as base64-encoded JSON in the URL
fragment — no server-side storage required.

Link format:
    https://<share_base_url>/#<base64url-encoded-json>
"""

from __future__ import annotations

import asyncio
import binascii
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ExternalID,
    MediaType,
    PlaylistMatchPolicy,
)
from music_assistant_models.errors import (
    InvalidDataError,
)
from music_assistant_models.media_items import (
    Album,
    BrowseFolder,
    ItemMapping,
    Track,
)

from music_assistant.helpers.playlists import (
    PlaylistItem,
    construct_media_item_from_playlist_item,
    generate_m3u,
)
from music_assistant.models.music_provider import PROVIDER_FETCH_ERRORS
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.share_provider.links import (
    build_provider_refs,
    compact_artwork_ref,
    is_valid_artwork_url,
)
from music_assistant.providers.share_provider.payload import (
    SUPPORTED_SHARE_MEDIA_TYPES,
    SharePayload,
    TrackStub,
)
from music_assistant.providers.share_provider.resolver import ShareLinkResolver

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import MediaItemType, Playlist
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_SHARE_BASE_URL = "share_base_url"
DEFAULT_SHARE_BASE_URL = "https://share.music-assistant.io"
# Leave room for the API command envelope under the 64 KiB fallback DataChannel limit.
MAX_SHARE_PLAYLIST_PAYLOAD_SIZE = 48 * 1024
# Provider links are optional, so third-party outages must not hold up share generation.
LOGGER = logging.getLogger(__name__)
__all__ = ["SharePayload", "ShareProvider", "TrackStub", "setup"]


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
            self._unregister_api_commands.append(
                self.mass.register_api_command(
                    "share/import_playlist",
                    self.import_shared_playlist,
                    required_scope=Scope.LIBRARY_WRITE,
                )
            )
            self._unregister_api_commands.append(
                self.mass.register_api_command(
                    "share/preview_playlist",
                    self.preview_shared_playlist,
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

    # ------------------------------------------------------------------
    # WebSocket API commands
    # ------------------------------------------------------------------

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
        encoded_payload = payload.to_base64()
        if payload.type == "playlist" and len(encoded_payload) > MAX_SHARE_PLAYLIST_PAYLOAD_SIZE:
            raise InvalidDataError(
                "This playlist is too large to share.",
                translation_key="share_playlist_too_large",
                translation_owner=self.translation_owner,
            )
        base_url = self.config.get_value(CONF_SHARE_BASE_URL) or DEFAULT_SHARE_BASE_URL
        return f"{base_url}/#{encoded_payload}"

    async def resolve_share_link(
        self, encoded: str, allow_search: bool = True
    ) -> MediaItemType | None:
        """
        Resolve a share payload to a local MA item.

        :param encoded: Base64-encoded share payload.
        :param allow_search: Allow text search when no external ID matches.
        :return: The resolved MA item, or None if nothing matched.
        """
        try:
            payload = SharePayload.from_base64(encoded)
        except (binascii.Error, TypeError, ValueError) as err:
            LOGGER.debug("Could not decode share payload: %s", err)
            return None

        if payload.type == "playlist":
            return None
        return await self._resolve_media_item(payload, allow_search=allow_search)

    async def preview_shared_playlist(
        self, encoded: str, offset: int = 0, limit: int = 50
    ) -> list[Track]:
        """Return a read-only block of shared tracks, keeping unresolved entries."""
        if offset < 0 or limit < 1:
            raise InvalidDataError("Invalid share preview range")
        try:
            payload = SharePayload.from_base64(encoded)
        except (binascii.Error, TypeError, ValueError) as err:
            raise InvalidDataError("Invalid share payload") from err
        if payload.type != "playlist":
            raise InvalidDataError("Share payload is not a playlist")
        search_provider_instance_ids = await self._searchable_music_provider_instances()
        semaphore = asyncio.Semaphore(2)
        resolved_by_payload: dict[str, asyncio.Task[Track | None]] = {}

        async def resolve_payload(track_payload: SharePayload) -> Track | None:
            async with semaphore:
                try:
                    item = await self._resolve_media_item(
                        track_payload,
                        hydrate_track=False,
                        search_provider_instance_ids=search_provider_instance_ids,
                    )
                except PROVIDER_FETCH_ERRORS as err:
                    LOGGER.debug(
                        "Could not resolve shared preview track %s: %s",
                        track_payload.title,
                        err,
                    )
                    return None
            return item if isinstance(item, Track) else None

        async def resolve_track(index: int, stub: TrackStub) -> Track:
            track_payload = SharePayload(
                type="track",
                title=stub.title,
                artist=stub.artist,
                artists=stub.artists,
                version=stub.version,
                album=stub.album,
                isrc=stub.isrc,
                mbid=stub.mbid,
                mb_track=stub.mb_track,
                duration=stub.duration,
                provider_refs=stub.provider_refs,
            )
            encoded_track = track_payload.to_base64()
            task = resolved_by_payload.get(encoded_track)
            if task is None:
                task = asyncio.create_task(resolve_payload(track_payload))
                resolved_by_payload[encoded_track] = task
            item = await task
            if item is not None:
                return replace(item, position=index + 1)
            preview = construct_media_item_from_playlist_item(
                self._playlist_item_from_payload(track_payload), self.mass
            )
            if not isinstance(preview, Track):
                raise InvalidDataError("Invalid shared track preview")
            if track_payload.album:
                preview.album = ItemMapping(
                    item_id=track_payload.album,
                    provider=self.instance_id,
                    name=track_payload.album,
                    media_type=MediaType.ALBUM,
                )
            return replace(
                preview,
                item_id=f"share-preview://track/{encoded_track}",
                provider="builtin",
                uri=None,
                provider_mappings=set(),
                position=index + 1,
            )

        tracks = payload.tracks or []
        return list(
            await asyncio.gather(
                *(
                    resolve_track(offset + index, stub)
                    for index, stub in enumerate(tracks[offset : offset + limit])
                )
            )
        )

    async def import_shared_playlist(self, encoded: str) -> Playlist:
        """Import a playlist after the caller has confirmed the operation."""
        try:
            payload = SharePayload.from_base64(encoded)
        except (binascii.Error, TypeError, ValueError) as err:
            raise InvalidDataError(
                "Invalid share payload",
                translation_key="invalid_share_payload",
                translation_owner=self.translation_owner,
            ) from err
        if payload.type != "playlist":
            raise InvalidDataError(
                "Share payload is not a playlist",
                translation_key="share_payload_not_playlist",
                translation_owner=self.translation_owner,
            )
        if not payload.tracks:
            raise InvalidDataError(
                "Shared playlist contains no tracks",
                translation_key="share_playlist_empty",
                translation_owner=self.translation_owner,
            )

        playlist_items: list[PlaylistItem] = []
        for track in payload.tracks:
            track_payload = SharePayload(
                type="track",
                title=track.title,
                artist=track.artist,
                artists=track.artists,
                version=track.version,
                album=track.album,
                isrc=track.isrc,
                mbid=track.mbid,
                mb_track=track.mb_track,
                duration=track.duration,
                provider_refs=track.provider_refs,
            )
            playlist_items.append(
                self._playlist_item_from_payload(track_payload, include_isrc=False)
            )
        return await self.mass.music.playlists.import_playlist(
            generate_m3u(payload.title, playlist_items),
            library_matching=True,
            match_policy=PlaylistMatchPolicy.SAME_RECORDING,
        )

    # ------------------------------------------------------------------
    # private helpers — payload building
    # ------------------------------------------------------------------

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
        art_ref: str | None = None
        duration: int | None = None
        provider_refs: dict[str, str] | None = None
        tracks: list[TrackStub] | None = None

        if item.media_type not in SUPPORTED_SHARE_MEDIA_TYPES:
            raise InvalidDataError(f"Sharing {media_type} items is not supported")

        if item.image and item.image.remotely_accessible and is_valid_artwork_url(item.image.path):
            art_ref = compact_artwork_ref(item.image.path)

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
            provider_refs = build_provider_refs(item)

        elif item.media_type == MediaType.ARTIST:
            mbid = item.get_external_id(ExternalID.MB_ARTIST)
            provider_refs = build_provider_refs(item)

        elif item.media_type == MediaType.ALBUM:
            version_str = getattr(item, "version", None) or None
            mbid = item.get_external_id(ExternalID.MB_ALBUM)
            barcode = item.get_external_id(ExternalID.BARCODE)
            if isinstance(item, Album):
                artist_credits = [artist.name for artist in item.artists] or None
                artist_str = ", ".join(artist_credits) if artist_credits else None
            provider_refs = build_provider_refs(item)

        elif item.media_type == MediaType.PLAYLIST:
            tracks = await self._build_playlist_tracks(item)

        return SharePayload(
            type=cast("Literal['track', 'album', 'artist', 'playlist']", media_type),
            title=item.name,
            artist=artist_str,
            artists=artist_credits,
            version=version_str,
            album=album_str,
            isrc=isrc,
            mbid=mbid,
            mb_track=mb_track,
            barcode=barcode,
            art_ref=art_ref,
            provider_refs=provider_refs or None,
            tracks=tracks,
            duration=duration,
        )

    async def _build_playlist_tracks(self, playlist: MediaItemType) -> list[TrackStub]:
        """Fetch and serialize playlist tracks as minimal stubs (no provider links)."""
        result: list[TrackStub] = []
        for track in await self.mass.music.playlists.tracks(
            playlist.item_id, playlist.provider, limit=0
        ):
            if not isinstance(track, Track):
                continue
            result.append(
                TrackStub(
                    title=track.name,
                    artist=", ".join(artist.name for artist in track.artists) or None,
                    artists=[artist.name for artist in track.artists] or None,
                    version=track.version or None,
                    isrc=track.get_external_id(ExternalID.ISRC),
                    mbid=track.get_external_id(ExternalID.MB_RECORDING),
                    mb_track=track.get_external_id(ExternalID.MB_TRACK),
                    album=track.album.name if track.album else None,
                    duration=track.duration,
                    provider_refs=build_provider_refs(track),
                )
            )
        return result

    # ------------------------------------------------------------------
    # private helpers — resolution
    # ------------------------------------------------------------------

    async def _resolve_media_item(
        self,
        payload: SharePayload,
        allow_search: bool = True,
        hydrate_track: bool = True,
        search_provider_instance_ids: set[str] | None = None,
    ) -> MediaItemType | None:
        """Resolve a payload using the dedicated share-link resolver."""
        return await ShareLinkResolver(self.mass).resolve_media_item(
            payload,
            allow_search=allow_search,
            hydrate_track=hydrate_track,
            search_provider_instance_ids=search_provider_instance_ids,
        )

    async def _searchable_music_provider_instances(self) -> set[str]:
        return await ShareLinkResolver(self.mass)._searchable_music_provider_instances()

    def _track_from_payload(self, payload: SharePayload) -> Track:
        return ShareLinkResolver(self.mass)._track_from_payload(payload)

    @staticmethod
    def _playlist_item_from_payload(
        payload: SharePayload, include_isrc: bool = True
    ) -> PlaylistItem:
        return ShareLinkResolver._playlist_item_from_payload(payload, include_isrc)

    @staticmethod
    def _album_reference(payload: SharePayload) -> Album | ItemMapping:
        return ShareLinkResolver._album_reference(payload)

    @staticmethod
    def _matches_album_reference(
        payload: SharePayload,
        reference: Album | ItemMapping,
        candidate: Album | ItemMapping,
    ) -> bool:
        return ShareLinkResolver._matches_album_reference(payload, reference, candidate)

    @staticmethod
    def _artist_reference(payload: SharePayload) -> ItemMapping:
        return ShareLinkResolver._artist_reference(payload)
