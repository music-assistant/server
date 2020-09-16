"""All logic for metadata retrieval."""
# TODO: split up into (optional) providers
import json
import logging
import re
from typing import Optional

import aiohttp
from asyncio_throttle import Throttler
from music_assistant.cache import async_use_cache
from music_assistant.utils import compare_strings, get_compare_string

LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'

LOGGER = logging.getLogger("mass")


class MetaData:
    """Several helpers to search and store metadata for mediaitems."""

    # TODO: create periodic task to search for missing metadata
    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        self.musicbrainz = MusicBrainz(mass)
        self.fanarttv = FanartTv(mass)

    async def async_get_artist_metadata(self, mb_artist_id, cur_metadata):
        """Get/update rich metadata for an artist by providing the musicbrainz artist id."""
        metadata = cur_metadata
        if "fanart" not in metadata:
            res = await self.fanarttv.async_get_artist_images(mb_artist_id)
            if res:
                self.merge_metadata(cur_metadata, res)
        return metadata

    async def async_get_mb_artist_id(
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
            mb_artist_id = await self.musicbrainz.async_search_artist_by_album(
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
            mb_artist_id = await self.musicbrainz.async_search_artist_by_track(
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
            mb_artist_id = await self.musicbrainz.async_search_artist_by_album(
                artistname, albumname
            )
            if mb_artist_id:
                LOGGER.debug(
                    "Got MusicbrainzArtistId for %s after search on albumname %s --> %s",
                    artistname,
                    albumname,
                    mb_artist_id,
                )
        if not mb_artist_id and trackname:
            mb_artist_id = await self.musicbrainz.async_search_artist_by_track(
                artistname, trackname
            )
            if mb_artist_id:
                LOGGER.debug(
                    "Got MusicbrainzArtistId for %s after search on trackname %s --> %s",
                    artistname,
                    trackname,
                    mb_artist_id,
                )
        return mb_artist_id

    @staticmethod
    def merge_metadata(cur_metadata, new_values):
        """Merge new info into the metadata dict without overwriting existing values."""
        for key, value in new_values.items():
            if not cur_metadata.get(key):
                cur_metadata[key] = value
        return cur_metadata


class MusicBrainz:
    """Handle getting Id's from MusicBrainz."""

    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache
        self.throttler = Throttler(rate_limit=1, period=1)

    async def async_search_artist_by_album(
        self, artistname, albumname=None, album_upc=None
    ):
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
            result = await self.async_get_data(endpoint, params)
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

    async def async_search_artist_by_track(
        self, artistname, trackname=None, track_isrc=None
    ):
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
        result = await self.async_get_data(endpoint, params)
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

    @async_use_cache(2)
    async def async_get_data(self, endpoint: str, params: Optional[dict] = None):
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
                    json.decoder.JSONDecodeError,
                ) as exc:
                    msg = await response.text()
                    LOGGER.exception("%s - %s", str(exc), msg)
                    result = None
                return result


class FanartTv:
    """FanartTv support for metadata retrieval."""

    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache
        self.throttler = Throttler(rate_limit=1, period=2)

    async def async_get_artist_images(self, mb_artist_id):
        """Retrieve images by musicbrainz artist id."""
        metadata = {}
        data = await self.async_get_data("music/%s" % mb_artist_id)
        if data:
            if data.get("hdmusiclogo"):
                metadata["logo"] = data["hdmusiclogo"][0]["url"]
            elif data.get("musiclogo"):
                metadata["logo"] = data["musiclogo"][0]["url"]
            if data.get("artistbackground"):
                count = 0
                for item in data["artistbackground"]:
                    key = "fanart" if count == 0 else "fanart.%s" % count
                    metadata[key] = item["url"]
            if data.get("artistthumb"):
                url = data["artistthumb"][0]["url"]
                if "2a96cbd8b46e442fc41c2b86b821562f" not in url:
                    metadata["image"] = url
            if data.get("musicbanner"):
                metadata["banner"] = data["musicbanner"][0]["url"]
        return metadata

    @async_use_cache(30)
    async def async_get_data(self, endpoint, params=None):
        """Get data from api."""
        if params is None:
            params = {}
        url = "http://webservice.fanart.tv/v3/%s" % endpoint
        params["api_key"] = "639191cb0774661597f28a47e7e2bad5"
        async with self.throttler:
            async with self.mass.http_session.get(
                url, params=params, verify_ssl=False
            ) as response:
                try:
                    result = await response.json()
                except (
                    aiohttp.client_exceptions.ContentTypeError,
                    json.decoder.JSONDecodeError,
                ):
                    LOGGER.error("Failed to retrieve %s", endpoint)
                    text_result = await response.text()
                    LOGGER.debug(text_result)
                    return None
                except aiohttp.client_exceptions.ClientConnectorError:
                    LOGGER.error("Failed to retrieve %s", endpoint)
                    return None
                if "error" in result and "limit" in result["error"]:
                    LOGGER.error(result["error"])
                    return None
                return result
