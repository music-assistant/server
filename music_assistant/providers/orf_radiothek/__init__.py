"""ORF Radiothek / ORF Sound provider for Music Assistant.

Features:
- Live radios (ORF stations + privates) from ORF bundle.json
- ORF station logos from local provider media/<station>.png (served via resolve_image)
- Catch-up broadcasts exposed as Podcasts + PodcastEpisodes (last N days), auto-removed by sync
- ORF Sound “actual podcasts” (api 2.0) exposed as Podcasts + PodcastEpisodes (full feed)

Endpoints:
- bundle.json:
  https://orf.at/app-infos/sound/web/1.0/bundle.json?_o=sound.orf.at
- broadcasts by day:
  https://audioapi.orf.at/<station>/api/json/5.0/broadcasts/<YYYYMMDD>
- broadcast detail:
  https://audioapi.orf.at/<station>/api/json/5.0/broadcast/<id>
- podcasts index:
  https://audioapi.orf.at/radiothek/api/public/2.0/podcasts
- podcast detail (+episodes):
  https://audioapi.orf.at/radiothek/api/public/2.0/podcast/<id>?episodes=episodes
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .helpers import (
    OrfPodcast,
    OrfPodcastEpisode,
    OrfStation,
    PrivateStation,
    parse_orf_podcast_episodes,
    parse_orf_podcasts_index,
    parse_orf_stations,
    parse_private_stations,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


# ORF Sound bundle (stations + privates)
API_BUNDLE = "https://orf.at/app-infos/sound/web/1.0/bundle.json?_o=sound.orf.at"

# ORF broadcasts (catch-up “Sendungen” per station/day)
BROADCASTS_URL = "https://audioapi.orf.at/{station}/api/json/5.0/broadcasts/{yyyymmdd}"
BROADCAST_URL = "https://audioapi.orf.at/{station}/api/json/5.0/broadcast/{bid}"

# ORF actual podcasts (API 2.0)
PODCASTS_INDEX_URL = "https://audioapi.orf.at/radiothek/api/public/2.0/podcasts"
PODCAST_DETAIL_URL = (
    "https://audioapi.orf.at/radiothek/api/public/2.0/podcast/{pid}?episodes=episodes"
)

# Provider config
CONF_STREAM_PROTO = "stream_proto"  # hls | shoutcast (ORF stations only)
CONF_STREAM_QUALITY = "stream_quality"  # hls: q1a/q2a/q3a/q4a/qxa ; shoutcast: q1a/q2a
CONF_INCLUDE_HIDDEN = "include_hidden"

CONF_CATCHUP_PROTO = "catchup_proto"  # progressive | hls
CONF_CATCHUP_STATIONS = "catchup_stations"  # optional comma-separated station ids

# local-image pseudo scheme (provider-owned)
LOCAL_IMG_PREFIX = "radiothek://station/"
CATCHUP_DAYS = 30

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.LIBRARY_PODCASTS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Set up the ORF Radiothek provider."""
    return RadiothekProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return provider configuration entries."""
    # ruff: noqa: ARG001
    values = values or {}

    return (
        ConfigEntry(
            key=CONF_STREAM_PROTO,
            type=ConfigEntryType.STRING,
            label="Preferred ORF protocol",
            required=False,
            default_value="hls",
            description=(
                "Used for ORF stations (template-based). "
                "Privates use explicit URLs from bundle.json."
            ),
            value=values.get(CONF_STREAM_PROTO),
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_STREAM_QUALITY,
            type=ConfigEntryType.STRING,
            label="ORF quality",
            required=False,
            default_value="qxa",
            description="For ORF HLS: q1a/q2a/q3a/q4a/qxa. For shoutcast: q1a/q2a.",
            value=values.get(CONF_STREAM_QUALITY),
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_INCLUDE_HIDDEN,
            type=ConfigEntryType.BOOLEAN,
            label="Include hidden stations",
            required=False,
            default_value=False,
            description="Include stations with hideFromStations=true.",
            value=values.get(CONF_INCLUDE_HIDDEN),
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_CATCHUP_PROTO,
            type=ConfigEntryType.STRING,
            label="Catch-up stream type",
            required=False,
            default_value="progressive",
            description="Use 'progressive' (mp3) or 'hls' (m3u8) URLs from the broadcast detail.",
            value=values.get(CONF_CATCHUP_PROTO),
        ),
        ConfigEntry(
            key=CONF_CATCHUP_STATIONS,
            type=ConfigEntryType.STRING,
            label="Catch-up stations (optional)",
            required=False,
            default_value="",
            description=(
                "Comma-separated station ids (e.g. 'stm,wie,oe1'). "
                "Empty = all ORF stations from bundle."
            ),
            value=values.get(CONF_CATCHUP_STATIONS),
        ),
    )


class RadiothekProvider(MusicProvider):
    """ORF Radiothek provider."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize provider state."""
        super().__init__(*args, **kwargs)
        self._bundle: dict[str, Any] | None = None
        self._media_dir = Path(__file__).parent / "media"

        self.stream_proto = "hls"
        self.stream_quality = "qxa"
        self.include_hidden = False

        self.catchup_proto = "progressive"
        self.catchup_stations = ""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True for streaming providers."""
        return True

    async def handle_async_init(self) -> None:
        """Load config and prime caches."""
        self.stream_proto = str(self.config.get_value(CONF_STREAM_PROTO) or "hls").lower()
        self.stream_quality = str(self.config.get_value(CONF_STREAM_QUALITY) or "qxa").lower()
        self.include_hidden = bool(self.config.get_value(CONF_INCLUDE_HIDDEN) or False)

        self.catchup_proto = str(self.config.get_value(CONF_CATCHUP_PROTO) or "progressive").lower()
        self.catchup_stations = str(self.config.get_value(CONF_CATCHUP_STATIONS) or "").strip()

        if self.stream_proto not in ("hls", "shoutcast"):
            self.stream_proto = "hls"

        if self.stream_proto == "shoutcast":
            if self.stream_quality not in ("q1a", "q2a"):
                self.stream_quality = "q2a"
        elif self.stream_quality not in ("q1a", "q2a", "q3a", "q4a", "qxa"):
            self.stream_quality = "qxa"

        if self.catchup_proto not in ("progressive", "hls"):
            self.catchup_proto = "progressive"

        await self._get_bundle(force=True)

    # ----------------------------
    # HTTP / caching helpers
    # ----------------------------

    async def _http_get_json(self, url: str) -> dict[str, Any]:
        async with self.mass.http_session.get(
            url,
            headers={"User-Agent": "Music Assistant"},
            timeout=ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if not isinstance(data, dict):
                raise TypeError("Expected JSON object")
            return data

    async def _get_bundle(self, force: bool = False) -> dict[str, Any]:
        if self._bundle is not None and not force:
            return self._bundle
        try:
            self._bundle = await self._http_get_json(API_BUNDLE)
            return self._bundle
        except (ClientError, TimeoutError, ValueError) as err:
            self.logger.warning("Failed to fetch bundle.json: %s", err)
            if self._bundle is not None:
                return self._bundle
            raise

    @use_cache(3600 * 24)
    async def _get_broadcasts_for_day(self, station: str, yyyymmdd: str) -> list[dict[str, Any]]:
        data = await self._http_get_json(BROADCASTS_URL.format(station=station, yyyymmdd=yyyymmdd))
        payload = data.get("payload")
        if not isinstance(payload, list):
            return []
        return [x for x in payload if isinstance(x, dict)]

    @use_cache(3600 * 24)
    async def _get_broadcast_detail(self, station: str, bid: int) -> dict[str, Any]:
        data = await self._http_get_json(BROADCAST_URL.format(station=station, bid=bid))
        payload = data.get("payload")
        return payload if isinstance(payload, dict) else {}

    @use_cache(3600 * 24)
    async def _get_orf_podcasts_index_payload(self) -> dict[str, Any]:
        data = await self._http_get_json(PODCASTS_INDEX_URL)
        payload = data.get("payload")
        return payload if isinstance(payload, dict) else {}

    async def _get_orf_podcasts_index(self) -> list[OrfPodcast]:
        payload = await self._get_orf_podcasts_index_payload()
        return parse_orf_podcasts_index(payload)

    @use_cache(3600 * 24)
    async def _get_orf_podcast_detail(self, pid: int) -> dict[str, Any]:
        data = await self._http_get_json(PODCAST_DETAIL_URL.format(pid=pid))
        payload = data.get("payload")
        return payload if isinstance(payload, dict) else {}

    # ----------------------------
    # Bundle parsing
    # ----------------------------

    def _iter_orf_stations(self, bundle: dict[str, Any]) -> list[OrfStation]:
        return parse_orf_stations(bundle, include_hidden=self.include_hidden)

    def _iter_privates(self, bundle: dict[str, Any]) -> list[PrivateStation]:
        return parse_private_stations(bundle)

    def _privates_by_id(self, bundle: dict[str, Any]) -> dict[str, PrivateStation]:
        return {p.id: p for p in self._iter_privates(bundle)}

    def _catchup_station_ids(self, bundle: dict[str, Any]) -> list[str]:
        stations = [s.id for s in self._iter_orf_stations(bundle)]
        if self.catchup_stations:
            allowed = {s.strip() for s in self.catchup_stations.split(",") if s.strip()}
            stations = [s for s in stations if s in allowed]
        return stations

    # ----------------------------
    # Images
    # ----------------------------

    def _orf_local_icon_image(self, station_id: str) -> MediaItemImage | None:
        if (self._media_dir / f"{station_id}.png").is_file():
            return MediaItemImage(
                type=ImageType.THUMB,
                path=f"{LOCAL_IMG_PREFIX}{station_id}.png",
                provider=self.domain,
                remotely_accessible=False,
            )
        return None

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve provider-local image paths to a file path."""
        if not path.startswith(LOCAL_IMG_PREFIX):
            return path

        filename = path.removeprefix(LOCAL_IMG_PREFIX)
        if "/" in filename or "\\" in filename or ".." in filename:
            raise MediaNotFoundError("Image not found.")

        fpath = self._media_dir / filename
        if not fpath.is_file():
            raise MediaNotFoundError("Image not found.")

        return str(fpath)

    # ----------------------------
    # Stream URL helpers (radio)
    # ----------------------------

    def _build_orf_url(self, station: OrfStation) -> str | None:
        tmpl = station.live_stream_url_template
        if not isinstance(tmpl, str) or "{quality}" not in tmpl:
            return None
        if self.stream_proto == "shoutcast":
            return f"https://orf-live.ors-shoutcast.at/{station.id}-{self.stream_quality}"
        return tmpl.replace("{quality}", self.stream_quality)

    def _build_private_url(self, pstation: PrivateStation) -> tuple[str | None, str | None]:
        if not pstation.streams:
            return None, None
        s0 = pstation.streams[0]
        return s0.url, s0.format

    def _content_type_from_url_or_format(self, url: str, fmt: str | None) -> ContentType:
        if fmt:
            f = fmt.lower()
            if f == "mp3":
                return ContentType.try_parse("mp3")
            if f in ("aac", "aacp"):
                return ContentType.try_parse("aac")
        if ".m3u8" in url.lower():
            return ContentType.try_parse("aac")
        return ContentType.try_parse("unknown")

    # ----------------------------
    # ID schemes (avoid collisions)
    # ----------------------------

    # catch-up podcasts/episodes from broadcasts API
    def _catchup_podcast_id(self, station_id: str) -> str:
        return f"br:{station_id}"

    def _catchup_episode_id(self, station_id: str, bid: int) -> str:
        return f"br:{station_id}:{bid}"

    def _parse_catchup_episode_id(self, prov_episode_id: str) -> tuple[str, int]:
        # br:<station>:<bid>
        _, station, bid_s = prov_episode_id.split(":", 2)
        return station, int(bid_s)

    # actual podcasts API 2.0
    def _podcast_id(self, pid: int) -> str:
        return f"pod:{pid}"

    def _pod_episode_id(self, pid: int, guid: str) -> str:
        return f"pod:{pid}:{guid}"

    def _parse_pod_episode_id(self, prov_episode_id: str) -> tuple[int, str]:
        # pod:<pid>:<guid>
        _, pid_s, guid = prov_episode_id.split(":", 2)
        return int(pid_s), guid

    # ----------------------------
    # Text helpers
    # ----------------------------
    def _strip_html(self, s: str | None) -> str | None:
        if not s:
            return None
        return re.sub(r"<[^>]+>", "", s).strip()

    def _sanitize_template_url(self, url: str) -> str:
        # ORF template URLs contain "{&offset}" / "{&duration}" etc.
        return re.sub(r"\{[^}]+\}", "", url)

    # ----------------------------
    # Media item constructors
    # ----------------------------

    def _radio_item(self, item_id: str, name: str) -> Radio:
        return Radio(
            name=name,
            item_id=item_id,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    def _podcast_from_station(self, station: OrfStation) -> Podcast:
        name = station.name or station.id
        pid = self._catchup_podcast_id(station.id)
        p = Podcast(
            name=name,
            item_id=pid,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=pid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        p.metadata.description = f"Catch-up broadcasts for {name}"
        img = self._orf_local_icon_image(station.id)
        if img:
            # img is probably already a MediaItemImage
            p.metadata.add_image(img)
        return p

    def _episode_from_broadcast_obj(
        self,
        b: dict[str, Any],
        station_id: str,
        podcast_title: str,
        podcast_id: str,
    ) -> PodcastEpisode | None:
        bid = b.get("id")
        title = b.get("title")
        if not isinstance(bid, int) or not isinstance(title, str) or not title:
            return None

        prefix = self.iso_prefix(b.get("niceTime"))
        name = f"{prefix} - {title}" if prefix else title

        duration_sec: int | None = None
        dur_ms = b.get("duration")
        if isinstance(dur_ms, int) and dur_ms > 0:
            duration_sec = int(dur_ms / 1000)

        eid = self._catchup_episode_id(station_id, bid)

        ep = PodcastEpisode(
            name=name,
            item_id=eid,
            provider=self.instance_id,
            position=0,
            duration=duration_sec or 0,
            podcast=ItemMapping(
                item_id=podcast_id,
                provider=self.instance_id,
                name=podcast_title,
                media_type=MediaType.PODCAST,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=eid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        sub = self._strip_html(b.get("subtitle"))
        if sub:
            ep.metadata.description = sub

        # best image
        imgs = b.get("images")
        if isinstance(imgs, list) and imgs:
            best_url: str | None = None
            best_w = -1
            for img in imgs:
                if not isinstance(img, dict):
                    continue
                versions = img.get("versions")
                if not isinstance(versions, list):
                    continue
                for v in versions:
                    if not isinstance(v, dict):
                        continue
                    url = v.get("path")
                    if not isinstance(url, str) or not url.startswith("http"):
                        continue
                    w = int(v.get("width") or 0)
                    if w > best_w:
                        best_w = w
                        best_url = url
            if best_url:
                ep.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=best_url,
                        provider=self.domain,
                        remotely_accessible=True,
                    )
                )

        return ep

    def _podcast_from_orf_podcast_obj(self, pod: OrfPodcast) -> Podcast:
        pid = pod.id
        prov_id = self._podcast_id(pid)
        p = Podcast(
            name=pod.title or prov_id,
            item_id=prov_id,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        if pod.description:
            p.metadata.description = pod.description

        # image (best available)
        if pod.image:
            best = pod.image.best()
            if best:
                p.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=best,
                        provider=self.domain,
                        remotely_accessible=True,
                    )
                )

        return p

    @staticmethod
    def iso_prefix(ts: str | None) -> str:
        """Create a compact timestamp prefix for titles."""
        if not ts:
            return ""
        ts = ts.strip()
        if "T" in ts:
            return ts[:16].replace("T", " ")
        return ts

    def _episode_from_orf_podcast_episode_obj(
        self, ep: OrfPodcastEpisode, podcast: Podcast
    ) -> PodcastEpisode:
        guid = ep.guid
        pid = int(podcast.item_id.split(":", 1)[1])
        eid = self._pod_episode_id(pid, guid)

        base_title = ep.title or guid
        prefix = self.iso_prefix(ep.published)
        name = f"{prefix} - {base_title}" if prefix else base_title

        duration_sec: int | None = None
        if ep.duration_ms and ep.duration_ms > 0:
            duration_sec = int(ep.duration_ms / 1000)

        pe = PodcastEpisode(
            name=name,
            item_id=eid,
            provider=self.instance_id,
            position=0,
            duration=duration_sec or 0,
            podcast=podcast,
            provider_mappings={
                ProviderMapping(
                    item_id=eid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

        if ep.description:
            pe.metadata.description = ep.description

        # image (episode-level)
        if ep.image:
            best = ep.image.best()
            if best:
                pe.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=best,
                        provider=self.domain,
                        remotely_accessible=True,
                    )
                )

        if (not pe.metadata.images) and podcast.metadata.images:
            for img in podcast.metadata.images:
                pe.metadata.add_image(img)
        return pe

    # ----------------------------
    # MA API: Radios
    # ----------------------------

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Yield all radios exposed by this provider."""
        bundle = await self._get_bundle()

        # ORF stations (local icons)
        for st in self._iter_orf_stations(bundle):
            r = self._radio_item(st.id, st.name or st.id)
            img = self._orf_local_icon_image(st.id)
            if img:
                r.metadata.add_image(img)
            yield r

        # privates (remote icons)
        for pst in self._iter_privates(bundle):
            r = self._radio_item(pst.id, pst.name or pst.id)
            for url in pst.image_urls:
                r.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=url,
                        provider=self.domain,
                        remotely_accessible=True,
                    )
                )
            yield r

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Yield all podcasts exposed by this provider."""
        bundle = await self._get_bundle()

        # A) catch-up “podcasts” (one per station, filtered)
        stations = {s.id: s for s in self._iter_orf_stations(bundle)}
        for station_id in self._catchup_station_ids(bundle):
            st = stations.get(station_id)
            if st:
                yield self._podcast_from_station(st)

        # B) actual ORF podcasts
        pods = await self._get_orf_podcasts_index()
        for pod in pods:
            yield self._podcast_from_orf_podcast_obj(pod)

    @use_cache(3600 * 24)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get one specific Podcast by id."""
        bundle = await self._get_bundle()

        # catch-up station podcasts: br:<station>
        if prov_podcast_id.startswith("br:"):
            station_id = prov_podcast_id.split(":", 1)[1]
            stations = {s.id: s for s in self._iter_orf_stations(bundle)}
            st = stations.get(station_id)
            if not st:
                raise MediaNotFoundError("Podcast not found.")
            return self._podcast_from_station(st)

        # actual podcasts: pod:<id>
        if prov_podcast_id.startswith("pod:"):
            try:
                pid = int(prov_podcast_id.split(":", 1)[1])
            except (ValueError, IndexError) as err:
                raise MediaNotFoundError("Podcast not found.") from err

            pods = await self._get_orf_podcasts_index()
            pod = next((p for p in pods if p.id == pid), None)
            if not pod:
                detail = await self._get_orf_podcast_detail(pid)
                if not detail:
                    raise MediaNotFoundError("Podcast not found.")
                pod = OrfPodcast.from_index_item(detail) or OrfPodcast(
                    id=pid, title=str(detail.get("title") or pid)
                )
            return self._podcast_from_orf_podcast_obj(pod)

        raise MediaNotFoundError("Podcast not found.")

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get episodes of a specific podcast."""
        bundle = await self._get_bundle()

        # ----------------------
        # actual ORF podcasts
        # ----------------------
        if prov_podcast_id.startswith("pod:"):
            pid = int(prov_podcast_id.split(":", 1)[1])
            pods = await self._get_orf_podcasts_index()
            pod_obj = next((p for p in pods if p.id == pid), None)
            if not pod_obj:
                # allow if index missing but detail exists
                detail = await self._get_orf_podcast_detail(pid)
                if not detail:
                    raise MediaNotFoundError("Podcast not found.")
                pod_obj = OrfPodcast.from_index_item(detail) or OrfPodcast(
                    id=pid, title=str(detail.get("title") or pid)
                )

            podcast = self._podcast_from_orf_podcast_obj(pod_obj)

            detail = await self._get_orf_podcast_detail(pid)
            for orf_ep in parse_orf_podcast_episodes(detail):
                if not orf_ep.enclosures or not orf_ep.enclosures[0].url:
                    continue
                yield self._episode_from_orf_podcast_episode_obj(orf_ep, podcast)
            return

        # ----------------------
        # catch-up station podcasts
        # ----------------------
        if not prov_podcast_id.startswith("br:"):
            raise MediaNotFoundError("Podcast not found.")

        station_id = prov_podcast_id.split(":", 1)[1]

        # enforce station filter
        if self.catchup_stations:
            allowed = {s.strip() for s in self.catchup_stations.split(",") if s.strip()}
            if station_id not in allowed:
                return

        stations = {s.id: s for s in self._iter_orf_stations(bundle)}
        st = stations.get(station_id)
        if not st:
            raise MediaNotFoundError("Podcast not found.")
        podcast_title = st.name or station_id

        today = datetime.now(UTC).date()
        for day_offset in range(CATCHUP_DAYS):
            d = today - timedelta(days=day_offset)
            yyyymmdd = f"{d.year:04d}{d.month:02d}{d.day:02d}"
            items = await self._get_broadcasts_for_day(station_id, yyyymmdd)
            for b in items:
                episode = self._episode_from_broadcast_obj(
                    b=b,
                    station_id=station_id,
                    podcast_title=podcast_title,
                    podcast_id=prov_podcast_id,
                )
                if episode:
                    yield episode

    @use_cache(3600 * 24)
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get specific episode of specific podcast."""
        bundle = await self._get_bundle()

        # actual ORF podcasts: pod:<pid>:<guid>
        if prov_episode_id.startswith("pod:"):
            pid, guid = self._parse_pod_episode_id(prov_episode_id)

            pods = await self._get_orf_podcasts_index()
            pod_obj = next((p for p in pods if p.id == pid), None)
            if not pod_obj:
                detail = await self._get_orf_podcast_detail(pid)
                if not detail:
                    raise MediaNotFoundError("Podcast not found.")
                pod_obj = OrfPodcast.from_index_item(detail) or OrfPodcast(
                    id=pid, title=str(detail.get("title") or pid)
                )

            podcast = self._podcast_from_orf_podcast_obj(pod_obj)

            detail = await self._get_orf_podcast_detail(pid)
            for orf_ep in parse_orf_podcast_episodes(detail):
                if orf_ep.guid == guid:
                    return self._episode_from_orf_podcast_episode_obj(orf_ep, podcast)

            raise MediaNotFoundError("Podcast episode not found.")

        # catch-up episodes: br:<station>:<bid>
        if prov_episode_id.startswith("br:"):
            station_id, bid = self._parse_catchup_episode_id(prov_episode_id)
            stations = {s.id: s for s in self._iter_orf_stations(bundle)}
            st = stations.get(station_id)
            if not st:
                raise MediaNotFoundError("Podcast not found.")
            podcast_title = st.name or station_id
            podcast_id = self._catchup_podcast_id(station_id)

            b = await self._get_broadcast_detail(station_id, bid)
            episode = self._episode_from_broadcast_obj(
                b=b,
                station_id=station_id,
                podcast_title=podcast_title,
                podcast_id=podcast_id,
            )
            if not episode:
                raise MediaNotFoundError("Podcast episode not found.")

            desc = self._strip_html(b.get("description"))
            if desc:
                episode.metadata.description = desc

            return episode

        raise MediaNotFoundError("Podcast episode not found.")

    # ----------------------------
    # MA API: Search
    # ----------------------------

    @use_cache(3600 * 6)
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 10,
    ) -> SearchResults:
        """Search radios, podcasts or podcast episodes."""
        res = SearchResults()
        q = search_query.strip().lower()
        bundle = await self._get_bundle()

        if MediaType.RADIO in media_types:
            radios: list[Radio] = []

            for st in self._iter_orf_stations(bundle):
                if q in st.id.lower() or q in (st.name or "").lower():
                    r = self._radio_item(st.id, st.name or st.id)
                    img = self._orf_local_icon_image(st.id)
                    if img:
                        r.metadata.add_image(img)
                    radios.append(r)
                    if len(radios) >= limit:
                        break

            if len(radios) < limit:
                for pst in self._iter_privates(bundle):
                    if q in pst.id.lower() or q in (pst.name or "").lower():
                        r = self._radio_item(pst.id, pst.name or pst.id)
                        for url in pst.image_urls:
                            r.metadata.add_image(
                                MediaItemImage(
                                    type=ImageType.THUMB,
                                    path=url,
                                    provider=self.domain,
                                    remotely_accessible=True,
                                )
                            )
                        radios.append(r)
                        if len(radios) >= limit:
                            break

            res.radio = radios

        # Optional: podcast search (station catch-up podcasts + actual podcasts)
        if MediaType.PODCAST in media_types and hasattr(res, "podcasts"):
            podcasts: list[Podcast] = []

            # catch-up station podcasts
            stations: dict[str, OrfStation] = {s.id: s for s in self._iter_orf_stations(bundle)}
            for station_id in self._catchup_station_ids(bundle):
                if station_id not in stations:
                    continue
                st = stations[station_id]
                if q in station_id.lower() or q in (st.name or "").lower():
                    podcasts.append(self._podcast_from_station(st))
                    if len(podcasts) >= limit:
                        break

            # actual podcasts
            if len(podcasts) < limit:
                pods = await self._get_orf_podcasts_index()
                for pod in pods:
                    title = (pod.title or "").lower()
                    author = (pod.author or "").lower()
                    if q in title or q in author:
                        podcasts.append(self._podcast_from_orf_podcast_obj(pod))
                        if len(podcasts) >= limit:
                            break

            res.podcasts = podcasts

        return res

    # ----------------------------
    # MA API: Lookup radios
    # ----------------------------

    @use_cache(3600 * 24)
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Search single radio."""
        bundle = await self._get_bundle()

        stations = {s.id: s for s in self._iter_orf_stations(bundle)}
        st = stations.get(prov_radio_id)
        if st:
            r = self._radio_item(prov_radio_id, st.name or prov_radio_id)
            img = self._orf_local_icon_image(prov_radio_id)
            if img:
                r.metadata.add_image(img)
            return r

        priv = self._privates_by_id(bundle).get(prov_radio_id)
        if priv:
            r = self._radio_item(prov_radio_id, priv.name or prov_radio_id)
            for url in priv.image_urls:
                r.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=url,
                        provider=self.domain,
                        remotely_accessible=True,
                    )
                )
            return r

        raise MediaNotFoundError("Radio not found.")

    # ----------------------------
    # MA API: Playback
    # ----------------------------

    async def _get_radio_stream_details(self, item_id: str) -> StreamDetails:
        bundle = await self._get_bundle()

        stations = {s.id: s for s in self._iter_orf_stations(bundle)}
        if item_id in stations:
            url = self._build_orf_url(stations[item_id])
            if not url:
                raise UnplayableMediaError("No stream URL for ORF station.")
            ctype = self._content_type_from_url_or_format(url, None)
            return StreamDetails(
                provider=self.domain,
                item_id=item_id,
                media_type=MediaType.RADIO,
                stream_type=StreamType.HTTP,
                path=url,
                audio_format=AudioFormat(content_type=ctype),
                can_seek=False,
                allow_seek=False,
            )

        priv = self._privates_by_id(bundle).get(item_id)
        if priv:
            url, fmt = self._build_private_url(priv)
            if not url:
                raise UnplayableMediaError("No stream URL for private station.")
            ctype = self._content_type_from_url_or_format(url, fmt)
            return StreamDetails(
                provider=self.domain,
                item_id=item_id,
                media_type=MediaType.RADIO,
                stream_type=StreamType.HTTP,
                path=url,
                audio_format=AudioFormat(content_type=ctype),
                can_seek=False,
                allow_seek=False,
            )

        raise MediaNotFoundError("Radio not found.")

    async def _get_podcast_episode_stream_details(self, item_id: str) -> StreamDetails:
        if item_id.startswith("pod:"):
            return await self._get_orf_podcast_episode_stream_details(item_id)

        if item_id.startswith("br:"):
            return await self._get_broadcast_episode_stream_details(item_id)

        raise MediaNotFoundError("Podcast episode not found.")

    async def _get_orf_podcast_episode_stream_details(self, item_id: str) -> StreamDetails:
        pid, guid = self._parse_pod_episode_id(item_id)
        detail = await self._get_orf_podcast_detail(pid)

        eps = detail.get("episodes")
        if not isinstance(eps, list):
            raise UnplayableMediaError("No episodes for podcast")

        target: dict[str, Any] | None = None
        for ep in eps:
            if isinstance(ep, dict) and ep.get("guid") == guid:
                target = ep
                break
        if not target:
            raise MediaNotFoundError("Podcast episode not found")

        enc = target.get("enclosures")
        if not isinstance(enc, list) or not enc or not isinstance(enc[0], dict):
            raise UnplayableMediaError("No enclosure for episode")
        url = enc[0].get("url")
        if not isinstance(url, str) or not url:
            raise UnplayableMediaError("No playable url for episode")

        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=url,
            audio_format=AudioFormat(content_type=ContentType.try_parse("mp3")),
            can_seek=True,
            allow_seek=True,
        )

    async def _get_broadcast_episode_stream_details(self, item_id: str) -> StreamDetails:
        station_id, bid = self._parse_catchup_episode_id(item_id)
        b = await self._get_broadcast_detail(station_id, bid)

        streams = b.get("streams")
        if not isinstance(streams, list) or not streams:
            raise UnplayableMediaError("No streams for episode")

        s0 = streams[0]
        urls = s0.get("urls") if isinstance(s0, dict) else None
        if not isinstance(urls, dict):
            raise UnplayableMediaError("No stream urls for episode")

        if self.catchup_proto == "hls":
            url = urls.get("hls")
            ctype = ContentType.try_parse("aac")
            stream_type = StreamType.HLS
        else:
            url = urls.get("progressive")
            ctype = ContentType.try_parse("mp3")
            stream_type = StreamType.HTTP

        if not isinstance(url, str) or not url:
            raise UnplayableMediaError("No playable url for episode")

        url = self._sanitize_template_url(url)

        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=stream_type,
            path=url,
            audio_format=AudioFormat(content_type=ctype),
            can_seek=True,
            allow_seek=True,
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve Playable stream."""
        if media_type == MediaType.RADIO:
            return await self._get_radio_stream_details(item_id)

        if media_type == MediaType.PODCAST_EPISODE:
            return await self._get_podcast_episode_stream_details(item_id)

        raise UnplayableMediaError("Unsupported media type")
