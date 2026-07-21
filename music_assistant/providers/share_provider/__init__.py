"""Share Links plugin for Music Assistant.

Generates and resolves provider-agnostic share links for tracks, artists and
playlists. Links encode item metadata (ISRC, MusicBrainz ID, title, artist,
cover art URL) as base64-encoded JSON in the URL fragment, so no server-side
storage is required.

Link format:
    https://<share_base_url>/#<base64url-encoded-json>

The share landing page (hosted at share_base_url) decodes the fragment,
shows a preview card, and offers an "Open in Music Assistant" button that
routes the payload to this plugin's resolve endpoint on the local MA instance.
"""

from __future__ import annotations

import base64
import json
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import MediaItemType, Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_SHARE_BASE_URL = "share_base_url"
DEFAULT_SHARE_BASE_URL = "https://share.music-assistant.io"

LOGGER = logging.getLogger(__name__)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return ShareProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return config entries to set up this provider."""
    return (
        ConfigEntry(
            key=CONF_SHARE_BASE_URL,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_SHARE_BASE_URL,
            label="Share base URL",
            description=(
                "Base URL of the share landing page. "
                "Change this if you host your own share page."
            ),
        ),
    )


@dataclass
class SharePayload:
    """Provider-agnostic share payload encoded in the share link."""

    v: int  # schema version
    type: str  # "track" | "artist" | "playlist"
    title: str
    artist: str | None = None  # primary artist name (tracks/albums)
    album: str | None = None  # album name (tracks)
    isrc: str | None = None  # ISRC for tracks (most reliable cross-provider ID)
    mbid: str | None = None  # MusicBrainz recording/artist/release-group ID
    art: str | None = None  # thumbnail URL for the share preview card

    def to_base64(self) -> str:
        """Encode payload as URL-safe base64 JSON."""
        data = {k: v for k, v in asdict(self).items() if v is not None}
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()

    @classmethod
    def from_base64(cls, encoded: str) -> SharePayload:
        """Decode payload from URL-safe base64 JSON."""
        data = json.loads(base64.urlsafe_b64decode(encoded + "=="))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class ShareProvider(PluginProvider):
    """Share Links plugin provider."""

    async def handle_async_init(self) -> None:
        """Register API commands on startup."""
        self.mass.register_api_command("share/generate", self.generate_share_link)
        self.mass.register_api_command("share/resolve", self.resolve_share_link)

    async def generate_share_link(self, uri: str) -> str:
        """
        Generate a shareable link for a given MA item URI.

        :param uri: The MA item URI (e.g. ``spotify://track/3n3Ppam7vgaVa1iaRUIOKE``).
        :return: A fully-qualified share URL with the payload in the fragment.
        """
        item = await self.mass.music.get_item_by_uri(uri)
        payload = self._build_payload(item)
        base_url = self.config.get_value(CONF_SHARE_BASE_URL) or DEFAULT_SHARE_BASE_URL
        return f"{base_url}/#{payload.to_base64()}"

    async def resolve_share_link(self, encoded: str) -> MediaItemType | None:
        """
        Resolve a share payload to a local MA item.

        Resolution order for tracks:
        1. ISRC lookup across all music providers that support it.
        2. Title + artist text search as fallback.

        :param encoded: Base64-encoded share payload (the URL fragment).
        :return: The resolved MA item, or None if nothing matched.
        """
        try:
            payload = SharePayload.from_base64(encoded)
        except Exception:
            LOGGER.warning("Could not decode share payload: %s", encoded)
            return None

        if payload.type == "track":
            return await self._resolve_track(payload)
        if payload.type == "artist":
            return await self._resolve_artist(payload)

        return None

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _build_payload(self, item: MediaItemType) -> SharePayload:
        """Build a SharePayload from a MediaItem."""
        media_type = item.media_type.value
        artist_str: str | None = None
        album_str: str | None = None
        isrc: str | None = None
        mbid: str | None = None
        art: str | None = None

        if item.image:
            art = item.image.path

        if media_type == "track":
            from music_assistant_models.media_items import Track
            track: Track = item  # type: ignore[assignment]
            isrc = track.get_external_id(ExternalID.ISRC)
            mbid = track.get_external_id(ExternalID.MB_RECORDING)
            artist_str = track.artist_str or None
            if track.album:
                album_str = track.album.name

        elif media_type == "artist":
            mbid = item.get_external_id(ExternalID.MB_ARTIST)

        elif media_type == "album":
            from music_assistant_models.media_items import Album
            album: Album = item  # type: ignore[assignment]
            mbid = album.get_external_id(ExternalID.MB_ALBUM)
            artist_str = album.artist_str or None

        return SharePayload(
            v=1,
            type=media_type,
            title=item.name,
            artist=artist_str,
            album=album_str,
            isrc=isrc,
            mbid=mbid,
            art=art,
        )

    async def _resolve_track(self, payload: SharePayload) -> MediaItemType | None:
        """Resolve a track share payload."""
        # 1. Try ISRC lookup across all providers
        if payload.isrc:
            result = await self._lookup_by_isrc(payload.isrc)
            if result:
                return result

        # 2. Fall back to text search
        if payload.title:
            query = payload.title
            if payload.artist:
                query = f"{payload.artist} {payload.title}"
            results = await self.mass.music.search(query, media_types=[MediaType.TRACK], limit=5)
            if results.tracks:
                return results.tracks[0]

        return None

    async def _resolve_artist(self, payload: SharePayload) -> MediaItemType | None:
        """Resolve an artist share payload."""
        results = await self.mass.music.search(
            payload.title, media_types=[MediaType.ARTIST], limit=5
        )
        if results.artists:
            return results.artists[0]
        return None

    async def _lookup_by_isrc(self, isrc: str) -> MediaItemType | None:
        """Look up a track by ISRC across all available music providers."""
        for provider in self.mass.music.get_unique_providers():
            prov = self.mass.get_provider(provider)
            if prov is None:
                continue
            # use provider search with ISRC query — most providers support isrc:XXXX syntax
            with suppress(Exception):
                results = await prov.search(  # type: ignore[attr-defined]
                    f"isrc:{isrc}", [MediaType.TRACK], limit=1
                )
                if results and results.tracks:
                    return results.tracks[0]
        return None
