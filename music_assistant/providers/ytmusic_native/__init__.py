"""
Native YouTube Music provider for Music Assistant (experimental).

Talks to the real youtubei/v1 InnerTube API directly - no ytmusicapi, no yt-dlp,
no deno, no po_token server. Metadata replays the WEB_REMIX web client; audio is
served from premium itag 141/774 (256k) by solving the player cipher natively in
Node, falling back to ANDROID_VR (~150k) when the cipher can't be solved.

See reverseengeneer.md (§2-§8) for the full reverse-engineering write-up.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
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
from music_assistant.models.music_provider import MusicProvider

from . import parsers
from .cipher import CipherError, CipherSolver
from .constants import (
    BROWSE_HOME,
    BROWSE_LIBRARY_ALBUMS,
    BROWSE_LIBRARY_ARTISTS,
    BROWSE_LIBRARY_PLAYLISTS,
    BROWSE_LIBRARY_TRACKS,
    CONF_COOKIE,
    CONF_VISITOR_DATA,
    DEFAULT_STREAM_URL_EXPIRATION,
    DOMAIN,
    PREMIUM_CHECK_VIDEO_ID,
    PREMIUM_ITAGS,
    SEARCH_FILTER_PARAMS,
    VARIOUS_ARTISTS_YTM_ID,
)
from .innertube import InnerTube

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
}

MAX_CONTINUATION_PAGES = 50


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YoutubeMusicNativeProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        ConfigEntry(key=CONF_COOKIE, type=ConfigEntryType.SECURE_STRING, required=True),
        ConfigEntry(
            key=CONF_VISITOR_DATA,
            type=ConfigEntryType.STRING,
            required=False,
            default_value="",
        ),
    )


class YoutubeMusicNativeProvider(MusicProvider):
    """Native (reverse-engineered) YouTube Music provider."""

    _innertube: InnerTube
    _cipher: CipherSolver
    _has_premium: bool = False

    async def handle_async_init(self) -> None:
        """Set up the provider and capture session details."""
        cookie = str(self.config.get_value(CONF_COOKIE) or "")
        visitor_data = str(self.config.get_value(CONF_VISITOR_DATA) or "") or None
        self._innertube = InnerTube(self.mass.http_session, cookie, self.logger, visitor_data)
        await self._innertube.setup()
        self._cipher = CipherSolver(self.logger)
        if not self._cipher.available:
            self.logger.warning(
                "mini-racer (V8) runtime not available - premium 256k streaming "
                "disabled, falling back to ANDROID_VR (~150k)."
            )
        self._has_premium = await self._check_premium()
        if not self._has_premium:
            self.logger.info(
                "Account does not appear to have YouTube Music Premium "
                "(or cipher unavailable); using ANDROID_VR quality."
            )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True: the catalog differs from the library."""
        return True

    @use_cache(3600 * 24 * 7)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search YouTube Music."""
        body: dict[str, Any] = {"query": search_query}
        if len(media_types) == 1:
            filter_key = {
                MediaType.ARTIST: "artists",
                MediaType.ALBUM: "albums",
                MediaType.TRACK: "songs",
                MediaType.PLAYLIST: "playlists",
            }.get(media_types[0])
            if filter_key:
                body["params"] = SEARCH_FILTER_PARAMS[filter_key]
            elif media_types[0] == MediaType.RADIO:
                return SearchResults()
        response = await self._innertube.call_music("search", body)
        parsed = parsers.parse_search(response)
        results = SearchResults()
        if MediaType.ARTIST in media_types:
            results.artists = [self._to_artist(item) for item in parsed["artist"][:limit]]
        if MediaType.ALBUM in media_types:
            results.albums = [self._to_album(item) for item in parsed["album"][:limit]]
        if MediaType.PLAYLIST in media_types:
            results.playlists = [self._to_playlist(item) for item in parsed["playlist"][:limit]]
        if MediaType.TRACK in media_types:
            results.tracks = [
                track
                for item in parsed["track"][:limit]
                if (track := self._to_track(item)) is not None
            ]
        return results

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve all library artists."""
        for item in await self._browse_collect(BROWSE_LIBRARY_ARTISTS):
            if item["kind"] == "artist":
                yield self._to_artist(item)

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve all library albums."""
        for item in await self._browse_collect(BROWSE_LIBRARY_ALBUMS):
            if item["kind"] == "album":
                yield self._to_album(item)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve all library tracks."""
        for item in await self._browse_collect(BROWSE_LIBRARY_TRACKS):
            if item["kind"] == "track" and (track := self._to_track(item)):
                yield track

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve all library playlists."""
        for item in await self._browse_collect(BROWSE_LIBRARY_PLAYLISTS):
            if item["kind"] == "playlist":
                yield self._to_playlist(item)

    @use_cache(3600 * 24 * 30)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        response = await self._innertube.call_music("browse", {"browseId": prov_album_id})
        parsed = parsers.parse_album(response, prov_album_id)
        if not parsed["name"]:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return self._to_album(parsed)

    @use_cache(3600 * 24 * 30, allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        response = await self._innertube.call_music("browse", {"browseId": prov_album_id})
        parsed = parsers.parse_album(response, prov_album_id)
        tracks: list[Track] = []
        for number, item in enumerate(parsed["tracks"], 1):
            if track := self._to_track(item):
                track.track_number = number
                tracks.append(track)
        return tracks

    @use_cache(3600 * 24 * 30)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        response = await self._innertube.call_music("browse", {"browseId": prov_artist_id})
        parsed = parsers.parse_artist(response)
        if not parsed["name"]:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        parsed["channel_id"] = parsed.get("channel_id") or prov_artist_id
        return self._to_artist(parsed)

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        response = await self._innertube.call_music("browse", {"browseId": prov_artist_id})
        parsed = parsers.parse_artist(response)
        return [self._to_album(item) for item in parsed["albums"]]

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get the most popular tracks for the given artist."""
        response = await self._innertube.call_music("browse", {"browseId": prov_artist_id})
        parsed = parsers.parse_artist(response)
        return [
            track
            for item in parsed["top_tracks"][:25]
            if (track := self._to_track(item)) is not None
        ]

    @use_cache(3600 * 24 * 30)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        response = await self._innertube.call_player_web(prov_track_id)
        details = response.get("videoDetails")
        if not isinstance(details, dict) or not details.get("videoId"):
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        item = {
            "kind": "track",
            "video_id": details["videoId"],
            "name": details.get("title", ""),
            "artists": [{"id": details.get("channelId", ""), "name": details.get("author", "")}],
            "album": None,
            "duration": int(details["lengthSeconds"])
            if str(details.get("lengthSeconds", "")).isdigit()
            else None,
            "thumbnails": parsers.get_thumbnails(details),
            "explicit": False,
            "set_video_id": None,
        }
        track = self._to_track(item)
        if not track:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return track

    @use_cache(3600 * 24 * 7)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        browse_id = (
            prov_playlist_id if prov_playlist_id.startswith("VL") else f"VL{prov_playlist_id}"
        )
        response = await self._innertube.call_music("browse", {"browseId": browse_id})
        parsed = parsers.parse_playlist(response, prov_playlist_id.removeprefix("VL"))
        if not parsed["name"]:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
        return self._to_playlist(parsed)

    @use_cache(3600 * 3, allow_expired_cache=True)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get all tracks for the given playlist id."""
        if page > 0:
            return []
        prov_playlist_id = prov_playlist_id.removeprefix("VL")
        items = await self._browse_collect(f"VL{prov_playlist_id}")
        tracks: list[Track] = []
        for position, item in enumerate(items, 1):
            if item["kind"] == "track" and (track := self._to_track(item)):
                track.position = position
                tracks.append(track)
        return tracks

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the given track."""
        response = await self._innertube.call_music(
            "next", {"videoId": prov_track_id, "playlistId": f"RDAMVM{prov_track_id}"}
        )
        tracks: list[Track] = []
        for item in parsers.parse_watch_tracks(response):
            if item["video_id"] == prov_track_id:
                continue
            if track := self._to_track(item):
                tracks.append(track)
            if len(tracks) >= limit:
                break
        return tracks

    @use_cache(3600)
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get personalized recommendations from the home feed."""
        response = await self._innertube.call_music("browse", {"browseId": BROWSE_HOME})
        folders: list[RecommendationFolder] = []
        for shelf in parsers.find_all(response, "musicCarouselShelfRenderer"):
            title = parsers.get_text(parsers.find_one(shelf, "title"))
            if not title:
                continue
            folder = RecommendationFolder(
                name=title,
                item_id=f"{self.instance_id}_{title}",
                provider=self.instance_id,
                icon="mdi-music-note-outline",
            )
            for item in parsers.parse_items(shelf):
                media_item = self._to_media_item(item)
                if media_item is not None:
                    folder.items.append(media_item)
            if folder.items:
                folders.append(folder)
        return folders

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the streamdetails for the given track."""
        if self._has_premium and self._cipher.available:
            try:
                return await self._premium_stream_details(item_id)
            except (CipherError, UnplayableMediaError, KeyError) as err:
                self.logger.debug("Premium stream failed for %s, falling back: %s", item_id, err)
        return await self._android_vr_stream_details(item_id)

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve an image path (images are remotely accessible URLs)."""
        return path

    # ----------------- private -----------------

    async def _premium_stream_details(self, item_id: str) -> StreamDetails:
        response = await self._innertube.call_player_web(item_id)
        self._assert_playable(response, item_id)
        fmt = self._pick_premium_format(response)
        if not fmt:
            raise CipherError("no premium (itag 141/774) format available")
        url = fmt.get("url")
        if not url:
            url = await self._cipher.resolve_url(fmt["signatureCipher"], self._innertube)
        return self._build_stream_details(item_id, fmt, url)

    async def _android_vr_stream_details(self, item_id: str) -> StreamDetails:
        response = await self._innertube.call_player_android_vr(item_id)
        self._assert_playable(response, item_id)
        formats = self._audio_formats(response)
        fmt = max(
            (f for f in formats if f.get("url")),
            key=lambda f: f.get("bitrate", 0),
            default=None,
        )
        if not fmt:
            raise UnplayableMediaError(f"No streamable audio format for {item_id}")
        return self._build_stream_details(item_id, fmt, fmt["url"])

    def _build_stream_details(self, item_id: str, fmt: dict[str, Any], url: str) -> StreamDetails:
        mime = str(fmt.get("mimeType", ""))
        ext = "m4a" if "mp4" in mime else "webm"
        expiration = DEFAULT_STREAM_URL_EXPIRATION
        if expire := parse_qs(urlparse(url).query).get("expire", [None])[0]:
            if str(expire).isdigit():
                expiration = int(expire) - int(time.time())
        audio_format = AudioFormat(content_type=ContentType.try_parse(ext))
        if str(fmt.get("audioChannels", "")).isdigit():
            audio_format.channels = int(fmt["audioChannels"])
        if str(fmt.get("audioSampleRate", "")).isdigit():
            audio_format.sample_rate = int(fmt["audioSampleRate"])
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=audio_format,
            stream_type=StreamType.HTTP,
            path=url,
            can_seek=True,
            allow_seek=True,
            expiration=expiration,
        )

    @staticmethod
    def _assert_playable(response: dict[str, Any], item_id: str) -> None:
        status = response.get("playabilityStatus", {})
        if status.get("status") != "OK":
            reason = status.get("reason") or status.get("status") or "unknown"
            raise UnplayableMediaError(f"{item_id} not playable: {reason}")

    @staticmethod
    def _audio_formats(response: dict[str, Any]) -> list[dict[str, Any]]:
        adaptive = response.get("streamingData", {}).get("adaptiveFormats", [])
        return [f for f in adaptive if str(f.get("mimeType", "")).startswith("audio/")]

    def _pick_premium_format(self, response: dict[str, Any]) -> dict[str, Any] | None:
        by_itag = {f.get("itag"): f for f in self._audio_formats(response)}
        for itag in PREMIUM_ITAGS:
            if itag in by_itag:
                return by_itag[itag]
        return None

    async def _check_premium(self) -> bool:
        if not self._cipher.available:
            return False
        try:
            response = await self._innertube.call_player_web(PREMIUM_CHECK_VIDEO_ID)
            return self._pick_premium_format(response) is not None
        except Exception as err:
            self.logger.debug("Premium check failed: %s", err)
            return False

    async def _browse_collect(self, browse_id: str) -> list[dict[str, Any]]:
        response = await self._innertube.call_music("browse", {"browseId": browse_id})
        items = parsers.parse_items(response)
        token = parsers.find_continuation(response)
        seen: set[str] = set()
        pages = 0
        while token and token not in seen and pages < MAX_CONTINUATION_PAGES:
            seen.add(token)
            pages += 1
            response = await self._innertube.call_music("browse", {"continuation": token})
            new_items = parsers.parse_items(response)
            if not new_items:
                break
            items.extend(new_items)
            token = parsers.find_continuation(response)
        return items

    def _to_media_item(self, item: dict[str, Any]) -> MediaItemType | None:
        kind = item.get("kind")
        if kind == "track":
            return self._to_track(item)
        if kind == "album":
            return self._to_album(item)
        if kind == "artist":
            return self._to_artist(item)
        if kind == "playlist":
            return self._to_playlist(item)
        return None

    def _to_track(self, item: dict[str, Any]) -> Track | None:
        video_id = item.get("video_id")
        if not video_id:
            return None
        artists = self._artist_mappings(item.get("artists", []))
        if not artists:
            return None
        track = Track(
            item_id=video_id,
            provider=self.instance_id,
            name=item["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=video_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    url=f"{DOMAIN}/watch?v={video_id}",
                    audio_format=AudioFormat(content_type=ContentType.M4A),
                )
            },
        )
        track.artists = UniqueList(artists)
        if item.get("duration"):
            track.duration = int(item["duration"])
        if item.get("explicit"):
            track.metadata.explicit = True
        if album := item.get("album"):
            if album.get("id"):
                track.album = ItemMapping(
                    media_type=MediaType.ALBUM,
                    item_id=album["id"],
                    provider=self.instance_id,
                    name=album.get("name", ""),
                )
        if images := self._images(item.get("thumbnails")):
            track.metadata.images = images
        return track

    def _to_album(self, item: dict[str, Any]) -> Album:
        album_id = item.get("browse_id") or item.get("id")
        album = Album(
            item_id=album_id,
            provider=self.instance_id,
            name=item.get("name", ""),
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{DOMAIN}/browse/{album_id}",
                )
            },
        )
        album.artists = UniqueList(self._artist_mappings(item.get("artists", [])))
        if item.get("year") and str(item["year"]).isdigit():
            album.year = int(item["year"])
        if item.get("explicit"):
            album.metadata.explicit = True
        if images := self._images(item.get("thumbnails")):
            album.metadata.images = images
        return album

    def _to_artist(self, item: dict[str, Any]) -> Artist:
        artist_id = item.get("channel_id") or item.get("id") or VARIOUS_ARTISTS_YTM_ID
        artist = Artist(
            item_id=artist_id,
            provider=self.instance_id,
            name=item.get("name", ""),
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{DOMAIN}/channel/{artist_id}",
                )
            },
        )
        if item.get("description"):
            artist.metadata.description = item["description"]
        if images := self._images(item.get("thumbnails")):
            artist.metadata.images = images
        return artist

    def _to_playlist(self, item: dict[str, Any]) -> Playlist:
        playlist_id = item.get("playlist_id") or item.get("id")
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=item.get("name", ""),
            provider_mappings={
                ProviderMapping(
                    item_id=str(playlist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{DOMAIN}/playlist?list={playlist_id}",
                )
            },
        )
        playlist.owner = item.get("author") or self.name
        if item.get("description"):
            playlist.metadata.description = item["description"]
        if images := self._images(item.get("thumbnails")):
            playlist.metadata.images = images
        return playlist

    def _artist_mappings(self, artists: list[dict[str, Any]]) -> list[ItemMapping]:
        result: list[ItemMapping] = []
        for artist in artists:
            artist_id = artist.get("id")
            if not artist_id and artist.get("name") == "Various Artists":
                artist_id = VARIOUS_ARTISTS_YTM_ID
            if not artist_id:
                continue
            result.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=artist_id,
                    provider=self.instance_id,
                    name=artist.get("name", ""),
                )
            )
        return result

    def _images(self, thumbnails: list[dict[str, Any]] | None) -> UniqueList[MediaItemImage] | None:
        if not thumbnails:
            return None
        result: UniqueList[MediaItemImage] = UniqueList()
        seen: set[str] = set()
        for thumb in sorted(thumbnails, key=lambda t: t.get("width", 0), reverse=True):
            url = thumb.get("url", "")
            if not url:
                continue
            url_base = url.split("=w", maxsplit=1)[0].split("=s", maxsplit=1)[0]
            if url_base in seen:
                continue
            seen.add(url_base)
            result.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return result or None
