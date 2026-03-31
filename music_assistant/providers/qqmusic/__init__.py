"""QQ Music provider implementation."""

from __future__ import annotations

import asyncio
import html
import importlib
import logging
import re
import time
from asyncio import Semaphore
from base64 import b64encode
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
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

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_ACTION_CHECK_QR_AUTH,
    CONF_ACTION_CLEAR_AUTH,
    CONF_ACTION_START_QR_AUTH,
    CONF_LOGIN_TYPE,
    CONF_MUSICID,
    CONF_MUSICKEY,
    CONF_QR_IDENTIFIER,
    CONF_QR_PAGE_URL,
    CONF_QR_TYPE,
    CONF_QUALITY,
    CONF_UIN,
    QUALITY_FLAC,
    QUALITY_HI_RES,
    QUALITY_MP3_128,
    QUALITY_MP3_320,
)
from .helpers import clean_text, extract_artist_mid, extract_first_text, normalize_image_url
from .parsers import (
    describe_payload,
    extract_guess_recommend_tracks,
    extract_items,
    extract_newsong_tracks,
    extract_radar_recommend_tracks,
    extract_recommend_songlists,
    extract_song_id,
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
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.LYRICS,
}

_QR_ROUTE_UNREGISTER: dict[str, Any] = {}
_LRC_TIMESTAMP_PATTERN = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
_QRC_LINE_TIMESTAMP_PATTERN = re.compile(r"\[\d+,\d+\]")
_QRC_LINE_PREFIX_PATTERN = re.compile(r"^\[(\d+),(\d+)\]")
_QRC_WORD_TIMESTAMP_PATTERN = re.compile(r"\(\d+,\d+\)")
_RECOMMEND_GUESS_TTL = 60 * 60
_RECOMMEND_NEWSONG_TTL = 60 * 60 * 6
_RECOMMEND_PLAYLIST_TTL = 60 * 60 * 6


def _clear_qr_routes() -> None:
    """Unregister all temporary QR routes."""
    for unregister in list(_QR_ROUTE_UNREGISTER.values()):
        with suppress(Exception):
            unregister()
    _QR_ROUTE_UNREGISTER.clear()


def _register_qr_auth_page(
    mass: MusicAssistant,
    session_id: str,
    image_bytes: bytes,
    mime_type: str,
) -> str:
    """Register a temporary web route for QR image and return relative URL."""
    if not getattr(mass, "webserver", None):
        b64 = b64encode(image_bytes).decode("ascii")
        return f"data:{mime_type};base64,{b64}"

    route_path = f"/auth/qqmusic/qr/{session_id}"
    if unregister := _QR_ROUTE_UNREGISTER.pop(route_path, None):
        unregister()

    async def _serve_qr(_: web.Request) -> web.Response:
        return web.Response(
            body=image_bytes,
            content_type=mime_type,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    unregister = mass.webserver.register_dynamic_route(route_path, _serve_qr, "GET")
    _QR_ROUTE_UNREGISTER[route_path] = unregister
    return f"{route_path}?ts={int(time.time())}"


def _normalize_qq_lyric_text(raw_text: str) -> str:
    """Normalize QQ lyric/qrc payload to readable plain text."""
    text = html.unescape(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\n", "\n")
    text = _QRC_LINE_TIMESTAMP_PATTERN.sub("", text)
    text = _QRC_WORD_TIMESTAMP_PATTERN.sub("", text)
    lines: list[str] = []
    for raw_line in text.split("\n"):
        cleaned_line = raw_line.strip()
        if cleaned_line:
            lines.append(cleaned_line)
    return "\n".join(lines)


def _ms_to_lrc_timestamp(ms_value: int) -> str:
    """Convert milliseconds to LRC timestamp string (mm:ss.xx)."""
    minute = ms_value // 60000
    second = (ms_value % 60000) // 1000
    centisecond = (ms_value % 1000) // 10
    return f"{minute:02d}:{second:02d}.{centisecond:02d}"


def _qrc_to_lrc(raw_text: str) -> str:
    """Convert QQ QRC line-timed lyric text to basic LRC format."""
    text = html.unescape(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\n", "\n")
    lrc_lines: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        line_match = _QRC_LINE_PREFIX_PATTERN.match(line)
        if not line_match:
            continue
        start_ms = int(line_match.group(1))
        lyric_content = line[line_match.end() :]
        lyric_content = _QRC_WORD_TIMESTAMP_PATTERN.sub("", lyric_content).strip()
        if not lyric_content:
            continue
        lrc_lines.append(f"[{_ms_to_lrc_timestamp(start_ms)}]{lyric_content}")
    return "\n".join(lrc_lines)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return QQMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(  # noqa: PLR0915
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    if values is None:
        values = {}
    qr_identifier = str(values.get(CONF_QR_IDENTIFIER) or "")
    qr_page_url = str(values.get(CONF_QR_PAGE_URL) or "")
    has_qr_pending = bool(qr_identifier)
    has_saved_auth = bool(values.get(CONF_MUSICID) and values.get(CONF_MUSICKEY))
    is_verified = bool(values.get(CONF_UIN)) or has_saved_auth

    if action == CONF_ACTION_CLEAR_AUTH:
        values[CONF_UIN] = None
        values[CONF_MUSICID] = None
        values[CONF_MUSICKEY] = None
        values[CONF_LOGIN_TYPE] = None
        values[CONF_QR_IDENTIFIER] = None
        values[CONF_QR_TYPE] = None
        values[CONF_QR_PAGE_URL] = None
        has_qr_pending = False
        is_verified = False
        _clear_qr_routes()
    elif action == CONF_ACTION_START_QR_AUTH:
        _clear_qr_routes()
        login_mod = importlib.import_module("qqmusic_api.login")
        qr = await login_mod.get_qrcode(login_mod.QRLoginType.QQ)
        if not getattr(qr, "identifier", None) or not getattr(qr, "data", None):
            raise LoginFailed("Failed to generate QQ Music login QR code")
        values[CONF_QR_IDENTIFIER] = str(qr.identifier)
        values[CONF_QR_TYPE] = str(getattr(qr.qr_type, "value", "qq"))
        has_qr_pending = True
        session_id = values.get("session_id")
        if session_id:
            qr_page_url = _register_qr_auth_page(
                mass,
                str(session_id),
                bytes(qr.data),
                str(getattr(qr, "mimetype", "image/png")),
            )
            values[CONF_QR_PAGE_URL] = qr_page_url
            mass.signal_event(EventType.AUTH_SESSION, str(session_id), qr_page_url)
        # Auto-poll like other providers: user clicks once, scans, and auth completes.
        if hasattr(login_mod, "QR") and hasattr(login_mod, "check_qrcode"):
            deadline = time.monotonic() + 120
            qr_type_value = str(values.get(CONF_QR_TYPE))
            qr_type = next(
                (item for item in login_mod.QRLoginType if item.value == qr_type_value),
                login_mod.QRLoginType.QQ,
            )
            while time.monotonic() < deadline:
                qr_ref = login_mod.QR(
                    data=b"",
                    qr_type=qr_type,
                    mimetype="image/png",
                    identifier=str(values.get(CONF_QR_IDENTIFIER) or ""),
                )
                try:
                    event, credential = await login_mod.check_qrcode(qr_ref)
                except Exception:
                    # Keep polling through transient network/API hiccups.
                    await asyncio.sleep(1.5)
                    continue
                if event == login_mod.QRCodeLoginEvents.DONE and credential:
                    if not credential.musicid or not credential.musickey:
                        raise LoginFailed("QR login succeeded but credential is incomplete")
                    values[CONF_UIN] = str(credential.musicid)
                    values[CONF_MUSICID] = str(credential.musicid)
                    values[CONF_MUSICKEY] = str(credential.musickey)
                    values[CONF_LOGIN_TYPE] = str(credential.login_type or 2)
                    values[CONF_QR_IDENTIFIER] = None
                    values[CONF_QR_TYPE] = None
                    values[CONF_QR_PAGE_URL] = None
                    is_verified = True
                    has_qr_pending = False
                    break
                if event == login_mod.QRCodeLoginEvents.TIMEOUT:
                    values[CONF_QR_IDENTIFIER] = None
                    values[CONF_QR_TYPE] = None
                    values[CONF_QR_PAGE_URL] = None
                    has_qr_pending = False
                    raise InvalidDataError("QR code expired, please generate a new one")
                if event == login_mod.QRCodeLoginEvents.REFUSE:
                    raise InvalidDataError("Login was rejected in QQ app")
                await asyncio.sleep(1)
            if not is_verified and values.get(CONF_QR_IDENTIFIER):
                raise InvalidDataError(
                    "Waiting for scan confirmation timed out. "
                    "You can click Start QR login again or use Check QR status."
                )
    elif action == CONF_ACTION_CHECK_QR_AUTH:
        if not qr_identifier:
            raise InvalidDataError("Please generate a QR code first")
        login_mod = importlib.import_module("qqmusic_api.login")
        qr_type_val = str(values.get(CONF_QR_TYPE) or "qq")
        qr_type = next(
            (item for item in login_mod.QRLoginType if item.value == qr_type_val),
            login_mod.QRLoginType.QQ,
        )
        qr = login_mod.QR(
            data=b"",
            qr_type=qr_type,
            mimetype="image/png",
            identifier=qr_identifier,
        )
        event, credential = await login_mod.check_qrcode(qr)
        if event == login_mod.QRCodeLoginEvents.DONE and credential:
            if not credential.musicid or not credential.musickey:
                raise LoginFailed("QR login succeeded but credential is incomplete")
            values[CONF_UIN] = str(credential.musicid)
            values[CONF_MUSICID] = str(credential.musicid)
            values[CONF_MUSICKEY] = str(credential.musickey)
            values[CONF_LOGIN_TYPE] = str(credential.login_type or 2)
            values[CONF_QR_IDENTIFIER] = None
            values[CONF_QR_TYPE] = None
            values[CONF_QR_PAGE_URL] = None
            is_verified = True
            has_qr_pending = False
        elif event == login_mod.QRCodeLoginEvents.SCAN:
            raise InvalidDataError("QR code not scanned yet")
        elif event == login_mod.QRCodeLoginEvents.CONF:
            raise InvalidDataError("QR scanned, please confirm login in QQ app")
        elif event == login_mod.QRCodeLoginEvents.TIMEOUT:
            values[CONF_QR_IDENTIFIER] = None
            values[CONF_QR_TYPE] = None
            values[CONF_QR_PAGE_URL] = None
            has_qr_pending = False
            raise InvalidDataError("QR code expired, please generate a new one")
        elif event == login_mod.QRCodeLoginEvents.REFUSE:
            raise InvalidDataError("Login was rejected in QQ app")
        else:
            raise LoginFailed("Unable to determine QR login status")

    status_label = (
        "QQ Music login confirmed. Close the QR page and click Save to finish setup."
        if is_verified
        else "QR code generated. Open the popup page, scan with QQ, and confirm login."
        if has_qr_pending
        else "Click QR Login to start authentication."
    )
    help_text = (
        "Login flow: 1) Click QR Login. 2) In the newly opened page, scan with QQ and confirm. "
        "3) Close the QR page. 4) Click Save."
    )

    return (
        ConfigEntry(
            key="auth_help",
            type=ConfigEntryType.LABEL,
            label=help_text,
        ),
        ConfigEntry(
            key="auth_status",
            type=ConfigEntryType.LABEL,
            label=status_label,
        ),
        ConfigEntry(
            key=CONF_QR_PAGE_URL,
            type=ConfigEntryType.STRING,
            label="QR login page URL",
            required=False,
            hidden=not has_qr_pending or not qr_page_url,
            value=qr_page_url,
        ),
        ConfigEntry(
            key=CONF_UIN,
            type=ConfigEntryType.STRING,
            label=CONF_UIN,
            hidden=True,
            required=False,
            value=str(values.get(CONF_UIN) or ""),
        ),
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            label="Preferred quality",
            default_value=QUALITY_MP3_320,
            options=[
                ConfigValueOption("MP3 128kbps (most compatible)", QUALITY_MP3_128),
                ConfigValueOption("MP3 320kbps", QUALITY_MP3_320),
                ConfigValueOption("FLAC (fallback to MP3)", QUALITY_FLAC),
                ConfigValueOption(
                    "Hi-Res (Master, fallback to FLAC/MP3)",
                    QUALITY_HI_RES,
                ),
            ],
            hidden=not is_verified,
        ),
        ConfigEntry(
            key=CONF_ACTION_START_QR_AUTH,
            type=ConfigEntryType.ACTION,
            label="QR Login",
            description="Generate QR code and open the login popup page.",
            action=CONF_ACTION_START_QR_AUTH,
            hidden=is_verified,
        ),
        ConfigEntry(
            key=CONF_ACTION_CHECK_QR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Check QR status",
            description="Manually check whether QQ scan confirmation is completed.",
            action=CONF_ACTION_CHECK_QR_AUTH,
            hidden=not has_qr_pending or is_verified,
        ),
        ConfigEntry(
            key=CONF_ACTION_CLEAR_AUTH,
            type=ConfigEntryType.ACTION,
            label="Reset authentication",
            action=CONF_ACTION_CLEAR_AUTH,
            hidden=not (has_qr_pending or is_verified),
        ),
        ConfigEntry(
            key=CONF_MUSICID,
            type=ConfigEntryType.STRING,
            label=CONF_MUSICID,
            hidden=True,
            required=False,
            value=str(values.get(CONF_MUSICID) or ""),
        ),
        ConfigEntry(
            key=CONF_MUSICKEY,
            type=ConfigEntryType.SECURE_STRING,
            label=CONF_MUSICKEY,
            hidden=True,
            required=False,
            value=str(values.get(CONF_MUSICKEY) or ""),
        ),
        ConfigEntry(
            key=CONF_LOGIN_TYPE,
            type=ConfigEntryType.STRING,
            label=CONF_LOGIN_TYPE,
            hidden=True,
            required=False,
            value=str(values.get(CONF_LOGIN_TYPE) or ""),
        ),
        ConfigEntry(
            key=CONF_QR_IDENTIFIER,
            type=ConfigEntryType.STRING,
            label=CONF_QR_IDENTIFIER,
            hidden=True,
            required=False,
            value=str(values.get(CONF_QR_IDENTIFIER) or ""),
        ),
        ConfigEntry(
            key=CONF_QR_TYPE,
            type=ConfigEntryType.STRING,
            label=CONF_QR_TYPE,
            hidden=True,
            required=False,
            value=str(values.get(CONF_QR_TYPE) or ""),
        ),
    )


class QQMusicProvider(MusicProvider):
    """QQ Music provider."""

    _credential: Any = None
    _qq_search: Any = None
    _qq_song: Any = None
    _qq_album: Any = None
    _qq_singer: Any = None
    _qq_session_cls: Any = None
    _qq_set_session: Any = None
    _qq_clear_session: Any = None
    _qq_session: Any = None
    _qq_user: Any = None
    _qq_songlist: Any = None
    _qq_lyric: Any = None
    _qq_recommend: Any = None
    _qq_response_code_error: Any = None
    _qq_credential_expired_error: Any = None
    _qq_login_error: Any = None
    _api_semaphore: Semaphore
    _musicid: int = 0
    _euin: str = ""
    _recommend_payload_cache: dict[str, tuple[float, Any]]

    async def handle_async_init(self) -> None:
        """Validate auth and initialize qqmusic api adapters."""
        config_musicid = self.config.get_value(CONF_MUSICID) or self.config.get_value(CONF_UIN)
        config_musickey = self.config.get_value(CONF_MUSICKEY)
        config_login_type = self.config.get_value(CONF_LOGIN_TYPE)

        if not (config_musicid and config_musickey):
            raise LoginFailed("No QQ Music authentication configured, please login by QR code")
        uin = int(str(config_musicid))
        musickey = str(config_musickey)
        login_type_raw = str(config_login_type or "2")
        login_type = int(login_type_raw) if login_type_raw.isdigit() else 2

        credential_mod = importlib.import_module("qqmusic_api.utils.credential")
        session_mod = importlib.import_module("qqmusic_api.utils.session")
        exception_mod = importlib.import_module("qqmusic_api.exceptions")
        self._qq_search = importlib.import_module("qqmusic_api.search")
        self._qq_song = importlib.import_module("qqmusic_api.song")
        self._qq_album = importlib.import_module("qqmusic_api.album")
        self._qq_singer = importlib.import_module("qqmusic_api.singer")
        self._qq_user = importlib.import_module("qqmusic_api.user")
        self._qq_songlist = importlib.import_module("qqmusic_api.songlist")
        self._qq_lyric = importlib.import_module("qqmusic_api.lyric")
        self._qq_recommend = importlib.import_module("qqmusic_api.recommend")
        self._qq_session_cls = session_mod.Session
        self._qq_set_session = session_mod.set_session
        self._qq_clear_session = session_mod.clear_session
        # Keep qqmusic_api internal logs in sync with MA log level.
        logging.getLogger("qqmusicapi").setLevel(self.logger.level + 10)
        self._credential = credential_mod.Credential.from_cookies_dict(
            {
                "musicid": str(uin),
                "musickey": musickey,
                "loginType": login_type,
            }
        )
        self._qq_session = self._qq_session_cls(credential=self._credential, enable_sign=True)
        self._qq_set_session(self._qq_session)
        self._qq_response_code_error = getattr(exception_mod, "ResponseCodeError", None)
        self._qq_credential_expired_error = getattr(exception_mod, "CredentialExpiredError", None)
        self._qq_login_error = getattr(exception_mod, "LoginError", None)
        self._api_semaphore = Semaphore(4)
        self._musicid = int(uin)
        self._recommend_payload_cache = {}
        self.logger.info("QQ Music authenticated for uin %s", uin)

    async def _run_with_session(self, coro: Awaitable[Any]) -> Any:
        """Run qqmusic_api call with the provider-bound Session."""
        try:
            if self._qq_set_session and self._qq_session:
                self._qq_set_session(self._qq_session)
            async with self._api_semaphore:
                return await coro
        except Exception as err:
            raise self._translate_qq_exception(err) from err

    def _translate_qq_exception(self, err: Exception) -> Exception:
        """Translate qqmusic_api/http exceptions to MA domain exceptions."""
        if self._qq_credential_expired_error and isinstance(err, self._qq_credential_expired_error):
            return LoginFailed("QQ Music credential expired, please re-authenticate")
        if self._qq_login_error and isinstance(err, self._qq_login_error):
            return LoginFailed(f"QQ Music login failed: {err}")
        if self._qq_response_code_error and isinstance(err, self._qq_response_code_error):
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
        if self._qq_session:
            await self._qq_session.aclose()
        if self._qq_clear_session:
            self._qq_clear_session()
        if is_removed:
            _clear_qr_routes()
        self._qq_session = None
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
                self._qq_song.SongFileType.MASTER,
                self._qq_song.SongFileType.FLAC,
                self._qq_song.SongFileType.MP3_320,
                self._qq_song.SongFileType.MP3_128,
            ]
        if quality == QUALITY_FLAC:
            return [
                self._qq_song.SongFileType.FLAC,
                self._qq_song.SongFileType.MP3_320,
                self._qq_song.SongFileType.MP3_128,
            ]
        if quality == QUALITY_MP3_320:
            return [
                self._qq_song.SongFileType.MP3_320,
                self._qq_song.SongFileType.MP3_128,
            ]
        return [self._qq_song.SongFileType.MP3_128]

    async def _resolve_stream_url(
        self, item_id: str, track_obj: dict[str, Any]
    ) -> tuple[str, Any | None, bool, int | None]:
        """Resolve stream URL with full-stream and preview fallback."""
        stream_url = ""
        selected_file_type = None
        is_preview_stream = False
        preview_duration = None

        for file_type in self._get_candidate_file_types():
            url_map = await self._run_with_session(
                self._qq_song.get_song_urls(
                    [item_id],
                    file_type=file_type,
                    credential=self._credential,
                )
            )
            url = str(url_map.get(item_id) or "")
            if url.startswith("http"):
                return (url, file_type, False, None)

        # Fallback to 30s preview URL when full stream URL is unavailable.
        vs_list = track_obj.get("vs")
        if isinstance(vs_list, list):
            first_vs = next((vs for vs in vs_list if isinstance(vs, str) and vs), None)
            if first_vs:
                try_url = await self._run_with_session(self._qq_song.get_try_url(item_id, first_vs))
                if isinstance(try_url, str) and try_url.startswith("http"):
                    stream_url = try_url
                    selected_file_type = self._qq_song.SongFileType.MP3_128
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
        if selected_file_type == self._qq_song.SongFileType.FLAC:
            return ContentType.FLAC
        if selected_file_type in (
            self._qq_song.SongFileType.ACC_48,
            self._qq_song.SongFileType.ACC_96,
            self._qq_song.SongFileType.ACC_192,
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
        if selected_file_type == self._qq_song.SongFileType.FLAC:
            return AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16)
        if selected_file_type == self._qq_song.SongFileType.MASTER:
            return AudioFormat(content_type=ContentType.FLAC, sample_rate=192000, bit_depth=24)
        if selected_file_type == self._qq_song.SongFileType.MP3_320:
            return AudioFormat(content_type=ContentType.MPEG, bit_rate=320000)
        if selected_file_type == self._qq_song.SongFileType.MP3_128:
            return AudioFormat(content_type=ContentType.MPEG, bit_rate=128000)
        if selected_file_type == self._qq_song.SongFileType.ACC_192:
            return AudioFormat(content_type=ContentType.M4A, bit_rate=192000)
        if selected_file_type == self._qq_song.SongFileType.ACC_96:
            return AudioFormat(content_type=ContentType.M4A, bit_rate=96000)
        if selected_file_type == self._qq_song.SongFileType.ACC_48:
            return AudioFormat(content_type=ContentType.M4A, bit_rate=48000)
        return AudioFormat(content_type=self._get_content_type(selected_file_type))

    def _get_artist_mapping(self, artist_obj: dict[str, Any] | str) -> ItemMapping | None:
        if isinstance(artist_obj, str):
            # Without singer mid we can not resolve artist detail endpoints reliably.
            return None
        artist_id = extract_artist_mid(artist_obj)
        if not artist_id:
            return None
        artist_name = extract_first_text(
            artist_obj,
            (
                "name",
                "title",
                "singerName",
                "singername",
                "singer_name",
                "title_main",
                "search_title",
                "singerName_hilight",
                "name_hilight",
                "title_hilight",
            ),
            "Unknown Artist",
        )
        return ItemMapping(
            media_type=MediaType.ARTIST,
            item_id=artist_id,
            provider=self.instance_id,
            name=artist_name,
        )

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse raw qqmusic artist object."""
        artist_id = extract_artist_mid(artist_obj)
        if not artist_id:
            raise InvalidDataError("Artist object does not contain id/mid")
        artist_name = extract_first_text(
            artist_obj,
            (
                "name",
                "Name",
                "title",
                "singerName",
                "singername",
                "singer_name",
                "SingerName",
                "title_main",
                "search_title",
                "singerName_hilight",
                "name_hilight",
                "title_hilight",
                "singer_hilight",
                "matchName",
                "match_name",
                "nickname",
                "nick",
            ),
            "Unknown Artist",
        )
        artist = Artist(
            item_id=artist_id,
            provider=self.domain,
            name=artist_name,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://y.qq.com/n/ryqq/singer/{artist_id}",
                )
            },
        )
        subtitle = clean_text(
            artist_obj.get("subtitle")
            or artist_obj.get("desc")
            or artist_obj.get("description")
            or "",
            "",
        )
        if subtitle:
            artist.metadata.description = subtitle
        artist_image = (
            normalize_image_url(
                artist_obj.get("singerPic")
                or artist_obj.get("pic")
                or artist_obj.get("avatar")
                or artist_obj.get("Avatar")
                or artist_obj.get("AvatarUrl")
                or artist_obj.get("singer_pic")
            )
            or f"https://y.qq.com/music/photo_new/T001R500x500M000{artist_id}.jpg"
        )
        artist.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artist_image,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )
        return artist

    def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        """Parse raw qqmusic album object."""
        album_id = str(
            album_obj.get("mid")
            or album_obj.get("albumMid")
            or album_obj.get("albumMID")
            or album_obj.get("album_mid")
            or album_obj.get("albummid")
            or album_obj.get("id")
            or album_obj.get("albumID")
            or ""
        )
        if not album_id:
            raise InvalidDataError("Album object does not contain id/mid")
        raw_album_name = str(
            album_obj.get("title")
            or album_obj.get("name")
            or album_obj.get("albumName")
            or "Unknown Album"
        )
        album_subtitle = clean_text(
            album_obj.get("subtitle")
            or album_obj.get("albumTranName")
            or album_obj.get("title_extra")
            or "",
            "",
        )
        album_name, album_version = parse_title_and_version(raw_album_name, album_subtitle)
        album_name = clean_text(album_name, "Unknown Album")
        album = Album(
            item_id=album_id,
            provider=self.domain,
            name=album_name,
            version=album_version,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://y.qq.com/n/ryqq/albumDetail/{album_id}",
                )
            },
        )
        if release_str := album_obj.get("time_public") or album_obj.get("publishDate"):
            with suppress(ValueError):
                album.year = datetime.strptime(release_str, "%Y-%m-%d").year  # noqa: DTZ007
        album_desc = clean_text(
            album_obj.get("description")
            or album_obj.get("description2")
            or album_obj.get("desc")
            or "",
            "",
        )
        if album_desc:
            album.metadata.description = album_desc
        artist_candidates = album_obj.get("singer")
        if isinstance(artist_candidates, dict):
            artist_iterable: list[dict[str, Any] | str] = [artist_candidates]
        elif isinstance(artist_candidates, list):
            artist_iterable = artist_candidates
        elif isinstance(artist_candidates, str):
            artist_iterable = [artist_candidates]
        else:
            singer_list = album_obj.get("singer_list")
            artist_iterable = singer_list if isinstance(singer_list, list) else []
        for artist_obj in artist_iterable:
            if artist_mapping := self._get_artist_mapping(artist_obj):
                album.artists.append(artist_mapping)
        cover_path = normalize_image_url(
            album_obj.get("pic")
            or album_obj.get("picUrl")
            or album_obj.get("picurl")
            or album_obj.get("logo")
            or album_obj.get("cover")
        )
        if cover_path:
            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=cover_path,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        else:
            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=f"https://y.qq.com/music/photo_new/T002R500x500M000{album_id}.jpg",
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return album

    def _parse_track(self, track_obj: dict[str, Any]) -> Track:  # noqa: PLR0915
        """Parse raw qqmusic track object."""
        track_id = str(
            track_obj.get("mid")
            or track_obj.get("songMid")
            or track_obj.get("songmid")
            or track_obj.get("id")
            or ""
        )
        if not track_id:
            raise InvalidDataError("Track object does not contain id/mid")
        raw_track_name = clean_text(
            track_obj.get("title") or track_obj.get("name"),
            "Unknown Track",
        )
        track_subtitle = clean_text(
            track_obj.get("subtitle") or track_obj.get("title_extra") or "",
            "",
        )
        track_name, track_version = parse_title_and_version(raw_track_name, track_subtitle)
        duration = track_obj.get("interval")
        max_audio_format, max_quality_label = self._get_max_supported_audio_format(track_obj)
        track = Track(
            item_id=track_id,
            provider=self.domain,
            name=track_name,
            version=track_version,
            duration=int(duration) if duration else 0,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=max_audio_format,
                    url=f"https://y.qq.com/n/ryqq/songDetail/{track_id}",
                    details=max_quality_label,
                )
            },
        )
        track_desc = clean_text(
            track_obj.get("desc") or track_obj.get("content") or "",
            "",
        )
        if track_desc:
            track.metadata.description = track_desc
        album_obj = track_obj.get("album")
        album_id = ""
        album_name = ""
        if isinstance(album_obj, dict):
            album_id = str(
                album_obj.get("mid")
                or album_obj.get("albumMid")
                or album_obj.get("albumMID")
                or album_obj.get("albummid")
                or album_obj.get("albumID")
                or album_obj.get("albumid")
                or album_obj.get("id")
                or ""
            )
            album_name = clean_text(
                album_obj.get("name")
                or album_obj.get("title")
                or album_obj.get("albumName")
                or album_obj.get("albumTitle")
            )
        if not album_id:
            album_id = str(
                track_obj.get("albummid")
                or track_obj.get("albumMid")
                or track_obj.get("albumMID")
                or track_obj.get("albumid")
                or track_obj.get("albumID")
                or ""
            )
        if not album_name:
            album_name = clean_text(
                track_obj.get("albumname")
                or track_obj.get("albumName")
                or track_obj.get("albumTitle")
                or track_obj.get("album_title")
            )
        if album_id and album_name:
            track.album = Album(
                item_id=album_id,
                provider=self.domain,
                name=album_name,
                provider_mappings={
                    ProviderMapping(
                        item_id=album_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        url=f"https://y.qq.com/n/ryqq/albumDetail/{album_id}",
                    )
                },
            )

        singer_list: list[dict[str, Any] | str]
        raw_singer = (
            track_obj.get("singer") or track_obj.get("singer_list") or track_obj.get("singers")
        )
        if isinstance(raw_singer, dict):
            singer_list = [raw_singer]
        elif isinstance(raw_singer, list):
            singer_list = raw_singer
        elif isinstance(raw_singer, str):
            singer_list = [raw_singer]
        else:
            singer_list = []
        for artist_obj in singer_list:
            if artist_mapping := self._get_artist_mapping(artist_obj):
                track.artists.append(artist_mapping)
        if not track.artists:
            fallback_artist_id = str(
                track_obj.get("singerMid")
                or track_obj.get("singerMID")
                or track_obj.get("singer_mid")
                or ""
            )
            fallback_artist_name = clean_text(
                track_obj.get("singerName")
                or track_obj.get("singername")
                or track_obj.get("singer_name")
                or ""
            )
            if fallback_artist_id and fallback_artist_name:
                track.artists.append(
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=fallback_artist_id,
                        provider=self.instance_id,
                        name=fallback_artist_name,
                    )
                )

        cover_id = ""
        if isinstance(album_obj, dict):
            cover_id = str(
                album_obj.get("mid")
                or album_obj.get("albumMid")
                or album_obj.get("albumMID")
                or album_obj.get("albummid")
                or ""
            )
        if not cover_id:
            cover_id = album_id
        if cover_id:
            if isinstance(track.album, Album):
                track.album.metadata.images = UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=f"https://y.qq.com/music/photo_new/T002R500x500M000{cover_id}.jpg",
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                )
            track.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=f"https://y.qq.com/music/photo_new/T002R500x500M000{cover_id}.jpg",
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return track

    async def _resolve_song_id(self, prov_track_id: str) -> int:
        """Resolve provider track id (mid/id) to numeric song id."""
        if prov_track_id.isdigit():
            return int(prov_track_id)
        response = await self._run_with_session(self._qq_song.get_detail(prov_track_id))
        track_obj = response.get("track_info", {}) if isinstance(response, dict) else {}
        if song_id := extract_song_id(track_obj):
            return song_id
        raise MediaNotFoundError(f"Unable to resolve numeric song id for track {prov_track_id}")

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
        euin = await self._run_with_session(self._qq_user.get_euin(self._musicid))
        if not euin:
            raise LoginFailed("Failed to resolve QQ Music user profile (euin)")
        self._euin = str(euin)
        return self._euin

    def _build_playlist_id(self, dissid: int | str, dirid: int | str) -> str:
        """Build provider playlist id from dissid and dirid."""
        return f"{dissid}:{dirid}"

    def _parse_playlist_id(self, prov_playlist_id: str) -> tuple[int, int]:
        """Parse provider playlist id into (dissid, dirid)."""
        if ":" in prov_playlist_id:
            dissid_raw, dirid_raw = prov_playlist_id.split(":", 1)
            return (int(dissid_raw), int(dirid_raw))
        return (int(prov_playlist_id), 0)

    def _parse_playlist(self, playlist_obj: dict[str, Any]) -> Playlist:
        """Parse raw qqmusic playlist object."""
        dissid = (
            playlist_obj.get("tid") or playlist_obj.get("dissid") or playlist_obj.get("id") or 0
        )
        dirid = playlist_obj.get("dirid") or playlist_obj.get("dirId") or 0
        if not dissid:
            raise InvalidDataError("Playlist object missing dissid/tid")
        playlist_id = self._build_playlist_id(dissid, dirid)
        playlist_name = clean_text(
            playlist_obj.get("dirName")
            or playlist_obj.get("dirname")
            or playlist_obj.get("name")
            or playlist_obj.get("title")
            or playlist_obj.get("search_title")
            or playlist_obj.get("name_hilight")
            or playlist_obj.get("title_hilight")
            or playlist_obj.get("diss_name")
            or playlist_obj.get("dissname")
            or "QQ Music Playlist",
            "QQ Music Playlist",
        )
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.domain,
            name=playlist_name,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://y.qq.com/n/ryqq/playlist/{dissid}",
                )
            },
        )
        owner_name = str(
            playlist_obj.get("creator", {}).get("name")
            or playlist_obj.get("creator", {}).get("nick")
            or playlist_obj.get("nickname")
            or playlist_obj.get("nick")
            or ""
        )
        if owner_name:
            playlist.owner = owner_name
        description = clean_text(
            playlist_obj.get("desc") or playlist_obj.get("description"),
            "",
        )
        if description:
            playlist.metadata.description = description

        cover_val = playlist_obj.get("cover")
        cover = ""
        if isinstance(cover_val, dict):
            cover = normalize_image_url(
                cover_val.get("default_url")
                or cover_val.get("medium_url")
                or cover_val.get("big_url")
                or cover_val.get("small_url")
            )
        if not cover:
            cover = normalize_image_url(
                cover_val
                or playlist_obj.get("logo")
                or playlist_obj.get("picurl")
                or playlist_obj.get("cover_url_big")
                or playlist_obj.get("pic")
                or playlist_obj.get("picUrl")
            )
        if cover:
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=cover,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return playlist

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
                    self._qq_search.SearchType.SONG,
                    num=limit,
                )
            )
            result.tracks = []
            for item in raw_tracks:
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.tracks.append(self._parse_track(item))

        if MediaType.ALBUM in media_types:
            raw_albums = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    self._qq_search.SearchType.ALBUM,
                    num=limit,
                )
            )
            result.albums = []
            for item in raw_albums:
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.albums.append(self._parse_album(item))

        if MediaType.ARTIST in media_types:
            raw_artists = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    self._qq_search.SearchType.SINGER,
                    num=limit,
                )
            )
            result.artists = []
            for item in raw_artists:
                with suppress(InvalidDataError, TypeError, ValueError):
                    result.artists.append(self._parse_artist(item))

        if MediaType.PLAYLIST in media_types:
            raw_playlists = await self._run_with_session(
                self._qq_search.search_by_type(
                    search_query,
                    self._qq_search.SearchType.SONGLIST,
                    num=limit,
                )
            )
            result.playlists = []
            for item in raw_playlists:
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
        info = response.get("Info", {}) if isinstance(response, dict) else {}
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
        if not artist_obj:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        return self._parse_artist(artist_obj)

    @use_cache(3600 * 12)
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
                    self._qq_singer.TabType.ALBUM,
                    page=1,
                    num=100,
                )
            )
            if isinstance(tab_albums, list):
                raw_albums = [item for item in tab_albums if isinstance(item, dict)]
        except (MediaNotFoundError, InvalidDataError, TypeError, ValueError):
            raw_albums = []

        if not raw_albums:
            response = await self._run_with_session(
                self._qq_singer.get_album_list(
                    prov_artist_id,
                    number=100,
                    begin=0,
                )
            )
            if isinstance(response, dict):
                raw_albums = extract_items(
                    response,
                    ("albumList", "album_list", "list"),
                )

        if not raw_albums:
            all_albums = await self._run_with_session(
                self._qq_singer.get_album_list_all(prov_artist_id)
            )
            if isinstance(all_albums, list):
                raw_albums = [item for item in all_albums if isinstance(item, dict)]

        albums: list[Album] = []
        for item in raw_albums:
            with suppress(InvalidDataError, TypeError, ValueError):
                albums.append(self._parse_album(item))
        return albums

    @use_cache(3600 * 6)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks for artist."""
        if prov_artist_id.isdigit():
            raise MediaNotFoundError(
                f"Artist id {prov_artist_id} is not a QQ singer mid, cannot fetch top tracks"
            )
        response = await self._run_with_session(
            self._qq_singer.get_songs_list(
                prov_artist_id,
                number=100,
                begin=0,
            )
        )
        songs = [
            item.get("songInfo") for item in response.get("songList", []) if item.get("songInfo")
        ]
        return [self._parse_track(item) for item in songs if item.get("mid")]

    @use_cache(3600 * 24 * 7)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        album_value: str | int = int(prov_album_id) if prov_album_id.isdigit() else prov_album_id
        response = await self._run_with_session(self._qq_album.get_detail(album_value))
        if not response:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        album_obj: dict[str, Any] | None = None
        if isinstance(response, dict):
            basic_info = response.get("basicInfo")
            if isinstance(basic_info, dict):
                album_obj = dict(basic_info)
                if "singer" not in album_obj:
                    singer_list = response.get("singer", {}).get("singerList")
                    if isinstance(singer_list, list):
                        album_obj["singer"] = singer_list
            else:
                album_obj = response
        if not isinstance(album_obj, dict):
            raise MediaNotFoundError(f"Album {prov_album_id} returned unexpected payload")
        return self._parse_album(album_obj)

    @use_cache(3600 * 24 * 7)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for album id."""
        album_value: str | int = int(prov_album_id) if prov_album_id.isdigit() else prov_album_id
        songs = await self._run_with_session(self._qq_album.get_song(album_value, num=300, page=1))
        return [self._parse_track(item) for item in songs if item.get("mid")]

    @use_cache(3600 * 24 * 7)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track_value: str | int = int(prov_track_id) if prov_track_id.isdigit() else prov_track_id
        response = await self._run_with_session(self._qq_song.get_detail(track_value))
        track_obj = response.get("track_info")
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
            if isinstance(lyric_response, dict):
                lyric_text = str(lyric_response.get("lyric") or "").strip()
                trans_text = str(lyric_response.get("trans") or "").strip()
            # Fallback to QRC when standard lyric is empty/unavailable.
            if not lyric_text:
                lyric_response = await self._run_with_session(
                    self._qq_lyric.get_lyric(prov_track_id, qrc=True, trans=True)
                )
            if isinstance(lyric_response, dict):
                lyric_text = str(lyric_response.get("lyric") or lyric_text).strip()
                trans_text = str(lyric_response.get("trans") or trans_text).strip()
                if lyric_text:
                    if _LRC_TIMESTAMP_PATTERN.search(lyric_text):
                        track.metadata.lrc_lyrics = _normalize_qq_lyric_text(lyric_text)
                    else:
                        # QRC (e.g. [36438,1880]当(36438,161)...) -> LRC for synced display.
                        qrc_lrc = _qrc_to_lrc(lyric_text)
                        if qrc_lrc:
                            track.metadata.lrc_lyrics = qrc_lrc
                    track.metadata.lyrics = _normalize_qq_lyric_text(lyric_text)
                if trans_text:
                    trans_text = _normalize_qq_lyric_text(trans_text)
                    if track.metadata.lyrics:
                        track.metadata.lyrics = f"{track.metadata.lyrics}\n\n{trans_text}".strip()
                    else:
                        track.metadata.lyrics = trans_text
        except Exception as err:
            self.logger.debug("Failed to load QQ Music lyrics for %s: %s", prov_track_id, err)
        return track

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
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
            if isinstance(response, list):
                artists = [item for item in response if isinstance(item, dict)]
            elif isinstance(response, dict):
                artists = extract_items(
                    response,
                    ("List", "Users", "list", "v_list", "users", "singer_list", "singers"),
                )
            else:
                artists = []
            if not artists:
                break
            for artist_obj in artists:
                try:
                    yield self._parse_artist(artist_obj)
                    total_yielded += 1
                except (InvalidDataError, TypeError, ValueError):
                    continue
            if len(artists) < num:
                break
            page += 1
        self.logger.info("QQ library artists sync yielded %s artist(s)", total_yielded)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
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
            songs = response.get("songlist", [])
            if total is None:
                total = int(response.get("total_song_num") or 0)
            if not songs:
                break
            for song in songs:
                try:
                    yield self._parse_track(song)
                    yielded += 1
                except (InvalidDataError, TypeError, ValueError):
                    continue
            if total and yielded >= total:
                break
            page += 1

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from QQ Music."""
        euin = await self._ensure_user_euin()
        page = 1
        num = 100
        total_yielded = 0
        while True:
            response = await self._run_with_session(
                self._qq_user.get_fav_album(euin, page=page, num=num, credential=self._credential)
            )
            albums = extract_items(
                response,
                (
                    "album_list",
                    "albumList",
                    "v_list",
                    "albums",
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
                except (InvalidDataError, TypeError, ValueError):
                    continue
            if len(albums) < num:
                break
            page += 1
        self.logger.info("QQ library albums sync yielded %s album(s)", total_yielded)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve user playlists from QQ Music."""
        euin = await self._ensure_user_euin()
        created = await self._run_with_session(
            self._qq_user.get_created_songlist(str(self._musicid), credential=self._credential)
        )
        for playlist_obj in created or []:
            try:
                yield self._parse_playlist(playlist_obj)
            except (InvalidDataError, TypeError, ValueError):
                continue

        page = 1
        num = 100
        while True:
            response = await self._run_with_session(
                self._qq_user.get_fav_songlist(
                    euin, page=page, num=num, credential=self._credential
                )
            )
            fav_playlists = extract_items(
                response,
                ("list", "v_list", "playlist", "vec_kept_playlist"),
            )
            if not fav_playlists:
                break
            for playlist_obj in fav_playlists:
                try:
                    yield self._parse_playlist(playlist_obj)
                except (InvalidDataError, TypeError, ValueError):
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
        playlist_obj = response.get("dirinfo") if isinstance(response, dict) else None
        if not isinstance(playlist_obj, dict):
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        # Ensure parsed playlist keeps composite id including dirid.
        playlist_obj = {**playlist_obj, "dissid": dissid, "dirid": dirid}
        return self._parse_playlist(playlist_obj)

    @use_cache(3600)
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
        songs = response.get("songlist", [])
        results: list[Track] = []
        for index, song in enumerate(songs, start=1 + page * 200):
            try:
                track = self._parse_track(song)
                track.position = index
                results.append(track)
            except (InvalidDataError, TypeError, ValueError):
                continue
        return results

    async def recommendations(self) -> list[RecommendationFolder]:  # noqa: PLR0915
        """Get recommendations from QQ Music endpoints."""
        folders: list[RecommendationFolder] = []

        def _resolve_recommend_track(item: dict[str, Any]) -> Track | None:
            """Map recommendation track directly from raw response item."""
            with suppress(InvalidDataError, TypeError, ValueError):
                return self._parse_track(item)
            return None

        def _add_playlist_folder_items(
            folder: RecommendationFolder, playlist_items: list[dict[str, Any]]
        ) -> None:
            """Parse playlist candidates in original API order."""
            for item in playlist_items:
                with suppress(InvalidDataError, TypeError, ValueError):
                    playlist = self._parse_playlist(item)
                    folder.items.append(playlist)

        try:
            guess_response = await self._get_recommend_payload_cached(
                "guess_recommend",
                _RECOMMEND_GUESS_TTL,
                self._qq_recommend.get_guess_recommend,
            )
            self.logger.debug(
                "QQ recommendations guess payload: %s",
                describe_payload(guess_response),
            )
            guess_tracks = extract_guess_recommend_tracks(guess_response)
            guess_folder = RecommendationFolder(
                item_id="guess_recommend",
                provider=self.instance_id,
                name="猜你喜欢",
                icon="mdi-lightbulb-on-outline",
            )
            for item in guess_tracks:
                track = _resolve_recommend_track(item)
                if track:
                    guess_folder.items.append(track)
            if not guess_folder.items:
                radar_response = await self._get_recommend_payload_cached(
                    "guess_recommend_radar",
                    _RECOMMEND_GUESS_TTL,
                    self._qq_recommend.get_radar_recommend,
                )
                self.logger.debug(
                    "QQ recommendations radar payload: %s",
                    describe_payload(radar_response),
                )
                for item in extract_radar_recommend_tracks(radar_response):
                    track = _resolve_recommend_track(item)
                    if track:
                        guess_folder.items.append(track)
            if guess_folder.items:
                self.logger.debug(
                    "QQ recommendations: guess_recommend resolved %s items",
                    len(guess_folder.items),
                )
                folders.append(guess_folder)
        except Exception as err:
            self.logger.warning("QQ recommendations guess_recommend failed: %s", err)

        try:
            new_song_response = await self._get_recommend_payload_cached(
                "new_songs",
                _RECOMMEND_NEWSONG_TTL,
                self._qq_recommend.get_recommend_newsong,
            )
            self.logger.debug(
                "QQ recommendations new_song payload: %s",
                describe_payload(new_song_response),
            )
            new_tracks = extract_newsong_tracks(new_song_response)
            if new_tracks:
                new_song_folder = RecommendationFolder(
                    item_id="new_songs",
                    provider=self.instance_id,
                    name="推荐新歌",
                    icon="mdi-music-note-plus",
                )
                for item in new_tracks:
                    track = _resolve_recommend_track(item)
                    if track:
                        new_song_folder.items.append(track)
                if new_song_folder.items:
                    self.logger.debug(
                        "QQ recommendations: new_songs resolved %s items",
                        len(new_song_folder.items),
                    )
                    folders.append(new_song_folder)
        except Exception as err:
            self.logger.warning("QQ recommendations new_songs failed: %s", err)

        try:
            playlist_response = await self._get_recommend_payload_cached(
                "recommended_playlists",
                _RECOMMEND_PLAYLIST_TTL,
                self._qq_recommend.get_recommend_songlist,
            )
            self.logger.debug(
                "QQ recommendations songlist payload: %s",
                describe_payload(playlist_response),
            )
            playlist_items = extract_recommend_songlists(playlist_response)
            if playlist_items:
                playlist_folder = RecommendationFolder(
                    item_id="recommended_playlists",
                    provider=self.instance_id,
                    name="推荐歌单",
                    icon="mdi-playlist-music",
                )
                _add_playlist_folder_items(playlist_folder, playlist_items)
                if playlist_folder.items:
                    self.logger.debug(
                        "QQ recommendations: recommended_playlists resolved %s items",
                        len(playlist_folder.items),
                    )
                    folders.append(playlist_folder)
        except Exception as err:
            self.logger.warning("QQ recommendations songlist failed: %s", err)

        self.logger.debug("QQ recommendations returned %s folder(s)", len(folders))
        return folders

    async def create_playlist(self, name: str, media_types: set[MediaType]) -> Playlist:
        """Create a new playlist on provider with given name."""
        created = await self._run_with_session(
            self._qq_songlist.create(dirname=name, credential=self._credential)
        )
        if not isinstance(created, dict):
            raise InvalidDataError("QQ Music create playlist returned invalid response")
        dirid_raw = created.get("dirid") or created.get("dirId") or created.get("id")
        if dirid_raw is None:
            raise InvalidDataError("QQ Music create playlist response missing dirid")
        dirid = int(dirid_raw)
        dissid = int(created.get("tid") or created.get("dissid") or dirid)
        return await self.get_playlist(self._build_playlist_id(dissid, dirid))

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        dissid, dirid = self._parse_playlist_id(prov_playlist_id)
        target_dirid = dirid or dissid
        if target_dirid <= 0:
            raise InvalidDataError("QQ Music playlist id is invalid for playlist edit")
        song_ids: list[int] = []
        for track_id in prov_track_ids:
            try:
                song_ids.append(await self._resolve_song_id(track_id))
            except (MediaNotFoundError, InvalidDataError, ResourceTemporarilyUnavailable) as err:
                self.logger.warning("Skipping track %s while adding to playlist: %s", track_id, err)
        if not song_ids:
            raise InvalidDataError("No valid QQ Music tracks to add")
        await self._run_with_session(
            self._qq_songlist.add_songs(
                dirid=target_dirid,
                song_ids=song_ids,
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
        song_ids: list[int] = []
        target_positions = set(positions_to_remove)
        for track in playlist_tracks:
            if track.position not in target_positions:
                continue
            try:
                song_ids.append(await self._resolve_song_id(track.item_id))
            except (MediaNotFoundError, InvalidDataError, ResourceTemporarilyUnavailable) as err:
                self.logger.warning(
                    "Skipping track %s while removing from playlist: %s", track.item_id, err
                )
        if not song_ids:
            return
        await self._run_with_session(
            self._qq_songlist.del_songs(
                dirid=target_dirid,
                song_ids=song_ids,
                credential=self._credential,
            )
        )

    @use_cache(3600 * 24)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        song_id = await self._resolve_song_id(prov_track_id)
        response = await self._run_with_session(self._qq_song.get_similar_song(song_id))
        if not isinstance(response, list):
            return []
        tracks: list[Track] = []
        for item in response:
            if len(tracks) >= limit:
                break
            if not isinstance(item, dict):
                continue
            with suppress(InvalidDataError, TypeError, ValueError):
                tracks.append(self._parse_track(item))
        return tracks

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return streamdetails for given track id."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Unsupported media type {media_type}")
        track_response = await self._run_with_session(self._qq_song.get_detail(item_id))
        track_obj = track_response.get("track_info", {})
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
