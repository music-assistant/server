"""
Share Links plugin for Music Assistant.

Generates and resolves provider-agnostic share links for tracks, artists and
playlists. Links encode item metadata (ISRC, MusicBrainz ID, title, artist,
cover art URL) as base64-encoded JSON in the URL fragment, so no server-side
storage is required.

Link format:
    https://<share_base_url>/#<base64url-encoded-json>

The share landing page (hosted at share_base_url) decodes the fragment,
shows a preview card, and offers an "Open in Music Assistant" button that
calls the HTTP endpoint registered by this plugin on the local MA instance.
"""

from __future__ import annotations

import base64
import json
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ExternalID, MediaType

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_SHARE_BASE_URL = "share_base_url"
DEFAULT_SHARE_BASE_URL = "https://share.music-assistant.io"
HTTP_ROUTE = "/api/share/resolve"

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
    artist: str | None = None
    album: str | None = None
    isrc: str | None = None
    mbid: str | None = None
    art: str | None = None

    def to_base64(self) -> str:
        """Encode payload as URL-safe base64 JSON."""
        data = {k: v for k, v in asdict(self).items() if v is not None}
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()

    @classmethod
    def from_base64(cls, encoded: str) -> SharePayload:
        """Decode payload from URL-safe base64 JSON."""
        padded = encoded + "==" * ((4 - len(encoded) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class ShareProvider(PluginProvider):
    """Share Links plugin provider."""

    _unregister_route: object | None = None

    async def handle_async_init(self) -> None:
        """Register API commands and HTTP endpoint on startup."""
        self.mass.register_api_command("share/generate", self.generate_share_link)
        self.mass.register_api_command("share/resolve", self.resolve_share_link)
        # Register a plain HTTP POST endpoint so the share landing page can
        # call it directly without a WebSocket connection.
        self._unregister_route = self.mass.webserver.register_dynamic_route(
            HTTP_ROUTE, self._http_resolve, "POST"
        )
        # Also allow CORS preflight
        self.mass.webserver.register_dynamic_route(HTTP_ROUTE, self._http_resolve, "OPTIONS")
        LOGGER.debug("Registered HTTP share resolve endpoint at %s", HTTP_ROUTE)

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister HTTP route on unload."""
        if callable(self._unregister_route):
            self._unregister_route()

    # ------------------------------------------------------------------
    # HTTP endpoint (called by the share landing page)
    # ------------------------------------------------------------------

    async def _http_resolve(self, request: web.Request) -> web.Response:
        """
        Handle POST /api/share/resolve.

        Expected JSON body: {"encoded": "<base64-payload>"}
        Returns resolved MA item as JSON, or 404.
        Includes CORS headers so the share landing page (any origin) can call it.
        """
        cors_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=cors_headers)

        try:
            body = await request.json()
            encoded = body.get("encoded", "")
        except Exception:
            return web.Response(status=400, text="Invalid JSON body", headers=cors_headers)

        item = await self.resolve_share_link(encoded)
        if item is None:
            return web.Response(status=404, text="Not found", headers=cors_headers)

        return web.Response(
            content_type="application/json",
            text=json.dumps(item.to_dict()),
            headers=cors_headers,
        )

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
        payload = self._build_payload(item)
        base_url = self.config.get_value(CONF_SHARE_BASE_URL) or DEFAULT_SHARE_BASE_URL
        return f"{base_url}/#{payload.to_base64()}"

    async def resolve_share_link(self, encoded: str) -> MediaItemType | None:
        """
        Resolve a share payload to a local MA item.

        :param encoded: Base64-encoded share payload.
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
            # Only include art URLs that are publicly accessible from anywhere.
            # Local imageproxy URLs (e.g. http://localhost:8095/imageproxy/...)
            # are useless to the recipient of the share link.
            if item.image.remotely_accessible:
                art = item.image.path

        if media_type == "track":
            isrc = item.get_external_id(ExternalID.ISRC)
            mbid = item.get_external_id(ExternalID.MB_RECORDING)
            if hasattr(item, "artist_str"):
                artist_str = item.artist_str or None
            if hasattr(item, "album") and item.album:
                album_str = item.album.name
        elif media_type == "artist":
            mbid = item.get_external_id(ExternalID.MB_ARTIST)
        elif media_type == "album":
            mbid = item.get_external_id(ExternalID.MB_ALBUM)
            if hasattr(item, "artist_str"):
                artist_str = item.artist_str or None

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
        if payload.isrc:
            result = await self._lookup_by_isrc(payload.isrc)
            if result:
                return result

        if payload.title:
            query = f"{payload.artist} {payload.title}" if payload.artist else payload.title
            results = await self.mass.music.search(query, media_types=[MediaType.TRACK], limit=5)
            if results.tracks:
                return results.tracks[0]
        return None

    async def _resolve_artist(self, payload: SharePayload) -> MediaItemType | None:
        """Resolve an artist share payload."""
        results = await self.mass.music.search(
            payload.title, media_types=[MediaType.ARTIST], limit=5
        )
        return results.artists[0] if results.artists else None

    async def _lookup_by_isrc(self, isrc: str) -> MediaItemType | None:
        """Look up a track by ISRC via each provider's search (isrc:<code> syntax)."""
        for provider_id in self.mass.music.get_unique_providers():
            prov = self.mass.get_provider(provider_id)
            if prov is None:
                continue
            with suppress(Exception):
                results = await prov.search(  # type: ignore[attr-defined]
                    f"isrc:{isrc}", [MediaType.TRACK], limit=1
                )
                if results and results.tracks:
                    return results.tracks[0]
        return None
