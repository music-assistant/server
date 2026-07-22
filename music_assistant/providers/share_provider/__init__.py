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
import base64
import json
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ExternalID, MediaType

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import MediaItemType, Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_SHARE_BASE_URL = "share_base_url"
DEFAULT_SHARE_BASE_URL = "https://share.music-assistant.io"
HTTP_ROUTE = "/api/share/resolve"

LOGGER = logging.getLogger(__name__)

# Known web URL schemas per provider domain, keyed by (provider_domain, media_type).
# {id} is replaced with the provider item_id from the provider_mappings.
PROVIDER_URL_SCHEMAS: dict[tuple[str, str], str] = {
    ("spotify", "track"): "https://open.spotify.com/track/{id}",
    ("spotify", "artist"): "https://open.spotify.com/artist/{id}",
    ("tidal", "track"): "https://tidal.com/browse/track/{id}",
    ("tidal", "artist"): "https://tidal.com/browse/artist/{id}",
    ("qobuz", "track"): "https://open.qobuz.com/track/{id}",
    ("qobuz", "artist"): "https://open.qobuz.com/artist/{id}",
    ("ytmusic", "track"): "https://music.youtube.com/watch?v={id}",
    ("ytmusic", "artist"): "https://music.youtube.com/channel/{id}",
    ("deezer", "track"): "https://www.deezer.com/track/{id}",
    ("deezer", "artist"): "https://www.deezer.com/artist/{id}",
    ("apple_music", "track"): "https://music.apple.com/album/x/{id}",
    ("apple_music", "artist"): "https://music.apple.com/artist/x/{id}",
}


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
            description="Base URL of the share landing page.",
        ),
    )


@dataclass
class TrackStub:
    """Minimal track representation inside a playlist payload."""

    title: str
    artist: str | None = None
    isrc: str | None = None


@dataclass
class SharePayload:
    """Provider-agnostic share payload encoded in the share link."""

    v: int
    type: str  # "track" | "artist" | "playlist"
    title: str
    artist: str | None = None
    album: str | None = None
    isrc: str | None = None
    mbid: str | None = None
    art: str | None = None
    # Direct provider web URLs keyed by provider domain (tracks only).
    links: dict[str, str] | None = None
    # Minimal track list (playlists only).
    tracks: list[dict[str, Any]] | None = None

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
        self._unregister_route = self.mass.webserver.register_dynamic_route(
            HTTP_ROUTE, self._http_resolve, "POST"
        )
        self.mass.webserver.register_dynamic_route(HTTP_ROUTE, self._http_resolve, "OPTIONS")
        LOGGER.debug("Registered HTTP share resolve endpoint at %s", HTTP_ROUTE)

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister HTTP route on unload."""
        if callable(self._unregister_route):
            self._unregister_route()

    # ------------------------------------------------------------------
    # HTTP endpoint
    # ------------------------------------------------------------------

    async def _http_resolve(self, request: web.Request) -> web.Response:
        """Handle POST /api/share/resolve with CORS headers."""
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
        payload = await self._build_payload(item)
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
        if payload.type == "playlist":
            return await self._resolve_playlist(payload)
        return None

    # ------------------------------------------------------------------
    # private helpers — payload building
    # ------------------------------------------------------------------

    async def _build_payload(self, item: MediaItemType) -> SharePayload:
        """Build a SharePayload from a MediaItem."""
        media_type = item.media_type.value
        artist_str: str | None = None
        album_str: str | None = None
        isrc: str | None = None
        mbid: str | None = None
        art: str | None = None
        links: dict[str, str] | None = None
        tracks: list[dict[str, Any]] | None = None

        if item.image and item.image.remotely_accessible:
            art = item.image.path

        if media_type == "track":
            isrc = item.get_external_id(ExternalID.ISRC)
            mbid = item.get_external_id(ExternalID.MB_RECORDING)
            if hasattr(item, "artist_str"):
                artist_str = item.artist_str or None
            if hasattr(item, "album") and item.album:
                album_str = item.album.name
            links = self._build_provider_links(item, media_type)
            await self._enrich_links_via_isrc(links, isrc, item.name, artist_str)

        elif media_type == "artist":
            mbid = item.get_external_id(ExternalID.MB_ARTIST)
            links = self._build_provider_links(item, media_type)

        elif media_type == "album":
            mbid = item.get_external_id(ExternalID.MB_ALBUM)
            if hasattr(item, "artist_str"):
                artist_str = item.artist_str or None

        elif media_type == "playlist":
            tracks = await self._build_playlist_tracks(item)

        return SharePayload(
            v=1,
            type=media_type,
            title=item.name,
            artist=artist_str,
            album=album_str,
            isrc=isrc,
            mbid=mbid,
            art=art,
            links=links or None,
            tracks=tracks,
        )

    def _build_provider_links(self, item: MediaItemType, media_type: str) -> dict[str, str]:
        """Build direct provider web URLs from provider_mappings."""
        links: dict[str, str] = {}
        if not hasattr(item, "provider_mappings"):
            return links
        for mapping in item.provider_mappings:
            domain = mapping.provider_domain
            schema = PROVIDER_URL_SCHEMAS.get((domain, media_type))
            if schema:
                links[domain] = schema.format(id=mapping.item_id)
        return links

    async def _enrich_links_via_isrc(
        self, links: dict[str, str], isrc: str | None, title: str, artist: str | None
    ) -> None:
        """
        Add missing provider links using keyless public APIs and active MA providers.

        - Deezer: keyless ISRC lookup via public API.
        - Apple Music: keyless iTunes Search API (title + artist).
        - YouTube Music: via the active ytmusic MA provider if configured.

        Modifies ``links`` in place — only adds entries for providers not already present.
        """
        if not isrc and not title:
            return

        q = f"{artist} {title}" if artist else title

        # Deezer — keyless ISRC lookup
        if "deezer" not in links and isrc:
            with suppress(Exception):
                async with self.mass.http_session.get(
                    f"https://api.deezer.com/track/isrc:{isrc}",
                    timeout=4,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if link := data.get("link"):
                            links["deezer"] = link

        # Apple Music — keyless iTunes Search
        if "apple_music" not in links:
            with suppress(Exception):
                async with self.mass.http_session.get(
                    "https://itunes.apple.com/search",
                    params={"term": q, "entity": "song", "limit": 5},
                    timeout=4,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for r in data.get("results", []):
                            if r.get("kind") == "song":
                                links["apple_music"] = r["trackViewUrl"]
                                break

        # YouTube Music — via active ytmusic MA provider
        if "ytmusic" not in links:
            ytmusic_prov = next(
                (
                    p for p in (self.mass.get_provider(pid) for pid in self.mass.music.get_unique_providers())
                    if p and p.domain == "ytmusic"
                ),
                None,
            )
            if ytmusic_prov:
                with suppress(Exception):
                    results = await ytmusic_prov.search(q, [MediaType.TRACK], limit=3)  # type: ignore[attr-defined]
                    for track in results.tracks or []:
                        for mapping in track.provider_mappings:
                            if mapping.provider_domain == "ytmusic":
                                links["ytmusic"] = f"https://music.youtube.com/watch?v={mapping.item_id}"
                                break
                        if "ytmusic" in links:
                            break

    async def _build_playlist_tracks(self, playlist: MediaItemType) -> list[dict[str, Any]]:
        """Fetch and serialize playlist tracks as minimal stubs (no provider links)."""
        result: list[dict[str, Any]] = []
        with suppress(Exception):
            tracks = await self.mass.music.playlists.tracks(playlist.item_id, playlist.provider)
            for track in tracks:
                stub: dict[str, Any] = {"title": track.name}
                if hasattr(track, "artist_str") and track.artist_str:
                    stub["artist"] = track.artist_str
                isrc = track.get_external_id(ExternalID.ISRC)
                if isrc:
                    stub["isrc"] = isrc
                result.append(stub)
        return result

    # ------------------------------------------------------------------
    # private helpers — resolution
    # ------------------------------------------------------------------

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

    async def _resolve_playlist(self, payload: SharePayload) -> MediaItemType | None:
        """
        Resolve a playlist payload by searching for each track and creating
        a new MA playlist with the matches.
        """
        if not payload.tracks:
            return None

        # Resolve tracks in parallel (capped to avoid hammering the providers)
        sem = asyncio.Semaphore(5)

        async def _resolve_one(stub: dict[str, Any]) -> MediaItemType | None:
            async with sem:
                title = stub.get("title", "")
                artist = stub.get("artist")
                isrc = stub.get("isrc")
                if isrc:
                    result = await self._lookup_by_isrc(isrc)
                    if result:
                        return result
                if title:
                    q = f"{artist} {title}" if artist else title
                    sr = await self.mass.music.search(q, media_types=[MediaType.TRACK], limit=3)
                    if sr.tracks:
                        return sr.tracks[0]
            return None

        resolved = await asyncio.gather(*[_resolve_one(t) for t in payload.tracks])
        matched = [t for t in resolved if t is not None]
        if not matched:
            return None

        # Create a new library playlist with the resolved tracks
        new_playlist = await self.mass.music.playlists.create_playlist(payload.title)
        for track in matched:
            with suppress(Exception):
                await self.mass.music.playlists.add_playlist_tracks(
                    new_playlist.item_id, [track.uri]
                )
        return new_playlist

    async def _lookup_by_isrc(self, isrc: str) -> MediaItemType | None:
        """Look up a track by ISRC via each provider's search."""
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
