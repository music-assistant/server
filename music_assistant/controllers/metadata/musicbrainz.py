"""Handle getting Id's from MusicBrainz."""
from __future__ import annotations

import re
from json.decoder import JSONDecodeError

import aiohttp
from asyncio_throttle import Throttler

from music_assistant.helpers.cache import use_cache
from music_assistant.helpers.compare import compare_strings, get_compare_string
from music_assistant.helpers.typing import MusicAssistant

LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'


class MusicBrainz:
    """Handle getting Id's from MusicBrainz."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache
        self.logger = mass.logger.getChild("musicbrainz")
        self.throttler = Throttler(rate_limit=1, period=1)

    async def get_mb_artist_id(
        self,
        artistname,
        albumname=None,
        album_upc=None,
        trackname=None,
        track_isrc=None,
    ):
        """Retrieve musicbrainz artist id for the given details."""
        self.logger.debug(
            "searching musicbrainz for %s \
                (albumname: %s - album_upc: %s - trackname: %s - track_isrc: %s)",
            artistname,
            albumname,
            album_upc,
            trackname,
            track_isrc,
        )
        mb_artist_id = None
        if album_upc:
            mb_artist_id = await self.search_artist_by_album(
                artistname, None, album_upc
            )
            if mb_artist_id:
                self.logger.debug(
                    "Got MusicbrainzArtistId for %s after search on upc %s --> %s",
                    artistname,
                    album_upc,
                    mb_artist_id,
                )
        if not mb_artist_id and track_isrc:
            mb_artist_id = await self.search_artist_by_track(
                artistname, None, track_isrc
            )
            if mb_artist_id:
                self.logger.debug(
                    "Got MusicbrainzArtistId for %s after search on isrc %s --> %s",
                    artistname,
                    track_isrc,
                    mb_artist_id,
                )
        if not mb_artist_id and albumname:
            mb_artist_id = await self.search_artist_by_album(artistname, albumname)
            if mb_artist_id:
                self.logger.debug(
                    "Got MusicbrainzArtistId for %s after search on albumname %s --> %s",
                    artistname,
                    albumname,
                    mb_artist_id,
                )
        if not mb_artist_id and trackname:
            mb_artist_id = await self.search_artist_by_track(artistname, trackname)
            if mb_artist_id:
                self.logger.debug(
                    "Got MusicbrainzArtistId for %s after search on trackname %s --> %s",
                    artistname,
                    trackname,
                    mb_artist_id,
                )
        return mb_artist_id

    async def search_artist_by_album(self, artistname, albumname=None, album_upc=None):
        """Retrieve musicbrainz artist id by providing the artist name and albumname or upc."""
        for searchartist in [
            re.sub(LUCENE_SPECIAL, r"\\\1", artistname),
            get_compare_string(artistname),
        ]:
            if album_upc:
                query = f"barcode:{album_upc}"
            else:
                searchalbum = re.sub(LUCENE_SPECIAL, r"\\\1", albumname)
                query = f'artist:"{searchartist}" AND release:"{searchalbum}"'
            result = await self.get_data("release", query=query)
            if result and "releases" in result:
                for strictness in [True, False]:
                    for item in result["releases"]:
                        if album_upc or compare_strings(
                            item["title"], albumname, strictness
                        ):
                            for artist in item["artist-credit"]:
                                if compare_strings(
                                    artist["artist"]["name"], artistname, strictness
                                ):
                                    return artist["artist"]["id"]
                                for alias in artist.get("aliases", []):
                                    if compare_strings(
                                        alias["name"], artistname, strictness
                                    ):
                                        return artist["id"]
        return ""

    async def search_artist_by_track(self, artistname, trackname=None, track_isrc=None):
        """Retrieve artist id by providing the artist name and trackname or track isrc."""
        searchartist = re.sub(LUCENE_SPECIAL, r"\\\1", artistname)
        if track_isrc:
            result = await self.get_data(f"isrc/{track_isrc}", inc="artist-credits")
        else:
            searchtrack = re.sub(LUCENE_SPECIAL, r"\\\1", trackname)
            result = await self.get_data(
                "recording", query=f'"{searchtrack}" AND artist:"{searchartist}"'
            )
        if result and "recordings" in result:
            for strictness in [True, False]:
                for item in result["recordings"]:
                    if track_isrc or compare_strings(
                        item["title"], trackname, strictness
                    ):
                        for artist in item["artist-credit"]:
                            if compare_strings(
                                artist["artist"]["name"], artistname, strictness
                            ):
                                return artist["artist"]["id"]
                            for alias in artist.get("aliases", []):
                                if compare_strings(
                                    alias["name"], artistname, strictness
                                ):
                                    return artist["id"]
        return ""

    @use_cache(86400 * 30)
    async def get_data(self, endpoint: str, **kwargs):
        """Get data from api."""
        url = f"http://musicbrainz.org/ws/2/{endpoint}"
        headers = {
            "User-Agent": "Music Assistant/1.0.0 https://github.com/music-assistant"
        }
        kwargs["fmt"] = "json"
        async with self.throttler:
            async with self.mass.http_session.get(
                url, headers=headers, params=kwargs, verify_ssl=False
            ) as response:
                try:
                    result = await response.json()
                except (
                    aiohttp.client_exceptions.ContentTypeError,
                    JSONDecodeError,
                ) as exc:
                    msg = await response.text()
                    self.logger.error("%s - %s", str(exc), msg)
                    result = None
                return result
