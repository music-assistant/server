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
    MusicAssistantError,
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
from pydantic import ValidationError
from qqmusic_api import (
    ApiDataError,
    BaseApiException,
    CgiApiException,
    Credential,
    CredentialExpiredError,
    CredentialRefreshError,
    LoginError,
    NetworkError,
)
from qqmusic_api import Client as QQClient
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
from .helpers import (
    normalize_qq_lyric_text,
    qrc_to_lrc,
)
from .parsers import (
    build_playlist_id,
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
    from qqmusic_api.models.base import Song as QQMusicSong
    from qqmusic_api.models.song import GetCdnDispatchResponse, GetSongUrlsResponse

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
_RECOMMEND_GUESS_TTL = 60 * 60
_RECOMMEND_NEWSONG_TTL = 60 * 60 * 6
_RECOMMEND_PLAYLIST_TTL = 60 * 60 * 6
_CDN_DISPATCH_CACHE_KEY = "cdn_dispatch_sips_v1"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return QQMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


def _store_credential(values: dict[str, ConfigValueType], credential: Credential) -> None:
    if not credential.musicid or not credential.musickey:
        raise LoginFailed("QR login succeeded but credential is incomplete")
    values[CONF_CREDENTIAL_JSON] = credential.model_dump_json(by_alias=True)


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
    _cdn_dispatch_lock: asyncio.Lock

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the configuration (options) entries for the QQ Music provider.

        Authentication runs in the interactive setup flow (see ``setup_flow.py``); the only
        genuine option configured here is the preferred streaming quality.
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

    async def handle_async_init(self) -> None:
        """Validate auth and initialize qqmusic api adapters."""
        credential: Credential | None = None
        if credential_json := str(self.get_setup_value(CONF_CREDENTIAL_JSON) or "").strip():
            try:
                credential = Credential.model_validate_json(credential_json)
            except ValidationError as err:
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
                    "str_musicid": str(config_musicid).strip(),
                    "loginType": login_type,
                }
            )
        if not credential.str_musicid and credential.musicid:
            credential = credential.model_copy(update={"str_musicid": str(credential.musicid)})

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
        self._cdn_dispatch_lock = asyncio.Lock()
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
                self._qq_recommend.get_guess_recommend,
            )
            for song in guess_response.songs:
                with suppress(InvalidDataError, TypeError, ValueError):
                    items.append(self._parse_track(song.model_dump()))
            if not items:
                # Fall back to radar recommendations when the personalised
                # guess endpoint yields no usable tracks.
                radar_response = await self._get_recommend_payload_cached(
                    "guess_recommend_radar",
                    _RECOMMEND_GUESS_TTL,
                    self._qq_recommend.get_radar_recommend,
                )
                for song in radar_response.songs:
                    with suppress(InvalidDataError, TypeError, ValueError):
                        items.append(self._parse_track(song.model_dump()))
        elif item_id == "new_songs":
            new_song_response = await self._get_recommend_payload_cached(
                "new_songs",
                _RECOMMEND_NEWSONG_TTL,
                self._qq_recommend.get_recommend_newsong,
            )
            for song in new_song_response.songs:
                with suppress(InvalidDataError, TypeError, ValueError):
                    items.append(self._parse_track(song.model_dump()))
        elif item_id == "recommended_playlists":
            playlist_response = await self._get_recommend_payload_cached(
                "recommended_playlists",
                _RECOMMEND_PLAYLIST_TTL,
                self._qq_recommend.get_recommend_songlist,
            )
            for playlist in playlist_response.songlists:
                with suppress(InvalidDataError, TypeError, ValueError):
                    items.append(self._parse_playlist(playlist.model_dump()))
        return items

    def _persist_credential(self) -> None:
        """Persist the current credential into this provider's setup data."""
        if not self._credential:
            return
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
        except BaseApiException as err:
            raise self._translate_qq_exception(err) from err

    def _translate_qq_exception(self, err: BaseApiException) -> MusicAssistantError:
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
        if isinstance(err, NetworkError):
            return ResourceTemporarilyUnavailable(
                "QQ Music network temporarily unavailable",
                backoff_time=20,
            )
        if isinstance(err, ApiDataError):
            return ResourceTemporarilyUnavailable(
                "QQ Music API returned invalid data",
                backoff_time=30,
            )
        return ResourceTemporarilyUnavailable(
            "QQ Music API temporarily unavailable",
            backoff_time=30,
        )

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
        self, item_id: str, track: QQMusicSong
    ) -> tuple[str, Any | None, bool, int | None, int | None]:
        """Resolve stream URL with full-stream and preview fallback."""
        stream_url = ""
        selected_file_type = None
        is_preview_stream = False
        preview_duration = None
        stream_expiration = None
        song_mid = track.mid
        file_info = [
            SongFileInfo(
                song_mid,
                song_type=track.type,
                media_mid=track.file.media_mid or None,
            )
        ]

        for file_type in self._get_candidate_file_types():
            url_response = await self._run_with_session(
                self._qq_song.get_song_urls(
                    file_info,
                    file_type=file_type,
                )
            )
            url, stream_expiration = await self._extract_stream_url(url_response, song_mid)
            if url.startswith("http"):
                return (url, file_type, False, None, stream_expiration)

        # Use QQ Music's officially provided preview stream when full playback is unavailable.
        if first_vs := next((vs for vs in track.vs if vs), None):
            try_file_info = [
                SongFileInfo(
                    song_mid,
                    song_type=track.type,
                    media_mid=first_vs,
                )
            ]
            try_response = await self._run_with_session(
                self._qq_song.get_song_urls(
                    try_file_info,
                    file_type=SpecialSongFileType.TRY,
                )
            )
            try_url, stream_expiration = await self._extract_stream_url(try_response, song_mid)
            if try_url:
                stream_url = try_url
                selected_file_type = SpecialSongFileType.TRY
                is_preview_stream = True
                if track.file.try_end > track.file.try_begin:
                    preview_duration = int((track.file.try_end - track.file.try_begin) / 1000)
                self.logger.info(
                    "QQ Music full stream unavailable for %s, using preview stream fallback",
                    item_id,
                )

        return (
            stream_url,
            selected_file_type,
            is_preview_stream,
            preview_duration,
            stream_expiration,
        )

    async def _extract_stream_url(
        self, url_response: GetSongUrlsResponse, item_id: str
    ) -> tuple[str, int | None]:
        """Build a playable URL from the typed URL response and CDN dispatch data."""
        info = next((item for item in url_response.data if item.mid == item_id), None)
        expiration = url_response.expiration if url_response.expiration > 0 else None
        if info is None or info.result != 0 or not info.purl:
            return ("", expiration)
        if info.purl.startswith(("http://", "https://")):
            return (info.purl, expiration)
        cdn_base = await self._get_cdn_base()
        return (f"{cdn_base.rstrip('/')}/{info.purl.lstrip('/')}", expiration)

    async def _get_cdn_base(self) -> str:
        """Return a cached CDN root from QQ Music's public dispatch endpoint."""
        if cdn_base := await self._get_cached_cdn_base():
            return cdn_base
        async with self._cdn_dispatch_lock:
            if cdn_base := await self._get_cached_cdn_base():
                return cdn_base
            dispatch: GetCdnDispatchResponse = await self._run_with_session(
                self._qq_song.get_cdn_dispatch()
            )
            if dispatch.retcode != 0:
                raise ResourceTemporarilyUnavailable(
                    f"QQ Music CDN dispatch failed (code={dispatch.retcode})", backoff_time=30
                )
            sips: list[str] = [
                sip.rstrip("/") for sip in dispatch.sip if sip.startswith(("http://", "https://"))
            ]
            if not sips:
                raise ResourceTemporarilyUnavailable(
                    "QQ Music CDN dispatch returned no playable CDN", backoff_time=30
                )
            ttl = min(
                (
                    value
                    for value in (dispatch.refresh_time, dispatch.expiration, dispatch.cache_time)
                    if value > 0
                ),
                default=300,
            )
            await self.mass.cache.set(
                _CDN_DISPATCH_CACHE_KEY,
                sips,
                expiration=ttl,
                provider=self.instance_id,
            )
            return sips[0]

    async def _get_cached_cdn_base(self) -> str | None:
        """Return a valid cached CDN root, honoring a forced cache refresh."""
        cached_sips = await self.mass.cache.get(
            _CDN_DISPATCH_CACHE_KEY,
            provider=self.instance_id,
            allow_bypass=True,
        )
        if not isinstance(cached_sips, list):
            return None
        return next(
            (
                sip.rstrip("/")
                for sip in cached_sips
                if isinstance(sip, str) and sip.startswith(("http://", "https://"))
            ),
            None,
        )

    @staticmethod
    def _to_positive_int(value: Any) -> int:
        """Convert value to positive int, fallback to 0."""
        with suppress(TypeError, ValueError):
            parsed = int(value)
            if parsed > 0:
                return parsed
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
        if self._to_positive_int(file_obj.get("size_flac")) or _size_new_at(5):
            return (
                AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16),
                None,
            )
        if self._to_positive_int(file_obj.get("size_320mp3")) or _size_new_at(3):
            return (
                AudioFormat(content_type=ContentType.MPEG, bit_rate=320000),
                None,
            )
        if self._to_positive_int(file_obj.get("size_192ogg")):
            return (
                AudioFormat(content_type=ContentType.OGG, bit_rate=192000),
                None,
            )
        if self._to_positive_int(file_obj.get("size_192aac")):
            return (
                AudioFormat(content_type=ContentType.M4A, bit_rate=192000),
                None,
            )
        if self._to_positive_int(file_obj.get("size_128mp3")):
            return (
                AudioFormat(content_type=ContentType.MPEG, bit_rate=128000),
                None,
            )
        if self._to_positive_int(file_obj.get("size_96ogg")):
            return (
                AudioFormat(content_type=ContentType.OGG, bit_rate=96000),
                None,
            )
        if self._to_positive_int(file_obj.get("size_96aac")):
            return (
                AudioFormat(content_type=ContentType.M4A, bit_rate=96000),
                None,
            )
        if self._to_positive_int(file_obj.get("size_48aac")):
            return (
                AudioFormat(content_type=ContentType.M4A, bit_rate=48000),
                None,
            )
        if self._to_positive_int(file_obj.get("size_try")):
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
        return AudioFormat(content_type=ContentType.UNKNOWN)

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
        if response.track.id > 0:
            return (response.track.id, response.track.type)
        raise MediaNotFoundError(f"Unable to resolve numeric song info for track {prov_track_id}")

    async def _ensure_user_euin(self) -> str:
        """Resolve and cache current user's encrypted uin."""
        if self._euin:
            return self._euin
        euin = self._credential.encrypt_uin
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

    def _skipped_playlist_id(self, playlist_obj: dict[str, Any]) -> str | None:
        """Return the provider playlist id of a playlist that could not be parsed."""
        dissid = playlist_obj.get("id") or 0
        dirid = playlist_obj.get("dirid") or 0
        return build_playlist_id(dissid, dirid) if dissid else None

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
            response = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    SearchType.SONG,
                    num=limit,
                )
            )
            result.tracks = []
            for track in response.song:
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.tracks.append(self._parse_track(track.model_dump()))

        if MediaType.ALBUM in media_types:
            response = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    SearchType.ALBUM,
                    num=limit,
                )
            )
            result.albums = []
            for album in response.album:
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.albums.append(self._parse_album(album.model_dump()))

        if MediaType.ARTIST in media_types:
            response = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    SearchType.SINGER,
                    num=limit,
                )
            )
            result.artists = []
            for artist in response.singer:
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.artists.append(self._parse_artist(artist.model_dump()))

        if MediaType.PLAYLIST in media_types:
            response = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    SearchType.SONGLIST,
                    num=limit,
                )
            )
            result.playlists = []
            for playlist in response.songlist:
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.playlists.append(self._parse_playlist(playlist.model_dump()))
        return result

    @use_cache(3600 * 24 * 7)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.isdigit():
            raise MediaNotFoundError(
                f"Artist id {prov_artist_id} is not a QQ singer mid, cannot fetch artist details"
            )
        response = await self._run_with_session(self._qq_singer.get_info(prov_artist_id))
        artist_obj = response.singer.model_dump()
        if not artist_obj.get("name"):
            artist_obj["name"] = response.base_info.name
        if not artist_obj.get("singer_pic"):
            artist_obj["avatar_url"] = response.base_info.avatar
        if not artist_obj.get("mid"):
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return self._parse_artist(artist_obj)

    @use_cache(3600 * 12, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get all albums for artist."""
        if prov_artist_id.isdigit():
            raise MediaNotFoundError(
                f"Artist id {prov_artist_id} is not a QQ singer mid, cannot fetch albums"
            )
        response = await self._run_with_session(
            self._qq_singer.get_tab_detail(
                prov_artist_id,
                TabType.ALBUM,
                page=1,
                num=100,
            )
        )
        albums: list[Album] = []
        for album in response.album_tab.albums:
            with suppress(InvalidDataError, TypeError, ValueError):
                albums.append(self._parse_album(album.model_dump()))
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
        return [self._parse_track(song.model_dump()) for song in response.song_list if song.mid]

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
        album_obj = response.album.model_dump()
        album_obj["singers"] = [singer.model_dump() for singer in response.singers]
        if not album_obj.get("mid"):
            raise MediaNotFoundError(f"Album {prov_album_id} returned unexpected payload")
        return self._parse_album(album_obj)

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for album id."""
        album_value: str | int = int(prov_album_id) if prov_album_id.isdigit() else prov_album_id
        response = await self._run_with_session(
            self._qq_album.get_song(album_value, num=300, page=1)
        )
        return [self._parse_track(song.model_dump()) for song in response.song_list if song.mid]

    @use_cache(3600 * 24 * 7, cache_checksum="qqmusic_lyrics_v2")
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track_value: str | int = int(prov_track_id) if prov_track_id.isdigit() else prov_track_id
        response = await self._run_with_session(self._qq_song.get_detail(track_value))
        if not response.track.mid:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        track = self._parse_track(response.track.model_dump())
        try:
            # Prefer normal lyric first: this is typically LRC and works best for MA synced scroll.
            lyric_response = await self._run_with_session(
                self._qq_lyric.get_lyric(prov_track_id, qrc=False, trans=True)
            )
            lyric_text = lyric_response.lyric.strip()
            trans_text = lyric_response.trans.strip()
            # Fallback to QRC when standard lyric is empty/unavailable.
            if not lyric_text:
                lyric_response = await self._run_with_session(
                    self._qq_lyric.get_lyric(prov_track_id, qrc=True, trans=True)
                )
                lyric_text = lyric_response.lyric.strip()
                trans_text = lyric_response.trans.strip() or trans_text
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
        except MusicAssistantError as err:
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
                )
            )
            if not response.users:
                break
            for artist in response.users:
                artist_obj = artist.model_dump()
                try:
                    yield self._parse_artist(artist_obj)
                    total_yielded += 1
                except (InvalidDataError, TypeError, ValueError) as error:
                    mapping = get_artist_mapping(artist_obj, self.instance_id)
                    item_id = mapping.item_id if mapping else None
                    self.report_skipped_sync_item(MediaType.ARTIST, item_id, error)
                    continue
            if len(response.users) < num:
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
                self._qq_user.get_fav_song(euin, page=page, num=num)
            )
            if total is None:
                total = response.total
            if not response.songs:
                break
            for song in response.songs:
                try:
                    yield self._parse_track(song.model_dump())
                    yielded += 1
                except (InvalidDataError, TypeError, ValueError) as error:
                    self.report_skipped_sync_item(MediaType.TRACK, song.mid or None, error)
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
                self._qq_user.get_fav_album(euin, page=page, num=num)
            )
            if not response.albums:
                break
            for album in response.albums:
                album_obj = album.model_dump()
                try:
                    yield self._parse_album(album_obj)
                    total_yielded += 1
                except (InvalidDataError, TypeError, ValueError) as error:
                    self.report_skipped_sync_item(MediaType.ALBUM, album.mid or None, error)
                    continue
            if len(response.albums) < num:
                break
            page += 1
        self.logger.info("QQ library albums sync yielded %s album(s)", total_yielded)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve user playlists from QQ Music."""
        euin = await self._ensure_user_euin()
        created = await self._run_with_session(self._qq_user.get_created_songlist(self._musicid))
        for playlist in created.playlists:
            playlist_obj = playlist.model_dump()
            try:
                yield self._parse_playlist(playlist_obj)
            except (InvalidDataError, TypeError, ValueError) as error:
                self.report_skipped_sync_item(
                    MediaType.PLAYLIST, self._skipped_playlist_id(playlist_obj), error
                )
                continue

        page = 1
        num = 100
        while True:
            response = await self._run_with_session(
                self._qq_user.get_fav_songlist(euin, page=page, num=num)
            )
            if not response.playlists:
                break
            for playlist in response.playlists:
                playlist_obj = playlist.model_dump()
                try:
                    yield self._parse_playlist(playlist_obj)
                except (InvalidDataError, TypeError, ValueError) as error:
                    self.report_skipped_sync_item(
                        MediaType.PLAYLIST, self._skipped_playlist_id(playlist_obj), error
                    )
                    continue
            if len(response.playlists) < num:
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
        if not response.info.id:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        # Ensure parsed playlist keeps composite id including dirid.
        playlist_obj = response.info.model_dump()
        playlist_obj.update(id=dissid, dirid=dirid)
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
        results: list[Track] = []
        for index, song in enumerate(response.songs, start=1 + page * 200):
            try:
                track = self._parse_track(song.model_dump())
                track.position = index
                results.append(track)
            except InvalidDataError, TypeError, ValueError:
                continue
        return results

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        created = await self._run_with_session(self._qq_songlist.create(dirname=name))
        if created.id <= 0 or created.dirid <= 0:
            raise InvalidDataError("QQ Music create playlist response missing dirid")
        return await self.get_playlist(self._build_playlist_id(created.id, created.dirid))

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
        for artist in response.singerlist:
            if len(artists) >= limit:
                break
            with suppress(InvalidDataError, TypeError, ValueError):
                artists.append(self._parse_artist(artist.model_dump()))
        return artists

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        song_id = await self._resolve_song_id(prov_track_id)
        response = await self._run_with_session(self._qq_song.get_similar_song(song_id))
        tracks: list[Track] = []
        for group in response.song:
            if len(tracks) >= limit:
                break
            for song in group.song:
                if len(tracks) >= limit:
                    break
                with suppress(InvalidDataError, TypeError, ValueError):
                    tracks.append(self._parse_track(song.model_dump()))
        return tracks

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return streamdetails for given track id."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Unsupported media type {media_type}")
        track_response = await self._run_with_session(self._qq_song.get_detail(item_id))
        track = track_response.track
        if not track.mid:
            raise MediaNotFoundError(f"Track {item_id} not found")
        (
            stream_url,
            selected_file_type,
            is_preview_stream,
            preview_duration,
            stream_expiration,
        ) = await self._resolve_stream_url(item_id, track)

        if not stream_url:
            raise UnplayableMediaError(
                f"No playable stream URL returned for track {item_id} "
                f"(pay_play={track.pay.pay_play}, pay_status={track.pay.pay_status})"
            )

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
            expiration=stream_expiration or 600,
        )
