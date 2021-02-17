"""Handle getting Id's from MusicBrainz."""

import logging
import re
from json.decoder import JSONDecodeError
from typing import Optional

import aiohttp
from asyncio_throttle import Throttler
from music_assistant.helpers.cache import use_cache
from music_assistant.helpers.compare import compare_strings, get_compare_string

LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'

LOGGER = logging.getLogger("musicbrainz")


class MusicBrainz:
    """Handle getting Id's from MusicBrainz."""

    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache
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
        LOGGER.debug(
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
                LOGGER.debug(
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
                LOGGER.debug(
                    "Got MusicbrainzArtistId for %s after search on isrc %s --> %s",
                    artistname,
                    track_isrc,
                    mb_artist_id,
                )
        if not mb_artist_id and albumname:
            mb_artist_id = await self.search_artist_by_album(artistname, albumname)
            if mb_artist_id:
                LOGGER.debug(
                    "Got MusicbrainzArtistId for %s after search on albumname %s --> %s",
                    artistname,
                    albumname,
                    mb_artist_id,
                )
        if not mb_artist_id and trackname:
            mb_artist_id = await self.search_artist_by_track(artistname, trackname)
            if mb_artist_id:
                LOGGER.debug(
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
                endpoint = "release"
                params = {"query": "barcode:%s" % album_upc}
            else:
                searchalbum = re.sub(LUCENE_SPECIAL, r"\\\1", albumname)
                endpoint = "release"
                params = {
                    "query": 'artist:"%s" AND release:"%s"'
                    % (searchartist, searchalbum)
                }
            result = await self.get_data(endpoint, params)
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
        endpoint = "recording"
        searchartist = re.sub(LUCENE_SPECIAL, r"\\\1", artistname)
        # searchartist = searchartist.replace('/','').replace('\\','').replace('-', '')
        if track_isrc:
            endpoint = "isrc/%s" % track_isrc
            params = {"inc": "artist-credits"}
        else:
            searchtrack = re.sub(LUCENE_SPECIAL, r"\\\1", trackname)
            endpoint = "recording"
            params = {"query": '"%s" AND artist:"%s"' % (searchtrack, searchartist)}
        result = await self.get_data(endpoint, params)
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

    @use_cache(2)
    async def get_data(self, endpoint: str, params: Optional[dict] = None):
        """Get data from api."""
        if params is None:
            params = {}
        url = "http://musicbrainz.org/ws/2/%s" % endpoint
        headers = {"User-Agent": "Music Assistant/1.0.0 https://github.com/marcelveldt"}
        params["fmt"] = "json"
        async with self.throttler:
            async with self.mass.http_session.get(
                url, headers=headers, params=params, verify_ssl=False
            ) as response:
                try:
                    result = await response.json()
                except (
                    aiohttp.client_exceptions.ContentTypeError,
                    JSONDecodeError,
                ) as exc:
                    msg = await response.text()
                    LOGGER.error("%s - %s", str(exc), msg)
                    result = None
                return result
