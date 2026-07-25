"""QQ Music provider implementation."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from asyncio import Semaphore
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
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
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from qqmusic_api import (
    CgiApiException,
    Credential,
    CredentialExpiredError,
    CredentialRefreshError,
    LoginError,
)
from qqmusic_api import (
    Client as QQClient,
)
from qqmusic_api.algorithms import qrc_decrypt
from qqmusic_api.modules.search import SearchType
from qqmusic_api.modules.singer import TabType
from qqmusic_api.modules.song import SongFileInfo, SongFileType, SpecialSongFileType

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_CREDENTIAL_JSON,
    CONF_LOGIN_TYPE,
    CONF_MUSICID,
    CONF_MUSICKEY,
    CONF_QUALITY,
    CONF_UIN,
    QUALITY_FLAC,
    QUALITY_HI_RES,
    QUALITY_MP3_128,
    QUALITY_MP3_320,
)
from .helpers import extract_first_text, normalize_qq_lyric_text, qrc_to_lrc
from .parsers import (
    build_playlist_id,
    extract_guess_recommend_tracks,
    extract_items,
    extract_newsong_tracks,
    extract_radar_recommend_tracks,
    extract_recommend_songlists,
    extract_song_id,
    get_artist_mapping,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_playlist_id,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
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
    ProviderFeature.ARTIST_TRACKS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.SIMILAR_ARTISTS,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.LYRICS,
}

_LRC_TIMESTAMP_PATTERN = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
_HEX_LYRIC_PATTERN = re.compile(r"^[0-9A-Fa-f]{32,}$")
_RECOMMEND_GUESS_TTL = 60 * 60
_RECOMMEND_NEWSONG_TTL = 60 * 60 * 6
_RECOMMEND_PLAYLIST_TTL = 60 * 60 * 6


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return QQMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


def _store_credential(values: dict[str, ConfigValueType], credential: Any) -> None:
    if not credential.musicid or not credential.musickey:
        raise LoginFailed("QR login succeeded but credential is incomplete")
    if callable(getattr(credential, "model_dump_json", None)):
        credential_json = credential.model_dump_json(by_alias=True)
    else:
        fallback_credential = Credential.model_validate(
            {
                "musicid": int(credential.musicid),
                "musickey": str(credential.musickey),
                "loginType": int(getattr(credential, "login_type", 2) or 2),
                "refresh_key": str(getattr(credential, "refresh_key", "") or ""),
                "refresh_token": str(getattr(credential, "refresh_token", "") or ""),
                "encryptUin": str(getattr(credential, "encrypt_uin", "") or ""),
                "str_musicid": str(getattr(credential, "str_musicid", "") or ""),
            }
        )
        credential_json = fallback_credential.model_dump_json(by_alias=True)
    values[CONF_UIN] = str(credential.musicid)
    values[CONF_MUSICID] = str(credential.musicid)
    values[CONF_MUSICKEY] = str(credential.musickey)
    values[CONF_LOGIN_TYPE] = str(credential.login_type or 2)
    values[CONF_CREDENTIAL_JSON] = credential_json


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return the configuration (options) entries for the QQ Music provider.

    Authentication runs in the interactive setup flow (see ``setup_flow.py``); the only
    genuine option configured here is the preferred streaming quality.

    :param mass: The MusicAssistant instance.
    :param instance_id: Optional existing provider instance id (unused).
    :param action: Unused; retained for the config-entries signature contract.
    :param values: Unused; retained for the config-entries signature contract.
    """
    return (
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            default_value=QUALITY_MP3_320,
            options=[
                ConfigValueOption(QUALITY_MP3_128),
                ConfigValueOption(QUALITY_MP3_320),
                ConfigValueOption(QUALITY_FLAC),
                ConfigValueOption(QUALITY_HI_RES),
            ],
        ),
    )


class QQMusicProvider(MusicProvider):
    """QQ Music provider."""

    _credential: Any = None
    _qq_search: Any = None
    _qq_song: Any = None
    _qq_album: Any = None
    _qq_singer: Any = None
    _qq_client: Any = None
    _qq_user: Any = None
    _qq_songlist: Any = None
    _qq_lyric: Any = None
    _qq_recommend: Any = None
    _api_semaphore: Semaphore
    _credential_refresh_lock: asyncio.Lock
    _last_credential_check_monotonic: float
    _musicid: int = 0
    _euin: str = ""
    _recommend_payload_cache: dict[str, tuple[float, Any]]

    async def handle_async_init(self) -> None:
        """Validate auth and initialize qqmusic api adapters."""
        credential: Credential | None = None
        if credential_json := str(self.get_setup_value(CONF_CREDENTIAL_JSON) or "").strip():
            try:
                credential = Credential.model_validate_json(credential_json)
            except Exception as err:
                self.logger.warning(
                    "Failed to parse persisted QQ credential_json, fallback to legacy fields: %s",
                    err,
                )

        if not credential or not credential.musicid or not credential.musickey:
            config_musicid = self.get_setup_value(CONF_MUSICID) or self.get_setup_value(CONF_UIN)
            config_musickey = self.get_setup_value(CONF_MUSICKEY)
            config_login_type = self.get_setup_value(CONF_LOGIN_TYPE)
            if not (config_musicid and config_musickey):
                raise LoginFailed("No QQ Music authentication configured, please login by QR code")
            login_type_raw = str(config_login_type or "2")
            login_type = int(login_type_raw) if login_type_raw.isdigit() else 2
            credential = Credential.model_validate(
                {
                    "musicid": int(str(config_musicid).strip()),
                    "musickey": str(config_musickey),
                    "loginType": login_type,
                }
            )
        if not credential.encrypt_uin:
            raise LoginFailed(
                "QQ Music credential is missing encryptUin, please re-authenticate by QR code"
            )

        self._qq_client = QQClient(credential=credential)
        self._qq_search = self._qq_client.search
        self._qq_song = self._qq_client.song
        self._qq_album = self._qq_client.album
        self._qq_singer = self._qq_client.singer
        self._qq_user = self._qq_client.user
        self._qq_songlist = self._qq_client.songlist
        self._qq_lyric = self._qq_client.lyric
        self._qq_recommend = self._qq_client.recommend
        # Keep qqmusic_api internal logs in sync with MA log level.
        logging.getLogger("qqmusicapi").setLevel(self.logger.level + 10)
        self._credential = credential
        self._api_semaphore = Semaphore(4)
        self._credential_refresh_lock = asyncio.Lock()
        self._last_credential_check_monotonic = 0.0
        self._musicid = int(self._credential.musicid)
        self._recommend_payload_cache = {}
        self.logger.info("QQ Music authenticated for uin %s", self._musicid)
        # Persist complete credential once on init so legacy configs gain refresh fields.
        self._persist_credential()

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get the available QQ Music recommendation rows, without items."""
        return [
            RecommendationFolder(
                item_id="guess_recommend",
                provider=self.instance_id,
                name="Recommended tracks",
                translation_key="recommended_tracks",
                icon="mdi-lightbulb-on-outline",
            ),
            RecommendationFolder(
                item_id="new_songs",
                provider=self.instance_id,
                name="Recommended new tracks",
                translation_key="recommended_new_tracks",
                icon="mdi-music-note-plus",
            ),
            RecommendationFolder(
                item_id="recommended_playlists",
                provider=self.instance_id,
                name="Recommended playlists",
                translation_key="recommended_playlists",
                icon="mdi-playlist-music",
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single QQ Music recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        items: UniqueList[MediaItemType | ItemMapping | BrowseFolder] = UniqueList()
        if item_id == "guess_recommend":
            guess_response = await self._get_recommend_payload_cached(
                "guess_recommend",
                _RECOMMEND_GUESS_TTL,
                lambda: self._qq_recommend.get_guess_recommend(credential=self._credential),
            )
            for item in extract_guess_recommend_tracks(self._to_dict(guess_response)):
                with suppress(InvalidDataError, TypeError, ValueError):
                    items.append(self._parse_track(item))
            if not items:
                # Fall back to radar recommendations when the personalised
                # guess endpoint yields no usable tracks.
                radar_response = await self._get_recommend_payload_cached(
                    "guess_recommend_radar",
                    _RECOMMEND_GUESS_TTL,
                    self._qq_recommend.get_radar_recommend,
                )
                for item in extract_radar_recommend_tracks(self._to_dict(radar_response)):
                    with suppress(InvalidDataError, TypeError, ValueError):
                        items.append(self._parse_track(item))
        elif item_id == "new_songs":
            new_song_response = await self._get_recommend_payload_cached(
                "new_songs",
                _RECOMMEND_NEWSONG_TTL,
                self._qq_recommend.get_recommend_newsong,
            )
            for item in extract_newsong_tracks(self._to_dict(new_song_response)):
                with suppress(InvalidDataError, TypeError, ValueError):
                    items.append(self._parse_track(item))
        elif item_id == "recommended_playlists":
            playlist_response = await self._get_recommend_payload_cached(
                "recommended_playlists",
                _RECOMMEND_PLAYLIST_TTL,
                self._qq_recommend.get_recommend_songlist,
            )
            for item in extract_recommend_songlists(self._to_dict(playlist_response)):
                with suppress(InvalidDataError, TypeError, ValueError):
                    items.append(self._parse_playlist(item))
        return items

    def _persist_credential(self) -> None:
        """Persist the current credential into this provider's setup data."""
        if not self._credential:
            return
        self._update_setup_data(CONF_UIN, str(self._credential.musicid))
        self._update_setup_data(CONF_MUSICID, str(self._credential.musicid))
        self._update_setup_data(CONF_MUSICKEY, str(self._credential.musickey))
        self._update_setup_data(CONF_LOGIN_TYPE, str(self._credential.login_type or 2))
        self._update_setup_data(
            CONF_CREDENTIAL_JSON, self._credential.model_dump_json(by_alias=True)
        )

    async def _ensure_valid_credential(self) -> None:
        """Refresh credential when expired and persistence data allows refresh."""
        if not self._credential:
            raise LoginFailed("QQ Music credential is not initialized")
        now = time.monotonic()
        # Avoid checking expiry on every single API call.
        if (now - self._last_credential_check_monotonic) < 300:
            return
        async with self._credential_refresh_lock:
            now = time.monotonic()
            if (now - self._last_credential_check_monotonic) < 300:
                return
            self._last_credential_check_monotonic = now
            if not self._qq_client:
                raise LoginFailed("QQ Music client is not initialized")
            if not await self._qq_client.login.check_expired(self._credential):
                return
            try:
                self._credential = await self._qq_client.login.refresh_credential(self._credential)
            except CredentialRefreshError as err:
                raise LoginFailed(
                    "QQ Music credential refresh failed, please re-authenticate"
                ) from err
            self._qq_client.credential = self._credential
            self._persist_credential()
            self.logger.info("QQ Music credential refreshed and persisted")

    async def _run_with_session(self, coro: Awaitable[Any]) -> Any:
        """Run qqmusic_api call with the provider-bound Client."""
        try:
            await self._ensure_valid_credential()
            async with self._api_semaphore:
                return await coro
        except Exception as err:
            raise self._translate_qq_exception(err) from err

    def _translate_qq_exception(self, err: Exception) -> Exception:
        """Translate qqmusic_api/http exceptions to MA domain exceptions."""
        if isinstance(err, CredentialExpiredError):
            return LoginFailed("QQ Music credential expired, please re-authenticate")
        if isinstance(err, LoginError):
            return LoginFailed(f"QQ Music login failed: {err}")
        if isinstance(err, CgiApiException):
            code = getattr(err, "code", None)
            if code in (1000, 2000):
                return LoginFailed(f"QQ Music API auth/sign failure (code={code})")
            if code == 404:
                return MediaNotFoundError("QQ Music item not found (code=404)")
            if code == 10007:
                return MediaNotFoundError(
                    "QQ Music item not found or invalid provider id (code=10007)"
                )
            return ResourceTemporarilyUnavailable(
                f"QQ Music API error (code={code})",
                backoff_time=30,
            )
        err_str = str(err).lower()
        if "timeout" in err_str or "temporarily" in err_str or "connection" in err_str:
            return ResourceTemporarilyUnavailable(
                "QQ Music network temporarily unavailable",
                backoff_time=20,
            )
        return err

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of provider."""
        if self._qq_client:
            await self._qq_client.close()
        self._qq_client = None
        self._recommend_payload_cache = {}
        await super().unload(is_removed)

    async def _get_recommend_payload_cached(
        self, key: str, ttl: int, fetcher: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Return recommendation payload from in-memory TTL cache or fetch fresh."""
        if cached := self._recommend_payload_cache.get(key):
            timestamp, payload = cached
            if (time.time() - timestamp) < ttl:
                self.logger.debug("QQ recommendations %s payload cache hit", key)
                return payload
        payload = await self._run_with_session(fetcher())
        self._recommend_payload_cache[key] = (time.time(), payload)
        return payload

    def _to_dict(self, data: Any) -> dict[str, Any]:
        """Normalize qqmusic-api response models to dictionaries."""
        if isinstance(data, dict):
            return data
        if callable(dump := getattr(data, "model_dump", None)):
            dumped = dump(by_alias=True)
            return dumped if isinstance(dumped, dict) else {}
        return {}

    def _decode_lyric_response(self, response: Any) -> dict[str, Any]:
        """Normalize and decrypt QQ Music lyric responses."""
        if callable(decrypt := getattr(response, "decrypt", None)):
            response = decrypt()
        lyric_obj = dict(self._to_dict(response))
        if str(lyric_obj.get("crypt") or "0") != "1":
            return lyric_obj
        for key in ("lyric", "trans", "roma"):
            value = str(lyric_obj.get(key) or "").strip()
            if value and _HEX_LYRIC_PATTERN.fullmatch(value):
                try:
                    lyric_obj[key] = qrc_decrypt(value)
                except (TypeError, ValueError) as err:
                    self.logger.debug("Failed to decrypt QQ Music %s lyric payload: %s", key, err)
        return lyric_obj

    def _response_items(self, data: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        """Extract dict items from list, dict, or qqmusic-api response model."""
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return extract_items(self._to_dict(data), keys)

    def _get_candidate_file_types(self) -> list[Any]:
        """Return ordered quality candidates based on provider config."""
        quality = str(self.config.get_value(CONF_QUALITY) or QUALITY_MP3_320)
        if quality == QUALITY_HI_RES:
            return [
                SongFileType.MASTER,
                SongFileType.FLAC,
                SongFileType.MP3_320,
                SongFileType.MP3_128,
            ]
        if quality == QUALITY_FLAC:
            return [
                SongFileType.FLAC,
                SongFileType.MP3_320,
                SongFileType.MP3_128,
            ]
        if quality == QUALITY_MP3_320:
            return [
                SongFileType.MP3_320,
                SongFileType.MP3_128,
            ]
        return [SongFileType.MP3_128]

    async def _resolve_stream_url(
        self, item_id: str, track_obj: dict[str, Any]
    ) -> tuple[str, Any | None, bool, int | None]:
        """Resolve stream URL with full-stream and preview fallback."""
        stream_url = ""
        selected_file_type = None
        is_preview_stream = False
        preview_duration = None
        file_obj = track_obj.get("file")
        if not isinstance(file_obj, dict):
            file_obj = {}
        media_mid = str(file_obj.get("media_mid") or file_obj.get("mediaMid") or "")
        song_mid = str(
            track_obj.get("mid") or track_obj.get("songMid") or track_obj.get("songmid") or item_id
        )
        song_type = self._to_positive_int(track_obj.get("type") or track_obj.get("songtype"))
        file_info = [SongFileInfo(song_mid, song_type=song_type, media_mid=media_mid or None)]

        for file_type in self._get_candidate_file_types():
            url_response = await self._run_with_session(
                self._qq_song.get_song_urls(
                    file_info,
                    file_type=file_type,
                    credential=self._credential,
                )
            )
            url = self._extract_stream_url(url_response, song_mid)
            if url.startswith("http"):
                return (url, file_type, False, None)

        # Fallback to 30s preview URL when full stream URL is unavailable.
        vs_list = track_obj.get("vs")
        if isinstance(vs_list, list):
            first_vs = next((vs for vs in vs_list if isinstance(vs, str) and vs), None)
            if first_vs:
                try_file_info = [
                    SongFileInfo(
                        song_mid,
                        song_type=song_type,
                        media_mid=first_vs,
                    )
                ]
                try_response = await self._run_with_session(
                    self._qq_song.get_song_urls(
                        try_file_info,
                        file_type=SpecialSongFileType.TRY,
                        credential=self._credential,
                    )
                )
                if try_url := self._extract_stream_url(try_response, song_mid):
                    stream_url = try_url
                    selected_file_type = SpecialSongFileType.TRY
                    is_preview_stream = True
                    try_begin = track_obj.get("file", {}).get("try_begin")
                    try_end = track_obj.get("file", {}).get("try_end")
                    if (
                        isinstance(try_begin, int)
                        and isinstance(try_end, int)
                        and try_end > try_begin
                    ):
                        preview_duration = int((try_end - try_begin) / 1000)
                    self.logger.info(
                        "QQ Music full stream unavailable for %s, using preview stream fallback",
                        item_id,
                    )

        return (stream_url, selected_file_type, is_preview_stream, preview_duration)

    def _extract_stream_url(self, url_response: Any, item_id: str) -> str:
        """Extract absolute stream URL from qqmusic-api 0.6 or legacy URL payload."""
        if isinstance(url_response, dict) and isinstance(url_response.get(item_id), str):
            return str(url_response[item_id])
        response = self._to_dict(url_response)
        url_items = response.get("midurlinfo") or response.get("data") or []
        if not isinstance(url_items, list):
            return ""
        cdn_base = getattr(self._qq_song, "_SONG_URL_FALLBACK_DOMAIN", None)
        if not cdn_base:
            self.logger.debug("QQ Music API did not expose stream URL fallback domain")
            cdn_base = "https://isure.stream.qqmusic.qq.com/"
        cdn_base = str(cdn_base)
        for item in url_items:
            if not isinstance(item, dict):
                continue
            if str(item.get("songmid") or item.get("mid") or "") != item_id:
                continue
            purl = str(item.get("purl") or "")
            if purl.startswith("http"):
                return purl
            if purl:
                return f"{cdn_base.rstrip('/')}/{purl.lstrip('/')}"
        return ""

    def _get_stream_expiration(self, stream_url: str) -> int:
        """Derive expiration from stream URL query string."""
        expiration = 3600
        if parsed_qs := parse_qs(urlparse(stream_url).query):
            for param in ("Expires", "expire"):
                if expire_ts := parsed_qs.get(param, [None])[0]:
                    expiration = max(30, int(expire_ts) - int(time.time()) - 10)
                    break
        return expiration

    def _get_content_type(self, selected_file_type: Any | None) -> ContentType:
        """Map qqmusic file type enum to MA content type."""
        if not selected_file_type:
            return ContentType.UNKNOWN
        if selected_file_type in (SongFileType.FLAC, SongFileType.MASTER):
            return ContentType.FLAC
        if selected_file_type in (
            SongFileType.ACC_48,
            SongFileType.ACC_96,
            SongFileType.ACC_192,
        ):
            return ContentType.M4A
        return ContentType.MPEG

    @staticmethod
    def _to_positive_int(value: Any) -> int:
        """Convert value to positive int, fallback to 0."""
        with suppress(TypeError, ValueError):
            parsed = int(value)
            if parsed > 0:
                return parsed
        return 0

    def _file_size(self, file_obj: dict[str, Any], *keys: str) -> int:
        """Read first positive file size from multiple key variants."""
        for key in keys:
            if size := self._to_positive_int(file_obj.get(key)):
                return size
        return 0

    def _get_max_supported_audio_format(
        self, track_obj: dict[str, Any]
    ) -> tuple[AudioFormat, str | None]:
        """Infer max supported audio quality from QQ track file metadata."""
        file_obj = track_obj.get("file")
        if not isinstance(file_obj, dict):
            return (AudioFormat(content_type=ContentType.UNKNOWN), None)

        size_new = file_obj.get("size_new")
        size_new_list = size_new if isinstance(size_new, list) else []

        def _size_new_at(index: int) -> int:
            if index >= len(size_new_list):
                return 0
            return self._to_positive_int(size_new_list[index])

        # QQMusicApi docs: size_new[0] is "master" (24bit/192kHz).
        if _size_new_at(0):
            return (
                AudioFormat(content_type=ContentType.FLAC, sample_rate=192000, bit_depth=24),
                "Hi-Res",
            )
        if self._file_size(file_obj, "size_flac", "sizeFlac") or _size_new_at(5):
            return (
                AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16),
                None,
            )
        if self._file_size(file_obj, "size_320mp3", "size320mp3") or _size_new_at(3):
            return (
                AudioFormat(content_type=ContentType.MPEG, bit_rate=320000),
                None,
            )
        if self._file_size(file_obj, "size_192ogg", "size192ogg"):
            return (
                AudioFormat(content_type=ContentType.OGG, bit_rate=192000),
                None,
            )
        if self._file_size(file_obj, "size_192aac", "size192aac"):
            return (
                AudioFormat(content_type=ContentType.M4A, bit_rate=192000),
                None,
            )
        if self._file_size(file_obj, "size_128mp3", "size128mp3"):
            return (
                AudioFormat(content_type=ContentType.MPEG, bit_rate=128000),
                None,
            )
        if self._file_size(file_obj, "size_96ogg", "size96ogg"):
            return (
                AudioFormat(content_type=ContentType.OGG, bit_rate=96000),
                None,
            )
        if self._file_size(file_obj, "size_96aac", "size96aac"):
            return (
                AudioFormat(content_type=ContentType.M4A, bit_rate=96000),
                None,
            )
        if self._file_size(file_obj, "size_48aac", "size48aac"):
            return (
                AudioFormat(content_type=ContentType.M4A, bit_rate=48000),
                None,
            )
        if self._file_size(file_obj, "size_try", "sizeTry"):
            return (
                AudioFormat(content_type=ContentType.MPEG),
                None,
            )
        return (AudioFormat(content_type=ContentType.UNKNOWN), None)

    def _get_stream_audio_format(self, selected_file_type: Any | None) -> AudioFormat:
        """Build stream audio format for currently selected file type."""
        if not selected_file_type:
            return AudioFormat(content_type=ContentType.UNKNOWN)
        if selected_file_type == SongFileType.FLAC:
            return AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16)
        if selected_file_type == SongFileType.MASTER:
            return AudioFormat(content_type=ContentType.FLAC, sample_rate=192000, bit_depth=24)
        if selected_file_type == SongFileType.MP3_320:
            return AudioFormat(content_type=ContentType.MPEG, bit_rate=320000)
        if selected_file_type in (SongFileType.MP3_128, SpecialSongFileType.TRY):
            return AudioFormat(content_type=ContentType.MPEG, bit_rate=128000)
        if selected_file_type == SongFileType.ACC_192:
            return AudioFormat(content_type=ContentType.M4A, bit_rate=192000)
        if selected_file_type == SongFileType.ACC_96:
            return AudioFormat(content_type=ContentType.M4A, bit_rate=96000)
        if selected_file_type == SongFileType.ACC_48:
            return AudioFormat(content_type=ContentType.M4A, bit_rate=48000)
        return AudioFormat(content_type=self._get_content_type(selected_file_type))

    def _get_artist_mapping(self, artist_obj: dict[str, Any] | str) -> ItemMapping | None:
        return get_artist_mapping(artist_obj, self.instance_id)

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        return parse_artist(artist_obj, self.domain, self.instance_id)

    def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        return parse_album(album_obj, self.domain, self.instance_id)

    def _parse_track(self, track_obj: dict[str, Any]) -> Track:
        return parse_track(
            track_obj=track_obj,
            provider_domain=self.domain,
            provider_instance_id=self.instance_id,
            get_max_supported_audio_format=self._get_max_supported_audio_format,
        )

    async def _resolve_song_id(self, prov_track_id: str) -> int:
        """Resolve provider track id (mid/id) to numeric song id."""
        song_id, _song_type = await self._resolve_song_info(prov_track_id)
        return song_id

    async def _resolve_song_info(self, prov_track_id: str) -> tuple[int, int]:
        """Resolve provider track id to numeric song id and QQ song type."""
        if prov_track_id.isdigit():
            return (int(prov_track_id), 0)
        response = await self._run_with_session(self._qq_song.get_detail(prov_track_id))
        response_obj = self._to_dict(response)
        track_obj = response_obj.get("track_info") or response_obj.get("track") or {}
        if song_id := extract_song_id(track_obj):
            song_type = self._to_positive_int(track_obj.get("type") or track_obj.get("songtype"))
            return (song_id, song_type)
        raise MediaNotFoundError(f"Unable to resolve numeric song info for track {prov_track_id}")

    # Compatibility wrappers for existing tests/extensions.
    def _extract_song_id(self, track_obj: dict[str, Any]) -> int | None:
        """Backward-compatible wrapper for song id extraction helper."""
        return extract_song_id(track_obj)

    def _extract_items(
        self, data: dict[str, Any], candidate_keys: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Backward-compatible wrapper for list extraction helper."""
        return extract_items(data, candidate_keys)

    async def _ensure_user_euin(self) -> str:
        """Resolve and cache current user's encrypted uin."""
        if self._euin:
            return self._euin
        euin = str(getattr(self._credential, "encrypt_uin", "") or "")
        if not euin:
            raise LoginFailed("Failed to resolve QQ Music user profile (euin)")
        self._euin = euin
        return self._euin

    def _build_playlist_id(self, dissid: int | str, dirid: int | str) -> str:
        return build_playlist_id(dissid, dirid)

    def _parse_playlist_id(self, prov_playlist_id: str) -> tuple[int, int]:
        return parse_playlist_id(prov_playlist_id)

    def _parse_playlist(self, playlist_obj: dict[str, Any]) -> Playlist:
        return parse_playlist(playlist_obj, self.domain, self.instance_id)

    @use_cache(3600 * 3)
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on QQ Music."""
        result = SearchResults()
        if MediaType.TRACK in media_types:
            raw_tracks = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    SearchType.SONG,
                    num=limit,
                )
            )
            result.tracks = []
            for item in self._response_items(raw_tracks, ("song", "songlist", "list")):
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.tracks.append(self._parse_track(item))

        if MediaType.ALBUM in media_types:
            raw_albums = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    SearchType.ALBUM,
                    num=limit,
                )
            )
            result.albums = []
            for item in self._response_items(raw_albums, ("album", "album_list", "list")):
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.albums.append(self._parse_album(item))

        if MediaType.ARTIST in media_types:
            raw_artists = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    SearchType.SINGER,
                    num=limit,
                )
            )
            result.artists = []
            for item in self._response_items(raw_artists, ("singer", "singer_list", "list")):
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.artists.append(self._parse_artist(item))

        if MediaType.PLAYLIST in media_types:
            raw_playlists = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    SearchType.SONGLIST,
                    num=limit,
                )
            )
            result.playlists = []
            for item in self._response_items(raw_playlists, ("songlist", "playlists", "list")):
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.playlists.append(self._parse_playlist(item))
        return result

    @use_cache(3600 * 24 * 7)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.isdigit():
            raise MediaNotFoundError(
                f"Artist id {prov_artist_id} is not a QQ singer mid, cannot fetch artist details"
            )
        response = await self._run_with_session(self._qq_singer.get_info(prov_artist_id))
        artist_obj: dict[str, Any] | None = None
        response_obj = self._to_dict(response)
        info = response_obj.get("Info") or response_obj.get("info") or {}
        if isinstance(info, dict):
            singer_obj = info.get("Singer")
            base_info = info.get("BaseInfo")
            if isinstance(singer_obj, dict):
                artist_obj = dict(singer_obj)
            if isinstance(base_info, dict):
                if artist_obj is None:
                    artist_obj = dict(base_info)
                else:
                    if not extract_first_text(artist_obj, ("name", "Name", "singerName"), ""):
                        artist_obj["Name"] = base_info.get("Name") or base_info.get("name")
                    if not artist_obj.get("Avatar"):
                        artist_obj["Avatar"] = base_info.get("Avatar") or base_info.get("avatar")
            if artist_obj is None:
                artist_obj = info
        if artist_obj is None:
            singer_list = response_obj.get("singer_list")
            if isinstance(singer_list, list) and singer_list:
                singer_item = singer_list[0]
                if isinstance(singer_item, dict):
                    artist_obj = singer_item.get("basic_info") or singer_item
        if not artist_obj:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return self._parse_artist(artist_obj)

    @use_cache(3600 * 12, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get all albums for artist."""
        if prov_artist_id.isdigit():
            raise MediaNotFoundError(
                f"Artist id {prov_artist_id} is not a QQ singer mid, cannot fetch albums"
            )
        raw_albums: list[dict[str, Any]] = []
        try:
            tab_albums = await self._run_with_session(
                self._qq_singer.get_tab_detail(
                    prov_artist_id,
                    TabType.ALBUM,
                    page=1,
                    num=100,
                )
            )
            raw_albums = self._response_items(
                tab_albums,
                ("album_tab", "albumList", "album_list", "list"),
            )
        except MediaNotFoundError, InvalidDataError, TypeError, ValueError:
            raw_albums = []

        if not raw_albums:
            response = await self._run_with_session(
                self._qq_singer.get_album_list(
                    prov_artist_id,
                    num=100,
                    page=1,
                )
            )
            raw_albums = self._response_items(
                response,
                ("albumList", "album_list", "list"),
            )

        albums: list[Album] = []
        for item in raw_albums:
            with suppress(InvalidDataError, TypeError, ValueError):
                albums.append(self._parse_album(item))
        return albums

    async def _get_artist_song_list(self, prov_artist_id: str) -> list[Track]:
        """Get parsed tracks from QQ Music singer song list."""
        response = await self._run_with_session(
            self._qq_singer.get_songs_list(
                prov_artist_id,
                num=100,
                page=1,
            )
        )
        response_obj = self._to_dict(response)
        songs: list[dict[str, Any]] = []
        for item in response_obj.get("songList", []):
            if isinstance(item, dict) and isinstance(song_info := item.get("songInfo"), dict):
                songs.append(song_info)
        if not songs:
            songs = extract_items(response_obj, ("song_list", "songs", "list"))
        return [self._parse_track(item) for item in songs if item.get("mid")]

    @use_cache(3600 * 6, allow_expired_cache=True)
    async def get_artist_tracks(self, prov_artist_id: str) -> list[Track]:
        """Get tracks for artist."""
        if prov_artist_id.isdigit():
            raise MediaNotFoundError(
                f"Artist id {prov_artist_id} is not a QQ singer mid, cannot fetch tracks"
            )
        return await self._get_artist_song_list(prov_artist_id)

    @use_cache(3600 * 6, allow_expired_cache=True)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks for artist."""
        if prov_artist_id.isdigit():
            raise MediaNotFoundError(
                f"Artist id {prov_artist_id} is not a QQ singer mid, cannot fetch top tracks"
            )
        return await self._get_artist_song_list(prov_artist_id)

    @use_cache(3600 * 24 * 7)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        album_value: str | int = int(prov_album_id) if prov_album_id.isdigit() else prov_album_id
        response = await self._run_with_session(self._qq_album.get_detail(album_value))
        if not response:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        album_obj: dict[str, Any] | None = None
        response_obj = self._to_dict(response)
        basic_info = response_obj.get("basicInfo") or response_obj.get("album")
        if isinstance(basic_info, dict):
            album_obj = dict(basic_info)
            if "singer" not in album_obj:
                singer_list = response_obj.get("singer", {}).get("singerList")
                if not isinstance(singer_list, list):
                    singer_list = response_obj.get("singers")
                if isinstance(singer_list, list):
                    album_obj["singer"] = singer_list
        else:
            album_obj = response_obj
        if not isinstance(album_obj, dict):
            raise MediaNotFoundError(f"Album {prov_album_id} returned unexpected payload")
        return self._parse_album(album_obj)

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for album id."""
        album_value: str | int = int(prov_album_id) if prov_album_id.isdigit() else prov_album_id
        response = await self._run_with_session(
            self._qq_album.get_song(album_value, num=300, page=1)
        )
        response_obj = self._to_dict(response)
        songs: list[dict[str, Any]] = []
        for item in response_obj.get("songList", []):
            if isinstance(item, dict) and isinstance(song_info := item.get("songInfo"), dict):
                songs.append(song_info)
        if not songs:
            songs = self._response_items(response, ("song_list", "songs", "list"))
        return [self._parse_track(item) for item in songs if item.get("mid")]

    @use_cache(3600 * 24 * 7, cache_checksum="qqmusic_lyrics_v2")
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track_value: str | int = int(prov_track_id) if prov_track_id.isdigit() else prov_track_id
        response = await self._run_with_session(self._qq_song.get_detail(track_value))
        response_obj = self._to_dict(response)
        track_obj = response_obj.get("track_info") or response_obj.get("track")
        if not track_obj:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        track = self._parse_track(track_obj)
        try:
            # Prefer normal lyric first: this is typically LRC and works best for MA synced scroll.
            lyric_response = await self._run_with_session(
                self._qq_lyric.get_lyric(prov_track_id, qrc=False, trans=True)
            )
            lyric_text = ""
            trans_text = ""
            lyric_obj = self._decode_lyric_response(lyric_response)
            lyric_text = str(lyric_obj.get("lyric") or "").strip()
            trans_text = str(lyric_obj.get("trans") or "").strip()
            # Fallback to QRC when standard lyric is empty/unavailable.
            if not lyric_text:
                lyric_response = await self._run_with_session(
                    self._qq_lyric.get_lyric(prov_track_id, qrc=True, trans=True)
                )
                lyric_obj = self._decode_lyric_response(lyric_response)
            lyric_text = str(lyric_obj.get("lyric") or lyric_text).strip()
            trans_text = str(lyric_obj.get("trans") or trans_text).strip()
            if lyric_text:
                if _LRC_TIMESTAMP_PATTERN.search(lyric_text):
                    track.metadata.lrc_lyrics = normalize_qq_lyric_text(lyric_text)
                else:
                    # QRC (e.g. [36438,1880]当(36438,161)...) -> LRC for synced display.
                    qrc_lrc = qrc_to_lrc(lyric_text)
                    if qrc_lrc:
                        track.metadata.lrc_lyrics = qrc_lrc
                track.metadata.lyrics = normalize_qq_lyric_text(lyric_text)
            if trans_text:
                trans_text = normalize_qq_lyric_text(trans_text)
                if track.metadata.lyrics:
                    track.metadata.lyrics = f"{track.metadata.lyrics}\n\n{trans_text}".strip()
                else:
                    track.metadata.lyrics = trans_text
        except Exception as err:
            self.logger.debug("Failed to load QQ Music lyrics for %s: %s", prov_track_id, err)
        return track

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve followed artists from QQ Music."""
        euin = await self._ensure_user_euin()
        page = 1
        num = 100
        total_yielded = 0
        while True:
            response = await self._run_with_session(
                self._qq_user.get_follow_singers(
                    euin,
                    page=page,
                    num=num,
                    credential=self._credential,
                )
            )
            artists = self._response_items(
                response,
                ("List", "Users", "list", "v_list", "users", "singer_list", "singers"),
            )
            if not artists:
                break
            for artist_obj in artists:
                try:
                    yield self._parse_artist(artist_obj)
                    total_yielded += 1
                except InvalidDataError, TypeError, ValueError:
                    continue
            if len(artists) < num:
                break
            page += 1
        self.logger.info("QQ library artists sync yielded %s artist(s)", total_yielded)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from QQ Music."""
        euin = await self._ensure_user_euin()
        page = 1
        num = 100
        yielded = 0
        total = None
        while True:
            response = await self._run_with_session(
                self._qq_user.get_fav_song(euin, page=page, num=num, credential=self._credential)
            )
            response_obj = self._to_dict(response)
            songs = self._response_items(response, ("songlist", "song_list", "songs", "list"))
            if total is None:
                total = int(response_obj.get("total_song_num") or response_obj.get("total") or 0)
            if not songs:
                break
            for song in songs:
                try:
                    yield self._parse_track(song)
                    yielded += 1
                except InvalidDataError, TypeError, ValueError:
                    continue
            if total and yielded >= total:
                break
            page += 1

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from QQ Music."""
        euin = await self._ensure_user_euin()
        page = 1
        num = 100
        total_yielded = 0
        while True:
            response = await self._run_with_session(
                self._qq_user.get_fav_album(euin, page=page, num=num, credential=self._credential)
            )
            albums = self._response_items(
                response,
                (
                    "albums",
                    "album_list",
                    "albumList",
                    "v_list",
                    "list",
                    "v_album",
                    "favAlbumList",
                ),
            )
            if not albums:
                break
            for album_obj in albums:
                try:
                    yield self._parse_album(album_obj)
                    total_yielded += 1
                except InvalidDataError, TypeError, ValueError:
                    continue
            if len(albums) < num:
                break
            page += 1
        self.logger.info("QQ library albums sync yielded %s album(s)", total_yielded)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve user playlists from QQ Music."""
        euin = await self._ensure_user_euin()
        created = await self._run_with_session(
            self._qq_user.get_created_songlist(self._musicid, credential=self._credential)
        )
        for playlist_obj in self._response_items(
            created,
            ("playlists", "v_playlist", "list", "playlist"),
        ):
            try:
                yield self._parse_playlist(playlist_obj)
            except InvalidDataError, TypeError, ValueError:
                continue

        page = 1
        num = 100
        while True:
            response = await self._run_with_session(
                self._qq_user.get_fav_songlist(
                    euin, page=page, num=num, credential=self._credential
                )
            )
            fav_playlists = self._response_items(
                response,
                ("playlists", "list", "v_list", "playlist", "vec_kept_playlist"),
            )
            if not fav_playlists:
                break
            for playlist_obj in fav_playlists:
                try:
                    yield self._parse_playlist(playlist_obj)
                except InvalidDataError, TypeError, ValueError:
                    continue
            if len(fav_playlists) < num:
                break
            page += 1

    @use_cache(3600 * 3)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        dissid, dirid = self._parse_playlist_id(prov_playlist_id)
        response = await self._run_with_session(
            self._qq_songlist.get_detail(
                songlist_id=dissid,
                dirid=dirid,
                num=1,
                page=1,
                onlysong=False,
            )
        )
        response_obj = self._to_dict(response)
        playlist_obj = response_obj.get("dirinfo") or response_obj.get("info")
        if not isinstance(playlist_obj, dict):
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        # Ensure parsed playlist keeps composite id including dirid.
        playlist_obj = {**playlist_obj, "dissid": dissid, "dirid": dirid}
        return self._parse_playlist(playlist_obj)

    @use_cache(3600, allow_expired_cache=True)
    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get playlist tracks for given playlist id."""
        dissid, dirid = self._parse_playlist_id(prov_playlist_id)
        response = await self._run_with_session(
            self._qq_songlist.get_detail(
                songlist_id=dissid,
                dirid=dirid,
                num=200,
                page=page + 1,
                onlysong=True,
            )
        )
        songs = self._response_items(response, ("songlist", "songs", "song_list", "list"))
        results: list[Track] = []
        for index, song in enumerate(songs, start=1 + page * 200):
            try:
                track = self._parse_track(song)
                track.position = index
                results.append(track)
            except InvalidDataError, TypeError, ValueError:
                continue
        return results

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        created = await self._run_with_session(
            self._qq_songlist.create(dirname=name, credential=self._credential)
        )
        created_obj = self._to_dict(created)
        if not created_obj:
            raise InvalidDataError("QQ Music create playlist returned invalid response")
        dirid_raw = created_obj.get("dirid") or created_obj.get("dirId") or created_obj.get("id")
        if dirid_raw is None:
            raise InvalidDataError("QQ Music create playlist response missing dirid")
        dirid = int(dirid_raw)
        dissid = int(created_obj.get("tid") or created_obj.get("dissid") or dirid)
        return await self.get_playlist(self._build_playlist_id(dissid, dirid))

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        dissid, dirid = self._parse_playlist_id(prov_playlist_id)
        target_dirid = dirid or dissid
        if target_dirid <= 0:
            raise InvalidDataError("QQ Music playlist id is invalid for playlist edit")
        song_info: list[tuple[int, int]] = []
        for track_id in prov_track_ids:
            try:
                song_info.append(await self._resolve_song_info(track_id))
            except (MediaNotFoundError, InvalidDataError, ResourceTemporarilyUnavailable) as err:
                self.logger.warning("Skipping track %s while adding to playlist: %s", track_id, err)
        if not song_info:
            raise InvalidDataError("No valid QQ Music tracks to add")
        await self._run_with_session(
            self._qq_songlist.add_songs(
                dirid=target_dirid,
                song_info=song_info,
                tid=dissid,
                credential=self._credential,
            )
        )

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        dissid, dirid = self._parse_playlist_id(prov_playlist_id)
        target_dirid = dirid or dissid
        if target_dirid <= 0:
            raise InvalidDataError("QQ Music playlist id is invalid for playlist edit")
        playlist_tracks = await self.get_playlist_tracks(prov_playlist_id, page=0)
        song_info: list[tuple[int, int]] = []
        target_positions = set(positions_to_remove)
        for track in playlist_tracks:
            if track.position not in target_positions:
                continue
            try:
                song_info.append(await self._resolve_song_info(track.item_id))
            except (MediaNotFoundError, InvalidDataError, ResourceTemporarilyUnavailable) as err:
                self.logger.warning(
                    "Skipping track %s while removing from playlist: %s", track.item_id, err
                )
        if not song_info:
            return
        await self._run_with_session(
            self._qq_songlist.del_songs(
                dirid=target_dirid,
                song_info=song_info,
                tid=dissid,
                credential=self._credential,
            )
        )

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Retrieve a dynamic list of similar artists based on the provided artist."""
        if prov_artist_id.isdigit():
            raise MediaNotFoundError(
                f"Artist id {prov_artist_id} is not a QQ singer mid, cannot fetch similar artists"
            )
        response = await self._run_with_session(
            self._qq_singer.get_similar(prov_artist_id, number=limit)
        )
        artists: list[Artist] = []
        for item in self._response_items(
            response, ("singerlist", "singer_list", "singers", "list")
        ):
            if len(artists) >= limit:
                break
            with suppress(InvalidDataError, TypeError, ValueError):
                artists.append(self._parse_artist(item))
        return artists

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        song_id = await self._resolve_song_id(prov_track_id)
        response = await self._run_with_session(self._qq_song.get_similar_song(song_id))
        response_obj = self._to_dict(response)
        response_items: Any = response if isinstance(response, list) else []
        if not response_items:
            response_items = response_obj.get("song") or response_obj.get("songlist") or []
        if not isinstance(response_items, list):
            return []
        tracks: list[Track] = []
        for item in response_items:
            if len(tracks) >= limit:
                break
            if not isinstance(item, dict):
                continue
            grouped_songs = item.get("song")
            candidates = grouped_songs if isinstance(grouped_songs, list) else [item]
            for candidate in candidates:
                if len(tracks) >= limit:
                    break
                if not isinstance(candidate, dict):
                    continue
                with suppress(InvalidDataError, TypeError, ValueError):
                    tracks.append(self._parse_track(candidate))
        return tracks

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return streamdetails for given track id."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Unsupported media type {media_type}")
        track_response = await self._run_with_session(self._qq_song.get_detail(item_id))
        track_response_obj = self._to_dict(track_response)
        track_obj = track_response_obj.get("track_info") or track_response_obj.get("track") or {}
        if not track_obj:
            raise MediaNotFoundError(f"Track {item_id} not found")
        (
            stream_url,
            selected_file_type,
            is_preview_stream,
            preview_duration,
        ) = await self._resolve_stream_url(item_id, track_obj)

        if not stream_url:
            pay_info = track_obj.get("pay", {})
            pay_play = pay_info.get("pay_play", "unknown")
            pay_status = pay_info.get("pay_status", "unknown")
            raise UnplayableMediaError(
                f"No playable stream URL returned for track {item_id} "
                f"(pay_play={pay_play}, pay_status={pay_status})"
            )

        expiration = self._get_stream_expiration(stream_url)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._get_stream_audio_format(selected_file_type),
            stream_type=StreamType.HTTP,
            path=stream_url,
            duration=preview_duration if is_preview_stream else None,
            data={"preview": is_preview_stream},
            can_seek=True,
            allow_seek=True,
            expiration=expiration,
        )
