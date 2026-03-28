"""YouTube music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import time
from io import StringIO
from typing import Any
from urllib.parse import parse_qs, urlparse

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, SetupFailedError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    Playlist,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.util import install_package
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_API_KEY,
    CONF_COOKIES,
    CONF_PLAYLIST_LIMIT,
    DEFAULT_LIVE_STREAM_URL_EXPIRATION,
    DEFAULT_PLAYLIST_LIMIT,
    DEFAULT_STREAM_URL_EXPIRATION,
)
from .helpers import (
    extract_channel_info,
    extract_channel_playlists,
    extract_channel_videos,
    extract_playlist_info,
    extract_playlist_videos,
    extract_stream_or_live,
    extract_video_info,
    search_channels,
    search_playlists,
    search_yt,
)
from .parsers import parse_channel_as_artist, parse_playlist, parse_playlist_as_album, parse_track
from .youtube_api import (
    YouTubeDataAPIError,
    api_get_channel,
    api_get_channel_playlists,
    api_get_channel_videos,
    api_get_playlist,
    api_get_playlist_videos,
    api_get_video,
    api_search_channels,
    api_search_playlists,
    api_search_videos,
)

# Type alias matching MA-server's PlaylistPlayableItem (Track | Radio | PodcastEpisode | Audiobook)
# Defined locally to support MA versions that don't yet export it from constants
PlaylistPlayableItem = Track


class YouTubeProvider(MusicProvider):
    """Music provider that plays YouTube videos via yt-dlp."""

    _yt_dlp: Any = None
    _netscape_cookies: str | None = None

    async def handle_async_init(self) -> None:
        """Set up the YouTube provider."""
        logging.getLogger("yt_dlp").setLevel(self.logger.level + 10)
        await self._install_packages()
        if raw_cookies := self.config.get_value(CONF_COOKIES):
            self._netscape_cookies = _to_netscape_cookies(str(raw_cookies))
        if self.config.get_value(CONF_API_KEY):
            self.logger.info("YouTube Data API key configured, using API for search/metadata")
        else:
            self.logger.debug("No YouTube Data API key configured, using yt-dlp only")

    async def _install_packages(self) -> None:
        """Install/update yt-dlp dynamically on every load.

        Google breaks yt-dlp compatibility frequently; installing without a
        pinned version on each startup keeps the provider working without
        requiring a Music Assistant release.
        """
        await install_package("yt-dlp[default]")
        try:
            self._yt_dlp = await asyncio.to_thread(importlib.import_module, "yt_dlp")
        except ImportError as err:
            raise SetupFailedError("Package yt_dlp failed to install") from err

    @property
    def is_streaming_provider(self) -> bool:
        """Return True as YouTube is an online streaming provider."""
        return True

    @property
    def _api_key(self) -> str | None:
        """Return the configured YouTube Data API key, if any."""
        key = self.config.get_value(CONF_API_KEY)
        return str(key) if key else None

    @use_cache(3600 * 24)
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on YouTube.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        tracks: list[Track] = []
        artists: list[Artist] = []
        playlists: list[Playlist] = []
        if MediaType.TRACK in media_types:
            entries = await self._search_videos(search_query, limit)
            for entry in entries:
                if track := parse_track(entry, self.domain, self.instance_id):
                    tracks.append(track)
        if MediaType.ARTIST in media_types:
            channel_entries = await self._search_channels(search_query, limit)
            for entry in channel_entries:
                artists.append(parse_channel_as_artist(entry, self.domain, self.instance_id))
        if MediaType.PLAYLIST in media_types:
            playlist_entries = await self._search_playlists(search_query, limit)
            for entry in playlist_entries:
                playlists.append(parse_playlist(entry, self.domain, self.instance_id))
        return SearchResults(tracks=tracks, artists=artists, playlists=playlists)

    @use_cache(3600 * 24 * 7)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by video id."""
        entry = await self._get_video_info(prov_track_id)
        if not entry:
            msg = f"Item {prov_track_id} not found"
            raise MediaNotFoundError(msg)
        track = parse_track(entry, self.domain, self.instance_id)
        if not track:
            msg = f"Item {prov_track_id} not found"
            raise MediaNotFoundError(msg)
        return track

    @use_cache(3600 * 24 * 7)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get channel details as an Artist by channel id."""
        entry = await self._get_channel_info(prov_artist_id)
        if not entry:
            msg = f"Channel {prov_artist_id} not found"
            raise MediaNotFoundError(msg)
        return parse_channel_as_artist(entry, self.domain, self.instance_id)

    @use_cache(3600 * 24)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get channel playlists as albums for the given artist (channel)."""
        raw_limit = self.config.get_value(CONF_PLAYLIST_LIMIT)
        limit = int(str(raw_limit)) if raw_limit else DEFAULT_PLAYLIST_LIMIT
        entries = await self._get_channel_playlists(prov_artist_id, limit)
        albums: list[Album] = []
        for entry in entries:
            entry.setdefault("channel_id", prov_artist_id)
            albums.append(parse_playlist_as_album(entry, self.domain, self.instance_id))
        return albums

    @use_cache(3600 * 24 * 7)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get playlist details as an Album by playlist id."""
        entry = await self._get_playlist_info(prov_album_id)
        if not entry:
            msg = f"Playlist {prov_album_id} not found"
            raise MediaNotFoundError(msg)
        return parse_playlist_as_album(entry, self.domain, self.instance_id)

    @use_cache(3600 * 24)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get tracks from a playlist (album)."""
        entries = await self._get_playlist_videos(prov_album_id, 50)
        tracks: list[Track] = []
        for entry in entries:
            if track := parse_track(entry, self.domain, self.instance_id):
                tracks.append(track)
        return tracks

    @use_cache(3600 * 24 * 7)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by playlist id."""
        entry = await self._get_playlist_info(prov_playlist_id)
        if not entry:
            msg = f"Playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg)
        return parse_playlist(entry, self.domain, self.instance_id)

    @use_cache(3600 * 24)
    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[PlaylistPlayableItem]:
        """Get tracks from a playlist."""
        if page > 0:
            return []
        entries = await self._get_playlist_videos(prov_playlist_id, 50)
        tracks: list[PlaylistPlayableItem] = []
        for entry in entries:
            if track := parse_track(entry, self.domain, self.instance_id):
                tracks.append(track)
        return tracks

    @use_cache(3600 * 24)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist (channel)."""
        artist = await self.get_artist(prov_artist_id)
        entries = await self._get_channel_videos(prov_artist_id, 25)
        tracks: list[Track] = []
        for entry in entries:
            # Flat channel video entries lack channel metadata; inject it so
            # parse_track can create a proper artist mapping.
            entry.setdefault("channel_id", prov_artist_id)
            entry.setdefault("channel", artist.name)
            if track := parse_track(entry, self.domain, self.instance_id):
                tracks.append(track)
        return tracks

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return stream details for the given track.

        Handles both regular videos (HTTP direct URL) and live streams (HLS)
        in a single yt-dlp extraction.
        """
        result = await asyncio.to_thread(
            extract_stream_or_live, self._yt_dlp, self._ydl_opts(), item_id
        )
        if result.get("is_live"):
            return self._build_live_stream_details(item_id, result)
        return self._build_vod_stream_details(item_id, result)

    def _build_vod_stream_details(
        self, item_id: str, stream_format: dict[str, Any]
    ) -> StreamDetails:
        """Build stream details for a regular (non-live) video.

        :param item_id: YouTube video ID.
        :param stream_format: Selected audio format dict from yt-dlp.
        """
        url = stream_format["url"]
        expiration = DEFAULT_STREAM_URL_EXPIRATION
        if parsed := parse_qs(urlparse(url).query):
            if expire_ts := parsed.get("expire", [None])[0]:
                with contextlib.suppress(ValueError, TypeError):
                    expiration = int(expire_ts) - int(time.time())
        stream_details = StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(stream_format.get("audio_ext", "unknown")),
            ),
            stream_type=StreamType.HTTP,
            path=url,
            can_seek=True,
            allow_seek=True,
            expiration=expiration,
        )
        if channels := stream_format.get("audio_channels"):
            if str(channels).isdigit():
                stream_details.audio_format.channels = int(channels)
        if sample_rate := stream_format.get("asr"):
            stream_details.audio_format.sample_rate = int(sample_rate)
        return stream_details

    def _build_live_stream_details(self, item_id: str, live_info: dict[str, Any]) -> StreamDetails:
        """Build stream details for a live YouTube stream.

        :param item_id: YouTube video ID.
        :param live_info: Live stream info dict from extract_stream_or_live.
        """
        title = live_info.get("title") or item_id
        uploader = live_info.get("uploader") or "Unknown"
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.AAC),
            stream_type=StreamType.HLS,
            media_type=MediaType.RADIO,
            path=live_info["manifest_url"],
            can_seek=False,
            allow_seek=False,
            expiration=DEFAULT_LIVE_STREAM_URL_EXPIRATION,
            stream_metadata=StreamMetadata(title=title, artist=uploader),
        )

    # --- Internal methods: API-first with yt-dlp fallback ---

    async def _search_videos(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Search for videos, using the Data API if available.

        :param query: Search query string.
        :param limit: Maximum number of results.
        """
        if api_key := self._api_key:
            try:
                return await api_search_videos(self.mass.http_session, api_key, query, limit)
            except YouTubeDataAPIError:
                self.logger.warning("YouTube Data API search failed, falling back to yt-dlp")
        return await asyncio.to_thread(search_yt, self._yt_dlp, self._ydl_opts(), query, limit)

    async def _search_channels(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Search for channels, using the Data API if available.

        :param query: Search query string.
        :param limit: Maximum number of results.
        """
        if api_key := self._api_key:
            try:
                return await api_search_channels(self.mass.http_session, api_key, query, limit)
            except YouTubeDataAPIError:
                self.logger.warning(
                    "YouTube Data API channel search failed, falling back to yt-dlp"
                )
        return await asyncio.to_thread(
            search_channels, self._yt_dlp, self._ydl_opts(), query, limit
        )

    async def _search_playlists(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Search for playlists, using the Data API if available.

        :param query: Search query string.
        :param limit: Maximum number of results.
        """
        if api_key := self._api_key:
            try:
                return await api_search_playlists(self.mass.http_session, api_key, query, limit)
            except YouTubeDataAPIError:
                self.logger.warning(
                    "YouTube Data API playlist search failed, falling back to yt-dlp"
                )
        return await asyncio.to_thread(
            search_playlists, self._yt_dlp, self._ydl_opts(), query, limit
        )

    async def _get_video_info(self, video_id: str) -> dict[str, Any] | None:
        """Get video metadata, using the Data API if available.

        :param video_id: YouTube video ID.
        """
        if api_key := self._api_key:
            try:
                return await api_get_video(self.mass.http_session, api_key, video_id)
            except YouTubeDataAPIError:
                self.logger.warning("YouTube Data API video fetch failed, falling back to yt-dlp")
        return await asyncio.to_thread(extract_video_info, self._yt_dlp, self._ydl_opts(), video_id)

    async def _get_channel_info(self, channel_id: str) -> dict[str, Any] | None:
        """Get channel metadata, using the Data API if available.

        :param channel_id: YouTube channel ID.
        """
        if api_key := self._api_key:
            try:
                return await api_get_channel(self.mass.http_session, api_key, channel_id)
            except YouTubeDataAPIError:
                self.logger.warning("YouTube Data API channel fetch failed, falling back to yt-dlp")
        return await asyncio.to_thread(
            extract_channel_info, self._yt_dlp, self._ydl_opts(), channel_id
        )

    async def _get_channel_videos(self, channel_id: str, limit: int) -> list[dict[str, Any]]:
        """Get channel videos, using the Data API if available.

        :param channel_id: YouTube channel ID.
        :param limit: Maximum number of videos to return.
        """
        if api_key := self._api_key:
            try:
                return await api_get_channel_videos(
                    self.mass.http_session, api_key, channel_id, limit
                )
            except YouTubeDataAPIError:
                self.logger.warning(
                    "YouTube Data API channel videos failed, falling back to yt-dlp"
                )
        return await asyncio.to_thread(
            extract_channel_videos, self._yt_dlp, self._ydl_opts(), channel_id, limit
        )

    async def _get_channel_playlists(self, channel_id: str, limit: int) -> list[dict[str, Any]]:
        """Get channel playlists, using the Data API if available.

        :param channel_id: YouTube channel ID.
        :param limit: Maximum number of playlists to return.
        """
        if api_key := self._api_key:
            try:
                return await api_get_channel_playlists(
                    self.mass.http_session, api_key, channel_id, limit
                )
            except YouTubeDataAPIError:
                self.logger.warning(
                    "YouTube Data API channel playlists failed, falling back to yt-dlp"
                )
        return await asyncio.to_thread(
            extract_channel_playlists, self._yt_dlp, self._ydl_opts(), channel_id, limit
        )

    async def _get_playlist_info(self, playlist_id: str) -> dict[str, Any] | None:
        """Get playlist metadata, using the Data API if available.

        :param playlist_id: YouTube playlist ID.
        """
        if api_key := self._api_key:
            try:
                return await api_get_playlist(self.mass.http_session, api_key, playlist_id)
            except YouTubeDataAPIError:
                self.logger.warning(
                    "YouTube Data API playlist fetch failed, falling back to yt-dlp"
                )
        return await asyncio.to_thread(
            extract_playlist_info, self._yt_dlp, self._ydl_opts(), playlist_id
        )

    async def _get_playlist_videos(self, playlist_id: str, limit: int) -> list[dict[str, Any]]:
        """Get playlist videos, using the Data API if available.

        :param playlist_id: YouTube playlist ID.
        :param limit: Maximum number of videos to return.
        """
        if api_key := self._api_key:
            try:
                return await api_get_playlist_videos(
                    self.mass.http_session, api_key, playlist_id, limit
                )
            except YouTubeDataAPIError:
                self.logger.warning(
                    "YouTube Data API playlist videos failed, falling back to yt-dlp"
                )
        return await asyncio.to_thread(
            extract_playlist_videos, self._yt_dlp, self._ydl_opts(), playlist_id, limit
        )

    def _ydl_opts(self) -> dict[str, Any]:
        """Return base yt-dlp options for this provider instance."""
        has_cookies = self._netscape_cookies is not None
        opts: dict[str, Any] = {
            "quiet": self.logger.level > logging.DEBUG,
            "verbose": self.logger.level == VERBOSE_LOG_LEVEL,
            "extractor_args": {
                "youtube": {
                    "skip": ["translated_subs"],
                },
            },
        }
        if has_cookies:
            # With cookies: web_music is best for audio, web as fallback, tv
            # for "made for kids" content.
            opts["extractor_args"]["youtube"]["player_client"] = [
                "web_music",
                "web",
                "tv",
            ]
            opts["cookiefile"] = StringIO(self._netscape_cookies)
        else:
            # Without cookies: don't override player_client — let yt-dlp use
            # its own defaults which the yt-dlp team keeps updated as YouTube
            # changes client requirements. Web-based clients (mweb, web) need
            # a PoToken without cookies, and android_vr gets throttled.
            self.logger.debug(
                "No cookies configured — using yt-dlp default player clients. "
                "Configure cookies for more reliable playback."
            )
        return opts


def _to_netscape_cookies(cookies: str) -> str:
    """Convert a cookie string to Netscape cookies.txt format if needed.

    Accepts either Netscape format (returned as-is) or a raw cookie header value
    from browser DevTools (e.g. 'name1=val1; name2=val2') which is converted
    to Netscape format for .youtube.com.
    """
    # If it contains tabs, assume it's already in Netscape format
    if "\t" in cookies:
        return cookies
    # Convert raw "name=value; name2=value2" header format
    lines = ["# Netscape HTTP Cookie File"]
    for raw_pair in cookies.split(";"):
        pair = raw_pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        lines.append(f".youtube.com\tTRUE\t/\tTRUE\t0\t{name.strip()}\t{value.strip()}")
    return "\n".join(lines) + "\n"
