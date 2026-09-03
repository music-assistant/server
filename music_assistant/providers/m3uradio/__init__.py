"""M3U / IPTV radio music provider for Music Assistant.

Imports radio stations from a self-hosted M3U (or IPTV) playlist and exposes
them as a native radio source: library sync, browse-by-group, and search.

Modelled on the built-in `radiobrowser` provider
(music_assistant/providers/radiobrowser/__init__.py).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING

from aiohttp import ClientError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


CONF_M3U_URL = "m3u_url"
CONF_GROUPS = "groups"

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}

# the display name starts after the first comma that is outside quoted
# attribute values (attributes like group-title may contain commas)
EXTINF_NAME_RE = re.compile(r'#EXTINF:[^,"]*(?:"[^"]*"[^,"]*)*,(?P<name>.*)$')
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return M3URadioProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return config entries to set up this provider."""
    return (
        ConfigEntry(
            key=CONF_M3U_URL,
            type=ConfigEntryType.STRING,
            label="M3U / IPTV playlist URL",
            required=True,
            description="Direct URL to your M3U playlist.",
        ),
        ConfigEntry(
            key=CONF_GROUPS,
            type=ConfigEntryType.STRING,
            label="Group filter (optional)",
            required=False,
            default_value="",
            description=(
                "Comma-separated group-title values to include. "
                "Leave blank to import every entry in the playlist."
            ),
        ),
    )


class M3URadioProvider(MusicProvider):
    """Music provider serving radio stations from an M3U playlist."""

    _stations: dict[str, dict[str, str]]

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._stations = {}
        await self._refresh_stations()

    async def sync_library(self, media_type: MediaType) -> None:
        """Run library sync for this provider."""
        # re-fetch the playlist so scheduled syncs pick up playlist changes
        await self._refresh_stations()
        await super().sync_library(media_type)

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Retrieve all library radio stations from the provider."""
        for st in self._stations.values():
            yield self._parse_radio(st)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        st = self._stations.get(prov_radio_id)
        if not st:
            await self._refresh_stations()
            st = self._stations.get(prov_radio_id)
        if not st:
            raise MediaNotFoundError(f"Station {prov_radio_id} not found")
        return self._parse_radio(st)

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """Perform search on the provider."""
        result = SearchResults()
        if MediaType.RADIO not in media_types:
            return result
        q = search_query.lower()
        matches = [
            self._parse_radio(st) for st in self._stations.values() if q in st["name"].lower()
        ]
        result.radio = matches[:limit]
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """Browse this provider's items, grouped by group-title."""
        subpath = "" if "://" not in path else path.split("://", 1)[1]
        if not subpath:
            groups = sorted({st["group"] for st in self._stations.values() if st["group"]})
            return [
                BrowseFolder(
                    item_id=_slug(group),
                    provider=self.domain,
                    path=f"{path}{_slug(group)}",
                    name=group,
                )
                for group in groups
            ]
        return [
            self._parse_radio(st) for st in self._stations.values() if _slug(st["group"]) == subpath
        ]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for a radio station."""
        st = self._stations.get(item_id)
        if not st:
            raise MediaNotFoundError(f"Station {item_id} not found")
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            audio_format=AudioFormat(content_type=_guess_content_type(st["url"])),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=st["url"],
            can_seek=False,
            allow_seek=False,
        )

    async def _refresh_stations(self) -> None:
        """Fetch the playlist and rebuild the station index."""
        url = self.config.get_value(CONF_M3U_URL)
        if not url:
            self.logger.warning("No M3U URL configured")
            self._stations = {}
            return
        groups_raw = self.config.get_value(CONF_GROUPS) or ""
        group_filter = {g.strip().lower() for g in str(groups_raw).split(",") if g.strip()}
        try:
            async with self.mass.http_session.get(str(url)) as resp:
                resp.raise_for_status()
                content = await resp.text()
        except (TimeoutError, ClientError) as err:
            raise ProviderUnavailableError(f"Failed to fetch M3U playlist: {err}") from err
        index: dict[str, dict[str, str]] = {}
        for st in parse_m3u(content):
            if group_filter and st["group"].lower() not in group_filter:
                continue
            index[st["id"]] = st
        self._stations = index
        self.logger.info("Loaded %d stations from M3U", len(index))

    def _parse_radio(self, st: dict[str, str]) -> Radio:
        """Build a Radio media item from a parsed station dict."""
        radio = Radio(
            item_id=st["id"],
            provider=self.domain,
            name=st["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=st["id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if st.get("logo"):
            radio.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=st["logo"],
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return radio


def parse_m3u(content: str) -> list[dict[str, str]]:
    """Parse #EXTM3U content into a list of station dicts.

    Each dict has: id, name, url, logo, group, tvg_id.
    """
    stations: list[dict[str, str]] = []
    name = logo = group = tvg_id = ""
    pending = False
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.upper().startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            attrs = dict(ATTR_RE.findall(line))
            logo = attrs.get("tvg-logo", "")
            group = attrs.get("group-title", "")
            tvg_id = attrs.get("tvg-id", "")
            match = EXTINF_NAME_RE.search(line)
            name = match.group("name").strip() if match else ""
            pending = True
        elif line.startswith("#"):
            continue
        elif pending:
            url = line
            sid = tvg_id.strip() or _hash_id(name, url)
            stations.append(
                {
                    "id": sid,
                    "name": name or url,
                    "url": url,
                    "logo": logo,
                    "group": group,
                    "tvg_id": tvg_id,
                }
            )
            name = logo = group = tvg_id = ""
            pending = False
    return stations


def _hash_id(name: str, url: str) -> str:
    return hashlib.sha1(f"{name}|{url}".encode()).hexdigest()[:16]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "ungrouped"


def _guess_content_type(url: str) -> ContentType:
    lowered = url.lower()
    for ext, codec in (
        (".mp3", "mp3"),
        (".aac", "aac"),
        (".m4a", "aac"),
        (".ogg", "ogg"),
        (".opus", "ogg"),
        (".flac", "flac"),
    ):
        if ext in lowered:
            return ContentType.try_parse(codec)
    return ContentType.UNKNOWN
