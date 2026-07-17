"""MusicMe provider implementation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import urllib.parse
from collections.abc import Coroutine, Sequence
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import ClientConnectionError, ClientResponseError
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    SetupFailedError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Playlist,
    ProviderMapping,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CLIENT_JSON,
    DATASERVICE_BASE,
    LOGIN_URL,
    PARTNER_ID,
    STREAM_BASE,
    VALID_ID_RE,
    WEB_BASE,
)
from .helpers import decrypt

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class MusicMeProvider(MusicProvider):
    """Provider for the MusicMe streaming service."""

    _user_id: str | None = None
    http_session: aiohttp.ClientSession
    throttler: ThrottlerManager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if not self.config.get_value(CONF_USERNAME) or not self.config.get_value(CONF_PASSWORD):
            msg = "Missing MusicMe email or password"
            raise SetupFailedError(msg)
        # Dedicated session with its own cookie jar to support multi-instance
        # (each instance has its own MusicMe login cookies)
        self.http_session = create_clientsession(self.mass, cookie_jar=aiohttp.CookieJar())
        self.throttler = ThrottlerManager(rate_limit=1, period=1)
        await self._login()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if hasattr(self, "http_session") and not self.http_session.closed:
            await self.http_session.close()

    # ---- search ----

    @use_cache(3600 * 3)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """
        Perform search on MusicMe.

        Uses a two-pass strategy: the dataservice API first (fast, popular matches),
        then a web search fallback for niche queries.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        result = SearchResults()
        encoded_query = urllib.parse.quote_plus(search_query)

        data = await self._api_get(
            f"/search?query={encoded_query}"
            f"&resources=artists{{maxResults:{limit}}}"
            f",albums{{maxResults:{limit}}}"
            f",tracks{{maxResults:{limit}}}"
        )
        if data and "results" in data:
            res = data["results"]
            if MediaType.ARTIST in media_types:
                artists = res.get("artists", [])
                if isinstance(artists, list):
                    result.artists = [self._parse_artist(a) for a in artists if a.get("id")]
            if MediaType.ALBUM in media_types:
                albums = res.get("albums", [])
                if isinstance(albums, list):
                    result.albums = [self._parse_album(a) for a in albums if a.get("barcode")]
            if MediaType.TRACK in media_types:
                tracks = res.get("tracks", [])
                if isinstance(tracks, list):
                    result.tracks = [
                        self._parse_track(t)
                        for t in tracks
                        if t.get("barcode") and t.get("streamable") == 2
                    ]

        if not result.artists and not result.albums and not result.tracks:
            self.logger.debug("API search empty, trying web search fallback")
            web_result = await self._web_search_fallback(search_query, media_types, limit)
            if web_result:
                result = web_result

        return result

    # ---- browse ----

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse MusicMe content.

        :param path: The path to browse.
        """
        sub_path = ""
        if path and "://" in path:
            sub_path = path.split("://", 1)[1]
        if not path or not sub_path:
            return [
                BrowseFolder(
                    item_id="home",
                    provider=self.instance_id,
                    path=f"{self.instance_id}://home",
                    name="Featured",
                    translation_key="featured",
                ),
                BrowseFolder(
                    item_id="news",
                    provider=self.instance_id,
                    path=f"{self.instance_id}://news",
                    name="New releases",
                    translation_key="new_releases",
                ),
                BrowseFolder(
                    item_id="tops",
                    provider=self.instance_id,
                    path=f"{self.instance_id}://tops",
                    name="Top artists",
                    translation_key="top_artists",
                ),
                BrowseFolder(
                    item_id="radios",
                    provider=self.instance_id,
                    path=f"{self.instance_id}://radios",
                    name="Themed radios",
                    translation_key="themed_radios",
                ),
            ]

        parts = [p for p in sub_path.split("/") if p]
        section = parts[0] if parts else ""
        sub_id = parts[1] if len(parts) > 1 else None

        if section == "home":
            return await self._browse_home()
        if section == "news":
            return await self._browse_news(sub_id)
        if section == "tops":
            return await self._browse_tops()
        if section == "radios":
            return await self._browse_radio_themes(sub_id)
        return []

    @use_cache(3600)
    async def _browse_home(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse the MusicMe homepage."""
        data = await self._api_get("/home")
        if not data:
            return []
        items: list[MediaItemType | ItemMapping | BrowseFolder] = []
        for entry in data.get("results", {}).get("items", []):
            if entry.get("id"):
                items.append(self._parse_radio(entry))
        return items

    @use_cache(3600)
    async def _browse_news(
        self, style_id: str | None = None
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse new releases, optionally filtered by style."""
        sid = style_id or "0"
        data = await self._api_get(
            f"/news/{sid}?filters={{style:{sid}}}"
            f"&resources=albums{{maxResults:20}},focus-albums,styles"
        )
        if not data:
            return []
        results: list[MediaItemType | ItemMapping | BrowseFolder] = []

        if not style_id:
            for style in data.get("results", {}).get("styles", []):
                s_id = str(style.get("id", ""))
                s_name = style.get("name", "")
                if s_id and s_name and s_id != "0":
                    results.append(
                        BrowseFolder(
                            item_id=f"news_{s_id}",
                            provider=self.instance_id,
                            path=f"{self.instance_id}://news/{s_id}",
                            name=s_name,
                        )
                    )

        for album_obj in data.get("results", {}).get("albums", []):
            if album_obj.get("barcode") and album_obj.get("streamable", 0) == 2:
                results.append(self._parse_album(album_obj))
        return results

    @use_cache(3600)
    async def _browse_tops(self) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse top artists."""
        data = await self._api_get(
            "/tops?filters={style:0}&resources=styles,artists{maxResults:20},videos{maxResults:0}"
        )
        if not data:
            return []
        results: list[MediaItemType | ItemMapping | BrowseFolder] = []
        for artist_obj in data.get("results", {}).get("artists", []):
            if artist_obj.get("id"):
                results.append(self._parse_artist(artist_obj))
        return results

    @use_cache(3600)
    async def _browse_radio_themes(
        self, theme_id: str | None = None
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse radios grouped by theme."""
        tid = theme_id or "0"
        data = await self._api_get(
            f"/radios?filters={{theme:{tid}}}&resources=themes,theme-airplays{{maxResults:50}},home"
        )
        if not data:
            return []
        results: list[MediaItemType | ItemMapping | BrowseFolder] = []

        if not theme_id:
            for theme in data.get("results", {}).get("themes", []):
                t_id = str(theme.get("id", ""))
                t_name = theme.get("name", "")
                if t_id and t_name and t_id != "0":
                    results.append(
                        BrowseFolder(
                            item_id=f"radios_{t_id}",
                            provider=self.instance_id,
                            path=f"{self.instance_id}://radios/{t_id}",
                            name=f"{t_name} ({theme.get('radiocount', '')})",
                        )
                    )

        for item in data.get("results", {}).get("theme-airplays", []):
            if item.get("id"):
                results.append(self._parse_radio(item))
        return results

    # ---- recommendations ----

    @use_cache(3600 * 3)
    async def recommendations(self, wanted: set[str] | None = None) -> list[RecommendationFolder]:
        """
        Get MusicMe recommendations for the Discover page.

        :param wanted: optional set of row item_ids to build; when None, all rows are built.
        """

        def want(suffix: str) -> bool:
            return wanted is None or f"{self.instance_id}_{suffix}" in wanted

        want_home, want_news, want_tops, want_radios = (
            want("home"),
            want("news"),
            want("tops"),
            want("radios"),
        )

        calls: list[Coroutine[Any, Any, dict[str, Any] | None]] = []
        if want_home:
            calls.append(self._api_get("/home"))
        if want_news:
            calls.append(
                self._api_get(
                    "/news/0?filters={style:0}&resources=albums{maxResults:10},focus-albums,styles"
                )
            )
        if want_tops:
            calls.append(
                self._api_get(
                    "/tops?filters={style:0}"
                    "&resources=styles,artists{maxResults:10},videos{maxResults:0}"
                )
            )
        if want_radios:
            calls.append(
                self._api_get(
                    "/radios?filters={theme:0}&resources=themes,theme-airplays{maxResults:10},home"
                )
            )

        fetched = iter(await asyncio.gather(*calls))
        home_data = next(fetched) if want_home else None
        news_data = next(fetched) if want_news else None
        tops_data = next(fetched) if want_tops else None
        radio_data = next(fetched) if want_radios else None

        result: list[RecommendationFolder] = []

        if home_data:
            folder = RecommendationFolder(
                name="Featured",
                translation_key="featured",
                item_id=f"{self.instance_id}_home",
                provider=self.instance_id,
                icon="mdi-star",
            )
            for entry in home_data.get("results", {}).get("items", []):
                if entry.get("id"):
                    folder.items.append(self._parse_radio(entry))
            if folder.items:
                result.append(folder)

        if news_data:
            folder = RecommendationFolder(
                name="New releases",
                translation_key="new_releases",
                item_id=f"{self.instance_id}_news",
                provider=self.instance_id,
                icon="mdi-new-box",
            )
            for album_obj in news_data.get("results", {}).get("albums", []):
                if album_obj.get("barcode") and album_obj.get("streamable", 0) == 2:
                    folder.items.append(self._parse_album(album_obj))
            if folder.items:
                result.append(folder)

        if tops_data:
            folder = RecommendationFolder(
                name="Top artists",
                translation_key="top_artists",
                item_id=f"{self.instance_id}_tops",
                provider=self.instance_id,
                icon="mdi-trending-up",
            )
            for artist_obj in tops_data.get("results", {}).get("artists", []):
                if artist_obj.get("id"):
                    folder.items.append(self._parse_artist(artist_obj))
            if folder.items:
                result.append(folder)

        if radio_data:
            folder = RecommendationFolder(
                name="Radios",
                translation_key="radios",
                item_id=f"{self.instance_id}_radios",
                provider=self.instance_id,
                icon="mdi-radio",
            )
            for item in radio_data.get("results", {}).get("theme-airplays", [])[:10]:
                if item.get("id"):
                    folder.items.append(self._parse_radio(item))
            if folder.items:
                result.append(folder)

        return result

    # ---- library ----

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve library playlists from MusicMe."""
        data = await self._api_get('/playlists?resources=home{details:"list"}')
        if not data:
            return
        items: Any = data.get("results", {})
        if isinstance(items, dict) and "items" in items:
            items = items["items"]
        if isinstance(items, dict) and "results" in items:
            items = items["results"]
        if isinstance(items, list):
            for pl in items:
                if pl.get("id"):
                    yield self._parse_playlist(pl)

    # ---- item detail getters ----

    @use_cache(3600 * 24 * 30)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        data = await self._api_get(
            f"/artist/{prov_artist_id}"
            f"?resources=albums{{maxResults:0}},related-artists{{maxResults:0}}"
        )
        if not data or "item" not in data:
            msg = f"Artist {prov_artist_id} not found"
            raise MediaNotFoundError(msg)
        return self._parse_artist(data["item"])

    @use_cache(3600 * 24 * 30)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        data = await self._api_get(f"/album/{prov_album_id}?resources=tracks")
        if not data or "item" not in data:
            msg = f"Album {prov_album_id} not found"
            raise MediaNotFoundError(msg)
        return self._parse_album(data["item"])

    @use_cache(3600 * 24 * 30)
    async def get_track(self, prov_track_id: str) -> Track:
        """
        Get full track details by id.

        MusicMe doesn't have a single-track endpoint, so we fetch the parent album.
        """
        album_barcode = prov_track_id.split("-", maxsplit=1)[0]
        data = await self._api_get(f"/album/{album_barcode}?resources=tracks")
        if not data:
            msg = f"Track {prov_track_id} not found"
            raise MediaNotFoundError(msg)
        for track_obj in data.get("results", {}).get("tracks", []):
            if track_obj.get("barcode") == prov_track_id:
                return self._parse_track(track_obj)
        msg = f"Track {prov_track_id} not found in album {album_barcode}"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 24 * 30)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        data = await self._api_get(f"/playlist/{prov_playlist_id}?resources=tracks")
        if not data:
            msg = f"Playlist {prov_playlist_id} not found"
            raise MediaNotFoundError(msg)
        item = data.get("item", data)
        return self._parse_playlist(item)

    @use_cache(3600 * 24 * 14, allow_expired_cache=True)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all album tracks for given album id."""
        data = await self._api_get(f"/album/{prov_album_id}?resources=tracks")
        if not data:
            return []
        tracks = data.get("results", {}).get("tracks", [])
        return [self._parse_track(t) for t in tracks if t.get("barcode")]

    @use_cache(3600 * 24 * 7, allow_expired_cache=True)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """
        Get a list of top/popular tracks for the given artist.

        MusicMe has no dedicated top-tracks endpoint, so we collect the first
        tracks from the artist's most recent albums (sorted by date desc).
        """
        data = await self._api_get(f"/artist/{prov_artist_id}?resources=albums{{maxResults:5}}")
        if not data:
            return []
        albums = data.get("results", {}).get("albums", [])
        streamable_barcodes = [
            a["barcode"] for a in albums if a.get("barcode") and a.get("streamable", 0) == 2
        ]
        album_results = await asyncio.gather(
            *(self._api_get(f"/album/{bc}?resources=tracks") for bc in streamable_barcodes)
        )
        top_tracks: list[Track] = []
        for album_data in album_results:
            if not album_data:
                continue
            for t in album_data.get("results", {}).get("tracks", []):
                if t.get("barcode") and t.get("streamable", 0) == 2:
                    top_tracks.append(self._parse_track(t))
                if len(top_tracks) >= 20:
                    break
            if len(top_tracks) >= 20:
                break
        return top_tracks

    @use_cache(3600 * 24 * 14, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        data = await self._api_get(f"/artist/{prov_artist_id}?resources=albums{{maxResults:50}}")
        if not data:
            return []
        albums = data.get("results", {}).get("albums", [])
        return [self._parse_album(a) for a in albums if a.get("barcode")]

    @use_cache(3600 * 3, allow_expired_cache=True)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        if page > 0:
            return []
        data = await self._api_get(f"/playlist/{prov_playlist_id}?resources=tracks")
        if not data:
            return []
        tracks = data.get("results", {}).get("tracks", [])
        return [self._parse_track(t) for t in tracks if t.get("barcode")]

    # ---- streaming ----

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the content details for the given track when it will be streamed.

        :param item_id: The MusicMe track barcode, or radio airplay ID.
        :param media_type: The media type (TRACK or RADIO).
        """
        if media_type == MediaType.RADIO:
            track_barcode = await self._get_radio_track(item_id)
        else:
            track_barcode = item_id

        stream_url = await self._get_stream_url(track_barcode)
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.MP4,
                codec_type=ContentType.AAC,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
            stream_type=StreamType.HTTP,
            path=stream_url,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_radio_track(self, radio_id: str) -> str:
        """Get a streamable track barcode from a MusicMe radio/airplay."""
        data = await self._api_get(f"/airplay/{radio_id}?resources=tracks")
        if not data:
            msg = f"Radio {radio_id} returned no data"
            raise MediaNotFoundError(msg)
        tracks = data.get("results", {}).get("tracks", [])
        for t in tracks:
            if t.get("barcode") and t.get("streamable", 0) == 2:
                return str(t["barcode"])
        msg = f"No streamable tracks in radio {radio_id}"
        raise MediaNotFoundError(msg)

    # ---- parsers ----

    def _parse_artist(self, artist_obj: dict[str, Any]) -> Artist:
        """Parse a MusicMe artist object to a Music Assistant Artist."""
        artist_id = str(artist_obj.get("id", ""))
        artist = Artist(
            item_id=artist_id,
            provider=self.instance_id,
            name=artist_obj.get("name", "Unknown"),
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{WEB_BASE}{artist_obj['uri']}" if artist_obj.get("uri") else "",
                )
            },
        )
        # Use square artist images (r288) instead of the banner-shaped eyestoeye/144x56
        if artist_id:
            artist.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"https://covers-ng3.hosting-media.net/art/r288/{artist_id}.jpg",
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return artist

    def _parse_album(self, album_obj: dict[str, Any]) -> Album:
        """Parse a MusicMe album object to a Music Assistant Album."""
        barcode = str(album_obj.get("barcode", ""))
        album = Album(
            item_id=barcode,
            provider=self.instance_id,
            name=album_obj.get("name", album_obj.get("title", "Unknown")),
            provider_mappings={
                ProviderMapping(
                    item_id=barcode,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=album_obj.get("streamable", 0) == 2,
                    audio_format=AudioFormat(
                        content_type=ContentType.MP4,
                        codec_type=ContentType.AAC,
                        sample_rate=44100,
                        bit_depth=16,
                    ),
                    url=f"{WEB_BASE}{album_obj['uri']}" if album_obj.get("uri") else "",
                )
            },
        )
        for art_obj in album_obj.get("artists", []):
            if art_obj.get("id"):
                album.artists.append(self._parse_artist(art_obj))
        if cover := album_obj.get("cover_url"):
            album.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=cover,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        if streetdate := album_obj.get("streetdate"):
            with contextlib.suppress(ValueError, IndexError):
                album.year = int(streetdate[:4])
        return album

    def _parse_track(self, track_obj: dict[str, Any]) -> Track:
        """Parse a MusicMe track object to a Music Assistant Track."""
        barcode = str(track_obj.get("barcode", ""))
        track = Track(
            item_id=barcode,
            provider=self.instance_id,
            name=track_obj.get("title", "Unknown"),
            duration=track_obj.get("duration", 0),
            provider_mappings={
                ProviderMapping(
                    item_id=barcode,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=track_obj.get("streamable", 0) == 2,
                    audio_format=AudioFormat(
                        content_type=ContentType.MP4,
                        codec_type=ContentType.AAC,
                        sample_rate=44100,
                        bit_depth=16,
                    ),
                )
            },
        )
        # Parse disc and track number from barcode format: {albumBarcode}-{disc}_{track}
        if "-" in barcode and "_" in barcode:
            try:
                disc_track = barcode.split("-", 1)[1]
                disc_track_parts = disc_track.split("_")
                track.disc_number = int(disc_track_parts[0])
                track.track_number = int(disc_track_parts[1])
            except ValueError, IndexError:
                pass
        for art_obj in track_obj.get("artists", []):
            if art_obj.get("id"):
                track.artists.append(self._parse_artist(art_obj))
        if track_obj.get("album"):
            album_barcode = barcode.split("-", maxsplit=1)[0]
            track.album = ItemMapping(
                item_id=album_barcode,
                provider=self.instance_id,
                name=track_obj["album"],
                media_type=MediaType.ALBUM,
            )
        if cover := track_obj.get("cover_url"):
            track.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=cover,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return track

    def _parse_radio(self, radio_obj: dict[str, Any]) -> Radio:
        """Parse a MusicMe radio/airplay object to a Music Assistant Radio."""
        radio_id = str(radio_obj.get("id", ""))
        radio = Radio(
            item_id=radio_id,
            provider=self.instance_id,
            name=radio_obj.get("name", "Radio"),
            provider_mappings={
                ProviderMapping(
                    item_id=radio_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        img = radio_obj.get("tile_url") or radio_obj.get("img_url")
        if img:
            radio.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=img,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        preview_artists = radio_obj.get("preview_artists", [])
        if preview_artists:
            names = [a.get("name", "") for a in preview_artists[:8] if a.get("name")]
            if names:
                radio.metadata.description = ", ".join(names) + "..."
        return radio

    def _parse_playlist(self, playlist_obj: dict[str, Any]) -> Playlist:
        """Parse a MusicMe playlist object to a Music Assistant Playlist."""
        playlist_id = str(playlist_obj.get("id", ""))
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=playlist_obj.get("name", playlist_obj.get("title", "Playlist")),
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if cover := playlist_obj.get("cover_url"):
            playlist.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=cover,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return playlist

    # ---- private helpers ----

    async def _web_search_fallback(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 10,
    ) -> SearchResults | None:
        """
        Fall back to MusicMe's PHP web search when the dataservice API returns nothing.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return.
        """
        encoded = urllib.parse.quote_plus(search_query)
        url = f"{WEB_BASE}/search.php?ambsearch={encoded}&mmz=all"

        try:
            async with self.http_session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                raw_bytes = await resp.read()
                content = raw_bytes.decode("latin-1", errors="replace")
        except (ClientConnectionError, ClientResponseError) as err:
            self.logger.debug("Web search fallback failed: %s", err)
            return None

        result = SearchResults()

        match = re.search(r"window\.playerInit\s*=\s*(\{.*?\});", content, re.DOTALL)
        if not match:
            return None

        player_init = json_loads(match.group(1))
        content_on_load = player_init.get("contentOnLoad", {})
        content_type = content_on_load.get("type", "")
        content_id = content_on_load.get("id")

        if not content_id or not VALID_ID_RE.match(str(content_id)):
            return None

        if content_type == "artist" and MediaType.ARTIST in media_types:
            artist_data = await self._api_get(
                f"/artist/{content_id}"
                f"?resources=albums{{maxResults:{limit}}},related-artists{{maxResults:0}}"
            )
            if artist_data and "item" in artist_data:
                result.artists = [self._parse_artist(artist_data["item"])]
                if MediaType.ALBUM in media_types:
                    albums = artist_data.get("results", {}).get("albums", [])
                    result.albums = [self._parse_album(a) for a in albums if a.get("barcode")]

        elif content_type == "album" and MediaType.ALBUM in media_types:
            album_data = await self._api_get(f"/album/{content_id}?resources=tracks")
            if album_data and "item" in album_data:
                result.albums = [self._parse_album(album_data["item"])]
                if MediaType.TRACK in media_types:
                    tracks = album_data.get("results", {}).get("tracks", [])
                    result.tracks = [
                        self._parse_track(t)
                        for t in tracks[:limit]
                        if t.get("barcode") and t.get("streamable") == 2
                    ]

        return result if (result.artists or result.albums or result.tracks) else None

    async def _login(self) -> None:
        """Authenticate with MusicMe via web login and extract the userId."""
        email = self.config.get_value(CONF_USERNAME)
        password = self.config.get_value(CONF_PASSWORD)

        try:
            async with self.http_session.post(
                LOGIN_URL,
                data={
                    "email": email,
                    "password": password,
                    "act": "login",
                    "login_cookie": "1",
                },
                allow_redirects=True,
            ) as resp:
                resp.raise_for_status()
        except (ClientResponseError, ClientConnectionError) as err:
            msg = f"MusicMe login request failed: {err}"
            raise LoginFailed(msg) from err

        try:
            async with self.http_session.get(f"{WEB_BASE}/?f=1") as resp:
                resp.raise_for_status()
                raw_bytes = await resp.read()
                content = raw_bytes.decode("latin-1", errors="replace")
        except (ClientResponseError, ClientConnectionError) as err:
            msg = f"MusicMe page load failed: {err}"
            raise LoginFailed(msg) from err

        match = re.search(r"window\.playerInit\s*=\s*(\{.*?\});", content, re.DOTALL)
        if not match:
            msg = "Login failed — could not find playerInit in MusicMe response"
            raise LoginFailed(msg)

        player_init = json_loads(match.group(1))
        self._user_id = player_init.get("userId")
        if not self._user_id:
            msg = "Login failed — no userId in MusicMe playerInit"
            raise LoginFailed(msg)

        self.logger.info(
            "Successfully logged in to MusicMe (catalog=%s)",
            player_init.get("catalogSize", "?"),
        )

    def _build_base_params(self) -> str:
        """Build the common query string for dataservice API calls."""
        parts = ["format=json", f"partnerid={PARTNER_ID}", f"client={CLIENT_JSON}"]
        if self._user_id:
            parts.append(f"userid={self._user_id}")
        return "&".join(parts)

    @throttle_with_retries
    async def _api_get(self, endpoint: str) -> dict[str, Any] | None:
        """
        Make a GET request to MusicMe dataservice and decrypt the response.

        :param endpoint: API endpoint path (e.g. '/search?query=...')
        """
        url = f"{DATASERVICE_BASE}{endpoint}"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{self._build_base_params()}"

        self.logger.debug("GET %s", endpoint.split("?", maxsplit=1)[0])
        try:
            async with self.http_session.get(url) as response:
                if response.status == 429:
                    try:
                        backoff = min(int(response.headers.get("Retry-After", 10)), 300)
                    except ValueError, TypeError:
                        backoff = 10
                    raise RateLimited("Rate limited", backoff_time=backoff)
                if response.status in (502, 503):
                    raise ResourceTemporarilyUnavailable(
                        "Server temporarily unavailable", backoff_time=30
                    )
                if response.status == 404:
                    return None
                response.raise_for_status()
                raw = await response.text()
        except ClientConnectionError as err:
            path = endpoint.split("?", maxsplit=1)[0]
            raise ResourceTemporarilyUnavailable(
                f"Connection error for {path}", backoff_time=10
            ) from err

        try:
            result: dict[str, Any] = json_loads(decrypt(raw.strip()))
            return result
        except (ValueError, json.JSONDecodeError, IndexError, UnicodeDecodeError) as err:
            self.logger.warning("Failed to decrypt API response: %s", err)
            return None

    async def _get_stream_url(self, track_barcode: str) -> str:
        """
        Get a fresh streaming URL for a track.

        Tickets are single-use and ephemeral — must be generated right before playback.
        """
        nocache = int(time.time() * 1000)
        data = await self._api_get(f"/getstream/{PARTNER_ID}?ref={track_barcode}&nocache={nocache}")
        if not data or "ticket" not in data:
            msg = f"No streaming ticket for {track_barcode}"
            raise MediaNotFoundError(msg)

        # Double-encrypted: API response already decrypted, ticket needs a second pass
        try:
            ticket = decrypt(data["ticket"])
        except (ValueError, TypeError, IndexError, UnicodeDecodeError) as err:
            msg = f"Unable to decode streaming ticket for {track_barcode}"
            raise MediaNotFoundError(msg) from err
        return f"{STREAM_BASE}/{PARTNER_ID}/{ticket}.mp4"
