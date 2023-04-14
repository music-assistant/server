"""The AudioDB Metadata provider for Music Assistant."""
from __future__ import annotations

from json import JSONDecodeError
from typing import TYPE_CHECKING, Any

import aiohttp.client_exceptions
from asyncio_throttle import Throttler

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ProviderFeature
from music_assistant.common.models.media_items import (
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
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.helpers.app_vars import app_var  # pylint: disable=no-name-in-module
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

SUPPORTED_FEATURES = (
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.GET_ARTIST_MBID,
)

IMG_MAPPING = {
    "strArtistThumb": ImageType.THUMB,
    "strArtistLogo": ImageType.LOGO,
    "strArtistCutout": ImageType.CUTOUT,
    "strArtistClearart": ImageType.CLEARART,
    "strArtistWideThumb": ImageType.LANDSCAPE,
    "strArtistFanart": ImageType.FANART,
    "strArtistBanner": ImageType.BANNER,
    "strAlbumThumb": ImageType.THUMB,
    "strAlbumThumbHQ": ImageType.THUMB,
    "strAlbumCDart": ImageType.DISCART,
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
    "EP": AlbumType.EP,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = AudioDbMetadataProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return tuple()  # we do not have any config entries (yet)


class AudioDbMetadataProvider(MetadataProvider):
    """The AudioDB Metadata provider."""

    throttler: Throttler

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache
        self.throttler = Throttler(rate_limit=2, period=1)

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for artist on theaudiodb."""
        if data := await self._get_data("artist-mb.php", i=artist.musicbrainz_id):  # noqa: SIM102
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
        elif album.artists:
            # lookup by name
            artist = album.artists[0]
            result = await self._get_data("searchalbum.php", s=artist.name, a=album.name)
            if result and result.get("album"):
                for item in result["album"]:
                    assert isinstance(artist, Artist)
                    if artist.musicbrainz_id:
                        if artist.musicbrainz_id != item["strMusicBrainzArtistID"]:
                            continue
                    elif not compare_strings(artist.name, item["strArtistStripped"]):
                        continue
                    if compare_strings(album.name, item["strAlbumStripped"]):
                        adb_album = item
                        break
        if adb_album:
            if not album.year:
                album.year = int(adb_album.get("intYearReleased", "0"))
            if not album.musicbrainz_id:
                album.musicbrainz_id = adb_album["strMusicBrainzID"]
            assert isinstance(album.artists[0], Artist)
            if album.artists and not album.artists[0].musicbrainz_id:
                album.artists[0].musicbrainz_id = adb_album["strMusicBrainzArtistID"]
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
            assert isinstance(track_artist, Artist)
            result = await self._get_data("searchtrack.php?", s=track_artist.name, t=track.name)
            if result and result.get("track"):
                for item in result["track"]:
                    if track_artist.musicbrainz_id:
                        if track_artist.musicbrainz_id != item["strMusicBrainzArtistID"]:
                            continue
                    elif not compare_strings(track_artist.name, item["strArtist"]):
                        continue
                    if compare_strings(track.name, item["strTrack"]):
                        adb_track = item
                        break
            if adb_track:
                if not track.musicbrainz_id:
                    track.musicbrainz_id = adb_track["strMusicBrainzID"]
                assert isinstance(track.album, Album)
                if track.album and not track.album.musicbrainz_id:
                    track.album.musicbrainz_id = adb_track["strMusicBrainzAlbumID"]
                if not track_artist.musicbrainz_id:
                    track_artist.musicbrainz_id = adb_track["strMusicBrainzArtistID"]

                return self.__parse_track(adb_track)
        return None

    async def get_musicbrainz_artist_id(
        self,
        artist: Artist,
        ref_albums: Iterable[Album],
        ref_tracks: Iterable[Track],  # noqa: ARG002
    ) -> str | None:
        """Discover MusicBrainzArtistId for an artist given some reference albums/tracks."""
        musicbrainz_id = None
        if data := await self._get_data("searchalbum.php", s=artist.name):
            # NOTE: object is 'null' when no records found instead of empty array
            albums = data.get("album") or []
            for item in albums:
                if not compare_strings(item["strArtistStripped"], artist.name):
                    continue
                for ref_album in ref_albums:
                    if not compare_strings(item["strAlbumStripped"], ref_album.name):
                        continue
                    # found match - update album metadata too while we're here
                    if not ref_album.musicbrainz_id:
                        ref_album.metadata = self.__parse_album(item)
                        await self.mass.music.albums.add(ref_album, skip_metadata_lookup=True)
                    musicbrainz_id = item["strMusicBrainzArtistID"]

        return musicbrainz_id

    def __parse_artist(self, artist_obj: dict[str, Any]) -> MediaItemMetadata:
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
        if desc := artist_obj.get(f"strBiography{self.mass.metadata.preferred_language}"):
            metadata.description = desc
        else:
            metadata.description = artist_obj.get("strBiographyEN")
        # images
        metadata.images = []
        for key, img_type in IMG_MAPPING.items():
            for postfix in ("", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
                if img := artist_obj.get(f"{key}{postfix}"):
                    metadata.images.append(MediaItemImage(img_type, img))
                else:
                    break
        return metadata

    def __parse_album(self, album_obj: dict[str, Any]) -> MediaItemMetadata:
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
                MediaItemLink(LinkType.ALLMUSIC, f"https://www.allmusic.com/album/{link}")
            )

        # description
        if desc := album_obj.get(f"strDescription{self.mass.metadata.preferred_language}"):
            metadata.description = desc
        else:
            metadata.description = album_obj.get("strDescriptionEN")
        metadata.review = album_obj.get("strReview")
        # images
        metadata.images = []
        for key, img_type in IMG_MAPPING.items():
            for postfix in ("", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
                if img := album_obj.get(f"{key}{postfix}"):
                    metadata.images.append(MediaItemImage(img_type, img))
                else:
                    break
        return metadata

    def __parse_track(self, track_obj: dict[str, Any]) -> MediaItemMetadata:
        """Parse audiodb track object to MediaItemMetadata."""
        metadata = MediaItemMetadata()
        # generic data
        metadata.lyrics = track_obj.get("strTrackLyrics")
        metadata.style = track_obj.get("strStyle")
        if genre := track_obj.get("strGenre"):
            metadata.genres = {genre}
        metadata.mood = track_obj.get("strMood")
        # description
        if desc := track_obj.get(f"strDescription{self.mass.metadata.preferred_language}"):
            metadata.description = desc
        else:
            metadata.description = track_obj.get("strDescriptionEN")
        # images
        metadata.images = []
        for key, img_type in IMG_MAPPING.items():
            for postfix in ("", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
                if img := track_obj.get(f"{key}{postfix}"):
                    metadata.images.append(MediaItemImage(img_type, img))
                else:
                    break
        return metadata

    @use_cache(86400 * 14)
    async def _get_data(self, endpoint, **kwargs) -> dict | None:
        """Get data from api."""
        url = f"https://theaudiodb.com/api/v1/json/{app_var(3)}/{endpoint}"
        async with self.throttler:
            async with self.mass.http_session.get(url, params=kwargs, ssl=False) as response:
                try:
                    result = await response.json()
                except (
                    aiohttp.client_exceptions.ContentTypeError,
                    JSONDecodeError,
                ):
                    self.logger.error("Failed to retrieve %s", endpoint)
                    text_result = await response.text()
                    self.logger.debug(text_result)
                    return None
                except (
                    aiohttp.client_exceptions.ClientConnectorError,
                    aiohttp.client_exceptions.ServerDisconnectedError,
                ):
                    self.logger.warning("Failed to retrieve %s", endpoint)
                    return None
                if "error" in result and "limit" in result["error"]:
                    self.logger.warning(result["error"])
                    return None
                return result
