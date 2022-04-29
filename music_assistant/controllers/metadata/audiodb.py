"""TheAudioDb Metadata provider."""
from __future__ import annotations

from json.decoder import JSONDecodeError
from typing import Any, Dict, Optional

import aiohttp
from asyncio_throttle import Throttler

from music_assistant.helpers.app_vars import (  # pylint: disable=no-name-in-module
    app_var,
)
from music_assistant.helpers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ImageType,
    LinkType,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    Track,
)

IMG_MAPPING = {
    "strArtistThumb": ImageType.THUMB,
    "strArtistLogo": ImageType.LOGO,
    "strArtistCutout": ImageType.CUTOUT,
    "strArtistClearart": ImageType.CLEARART,
    "strArtistWideThumb": ImageType.WIDE_THUMB,
    "strArtistFanart": ImageType.FANART,
    "strArtistBanner": ImageType.BANNER,
    "strAlbumThumb": ImageType.THUMB,
    "strAlbumThumbHQ": ImageType.THUMB,
    "strAlbumCDart": ImageType.CDART,
    "strAlbum3DCase": ImageType.OTHER,
    "strAlbum3DFlat": ImageType.OTHER,
    "strAlbum3DFace": ImageType.OTHER,
    "strAlbum3DThumb": ImageType.OTHER,
    "strTrackThumb": ImageType.THUMB,
    "strTrack3DCase": ImageType.OTHER,
}

LINK_MAPPING = {
    "strWebsite": LinkType.WEBSITE,
    "strFacebook": LinkType.FACEBOOK,
    "strTwitter": LinkType.TWITTER,
    "strLastFMChart": LinkType.LASTFM,
}

ALBUMTYPE_MAPPING = {
    "Single": AlbumType.SINGLE,
    "Compilation": AlbumType.COMPILATION,
    "Album": AlbumType.ALBUM,
}


class TheAudioDb:
    """TheAudioDb metadata provider."""

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache
        self.logger = mass.logger.getChild("audiodb")
        self.throttler = Throttler(rate_limit=2, period=1)

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for artist on theaudiodb."""
        self.logger.debug("Fetching metadata for Artist %s on TheAudioDb", artist.name)
        if data := await self._get_data("artist-mb.php", i=artist.musicbrainz_id):
            if data.get("artists"):
                return self.__parse_artist(data["artists"][0])
        return None

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Retrieve metadata for album on theaudiodb."""
        adb_album = None
        if album.musicbrainz_id:
            result = await self._get_data("album-mb.php", i=album.musicbrainz_id)
            if result and result.get("album"):
                adb_album = result["album"][0]
        else:
            # lookup by name
            result = await self._get_data(
                "searchalbum.php", s=album.artist.name, a=album.name
            )
            if result and result.get("album"):
                for item in result["album"]:
                    if album.artist.musicbrainz_id:
                        if (
                            album.artist.musicbrainz_id
                            != item["strMusicBrainzArtistID"]
                        ):
                            continue
                    elif not compare_strings(
                        album.artist.name, item["strArtistStripped"]
                    ):
                        continue
                    if compare_strings(album.name, item["strAlbumStripped"]):
                        adb_album = item
                        break
        if adb_album:
            if not album.year:
                album.year = int(adb_album.get("intYearReleased", "0"))
            if not album.musicbrainz_id:
                album.musicbrainz_id = adb_album["strMusicBrainzID"]
            if not album.artist.musicbrainz_id:
                album.artist.musicbrainz_id = adb_album["strMusicBrainzArtistID"]
            if album.album_type == AlbumType.UNKNOWN:
                album.album_type = ALBUMTYPE_MAPPING.get(
                    adb_album.get("strReleaseFormat"), AlbumType.UNKNOWN
                )
            return self.__parse_album(adb_album)
        return None

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """Retrieve metadata for track on theaudiodb."""
        adb_track = None
        if track.musicbrainz_id:
            result = await self._get_data("track-mb.php", i=track.musicbrainz_id)
            if result and result.get("track"):
                return self.__parse_track(result["track"][0])

        # lookup by name
        for track_artist in track.artists:
            result = await self._get_data(
                "searchtrack.php?", s=track_artist.name, t=track.name
            )
            if result and result.get("track"):
                for item in result["track"]:
                    if track_artist.musicbrainz_id:
                        if (
                            track_artist.musicbrainz_id
                            != item["strMusicBrainzArtistID"]
                        ):
                            continue
                    elif not compare_strings(track_artist.name, item["strArtist"]):
                        continue
                    if compare_strings(track.name, item["strTrack"]):
                        adb_track = item
                        break
            if adb_track:
                if not track.musicbrainz_id:
                    track.musicbrainz_id = adb_track["strMusicBrainzID"]
                if not track.album.musicbrainz_id:
                    track.album.musicbrainz_id = adb_track["strMusicBrainzAlbumID"]
                if not track_artist.musicbrainz_id:
                    track_artist.musicbrainz_id = adb_track["strMusicBrainzArtistID"]

                return self.__parse_track(adb_track)
        return None

    def __parse_artist(self, artist_obj: Dict[str, Any]) -> MediaItemMetadata:
        """Parse audiodb artist object to MediaItemMetadata."""
        metadata = MediaItemMetadata()
        # generic data
        metadata.label = artist_obj.get("strLabel")
        metadata.style = artist_obj.get("strStyle")
        if genre := artist_obj.get("strGenre"):
            metadata.genres = {genre}
        metadata.mood = artist_obj.get("strMood")
        # links
        metadata.links = set()
        for key, link_type in LINK_MAPPING.items():
            if link := artist_obj.get(key):
                metadata.links.add(MediaItemLink(link_type, link))
        # description/biography
        if desc := artist_obj.get(
            f"strBiography{self.mass.metadata.preferred_language}"
        ):
            metadata.description = desc
        else:
            metadata.description = artist_obj.get("strBiographyEN")
        # images
        metadata.images = set()
        for key, img_type in IMG_MAPPING.items():
            for postfix in ("", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
                if img := artist_obj.get(f"{key}{postfix}"):
                    metadata.images.add(MediaItemImage(img_type, img))
                else:
                    break
        return metadata

    def __parse_album(self, album_obj: Dict[str, Any]) -> MediaItemMetadata:
        """Parse audiodb album object to MediaItemMetadata."""
        metadata = MediaItemMetadata()
        # generic data
        metadata.label = album_obj.get("strLabel")
        metadata.style = album_obj.get("strStyle")
        if genre := album_obj.get("strGenre"):
            metadata.genres = {genre}
        metadata.mood = album_obj.get("strMood")
        # links
        metadata.links = set()
        if link := album_obj.get("strWikipediaID"):
            metadata.links.add(
                MediaItemLink(LinkType.WIKIPEDIA, f"https://wikipedia.org/wiki/{link}")
            )
        if link := album_obj.get("strAllMusicID"):
            metadata.links.add(
                MediaItemLink(
                    LinkType.ALLMUSIC, f"https://www.allmusic.com/album/{link}"
                )
            )

        # description
        if desc := album_obj.get(
            f"strDescription{self.mass.metadata.preferred_language}"
        ):
            metadata.description = desc
        else:
            metadata.description = album_obj.get("strDescriptionEN")
        metadata.review = album_obj.get("strReview")
        # images
        metadata.images = set()
        for key, img_type in IMG_MAPPING.items():
            for postfix in ("", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
                if img := album_obj.get(f"{key}{postfix}"):
                    metadata.images.add(MediaItemImage(img_type, img))
                else:
                    break
        return metadata

    def __parse_track(self, track_obj: Dict[str, Any]) -> MediaItemMetadata:
        """Parse audiodb track object to MediaItemMetadata."""
        metadata = MediaItemMetadata()
        # generic data
        metadata.lyrics = track_obj.get("strTrackLyrics")
        metadata.style = track_obj.get("strStyle")
        if genre := track_obj.get("strGenre"):
            metadata.genres = {genre}
        metadata.mood = track_obj.get("strMood")
        # description
        if desc := track_obj.get(
            f"strDescription{self.mass.metadata.preferred_language}"
        ):
            metadata.description = desc
        else:
            metadata.description = track_obj.get("strDescriptionEN")
        # images
        metadata.images = set()
        for key, img_type in IMG_MAPPING.items():
            for postfix in ("", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
                if img := track_obj.get(f"{key}{postfix}"):
                    metadata.images.add(MediaItemImage(img_type, img))
                else:
                    break
        return metadata

    @use_cache(86400 * 14)
    async def _get_data(self, endpoint, **kwargs) -> Optional[dict]:
        """Get data from api."""
        url = f"https://theaudiodb.com/api/v1/json/{app_var(3)}/{endpoint}"
        async with self.throttler:
            async with self.mass.http_session.get(
                url, params=kwargs, verify_ssl=False
            ) as response:
                try:
                    result = await response.json()
                except (
                    aiohttp.ContentTypeError,
                    JSONDecodeError,
                ):
                    self.logger.error("Failed to retrieve %s", endpoint)
                    text_result = await response.text()
                    self.logger.debug(text_result)
                    return None
                except aiohttp.ClientConnectorError:
                    self.logger.error("Failed to retrieve %s", endpoint)
                    return None
                if "error" in result and "limit" in result["error"]:
                    self.logger.error(result["error"])
                    return None
                return result
