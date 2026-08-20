"""NetEase Cloud Music provider implementation (MVP)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
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
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.track_filter import filter_tracks
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_API_BASE_URL,
    CONF_COOKIE,
    CONF_QUALITY,
    CONF_UID,
    DEFAULT_API_BASE_URL,
    QUALITY_EXHIGH,
    QUALITY_HIGHER,
    QUALITY_HIRES,
    QUALITY_JYEFFECT,
    QUALITY_JYMASTER,
    QUALITY_LOSSLESS,
    QUALITY_STANDARD,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import BrowseFolder, MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.LYRICS,
}

_HTTP_TIMEOUT = ClientTimeout(total=20)
_LRC_TIMESTAMP_PATTERN = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
_LRC_META_TAG_PATTERN = re.compile(r"^\[[a-zA-Z]+:.*\]$")
_RECOMMEND_NEWSONG_TTL = 60 * 30
_RECOMMEND_PLAYLIST_TTL = 60 * 60
_RECOMMEND_DAILY_TTL = 60 * 30
_RECOMMEND_PERSONAL_FM_TTL = 60 * 5
_RECOMMEND_HEART_MODE_TTL = 60 * 60
CACHE_CATEGORY_RECOMMENDATIONS = 1
# NetEase song-detail payload uses this bit in `hr`/`h` mark metadata to indicate
# that the track has a Hi-Res tier in catalog metadata.
# Value observed from NeteaseCloudMusicApi-compatible responses.
_HIRES_MARK_FLAG = 17179869184
_PLAYLIST_PERSONAL_FM_ID = "personal_fm_dynamic"
_PLAYLIST_HEART_MODE_PREFIX = "heart_mode_dynamic"
_NCM_PROVIDER_ICON_URL = (
    "https://raw.githubusercontent.com/NeteaseCloudMusicApiEnhanced/"
    "api-enhanced/main/public/docs/netease.png"
)


def _to_positive_int(value: Any) -> int:
    """Convert unknown value to positive int, otherwise return 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        parsed = int(value)
        return max(0, parsed)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0
        with suppress(ValueError):
            parsed = int(float(stripped))
            return max(0, parsed)
    return 0


def _lrc_to_plain_text(lrc_text: str) -> str:
    """Convert timestamped lrc to plain multi-line lyric text."""
    lines: list[str] = []
    for raw_line in lrc_text.splitlines():
        line = _LRC_TIMESTAMP_PATTERN.sub("", raw_line).strip()
        if not line or _LRC_META_TAG_PATTERN.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _extract_song_image_url(song_obj: dict[str, Any]) -> str | None:
    """Extract best-effort cover image URL from a song payload object."""
    album_raw = (
        song_obj.get("al") if isinstance(song_obj.get("al"), dict) else song_obj.get("album")
    )
    album_data = album_raw if isinstance(album_raw, dict) else {}
    for candidate in (
        album_data.get("picUrl"),
        album_data.get("coverUrl"),
        song_obj.get("picUrl"),
        song_obj.get("albumPic"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _parse_track_duration_seconds(song_obj: dict[str, Any]) -> int:
    """Parse track duration in seconds from NCM payload fields with known units."""
    # Fields documented/observed in NCM payloads as milliseconds.
    duration_ms_candidates = (
        song_obj.get("dt"),
        song_obj.get("duration"),
        song_obj.get("songTime"),
        song_obj.get("durationMs"),
        song_obj.get("playTime"),
        (
            song_obj.get("bMusic", {}).get("playTime")
            if isinstance(song_obj.get("bMusic"), dict)
            else None
        ),
    )
    for duration_ms in duration_ms_candidates:
        parsed_ms = _to_positive_int(duration_ms)
        if parsed_ms > 0:
            return parsed_ms // 1000

    # Optional normalized fields that may already be in seconds.
    duration_sec_candidates = (
        song_obj.get("durationSec"),
        song_obj.get("durationSeconds"),
        song_obj.get("lengthSeconds"),
    )
    for duration_sec in duration_sec_candidates:
        parsed_sec = _to_positive_int(duration_sec)
        if parsed_sec > 0:
            return parsed_sec
    return 0


class NcmApiClient:
    """Small async client for NeteaseCloudMusicApi-compatible endpoints."""

    def __init__(self, session: ClientSession, base_url: str) -> None:
        """Initialize API client."""
        self._session = session
        self._base_url = base_url.rstrip("/")

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        cookie: str | None = None,
        allow_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        """Perform GET request and validate common NCM response format."""
        req_params: dict[str, Any] = {}
        if params:
            req_params.update(params)
        headers: dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
        if cookie:
            headers["Cookie"] = cookie
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            async with self._session.get(
                url,
                params=req_params,
                headers=headers,
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise ResourceTemporarilyUnavailable(
                        f"Netease API HTTP {resp.status} for {path}",
                        backoff_time=20,
                    )
        except TimeoutError as err:
            raise ResourceTemporarilyUnavailable(
                f"Netease API timeout for {url}. "
                "Please verify API base URL and that the backend service is running.",
                backoff_time=20,
            ) from err
        except ClientError as err:
            raise ResourceTemporarilyUnavailable(
                f"Netease API network error for {path}: {err}",
                backoff_time=20,
            ) from err
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise InvalidDataError(f"Netease API returned invalid JSON for {path}") from err
        if not isinstance(payload, dict):
            raise InvalidDataError(f"Netease API payload is not an object for {path}")
        code = _extract_code(payload)
        if allow_codes and code in allow_codes:
            return payload
        if code not in (None, 200):
            raise InvalidDataError(f"Netease API error code {code} for {path}")
        return payload


def _extract_code(payload: dict[str, Any]) -> int | None:
    """Extract API code from payload."""
    raw_code = payload.get("code")
    if raw_code is None and isinstance(payload.get("data"), dict):
        raw_code = payload["data"].get("code")
    try:
        return int(raw_code) if raw_code is not None else None
    except TypeError, ValueError:
        return None


def _extract_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Return payload.data when it is an object, otherwise payload itself."""
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _extract_cookie(payload: dict[str, Any]) -> str:
    """Extract login cookie string from payload."""
    data = _extract_data(payload)
    for candidate in (data.get("cookie"), payload.get("cookie")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _with_pc_os_cookie(cookie: str) -> str:
    """
    Return cookie string with os=pc for quality URL consistency.

    Netease API may return lower-tier URLs for non-pc `os` cookies even for
    entitled accounts. This hint only stabilizes server-side format selection;
    entitlement still comes from upstream account/song permission checks and we
    do not bypass locked content.
    """
    if not cookie.strip():
        return cookie
    parts = [part.strip() for part in cookie.split(";") if part.strip()]
    kept: list[str] = []
    os_set = False
    for part in parts:
        if "=" not in part:
            kept.append(part)
            continue
        key, value = part.split("=", 1)
        if key.strip().lower() == "os":
            kept.append("os=pc")
            os_set = True
            continue
        kept.append(f"{key.strip()}={value.strip()}")
    if not os_set:
        kept.append("os=pc")
    return "; ".join(kept)


async def _resolve_uid(client: NcmApiClient, cookie: str) -> str:
    """Resolve user id from login status endpoint."""

    def _as_uid(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text if text and text.isdigit() else None

    def _extract_uid(payload: dict[str, Any]) -> str | None:
        data = _extract_data(payload)
        # API variants may use one or two nested `data` wrappers.
        containers: list[dict[str, Any]] = []
        for candidate in (data, payload):
            if isinstance(candidate, dict):
                containers.append(candidate)
                nested = candidate.get("data")
                if isinstance(nested, dict):
                    containers.append(nested)
                    nested2 = nested.get("data")
                    if isinstance(nested2, dict):
                        containers.append(nested2)

        # API variants may use different field names depending on implementation/version.
        for container in containers:
            profile = container.get("profile")
            if isinstance(profile, dict):
                for key in ("userId", "uid", "id"):
                    if uid := _as_uid(profile.get(key)):
                        return uid
            account = container.get("account")
            if isinstance(account, dict):
                for key in ("id", "userId", "uid"):
                    if uid := _as_uid(account.get(key)):
                        return uid
            for key in ("uid", "userId", "id"):
                if uid := _as_uid(container.get(key)):
                    return uid
        return None

    payload = await client.get(
        "/login/status",
        params={"timestamp": int(time.time() * 1000), "cookie": cookie},
        cookie=cookie,
    )
    if uid := _extract_uid(payload):
        return uid
    # Fallback for API implementations that expose UID only via /user/account.
    account_payload = await client.get(
        "/user/account",
        params={"timestamp": int(time.time() * 1000), "cookie": cookie},
        cookie=cookie,
    )
    if uid := _extract_uid(account_payload):
        return uid
    raise LoginFailed("Login succeeded but user id is missing from login status")


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return NeteaseCloudMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


class NeteaseCloudMusicProvider(MusicProvider):
    """NetEase Cloud Music provider (MVP)."""

    _client: NcmApiClient
    _cookie: str
    _uid: str

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the configuration (options) entries for the NetEase Cloud Music provider.

        Authentication runs in the interactive setup flow (see ``setup_flow.py``); the only
        genuine option configured here is the preferred streaming quality.
        """
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                default_value=QUALITY_EXHIGH,
                options=[
                    ConfigValueOption(QUALITY_STANDARD),
                    ConfigValueOption(QUALITY_HIGHER),
                    ConfigValueOption(QUALITY_EXHIGH),
                    ConfigValueOption(QUALITY_LOSSLESS),
                    ConfigValueOption(QUALITY_HIRES),
                    ConfigValueOption(QUALITY_JYEFFECT),
                    ConfigValueOption(QUALITY_JYMASTER),
                ],
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of provider."""
        self._cookie = str(self.get_setup_value(CONF_COOKIE) or "").strip()
        self._uid = str(self.get_setup_value(CONF_UID) or "").strip()
        if not self._cookie:
            raise LoginFailed("No NetEase authentication configured, please login by QR code")

        api_base_url = str(self.get_setup_value(CONF_API_BASE_URL) or DEFAULT_API_BASE_URL).strip()
        self._client = NcmApiClient(self.mass.http_session, api_base_url)
        if not self._uid:
            self._uid = await _resolve_uid(self._client, self._cookie)
        self.logger.info("NetEase Cloud Music authenticated for uid %s", self._uid)

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's available recommendation rows, without items."""
        return [
            RecommendationFolder(
                item_id="recommended_radios",
                provider=self.instance_id,
                name="Personal Radio",
                translation_key="personal_radio",
                icon="mdi:radio",
            ),
            RecommendationFolder(
                item_id="daily_songs",
                provider=self.instance_id,
                name="Recommended tracks",
                translation_key="recommended_tracks",
                icon="mdi:star",
            ),
            RecommendationFolder(
                item_id="recommended_new_songs",
                provider=self.instance_id,
                name="Recommended new tracks",
                translation_key="recommended_new_tracks",
                icon="mdi:music-note",
            ),
            RecommendationFolder(
                item_id="recommended_playlists",
                provider=self.instance_id,
                name="Recommended playlists",
                translation_key="recommended_playlists",
                icon="mdi:playlist-music",
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        items: UniqueList[MediaItemType | ItemMapping | BrowseFolder] = UniqueList()

        if item_id == "recommended_radios":
            return await self._build_radio_items()

        if item_id == "daily_songs":
            daily_payload = await self._get_recommend_payload_cached(
                "daily_songs", _RECOMMEND_DAILY_TTL, "/recommend/songs"
            )
            daily_data = _extract_data(daily_payload)
            daily_songs = daily_data.get("dailySongs")
            if isinstance(daily_songs, list):
                for song_obj in daily_songs:
                    if not isinstance(song_obj, dict):
                        continue
                    with suppress(InvalidDataError):
                        items.append(self._parse_track(song_obj))
                daily_tracks = [item for item in items if isinstance(item, Track)]
                await self._fill_track_durations(daily_tracks)
            return items

        if item_id == "recommended_new_songs":
            new_song_payload = await self._get_recommend_payload_cached(
                "recommended_newsong",
                _RECOMMEND_NEWSONG_TTL,
                "/personalized/newsong",
                {"limit": 50},
            )
            new_song_data = _extract_data(new_song_payload)
            raw_new_songs = new_song_data.get("result")
            if isinstance(raw_new_songs, list):
                for item in raw_new_songs:
                    if not isinstance(item, dict):
                        continue
                    song_obj = item.get("song") if isinstance(item.get("song"), dict) else item
                    if not isinstance(song_obj, dict):
                        continue
                    with suppress(InvalidDataError):
                        items.append(self._parse_track(song_obj))
                new_tracks = [item for item in items if isinstance(item, Track)]
                await self._fill_track_durations(new_tracks)
            return items

        if item_id == "recommended_playlists":
            playlist_payload = await self._get_recommend_payload_cached(
                "recommended_playlists",
                _RECOMMEND_PLAYLIST_TTL,
                "/personalized",
                {"limit": 25},
            )
            playlist_data = _extract_data(playlist_payload)
            raw_playlists = playlist_data.get("result")
            if isinstance(raw_playlists, list):
                for playlist_obj in raw_playlists:
                    if not isinstance(playlist_obj, dict):
                        continue
                    with suppress(InvalidDataError):
                        items.append(self._parse_playlist(playlist_obj))
            return items

        return items

    def _get_item_mapping(self, media_type: MediaType, item_id: str, name: str) -> ItemMapping:
        """Create generic item mapping."""
        return ItemMapping(
            media_type=media_type, item_id=item_id, provider=self.instance_id, name=name
        )

    def _ensure_square_image_url(self, url: str, size: int = 500) -> str:
        """Return image URL with square-size hint when supported by source."""
        if not url or "param=" in url:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}param={size}y{size}"

    def _normalize_image_url(self, url: str) -> str:
        """Normalize image URL for frontend compatibility."""
        # NCM often returns http://p*.music.126.net links.
        # In secure/ingress contexts these can be blocked as mixed content,
        # which makes the frontend fall back to a generic provider icon.
        if url.startswith("http://"):
            host = urlparse(url).hostname or ""
            if host == "music.126.net" or host.endswith(".music.126.net"):
                return "https://" + url[len("http://") :]
        return url

    def _make_image_list(
        self, url: str | None, *, force_square: bool = False
    ) -> UniqueList[MediaItemImage]:
        """Create image list for media item."""
        if not url:
            return UniqueList()
        normalized = self._normalize_image_url(url)
        image_url = self._ensure_square_image_url(normalized) if force_square else normalized
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )

    def _get_quality_obj(self, song_obj: dict[str, Any], level: str) -> dict[str, Any] | None:
        """Map level to corresponding quality object in song/detail payload."""
        quality_key_map = {
            QUALITY_STANDARD: "l",
            QUALITY_HIGHER: "m",
            QUALITY_EXHIGH: "h",
            QUALITY_LOSSLESS: "sq",
            QUALITY_HIRES: "hr",
            QUALITY_JYEFFECT: "je",
            QUALITY_JYMASTER: "jm",
        }
        quality_key = quality_key_map.get(level.lower())
        if not quality_key:
            return None
        quality_obj = song_obj.get(quality_key)
        return quality_obj if isinstance(quality_obj, dict) else None

    def _infer_audio_format_from_level(
        self, level: str, quality_obj: dict[str, Any] | None = None
    ) -> tuple[AudioFormat, str | None]:
        """Infer best-effort AudioFormat and optional quality label from level info."""
        level_norm = level.lower()
        if level_norm in (QUALITY_HIRES, QUALITY_JYEFFECT, QUALITY_JYMASTER):
            content_type = ContentType.FLAC
            bit_depth = 24
            details = "Hi-Res"
        elif level_norm == QUALITY_LOSSLESS:
            content_type = ContentType.FLAC
            bit_depth = 16
            details = None
        else:
            content_type = ContentType.MP3
            bit_depth = 16
            details = None

        sample_rate = _to_positive_int(quality_obj.get("sr")) if quality_obj else 0
        bit_rate = _to_positive_int(quality_obj.get("br")) if quality_obj else 0
        return (
            AudioFormat(
                content_type=content_type,
                sample_rate=sample_rate or 44100,
                bit_depth=bit_depth,
                bit_rate=bit_rate or None,
            ),
            details,
        )

    def _normalize_level_name(self, value: Any) -> str | None:
        """Normalize any level-like value to known quality levels."""
        if not isinstance(value, str):
            return None
        level = value.strip().lower()
        aliases = {
            "hires": QUALITY_HIRES,
            "hi_res": QUALITY_HIRES,
            "hi-res": QUALITY_HIRES,
            "dolby": QUALITY_HIRES,
            "sky": QUALITY_HIRES,
            "jyeffect": QUALITY_JYEFFECT,
            "jymaster": QUALITY_JYMASTER,
            "lossless": QUALITY_LOSSLESS,
            "exhigh": QUALITY_EXHIGH,
            "higher": QUALITY_HIGHER,
            "standard": QUALITY_STANDARD,
        }
        return aliases.get(level)

    def _detect_max_quality_level(self, song_obj: dict[str, Any]) -> str:
        """Detect highest available quality for a track from song/detail fields."""
        level_priority = [
            QUALITY_JYMASTER,
            QUALITY_JYEFFECT,
            QUALITY_HIRES,
            QUALITY_LOSSLESS,
            QUALITY_EXHIGH,
            QUALITY_HIGHER,
            QUALITY_STANDARD,
        ]

        # 1) Prefer explicit quality objects from song/detail.
        for level in level_priority:
            quality_obj = self._get_quality_obj(song_obj, level)
            if isinstance(quality_obj, dict):
                if _to_positive_int(quality_obj.get("br")) or _to_positive_int(
                    quality_obj.get("sr")
                ):
                    return level
                if quality_obj:
                    return level

        # 2) Fallback to privilege-reported max/play/download levels.
        privilege = song_obj.get("privilege")
        if isinstance(privilege, dict):
            best_idx = len(level_priority)
            best_level: str | None = None
            for key in ("maxBrLevel", "dlLevel", "plLevel", "flLevel"):
                normalized = self._normalize_level_name(privilege.get(key))
                if not normalized:
                    continue
                idx = level_priority.index(normalized)
                if idx < best_idx:
                    best_idx = idx
                    best_level = normalized
            if best_level:
                return best_level

        # 3) Fallback to mark bit flag (Hi-Res support).
        mark = _to_positive_int(song_obj.get("mark"))
        if mark and (mark & _HIRES_MARK_FLAG):
            return QUALITY_HIRES

        return QUALITY_STANDARD

    def _apply_track_quality_from_song_detail(self, track: Track, song_obj: dict[str, Any]) -> None:
        """Populate mapping quality/details from detailed song object."""
        max_level = self._detect_max_quality_level(song_obj)
        quality_obj = self._get_quality_obj(song_obj, max_level)
        audio_format, quality_label = self._infer_audio_format_from_level(max_level, quality_obj)
        for mapping in track.provider_mappings:
            if mapping.provider_instance != self.instance_id:
                continue
            mapping.audio_format = audio_format
            mapping.details = quality_label
            break

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse artist object."""
        artist_id = str(artist_obj.get("id") or artist_obj.get("artistId") or "").strip()
        if not artist_id:
            raise InvalidDataError("Artist object missing id")
        name = str(artist_obj.get("name") or "Unknown Artist").strip()
        artist = Artist(
            item_id=artist_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://music.163.com/#/artist?id={artist_id}",
                )
            },
        )
        image_url = (
            artist_obj.get("picUrl")
            or artist_obj.get("img1v1Url")
            or artist_obj.get("cover")
            or artist_obj.get("avatar")
        )
        if isinstance(image_url, str):
            artist.metadata.images = self._make_image_list(image_url, force_square=True)
        return artist

    def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        """Parse album object."""
        album_id = str(album_obj.get("id") or album_obj.get("albumId") or "").strip()
        if not album_id:
            raise InvalidDataError("Album object missing id")
        name = str(album_obj.get("name") or "Unknown Album").strip()
        album = Album(
            item_id=album_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://music.163.com/#/album?id={album_id}",
                )
            },
        )
        if artists := album_obj.get("artists") or album_obj.get("ar"):
            if isinstance(artists, list):
                album.artists = UniqueList()
                for artist_obj in artists:
                    if not isinstance(artist_obj, dict):
                        continue
                    artist_id = str(artist_obj.get("id") or "").strip()
                    artist_name = str(artist_obj.get("name") or "Unknown Artist").strip()
                    if artist_id:
                        album.artists.append(
                            self._get_item_mapping(MediaType.ARTIST, artist_id, artist_name)
                        )
        image_url = (
            album_obj.get("picUrl")
            or album_obj.get("coverUrl")
            or album_obj.get("blurPicUrl")
            or album_obj.get("albumPic")
        )
        if isinstance(image_url, str):
            album.metadata.images = self._make_image_list(image_url)
        publish_time = album_obj.get("publishTime")
        if isinstance(publish_time, int) and publish_time > 0:
            with suppress(OSError, OverflowError, ValueError):
                album.year = datetime.fromtimestamp(publish_time / 1000, tz=UTC).year
        return album

    def _parse_track(self, song_obj: dict[str, Any]) -> Track:
        """Parse song object."""
        track_id = str(song_obj.get("id") or song_obj.get("songId") or "").strip()
        if not track_id:
            raise InvalidDataError("Track object missing id")
        name = str(song_obj.get("name") or "Unknown Track").strip()
        duration = _parse_track_duration_seconds(song_obj)
        max_level = self._detect_max_quality_level(song_obj)
        max_quality_obj = self._get_quality_obj(song_obj, max_level)
        max_audio_format, max_quality_label = self._infer_audio_format_from_level(
            max_level, max_quality_obj
        )
        track = Track(
            item_id=track_id,
            provider=self.instance_id,
            name=name,
            duration=duration,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=max_audio_format,
                    url=f"https://music.163.com/#/song?id={track_id}",
                    details=max_quality_label,
                )
            },
        )

        artists_raw = song_obj.get("ar") or song_obj.get("artists")
        if isinstance(artists_raw, list):
            track.artists = UniqueList()
            for artist_obj in artists_raw:
                if not isinstance(artist_obj, dict):
                    continue
                artist_id = str(artist_obj.get("id") or "").strip()
                artist_name = str(artist_obj.get("name") or "Unknown Artist").strip()
                if artist_id:
                    track.artists.append(
                        self._get_item_mapping(MediaType.ARTIST, artist_id, artist_name)
                    )

        album_raw = song_obj.get("al") or song_obj.get("album")
        if isinstance(album_raw, dict):
            album_id = str(album_raw.get("id") or "").strip()
            album_name = str(album_raw.get("name") or "Unknown Album").strip()
            if album_id:
                track.album = self._get_item_mapping(MediaType.ALBUM, album_id, album_name)
            image_url = (
                album_raw.get("picUrl")
                or album_raw.get("coverUrl")
                or album_raw.get("blurPicUrl")
                or song_obj.get("picUrl")
                or song_obj.get("albumPic")
            )
            if isinstance(image_url, str):
                track.metadata.images = self._make_image_list(image_url)
        return track

    def _parse_playlist(self, playlist_obj: dict[str, Any]) -> Playlist:
        """Parse playlist object."""
        playlist_id = str(playlist_obj.get("id") or playlist_obj.get("playlistId") or "").strip()
        if not playlist_id:
            raise InvalidDataError("Playlist object missing id")
        name = str(playlist_obj.get("name") or "Unknown Playlist").strip()
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://music.163.com/#/playlist?id={playlist_id}",
                )
            },
        )
        if isinstance(playlist_obj.get("description"), str):
            playlist.metadata.description = str(playlist_obj["description"]).strip()
        image_url = playlist_obj.get("coverImgUrl") or playlist_obj.get("picUrl")
        if isinstance(image_url, str):
            playlist.metadata.images = self._make_image_list(image_url)
        return playlist

    def _build_dynamic_playlist(
        self,
        item_id: str,
        name: str,
        translation_key: str | None = None,
        image_url: str | None = None,
    ) -> Playlist:
        """Create a dynamic playlist entry for radio-like flows."""
        playlist = Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            translation_key=translation_key,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            is_dynamic=True,
        )
        # Prefer real station/source artwork, fallback to provider icon.
        playlist.metadata.images = self._make_image_list(image_url or _NCM_PROVIDER_ICON_URL)
        return playlist

    def _parse_heart_mode_playlist_id(self, playlist_id: str) -> tuple[str, str] | None:
        """Parse heart mode dynamic playlist id into (seed_song_id, playlist_id)."""
        if not playlist_id.startswith(f"{_PLAYLIST_HEART_MODE_PREFIX}:"):
            return None
        parts = playlist_id.split(":")
        if len(parts) != 3:
            return None
        seed_song_id, source_playlist_id = parts[1], parts[2]
        if not seed_song_id.isdigit() or not source_playlist_id.isdigit():
            return None
        return seed_song_id, source_playlist_id

    async def _get_song_detail(self, ids: str) -> list[dict[str, Any]]:
        """Fetch song details for one or many ids."""
        payload = await self._client.get("/song/detail", params={"ids": ids}, cookie=self._cookie)
        data = _extract_data(payload)
        songs = data.get("songs")
        if isinstance(songs, list):
            return [item for item in songs if isinstance(item, dict)]
        return []

    async def _get_song_music_detail(self, song_id: str) -> dict[str, Any] | None:
        """Fetch extended quality info (jm/je/hr...) for a single song."""
        payload = await self._client.get(
            "/song/music/detail",
            params={"id": song_id},
            cookie=self._cookie,
        )
        return _extract_data(payload)

    def _merge_quality_objects(
        self, base_song_obj: dict[str, Any], quality_song_obj: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge quality objects from song/music/detail into song/detail object."""
        merged = dict(base_song_obj)
        for key in ("jm", "je", "hr", "sq", "h", "m", "l"):
            value = quality_song_obj.get(key)
            if isinstance(value, dict):
                merged[key] = value
        return merged

    async def _enrich_tracks_with_cover(self, tracks: list[Track]) -> None:
        """Enrich track cover/quality by querying song/detail in chunks."""
        track_ids = [track.item_id for track in tracks if track.item_id]
        if not track_ids:
            return

        details_by_id: dict[str, dict[str, Any]] = {}
        chunk_size = 200
        for idx in range(0, len(track_ids), chunk_size):
            chunk = track_ids[idx : idx + chunk_size]
            with suppress(InvalidDataError, ResourceTemporarilyUnavailable):
                detail_rows = await self._get_song_detail(",".join(chunk))
                for row in detail_rows:
                    row_id = str(row.get("id") or "").strip()
                    if row_id:
                        details_by_id[row_id] = row

        async def _fetch_quality(track_id: str) -> tuple[str, dict[str, Any] | None]:
            try:
                quality_obj = await self._get_song_music_detail(track_id)
            except InvalidDataError, ResourceTemporarilyUnavailable:
                return track_id, None
            return track_id, quality_obj if isinstance(quality_obj, dict) else None

        semaphore = asyncio.Semaphore(8)

        async def _bounded_fetch(track_id: str) -> tuple[str, dict[str, Any] | None]:
            async with semaphore:
                return await _fetch_quality(track_id)

        quality_tasks = [
            _bounded_fetch(track.item_id)
            for track in tracks
            if track.item_id and track.item_id in details_by_id
        ]
        quality_by_id = {
            track_id: quality_obj
            for track_id, quality_obj in (await asyncio.gather(*quality_tasks))
            if isinstance(quality_obj, dict)
        }

        for track in tracks:
            detail_obj = details_by_id.get(track.item_id)
            if not isinstance(detail_obj, dict):
                continue
            quality_obj = quality_by_id.get(track.item_id)
            if isinstance(quality_obj, dict):
                detail_obj = self._merge_quality_objects(detail_obj, quality_obj)
            if not track.metadata.images:
                album_raw = detail_obj.get("al") or detail_obj.get("album")
                if isinstance(album_raw, dict):
                    image_url = (
                        album_raw.get("picUrl")
                        or album_raw.get("coverUrl")
                        or album_raw.get("blurPicUrl")
                        or detail_obj.get("picUrl")
                        or detail_obj.get("albumPic")
                    )
                    if isinstance(image_url, str) and image_url:
                        track.metadata.images = self._make_image_list(image_url)
            self._apply_track_quality_from_song_detail(track, detail_obj)

    async def _fill_track_durations(self, tracks: list[Track]) -> None:
        """Fill missing track durations in bulk from song/detail."""
        missing_tracks = [track for track in tracks if not track.duration and track.item_id]
        if not missing_tracks:
            return
        track_by_id = {track.item_id: track for track in missing_tracks}
        chunk_size = 200
        ids = list(track_by_id)
        for idx in range(0, len(ids), chunk_size):
            chunk = ids[idx : idx + chunk_size]
            rows_by_id: dict[str, dict[str, Any]] = {}
            with suppress(InvalidDataError, ResourceTemporarilyUnavailable):
                detail_rows = await self._get_song_detail(",".join(chunk))
                for row in detail_rows:
                    row_id = str(row.get("id") or "").strip()
                    if row_id:
                        rows_by_id[row_id] = row
            # Some API deployments do not consistently support multi-id lookup.
            # Fallback to single-track detail requests when needed.
            missing_ids = [track_id for track_id in chunk if track_id not in rows_by_id]
            for track_id in missing_ids:
                with suppress(InvalidDataError, ResourceTemporarilyUnavailable):
                    single_rows = await self._get_song_detail(track_id)
                    if single_rows and isinstance(single_rows[0], dict):
                        rows_by_id[track_id] = single_rows[0]
            for track_id in chunk:
                track = track_by_id.get(track_id)
                if not track or track.duration:
                    continue
                row_data = rows_by_id.get(track_id)
                if row_data is None:
                    continue
                duration = _parse_track_duration_seconds(row_data)
                if duration > 0:
                    track.duration = duration

    def _search_plan(self, media_types: list[MediaType]) -> list[tuple[MediaType, int]]:
        """Build NCM search type plan from requested media types."""
        plan: list[tuple[MediaType, int]] = []
        if MediaType.TRACK in media_types:
            plan.append((MediaType.TRACK, 1))
        if MediaType.ARTIST in media_types:
            plan.append((MediaType.ARTIST, 100))
        if MediaType.ALBUM in media_types:
            plan.append((MediaType.ALBUM, 10))
        if MediaType.PLAYLIST in media_types:
            plan.append((MediaType.PLAYLIST, 1000))
        return plan

    async def _search_single(
        self, search_query: str, *, type_code: int, limit: int
    ) -> dict[str, Any]:
        """Run one NCM search request by type code."""
        return await self._client.get(
            "/search",
            params={"keywords": search_query, "type": type_code, "limit": limit},
            cookie=self._cookie,
        )

    def _parse_search_tracks(self, search_result: dict[str, Any], limit: int) -> list[Track]:
        """Parse track search result items."""
        tracks: list[Track] = []
        songs = search_result.get("songs")
        if not isinstance(songs, list):
            return tracks
        for song in songs[:limit]:
            if not isinstance(song, dict):
                continue
            with suppress(InvalidDataError):
                tracks.append(self._parse_track(song))
        return tracks

    def _parse_search_artists(self, search_result: dict[str, Any], limit: int) -> list[Artist]:
        """Parse artist search result items."""
        artists_result: list[Artist] = []
        artists = search_result.get("artists")
        if not isinstance(artists, list):
            return artists_result
        for artist in artists[:limit]:
            if not isinstance(artist, dict):
                continue
            with suppress(InvalidDataError):
                artists_result.append(self._parse_artist(artist))
        return artists_result

    def _parse_search_albums(self, search_result: dict[str, Any], limit: int) -> list[Album]:
        """Parse album search result items."""
        albums_result: list[Album] = []
        albums = search_result.get("albums")
        if not isinstance(albums, list):
            return albums_result
        for album in albums[:limit]:
            if not isinstance(album, dict):
                continue
            with suppress(InvalidDataError):
                albums_result.append(self._parse_album(album))
        return albums_result

    def _parse_search_playlists(self, search_result: dict[str, Any], limit: int) -> list[Playlist]:
        """Parse playlist search result items."""
        playlists_result: list[Playlist] = []
        playlists = search_result.get("playlists")
        if not isinstance(playlists, list):
            return playlists_result
        for playlist in playlists[:limit]:
            if not isinstance(playlist, dict):
                continue
            with suppress(InvalidDataError):
                playlists_result.append(self._parse_playlist(playlist))
        return playlists_result

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on NetEase Cloud Music."""
        result = SearchResults()
        track_results: list[Track] = []
        artist_results: list[Artist] = []
        album_results: list[Album] = []
        playlist_results: list[Playlist] = []
        search_plan = self._search_plan(media_types)
        responses = await asyncio.gather(
            *[
                self._search_single(search_query, type_code=type_code, limit=limit)
                for _, type_code in search_plan
            ],
            return_exceptions=True,
        )
        for idx, (media_type, _) in enumerate(search_plan):
            response = responses[idx]
            if isinstance(response, BaseException):
                self.logger.debug("NCM search failed for media_type=%s: %s", media_type, response)
                continue
            data = _extract_data(response)
            search_result = data.get("result")
            if not isinstance(search_result, dict):
                continue
            if media_type == MediaType.TRACK:
                track_results.extend(self._parse_search_tracks(search_result, limit))
            elif media_type == MediaType.ARTIST:
                artist_results.extend(self._parse_search_artists(search_result, limit))
            elif media_type == MediaType.ALBUM:
                album_results.extend(self._parse_search_albums(search_result, limit))
            elif media_type == MediaType.PLAYLIST:
                playlist_results.extend(self._parse_search_playlists(search_result, limit))
        result.tracks = track_results
        await self._enrich_tracks_with_cover(track_results)
        result.artists = artist_results
        result.albums = album_results
        result.playlists = playlist_results
        return result

    @use_cache(3600 * 24)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        payload = await self._client.get(
            "/artist/detail",
            params={"id": prov_artist_id},
            cookie=self._cookie,
        )
        data = _extract_data(payload)
        artist_obj = data.get("artist")
        if not isinstance(artist_obj, dict):
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return self._parse_artist(artist_obj)

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get all albums for an artist."""
        limit = 100
        offset = 0
        seen_album_ids: set[str] = set()
        albums: list[Album] = []
        for _ in range(50):
            payload = await self._client.get(
                "/artist/album",
                params={"id": prov_artist_id, "limit": limit, "offset": offset},
                cookie=self._cookie,
            )
            data = _extract_data(payload)
            raw_albums = data.get("hotAlbums") or data.get("albums")
            if not isinstance(raw_albums, list) or not raw_albums:
                break

            for album_obj in raw_albums:
                if not isinstance(album_obj, dict):
                    continue
                album_id = str(album_obj.get("id") or album_obj.get("albumId") or "").strip()
                if album_id and album_id in seen_album_ids:
                    continue
                with suppress(InvalidDataError):
                    album = self._parse_album(album_obj)
                    albums.append(album)
                    seen_album_ids.add(album.item_id)

            has_more = bool(data.get("more") or data.get("hasMore"))
            offset += limit
            if not has_more and len(raw_albums) < limit:
                break
        return albums

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks for given artist."""
        payload = await self._client.get(
            "/artist/top/song",
            params={"id": prov_artist_id},
            cookie=self._cookie,
        )
        data = _extract_data(payload)
        songs = data.get("songs")
        if not isinstance(songs, list):
            return []
        tracks: list[Track] = []
        for song_obj in songs:
            if not isinstance(song_obj, dict):
                continue
            with suppress(InvalidDataError):
                track = self._parse_track(song_obj)
                if track.duration <= 0:
                    # Album payload duration fields are authoritative for this endpoint;
                    # keep an explicit fallback here to avoid zero-length tracks.
                    duration = _parse_track_duration_seconds(song_obj)
                    if duration > 0:
                        track.duration = duration
                tracks.append(track)
        return tracks

    @use_cache(3600 * 24)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        payload = await self._client.get(
            "/album",
            params={"id": prov_album_id},
            cookie=self._cookie,
        )
        data = _extract_data(payload)
        album_obj = data.get("album")
        if not isinstance(album_obj, dict):
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return self._parse_album(album_obj)

    @use_cache(allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for album id."""
        payload = await self._client.get(
            "/album",
            params={"id": prov_album_id},
            cookie=self._cookie,
        )
        data = _extract_data(payload)
        songs = data.get("songs")
        if not isinstance(songs, list):
            return []
        tracks: list[Track] = []
        for song_obj in songs:
            if not isinstance(song_obj, dict):
                continue
            with suppress(InvalidDataError):
                tracks.append(self._parse_track(song_obj))
        return tracks

    @use_cache()
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        songs = await self._get_song_detail(prov_track_id)
        if not songs:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        song_obj = songs[0]
        with suppress(InvalidDataError, ResourceTemporarilyUnavailable):
            quality_obj = await self._get_song_music_detail(prov_track_id)
            if isinstance(quality_obj, dict):
                song_obj = self._merge_quality_objects(song_obj, quality_obj)
        track = self._parse_track(song_obj)
        with suppress(InvalidDataError, ResourceTemporarilyUnavailable):
            lyric_payload = await self._client.get(
                "/lyric",
                params={"id": prov_track_id},
                cookie=self._cookie,
            )
            lyric_data = _extract_data(lyric_payload)
            lrc = (
                lyric_data.get("lrc", {}).get("lyric")
                if isinstance(lyric_data.get("lrc"), dict)
                else ""
            )
            tlyric = (
                lyric_data.get("tlyric", {}).get("lyric")
                if isinstance(lyric_data.get("tlyric"), dict)
                else ""
            )
            lrc_text = str(lrc or "").strip()
            tlyric_text = str(tlyric or "").strip()
            if lrc_text and _LRC_TIMESTAMP_PATTERN.search(lrc_text):
                track.metadata.lrc_lyrics = lrc_text
                track.metadata.lyrics = _lrc_to_plain_text(lrc_text) or lrc_text
            elif lrc_text:
                track.metadata.lyrics = lrc_text
            elif tlyric_text:
                track.metadata.lyrics = tlyric_text
        return track

    @use_cache(3600 * 24)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if prov_playlist_id == _PLAYLIST_PERSONAL_FM_ID:
            return self._build_dynamic_playlist(
                _PLAYLIST_PERSONAL_FM_ID, "Personal FM", translation_key="personal_fm"
            )
        if heart_parts := self._parse_heart_mode_playlist_id(prov_playlist_id):
            seed_song_id, source_playlist_id = heart_parts
            return self._build_dynamic_playlist(
                f"{_PLAYLIST_HEART_MODE_PREFIX}:{seed_song_id}:{source_playlist_id}",
                "Heart Mode",
                translation_key="heart_mode",
            )
        if prov_playlist_id == _PLAYLIST_HEART_MODE_PREFIX:
            if playlist := await self._build_heart_mode_dynamic_playlist():
                return playlist
            raise MediaNotFoundError("Heart mode is currently unavailable, please try again later")

        payload = await self._client.get(
            "/playlist/detail",
            params={"id": prov_playlist_id},
            cookie=self._cookie,
        )
        data = _extract_data(payload)
        playlist_obj = data.get("playlist")
        if not isinstance(playlist_obj, dict):
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        return self._parse_playlist(playlist_obj)

    @use_cache(3600 * 3)
    async def _get_playlist_tracks_cached(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> Sequence[Track]:
        """Get playlist tracks for static playlists (cached)."""
        limit = 500
        offset = page * limit
        payload = await self._client.get(
            "/playlist/track/all",
            params={"id": prov_playlist_id, "limit": limit, "offset": offset},
            cookie=self._cookie,
        )
        data = _extract_data(payload)
        songs = data.get("songs")
        if not isinstance(songs, list):
            return []
        result: list[Track] = []
        for idx, song_obj in enumerate(songs, start=1):
            if not isinstance(song_obj, dict):
                continue
            with suppress(InvalidDataError):
                track = self._parse_track(song_obj)
                track.position = offset + idx
                result.append(track)
        return result

    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> Sequence[Track]:
        """Get all playlist tracks for given playlist id."""
        if prov_playlist_id == _PLAYLIST_PERSONAL_FM_ID:
            if page > 0:
                return []
            tracks = await self._pick_personal_fm_tracks(fresh=True, target_count=12)
            for idx, track in enumerate(tracks, start=1):
                track.position = idx
            return tracks
        if heart_parts := self._parse_heart_mode_playlist_id(prov_playlist_id):
            if page > 0:
                return []
            seed_song_id, source_playlist_id = heart_parts
            tracks = await self._pick_heart_mode_tracks(
                seed_song_id,
                source_playlist_id,
                count=20,
            )
            for idx, track in enumerate(tracks, start=1):
                track.position = idx
            return tracks
        if prov_playlist_id == _PLAYLIST_HEART_MODE_PREFIX:
            if page > 0:
                return []
            if playlist := await self._build_heart_mode_dynamic_playlist():
                heart_parts = self._parse_heart_mode_playlist_id(playlist.item_id)
                if heart_parts:
                    seed_song_id, source_playlist_id = heart_parts
                    tracks = await self._pick_heart_mode_tracks(
                        seed_song_id,
                        source_playlist_id,
                        count=20,
                    )
                    for idx, track in enumerate(tracks, start=1):
                        track.position = idx
                    return tracks
            return []

        return await self._get_playlist_tracks_cached(prov_playlist_id, page)

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve favorite artists from NCM."""
        limit = 200
        offset = 0
        for _ in range(100):
            payload = await self._client.get(
                "/artist/sublist",
                params={"limit": limit, "offset": offset, "cookie": self._cookie},
                cookie=self._cookie,
            )
            data = _extract_data(payload)
            artists = data.get("data") or data.get("artists")
            if not isinstance(artists, list) or not artists:
                break
            for artist_obj in artists:
                if not isinstance(artist_obj, dict):
                    continue
                try:
                    yield self._parse_artist(artist_obj)
                except InvalidDataError as err:
                    self.report_skipped_sync_item(MediaType.ARTIST, None, err)
            has_more = bool(data.get("more") or data.get("hasMore"))
            offset += limit
            if not has_more and len(artists) < limit:
                break

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve favorite albums from NCM."""
        limit = 200
        offset = 0
        for _ in range(100):
            payload = await self._client.get(
                "/album/sublist",
                params={"limit": limit, "offset": offset, "cookie": self._cookie},
                cookie=self._cookie,
            )
            data = _extract_data(payload)
            albums = data.get("data") or data.get("albums")
            if not isinstance(albums, list) or not albums:
                break
            for album_obj in albums:
                if not isinstance(album_obj, dict):
                    continue
                try:
                    yield self._parse_album(album_obj)
                except InvalidDataError as err:
                    self.report_skipped_sync_item(MediaType.ALBUM, None, err)
            has_more = bool(data.get("more") or data.get("hasMore"))
            offset += limit
            if not has_more and len(albums) < limit:
                break

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve liked tracks from NCM."""
        payload = await self._client.get(
            "/likelist",
            params={"uid": self._uid, "cookie": self._cookie},
            cookie=self._cookie,
        )
        data = _extract_data(payload)
        ids = data.get("ids") or payload.get("ids")
        if not isinstance(ids, list):
            return
        track_ids = [str(item) for item in ids if str(item).isdigit()]
        chunk_size = 200
        for idx in range(0, len(track_ids), chunk_size):
            chunk_ids = track_ids[idx : idx + chunk_size]
            songs = await self._get_song_detail(",".join(chunk_ids))
            fetched_ids: set[str] = set()
            for song_obj in songs:
                fetched_ids.add(str(song_obj.get("id") or song_obj.get("songId") or "").strip())
                try:
                    yield self._parse_track(song_obj)
                except InvalidDataError as err:
                    self.report_skipped_sync_item(MediaType.TRACK, None, err)
            # the liked list is authoritative, so a track the detail call left out is still
            # in the library and must not be read as removed
            for missing_id in chunk_ids:
                if missing_id not in fetched_ids:
                    self.report_skipped_sync_item(
                        MediaType.TRACK,
                        missing_id,
                        MediaNotFoundError(f"NCM did not return track {missing_id}"),
                    )

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve user playlists from NCM."""
        payload = await self._client.get(
            "/user/playlist",
            params={"uid": self._uid, "limit": 1000, "offset": 0, "cookie": self._cookie},
            cookie=self._cookie,
        )
        data = _extract_data(payload)
        playlists = data.get("playlist")
        if not isinstance(playlists, list):
            return
        for playlist_obj in playlists:
            if not isinstance(playlist_obj, dict):
                continue
            try:
                yield self._parse_playlist(playlist_obj)
            except InvalidDataError as err:
                self.report_skipped_sync_item(MediaType.PLAYLIST, None, err)

    async def _get_recommend_payload_cached(
        self,
        key: str,
        ttl: int,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return recommendation payload from MA cache or fetch fresh."""
        params_key = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
        cache_key = f"{key}:{params_key}"
        cached = await self.mass.cache.get(
            key=cache_key,
            provider=self.instance_id,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
            default=None,
        )
        if cached is not None:
            if isinstance(cached, dict):
                self.logger.debug("NCM recommendations %s payload cache hit", key)
                return cached
        payload = await self._client.get(path, params=params, cookie=self._cookie)
        await self.mass.cache.set(
            key=cache_key,
            provider=self.instance_id,
            category=CACHE_CATEGORY_RECOMMENDATIONS,
            data=payload,
            expiration=ttl,
        )
        return payload

    async def _pick_personal_fm_tracks(
        self, *, fresh: bool = False, target_count: int = 1
    ) -> list[Track]:
        """Fetch personal FM tracks, optionally aggregate multiple fresh pulls."""
        if not fresh:
            fm_payload = await self._get_recommend_payload_cached(
                "personal_fm",
                _RECOMMEND_PERSONAL_FM_TTL,
                "/personal_fm",
            )
            fm_data = _extract_data(fm_payload)
            fm_songs = fm_data.get("data")
            if not isinstance(fm_songs, list) or not fm_songs:
                return []
            cached_tracks: list[Track] = []
            for fm_item in fm_songs:
                if not isinstance(fm_item, dict):
                    continue
                song_obj = fm_item.get("song") if isinstance(fm_item.get("song"), dict) else fm_item
                if not isinstance(song_obj, dict):
                    continue
                with suppress(InvalidDataError):
                    cached_tracks.append(self._parse_track(song_obj))
            return cached_tracks

        # Fresh mode for dynamic playback: call endpoint in bounded batches and deduplicate tracks.
        result: list[Track] = []
        seen_ids: set[str] = set()
        attempts = max(2, min(max(target_count, 4), 8))
        no_new_rounds = 0
        for _ in range(attempts):
            fm_payload = await self._client.get(
                "/personal_fm",
                params={"timestamp": int(time.time() * 1000)},
                cookie=self._cookie,
            )
            fm_data = _extract_data(fm_payload)
            fm_songs = fm_data.get("data")
            if not isinstance(fm_songs, list) or not fm_songs:
                no_new_rounds += 1
                if no_new_rounds >= 2:
                    break
                continue
            before_count = len(result)
            for fm_item in fm_songs:
                if not isinstance(fm_item, dict):
                    continue
                song_obj = fm_item.get("song") if isinstance(fm_item.get("song"), dict) else fm_item
                if not isinstance(song_obj, dict):
                    continue
                with suppress(InvalidDataError):
                    track = self._parse_track(song_obj)
                    if track.item_id in seen_ids:
                        continue
                    seen_ids.add(track.item_id)
                    result.append(track)
            if len(result) == before_count:
                no_new_rounds += 1
            else:
                no_new_rounds = 0
            if len(result) >= target_count:
                break
            if no_new_rounds >= 2:
                break
        return filter_tracks(result)

    async def _get_heart_mode_seed(self) -> tuple[str, str, str | None] | None:
        """Resolve heart mode seed ids as (seed_song_id, playlist_id, image_url)."""
        daily_payload = await self._get_recommend_payload_cached(
            "daily_songs",
            _RECOMMEND_DAILY_TTL,
            "/recommend/songs",
        )
        daily_data = _extract_data(daily_payload)
        daily_songs = daily_data.get("dailySongs")
        if not isinstance(daily_songs, list) or not daily_songs:
            return None
        seed_song = next(
            (item for item in daily_songs if isinstance(item, dict) and item.get("id")),
            None,
        )
        if not isinstance(seed_song, dict):
            return None
        seed_song_id = str(seed_song.get("id") or "").strip()
        if not seed_song_id.isdigit():
            return None
        seed_song_image = _extract_song_image_url(seed_song)

        playlist_payload = await self._get_recommend_payload_cached(
            "heart_mode_playlist",
            _RECOMMEND_HEART_MODE_TTL,
            "/user/playlist",
            {"uid": self._uid, "limit": 1, "offset": 0},
        )
        playlist_data = _extract_data(playlist_payload)
        playlist_rows = playlist_data.get("playlist")
        if not isinstance(playlist_rows, list) or not playlist_rows:
            return None
        first_playlist = playlist_rows[0]
        if not isinstance(first_playlist, dict):
            return None
        playlist_id = str(first_playlist.get("id") or "").strip()
        if not playlist_id.isdigit():
            return None
        playlist_cover = first_playlist.get("coverImgUrl") or first_playlist.get("picUrl")
        if not seed_song_image and isinstance(playlist_cover, str) and playlist_cover.strip():
            seed_song_image = playlist_cover.strip()

        return seed_song_id, playlist_id, seed_song_image

    async def _build_heart_mode_dynamic_playlist(self) -> Playlist | None:
        """Build heart mode dynamic playlist item."""
        heart_parts = await self._get_heart_mode_seed()
        if heart_parts is None:
            return None
        seed_song_id, playlist_id, image_url = heart_parts
        return self._build_dynamic_playlist(
            f"{_PLAYLIST_HEART_MODE_PREFIX}:{seed_song_id}:{playlist_id}",
            "Heart Mode",
            translation_key="heart_mode",
            image_url=image_url,
        )

    async def _pick_heart_mode_tracks(
        self, seed_song_id: str, playlist_id: str, count: int = 20
    ) -> list[Track]:
        """Fetch heart mode recommendation tracks."""
        payload = await self._client.get(
            "/playmode/intelligence/list",
            params={
                "id": seed_song_id,
                "pid": playlist_id,
                "sid": seed_song_id,
                "count": count,
                "cookie": self._cookie,
            },
            cookie=self._cookie,
        )
        data = _extract_data(payload)
        rows = data.get("data")
        if not isinstance(rows, list) or not rows:
            return []
        result: list[Track] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            song_obj = None
            for key in ("songInfo", "song", "songData", "trackData"):
                if isinstance(row.get(key), dict):
                    song_obj = row[key]
                    break
            if song_obj is None and isinstance(row.get("id"), (int, str)):
                song_obj = row
            if not isinstance(song_obj, dict):
                continue
            with suppress(InvalidDataError):
                result.append(self._parse_track(song_obj))
        return filter_tracks(result)

    def _quality_candidates(self) -> list[str]:
        """Return ordered quality levels based on config."""
        raw_quality = str(self.config.get_value(CONF_QUALITY) or QUALITY_EXHIGH).lower()
        explicit_levels = {
            QUALITY_STANDARD,
            QUALITY_HIGHER,
            QUALITY_EXHIGH,
            QUALITY_LOSSLESS,
            QUALITY_HIRES,
            QUALITY_JYEFFECT,
            QUALITY_JYMASTER,
        }
        quality = (
            raw_quality
            if raw_quality in explicit_levels
            else (self._normalize_level_name(raw_quality) or raw_quality)
        )
        if quality == QUALITY_JYMASTER:
            return [
                QUALITY_JYMASTER,
                QUALITY_JYEFFECT,
                QUALITY_HIRES,
                QUALITY_LOSSLESS,
                QUALITY_EXHIGH,
                QUALITY_HIGHER,
                QUALITY_STANDARD,
            ]
        if quality == QUALITY_JYEFFECT:
            return [
                QUALITY_JYEFFECT,
                QUALITY_HIRES,
                QUALITY_LOSSLESS,
                QUALITY_EXHIGH,
                QUALITY_HIGHER,
                QUALITY_STANDARD,
            ]
        if quality == QUALITY_HIRES:
            return [
                QUALITY_JYMASTER,
                QUALITY_JYEFFECT,
                QUALITY_HIRES,
                QUALITY_LOSSLESS,
                QUALITY_EXHIGH,
                QUALITY_HIGHER,
                QUALITY_STANDARD,
            ]
        if quality == QUALITY_LOSSLESS:
            return [QUALITY_LOSSLESS, QUALITY_EXHIGH, QUALITY_HIGHER, QUALITY_STANDARD]
        if quality == QUALITY_EXHIGH:
            return [QUALITY_EXHIGH, QUALITY_HIGHER, QUALITY_STANDARD]
        if quality == QUALITY_HIGHER:
            return [QUALITY_HIGHER, QUALITY_STANDARD]
        if quality == QUALITY_STANDARD:
            return [QUALITY_STANDARD]
        return [QUALITY_EXHIGH, QUALITY_HIGHER, QUALITY_STANDARD]

    def _parse_content_type(self, stream_type: str | None, url: str) -> ContentType:
        """Map stream type/ext to MA content type."""
        if stream_type:
            lowered = stream_type.lower()
            if lowered == "flac":
                return ContentType.FLAC
            if lowered in ("mp3", "mpeg"):
                return ContentType.MP3
            if lowered in ("aac", "m4a"):
                return ContentType.AAC
        path = urlparse(url).path.lower()
        if path.endswith(".flac"):
            return ContentType.FLAC
        if path.endswith(".mp3"):
            return ContentType.MP3
        if path.endswith((".m4a", ".aac")):
            return ContentType.AAC
        return ContentType.UNKNOWN

    def _is_preview_stream(
        self,
        stream_info: dict[str, Any],
        track_duration_ms: int | None = None,
    ) -> bool:
        """Return True when stream payload indicates a trial/preview clip."""
        # 1) Strong signal: explicit free trial fragment window.
        free_trial_info = stream_info.get("freeTrialInfo")
        if isinstance(free_trial_info, dict):
            trial_start = _to_positive_int(free_trial_info.get("start"))
            trial_end = _to_positive_int(free_trial_info.get("end"))
            if trial_end > trial_start:
                return True

        # 2) Fallback signal: returned stream duration is much shorter than track duration.
        # Some responses include `freeTrialPrivilege` even for playable tracks, so we do not
        # treat its mere presence as preview.
        stream_time_ms = _to_positive_int(stream_info.get("time"))
        if track_duration_ms and stream_time_ms:
            # Keep a small tolerance to avoid false positives on rounding differences.
            if (stream_time_ms + 5000) < track_duration_ms:
                return True

        return False

    async def _get_track_stream_details(  # noqa: PLR0915
        self,
        track_id: str,
        *,
        stream_item_id: str,
        media_type: MediaType,
        allow_seek: bool,
    ) -> StreamDetails:
        """Resolve stream details for a concrete track id."""
        detail_obj: dict[str, Any] | None = None
        track_duration_ms: int | None = None
        track_duration_seconds: int | None = None
        with suppress(InvalidDataError, ResourceTemporarilyUnavailable):
            detail_rows = await self._get_song_detail(track_id)
            if detail_rows:
                detail_obj = detail_rows[0]
                if isinstance(detail_obj, dict):
                    track_duration_ms = _to_positive_int(detail_obj.get("dt")) or None
                    parsed_duration = _parse_track_duration_seconds(detail_obj)
                    if parsed_duration > 0:
                        track_duration_seconds = parsed_duration
        preview_fallback: StreamDetails | None = None
        for requested_level in self._quality_candidates():
            payload = await self._client.get(
                "/song/url/v1",
                params={"id": track_id, "level": requested_level},
                cookie=_with_pc_os_cookie(self._cookie),
            )
            data = _extract_data(payload)
            stream_rows = data.get("data")
            if not isinstance(stream_rows, list) or not stream_rows:
                continue
            stream_info = stream_rows[0] if isinstance(stream_rows[0], dict) else {}
            if not stream_info:
                continue
            stream_url = str(stream_info.get("url") or "").strip()
            if not stream_url:
                continue
            resolved_level = str(stream_info.get("level") or requested_level).lower()
            normalized_level = self._normalize_level_name(resolved_level) or resolved_level
            preview = self._is_preview_stream(stream_info, track_duration_ms)
            level_quality_obj = (
                self._get_quality_obj(detail_obj, normalized_level)
                if isinstance(detail_obj, dict)
                else None
            )
            inferred_format, _ = self._infer_audio_format_from_level(
                normalized_level, level_quality_obj
            )
            detected_content_type = self._parse_content_type(
                str(stream_info.get("type") or stream_info.get("encodeType") or ""),
                stream_url,
            )
            audio_format = AudioFormat(
                content_type=(
                    inferred_format.content_type
                    if detected_content_type == ContentType.UNKNOWN
                    else detected_content_type
                ),
                sample_rate=inferred_format.sample_rate,
                bit_depth=inferred_format.bit_depth,
                bit_rate=inferred_format.bit_rate,
            )
            bitrate = stream_info.get("br")
            if isinstance(bitrate, int) and bitrate > 0:
                audio_format.bit_rate = bitrate
            stream_sr = _to_positive_int(stream_info.get("sr"))
            if stream_sr > 0 and (
                level_quality_obj is None or audio_format.sample_rate in (0, 44100)
            ):
                audio_format.sample_rate = stream_sr
            stream_time_ms = _to_positive_int(stream_info.get("time"))
            stream_duration_seconds = int(stream_time_ms / 1000) if stream_time_ms > 0 else None
            expiration = 3600
            with suppress(TypeError, ValueError):
                parsed = parse_qs(urlparse(stream_url).query)
                if expire_raw := parsed.get("expire", [None])[0]:
                    expiration = max(60, int(expire_raw) - int(time.time()))
            details = StreamDetails(
                provider=self.instance_id,
                item_id=stream_item_id,
                media_type=media_type,
                audio_format=audio_format,
                stream_type=StreamType.HTTP,
                path=stream_url,
                can_seek=allow_seek,
                allow_seek=allow_seek,
                duration=track_duration_seconds or stream_duration_seconds,
                expiration=expiration,
                data={
                    "preview": preview,
                    "requested_level": requested_level,
                    "resolved_level": resolved_level,
                    "track_id": track_id,
                },
            )
            self.logger.debug(
                "NCM stream selected item=%s track=%s requested=%s resolved=%s preview=%s format=%s/%s bitrate=%s",
                stream_item_id,
                track_id,
                requested_level,
                resolved_level,
                preview,
                audio_format.sample_rate,
                audio_format.bit_depth,
                audio_format.bit_rate,
            )
            if not preview:
                return details
            if preview_fallback is None:
                preview_fallback = details
        # If account/song entitlement only allows trial playback, return preview stream.
        if preview_fallback is not None:
            return preview_fallback
        raise UnplayableMediaError(f"No playable stream URL returned for track {track_id}")

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return streamdetails for track."""
        if media_type == MediaType.TRACK:
            return await self._get_track_stream_details(
                item_id,
                stream_item_id=item_id,
                media_type=MediaType.TRACK,
                allow_seek=True,
            )
        raise UnsupportedFeaturedException(f"Unsupported media type {media_type}")

    async def _build_radio_items(self) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """Build the dynamic radio playlist items for the recommended_radios row."""
        items: UniqueList[MediaItemType | ItemMapping | BrowseFolder] = UniqueList()
        personal_fm_image_url: str | None = None
        with suppress(InvalidDataError, ResourceTemporarilyUnavailable):
            fm_payload = await self._get_recommend_payload_cached(
                "personal_fm",
                _RECOMMEND_PERSONAL_FM_TTL,
                "/personal_fm",
            )
            fm_data = _extract_data(fm_payload)
            fm_rows = fm_data.get("data")
            if isinstance(fm_rows, list) and fm_rows and isinstance(fm_rows[0], dict):
                fm_item = fm_rows[0]
                song_obj = fm_item.get("song") if isinstance(fm_item.get("song"), dict) else fm_item
                if isinstance(song_obj, dict):
                    personal_fm_image_url = _extract_song_image_url(song_obj)
        if not personal_fm_image_url:
            with suppress(InvalidDataError, ResourceTemporarilyUnavailable):
                daily_payload = await self._get_recommend_payload_cached(
                    "daily_songs",
                    _RECOMMEND_DAILY_TTL,
                    "/recommend/songs",
                )
                daily_data = _extract_data(daily_payload)
                daily_rows = daily_data.get("dailySongs")
                if isinstance(daily_rows, list) and daily_rows and isinstance(daily_rows[0], dict):
                    personal_fm_image_url = _extract_song_image_url(daily_rows[0])
        items.append(
            self._build_dynamic_playlist(
                _PLAYLIST_PERSONAL_FM_ID,
                "Personal FM",
                translation_key="personal_fm",
                image_url=personal_fm_image_url,
            )
        )
        if heart_playlist := await self._build_heart_mode_dynamic_playlist():
            items.append(heart_playlist)
        return items
