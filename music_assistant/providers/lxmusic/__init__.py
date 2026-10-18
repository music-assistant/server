"""LX Music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import secrets
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MusicAssistantError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.models.music_provider import MusicProvider

# The radio playlist reads MA's "filter recently played" helper, which lives in
# an internal module and may be missing across versions, so import it softly.
try:
    from music_assistant.helpers.track_filter import filter_tracks as _ma_filter_tracks
except Exception:
    _ma_filter_tracks = None

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

LOGGER = logging.getLogger(__name__)

DOMAIN = "lxmusic"
CONF_SERVER_URL = "server_url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEFAULT_SOURCE = "default_source"
CONF_SEARCH_SOURCES = "search_sources"
# When disabled, leaderboard playlists are no longer yielded by the library sync.
CONF_IMPORT_LEADERBOARDS = "import_leaderboards"
# When disabled, square/discover playlists are no longer yielded by the sync.
CONF_IMPORT_SQUARE = "import_square_playlists"
# Radio playlist rotates on time buckets instead of randomly on each open.
CONF_RADIO_INTERVAL = "radio_refresh_minutes"
# Per-slot source override for the daily / new-song recommendation rows.
CONF_DAILY_SOURCE = "daily_source"
CONF_NEWSONG_SOURCE = "newsong_source"
# Two-way love-list (hearted songs) sync, enabled by default.
# On: loveList is synced into the MA library as favorites by the built-in
#     library sync, and MA favorite toggles are written back to lxserver.
# Off: loveList is no longer synced; items already in the library are kept.
CONF_SYNC_LOVE_LIST = "sync_love_list"

# In-code display names are English; the zh bundles under translations/
# localize them via config_entries.*.options.* and media.folder.hot_* keys.
SOURCE_NAMES = {
    "kw": "Kuwo",
    "kg": "Kugou",
    "tx": "QQ Music",
    "wy": "NetEase Cloud Music",
    "mg": "Migu Music",
}

QUALITY_ORDER = ["flac", "320k", "128k"]

# --------------------------------------------------------------------------- #
# Recommendations and the dynamic radio playlist.
#
# All data comes from lxserver itself: the Subsonic layer (getDailySongs /
# getSongsByGenre / getAlbumList2) plus the frontend APIs /api/music/leaderboard/list
# and /api/music/songList/list. No ncm-api or third-party dependency.
# --------------------------------------------------------------------------- #
# Subsonic mount point; port 0 means the main server port, path is fixed to
# /rest unless the server overrides it (see CONF_SUBSONIC_PATH).
SUBSONIC_PATH_DEFAULT = "/rest"
SUBSONIC_CLIENT = "lxmusic-ma"
SUBSONIC_VERSION = "1.16.1"
# Subsonic auth: t = md5(password + salt). A random salt keeps plaintext
# passwords out of URL logs.
SUBSONIC_SALT_BYTES = 8

# Cache category. This MA release has no CACHE_CATEGORY_RECOMMENDATIONS, so
# pick an unused id above the ones taken by the metadata side.
CACHE_CATEGORY_RECOMMENDATIONS = 103
# Bump when the cached shape changes so stale entries are dropped.
_RECO_CACHE_VERSION = "v3"

# Cache TTLs per recommendation slot. Daily rows are shuffled per calendar
# day, so their TTL is generous to stay stable within a day.
_RECO_TTL_DAILY = 6 * 60 * 60
_RECO_TTL_NEWSONG = 30 * 60
_RECO_TTL_PLAYLISTS = 60 * 60
_RECO_TTL_RADIO = 5 * 60

# Recommendation item ids, also used as cache key prefixes.
RECO_DAILY = "daily_songs"
RECO_NEW = "recommended_new_songs"
RECO_PLAYLISTS = "recommended_playlists"
RECO_RADIO = "recommended_radios"

# Legacy single-board ids kept for fallback call sites; the main path mixes
# several boards per source (see NEWSONG_BOARDS_BY_SOURCE).
NEWSONG_BANGID_BY_SOURCE = {"wy": "3779629"}
NEWSONG_FALLBACK_SOURCES = ("wy", "tx", "kw", "kg", "mg")

# Cap tracks per album: covers are album art, so same-album rows look
# duplicated in the UI.
DAILY_MAX_PER_ALBUM = 3
DAILY_TARGET = 60
NEWSONG_MAX_PER_ALBUM = 3
NEWSONG_TARGET = 100
# Daily rows mix several official boards of one platform instead of the
# server-side getDailySongs, which only draws from a handful of albums and
# therefore repeats the same cover. Defaults are configurable per platform.
RECO_SOURCE_DEFAULT = "kw"
# Dropdown options, in display order.
RECO_SOURCES = ("kw", "kg", "wy", "mg", "tx")
# Boards mixed per platform. Board names differ across platforms, so they are
# resolved by exact match first and fuzzy match second; a server-side rename
# then degrades one board instead of the whole slot.
DAILY_BOARDS_BY_SOURCE: dict[str, tuple[str, ...]] = {
    # Kuwo has no plain "new/hot" board; use the closest equivalents plus the
    # flagship chart to grow the pool.
    "kg": ("飙升榜", "TOP500", "酷狗音乐人原创榜", "ACG新歌榜", "抖音热歌榜"),
    "wy": ("飙升榜", "新歌榜", "原创榜", "热歌榜"),
    "kw": ("飙升榜", "新歌榜", "热歌榜", "流行趋势榜"),
    "mg": ("新歌榜", "热歌榜", "原创榜", "音乐风向榜"),
    "tx": ("飙升榜", "新歌榜", "热歌榜", "流行指数榜"),
}
# New-song rows only mix "new" oriented boards (new / rising / original),
# never hot or classic charts. Mixing several boards roughly doubles cover
# diversity compared to a single new-song board.
NEWSONG_BOARDS_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "kw": ("新歌榜", "飙升榜", "网红新歌榜", "腾讯音乐人原创榜"),
    "kg": ("飙升榜", "ACG新歌榜", "古风新歌榜", "酷狗音乐人原创榜"),
    "wy": ("新歌榜", "飙升榜", "原创榜", "欧美新歌榜"),
    "mg": ("新歌榜", "原创榜", "音乐风向榜"),
    "tx": ("新歌榜", "飙升榜", "腾讯音乐人原创榜", "综艺新歌榜"),
}
# Fuzzy board keywords used when a platform's configured boards all come back empty.
DAILY_FALLBACK_KEYWORDS = ("热歌", "新歌", "飙升")
NEWSONG_FALLBACK_KEYWORDS = ("新歌", "飙升", "原创")


# Radio candidates come from real leaderboards. The Subsonic getSongsByGenre
# implementation ignores the genre argument and returns the same handful of
# songs, so picking random boards (every platform has dozens) is what actually
# provides variety.
RADIO_BOARD_PICKS = 6
RADIO_SONGS_PER_BOARD = 10
# Item id of the dynamic radio playlist, dispatched through get_playlist*.
RADIO_PLAYLIST_ITEM_ID = "list:fm:radio"
RADIO_NAME = "LX Radio"
# Rotation interval bounds in minutes, exposed as a provider config entry.
RADIO_INTERVAL_DEFAULT = 60
RADIO_INTERVAL_MIN = 5
RADIO_INTERVAL_MAX = 1440

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.LYRICS,
    ProviderFeature.RECOMMENDATIONS,
    # Declaring this makes MA call set_favorite when the user hearts/unhearts a
    # track so the state is written back to the lxserver love list.
    ProviderFeature.FAVORITE_TRACKS_EDIT,
}


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider with given configuration."""
    return LxMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


def _normalize_lx_interval(value: Any) -> str:
    """
    Normalize any interval value into the "MM:SS" text the lyric API wants.

    The source SDKs split interval as a string, so a bare number, a float or
    None either raises or loses precision. Handling it in one place:

    - already "MM:SS" or "MM:SS.xxx" -> returned unchanged
    - a number, or a string of seconds -> formatted as "MM:SS"
    - empty, None or unparsable -> "", which the server SDKs treat as zero

    Search results already use "MM:SS" while MA durations are seconds, so both
    forms can arrive here.
    """
    if value is None or value == "":
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if ":" in s:
        return s
    # bare number (seconds) -> MM:SS
    try:
        total = int(float(s))
    except TypeError, ValueError:
        return s  # let the server deal with it rather than raising
    if total < 0:
        return ""
    m, sec = divmod(total, 60)
    return f"{m:02d}:{sec:02d}"


class LxMusicProvider(MusicProvider):
    """Provide an LX Music server as a music source."""

    _http_session: aiohttp.ClientSession | None = None
    _token: str | None = None

    # One-off cleanup of legacy square playlist names.
    #
    # The name comparison above only runs when a sync yields the same item id,
    # but only the top N playlists per tag are yielded, so stale entries outside
    # that set never get a chance to be renamed. This runs a direct database
    # update instead, which is not limited by the sync scope.
    #
    # The current template is ``LX ·<tag>:<name>``. One regex handles both the
    # historical two-layer and single-layer forms, taking the final tag from
    # group 2 and the remainder from group 3.
    _SONGLIST_NAME_RE = re.compile(r"^(LX )歌单·(?:[^·]+·)?([^·:]+)(:.*)$")

    async def handle_async_init(self) -> None:  # noqa: PLR0915
        """
        Set up the provider.

        The setup flow stores form values in the encrypted ``setup_data`` field
        and leaves ``values`` empty, so every option must be read through
        ``get_setup_value``; ``config.get_value`` would always return None and
        the provider would fail with a misleading login error.
        """
        self._server_url = str(
            self.get_setup_value(CONF_SERVER_URL) or "http://localhost:9527"
        ).rstrip("/")
        self._username = self.get_setup_value(CONF_USERNAME) or "admin"
        self._password = self.get_setup_value(CONF_PASSWORD) or ""
        self._default_source = self.get_setup_value(CONF_DEFAULT_SOURCE) or "wy"
        # Leaderboard import toggle. Defaults to on; existing playlists stay in
        # the database when turned off, they are just no longer re-yielded.
        # _setup_bool handles both the missing-entry default and the "false"
        # string encoding; the previous ``or True`` form was always True.
        self._import_leaderboards = self._setup_bool(CONF_IMPORT_LEADERBOARDS)
        # Square/discover playlist import toggle, independent of the above.
        self._import_square = self._setup_bool(CONF_IMPORT_SQUARE)
        raw_sources = self.get_setup_value(CONF_SEARCH_SOURCES) or "kw,kg,tx,wy,mg"
        self._search_sources = [s.strip() for s in raw_sources.split(",") if s.strip()] or ["wy"]
        # The radio interval is only logged here; the effective value is read
        # per use by _radio_interval_seconds(), so config changes apply at once.
        LOGGER.info("lxmusic: radio rotation interval %d minutes", self._radio_interval_minutes())
        # Must stay after _search_sources is set: this line references it, and
        # logging it earlier would raise AttributeError during init.
        LOGGER.info(
            "lxmusic: init server=%s user=%s sources=%s default=%s",
            self._server_url,
            self._username,
            self._search_sources,
            self._default_source,
        )
        # Parsed tracks and their raw source items, reused by get_track / get_stream_details.
        self._track_cache: dict[str, Track] = {}
        self._raw_cache: dict[str, dict[str, Any]] = {}
        # Lyric cache keyed by provider track id. Not persisted; a None value
        # records "fetched, no lyrics" so the server is not hit again.
        self._lyrics_cache: dict[str, str | None] = {}
        # Artist / album / playlist metadata used to look details back up by id.
        self._artist_cache: dict[str, dict[str, Any]] = {}
        self._album_cache: dict[str, dict[str, Any]] = {}
        self._playlist_cache: dict[str, list[dict[str, Any]]] = {}
        # Square/discover playlist metadata, used to fill in names and covers.
        self._square_meta: dict[str, dict[str, Any]] = {}
        # Square tag cache (raw tags, hot tags). The server does not filter
        # /songList/list by tag, so expanding parent tags would duplicate
        # results; both the top-level browse folders and the sub-tag decision
        # are derived from this single cache.
        self._square_tags_cache: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None
        # Tracks already parsed during search/artist pages, keyed by album id,
        # so opening an album does not depend on a follow-up lookup.
        self._album_tracks: dict[str, list[Track]] = {}
        self._user_lists_cache: dict[str, Any] | None = None
        # The user list snapshot needs a TTL: without one, playlists created,
        # renamed or deleted on the lxserver side never show up in MA. 60s is
        # short enough to catch changes without hitting the server on every call.
        self._user_lists_cache_time: float = 0.0
        self._user_lists_cache_lock = asyncio.Lock()
        self._USER_LISTS_CACHE_TTL: float = 60.0
        # Cover backfill cache for user playlists, keyed by (source, name, singer),
        # so reopening a playlist does not re-search every track.
        self._pic_enrich_cache: dict[tuple[str, str, str], str | None] = {}
        # Cross-platform songmid resolution cache, same key, avoids repeat searches.
        self._songmid_resolve_cache: dict[tuple[str, str, str], str | None] = {}
        # Metadata for virtual playlists (leaderboards + square picks), filled
        # while yielding the library so get_playlist does not call the server again.
        self._virtual_meta: dict[str, dict[str, Any]] = {}
        # Square playlists per tag, keeps the total playlist count sane.
        self._LEADERBOARD_TOPN: int = 5
        # Board name -> bangid. Ids are handed out by the server and rarely
        # change, so one lookup per process is enough.
        self._board_id_cache: dict[str, str | None] = {}
        # Radio rotates on time buckets: bucket = now / interval. Within one
        # bucket the pool and shuffle order are stable, across buckets they change.
        # Only the last bucket number is kept, for logging.
        self._radio_bucket: int = -1

        # Fail early with a clear message: the server answers an empty username
        # or password with a bare "400 Missing username or password", which gives
        # no hint that a provider option is missing.
        if not str(self._username).strip():
            raise LoginFailed(
                "Username is empty. Set the lxserver username in the provider settings.",
                translation_key="empty_username",
                translation_owner=self.translation_owner,
            )
        if not str(self._password):
            raise LoginFailed(
                "Password is empty. Set the lxserver password in the "
                "provider settings (reset it in the lxserver admin panel "
                "if forgotten).",
                translation_key="empty_password",
                translation_owner=self.translation_owner,
            )

        await self._login()

        # Force a sync shortly after startup. The default provider sync runs
        # every 12 hours, so config or naming changes would otherwise stay
        # invisible in the UI for a long time.
        #
        # start_sync is used instead of schedule_provider_sync: scheduling alone
        # can be a no-op this early in startup, while start_sync schedules and
        # then runs the task immediately.
        #
        # Swallow errors: a failed sync must not break handle_async_init, which
        # would hide the provider from the UI entirely.
        try:

            async def _delayed_sync() -> None:
                # Give MA a moment to finish starting before syncing. Tracks are
                # included so the love list lands in MA favorites right away
                # instead of waiting for the 12-hour library sync cycle.
                await asyncio.sleep(10)
                try:
                    await self.mass.music.start_sync(
                        media_types=[MediaType.PLAYLIST, MediaType.TRACK],
                        providers=[self.instance_id],
                    )
                except Exception as err:
                    LOGGER.warning("lxmusic: forced post-startup sync failed: %s", err)

            self.mass.create_task(_delayed_sync())
        except Exception as err:
            LOGGER.warning("lxmusic: failed to schedule post-startup sync: %s", err)

        # The one-off cleanup of legacy two-layer playlist names is done and is
        # not scheduled again: it would only scan the database for nothing.

        # Warm the leaderboard metadata in the background after startup. Filling
        # ~160 boards takes about 25s; without this the first browse click waits
        # for it and looks like a hang.
        # Swallow errors: a failed prefetch must not break handle_async_init.
        try:

            async def _prefetch_leaderboards() -> None:
                # Short delay so the prefetch does not compete with other work
                # scheduled during startup.
                await asyncio.sleep(3)
                if not getattr(self, "_import_leaderboards", True):
                    LOGGER.debug("lxmusic: leaderboard toggle off, skipping prefetch")
                    return
                try:
                    await self._fill_leaderboards([])
                    LOGGER.info("lxmusic: leaderboard prefetch finished")
                except Exception as err:
                    LOGGER.warning("lxmusic: leaderboard prefetch failed: %s", err)

            self.mass.create_task(_prefetch_leaderboards())
        except Exception as err:
            LOGGER.warning("lxmusic: failed to schedule leaderboard prefetch: %s", err)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return config entries to set up this provider.

        ``default_value`` must be read back through ``get_setup_value``: the
        setup flow stores submitted values in ``setup_data`` and leaves
        ``values`` empty, so a literal default would both mislead the user and
        silently overwrite the real value when the form is saved again.
        """
        return (
            ConfigEntry(
                key=CONF_SERVER_URL,
                type=ConfigEntryType.STRING,
                default_value=str(
                    self.get_setup_value(CONF_SERVER_URL) or "http://localhost:9527"
                ).rstrip("/"),
                required=True,
            ),
            ConfigEntry(
                key=CONF_USERNAME,
                type=ConfigEntryType.STRING,
                default_value=str(self.get_setup_value(CONF_USERNAME) or "admin"),
                required=True,
            ),
            ConfigEntry(
                key=CONF_PASSWORD,
                type=ConfigEntryType.SECURE_STRING,
                required=True,
                # SECURE_STRING is echoed masked; fall back to empty so the user
                # re-enters it when the stored value is unavailable.
                default_value=str(self.get_setup_value(CONF_PASSWORD) or ""),
            ),
            ConfigEntry(
                key=CONF_DEFAULT_SOURCE,
                type=ConfigEntryType.STRING,
                default_value=str(self.get_setup_value(CONF_DEFAULT_SOURCE) or "wy"),
                required=False,
            ),
            ConfigEntry(
                key=CONF_SEARCH_SOURCES,
                type=ConfigEntryType.STRING,
                default_value=str(self.get_setup_value(CONF_SEARCH_SOURCES) or "kw,kg,tx,wy,mg"),
                required=False,
            ),
            # Two-way love-list sync toggle: off keeps playlist sync only.
            ConfigEntry(
                key=CONF_SYNC_LOVE_LIST,
                type=ConfigEntryType.BOOLEAN,
                default_value=self._sync_love_list_enabled(),
                required=False,
            ),
            # Leaderboards and square playlists live in the browse view only, not in
            # library playlists; these toggles show or hide those browse entries.
            ConfigEntry(
                key=CONF_IMPORT_LEADERBOARDS,
                type=ConfigEntryType.BOOLEAN,
                default_value=self._setup_bool(CONF_IMPORT_LEADERBOARDS),
                required=False,
            ),
            ConfigEntry(
                key=CONF_IMPORT_SQUARE,
                type=ConfigEntryType.BOOLEAN,
                default_value=self._setup_bool(CONF_IMPORT_SQUARE),
                required=False,
            ),
            # Per-slot recommendation sources. Read per use, so changes apply at once.
            ConfigEntry(
                key=CONF_DAILY_SOURCE,
                type=ConfigEntryType.STRING,
                default_value=str(self.get_setup_value(CONF_DAILY_SOURCE) or RECO_SOURCE_DEFAULT),
                options=[ConfigValueOption(value=src) for src in RECO_SOURCES],
                required=False,
            ),
            ConfigEntry(
                key=CONF_NEWSONG_SOURCE,
                type=ConfigEntryType.STRING,
                default_value=str(self.get_setup_value(CONF_NEWSONG_SOURCE) or RECO_SOURCE_DEFAULT),
                options=[ConfigValueOption(value=src) for src in RECO_SOURCES],
                required=False,
            ),
            # Radio rotation interval. Read per use, so changes apply without a restart.
            ConfigEntry(
                key=CONF_RADIO_INTERVAL,
                type=ConfigEntryType.INTEGER,
                default_value=int(
                    self.get_setup_value(CONF_RADIO_INTERVAL) or RADIO_INTERVAL_DEFAULT
                ),
                range=(RADIO_INTERVAL_MIN, RADIO_INTERVAL_MAX),
                required=False,
                translation_params=[
                    str(RADIO_INTERVAL_DEFAULT),
                    str(RADIO_INTERVAL_MIN),
                    str(RADIO_INTERVAL_MAX),
                ],
            ),
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True as we provide streaming media."""
        return True

    async def test_connection(self) -> bool:
        """Test the connection by logging in; /api/status requires auth and would 401."""
        try:
            session = self._session()
            async with session.post(
                f"{self._server_url}/api/user/login",
                json={"username": self._username, "password": self._password},
            ) as resp:
                return resp.status == 200
        except Exception as err:
            LOGGER.warning("LX Music connection test failed: %s", err)
        return False

    async def search(  # noqa: PLR0915
        self,
        search_query: Any,
        media_types: list[MediaType],
        limit: int = 25,
    ) -> SearchResults:
        """
        Search the LX Music server across configured sources.

        lxserver only exposes a song-name search, so artist and album results
        are aggregated from the tracks found.
        """
        results = SearchResults()
        keyword = search_query if isinstance(search_query, str) else search_query.search_term
        want_track = MediaType.TRACK in media_types
        want_artist = MediaType.ARTIST in media_types
        want_album = MediaType.ALBUM in media_types
        want_playlist = MediaType.PLAYLIST in media_types
        if not (want_track or want_artist or want_album or want_playlist):
            return results

        seen_tracks: set[str] = set()
        seen_artists: set[str] = set()
        seen_albums: set[str] = set()
        per_source = max(5, limit // max(1, len(self._search_sources)))

        for source in self._search_sources:
            items = await self._search_source(source, keyword, page_size=per_source)
            for item in items:
                # tracks
                if want_track:
                    song_id = self._item_song_id(item)
                    if song_id:
                        uid = f"{source}:{song_id}"
                        if uid not in seen_tracks:
                            seen_tracks.add(uid)
                            track = await self._parse_track(item, source)
                            if track:
                                results.tracks.append(track)
                # artists: singer may be a string or a list; keep the real id when present
                if want_artist:
                    singer_names, real_artist_id = self._singer_info(item)
                    for artist_name in singer_names:
                        aid = self._artist_item_id(source, artist_name)
                        if aid in seen_artists:
                            continue
                        seen_artists.add(aid)
                        # prefer the id seen in this search, else the cached one
                        rid = real_artist_id or self._artist_cache.get(aid, {}).get("real_id")
                        results.artists.append(self._register_artist(source, artist_name, rid))
                # albums: prefer the real album id so tracks can be fetched later
                if want_album:
                    album_id = self._album_id(item)
                    album_name = item.get("albumName") or item.get("album") or "Unknown Album"
                    aid = (
                        f"{source}:{album_id}"
                        if album_id
                        else self._artist_item_id(source, album_name)
                    )
                    if aid not in seen_albums:
                        seen_albums.add(aid)
                        results.albums.append(self._register_album(source, album_name, album_id))
            # stop once every requested type hit the limit
            if (
                (not want_track or len(results.tracks) >= limit)
                and (not want_artist or len(results.artists) >= limit)
                and (not want_album or len(results.albums) >= limit)
            ):
                break

        # Playlists: search square/discover lists first, then match the user's own lists by name
        if want_playlist:
            seen_playlists: set[str] = set()
            for pl in await self._search_song_lists(keyword, limit=limit):
                if pl.item_id not in seen_playlists:
                    seen_playlists.add(pl.item_id)
                    results.playlists.append(pl)
            data = await self._get_user_lists()
            if data:
                kw = keyword.lower()
                for pid, pname, songs in self._iter_user_playlists(data):
                    # match the playlist name, or any track name/artist inside it
                    hit = bool(kw) and kw in (pname or "").lower()
                    if not hit:
                        for s in songs:
                            s_names, _ = self._singer_info(s)
                            s_text = (
                                (s.get("name") or s.get("songName") or "")
                                + "、"
                                + "、".join(s_names)
                            ).lower()
                            if kw and kw in s_text:
                                hit = True
                                break
                    if hit:
                        item_id = f"list:{pid}"
                        self._playlist_cache[item_id] = songs
                        results.playlists.append(
                            Playlist(
                                item_id=item_id,
                                provider=self.instance_id,
                                name=pname,
                                translation_key=self._virtual_translation_key(pid),
                                provider_mappings={
                                    ProviderMapping(
                                        item_id=item_id,
                                        provider_domain=self.domain,
                                        provider_instance=self.instance_id,
                                    )
                                },
                            )
                        )
        return results

    async def browse(self, path: str | None = None) -> Sequence[BrowseFolder | ItemMapping | Any]:
        """
        Browse the LX Music provider.

        Virtual playlists (leaderboards, square, defaults) are exposed here only,
        not under the library playlists.

        MA 2.x BrowseFolder has no ``items`` field, so browse() must return a
        flat sequence of sibling nodes; MA re-enters browse() with each folder's
        item_id as the next path.
        """
        return await self._browse_impl(path)

    # ------------------------------------------------------------------ #
    # Media item getters
    # ------------------------------------------------------------------ #
    async def get_track(self, prov_track_id: str) -> Track:
        """Get a single track."""
        cached = getattr(self, "_track_cache", {}).get(prov_track_id)
        if cached is not None:
            # The cache is filled by _parse_track on hot paths, which does not
            # fetch lyrics; do it here. The lyric cache makes this a no-op when
            # they were already fetched.
            await self._maybe_fetch_lyrics(cached)
            return cached
        source, song_id = self._split_id(prov_track_id)
        items = await self._search_source(source, song_id, page_size=5)
        for item in items:
            if self._item_song_id(item) == song_id:
                track = await self._parse_track(item, source)
                if track:
                    # Attach lyrics so the MA lyric controller hits its first
                    # lookup path. Only done here, not in _parse_track, to keep
                    # search and listing paths free of extra requests.
                    await self._maybe_fetch_lyrics(track, item)
                    return track
        # A songmid is not a usable search keyword on most sources, so on a cold
        # cache fall back to scanning the user's own lists (love list, favorites,
        # custom playlists). Cached with a TTL, at most one request.
        raw = getattr(self, "_raw_cache", {}).get(prov_track_id)
        if isinstance(raw, dict):
            track = await self._parse_track(raw, source)
            if track:
                await self._maybe_fetch_lyrics(track, raw)
                return track
        data = await self._get_user_lists()
        if isinstance(data, dict):
            for _pl_id, _name, songs in self._iter_user_playlists(data):
                for item in songs:
                    if (
                        isinstance(item, dict)
                        and str(item.get("source") or source) == source
                        and self._item_song_id(item) == song_id
                    ):
                        track = await self._parse_track(item, source)
                        if track:
                            await self._maybe_fetch_lyrics(track, item)
                            return track
        raise FileNotFoundError(f"Track not found: {prov_track_id}")

    async def get_album(self, prov_album_id: str) -> Album:
        """
        Album metadata.

        The Album model in this MA version has no tracks field; tracks are
        returned separately by get_album_tracks().
        """
        info = self._album_cache.get(prov_album_id)
        album_name = info["name"] if info else (self._split_id(prov_album_id)[1] or "Unknown Album")
        return Album(
            item_id=prov_album_id,
            provider=self.instance_id,
            name=album_name or "Unknown Album",
            provider_mappings={
                ProviderMapping(
                    item_id=prov_album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Album tracks: real albumId first, then cache, then a search by album name."""
        info = self._album_cache.get(prov_album_id)
        source = info["source"] if info else self._split_id(prov_album_id)[0]
        real_id = info.get("real_id") if info else None
        album_name = info["name"] if info else (self._split_id(prov_album_id)[1] or "Unknown Album")
        # 1) fetch the full album by real albumId
        if real_id:
            try:
                items = await self._fetch_paged(
                    "/api/music/albumSongs",
                    {"source": source, "id": real_id},
                    max_items=100,
                )
                out: list[Track] = []
                for item in items:
                    track = await self._parse_track(item, source)
                    if track:
                        out.append(track)
                if out:
                    return out
            except Exception as err:
                LOGGER.debug("albumSongs failed %s: %s", prov_album_id, err)
        # 2) fall back to tracks already parsed for this album, which needs no albumId
        cached = self._album_tracks.get(prov_album_id)
        if cached:
            seen_tracks: set[str] = set()
            out = []
            for track in cached:
                if track.item_id not in seen_tracks:
                    seen_tracks.add(track.item_id)
                    out.append(track)
            if out:
                return out
        # 3) last resort: search by album name and keep matching albums only
        out = []
        if album_name and album_name != "Unknown Album":
            try:
                for src in self._search_sources:
                    items = await self._search_source(src, album_name, page_size=30)
                    for item in items:
                        an = item.get("albumName") or item.get("album") or ""
                        if an and an == album_name:
                            track = await self._parse_track(item, src)
                            if track:
                                out.append(track)
                    if len(out) >= 20:
                        break
            except Exception as err:
                LOGGER.debug("album re-search failed %s: %s", prov_album_id, err)
        return out

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Artist metadata: cached name if known, else derived from the id."""
        info = self._artist_cache.get(prov_artist_id)
        if info:
            return self._make_artist(info["source"], info["name"], prov_artist_id)
        _, name = self._split_id(prov_artist_id)
        return Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """
        Playlist metadata for user lists (list:<id>) and square lists (sl:<source>:<id>).

        The Playlist model in this MA version has no tracks field; tracks are
        returned separately by get_playlist_tracks().

        Virtual playlists are also supported:
        - list:board:<src>:<bangid>    -> leaderboard
        - list:songlist:<src>:<sl_id>  -> square pick
        - list:fm:radio                -> the rotating radio playlist
        """
        # Radio: a dynamic playlist whose metadata comes from the current pool
        if prov_playlist_id == RADIO_PLAYLIST_ITEM_ID:
            return await self._build_radio_playlist()
        # Virtual playlists (leaderboards / square picks): reuse the metadata cached
        if prov_playlist_id.startswith(("list:board:", "list:songlist:")):
            meta = self._virtual_meta.get(prov_playlist_id)
            if not meta and prov_playlist_id.startswith("list:songlist:"):
                # Without metadata the name falls back to the raw item id, which
                # shows up in the UI as something like
                # "list:songlist:kw:digest-8__3689581612". Fetch it on demand:
                # songList/detail returns name/img/author in its info field.
                parts = prov_playlist_id.split(":", 3)
                meta = await self._songlist_meta(
                    parts[2] if len(parts) > 2 else "",
                    parts[3] if len(parts) > 3 else "",
                )
            playlist = Playlist(
                item_id=prov_playlist_id,
                provider=self.instance_id,
                name=(meta or {}).get("name") or prov_playlist_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_playlist_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            # Leaderboards store their cover under "pic" and square playlists
            # under "img", so accept both keys here.
            img = (meta or {}).get("pic") or (meta or {}).get("img")
            if img:
                playlist.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=img,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            return playlist
        if prov_playlist_id.startswith("sl:"):
            parts = prov_playlist_id.split(":", 2)
            source = parts[1] if len(parts) > 1 else self._default_source
            sl_id = parts[2] if len(parts) > 2 else ""
            return self._make_square_playlist(source, sl_id)

        pid = prov_playlist_id.removeprefix("list:")
        data = await self._get_user_lists()
        name = pid
        songs: list[dict[str, Any]] = []
        if data:
            for pl_id, pl_name, pl_songs in self._iter_user_playlists(data):
                if pl_id == pid:
                    name = pl_name
                    songs = pl_songs
                    break
        # Cache the raw song list so get_playlist_tracks can reuse it
        self._playlist_cache[prov_playlist_id] = songs
        return Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=name,
            translation_key=self._virtual_translation_key(pid),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """
        Return playlist tracks, paginated as the MA protocol requires.

        MA calls this with page=0,1,2,... until a page comes back empty, so the
        result must be sliced by page or the loop never terminates and the UI
        ends up with no tracks at all.

        Virtual playlist ids are dispatched here:
        - list:board:<src>:<bangid>    -> leaderboard
        - list:songlist:<src>:<sl_id>  -> square pick
        - list:fm:radio                -> radio, page 0 only
        - anything else                -> the user's own playlists
        """
        page_size = 100
        start = page * page_size
        # The radio playlist is dynamic: MA only reads one batch from it, so
        # content is returned on page 0 and later pages must be empty or the
        # pagination loop would not stop.
        if prov_playlist_id == RADIO_PLAYLIST_ITEM_ID:
            if page > 0:
                return []
            return await self._get_radio_tracks()
        # Leaderboard virtual playlist
        if prov_playlist_id.startswith("list:board:"):
            parts = prov_playlist_id.split(":", 3)
            source = parts[2] if len(parts) > 2 else ""
            bangid = parts[3] if len(parts) > 3 else ""
            return await self._get_leaderboard_tracks(source, bangid, page)
        # Square pick virtual playlist id
        if prov_playlist_id.startswith("list:songlist:"):
            parts = prov_playlist_id.split(":", 3)
            source = parts[2] if len(parts) > 2 else self._default_source
            sl_id = parts[3] if len(parts) > 3 else ""
            return await self._get_songlist_tracks(source, sl_id, page)
        if prov_playlist_id.startswith("sl:"):
            parts = prov_playlist_id.split(":", 2)
            source = parts[1] if len(parts) > 1 else self._default_source
            sl_id = parts[2] if len(parts) > 2 else ""
            try:
                items = await self._fetch_paged(
                    "/api/music/songList/detail",
                    {"source": source, "id": sl_id},
                    max_items=1000,
                )
            except Exception as err:
                LOGGER.debug("square playlist detail failed %s: %s", prov_playlist_id, err)
                return []
            out: list[Track] = []
            for item in items[start : start + page_size]:
                track = await self._parse_track(item, source)
                if track:
                    out.append(track)
            return out

        # User playlist: prefer the cached song list
        songs = self._playlist_cache.get(prov_playlist_id)
        if songs is None:
            await self.get_playlist(prov_playlist_id)
            songs = self._playlist_cache.get(prov_playlist_id, [])
        # The user list endpoint returns no cover field, so tracks from user
        # playlists would show without artwork; enrich the first page by
        # re-searching those tracks concurrently.
        page_songs = songs[start : start + page_size]
        if page == 0 and page_songs:
            await self._enrich_playlist_pics(page_songs)
        out = []
        for item in page_songs:
            src = item.get("source") or self._default_source
            track = await self._parse_track(item, src)
            if track:
                out.append(track)
        return out

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Return the recommendation slots.

        MA applies a 5 second timeout to this call, so no network request is
        made here at all: only static descriptions are returned. The actual
        fetching happens in get_recommendation_items, which has a 30 second
        budget.
        """
        # Names/subtitles are English here; the per-source translation keys
        # (daily_<src> / new_<src>) carry the localized wording instead of
        # interpolating an English platform name into a Chinese sentence.
        daily_src = self._daily_source()
        new_src = self._newsong_source()
        return [
            RecommendationFolder(
                item_id=RECO_RADIO,
                provider=self.instance_id,
                name=RADIO_NAME,
                icon="mdi:radio",
                subtitle=(
                    "Randomly samples multiple charts, switches to a new "
                    f"batch every {self._radio_interval_minutes()} minutes"
                ),
                translation_key="radio",
                translation_params=[str(self._radio_interval_minutes())],
            ),
            RecommendationFolder(
                item_id=RECO_DAILY,
                provider=self.instance_id,
                name="Daily Picks",
                icon="mdi:star",
                subtitle=(
                    f"Mixed from {SOURCE_NAMES.get(daily_src, daily_src)} "
                    "official charts, refreshed daily"
                ),
                translation_key=f"daily_{daily_src}",
            ),
            RecommendationFolder(
                item_id=RECO_NEW,
                provider=self.instance_id,
                name="New Song Picks",
                icon="mdi:music-note",
                subtitle=(f"Mixed from {SOURCE_NAMES.get(new_src, new_src)} new-song charts"),
                translation_key=f"new_{new_src}",
            ),
            RecommendationFolder(
                item_id=RECO_PLAYLISTS,
                provider=self.instance_id,
                name="Playlist Picks",
                icon="mdi:playlist-music",
                subtitle="Curated square playlists from lxserver",
                translation_key="picks",
            ),
        ]

    async def get_recommendation_items(self, item_id: str) -> UniqueList[Any]:
        """
        Fetch the content of one recommendation slot, making requests here.

        MA caps this at 30 seconds and degrades errors to an empty list, but
        catching here as well keeps the real cause in the logs instead of a
        single generic failure message.
        """
        items: UniqueList[Any] = UniqueList()
        rows: list[dict[str, Any]] = []
        try:
            if item_id == RECO_RADIO:
                playlist = await self._build_radio_playlist()
                if playlist:
                    items.append(playlist)
                return items
            if item_id == RECO_DAILY:
                rows = await self._reco_daily_rows()
            elif item_id == RECO_NEW:
                rows = await self._reco_new_rows()
            elif item_id == RECO_PLAYLISTS:
                rows = await self._reco_playlist_rows()
            else:
                return items
        except Exception as err:
            LOGGER.debug("lxmusic: recommendation slot %s fetch failed: %s", item_id, err)
            return items

        if item_id == RECO_PLAYLISTS:
            for row in rows:
                items.append(
                    self._make_virtual_playlist(
                        f"list:songlist:{row.get('source')}:{row.get('id')}",
                        str(row.get("name") or "Playlist"),
                        row.get("img"),
                        subtitle=(f"by {row['author']}" if row.get("author") else None),
                    )
                )
            return items

        for row in rows:
            source = str(row.get("source") or self._default_source)
            try:
                track = await self._parse_track(row, source)
            except Exception as err:
                LOGGER.debug("lxmusic: recommendation slot %s track parse failed: %s", item_id, err)
                continue
            if track:
                items.append(track)
        return items

    async def get_stream_details(  # noqa: PLR0915
        self, item_id: str, media_type: MediaType = MediaType.TRACK
    ) -> StreamDetails | None:
        """
        Get stream details (playback URL) for a track.

        /api/music/url needs a songInfo object with at least source and
        songmid, and quality is one of flac/320k/128k. A cached raw item is
        passed whole when available, which matches more often.

        The server only falls back between APIs within one platform, it does
        not search other platforms for the song. Cross-platform re-search by
        name is deliberately not done here: same-name hits are often covers,
        remixes or other versions, which plays the wrong recording.

        So this method only:
        1) loops qualities on the original source and lets the server try its
           own custom APIs
        2) health-checks the returned URL, since a broken gateway can still
           answer 200 with an unusable body
        3) gives up when no healthy URL is available, which means the user has
           to enable another working custom source in the lxserver web UI
        """
        source, song_id = self._split_id(item_id)
        raw = getattr(self, "_raw_cache", {}).get(item_id)
        # Loop qualities on the original source only. Cross-platform fallback is
        # off because same-name matches play the wrong recording.
        sources_to_try = [source]
        tried: set[str] = set()
        last_err: str | None = None

        # Pull name/singer/interval out of the raw item for logging only
        if raw:
            item_name = (raw.get("name") or raw.get("songName") or "").strip()
            singer_raw = raw.get("singer") or raw.get("singerName") or ""
            if isinstance(singer_raw, list):
                singer_name = ""
                if singer_raw and isinstance(singer_raw[0], dict):
                    singer_name = singer_raw[0].get("name", "")
                elif singer_raw:
                    singer_name = str(singer_raw[0])
                singer_name = singer_name or singer_raw[0].get("name", "") if singer_raw else ""
            elif isinstance(singer_raw, str):
                singer_name = singer_raw.split("/")[0].split("、")[0].strip()
            else:
                singer_name = ""
            item_interval = raw.get("interval") or raw.get("duration") or raw.get("time") or ""
        else:
            item_name = ""
            singer_name = ""
            item_interval = ""

        LOGGER.debug(
            "lxmusic: fetching playback url item_id=%s source=%s song_id=%s name=%r singer=%r interval=%r sources_to_try=%s",
            item_id,
            source,
            song_id,
            item_name,
            singer_name,
            item_interval,
            sources_to_try,
        )

        for src in sources_to_try:
            if not src or src in tried:
                continue
            tried.add(src)

            # cross-platform fallback is disabled, original source only
            actual_songmid: str | None = None
            if raw:
                actual_songmid = raw.get("songmid") or raw.get("id") or raw.get("songId") or song_id
            else:
                actual_songmid = song_id

            # Prefer the cached full item as songInfo, else a minimal one.
            # Force the source to the one being tried so the server targets the
            # right platform, and set songmid explicitly rather than defaulting.
            if raw:
                song_info: dict[str, Any] = dict(raw)
                song_info["source"] = src
                song_info.setdefault("songmid", actual_songmid or song_id)
            else:
                song_info = {
                    "source": src,
                    "songmid": actual_songmid,
                    "name": item_name,
                    "singer": singer_name,
                }
            LOGGER.debug(
                "lxmusic: trying source src=%s songmid=%s (item_id=%s, songInfo.source=%s)",
                src,
                actual_songmid,
                item_id,
                song_info.get("source"),
            )
            # The server reads either a nested songInfo or top-level
            # source/songmid, depending on the caller, so send both. That is the
            # shape the web player posts, and it lets the server match custom
            # sources.
            for quality in QUALITY_ORDER:
                payload: dict[str, Any] = {
                    "songInfo": song_info,
                    "quality": quality,
                    "source": src,
                    "songmid": actual_songmid,
                    "musicId": actual_songmid,
                }
                try:
                    LOGGER.debug(
                        "lxmusic: requesting playback url source=%s quality=%s songInfo=%s",
                        src,
                        quality,
                        {
                            k: v
                            for k, v in song_info.items()
                            if k in ("source", "songmid", "name", "singer")
                        },
                    )
                    # Private custom sources are only considered when the request
                    # carries a valid x-user-name header that also passes the
                    # x-user-token check; otherwise only public sources match and
                    # the server reports no custom source for this platform.
                    # The username comes from the header, not the body.
                    result = await self._request(
                        "POST",
                        "/api/music/url",
                        data=payload,
                        timeout=20,
                        extra_headers={"x-user-name": self._username},
                    )
                    LOGGER.debug(
                        "lxmusic: playback url raw response source=%s quality=%s result=%s",
                        src,
                        quality,
                        result,
                    )
                    url = self._extract_url(result)
                    if url:
                        # Health check: a blocked gateway answers 200 with an
                        # empty body, which is not playable.
                        if not await self._check_url_playable(url):
                            last_err = (
                                f"{src}/{quality}: url not playable (dead gateway?) {url[:60]}"
                            )
                            LOGGER.warning("lxmusic: %s", last_err)
                            continue
                        # Determine content_type from the server response rather
                        # than hardcoding MP3: with the server set to highest
                        # quality it may return flac, and labeling those bytes
                        # audio/mpeg makes clients decode flac with an mp3
                        # decoder, which sounds distorted. Match the type field
                        # first, then fall back to the URL suffix.
                        ct = self._pick_content_type(result, url, quality)
                        LOGGER.debug(
                            "lxmusic: playback url ok %s -> %s (quality=%s content_type=%s)",
                            item_id,
                            url[:120],
                            quality,
                            ct,
                        )
                        # allow_seek must be set, otherwise the MA stream
                        # controller resets a requested seek position to 0 and
                        # the progress bar appears to do nothing.
                        #
                        # can_seek only says MA may seek within the byte stream;
                        # allow_seek is the provider asserting the URL supports
                        # range requests, as tidal and the local provider do.
                        #
                        # Risk: a few custom sources may not support ranges, but
                        # the amcfy bridge already sniffs magic bytes and guards
                        # such hosts, and the MA web UI plays with native ranges,
                        # so this does not change bridge behaviour.
                        return StreamDetails(
                            item_id=item_id,
                            provider=self.instance_id,
                            audio_format=AudioFormat(content_type=ct),
                            stream_type=StreamType.HTTP,
                            path=url,
                            can_seek=True,
                            allow_seek=True,
                        )
                    # Response arrived but carried no usable url; log it for triage
                    LOGGER.debug(
                        "lxmusic: no url extracted from response source=%s quality=%s result=%s",
                        src,
                        quality,
                        result,
                    )
                except Exception as err:
                    last_err = f"{src}/{quality}: {err}"
                    LOGGER.warning("lxmusic: playback url attempt failed %s", last_err)
                    continue

        LOGGER.error(
            "lxmusic: no playback url for %s; tried sources %s, last error: %s. "
            "Check that 1) a custom source for that platform is enabled in lxserver "
            "and 2) the track plays in the lxserver web player.",
            item_id,
            list(tried),
            last_err or "(none)",
        )
        return None

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Return similar tracks using same-source search."""
        source, song_id = self._split_id(prov_track_id)
        items = await self._search_source(source, song_id, page_size=limit)
        out: list[Track] = []
        for item in items:
            track = await self._parse_track(item, source)
            if track and track.item_id != prov_track_id:
                out.append(track)
        return out

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """
        Yield the lxserver love list as the source of MA library favorites.

        This is the LX -> MA direction: the built-in library sync task consumes
        this generator, adds each track to the library and marks it favorite.
        The framework writes that flag straight to the database without calling
        this provider's set_favorite, so there is no feedback loop.

        When a song is unhearted on the lxserver side it disappears from this
        generator, and the framework's deletion branch then drops it from MA
        favorites, which relies on the library_sync_deletions core setting.

        The `if False: yield` below keeps this an async generator on purpose:
        the framework consumes it with `async for`, and a plain coroutine
        returning a list would fail.
        """
        if False:
            yield  # type: ignore[misc]
        if not self._sync_love_list_enabled():
            LOGGER.debug("lxmusic: love-list sync disabled, yielding nothing")
            # Yielding nothing while library_sync_deletions is on (the core
            # default) would drop every LX favorite from the library, which
            # contradicts the documented "items already in the library stay".
            # item_id=None holds back this run's deletion pass for tracks.
            self.report_skipped_sync_item(
                MediaType.TRACK,
                None,
                MusicAssistantError(
                    "LX heart sync is turned off; this sync round was skipped.",
                    translation_key="love_sync_disabled",
                    translation_owner=self.translation_owner,
                ),
            )
            return
        # Library sync is infrequent, so bypass the TTL and fetch the current
        # love list to reflect hearts added or removed just before this run.
        self._user_lists_cache_time = 0.0
        data = await self._get_user_lists()
        if not isinstance(data, dict):
            LOGGER.warning("lxmusic: love-list sync could not read /api/user/list, skipping")
            # Same guard as above: a transient fetch failure must not read as
            # "the love list is empty" and mass-unfavorite everything.
            self.report_skipped_sync_item(
                MediaType.TRACK,
                None,
                MusicAssistantError(
                    "Failed to fetch the hearted playlist; this sync round was skipped.",
                    translation_key="love_list_fetch_failed",
                    translation_owner=self.translation_owner,
                ),
            )
            return
        love_items: list[dict[str, Any]] = []
        for pl_id, _name, songs in self._iter_user_playlists(data):
            if pl_id == "__love__":
                love_items = songs
                break
        LOGGER.info("lxmusic: love-list sync has %d tracks", len(love_items))
        for item in love_items:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or self._default_source)
            try:
                track = await self._parse_track(item, source)
            except Exception as err:
                LOGGER.warning(
                    "lxmusic: love-list track parse failed %r: %s",
                    self._item_song_id(item) if isinstance(item, dict) else item,
                    err,
                )
                continue
            if not track:
                continue
            # the framework uses this to favorite library items not yet favorited
            track.favorite = True
            yield track

    async def set_favorite(self, prov_item_id: str, media_type: MediaType, favorite: bool) -> None:
        """
        MA -> LX direction: write the user's favorite toggle back to the love list.

        MA only calls this once FAVORITE_TRACKS_EDIT is declared, and only for
        tracks. The server endpoints are:
        - add: POST /api/music/user/list/add with listId "love", the full
          MusicInfo and a location; it dedupes by musicInfo.id, so it is idempotent
        - remove: POST /api/music/user/list/remove with listId "love" and the
          song id; removing an absent entry also succeeds
        Both answer with plain text rather than JSON, so these go through
        _request_text; the shared _request would fail to parse the body.

        Adding needs a complete MusicInfo while MA only gives us the provider
        item id, and song search does not accept a songmid, so the raw item is
        taken from, in order:
        1. _raw_cache, from an earlier search, listing or love-list sync;
        2. the current love list cache, in case it is already there;
        3. get_track for the metadata, then a re-search by name and artist for
           the full MusicInfo.
        If none of that works, raise: a favorite the user set should fail
        visibly rather than be dropped silently.
        """
        if media_type != MediaType.TRACK:
            # Only FAVORITE_TRACKS_EDIT is declared, so other media types should
            # not arrive; ignore them quietly rather than raising into the UI.
            LOGGER.debug(
                "lxmusic: set_favorite ignoring non-track type %s: %s", media_type, prov_item_id
            )
            return
        if not self._sync_love_list_enabled():
            # FAVORITE_TRACKS_EDIT is declared unconditionally, so the UI still
            # offers the heart even with sync off; no-op quietly rather than
            # raising an error into the player.
            LOGGER.info(
                "lxmusic: love-list sync disabled, favorite change not written back: %s (%s)",
                "add" if favorite else "remove",
                prov_item_id,
            )
            return
        source, song_id = self._split_id(prov_item_id)
        if not song_id:
            raise RuntimeError(f"LX Music cannot parse track id: {prov_item_id!r}")

        if favorite:
            music_info = await self._love_music_info(prov_item_id, source, song_id)
            # The server dedupes and removes entries by musicInfo.id, but search
            # results only carry songmid, so set id explicitly or add fails.
            music_info["id"] = song_id
            resp = await self._request_text(
                "POST",
                "/api/music/user/list/add",
                data={
                    "listId": "love",
                    "musicInfos": [music_info],
                    "location": "top",
                },
            )
            LOGGER.info(
                "lxmusic: hearting %s:%s (%s) -> %s",
                source,
                song_id,
                music_info.get("name"),
                resp[:80],
            )
        else:
            resp = await self._request_text(
                "POST",
                "/api/music/user/list/remove",
                data={"listId": "love", "songIds": [song_id]},
            )
            LOGGER.info("lxmusic: unhearting %s:%s -> %s", source, song_id, resp[:80])

        # Invalidate the user-list cache after a successful write so the next
        # library sync or playlist listing sees the new state right away.
        self._user_lists_cache_time = 0.0
        # Cache entries are keyed by the full provider item id ("list:__love__"),
        # not the bare pid; popping "__love__" alone never hit anything.
        self._playlist_cache.pop("list:__love__", None)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Return empty; LX Server has no favorites API."""
        if False:
            yield  # type: ignore[misc]

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Return empty; LX Server has no favorites API."""
        if False:
            yield  # type: ignore[misc]

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """
        Yield user-owned playlists (webplayer + love list) to the MA library.

        Virtual playlists (leaderboards, square picks, the recent-play list) are
        not yielded here; they are exposed through browse only, which populates
        the _virtual_meta cache via _collect_virtual_meta().

        Yield order:
        - the love list
        - playlists the user created in the lxserver web player
        """
        data = await self._get_user_lists()
        if not data:
            # Same guard as get_library_tracks: yielding nothing while
            # library_sync_deletions is on would wipe every LX playlist from
            # the library on a transient fetch failure.
            self.report_skipped_sync_item(
                MediaType.PLAYLIST,
                None,
                MusicAssistantError(
                    "Failed to fetch user playlists; this sync round was skipped.",
                    translation_key="user_playlists_fetch_failed",
                    translation_owner=self.translation_owner,
                ),
            )
            return
        for pid, pname, songs in self._iter_user_playlists(data):
            # Skip the recent-play list: it is a virtual playlist exposed
            # only through the browse path playlists/recent.
            if pid == "__default__":
                continue
            item_id = f"list:{pid}"
            self._playlist_cache[item_id] = songs
            # Use the first track's cover as the playlist cover.
            first_song = songs[0] if songs else None
            first_pic = ""
            if isinstance(first_song, dict):
                first_pic = (
                    first_song.get("img") or first_song.get("pic") or first_song.get("image") or ""
                ).strip()
            playlist = Playlist(
                item_id=item_id,
                provider=self.instance_id,
                name=pname,
                translation_key=self._virtual_translation_key(pid),
                provider_mappings={
                    ProviderMapping(
                        item_id=item_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            if first_pic:
                playlist.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=first_pic,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            yield playlist

    async def sync_library(self, media_type: MediaType) -> None:
        """
        Override base class: after base sync, force-sync LX webplayer playlist names to MA.

        BUG #79: user renamed webplayer playlist in LX app, but MA name stayed old.
        Reason: MA Playlist._update_library_item hardcodes name=cur_item.name when
        overwrite=False, and base sync uses overwrite=False by default. Only the
        is_dynamic+not_editable+(name/images diff) branch uses overwrite=True,
        which LX webplayer playlists do not hit.

        This override re-compares webplayer_* names after base sync and calls
        update_item_in_library(overwrite=True) on mismatches.
        """
        await super().sync_library(media_type)
        if media_type == MediaType.PLAYLIST:
            try:
                await self._sync_webplayer_playlist_names()
            except Exception as err:
                LOGGER.debug("lxmusic: webplayer name sync failed: %s", err)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Artist albums: artistAlbums when a real artist id exists, else aggregate a search by name."""
        info = self._artist_cache.get(prov_artist_id)
        if not info:
            source, name = self._split_id(prov_artist_id)
            info = {"source": source, "name": name, "real_id": None}
        source = info["source"]
        name = info["name"]
        real_id = info.get("real_id")
        albums: list[Album] = []
        if real_id:
            try:
                items = await self._fetch_paged(
                    "/api/music/artistAlbums",
                    {"source": source, "id": real_id},
                    max_items=50,
                )
                for item in items:
                    aid_real = self._album_id(item) or item.get("id")
                    aname = (
                        item.get("albumName")
                        or item.get("name")
                        or item.get("album")
                        or "Unknown Album"
                    )
                    albums.append(self._register_album(source, aname, aid_real))
            except Exception as err:
                LOGGER.debug("artistAlbums failed %s: %s", prov_artist_id, err)
        if not albums:
            # fallback: search by artist name and group the hits by album name
            seen_albums: set[str] = set()
            for src in self._search_sources:
                items = await self._search_source(src, name, page_size=30)
                for item in items:
                    singer_names, _ = self._singer_info(item)
                    if name not in singer_names:
                        continue
                    aname = item.get("albumName") or item.get("album") or ""
                    if not aname or aname in seen_albums:
                        continue
                    seen_albums.add(aname)
                    albums.append(self._register_album(src, aname, self._album_id(item)))
                if len(albums) >= 20:
                    break
        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Artist top tracks: artistSongs when a real artist id exists, else search by name and filter."""
        info = self._artist_cache.get(prov_artist_id)
        if not info:
            source, name = self._split_id(prov_artist_id)
            info = {"source": source, "name": name, "real_id": None}
        source = info["source"]
        name = info["name"]
        real_id = info.get("real_id")
        tracks: list[Track] = []
        if real_id:
            try:
                items = await self._fetch_paged(
                    "/api/music/artistSongs",
                    {"source": source, "id": real_id},
                    max_items=50,
                )
                for item in items:
                    track = await self._parse_track(item, source)
                    if track:
                        tracks.append(track)
            except Exception as err:
                LOGGER.debug("artistSongs failed %s: %s", prov_artist_id, err)
        if not tracks:
            # fallback: search by artist name and keep matching artists only
            for src in self._search_sources:
                items = await self._search_source(src, name, page_size=30)
                for item in items:
                    singer_names, _ = self._singer_info(item)
                    if name in singer_names:
                        track = await self._parse_track(item, src)
                        if track:
                            tracks.append(track)
                if len(tracks) >= 20:
                    break
        # dedupe
        seen: set[str] = set()
        out: list[Track] = []
        for track in tracks:
            if track.item_id not in seen:
                seen.add(track.item_id)
                out.append(track)
        return out

    async def unload(self, is_removed: bool = False) -> None:
        """Clean up the HTTP session."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #
    def _session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._http_session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        use_auth: bool = True,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform an HTTP request against the LX Music server."""
        headers: dict[str, str] = {}
        if use_auth and self._token:
            headers["x-user-token"] = self._token
        if extra_headers:
            headers.update(extra_headers)
        url = f"{self._server_url}{path}"
        session = self._session()
        request_kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "json": data,
            "params": params,
            "headers": headers,
        }
        if timeout is not None:
            request_kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        async with session.request(**request_kwargs) as resp:
            if resp.status == 401 and use_auth:
                await self._login()
                headers["x-user-token"] = self._token or ""
                async with session.request(
                    method, url, json=data, params=params, headers=headers
                ) as resp2:
                    return await self._handle_response(resp2)
            return await self._handle_response(resp)

    @staticmethod
    async def _handle_response(resp: aiohttp.ClientResponse) -> Any:
        """Read a response, raising with the body included on a non-2xx status."""
        if resp.status >= 400:
            body = ""
            with suppress(Exception):
                body = await resp.text()
            raise RuntimeError(
                f"HTTP request failed {resp.status} {resp.reason}, body: {body[:500]}"
            )
        return await resp.json()

    async def _login(self) -> None:
        """Authenticate and store the token."""
        # Log the credentials actually sent: an empty username or password here
        # is what makes the server answer 400.
        LOGGER.warning(
            "lxmusic: login request | url=%s/api/user/login | username=%r | password_len=%d",
            self._server_url,
            self._username,
            len(self._password or ""),
        )
        try:
            result = await self._request(
                "POST",
                "/api/user/login",
                data={"username": self._username, "password": self._password},
                use_auth=False,
            )
        except Exception as err:
            LOGGER.exception("lxmusic: login failed: %s", err)
            raise
        if isinstance(result, dict):
            self._token = result.get("token") or result.get("data", {}).get("token")
        if not self._token:
            raise LoginFailed(
                "Login failed: the server returned no token. Check the username and password.",
                translation_key="login_no_token",
                translation_owner=self.translation_owner,
            )
        LOGGER.info("lxmusic: logged in to lxserver as %s", self._username)

    # ------------------------------------------------------------------ #
    # Search & browse
    # ------------------------------------------------------------------ #
    async def _search_source(
        self, source: str, keyword: str, page: int = 1, page_size: int = 30
    ) -> list[dict[str, Any]]:
        """Search a single source."""
        try:
            result = await self._request(
                "GET",
                "/api/music/search",
                params={
                    "source": source,
                    "name": keyword,
                    "page": page,
                    "limit": page_size,
                },
            )
        except Exception as err:
            LOGGER.debug("LX search source %s failed: %s", source, err)
            return []
        return self._normalize_list(result)

    @staticmethod
    def _normalize_list(result: Any) -> list[dict[str, Any]]:
        """
        Normalize the many response shapes lxserver returns into a plain list.

        Handles bare lists plus wrappers such as {list: [...]}, {songs: [...]},
        {musics: [...]}, {tags: [...]}, {boards: [...]}, {playlists: [...]} and
        the same nested under {data: ...}.
        """
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("list", "songs", "musics", "tags", "boards", "playlists", "tags"):
                if isinstance(result.get(key), list):
                    return result[key]
            data = result.get("data", result)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("list", "songs", "musics", "tags", "boards", "playlists", "tags"):
                    if isinstance(data.get(key), list):
                        return data[key]
        return []

    async def _search_song_lists(self, keyword: str, limit: int = 25) -> list[Playlist]:
        """
        Search square/discover playlists via /api/music/songList/search.

        Queries every configured source, converts hits into sl:<source>:<id>
        playlists and caches name/cover in _square_meta for later lookups.
        """
        if not keyword:
            return []
        out: list[Playlist] = []
        seen: set[str] = set()
        for source in self._search_sources:
            try:
                result = await self._request(
                    "GET",
                    "/api/music/songList/search",
                    params={
                        "source": source,
                        "text": keyword,
                        "page": 1,
                        "limit": max(5, limit // max(1, len(self._search_sources))),
                    },
                )
            except Exception as err:
                LOGGER.debug("songList/search source %s failed: %s", source, err)
                continue
            for sl in self._normalize_list(result):
                sl_id = sl.get("id") or sl.get("listId") or sl.get("playId")
                if not sl_id:
                    continue
                sl_source = sl.get("source") or source
                item_id = f"sl:{sl_source}:{sl_id}"
                if item_id in seen:
                    continue
                seen.add(item_id)
                sl_name = sl.get("name") or sl.get("listName") or str(sl_id)
                sl_img = (
                    sl.get("img")
                    or sl.get("pic")
                    or sl.get("image")
                    or sl.get("cover")
                    or sl.get("coverImgUrl")
                )
                self._square_meta[item_id] = {
                    "source": sl_source,
                    "id": str(sl_id),
                    "name": sl_name,
                    "img": sl_img,
                }
                playlist = Playlist(
                    item_id=item_id,
                    provider=self.instance_id,
                    name=sl_name,
                    provider_mappings={
                        ProviderMapping(
                            item_id=item_id,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )
                if sl_img:
                    playlist.metadata.images = [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=sl_img,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                out.append(playlist)
                if len(out) >= limit:
                    return out
        return out

    async def _browse_impl(  # noqa: PLR0915
        self, path: str | None = None
    ) -> Sequence[BrowseFolder | ItemMapping | Any]:
        """Run the real browse implementation; see the browse() docstring."""
        _p = self.instance_id
        if not path or path == f"{_p}://":
            # top level: one BrowseFolder per section
            children: list[BrowseFolder | ItemMapping] = []
            # browse() is fully overridden, so the recommendations entry the base
            # class would add automatically has to be added here as well.
            children.append(
                BrowseFolder(
                    item_id="recommendations",
                    provider=self.instance_id,
                    name="Recommendations",
                    translation_key="recommendations",
                )
            )
            # Per-source "hot" entries. lxserver treats "hot" as a keyword search
            # rather than a real playlist, so these stay plain folders.
            for source in self._search_sources:
                children.append(
                    BrowseFolder(
                        item_id=f"source/{source}",
                        provider=self.instance_id,
                        name=f"{SOURCE_NAMES.get(source, source)} Hot",
                        translation_key=f"hot_{source}",
                    )
                )
            # The user's own playlists reach MA through the library sync, so a
            # separate browse entry would only duplicate them.
            children.append(
                BrowseFolder(
                    item_id="playlists/recent",
                    provider=self.instance_id,
                    name="Recently Played",
                    translation_key="recent_played",
                )
            )
            if getattr(self, "_import_leaderboards", True):
                children.append(
                    BrowseFolder(
                        item_id="playlists/board",
                        provider=self.instance_id,
                        name="Leaderboards",
                        translation_key="leaderboards",
                    )
                )
            if getattr(self, "_import_square", True):
                children.append(
                    BrowseFolder(
                        item_id="playlists/square",
                        provider=self.instance_id,
                        name="Square Playlists",
                        translation_key="square_playlists",
                    )
                )
            return children

        # Recommendation browsing: same semantics as the base class, reimplemented
        # because browse() is overridden.
        if path == f"{_p}://recommendations":
            rows = await self.get_recommendations()
            return [
                BrowseFolder(
                    item_id=row.item_id,
                    provider=self.instance_id,
                    name=row.name,
                    is_playable=row.is_playable,
                    image=row.image,
                    path=f"{path}/{row.item_id}",
                    # carry the localization over: the recommendation rows
                    # already carry keys, the plain folders would drop them
                    translation_key=row.translation_key,
                    translation_params=row.translation_params,
                )
                for row in rows
            ]
        if path.startswith(f"{_p}://recommendations/"):
            row_id = path.replace(f"{_p}://recommendations/", "", 1)
            return list(await self.get_recommendation_items(row_id))

        if path.startswith(f"{_p}://source/"):
            source = path.replace(f"{_p}://source/", "")
            items = await self._search_source(source, "热门", page_size=20)
            children: list[ItemMapping] = []
            for item in items:
                track = await self._parse_track(item, source)
                if not track:
                    continue
                # carry the cover over as ItemMapping.image, otherwise the browse
                # list shows tracks without artwork
                thumb_image = None
                if track.metadata and track.metadata.images:
                    for img in track.metadata.images:
                        if img.type == ImageType.THUMB:
                            thumb_image = img
                            break
                children.append(
                    ItemMapping(
                        media_type=MediaType.TRACK,
                        item_id=track.item_id,
                        provider=self.instance_id,
                        name=track.name,
                        image=thumb_image,
                    )
                )
            return children

        # The playlists/user branch is gone: user playlists come from the library
        # sync, and a browse entry here would show them twice.

        if path == f"{_p}://playlists/recent":
            children = []
            data = await self._get_user_lists()
            if data:
                for pid, pname, songs in self._iter_user_playlists(data):
                    if pid == "__default__":
                        item_id = f"list:{pid}"
                        self._playlist_cache[item_id] = songs
                        children.append(
                            ItemMapping(
                                media_type=MediaType.PLAYLIST,
                                item_id=item_id,
                                provider=self.instance_id,
                                name=pname,
                                translation_key="recent_played",
                            )
                        )
                        break
            return children

        if path == f"{_p}://playlists/board":
            # Boards are listed flat instead of behind a per-platform folder: the
            # extra level was one click for nothing, and boards that share a name
            # across platforms are told apart by a [platform] prefix.
            # _collect_virtual_meta is cache-guarded and prefetched at startup, so
            # the first click is served from memory.
            children: list[ItemMapping] = []
            boards = await self._collect_virtual_meta(kinds=("board",))
            for b in boards:
                b_pic = (b.get("pic") or "").strip()
                b_image = (
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=b_pic,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                    if b_pic
                    else None
                )
                children.append(
                    ItemMapping(
                        media_type=MediaType.PLAYLIST,
                        item_id=b["item_id"],
                        provider=self.instance_id,
                        name=b["name"],
                        image=b_image,
                    )
                )
            return children

        if path in {f"{_p}://playlists/square", f"{_p}://playlists/square/all"}:
            # The server does not filter /songList/list by tag, so expanding
            # parent tags into sub-tags only repeats the same full list.
            # Square playlists are shown flat at this level; an extra folder in
            # front of them was one click for nothing. The /all suffix is kept
            # for paths stored before that change.
            children: list[ItemMapping] = []
            try:
                items = await self._fetch_square_all_items()
                children.extend(items)
                LOGGER.debug(
                    "lxmusic: square playlists | %d items (flat listing)",
                    len(children),
                )
            except Exception as err:
                LOGGER.debug("failed to fetch square playlists: %s", err)
            return children

        # Unknown path: return an empty list
        return []

    def _make_square_playlist(self, source: str, sl_id: str) -> Playlist:
        """Build a square playlist, filling name and cover from _square_meta."""
        item_id = f"sl:{source}:{sl_id}"
        meta = self._square_meta.get(item_id, {})
        playlist = Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=meta.get("name") or sl_id,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if meta.get("img"):
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=meta["img"],
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        return playlist

    # ------------------------------------------------------------------ #
    # Recommendations and the radio playlist
    #
    # Everything comes from lxserver itself, with no external dependency:
    #   daily picks    -> Subsonic getDailySongs / leaderboards
    #   new songs      -> /api/music/leaderboard/list
    #   playlists      -> /api/music/songList/list
    #   radio          -> /api/music/leaderboard/boards, boards picked at random
    # ------------------------------------------------------------------ #
    async def _subsonic_get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """
        Call the Subsonic layer built into lxserver (``/rest/<endpoint>``).

        Subsonic auth here is ``t = md5(password + salt)`` with ``s = salt``.
        The salt is random per request so the plaintext password never reaches
        a URL, and logs only ever show the token.

        Note: on auth failure the server still answers HTTP 200 with
        ``status == "failed"`` and ``error.code = 40``, so the status must be
        checked explicitly. Otherwise failures look like empty data and the
        recommendation slots stay blank without any error.
        """
        salt = secrets.token_hex(SUBSONIC_SALT_BYTES)
        token = hashlib.md5(f"{self._password}{salt}".encode()).hexdigest()
        query: dict[str, Any] = {
            "u": self._username,
            "t": token,
            "s": salt,
            "v": SUBSONIC_VERSION,
            "c": SUBSONIC_CLIENT,
            "f": "json",
        }
        query.update(params or {})
        result = await self._request(
            "GET",
            f"{SUBSONIC_PATH_DEFAULT}/{endpoint}",
            params=query,
            use_auth=False,
            timeout=timeout,
        )
        payload = result.get("subsonic-response") if isinstance(result, dict) else None
        if not isinstance(payload, dict):
            raise RuntimeError(  # noqa: TRY004
                f"Subsonic {endpoint} unexpected response shape: {type(result).__name__}"
            )
        if payload.get("status") != "ok":
            err = payload.get("error")
            detail = err.get("message") if isinstance(err, dict) else err
            raise RuntimeError(f"Subsonic {endpoint} call failed: {detail or 'unknown error'}")
        return payload

    @staticmethod
    def _subsonic_songs(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        """
        Extract the song array from a Subsonic response.

        The wrapper key differs per endpoint: getDailySongs uses
        recommendedSongs, getSongsByGenre uses songsByGenre, getAlbum uses
        album. A single object is also valid per the Subsonic spec, so
        normalize everything to a list.
        """
        node = payload.get(key)
        if not isinstance(node, dict):
            return []
        songs = node.get("song")
        if isinstance(songs, dict):
            songs = [songs]
        if not isinstance(songs, list):
            return []
        return [s for s in songs if isinstance(s, dict)]

    @staticmethod
    def _subsonic_song_to_lx(song: dict[str, Any]) -> dict[str, Any] | None:
        """
        Convert a Subsonic Song into an lxserver item for _parse_track.

        Subsonic ids look like ``tx_004CP8rh41xe1l``: the prefix is the source
        and the remainder is the platform songmid, so the existing
        get_stream_details path for official sources still works.

        albumId is stripped the same way: Subsonic writes ``alb_tx_<mid>``
        while the album endpoints only accept the bare mid.
        """
        raw_id = str(song.get("id") or "")
        source, _, songmid = raw_id.partition("_")
        if not songmid or source not in SOURCE_NAMES:
            return None
        name = str(song.get("name") or song.get("title") or "").strip()
        if not name:
            return None
        item: dict[str, Any] = {
            "source": source,
            "songmid": songmid,
            "name": name,
            "singer": song.get("artist") or "",
            "albumName": song.get("album") or "",
            "interval": song.get("duration") or 0,
        }
        album_id = str(song.get("albumId") or "")
        prefix = f"alb_{source}_"
        if album_id.startswith(prefix):
            item["albumId"] = album_id[len(prefix) :]
        elif album_id and not album_id.startswith("alb_"):
            item["albumId"] = album_id
        cover = song.get("coverArt")
        # In responses such as getAlbumList2 coverArt is an internal id rather
        # than a URL, and only http(s) links can be handed to MA as artwork.
        if isinstance(cover, str) and cover.startswith(("http://", "https://")):
            item["img"] = cover
        return item

    async def _reco_cache_get(self, key: str) -> Any:
        """Read the recommendation cache; cache errors count as a miss."""
        try:
            return await self.mass.cache.get(
                key=f"{_RECO_CACHE_VERSION}:{key}",
                provider=self.instance_id,
                category=CACHE_CATEGORY_RECOMMENDATIONS,
                default=None,
            )
        except Exception as err:
            LOGGER.debug("lxmusic: recommendation cache read failed %s: %s", key, err)
            return None

    async def _reco_cache_set(self, key: str, data: Any, ttl: int) -> None:
        try:
            await self.mass.cache.set(
                key=f"{_RECO_CACHE_VERSION}:{key}",
                data=data,
                expiration=ttl,
                provider=self.instance_id,
                category=CACHE_CATEGORY_RECOMMENDATIONS,
            )
        except Exception as err:
            LOGGER.debug("lxmusic: recommendation cache write failed %s: %s", key, err)

    async def _reco_fetch(self, key: str, ttl: int, loader: Any) -> list[dict[str, Any]]:
        """
        Cache-first fetch: return the cache, else run the loader and store it.

        The loader must return plain JSON-serializable dicts, so the cache holds
        raw items rather than Track objects and stays valid across MA upgrades
        or parser changes.
        """
        cached = await self._reco_cache_get(key)
        if isinstance(cached, list) and cached:
            rows = [x for x in cached if isinstance(x, dict)]
            if rows:
                return rows
        items = await loader()
        if items:
            await self._reco_cache_set(key, items, ttl)
        return items

    def _reco_source(self, key: str, label: str) -> str:
        """
        Which platform's boards feed the daily / new-song slots.

        Read from the provider config, falling back to the default on an
        unknown value. get_setup_value checks setup_data and then the live
        config, so changes apply without restarting MA.
        """
        raw = self.get_setup_value(key, RECO_SOURCE_DEFAULT)
        src = str(raw or "").strip().lower()
        if src not in RECO_SOURCES:
            if src:
                LOGGER.debug(
                    "lxmusic: %s source %r is invalid, using %s", label, raw, RECO_SOURCE_DEFAULT
                )
            return RECO_SOURCE_DEFAULT
        return src

    def _daily_source(self) -> str:
        """Daily source, see _reco_source."""
        return self._reco_source(CONF_DAILY_SOURCE, "daily recommendations")

    def _newsong_source(self) -> str:
        """New-song source, see _reco_source."""
        return self._reco_source(CONF_NEWSONG_SOURCE, "new song recommendations")

    async def _reco_daily_rows(self) -> list[dict[str, Any]]:
        """
        Daily picks: mix 4-5 official boards of one platform, shuffle by date.

        The server's getDailySongs is assembled from a handful of recommended
        albums, so its tracks share very few covers and the UI shows the same
        artwork over and over. Mixing several boards instead gives close to one
        distinct cover per row.

        Board names differ per platform and are listed in
        DAILY_BOARDS_BY_SOURCE. A board that fails is skipped; when the whole
        platform comes back empty the fallback keywords are tried, then the
        server-side getDailySongs. The shuffle seed is the calendar day, so the
        list is stable within a day and rotates daily.
        """
        source = self._daily_source()
        boards = DAILY_BOARDS_BY_SOURCE.get(source, DAILY_BOARDS_BY_SOURCE[RECO_SOURCE_DEFAULT])

        async def _load() -> list[dict[str, Any]]:
            pool: list[dict[str, Any]] = []
            for name in boards:
                try:
                    pool.extend(await self._board_rows_by_name(name, source=source))
                except Exception as err:
                    LOGGER.debug("lxmusic: daily board %s/%s failed: %s", source, name, err)
            # nothing from the configured boards: retry once with fallback names
            if not pool:
                for keyword in DAILY_FALLBACK_KEYWORDS:
                    try:
                        pool = await self._board_rows_by_name(keyword, source=source)
                    except Exception as err:
                        LOGGER.debug(
                            "lxmusic: daily fallback board %s/%s failed: %s", source, keyword, err
                        )
                        continue
                    if pool:
                        break
            # still empty: hot boards from the other platforms
            if not pool:
                for src in NEWSONG_FALLBACK_SOURCES:
                    if src == source:
                        continue
                    try:
                        pool = await self._board_rows_by_name("热歌", source=src)
                    except Exception as err:
                        LOGGER.debug("lxmusic: daily hot-board fallback %s failed: %s", src, err)
                        continue
                    if pool:
                        break
            # last resort: the server-side getDailySongs
            if not pool:
                pool = await self._reco_daily_rows_subsonic()

            # dedupe across boards, since one track often appears on several
            seen: set[str] = set()
            unique: list[dict[str, Any]] = []
            for row in pool:
                key = self._row_song_key(row)
                if not key or key in seen:
                    continue
                seen.add(key)
                unique.append(row)
            # cap per album: covers are album art
            unique = self._limit_by_album(unique, DAILY_MAX_PER_ALBUM)
            # shuffle by calendar day: stable today, different tomorrow
            rng = random.Random(int(time.time()) // 86400)
            rng.shuffle(unique)
            return unique[:DAILY_TARGET]

        # Key the cache by platform so switching does not serve the old platform
        return await self._reco_fetch(f"{RECO_DAILY}:{source}", _RECO_TTL_DAILY, _load)

    async def _reco_daily_rows_subsonic(self) -> list[dict[str, Any]]:
        """Fallback: the server-side Subsonic getDailySongs."""
        try:
            payload = await self._subsonic_get("getDailySongs", {"size": 100})
        except Exception as err:
            LOGGER.debug("lxmusic: getDailySongs failed: %s", err)
            return []
        return [
            lx
            for lx in (
                self._subsonic_song_to_lx(s)
                for s in self._subsonic_songs(payload, "recommendedSongs")
            )
            if lx
        ]

    @staticmethod
    def _row_song_key(row: dict[str, Any]) -> str:
        """Dedup key for a song: platform plus song id."""
        return f"{row.get('source')}:{row.get('songmid') or row.get('songId') or row.get('id')}"

    @staticmethod
    def _limit_by_album(rows: list[dict[str, Any]], max_per_album: int) -> list[dict[str, Any]]:
        """
        Cap tracks per album, preserving order.

        Covers are album art, so same-album rows look duplicated. Rows without
        an album id are not counted and are always kept.
        """
        out: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for row in rows:
            album = str(row.get("albumId") or "")
            if album:
                n = counts.get(album, 0)
                if n >= max_per_album:
                    continue
                counts[album] = n + 1
            out.append(row)
        return out

    async def _reco_new_rows(self) -> list[dict[str, Any]]:
        """
        New-song picks: mix the platform's "new" boards, dedupe and cap per album.

        This used to take the first non-empty new-song board from a fixed source
        list, i.e. a single board, which yields far fewer distinct covers than
        mixing several new-oriented boards (new / rising / original) within one
        platform. Hot and classic charts are deliberately excluded.

        The shuffle seed is an hourly bucket, so the list is stable for an hour
        and then rotates, matching the cache TTL. A board that fails is skipped;
        if the platform comes back empty the fallback keywords are tried, then
        other platforms' new-song boards, then the Subsonic newest albums.
        """
        source = self._newsong_source()
        boards = NEWSONG_BOARDS_BY_SOURCE.get(source, NEWSONG_BOARDS_BY_SOURCE[RECO_SOURCE_DEFAULT])

        async def _load() -> list[dict[str, Any]]:
            pool: list[dict[str, Any]] = []
            for name in boards:
                try:
                    pool.extend(await self._board_rows_by_name(name, source=source))
                except Exception as err:
                    LOGGER.debug("lxmusic: new-song board %s/%s failed: %s", source, name, err)
            # nothing from the configured boards: retry once with fallback names
            if not pool:
                for keyword in NEWSONG_FALLBACK_KEYWORDS:
                    try:
                        pool = await self._board_rows_by_name(keyword, source=source)
                    except Exception as err:
                        LOGGER.debug(
                            "lxmusic: new-song fallback board %s/%s failed: %s",
                            source,
                            keyword,
                            err,
                        )
                        continue
                    if pool:
                        break
            # still empty: new-song boards from the other platforms
            if not pool:
                for src in NEWSONG_FALLBACK_SOURCES:
                    if src == source:
                        continue
                    try:
                        pool = await self._board_rows_by_name("新歌", source=src)
                    except Exception as err:
                        LOGGER.debug("lxmusic: new-song fallback %s failed: %s", src, err)
                        continue
                    if pool:
                        break
            # last resort: tracks from the newest Subsonic albums
            if not pool:
                pool = await self._recent_album_rows()

            # dedupe across boards, since one track is often on several charts
            seen: set[str] = set()
            unique: list[dict[str, Any]] = []
            for row in pool:
                key = self._row_song_key(row)
                if not key or key in seen:
                    continue
                seen.add(key)
                unique.append(row)
            # cap per album: covers are album art
            unique = self._limit_by_album(unique, NEWSONG_MAX_PER_ALBUM)
            rng = random.Random(int(time.time()) // 3600)
            rng.shuffle(unique)
            return unique[:NEWSONG_TARGET]

        # Key the cache by platform so switching does not serve the old platform
        return await self._reco_fetch(f"{RECO_NEW}:{source}", _RECO_TTL_NEWSONG, _load)

    async def _songlist_meta(self, source: str, sl_id: str) -> dict[str, Any]:
        """
        Fetch square playlist metadata (name/img/author/desc) with a persistent cache.

        _virtual_meta is in-memory, so after a restart, or when recommendation
        rows come from the cache, get_playlist would display the raw item id as
        the name. songList/detail carries the full playlist info, so fetch it
        once and keep it in the MA cache for an hour; the result also refills
        _virtual_meta.

        Note: this endpoint ignores page/limit and returns the whole track list,
        which is discarded here since only the info field is wanted.
        """
        if not source or not sl_id:
            return {}
        item_id = f"list:songlist:{source}:{sl_id}"
        cached = self._virtual_meta.get(item_id)
        if cached and cached.get("name"):
            return cached
        cache_key = f"slmeta_{source}_{sl_id}"
        stored = await self._reco_cache_get(cache_key)
        if isinstance(stored, dict) and stored.get("name"):
            self._virtual_meta[item_id] = stored
            return stored
        try:
            data = await self._request(
                "GET",
                "/api/music/songList/detail",
                params={"source": source, "id": sl_id, "page": 1, "limit": 1},
            )
        except Exception as err:
            LOGGER.debug("lxmusic: square playlist metadata failed %s: %s", item_id, err)
            return {}
        info = data.get("info") if isinstance(data, dict) else None
        if not isinstance(info, dict) or not info.get("name"):
            return {}
        meta = {
            "name": str(info.get("name") or "").strip(),
            "source": source,
            "sl_id": sl_id,
            "img": str(info.get("img") or "").strip(),
            "author": str(info.get("author") or "").strip(),
            "desc": str(info.get("desc") or "").strip(),
            "kind": "songlist",
        }
        self._virtual_meta[item_id] = meta
        await self._reco_cache_set(cache_key, meta, _RECO_TTL_PLAYLISTS)
        return meta

    async def _reco_playlist_rows(self) -> list[dict[str, Any]]:
        """Recommended playlists: square lists from all platforms, reusing the sl: virtual playlists."""

        async def _load() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for src in ("tx", "wy", "kw", "kg", "mg"):
                try:
                    data = await self._request(
                        "GET",
                        "/api/music/songList/list",
                        params={"source": src, "page": 1},
                    )
                except Exception as err:
                    LOGGER.debug("lxmusic: square playlists %s failed: %s", src, err)
                    continue
                raw = data.get("list") if isinstance(data, dict) else None
                if not isinstance(raw, list):
                    continue
                for sl in raw:
                    if not isinstance(sl, dict):
                        continue
                    sl_id = str(sl.get("id") or "").strip()
                    if not sl_id:
                        continue
                    item = {
                        "source": src,
                        "id": sl_id,
                        "name": (sl.get("name") or "Untitled Playlist").strip(),
                        "author": (sl.get("author") or "").strip(),
                        "img": (sl.get("img") or "").strip(),
                    }
                    rows.append(item)
                    # Register the virtual playlist metadata so opening it hits
                    # the cache instead of calling the server again.
                    self._virtual_meta.setdefault(
                        f"list:songlist:{src}:{sl_id}",
                        {
                            "name": item["name"],
                            "source": src,
                            "sl_id": sl_id,
                            "img": item["img"],
                            "author": item["author"],
                            "kind": "songlist",
                        },
                    )
            # sources are fetched in order, so shuffle to avoid one platform first
            random.shuffle(rows)
            return rows[:60]

        return await self._reco_fetch(RECO_PLAYLISTS, _RECO_TTL_PLAYLISTS, _load)

    def _radio_interval_minutes(self) -> int:
        """
        Radio rotation interval in minutes, from the provider config.

        Out-of-range values are clamped. The config entry is an INTEGER but may
        arrive as a string, so convert it here.
        """
        raw = self.get_setup_value(CONF_RADIO_INTERVAL, RADIO_INTERVAL_DEFAULT)
        try:
            minutes = int(raw)  # type: ignore[arg-type]
        except TypeError, ValueError:
            minutes = RADIO_INTERVAL_DEFAULT
        return max(RADIO_INTERVAL_MIN, min(RADIO_INTERVAL_MAX, minutes or RADIO_INTERVAL_DEFAULT))

    def _radio_interval_seconds(self) -> int:
        """Rotation interval in seconds, floored at 60 to avoid dividing by zero."""
        return max(60, self._radio_interval_minutes() * 60)

    def _radio_bucket_no(self) -> int:
        """Return the current radio time bucket (stable content within, rotation across)."""
        return int(time.time()) // self._radio_interval_seconds()

    async def _reco_radio_pool(self) -> list[dict[str, Any]]:
        """
        Radio candidate pool: real leaderboards picked per time bucket, plus daily picks.

        The seed is the time bucket rather than an open counter, so within one
        interval every open shows the same boards, tracks and order, and only
        the next bucket changes them. The whole pool is cached under the bucket
        with a TTL equal to the interval, so it expires exactly when a new
        bucket starts.
        """
        bucket = self._radio_bucket_no()
        ttl = self._radio_interval_seconds()

        async def _load() -> list[dict[str, Any]]:
            boards = await self._reco_boards()
            pool: list[dict[str, Any]] = []
            if boards:
                # seed = bucket number: the sample is fixed within one interval
                picks = random.Random(bucket).sample(boards, min(RADIO_BOARD_PICKS, len(boards)))
                for src, bangid, _name in picks:
                    pool.extend(await self._reco_board_songs(src, bangid))
            # mix in the daily picks so the radio is not purely random charts
            try:
                pool.extend((await self._reco_daily_rows())[:30])
            except Exception as err:
                LOGGER.debug("lxmusic: radio daily-pick mix-in failed: %s", err)
            return pool

        return await self._reco_fetch(f"radio_pool_{bucket}", ttl, _load)

    async def _reco_boards(self) -> list[tuple[str, str, str]]:
        """All (source, bangid, board name) triples across platforms, cached for an hour."""

        async def _load() -> list[tuple[str, str, str]]:
            out: list[tuple[str, str, str]] = []
            for src in NEWSONG_FALLBACK_SOURCES:
                try:
                    data = await self._request(
                        "GET", "/api/music/leaderboard/boards", params={"source": src}
                    )
                except Exception as err:
                    LOGGER.debug("lxmusic: radio board list %s failed: %s", src, err)
                    continue
                rows = data.get("list") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    bangid = str(row.get("bangid") or "").strip()
                    if not bangid:
                        continue
                    out.append((src, bangid, str(row.get("name") or bangid)))
            return out

        return await self._reco_fetch("radio_boards", _RECO_TTL_PLAYLISTS, _load)

    async def _reco_board_songs(self, source: str, bangid: str) -> list[dict[str, Any]]:
        """Candidate songs from one board, cached per board, first N only."""

        async def _load() -> list[dict[str, Any]]:
            rows = await self._board_rows(source, bangid)
            rows = rows[:RADIO_SONGS_PER_BOARD]
            # Board rows carry their own source, but override it explicitly:
            # playback must use the requested platform, otherwise _parse_track
            # may pick up an otherSource and fail to play.
            for row in rows:
                if isinstance(row, dict):
                    row["source"] = source
            return rows

        return await self._reco_fetch(f"radio_board_{source}_{bangid}", _RECO_TTL_RADIO, _load)

    async def _build_radio_playlist(self) -> Playlist:
        """Descriptor for the dynamic radio playlist (is_dynamic=True)."""
        image: str | None = None
        try:
            for row in await self._reco_radio_pool():
                img = row.get("img")
                if isinstance(img, str) and img:
                    image = img
                    break
        except Exception as err:
            LOGGER.debug("lxmusic: radio cover lookup failed: %s", err)
        interval = self._radio_interval_minutes()
        return self._make_virtual_playlist(
            RADIO_PLAYLIST_ITEM_ID,
            RADIO_NAME,
            image,
            is_dynamic=True,
            subtitle=(
                "Randomly samples multiple charts, switches to a new "
                f"batch every {interval} minutes"
            ),
            translation_key="radio",
            translation_params=[str(interval)],
        )

    async def _get_radio_tracks(self) -> list[Track]:
        """
        One batch of radio tracks: shuffle the pool, drop recent plays, parse.

        The shuffle seed is the time bucket, so within one interval every open
        yields the same tracks in the same order. MA's dynamic queue filters
        duplicates itself; calling filter_tracks here only skips tracks it would
        discard anyway.
        """
        try:
            pool = await self._reco_radio_pool()
        except Exception as err:
            LOGGER.debug("lxmusic: radio candidate pool failed: %s", err)
            return []
        if not pool:
            return []
        bucket = self._radio_bucket_no()
        if bucket != self._radio_bucket:
            self._radio_bucket = bucket
            LOGGER.debug("lxmusic: radio moved to time bucket %d", bucket)
        rng = random.Random(bucket)
        candidates = list(pool)
        rng.shuffle(candidates)
        # dedupe by songmid: boards and daily picks overlap
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for row in candidates:
            key = f"{row.get('source')}:{row.get('songmid')}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        tracks: list[Track] = []
        for row in unique:
            source = str(row.get("source") or self._default_source)
            try:
                track = await self._parse_track(row, source)
            except Exception as err:
                LOGGER.debug("lxmusic: radio track parse failed: %s", err)
                continue
            if track:
                tracks.append(track)
        if _ma_filter_tracks is not None:
            try:
                filtered = _ma_filter_tracks(tracks)
                if filtered:
                    tracks = filtered
            except Exception as err:
                LOGGER.debug("lxmusic: radio filter_tracks ignored: %s", err)
        return tracks

    def _make_virtual_playlist(
        self,
        item_id: str,
        name: str,
        image: str | None = None,
        *,
        is_dynamic: bool = False,
        subtitle: str | None = None,
        translation_key: str | None = None,
        translation_params: list[str] | None = None,
    ) -> Playlist:
        """Build a virtual or dynamic playlist object, shared by radio and picks."""
        playlist = Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            is_dynamic=is_dynamic,
            translation_key=translation_key,
            translation_params=translation_params,
        )
        if subtitle:
            playlist.metadata.description = subtitle
        if image:
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        return playlist

    async def _board_rows_by_name(
        self, keyword: str, source: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Fetch leaderboard songs by board name, returning raw lxserver items.

        Board ids are handed out by the server and differ per platform, so the
        board is looked up by keyword first. Results are cached in memory, one
        lookup per process.
        """
        sources = [source] if source else list(NEWSONG_FALLBACK_SOURCES)
        for src in sources:
            bangid = await self._resolve_board_id(src, keyword)
            if not bangid:
                continue
            rows = await self._board_rows(src, bangid)
            if rows:
                return rows
        return []

    async def _resolve_board_id(self, source: str, keyword: str) -> str | None:
        """Fuzzy-match a board name to its bangid on one platform."""
        cache: dict[str, str | None] = self._board_id_cache
        key = f"{source}:{keyword}"
        if key in cache:
            return cache[key]
        bangid: str | None = None
        try:
            data = await self._request(
                "GET", "/api/music/leaderboard/boards", params={"source": source}
            )
            # Note: this endpoint wraps its result in "list", not "boards".
            rows = data.get("list") if isinstance(data, dict) else None
            if isinstance(rows, list):
                best: tuple[int, str] | None = None
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("name") or "")
                    bid = str(row.get("bangid") or "").strip()
                    if not bid or keyword not in name:
                        continue
                    # an exact name match is the intended board, take it at once
                    # (daily picks address boards by full name)
                    if name.strip() == keyword.strip():
                        best = (0, bid)
                        break
                    # shorter names are closer to the flagship board of that kind
                    score = len(name)
                    if best is None or score < best[0]:
                        best = (score, bid)
                if best:
                    bangid = best[1]
        except Exception as err:
            LOGGER.debug("lxmusic: board lookup failed %s/%s: %s", source, keyword, err)
        cache[key] = bangid
        return bangid

    async def _board_rows(self, source: str, bangid: str) -> list[dict[str, Any]]:
        """Fetch the first page of one board as raw lxserver items."""
        try:
            data = await self._request(
                "GET",
                "/api/music/leaderboard/list",
                params={"source": source, "bangid": bangid, "page": 1},
            )
        except Exception as err:
            LOGGER.debug("lxmusic: board %s/%s fetch failed: %s", source, bangid, err)
            return []
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)][:100]

    async def _recent_album_rows(self) -> list[dict[str, Any]]:
        """Fallback: newest Subsonic albums and their tracks, used only when new-song boards fail."""
        rows: list[dict[str, Any]] = []
        albums: Any = []
        try:
            payload = await self._subsonic_get("getAlbumList2", {"type": "recent", "size": 6})
            node = payload.get("albumList2")
            albums = node.get("album") if isinstance(node, dict) else []
            if isinstance(albums, dict):
                albums = [albums]
        except Exception as err:
            LOGGER.debug("lxmusic: getAlbumList2 failed: %s", err)
            return rows
        for album in (albums if isinstance(albums, list) else [])[:6]:
            if not isinstance(album, dict):
                continue
            album_id = str(album.get("id") or "")
            if not album_id:
                continue
            try:
                album_payload = await self._subsonic_get("getAlbum", {"id": album_id})
            except Exception as err:
                LOGGER.debug("lxmusic: getAlbum %s failed: %s", album_id, err)
                continue
            for song in self._subsonic_songs(album_payload, "album"):
                lx = self._subsonic_song_to_lx(song)
                if lx:
                    rows.append(lx)
            if len(rows) >= 100:
                break
        return rows[:100]

    # ------------------------------------------------------------------ #
    # Library sync (love list into MA favorites)
    # ------------------------------------------------------------------ #
    def _setup_bool(self, key: str, default: bool = True) -> bool:
        """
        Read a BOOLEAN config entry as a real bool.

        Two traps make a bare bool(get_setup_value(...)) wrong here: MA may
        store the value as a string such as "false" (and bool("false") is
        True), and the entry is absent before the first save, where the
        default must win. Note that ``value or True`` is also useless since
        it is always True.
        """
        raw = self.get_setup_value(key, default)
        if isinstance(raw, str):
            return raw.strip().lower() not in ("false", "0", "off", "no", "")
        return default if raw is None else bool(raw)

    def _sync_love_list_enabled(self) -> bool:
        """
        Love-list sync toggle from the sync_love_list config entry, on by default.

        Read through get_setup_value on every use, like the radio interval, so
        a change in MA settings takes effect without restarting MA. A BOOLEAN
        entry may arrive as a string such as "false", so parse it here.
        """
        return self._setup_bool(CONF_SYNC_LOVE_LIST, True)

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> str:
        """
        POST/GET and return the body as text, tolerating non-JSON responses.

        The love-list write endpoints answer plain text on success, which the
        shared _request would break on since it always calls resp.json().
        Non-2xx raises with the body included, which is what makes server-side
        validation errors such as missing listId diagnosable.
        """
        headers: dict[str, str] = {}
        if self._token:
            headers["x-user-token"] = self._token
        url = f"{self._server_url}{path}"
        session = self._session()
        async with session.request(method, url, json=data, headers=headers) as resp:
            if resp.status == 401:
                await self._login()
                headers["x-user-token"] = self._token or ""
                async with session.request(method, url, json=data, headers=headers) as resp2:
                    body = await resp2.text()
                    if resp2.status >= 400:
                        raise RuntimeError(
                            f"HTTP request failed {resp2.status} {resp2.reason}, body: {body[:500]}"
                        )
                    return body.strip()
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(
                    f"HTTP request failed {resp.status} {resp.reason}, body: {body[:500]}"
                )
            return body.strip()

    async def _love_music_info(
        self, prov_item_id: str, source: str, song_id: str
    ) -> dict[str, Any]:
        """
        Build a full LX.Music.MusicInfo for hearting, raising if impossible.

        MA only gives us the provider item id, while the add endpoint needs a
        complete MusicInfo. Song search does not accept a songmid as keyword, so
        the info cannot be rebuilt that way. Sources, fastest first:
        1. _raw_cache, left behind by an earlier search, listing or sync of this track;
        2. the user playlist cache, which includes the love list;
        3. the MA database, looked up by provider item id, to get name and artist
           and then search for them;
        4. get_track, which works on the few platforms where songmid is searchable;
        5. as a last resort assemble one from the metadata we do have; missing
           fields still store fine.
        """
        key = f"{source}:{song_id}"
        # 1) fastest: raw item cached by any earlier path that resolved this track
        raw = self._raw_cache.get(key)
        if isinstance(raw, dict) and raw.get("name"):
            return dict(raw)
        # 2) already present in the user playlist or love list cache
        data = await self._get_user_lists()
        if isinstance(data, dict):
            for _pl_id, _name, songs in self._iter_user_playlists(data):
                for item in songs:
                    if (
                        isinstance(item, dict)
                        and str(item.get("source") or source) == source
                        and self._item_song_id(item) == song_id
                        and item.get("name")
                    ):
                        return dict(item)
        # 3) look the item up in the MA library: the framework adds it to the
        #    library before forwarding the favorite, so name and artist are
        #    normally available here without any network search.
        db_track = None
        try:
            db_track = await self.mass.music.tracks.get_library_item_by_prov_id(
                prov_item_id, self.instance_id
            )
        except Exception as err:
            LOGGER.debug("lxmusic: library lookup failed %s: %s", prov_item_id, err)
        meta = db_track or await self._love_meta_fallback(prov_item_id)
        if meta is None:
            LOGGER.warning("lxmusic: no metadata for %s, cannot heart it", prov_item_id)
            raise MusicAssistantError(
                f"Track {prov_item_id} not found on lxserver or in Music "
                "Assistant; cannot heart it (it may have been removed from "
                "the source platform).",
                translation_key="track_info_not_found",
                translation_owner=self.translation_owner,
            )
        # 4) re-search by name plus artist to get the server's full MusicInfo
        singer = ""
        artists = list(getattr(meta, "artists", None) or [])
        if artists:
            singer = str(getattr(artists[0], "name", "") or "")
        keyword = f"{getattr(meta, 'name', '') or ''} {singer}".strip()
        if keyword:
            for item in await self._search_source(source, keyword, page_size=20):
                if not isinstance(item, dict):
                    continue
                if self._item_song_id(item) == song_id and item.get("name"):
                    return dict(item)
            # no exact id for the combined query: retry with the bare track name
            name_only = str(getattr(meta, "name", "") or "").strip()
            if name_only and name_only != keyword:
                for item in await self._search_source(source, name_only, page_size=30):
                    if (
                        isinstance(item, dict)
                        and self._item_song_id(item) == song_id
                        and item.get("name")
                    ):
                        return dict(item)
        # 5) last resort: assemble from metadata. The server only requires
        #    listId and a musicInfos array, so missing fields still store, and
        #    the LX client may re-match the track when it opens it.
        LOGGER.warning(
            "lxmusic: %s could not be rebuilt from search, hearting from metadata", prov_item_id
        )
        return {
            "name": getattr(meta, "name", "") or "Unknown Song",
            "singer": "、".join(
                str(getattr(a, "name", "") or "") for a in artists if getattr(a, "name", "")
            )
            or "Unknown Artist",
            "source": source,
            "songmid": song_id,
            "albumName": getattr(getattr(meta, "album", None), "name", "") or "",
            "albumId": getattr(getattr(meta, "album", None), "item_id", "") or "",
            "interval": _normalize_lx_interval(getattr(meta, "duration", 0)),
            "types": [{"type": "128k", "size": "0"}, {"type": "320k", "size": "0"}],
        }

    async def _love_meta_fallback(self, prov_item_id: str) -> Any:
        """Last attempt before giving up: fetch metadata through get_track."""
        try:
            return await self.get_track(prov_item_id)
        except Exception as err:
            LOGGER.debug("lxmusic: get_track(%s) fallback failed: %s", prov_item_id, err)
            return None

    def _library_item_needs_update(self, library_item, prov_item) -> bool:
        """
        Decide whether a library playlist needs an update.

        The base implementation compares provider mappings and date added only,
        never the name. Virtual playlist ids are stable while their names are
        generated from a display template, so after a template change the stored
        names would never be refreshed.

        This override also compares names, which is safe for user playlists too:
        renaming one there should update the library entry as well.

        Images are compared on top of that. Covers come from the first track of
        the detail page and are not part of the base sync comparison, so without
        this a stored cover would stay empty forever as long as the name held.
        """
        base = super()._library_item_needs_update(library_item, prov_item)
        lib_name = getattr(library_item, "name", None)
        prov_name = getattr(prov_item, "name", None)
        name_differs = lib_name != prov_name

        # compare covers: reduce the first thumbnail to its path
        def _first_thumb_path(item) -> str:
            meta = getattr(item, "metadata", None)
            if not meta:
                return ""
            images = getattr(meta, "images", None) or []
            for img in images:
                # ImageType may not expose .value, fall back to comparing as text
                t = getattr(img, "type", None)
                if str(t).endswith("THUMB") or str(t) == "thumb" or str(t) == "ImageType.THUMB":
                    return (getattr(img, "path", "") or "").strip()
            return ""

        lib_img = _first_thumb_path(library_item)
        prov_img = _first_thumb_path(prov_item)
        image_differs = lib_img != prov_img

        needs = base or name_differs or image_differs
        # Logged at INFO so it is visible without raising the log level. During
        # sync the provider logger is swapped for the framework's own, which
        # points at the same logger, and the super() call below logs through it.
        # Kept at DEBUG rather than WARNING so a sync does not emit dozens of
        # warning lines; it is still visible with the provider logger at DEBUG.
        if (name_differs and not base) or image_differs:
            LOGGER.debug(
                "lxmusic: update triggered | lib_id=%s prov_item_id=%s name_diff=%s image_diff=%s (lib=%r prov=%r)",
                getattr(library_item, "item_id", "?"),
                getattr(prov_item, "item_id", "?"),
                name_differs,
                image_differs,
                lib_img[:60],
                prov_img[:60],
            )
        return needs

    async def _cleanup_double_layer_names(self) -> None:
        """
        Rename stored square playlists in the database to ``LX ·<tag>:<name>``.

        Updates playlists.name through the database directly rather than
        update_item_in_library, so no MEDIA_ITEM_UPDATED event fires and
        interrupts playback; the UI picks the new names up on its next fetch.
        """
        pattern = self._SONGLIST_NAME_RE
        db = self.mass.music.database
        # Join provider mappings to touch only LX playlists. Avoid GROUP BY and
        # use DISTINCT only: rows come back without a standard mapping
        # interface, so positional access is the reliable option.
        query = (
            "SELECT DISTINCT pl.item_id, pl.name "
            "FROM playlists pl "
            "JOIN provider_mappings pm ON pl.item_id = pm.item_id "
            "WHERE pm.provider_domain = :domain "
            "  AND pl.name LIKE 'LX 歌单·%'"
        )
        cleaned = 0
        skipped = 0
        failed = 0
        try:
            async for row in db.iter_rows_from_query(query, params={"domain": self.domain}):
                # The row type supports both positional and by-name access; use
                # positional consistently so a parsing hiccup cannot cause an
                # entry to be skipped.
                item_id = row[0]
                old_name = row[1] or ""
                if not item_id:
                    continue
                m = pattern.match(old_name)
                if not m:
                    skipped += 1
                    continue
                # group(1) is the prefix, group(2) the final tag in both the
                # two-layer and single-layer forms, group(3) the remainder.
                new_name = f"LX ·{m.group(2)}{m.group(3)}"
                if new_name == old_name:
                    skipped += 1
                    continue
                try:
                    await db.update(
                        "playlists",
                        match={"item_id": int(item_id)},
                        values={"name": new_name},
                    )
                    cleaned += 1
                    if cleaned <= 3 or cleaned % 100 == 0:
                        LOGGER.info(
                            "lxmusic: playlist name cleanup | item_id=%s '%s' -> '%s'",
                            item_id,
                            old_name,
                            new_name,
                        )
                except Exception as err:
                    failed += 1
                    LOGGER.debug(
                        "lxmusic: playlist name cleanup failed | item_id=%s err=%s",
                        item_id,
                        err,
                    )
        except Exception as err:
            LOGGER.debug("lxmusic: playlist name cleanup query failed: %s", err)
            return
        LOGGER.info(
            "lxmusic: playlist name cleanup done | cleaned=%d skipped=%d failed=%d",
            cleaned,
            skipped,
            failed,
        )

    async def _sync_webplayer_playlist_names(self) -> None:
        """
        Compare LX server userList webplayer_* playlists, update MA DB name on mismatch.

        BUG #79 helper. Only touches list:webplayer_* item_ids:
        - defaultList / loveList / virtual playlists (leaderboards/square) are NOT touched
          here; they are handled by other cleanup/yield paths.
        - Runs after lxmusic base sync, calls update_item_in_library(overwrite=True)
          to flush the latest name to DB.

        Note: update_item_in_library triggers MEDIA_ITEM_UPDATED event and cache
        cleanup -- this is desired. base MusicProvider.on_item_updated is a no-op,
        so no write-back to LX server (avoiding loops).
        """
        data = await self._get_user_lists()
        if not data:
            return
        user_list = data.get("userList") or []
        if not isinstance(user_list, list):
            return
        db = self.mass.music.database
        matched = 0
        updated = 0
        failed = 0
        query = (
            "SELECT pl.item_id, pl.name "
            "FROM playlists pl "
            "JOIN provider_mappings pm ON pl.item_id = pm.item_id "
            "WHERE pm.provider_domain = :domain "
            "  AND pm.provider_item_id = :prov_item_id "
            "LIMIT 1"
        )
        for entry in user_list:
            if not isinstance(entry, dict):
                continue
            pl_id = entry.get("id")
            new_name = (entry.get("name") or "").strip()
            if not isinstance(pl_id, str) or not pl_id or not new_name:
                continue
            if not pl_id.startswith("webplayer_"):
                # Only process user-created/renamed webplayer local playlists.
                # Skip other userList entries (lxserver built-in/default) to avoid
                # accidentally overwriting MA virtual playlists.
                continue
            prov_item_id = f"list:{pl_id}"
            row = None
            try:
                async for r in db.iter_rows_from_query(
                    query,
                    params={"domain": self.domain, "prov_item_id": prov_item_id},
                ):
                    row = r
                    break
            except Exception as err:
                failed += 1
                LOGGER.debug(
                    "lxmusic: webplayer name query failed | prov_item_id=%s err=%s",
                    prov_item_id,
                    err,
                )
                continue
            if row is None:
                # MA DB does not have this row yet (first sync?), will be added next time
                continue
            item_id = int(row[0])
            old_name = row[1] or ""
            if old_name == new_name:
                matched += 1
                continue
            try:
                pl_obj = Playlist(
                    item_id=prov_item_id,
                    provider=self.instance_id,
                    name=new_name,
                    provider_mappings={
                        ProviderMapping(
                            item_id=prov_item_id,
                            provider_domain=self.domain,
                            provider_instance=self.instance_id,
                        )
                    },
                )
                await self.mass.music.playlists.update_item_in_library(
                    item_id=item_id,
                    update=pl_obj,
                    overwrite=True,
                )
                updated += 1
                LOGGER.info(
                    "lxmusic: webplayer playlist rename | prov_item_id=%s '%s' -> '%s'",
                    prov_item_id,
                    old_name,
                    new_name,
                )
            except Exception as err:
                failed += 1
                LOGGER.debug(
                    "lxmusic: webplayer rename failed | prov_item_id=%s err=%s",
                    prov_item_id,
                    err,
                )
        if updated > 0 or failed > 0:
            LOGGER.info(
                "lxmusic: webplayer name sync done | matched=%d updated=%d failed=%d",
                matched,
                updated,
                failed,
            )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split_id(prov_id: str) -> tuple[str, str]:
        if ":" in prov_id:
            source, _, song_id = prov_id.partition(":")
            return source, song_id
        return "wy", prov_id

    @staticmethod
    def _item_song_id(item: dict[str, Any]) -> str:
        """Search results identify songs by songmid."""
        return str(item.get("songmid") or item.get("songId") or item.get("id") or "")

    @staticmethod
    def _album_id(item: dict[str, Any]) -> str | None:
        """Accept the various spellings of the album id field."""
        for key in ("albumId", "albumid", "album_id"):
            value = item.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _singer_info(item: dict[str, Any]) -> tuple[list[str], str | None]:
        """
        Extract artist names and the real artist id.

        The singer field may be a string, possibly several names separated by
        slashes, or a list of objects with name/id/mid. Always returns a
        (names, real_id) pair.
        """
        singer = item.get("singer") or item.get("artist") or ""
        names: list[str] = []
        real_id: str | None = None
        if isinstance(singer, list):
            for sub in singer:
                if isinstance(sub, dict):
                    if sub.get("name"):
                        names.append(str(sub["name"]))
                    if not real_id and (sub.get("id") or sub.get("mid")):
                        real_id = str(sub.get("id") or sub.get("mid"))
                elif isinstance(sub, str) and sub.strip():
                    names.append(sub.strip())
        else:
            text = str(singer)
            names = [a.strip() for a in text.replace("/", "、").split("、") if a.strip()]
            rid = item.get("singerId") or item.get("singerMid") or item.get("singer_id")
            if rid:
                real_id = str(rid)
        return names, real_id

    def _make_artist(self, source: str, name: str, item_id: str | None = None) -> Artist:
        aid = item_id or f"{source}:{hashlib.md5(name.encode()).hexdigest()[:12]}"
        return Artist(
            item_id=aid,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=aid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    def _make_album(
        self, source: str, album_id: str, name: str, item_id: str | None = None
    ) -> Album:
        aid = item_id or f"{source}:{album_id}"
        return Album(
            item_id=aid,
            provider=self.instance_id,
            name=name,
            provider_mappings={
                ProviderMapping(
                    item_id=aid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    # ------------------------------------------------------------------ #
    # Cache and lookup helpers
    # ------------------------------------------------------------------ #
    def _artist_item_id(self, source: str, name: str) -> str:
        return f"{source}:{hashlib.md5(name.encode()).hexdigest()[:12]}"

    def _register_artist(self, source: str, name: str, real_id: str | None = None) -> Artist:
        aid = self._artist_item_id(source, name)
        self._artist_cache[aid] = {
            "source": source,
            "name": name,
            "real_id": real_id,
        }
        return self._make_artist(source, name, aid)

    def _register_album(self, source: str, name: str, real_id: str | None = None) -> Album:
        aid = f"{source}:{real_id}" if real_id else self._artist_item_id(source, name)
        self._album_cache[aid] = {
            "source": source,
            "name": name,
            "real_id": real_id,
        }
        return self._make_album(source, real_id or name, name, aid)

    async def _fetch_paged(
        self, path: str, params: dict[str, Any], max_items: int = 50
    ) -> list[dict[str, Any]]:
        """
        Page through lxserver list endpoints such as artistSongs or songList/detail.

        The server answers HTTP 500 with {"error": "try max num"} for an
        out-of-range page or when concurrency is exceeded, rather than an empty
        list. Letting that escape made whole playlists look empty even when the
        first page had succeeded, so:
        - each page is retried up to 3 times with backoff to ride out throttling
        - a failure on page 1 is raised, since the caller must know nothing was fetched
        - a failure on a later page ends pagination and the partial result is kept

        Also note that songList/detail ignores page and limit and returns the
        entire list every time, so a short-page check would never terminate.
        Items are deduped by key instead, and a page with no new entries ends it.
        """
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        while len(items) < max_items:
            batch: list[dict[str, Any]] = []
            last_err: Exception | None = None
            for retry in range(3):
                try:
                    batch = self._normalize_list(
                        await self._request(
                            "GET",
                            path,
                            params={**params, "page": page, "limit": 50},
                        )
                    )
                    last_err = None
                    break
                except Exception as err:
                    last_err = err
                    if retry < 2:
                        await asyncio.sleep(0.4 * (2**retry))
            if last_err is not None:
                if page == 1:
                    raise last_err
                LOGGER.debug(
                    "lxmusic: %s page %d failed, treating as end of list (%d kept): %s",
                    path,
                    page,
                    len(items),
                    last_err,
                )
                break
            if not batch:
                break
            added = 0
            for entry in batch:
                key = self._paged_item_key(entry, len(items) + added)
                if key in seen:
                    continue
                seen.add(key)
                items.append(entry)
                added += 1
            if not added:
                # the server ignored pagination, or the list really ended
                break
            if len(batch) < 50:
                break
            page += 1
        return items[:max_items]

    @staticmethod
    def _paged_item_key(entry: Any, fallback_index: int) -> str:
        """Stable dedup key for a paged item; non-dicts fall back to the index so they are never dropped."""
        if not isinstance(entry, dict):
            return f"pos:{fallback_index}"
        for key in ("songmid", "songId", "id", "mid", "bangid", "dissid"):
            val = entry.get(key)
            if val:
                return f"{entry.get('source', '')}:{key}:{val}"
        return f"name:{entry.get('name', '')}|{entry.get('singer', '')}|{entry.get('interval', '')}"

    async def _get_leaderboard_tracks(self, source: str, bangid: str, page: int = 0) -> list[Track]:
        """
        Leaderboard tracks, dispatched from the list:board: virtual playlist.

        The leaderboard endpoint returns a plain list whose entries carry a
        numeric songmid, so playback goes straight through the official source
        for that platform with no prefix stripping.

        The MA page argument must be forwarded: MA calls with page=0,1,2,... and
        always requesting page 1 both truncates boards longer than one page and
        makes MA ask for pages that repeat data. MA is 0-based and the server is
        1-based, hence the +1.
        """
        page_size = 100
        start = page * page_size
        try:
            data = await self._request(
                "GET",
                "/api/music/leaderboard/list",
                params={"source": source, "bangid": bangid, "page": page + 1},
            )
        except Exception as err:
            LOGGER.debug("lxmusic: leaderboard fetch failed %s/%s: %s", source, bangid, err)
            return []
        items: list[dict[str, Any]] = []
        if isinstance(data, dict):
            items = data.get("list", []) or []
        elif isinstance(data, list):
            items = data
        out: list[Track] = []
        for item in items[start : start + page_size]:
            if not isinstance(item, dict):
                continue
            # leaderboard songmids are already numeric, no prefix to strip
            track = await self._parse_track(item, source)
            if track:
                out.append(track)
        return out

    async def _get_songlist_tracks(self, source: str, sl_id: str, page: int = 0) -> list[Track]:
        """
        Square playlist tracks, dispatched from the list:songlist: virtual playlist.

        Fetched through songList/detail; the songmids are numeric and already
        bound to the right platform by the server.
        """
        page_size = 100
        start = page * page_size
        # The track path may run before get_playlist, so make sure the metadata
        # exists here too; a cache hit costs nothing. Otherwise the playlist
        # title degrades to the raw item id.
        if page == 0 and not self._virtual_meta.get(f"list:songlist:{source}:{sl_id}"):
            await self._songlist_meta(source, sl_id)
        try:
            items = await self._fetch_paged(
                "/api/music/songList/detail",
                {"source": source, "id": sl_id},
                max_items=1000,
            )
        except Exception as err:
            LOGGER.debug("lxmusic: square playlist detail failed %s/%s: %s", source, sl_id, err)
            return []
        out: list[Track] = []
        for item in items[start : start + page_size]:
            if not isinstance(item, dict):
                continue
            track = await self._parse_track(item, source)
            if track:
                out.append(track)
        return out

    async def _get_user_lists(self) -> dict[str, Any] | None:
        """
        Fetch the current user's playlists (recent, love, custom) with a TTL cache.

        The cache must expire: without a TTL, playlists created on the lxserver
        side were never discovered by MA. 60s lets the sync task pick up changes
        without putting this on the hot path. A lock keeps concurrent callers
        from issuing the same request.
        """
        now = asyncio.get_event_loop().time()
        if (
            self._user_lists_cache is not None
            and (now - self._user_lists_cache_time) < self._USER_LISTS_CACHE_TTL
        ):
            return self._user_lists_cache
        async with self._user_lists_cache_lock:
            # re-check inside the lock so waiters do not refetch after the winner did
            now = asyncio.get_event_loop().time()
            if (
                self._user_lists_cache is not None
                and (now - self._user_lists_cache_time) < self._USER_LISTS_CACHE_TTL
            ):
                return self._user_lists_cache
            try:
                data = await self._request("GET", "/api/user/list")
            except Exception as err:
                LOGGER.debug("failed to fetch user playlists: %s", err)
                # On failure keep the previous cache: do not refresh the
                # timestamp, and do not drop a usable snapshot over a transient
                # network error.
                return self._user_lists_cache
            if isinstance(data, dict):
                self._user_lists_cache = data
                self._user_lists_cache_time = asyncio.get_event_loop().time()
                LOGGER.debug(
                    "lxmusic: user list cache refreshed, userList entries=%d",
                    len(data.get("userList", []) or []),
                )
            else:
                LOGGER.debug(
                    "lxmusic: /api/user/list returned %s instead of a dict",
                    type(data).__name__,
                )
            return self._user_lists_cache

    async def _collect_virtual_meta(  # noqa: PLR0915
        self, kinds: tuple[str, ...] = ("board", "songlist")
    ) -> list[dict[str, str]]:
        """
        Populate ``_virtual_meta`` for LX leaderboards / square playlists.

        2026-09-06 BUG #80 split: virtual playlists are NOT yielded into the
        MA library any more (no more ``get_library_playlists`` yield of
        ``list:board:*`` / ``list:songlist:*``). They live only in the browse
        tree under ``{instance_id}://playlists/board`` and
        ``{instance_id}://playlists/square/<tag>``.

        This helper is the single source of truth for those virtual playlists:
        it does the same LX server fetch + pic concurrency the old yield block
        did, then writes ``_virtual_meta[item_id]`` so ``get_playlist`` can
        still resolve metadata via the existing ``_virtual_meta`` path.

        Returns: list of ``{"item_id", "name", "pic", "kind"}`` dicts for
        callers to build ``ItemMapping`` entries.

        ``kinds`` controls what to fetch (subset of ``("board", "songlist")``)
        and respects the ``_import_leaderboards`` / ``_import_square`` flags
        (skipped entries are filtered out, no error).
        """
        out: list[dict[str, str]] = []

        # --- leaderboards ---
        if "board" in kinds and getattr(self, "_import_leaderboards", True):
            # Cache guard. Filling boards and covers takes about 25s; without
            # this, every browse click that reached this point re-fetched
            # everything and the UI sat blank long enough to look broken.
            # After the first fill the covers live in _virtual_meta and browse
            # is served from there.
            cached_boards: list[tuple[str, dict[str, Any]]] = [
                (iid, meta)
                for iid, meta in self._virtual_meta.items()
                if isinstance(meta, dict) and meta.get("kind") == "board"
            ]
            if cached_boards:
                # All five sources the server supports must be listed here;
                # omitting one silently leaves its boards out of the listing.
                source_order = {"kg": 0, "kw": 1, "wy": 2, "tx": 3, "mg": 4}

                def _is_hot_cached(name: str) -> bool:
                    return (
                        "飙升" in name or "TOP500" in name or "热歌榜" in name or "新歌榜" in name
                    )

                cached_boards.sort(
                    key=lambda pair: (
                        source_order.get(pair[1].get("source", ""), 99),
                        0 if _is_hot_cached(pair[1].get("name", "")) else 1,
                        pair[1].get("bangid", ""),
                    )
                )
                for iid, meta in cached_boards:
                    out.append(
                        {
                            "item_id": iid,
                            "name": meta.get("name", ""),
                            "pic": meta.get("pic", ""),
                            "kind": "board",
                            "source": meta.get("source", ""),
                        }
                    )
                LOGGER.debug(
                    "lxmusic: leaderboards served from cache, %d entries, no refetch",
                    len(cached_boards),
                )
            else:
                # Cache miss: first fill of boards and covers. By the time a
                # browse subfolder reaches this, the startup prefetch has
                # normally populated _virtual_meta and the branch above applies.
                await self._fill_leaderboards(out)
        elif "board" in kinds:
            LOGGER.debug("lxmusic: leaderboard toggle off, browse entry hidden")

        # --- square playlists (top N per tag) ---
        if "songlist" in kinds and getattr(self, "_import_square", True):
            per_tag = getattr(self, "_LEADERBOARD_TOPN", 5)
            try:
                tags_resp = await self._request("GET", "/api/music/songList/tags")
            except Exception as err:
                LOGGER.debug("lxmusic: playlist categories failed: %s", err)
                tags_resp = None
            if isinstance(tags_resp, dict):
                seen_sl_ids: set[str] = set()
                sl_jobs: list[dict[str, Any]] = []
                for group in tags_resp.get("tags", []) or []:
                    if not isinstance(group, dict):
                        continue
                    tag_list = group.get("list", []) or []
                    for tag in tag_list:
                        if not isinstance(tag, dict):
                            continue
                        tag_id = (tag.get("id") or "").strip()
                        tag_src = (tag.get("source") or "").strip()
                        if not tag_id or not tag_src:
                            continue
                        try:
                            sl_resp = await self._request(
                                "GET",
                                "/api/music/songList/list",
                                params={"source": tag_src, "tagId": tag_id, "page": 1},
                            )
                        except Exception as err:
                            LOGGER.debug(
                                "lxmusic: square playlist list failed %s/%s: %s",
                                tag_src,
                                tag_id,
                                err,
                            )
                            continue
                        if not isinstance(sl_resp, dict):
                            continue
                        picked = 0
                        for sl in sl_resp.get("list", []) or []:
                            if not isinstance(sl, dict) or picked >= per_tag:
                                continue
                            sl_id = str(sl.get("id") or "").strip()
                            if not sl_id or sl_id in seen_sl_ids:
                                continue
                            seen_sl_ids.add(sl_id)
                            picked += 1
                            sl_name = (sl.get("name") or "Untitled Playlist").strip()
                            sl_author = (sl.get("author") or "").strip()
                            display = f"LX ·{tag_id}:{sl_name}"
                            if sl_author:
                                display = f"{display} - {sl_author}"
                            item_id = f"list:songlist:{tag_src}:{sl_id}"
                            self._playlist_cache.setdefault(item_id, [])
                            self._virtual_meta[item_id] = {
                                "name": display,
                                "source": tag_src,
                                "sl_id": sl_id,
                                "img": sl.get("img") or "",
                                "author": sl.get("author") or "",
                                "kind": "songlist",
                                "tag_id": tag_id,
                            }
                            sl_jobs.append(
                                {
                                    "item_id": item_id,
                                    "source": tag_src,
                                    "sl_id": sl_id,
                                    "display": display,
                                    "tag_id": tag_id,
                                    "img": (sl.get("img") or "").strip(),
                                }
                            )

                sl_pic_map: dict[str, str] = {}
                if sl_jobs:
                    sem = asyncio.Semaphore(8)

                    async def _fetch_sl_pic(job: dict[str, Any]) -> tuple[str, str | None]:
                        async with sem:
                            pic = await self._fetch_first_pic(
                                "/api/music/songList/detail",
                                source=job["source"],
                                id=job["sl_id"],
                            )
                            return (job["item_id"], pic)

                    results = await asyncio.gather(
                        *(_fetch_sl_pic(j) for j in sl_jobs),
                        return_exceptions=True,
                    )
                    for r in results:
                        if isinstance(r, BaseException):
                            LOGGER.debug("lxmusic: square playlist cover lookup failed: %s", r)
                            continue
                        item_id, pic = r
                        if pic:
                            sl_pic_map[item_id] = pic

                LOGGER.debug(
                    "lxmusic: browse preparing %d LX square playlists (%d covers)",
                    len(sl_jobs),
                    len(sl_pic_map),
                )
                for job in sl_jobs:
                    item_id = job["item_id"]
                    display = job["display"]
                    pic = sl_pic_map.get(item_id) or job.get("img", "")
                    out.append(
                        {
                            "item_id": item_id,
                            "name": display,
                            "pic": pic,
                            "kind": "songlist",
                            "tag_id": job["tag_id"],
                        }
                    )
        elif "songlist" in kinds:
            LOGGER.debug("lxmusic: square toggle off, browse entry hidden")

        return out

    async def _fill_leaderboards(self, out: list[dict[str, str]]) -> None:  # noqa: PLR0915
        """
        Fill leaderboards and their covers into out and _virtual_meta on first use.

        Split out of _collect_virtual_meta so it is reachable from the cache-miss
        branch; inlining it behind a duplicated condition left the fill
        unreachable and browse returned no boards at all.

        Boards are listed flat, without a per-platform folder, and named
        "[platform] board" so that identically named boards from different
        platforms stay distinguishable.
        """
        leaderboard_sources: tuple[str, ...] = ("kg", "kw", "wy", "tx", "mg")
        # Short display prefixes for boards. These are deliberately shorter than
        # the full names in SOURCE_NAMES, so they are kept in their own table;
        # add new platforms to both.
        source_label: dict[str, str] = {
            "kg": "Kugou",
            "kw": "Kuwo",
            "wy": "NetEase",
            "tx": "QQ",
            "mg": "Migu",
        }
        boards_resp_map: dict[str, dict[str, Any]] = {}
        for src in leaderboard_sources:
            try:
                resp = await self._request(
                    "GET",
                    "/api/music/leaderboard/boards",
                    params={"source": src},
                )
            except Exception as err:
                LOGGER.debug("lxmusic: leaderboard categories failed for %s: %s", src, err)
                resp = None
            if isinstance(resp, dict):
                boards_resp_map[src] = resp
            # stagger requests to reduce the chance of hitting the server mutex
            await asyncio.sleep(0.3)

        all_jobs: list[dict[str, str]] = []
        for src in leaderboard_sources:
            resp = boards_resp_map.get(src)
            if not resp:
                continue
            src_actual = (resp.get("source") or src).strip()
            src_zh = source_label.get(src_actual, src_actual)
            for board in resp.get("list") or []:
                if not isinstance(board, dict):
                    continue
                bangid = str(board.get("bangid") or "").strip()
                bname = (board.get("name") or "Untitled Chart").strip()
                if not bangid:
                    continue
                all_jobs.append(
                    {
                        "source": src_actual,
                        "bangid": bangid,
                        "bname": bname,
                        # localized platform prefix, see the table above
                        "name": f"[{src_zh}] {bname}",
                    }
                )

        board_pic_map: dict[str, str] = {}
        # Skip boards that come back empty. Some boards return an empty list or
        # HTTP 500 on the server, and importing them yields playlists the user
        # then finds empty. Probing the first page once both detects that and
        # supplies the cover, so it costs no extra requests.
        skipped_empty: list[tuple[str, str]] = []  # (bangid, name), for logging
        skipped_keys: set[str] = set()
        if all_jobs:
            # Cover fetches run one at a time. A wider semaphore trips the server
            # mutex at this scale and returns empty covers; serial is slower but
            # reliable, roughly 150ms each and about 25s in total, and the
            # startup prefetch keeps that cost out of the user's way.
            sem = asyncio.Semaphore(1)

            async def _probe_board(
                job: dict[str, str],
            ) -> tuple[str, list[dict[str, Any]]]:
                """Return (key, items); an empty or failed items list skips the board."""
                key = f"{job['source']}:{job['bangid']}"
                async with sem:
                    for retry in range(3):
                        try:
                            data = await self._request(
                                "GET",
                                "/api/music/leaderboard/list",
                                params={
                                    "source": job["source"],
                                    "bangid": job["bangid"],
                                    "page": 1,
                                },
                            )
                            items = data.get("list", []) if isinstance(data, dict) else []
                            if not isinstance(items, list):
                                items = []
                            return (key, items)
                        except Exception as err:
                            if retry < 2:
                                await asyncio.sleep(0.5 * (2**retry))
                            else:
                                LOGGER.debug(
                                    "lxmusic: board probe %s failed after 3 tries: %s",
                                    key,
                                    err,
                                )
                                return (key, [])
                    return (key, [])

            results = await asyncio.gather(
                *(_probe_board(j) for j in all_jobs),
                return_exceptions=True,
            )
            for j, r in zip(all_jobs, results, strict=True):
                if isinstance(r, BaseException):
                    LOGGER.debug("lxmusic: board probe error: %s", r)
                    skipped_keys.add(f"{j['source']}:{j['bangid']}")
                    skipped_empty.append((j["bangid"], j["bname"]))
                    continue
                key, items = r
                if not items:
                    skipped_keys.add(key)
                    skipped_empty.append((j["bangid"], j["bname"]))
                    continue
                # cover from the first track, preferring item.pic or al.picUrl
                first = items[0]
                if isinstance(first, dict):
                    pic = (
                        first.get("pic")
                        or (
                            (first.get("al") or {}).get("picUrl")
                            if isinstance(first.get("al"), dict)
                            else ""
                        )
                        or first.get("img")
                        or ""
                    ).strip()
                    if pic:
                        board_pic_map[key] = pic

        # drop boards that came back empty
        if skipped_empty:
            LOGGER.info(
                "lxmusic: skipped %d empty boards (empty list or 500): %s",
                len(skipped_empty),
                ", ".join(f"{bid}/{nm}" for bid, nm in skipped_empty[:5])
                + ("..." if len(skipped_empty) > 5 else ""),
            )
        LOGGER.info(
            "lxmusic: first fill of %d LX leaderboards (5 sources, %d covers, %d empty skipped)",
            len(all_jobs),
            len(board_pic_map),
            len(skipped_empty),
        )
        for job in all_jobs:
            bangid = job["bangid"]
            src = job["source"]
            key = f"{src}:{bangid}"
            if key in skipped_keys:
                # Empty board or a server error: do not import it.
                continue
            item_id = f"list:board:{src}:{bangid}"
            self._playlist_cache.setdefault(item_id, [])
            self._virtual_meta[item_id] = {
                "name": job["name"],
                "source": src,
                "bangid": bangid,
                "kind": "board",
                "pic": board_pic_map.get(key, ""),
            }
            out.append(
                {
                    "item_id": item_id,
                    "name": job["name"],
                    "pic": board_pic_map.get(key, ""),
                    "kind": "board",
                    "source": src,
                }
            )

    async def _fetch_first_pic(self, path: str, **params) -> str | None:
        """
        Fetch the cover of the first entry of an lxserver list endpoint.

        Leaderboards and square playlists often have an empty top-level image
        field, which left synced virtual playlists without artwork. Fetching the
        first page and taking the first track's cover fills that in.

        Used for leaderboards (leaderboard/list) and square playlists
        (songList/detail). User playlists do not need it, since their songs are
        already in hand. When a track has no cover the caller should fall back
        to the playlist's own image.

        Any failure returns None and leaves the decision to the caller, so the
        surrounding yield flow is unaffected.
        """
        try:
            resp = await self._request("GET", path, params=params)
        except Exception as err:
            LOGGER.debug("lxmusic: fetch_first_pic %s failed: %s", path, err)
            return None
        if not isinstance(resp, dict):
            return None
        items = resp.get("list") or []
        if not items:
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        url = (first.get("pic") or first.get("img") or "").strip()
        return url or None

    async def _get_square_tags_cached(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Fetch the square tag API once and reuse the result.

        Returns the raw tag list plus the hot tag list. Both the top level and
        the sub level of browse share this cache to avoid repeat requests.
        Since the server does not filter songList/list by tag, expanding parent
        tags into sub-tags is pointless; the hotTag field the API returns is
        used as the top-level categories instead.
        """
        cached = getattr(self, "_square_tags_cache", None)
        if cached is not None:
            return cached
        resp = await self._request("GET", "/api/music/songList/tags")
        raw = self._normalize_list(resp.get("tags") if isinstance(resp, dict) else resp)
        hot_tags: list[dict[str, Any]] = []
        if isinstance(resp, dict):
            for h in self._normalize_list(resp.get("hotTag")):
                if isinstance(h, dict):
                    hot_tags.append(h)
        self._square_tags_cache = (raw, hot_tags)
        LOGGER.info(
            "lxmusic: cached square tags | parent tags=%d hotTags=%d",
            len(raw),
            len(hot_tags),
        )
        return self._square_tags_cache

    async def _fetch_square_all_items(self) -> list[ItemMapping]:
        """
        Fetch every square playlist, sort by track count and keep the top 200.

        Three server behaviours shape this:
        - songList/list ignores the tag argument, so any tag-based slicing
          returns the same full set and a single flat fetch is the right shape
        - the endpoint is genuinely paginated with a fixed page size, so one
          page is only a fraction of the catalog
        - the server serializes concurrent requests, so fetching pages in
          parallel returns data for one page and empty lists for the rest

        Hence pages are fetched sequentially, each retried a few times with
        backoff, stopping at the first empty page. The page count is estimated
        from the total reported on page 1, results are deduped across pages,
        sorted by track count and truncated.
        """
        page_size = 36  # the server hardcodes 36 per page and ignores limit

        # first page: total plus the starting batch
        first_resp = await self._request(
            "GET",
            "/api/music/songList/list",
            params={
                "source": self._default_source,
                "page": 1,
                "limit": page_size,
            },
        )
        first_lists = self._normalize_list(first_resp)
        server_total = 0
        if isinstance(first_resp, dict):
            try:
                server_total = int(first_resp.get("total") or 0)
            except TypeError, ValueError:
                server_total = 0

        # estimate the page count from the reported total, with a cap
        # when the total field is missing, fall back to a safe cap
        max_pages = min((server_total // page_size) + 2, 50) if server_total > 0 else 50

        all_lists: list[dict[str, Any]] = list(first_lists)

        # fetch the remaining pages sequentially
        for p in range(2, max_pages + 1):
            page_lists: list[dict[str, Any]] = []
            for retry in range(3):
                try:
                    resp = await self._request(
                        "GET",
                        "/api/music/songList/list",
                        params={
                            "source": self._default_source,
                            "page": p,
                            "limit": page_size,
                        },
                    )
                    page_lists = self._normalize_list(resp)
                    break
                except Exception as err:
                    LOGGER.debug(
                        "lxmusic: square playlist page=%d retry %d: %s",
                        p,
                        retry + 1,
                        err,
                    )
                    await asyncio.sleep(0.5 * (2**retry))
            if not page_lists:
                # empty list means the end, or the retries failed: stop here
                break
            all_lists.extend(page_lists)

        # dedupe across pages and build ItemMappings; pages can overlap
        out: list[ItemMapping] = []
        seen: set[str] = set()
        for sl in all_lists:
            sl_id_raw = sl.get("id") or sl.get("listId") or sl.get("playId")
            sl_id = str(sl_id_raw) if sl_id_raw else ""
            if not sl_id or sl_id in seen:
                continue
            seen.add(sl_id)
            sl_name = sl.get("name") or sl.get("listName") or sl_id
            sl_source = sl.get("source") or self._default_source
            sl_item_id = f"sl:{sl_source}:{sl_id}"
            sl_total = 0
            with suppress(TypeError, ValueError):
                sl_total = int(sl.get("total") or 0)
            self._square_meta[sl_item_id] = {
                "source": sl_source,
                "id": sl_id,
                "name": sl_name,
                "img": (
                    sl.get("img")
                    or sl.get("pic")
                    or sl.get("image")
                    or sl.get("cover")
                    or sl.get("coverImgUrl")
                ),
                "total": sl_total,
            }
            sl_img = (self._square_meta[sl_item_id].get("img") or "").strip()
            sl_image = (
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sl_img,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
                if sl_img
                else None
            )
            out.append(
                ItemMapping(
                    media_type=MediaType.PLAYLIST,
                    item_id=sl_item_id,
                    provider=self.instance_id,
                    name=sl_name,
                    image=sl_image,
                )
            )
        # sort by track count and keep the top slice
        out.sort(
            key=lambda im: self._square_meta.get(im.item_id, {}).get("total", 0),
            reverse=True,
        )
        LOGGER.info(
            "lxmusic: square playlists | %d fetched after dedup, top %d, server_total=%d",
            len(out),
            min(200, len(out)),
            server_total,
        )
        return out[:200]

    async def _fetch_square_sub_tag_items(
        self,
        sub_tid: str,
        tag_name: str = "",
    ) -> list[ItemMapping]:
        """
        Fetch playlists for one sub tag, deduped, cached and with covers.

        Kept for the browse square/{id} sub level. Because the server does not
        filter by tag, every sub tag returns the same content, so the display
        name carries a [tag] prefix to make it obvious which category a listing
        came from instead of looking like a stuck view. The cached name stays
        unmodified.

        Currently unused for the same reason; the top level browse path uses
        _fetch_square_all_items. Retained as an extension point.
        """
        lists: list[dict[str, Any]] = []
        for param_name in ("tag", "id"):
            lists = self._normalize_list(
                await self._request(
                    "GET",
                    "/api/music/songList/list",
                    params={
                        param_name: sub_tid,
                        "source": self._default_source,
                        "page": 1,
                        "limit": 50,
                    },
                )
            )
            if lists:
                break
        out: list[ItemMapping] = []
        seen_local: set[str] = set()
        for sl in lists:
            sl_id_raw = sl.get("id") or sl.get("listId") or sl.get("playId")
            sl_id = str(sl_id_raw) if sl_id_raw else ""
            if not sl_id or sl_id in seen_local:
                continue
            seen_local.add(sl_id)
            sl_name = sl.get("name") or sl.get("listName") or sl_id
            sl_source = sl.get("source") or self._default_source
            sl_item_id = f"sl:{sl_source}:{sl_id}"
            # Prefix the display name with the hot tag, separated by | so it
            # cannot collide with brackets in a playlist's own name. The cache
            # keeps the original name, which get_playlist overwrites with real
            # data anyway.
            display_name = f"[{tag_name}] {sl_name}" if tag_name else sl_name
            self._square_meta[sl_item_id] = {
                "source": sl_source,
                "id": sl_id,
                "name": sl_name,
                "img": (
                    sl.get("img")
                    or sl.get("pic")
                    or sl.get("image")
                    or sl.get("cover")
                    or sl.get("coverImgUrl")
                ),
            }
            sl_img = (self._square_meta[sl_item_id].get("img") or "").strip()
            sl_image = (
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=sl_img,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
                if sl_img
                else None
            )
            out.append(
                ItemMapping(
                    media_type=MediaType.PLAYLIST,
                    item_id=sl_item_id,
                    provider=self.instance_id,
                    name=display_name,
                    image=sl_image,
                )
            )
        return out

    @staticmethod
    def _virtual_translation_key(pid: str) -> str | None:
        """
        Return the translation slug for virtual playlists, None for real user ones.

        The recent-play and love lists have stable ids, so their names can be
        localized through media.playlist.<slug>.name instead of freezing one
        language into the stored name.
        """
        return {"__default__": "recent_played", "__love__": "love"}.get(pid)

    @staticmethod
    def _iter_user_playlists(data: dict[str, Any]) -> list[tuple[str, str, list[dict[str, Any]]]]:
        """
        Iterate user playlists, returning (id, name, songs) triples.

        The server's shape differs per section: defaultList and loveList are
        bare song arrays rather than {id, name, list} objects, so they have to
        be wrapped as virtual playlists with synthetic ids that cannot collide
        with real ones. userList entries are proper playlist objects.

        Treating the first two as objects, as an earlier version did, silently
        dropped them and the recent-play and love playlists never reached MA.
        """
        out: list[tuple[str, str, list[dict[str, Any]]]] = []
        # defaultList is the recent-play list and loveList the love list; MA shows the latter as a favorites playlist
        for key, virtual_id, virtual_name in (
            ("defaultList", "__default__", "Recently Played"),
            ("loveList", "__love__", "LX Favorites"),
        ):
            lst = data.get(key)
            if isinstance(lst, list):
                # a bare song array, wrap it as a virtual playlist
                out.append((virtual_id, virtual_name, lst))
            elif isinstance(lst, dict):
                # older servers may still nest {id, name, list}
                out.append((lst.get("id", key), lst.get("name", virtual_name), lst.get("list", [])))
        for lst in data.get("userList", []) or []:
            if isinstance(lst, dict):
                pl_id = lst.get("id")
                if not pl_id:
                    # skip entries without an id so they cannot clash with the virtual ones
                    continue
                out.append(
                    (
                        pl_id,
                        lst.get("name", "Untitled Playlist"),
                        lst.get("list", []),
                    )
                )
        return out

    @staticmethod
    def _extract_url(result: Any) -> str | None:
        if isinstance(result, str):
            return result or None
        if not isinstance(result, dict):
            return None
        for key in ("url", "playUrl", "src"):
            val = result.get(key)
            if isinstance(val, str) and val:
                return val
        data = result.get("data", result)
        if isinstance(data, dict):
            return data.get("url") or data.get("playUrl") or data.get("src")
        if isinstance(data, str):
            return data or None
        return None

    @staticmethod
    def _pick_content_type(
        result: Any,
        url: str,
        quality: str,
    ) -> Any:
        """
        Determine the audio content type lxserver actually returned.

        Hardcoding MP3 was wrong: with the server set to highest quality the URL
        is often flac, and labeling those bytes audio/mpeg makes clients decode
        flac with an mp3 decoder, which sounds distorted.

        In order:
        1) the type/quality field in the server response, usually under
           result.data.type or result.type, holding flac/320k/128k
        2) the URL suffix, such as .flac, .mp3, .m4a, .ogg or .opus
        3) MP3 as the default, which is correct for the 128k and 320k tiers
        """
        # first: the type field in the server response
        type_hint = ""
        if isinstance(result, dict):
            for container in (
                result,
                result.get("data") if isinstance(result.get("data"), dict) else {},
            ):
                t = container.get("type") if isinstance(container, dict) else None
                if isinstance(t, str) and t:
                    type_hint = t.lower()
                    break
        if not type_hint:
            type_hint = (quality or "").lower()

        if type_hint in ("flac", "flac24bit", "flac24", "lossless", "ape", "wav"):
            return ContentType.FLAC
        if type_hint in ("320k", "320", "mp3", "128k", "128"):
            return ContentType.MP3
        if type_hint in ("aac", "m4a", "alac"):
            return ContentType.AAC if type_hint == "aac" else ContentType.M4A
        if type_hint in ("ogg", "vorbis"):
            return ContentType.OGG
        if type_hint == "opus":
            return ContentType.OPUS

        # second: the URL suffix
        u = (url or "").lower().split("?", 1)[0]
        if u.endswith(".flac"):
            return ContentType.FLAC
        if u.endswith(".m4a"):
            return ContentType.M4A
        if u.endswith(".aac"):
            return ContentType.AAC
        if u.endswith(".ogg"):
            return ContentType.OGG
        if u.endswith(".opus"):
            return ContentType.OPUS
        if u.endswith(".mp3"):
            return ContentType.MP3

        # final default
        return ContentType.MP3

    @staticmethod
    def _infer_metadata_content_type(item: dict[str, Any], source: str) -> Any:  # noqa: PLR0911
        """
        Infer the content type to advertise from the fields an item provides.

        Advertising MP3 for everything made the bridge always send audio/mpeg.
        Look at what the item actually carries:
        - a single type field with flac/320k/128k: use it
        - a types array, present on some server versions: prefer flac
        - a quality field, possibly nested in meta
        - the suffix of a preview URL in the item
        - none of the above: the platform default, preferring flac since
          highest-quality settings usually return it
        """
        # 1) single type value
        t = item.get("type")
        if isinstance(t, str) and t:
            tl = t.lower()
            if "flac" in tl:
                return ContentType.FLAC
            if "320" in tl or "128" in tl:
                return ContentType.MP3
            if "aac" in tl or "m4a" in tl:
                return ContentType.M4A
            if "ogg" in tl:
                return ContentType.OGG
            if "opus" in tl:
                return ContentType.OPUS

        # 2) list of available qualities
        for key in ("types", "qualities", "_quality", "qualityList"):
            arr = item.get(key)
            if isinstance(arr, list) and arr:
                # prefer flac when offered
                for q in arr:
                    if isinstance(q, str) and "flac" in q.lower():
                        return ContentType.FLAC
                for q in arr:
                    if isinstance(q, str) and ("320" in q.lower() or "128" in q.lower()):
                        return ContentType.MP3
                break

        # 3) a lone quality field
        q = item.get("quality") or item.get("_quality")
        if isinstance(q, str) and q:
            ql = q.lower()
            if "flac" in ql:
                return ContentType.FLAC
            if "320" in ql or "128" in ql or "mp3" in ql:
                return ContentType.MP3
            if "m4a" in ql or "aac" in ql:
                return ContentType.M4A

        # 4) preview URL suffix
        for key in ("previewUrl", "preview_url", "trialUrl", "_preview"):
            val = item.get(key)
            if isinstance(val, str) and val:
                u = val.lower().split("?", 1)[0]
                if u.endswith(".flac"):
                    return ContentType.FLAC
                if u.endswith(".m4a"):
                    return ContentType.M4A
                if u.endswith(".aac"):
                    return ContentType.AAC
                if u.endswith(".ogg"):
                    return ContentType.OGG
                if u.endswith(".opus"):
                    return ContentType.OPUS
                if u.endswith(".mp3"):
                    return ContentType.MP3
                break

        # 5) infer the platform default, preferring flac when the server is set to highest quality
        source_default_flac = {
            "kw",
            "kg",
            "tx",
            "wy",
            "qq",
            "netease",
            "163",
            "mg",
        }
        if source in source_default_flac:
            return ContentType.FLAC
        return ContentType.MP3

    async def _parse_track(self, item: dict[str, Any], source: str) -> Track | None:
        """Convert a raw LX Music item dict into a Track."""
        song_id = self._item_song_id(item)
        if not song_id:
            return None
        name = item.get("name") or item.get("songName") or "Unknown Song"
        singer_names, artist_real_id = self._singer_info(item)
        artist_name = singer_names[0] if singer_names else "Unknown Artist"
        album_name = item.get("albumName") or item.get("album") or "Unknown Album"
        album_real_id = self._album_id(item)
        duration = item.get("interval") or item.get("duration") or item.get("time") or 0
        if isinstance(duration, str):
            duration = self._parse_duration(duration)

        # The server resolves prefixed and bare ids through different paths: a
        # prefixed id goes through the configured custom source, which may be
        # down, while a bare numeric id uses the platform's official endpoint.
        # Search hits already carry a numeric songmid and play fine, but songs in
        # user playlists have prefixed ids and stall on the custom source.
        # So store a copy in the raw cache with the prefix stripped as songmid,
        # which lets get_stream_details use it without caring about id format.
        # The track's own item_id keeps the original prefixed form so existing
        # library entries are unaffected; only the cached copy changes.
        prefix = f"{source}_"
        if (
            song_id.startswith(prefix)
            and not item.get("songmid")
            and song_id[len(prefix) :].isdigit()
        ):
            raw_for_cache: dict[str, Any] = dict(item)
            raw_for_cache["songmid"] = song_id[len(prefix) :]
            LOGGER.debug(
                "lxmusic: stripped songmid=%s added to raw (item.id=%s)",
                raw_for_cache["songmid"],
                song_id,
            )
        else:
            raw_for_cache = item

        artist_aid = self._artist_item_id(source, artist_name)
        album_aid = (
            f"{source}:{album_real_id}"
            if album_real_id
            else self._artist_item_id(source, album_name)
        )
        # register artist/album metadata so detail lookups can resolve them later
        self._artist_cache.setdefault(
            artist_aid,
            {"source": source, "name": artist_name, "real_id": artist_real_id},
        )
        self._album_cache.setdefault(
            album_aid,
            {"source": source, "name": album_name, "real_id": album_real_id},
        )

        track = Track(
            item_id=f"{source}:{song_id}",
            provider=self.instance_id,
            name=name,
            duration=duration,
            artists=[
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=artist_aid,
                    provider=self.instance_id,
                    name=artist_name,
                )
            ],
            album=ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=album_aid,
                provider=self.instance_id,
                name=album_name,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=f"{source}:{song_id}",
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    # audio_format must not be hardcoded either: the bridge derives
                    # the response Content-Type from it, and labeling flac bytes as
                    # mp3 makes clients decode them with the wrong decoder. Infer
                    # the quality from the item, defaulting to FLAC, since with
                    # highest quality most platforms return flac and the actual URL
                    # suffix is checked later as a backstop.
                    audio_format=AudioFormat(
                        content_type=self._infer_metadata_content_type(item, source),
                    ),
                    available=True,
                )
            },
        )
        # file under its album so opening that album reuses already parsed tracks
        self._album_tracks.setdefault(album_aid, [])
        if track.item_id not in {t.item_id for t in self._album_tracks[album_aid]}:
            self._album_tracks[album_aid].append(track)

        pic = item.get("img") or item.get("pic") or item.get("image") or item.get("cover")
        if pic:
            track.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=pic,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        # cache the Track and its raw item for get_track / get_stream_details
        if hasattr(self, "_track_cache"):
            self._track_cache[track.item_id] = track
        if hasattr(self, "_raw_cache"):
            self._raw_cache[track.item_id] = raw_for_cache
        return track

    # ------------------------------------------------------------------ #
    # Lyrics, via the lxserver lyric endpoint
    # ------------------------------------------------------------------ #
    async def _maybe_fetch_lyrics(self, track: Track, raw: dict[str, Any] | None = None) -> None:
        """
        Fetch lyrics and attach them to the track.

        - skip tracks that already have lyrics
        - use the lyric cache when it has an entry, including a cached None
        - failures and missing lyrics are silent apart from a debug log, so
          get_track never fails over lyrics
        """
        if track.metadata and track.metadata.lrc_lyrics:
            return
        cache_key = track.item_id
        cached_lrc = getattr(self, "_lyrics_cache", {}).get(cache_key, "__missing__")
        if cached_lrc != "__missing__":
            if cached_lrc:
                self._set_track_lrc(track, cached_lrc)
            return
        # cache miss: ask the server
        raw_item = raw if raw is not None else self._raw_cache.get(cache_key)
        try:
            lrc = await self._fetch_lyrics(cache_key, raw=raw_item)
        except Exception as err:
            LOGGER.debug("lxmusic: lyric fetch error track=%s err=%s", cache_key, err)
            lrc = None
        if not hasattr(self, "_lyrics_cache"):
            self._lyrics_cache = {}
        # cache both hits and misses, so a track without lyrics is not refetched
        self._lyrics_cache[cache_key] = lrc
        if lrc:
            self._set_track_lrc(track, lrc)

    @staticmethod
    def _set_track_lrc(track: Track, lrc: str) -> None:
        """
        Attach lrc text to the track metadata.

        The MA lyric controller reads these fields first, so filling both keeps
        every client able to render them. The lyrics field is normalized.
        """
        if not track.metadata:
            track.metadata = MediaItemMetadata()
        track.metadata.lrc_lyrics = lrc
        track.metadata.lyrics = lrc

    async def _fetch_lyrics(
        self,
        prov_track_id: str,
        *,
        raw: dict[str, Any] | None = None,
    ) -> str | None:
        """
        Fetch lyrics from the lxserver GET lyric endpoint.

        Returns lrc text with timestamps, or None when there are none or the
        call failed.

        GET rather than POST: the POST handler awaits the value returned by
        musicSdk.getLyric, which is a wrapper object holding the real promise
        rather than a promise itself. Awaiting it returns the wrapper, JSON
        serializes its promise field to {}, and the result never carries lyrics.
        The GET handler awaits the inner promise and answers with the lyric
        object, so it works.

        Extra query fields some sources need:
        - wy: songmid, name, singer, interval (interval as "MM:SS")
        - kg: songmid, name, hash, interval ("MM:SS")
        - mg: songmid, copyrightId, lrcUrl, mrcUrl, trcUrl, sent when present
        - tx/kw: songmid, interval optional
        """
        source, song_id = self._split_id(prov_track_id)
        raw_item = raw or {}
        singer = self._extract_singer_for_lyric(raw_item) if raw_item else ""
        name = (raw_item.get("name") or raw_item.get("songName") or "") if raw_item else ""
        interval_raw = (
            (raw_item.get("interval") or raw_item.get("duration") or raw_item.get("time") or "")
            if raw_item
            else ""
        )
        hash_val = (raw_item.get("hash") or "") if raw_item else ""

        # interval must be a "MM:SS" string, since the source SDKs split it as
        # text. Search results already provide that form; convert bare seconds.
        interval_str = _normalize_lx_interval(interval_raw)

        params: dict[str, Any] = {
            "source": source,
            "songmid": song_id,
            "interval": interval_str,
        }
        if name:
            params["name"] = name
        if singer:
            params["singer"] = singer
        if source == "kg" and hash_val:
            params["hash"] = hash_val

        try:
            resp = await self._request(
                "GET",
                "/api/music/lyric",
                params=params,
                timeout=8.0,
            )
        except Exception as err:
            LOGGER.debug(
                "lxmusic: lyric GET error track=%s params=%s err=%s",
                prov_track_id,
                params,
                err,
            )
            return None

        if not isinstance(resp, dict):
            LOGGER.debug(
                "lxmusic: lyric response not a dict track=%s type=%s",
                prov_track_id,
                type(resp).__name__,
            )
            return None

        # the endpoint returns {lyric, tlyric, ...} or a plain text body
        return self._extract_lrc_from_payload(resp, source, song_id)

    @staticmethod
    def _extract_lrc_from_payload(payload: dict[str, Any], source: str, song_id: str) -> str | None:
        """
        Pull lrc text out of a lyric response payload.

        Field names vary by source, so accept data.lyric, data.lrc, data.lyrics,
        or the same names un-nested. Plain text containing timestamps is taken
        as lrc.
        """
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        lrc_raw = data.get("lyric") or data.get("lrc") or data.get("lyrics") or ""
        if not isinstance(lrc_raw, str) or not lrc_raw.strip():
            LOGGER.debug(
                "lxmusic: lyric endpoint returned no text source=%s songmid=%s payload=%s",
                source,
                song_id,
                str(payload)[:400] if isinstance(payload, dict) else payload,
            )
            return None
        LOGGER.debug(
            "lxmusic: lyric fetched source=%s songmid=%s lrc_len=%d",
            source,
            song_id,
            len(lrc_raw),
        )
        return lrc_raw

    @staticmethod
    def _extract_singer_for_lyric(item: dict[str, Any]) -> str:
        """
        Extract a singer string from a raw MusicInfo.

        Same normalization as the cover backfill, which accepts a list, a string
        or a dict, but kept separate so the two do not become coupled.
        """
        singer_raw = item.get("singer") or ""
        if isinstance(singer_raw, list):
            if singer_raw and isinstance(singer_raw[0], dict):
                return str(singer_raw[0].get("name", "") or "")
            if singer_raw and isinstance(singer_raw[0], str):
                return str(singer_raw[0])
            return ""
        if isinstance(singer_raw, str):
            return singer_raw.split("/")[0].split("、")[0].strip()
        return ""

    async def _enrich_playlist_pics(self, items: list[dict[str, Any]]) -> None:  # noqa: PLR0915
        """
        Backfill covers and album info for user playlist tracks, in place.

        The user list endpoint returns no cover or album fields, unlike search
        results and square playlists, so tracks in the user's own playlists
        showed without artwork and under an unknown album. Re-searching by name
        and artist recovers them; the first hit whose duration is within a few
        seconds is taken, since same-name matches are often covers or remixes.

        Results are cached per source, name, singer and interval to avoid repeat
        searches, and a handful of searches run concurrently rather than
        serially.
        """
        if not items:
            return
        sem = asyncio.Semaphore(5)

        def _extract_singer(item: dict[str, Any]) -> str:
            singer_raw = item.get("singer") or ""
            if isinstance(singer_raw, list):
                if singer_raw and isinstance(singer_raw[0], dict):
                    return singer_raw[0].get("name", "") or ""
                if singer_raw and isinstance(singer_raw[0], str):
                    return str(singer_raw[0])
                return ""
            return str(singer_raw).split("/")[0].split("、")[0].strip()

        async def _enrich_one(item: dict[str, Any]) -> None:  # noqa: PLR0915
            src = item.get("source") or self._default_source
            name = (item.get("name") or "").strip()
            singer_key = _extract_singer(item)
            if not name:
                return
            need_pic = not (
                item.get("img") or item.get("pic") or item.get("image") or item.get("cover")
            )
            need_album = not (item.get("albumName") or item.get("album")) or not self._album_id(
                item
            )
            if not need_pic and not need_album:
                return
            # original duration, used to reject same-name covers
            item_interval = item.get("interval") or item.get("duration") or item.get("time") or ""
            expected_seconds = (
                item_interval
                if isinstance(item_interval, int)
                else self._parse_duration(str(item_interval))
            )
            cache_key = (src, name, singer_key, expected_seconds)
            if cache_key in self._pic_enrich_cache:
                cached = self._pic_enrich_cache[cache_key]
                if isinstance(cached, dict):
                    if need_pic and cached.get("pic"):
                        item["pic"] = cached["pic"]
                    if need_album and cached.get("album_name"):
                        item["albumName"] = cached["album_name"]
                        if cached.get("album_id"):
                            item["albumId"] = cached["album_id"]
                return
            async with sem:
                keyword = f"{name} {singer_key}".strip()
                try:
                    hits = await self._search_source(src, keyword, page=1, page_size=10)
                except Exception as err:
                    LOGGER.debug(
                        "lxmusic: user playlist metadata backfill failed %s/%s/%s: %s",
                        src,
                        name,
                        singer_key,
                        err,
                    )
                    self._pic_enrich_cache[cache_key] = None
                    return
                picked_pic: str | None = None
                picked_album_name: str | None = None
                picked_album_id: str | None = None
                fallback_pic: str | None = None  # cover to use if no duration matches
                for hit in hits:
                    pic = hit.get("img") or hit.get("pic") or hit.get("image") or hit.get("cover")
                    aname = hit.get("albumName") or hit.get("album")
                    aid = hit.get("albumId") or hit.get("albumid") or hit.get("album_id")
                    if not pic and not aname:
                        continue
                    # duration check
                    hit_interval = hit.get("interval") or hit.get("duration") or ""
                    hit_seconds = self._parse_duration(str(hit_interval)) if hit_interval else 0
                    interval_ok = (
                        expected_seconds <= 0
                        or hit_seconds <= 0
                        or abs(hit_seconds - expected_seconds) <= 5
                    )
                    if interval_ok:
                        if not picked_pic and pic:
                            picked_pic = pic
                        if not picked_album_name and aname:
                            picked_album_name = aname
                            picked_album_id = str(aid) if aid else None
                        if picked_pic and picked_album_name:
                            break
                    # duration differs but the cover is usable as a fallback
                    elif not fallback_pic and pic:
                        fallback_pic = pic
                # write back in place
                if need_pic:
                    final_pic = picked_pic or fallback_pic
                    if final_pic:
                        item["pic"] = final_pic
                if need_album and picked_album_name:
                    item["albumName"] = picked_album_name
                    if picked_album_id:
                        item["albumId"] = picked_album_id
                # cache None on a miss too, so it is not re-searched every time
                if picked_pic or picked_album_name or fallback_pic:
                    self._pic_enrich_cache[cache_key] = {
                        "pic": picked_pic or fallback_pic,
                        "album_name": picked_album_name,
                        "album_id": picked_album_id,
                    }
                else:
                    self._pic_enrich_cache[cache_key] = None
                if picked_pic or picked_album_name:
                    LOGGER.debug(
                        "lxmusic: user playlist metadata backfill %s/%s/%s -> pic=%s album=%s/%s",
                        src,
                        name,
                        singer_key,
                        bool(item.get("pic")),
                        item.get("albumName"),
                        item.get("albumId"),
                    )

        await asyncio.gather(*(_enrich_one(it) for it in items))

    async def _resolve_songmid_for_src(
        self,
        src: str,
        name: str,
        singer: str,
        expected_interval: str | int | None = None,
    ) -> str | None:
        """
        Resolve a track's songmid on another platform by re-searching.

        The playback endpoint only uses custom sources within one platform and
        does not fall back across platforms, so when a platform's custom source
        is down the client has to find the real songmid elsewhere.

        Taking the first search hit is not good enough: the same name often
        matches a cover, a remix from another album, or a different artist, all
        of which play the wrong recording. When the original duration is known,
        a hit must be within a few seconds of it or it is discarded.

        Results are cached per source, name, singer and interval.
        """
        cache_key = (src, name, singer, expected_interval or "")
        if cache_key in self._songmid_resolve_cache:
            return self._songmid_resolve_cache[cache_key]
        if not name:
            self._songmid_resolve_cache[cache_key] = None
            return None
        # normalize the expected duration to seconds for comparison
        expected_seconds: int = 0
        if expected_interval:
            if isinstance(expected_interval, int):
                expected_seconds = expected_interval
            else:
                expected_seconds = self._parse_duration(str(expected_interval))
        keyword = f"{name} {singer}".strip()
        try:
            hits = await self._search_source(src, keyword, page=1, page_size=10)
        except Exception as err:
            LOGGER.debug(
                "lxmusic: cross-platform re-search failed %s/%s/%s: %s",
                src,
                name,
                singer,
                err,
            )
            self._songmid_resolve_cache[cache_key] = None
            return None
        for hit in hits:
            mid = self._item_song_id(hit)
            if not mid:
                continue
            # duration check: require a close match when the original is known
            if expected_seconds > 0:
                hit_seconds = self._parse_duration(str(hit.get("interval", "")))
                if hit_seconds <= 0:
                    # the hit has no duration to compare, skip rather than risk it
                    continue
                if abs(hit_seconds - expected_seconds) > 5:
                    LOGGER.debug(
                        "lxmusic: cross-platform hit %s %s/%s duration mismatch (original=%ds hit=%ds), skipped",
                        src,
                        hit.get("name"),
                        hit.get("singer"),
                        expected_seconds,
                        hit_seconds,
                    )
                    continue
            self._songmid_resolve_cache[cache_key] = mid
            LOGGER.debug(
                "lxmusic: cross-platform re-search %s/%s/%s -> songmid=%s (duration matched)",
                src,
                name,
                singer,
                mid,
            )
            return mid
        LOGGER.warning(
            "lxmusic: cross-platform re-search %s/%s/%s found no duration match, giving up to avoid the wrong track",
            src,
            name,
            singer,
        )
        self._songmid_resolve_cache[cache_key] = None
        return None

    async def _check_url_playable(self, url: str) -> bool:
        """
        Check with a HEAD request whether a URL really serves audio.

        Some gateway proxies answer 200 with an empty body when they are
        blocked, and the server cannot resolve the real audio URL from such a
        redirect either. Handing that URL to ffmpeg fails with "Invalid data
        found when processing input", so the client checks the content type and
        rejects non-audio URLs.

        True: playable, the content type is audio, video or a playlist.
        False: not playable, e.g. an html body or a dead gateway.

        Known-dead gateway hosts are rejected outright, without a request, so
        that other fallbacks get a chance instead of waiting on a HEAD.
        """
        if not url or not url.startswith("http"):
            return False
        # Hosts known to answer 200 with an empty body or a bot-block code are
        # skipped outright, since even a HEAD gives no usable signal.
        bad_hosts = ("metingapi.nanorocky.top",)
        if any(h in url for h in bad_hosts):
            LOGGER.warning(
                "lxmusic: url matches a known dead gateway url=%s, skipped",
                url[:80],
            )
            return False
        try:
            session = self._session()
            async with session.head(url, allow_redirects=True, timeout=8) as resp:
                ct = (resp.headers.get("Content-Type") or "").lower()
                playable = (
                    ct.startswith(("audio/", "video/", "application/octet-stream"))
                    or "mpegurl" in ct
                    or "mp2t" in ct
                )
                if not playable:
                    LOGGER.warning(
                        "lxmusic: url content-type=%s not playable url=%s",
                        ct,
                        url[:80],
                    )
                return playable
        except Exception as err:
            # on a network error treat it as unusable and let other sources try
            LOGGER.warning(
                "lxmusic: HEAD check error url=%s err=%s, treating as unavailable",
                url[:80],
                err,
            )
            return False

    @staticmethod
    def _parse_duration(text: str) -> int:
        """Parse 'mm:ss' or 'hh:mm:ss' into seconds."""
        parts = [int(p) for p in text.split(":") if p.isdigit()]
        if not parts:
            return 0
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0]
