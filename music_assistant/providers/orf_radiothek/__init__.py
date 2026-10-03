"""
ORF Radiothek / ORF Sound provider for Music Assistant.

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
from collections.abc import AsyncGenerator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.constants import DEFAULT_AUDIOBOOK_PODCAST_GENRE
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.datetime import from_iso_string, utc
from music_assistant.helpers.throttle_retry import RequestPriority, Throttler, set_request_priority
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
    from music_assistant_models.config_entries import ProviderConfig
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
# recent days still change (live/upcoming broadcasts), older days are final
CATCHUP_RECENT_DAYS_CACHE = 3600
CATCHUP_PAST_DAYS_CACHE = 3600 * 24 * 7
# a broadcast that is still on air gains stream segments while it runs
BROADCAST_UNFINISHED_CACHE = 300
BROADCAST_FINISHED_CACHE = 3600 * 24
# real audio durations of finished broadcasts, kept for as long as ORF keeps the audio
BROADCAST_DURATIONS_CACHE = 3600 * 24 * CATCHUP_DAYS
# requests per second to the ORF APIs, background work gets half of it
ORF_RATE_LIMIT = 5
# catch-up audio is read over a long time, only connecting and stalls are bounded
CATCHUP_AUDIO_TIMEOUT = ClientTimeout(total=None, sock_connect=20, sock_read=60)
CATCHUP_AUDIO_CHUNK_SIZE = 64 * 1024

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Set up the ORF Radiothek provider."""
    return RadiothekProvider(mass, manifest, config, SUPPORTED_FEATURES)


class RadiothekProvider(MusicProvider):
    """ORF Radiothek provider."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize provider state."""
        super().__init__(*args, **kwargs)
        self._bundle: dict[str, Any] | None = None
        self._media_dir = Path(__file__).parent / "media"
        self._throttler = Throttler(rate_limit=ORF_RATE_LIMIT, period=1.0)

        self.stream_proto = "hls"
        self.stream_quality = "qxa"
        self.include_hidden = False

        self.catchup_proto = "progressive"
        self.catchup_stations = ""

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return provider configuration entries."""
        return (
            ConfigEntry(
                key=CONF_STREAM_PROTO,
                type=ConfigEntryType.STRING,
                required=False,
                default_value="hls",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_STREAM_QUALITY,
                type=ConfigEntryType.STRING,
                required=False,
                default_value="qxa",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_INCLUDE_HIDDEN,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_CATCHUP_PROTO,
                type=ConfigEntryType.STRING,
                required=False,
                default_value="progressive",
            ),
            ConfigEntry(
                key=CONF_CATCHUP_STATIONS,
                type=ConfigEntryType.STRING,
                required=False,
                default_value="",
            ),
        )

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

        try:
            await self._get_bundle(force=True)
        except (ClientError, TimeoutError, ValueError, InvalidDataError) as err:
            raise ProviderUnavailableError(f"Unable to fetch ORF station bundle: {err}") from err

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the audio of a catch-up broadcast, all of its stream segments back to back.

        :param streamdetails: The StreamDetails previously returned by get_stream_details.
        :param seek_position: Position in seconds to start from.
        """
        parts = streamdetails.data
        if not isinstance(parts, list) or not parts:
            raise UnplayableMediaError("No stream segments for episode")

        skip = float(max(seek_position, 0))
        for part in parts:
            if not isinstance(part, MultiPartPath):
                continue
            if part.duration and skip >= part.duration:
                skip -= part.duration
                continue
            url = self._segment_url_from(part.path, int(skip * 1000))
            skip = 0
            async with self.mass.http_session.get(
                url,
                headers={"User-Agent": "Music Assistant"},
                timeout=CATCHUP_AUDIO_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.content.iter_chunked(CATCHUP_AUDIO_CHUNK_SIZE):
                    yield chunk

    # ----------------------------
    # HTTP / caching helpers
    # ----------------------------

    async def _http_get_json(self, url: str) -> dict[str, Any]:
        await self._throttler.acquire()
        async with self.mass.http_session.get(
            url,
            headers={"User-Agent": "Music Assistant"},
            timeout=ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if not isinstance(data, dict):
                raise InvalidDataError("Expected JSON object")
            return data

    async def _get_bundle(self, force: bool = False) -> dict[str, Any]:
        if self._bundle is not None and not force:
            return self._bundle
        try:
            self._bundle = await self._http_get_json(API_BUNDLE)
            return self._bundle
        except (ClientError, TimeoutError, ValueError, InvalidDataError) as err:
            self.logger.warning("Failed to fetch bundle.json: %s", err)
            if self._bundle is not None:
                return self._bundle
            raise

    async def _get_broadcasts_for_day(self, station: str, day: date) -> list[dict[str, Any]]:
        yyyymmdd = day.strftime("%Y%m%d")
        cache_key = f"broadcasts.{station}.{yyyymmdd}"
        cached = await self.mass.cache.get(cache_key, provider=self.instance_id)
        if isinstance(cached, list):
            return cached

        try:
            data = await self._http_get_json(
                BROADCASTS_URL.format(station=station, yyyymmdd=yyyymmdd)
            )
        except (ClientError, TimeoutError, ValueError, InvalidDataError) as err:
            self.logger.warning("Failed to fetch broadcasts of %s for %s: %s", station, day, err)
            return []

        payload = data.get("payload")
        items = [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []
        # an empty day is not cached: ORF may not have published it yet
        if items:
            is_recent = (utc().date() - day).days <= 1
            await self.mass.cache.set(
                cache_key,
                items,
                expiration=CATCHUP_RECENT_DAYS_CACHE if is_recent else CATCHUP_PAST_DAYS_CACHE,
                provider=self.instance_id,
            )
        return items

    async def _get_broadcast_detail(self, station: str, bid: int) -> dict[str, Any]:
        cache_key = f"broadcast.{station}.{bid}"
        cached = await self.mass.cache.get(cache_key, provider=self.instance_id)
        if isinstance(cached, dict):
            return cached

        payload = await self._fetch_broadcast_detail(station, bid)
        if not payload:
            return {}
        # state "C" means the broadcast has completed
        finished = payload.get("state") == "C"
        await self.mass.cache.set(
            cache_key,
            payload,
            expiration=BROADCAST_FINISHED_CACHE if finished else BROADCAST_UNFINISHED_CACHE,
            provider=self.instance_id,
        )
        return payload

    async def _fetch_broadcast_detail(self, station: str, bid: int) -> dict[str, Any]:
        data = await self._http_get_json(BROADCAST_URL.format(station=station, bid=bid))
        payload = data.get("payload")
        return payload if isinstance(payload, dict) else {}

    def _durations_cache_key(self, station: str) -> str:
        return f"broadcast_durations.{station}"

    async def _get_known_durations(self, station: str) -> dict[str, int]:
        """
        Return the real audio durations of finished broadcasts of a station.

        Keys are broadcast ids, values are seconds (0 when ORF gave no usable duration).

        :param station: The ORF station id.
        """
        cached = await self.mass.cache.get(
            self._durations_cache_key(station), provider=self.instance_id
        )
        return cached if isinstance(cached, dict) else {}

    async def _fill_broadcast_durations(
        self, station: str, missing: list[int], current: set[int]
    ) -> None:
        """
        Look up the real audio duration of broadcasts in the background.

        :param station: The ORF station id.
        :param missing: Broadcast ids without a known duration, in the order to look them up.
        :param current: All broadcast ids still listed, older ones are dropped from the cache.
        """
        set_request_priority(RequestPriority.LOW)
        known = {
            bid: seconds
            for bid, seconds in (await self._get_known_durations(station)).items()
            if bid.isdigit() and int(bid) in current
        }
        unsaved = 0
        for bid in missing:
            if str(bid) in known:
                continue
            try:
                detail = await self._fetch_broadcast_detail(station, bid)
            except (ClientError, TimeoutError, ValueError, InvalidDataError) as err:
                self.logger.debug("Failed to fetch broadcast %s of %s: %s", bid, station, err)
                continue
            # the detail of a broadcast still on air is incomplete
            if detail.get("state") != "C":
                continue
            known[str(bid)] = self._broadcast_audio_seconds(detail) or 0
            unsaved += 1
            # save progress regularly, so an interrupted fill is not repeated from the start
            if unsaved >= 25:
                await self._save_known_durations(station, known)
                unsaved = 0
        await self._save_known_durations(station, known)

    async def _save_known_durations(self, station: str, known: dict[str, int]) -> None:
        await self.mass.cache.set(
            self._durations_cache_key(station),
            known,
            expiration=BROADCAST_DURATIONS_CACHE,
            provider=self.instance_id,
        )

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

    def _broadcast_segments(self, b: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the stream segments of a broadcast detail, in playback order."""
        streams = b.get("streams")
        if not isinstance(streams, list):
            return []
        return [s for s in streams if isinstance(s, dict)]

    def _segment_url(self, segment: dict[str, Any], proto: str) -> str | None:
        """Return the playable url of one broadcast stream segment for a protocol."""
        urls = segment.get("urls")
        url = urls.get(proto) if isinstance(urls, dict) else None
        if not isinstance(url, str) or not url:
            return None
        return self._sanitize_template_url(url)

    def _broadcast_audio_seconds(self, b: dict[str, Any]) -> int | None:
        """Return the real audio duration of a broadcast detail, or None when unknown."""
        seg_seconds = [self._segment_seconds(s) for s in self._broadcast_segments(b)]
        if not seg_seconds or not all(seg_seconds):
            return None
        return int(sum(x for x in seg_seconds if x))

    @staticmethod
    def _segment_url_from(url: str, skip_ms: int) -> str:
        """
        Return a segment url that starts skip_ms into the segment.

        ORF serves a segment from the millisecond "offset" query parameter onwards, so moving
        it seeks without downloading the skipped audio.

        :param url: The (sanitized) progressive url of the segment.
        :param skip_ms: Milliseconds to skip from the start of the segment.
        """
        if skip_ms <= 0:
            return url
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        offset = next((v for k, v in query if k == "offset"), None)
        if offset is None or not offset.isdigit():
            return url
        new_offset = str(int(offset) + skip_ms)
        query = [(k, new_offset if k == "offset" else v) for k, v in query]
        return urlunsplit(parts._replace(query=urlencode(query)))

    @staticmethod
    def _duration_from_seconds(seconds: float | None) -> int | None:
        """Return a whole-second duration, or None when unknown."""
        return int(seconds) if seconds else None

    @staticmethod
    def _segment_seconds(segment: dict[str, Any]) -> float | None:
        """Return the duration of one broadcast stream segment in seconds."""
        dur_ms = segment.get("duration")
        if isinstance(dur_ms, int) and dur_ms > 0:
            return dur_ms / 1000
        return None

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
        release_date = self._release_date(b.get("niceTime"))

        duration_sec: int | None = None
        dur_ms = b.get("duration")
        if isinstance(dur_ms, int) and dur_ms > 0:
            duration_sec = int(dur_ms / 1000)

        eid = self._catchup_episode_id(station_id, bid)

        ep = PodcastEpisode(
            name=name,
            item_id=eid,
            provider=self.instance_id,
            position=self._position_from_date(release_date),
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
        ep.metadata.release_date = release_date

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
        p.metadata.genres = {DEFAULT_AUDIOBOOK_PODCAST_GENRE}

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

    @staticmethod
    def _release_date(ts: Any) -> datetime | None:
        """Return the datetime of an ISO timestamp, or None when it cannot be read."""
        if not ts:
            return None
        try:
            return from_iso_string(ts)
        except TypeError, ValueError:
            return None

    @staticmethod
    def _position_from_date(release_date: datetime | None) -> int:
        """Return a sortable episode position (newest highest) for a release date."""
        return int(release_date.timestamp()) if release_date else 0

    def _episode_from_orf_podcast_episode_obj(
        self, ep: OrfPodcastEpisode, podcast: Podcast
    ) -> PodcastEpisode:
        guid = ep.guid
        pid = int(podcast.item_id.split(":", 1)[1])
        eid = self._pod_episode_id(pid, guid)

        base_title = ep.title or guid
        prefix = self.iso_prefix(ep.published)
        name = f"{prefix} - {base_title}" if prefix else base_title
        release_date = self._release_date(ep.published)

        duration_sec: int | None = None
        if ep.duration_ms and ep.duration_ms > 0:
            duration_sec = int(ep.duration_ms / 1000)

        pe = PodcastEpisode(
            name=name,
            item_id=eid,
            provider=self.instance_id,
            position=self._position_from_date(release_date),
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
        pe.metadata.release_date = release_date

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

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's radio stations and podcasts.

        :param path: The path to browse, (e.g. provider_id://artists).
        """
        subpath = path.split("://", 1)[1] if "://" in path else ""

        if subpath == "radios":
            return await self._browse_radios()
        if subpath == "podcasts":
            return await self._browse_podcasts()

        # top-level: show category folders
        return [
            BrowseFolder(
                item_id="radios",
                provider=self.instance_id,
                path=f"{self.instance_id}://radios",
                name="Radio Stations",
                translation_key="radio_stations",
            ),
            BrowseFolder(
                item_id="podcasts",
                provider=self.instance_id,
                path=f"{self.instance_id}://podcasts",
                name="Podcasts",
                translation_key="podcasts",
            ),
        ]

    async def _browse_radios(self) -> list[Radio]:
        """Return all radio stations for browsing."""
        bundle = await self._get_bundle()
        radios: list[Radio] = []

        for st in self._iter_orf_stations(bundle):
            r = self._radio_item(st.id, st.name or st.id)
            img = self._orf_local_icon_image(st.id)
            if img:
                r.metadata.add_image(img)
            radios.append(r)

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
            radios.append(r)

        return radios

    async def _browse_podcasts(self) -> list[Podcast]:
        """Return all podcasts for browsing."""
        bundle = await self._get_bundle()
        podcasts: list[Podcast] = []

        # catch-up station podcasts
        stations = {s.id: s for s in self._iter_orf_stations(bundle)}
        for station_id in self._catchup_station_ids(bundle):
            st = stations.get(station_id)
            if st:
                podcasts.append(self._podcast_from_station(st))

        # actual ORF podcasts
        pods = await self._get_orf_podcasts_index()
        for pod in pods:
            podcasts.append(self._podcast_from_orf_podcast_obj(pod))

        return podcasts

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

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
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

        # the day listing only has the scheduled duration, which includes the gaps between
        # stream segments (news etc.); real durations are looked up in the background
        known = await self._get_known_durations(station_id)
        missing: list[int] = []
        current: set[int] = set()

        today = utc().date()
        for day_offset in range(CATCHUP_DAYS):
            d = today - timedelta(days=day_offset)
            items = await self._get_broadcasts_for_day(station_id, d)
            for b in items:
                episode = self._episode_from_broadcast_obj(
                    b=b,
                    station_id=station_id,
                    podcast_title=podcast_title,
                    podcast_id=prov_podcast_id,
                )
                if not episode:
                    continue
                bid = int(b["id"])
                current.add(bid)
                if b.get("state") == "C":
                    seconds = known.get(str(bid))
                    if seconds is None:
                        missing.append(bid)
                    elif seconds:
                        episode.duration = seconds
                yield episode

        if missing:
            self.mass.create_task(
                self._fill_broadcast_durations(station_id, missing, current),
                task_id=f"orf_radiothek.durations.{self.instance_id}.{station_id}",
                task_name="orf_radiothek_fill_durations",
            )

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

            # the scheduled duration includes the gaps between stream segments (news etc.)
            if seconds := self._broadcast_audio_seconds(b):
                episode.duration = seconds

            return episode

        raise MediaNotFoundError("Podcast episode not found.")

    # ----------------------------
    # MA API: Search
    # ----------------------------

    @use_cache(3600 * 24)
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

        segments = self._broadcast_segments(b)
        if not segments:
            raise UnplayableMediaError("No streams for episode")

        # a broadcast can be split into several segments (e.g. around the news); those are
        # played back to back, which only works for progressive streams
        if self.catchup_proto == "hls" and len(segments) == 1:
            url = self._segment_url(segments[0], "hls")
            if not url:
                raise UnplayableMediaError("No playable url for episode")
            return StreamDetails(
                provider=self.domain,
                item_id=item_id,
                media_type=MediaType.PODCAST_EPISODE,
                stream_type=StreamType.HLS,
                path=url,
                duration=self._duration_from_seconds(self._segment_seconds(segments[0])),
                audio_format=AudioFormat(content_type=ContentType.try_parse("aac")),
                can_seek=True,
                allow_seek=True,
            )

        parts: list[MultiPartPath] = []
        for segment in segments:
            if url := self._segment_url(segment, "progressive"):
                parts.append(MultiPartPath(path=url, duration=self._segment_seconds(segment)))
        if not parts:
            raise UnplayableMediaError("No playable url for episode")

        duration: int | None = None
        if all(part.duration for part in parts):
            duration = self._duration_from_seconds(sum(part.duration or 0 for part in parts))

        # streamed by get_audio_stream, which seeks through the url instead of downloading
        # (and discarding) all audio before the seek position
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.CUSTOM,
            data=parts,
            duration=duration,
            audio_format=AudioFormat(content_type=ContentType.try_parse("mp3")),
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
