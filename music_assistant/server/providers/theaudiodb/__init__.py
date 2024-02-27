"""The AudioDB Metadata provider for Music Assistant."""

from __future__ import annotations

from json import JSONDecodeError
from typing import TYPE_CHECKING, Any

import aiohttp.client_exceptions
from asyncio_throttle import Throttler

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
    from music_assistant.common.models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

SUPPORTED_FEATURES = (
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
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
    await prov.handle_async_init()
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
    return ()  # we do not have any config entries (yet)


class AudioDbMetadataProvider(MetadataProvider):
    """The AudioDB Metadata provider."""

    throttler: Throttler

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache
        self.throttler = Throttler(rate_limit=2, period=1)

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for artist on theaudiodb."""
        if not artist.mbid:
            # for 100% accuracy we require the musicbrainz id for all lookups
            return None
        if data := await self._get_data("artist-mb.php", i=artist.mbid):
            if data.get("artists"):
                return self.__parse_artist(data["artists"][0])
        return None

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Retrieve metadata for album on theaudiodb."""
        if not album.mbid:
            # for 100% accuracy we require the musicbrainz id for all lookups
            return None
        result = await self._get_data("album-mb.php", i=album.mbid)
        if result and result.get("album"):
            adb_album = result["album"][0]
            # fill in some missing album info if needed
            if not album.year:
                album.year = int(adb_album.get("intYearReleased", "0"))
            if album.artists and not album.artists[0].mbid:
                album.artists[0].mbid = adb_album["strMusicBrainzArtistID"]
            if album.album_type == AlbumType.UNKNOWN:
                album.album_type = ALBUMTYPE_MAPPING.get(
                    adb_album.get("strReleaseFormat"), AlbumType.UNKNOWN
                )
            return self.__parse_album(adb_album)
        return None

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """Retrieve metadata for track on theaudiodb."""
        if track.mbid:
            result = await self._get_data("track-mb.php", i=track.mbid)
            if result and result.get("track"):
                return self.__parse_track(result["track"][0])
            # if there was no match on mbid, there will certainly be no match by name
            return None
        # fallback if no musicbrainzid: lookup by name
        for track_artist in track.artists:
            # make sure to include the version in the track name
            track_name = f"{track.name} {track.version}" if track.version else track.name
            result = await self._get_data("searchtrack.php?", s=track_artist.name, t=track_name)
            if result and result.get("track"):
                for item in result["track"]:
                    # some safety checks
                    if track_artist.mbid and track_artist.mbid != item["strMusicBrainzArtistID"]:
                        continue
                    if (
                        track.album
                        and track.album.mbid
                        and track.album.mbid != item["strMusicBrainzAlbumID"]
                    ):
                        continue
                    if not compare_strings(track_artist.name, item["strArtist"]):
                        continue
                    if compare_strings(track_name, item["strTrack"]):
                        return self.__parse_track(item)
        return None

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
                metadata.links.add(MediaItemLink(type=link_type, url=link))
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
                    metadata.images.append(MediaItemImage(type=img_type, path=img))
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
                MediaItemLink(type=LinkType.WIKIPEDIA, url=f"https://wikipedia.org/wiki/{link}")
            )
        if link := album_obj.get("strAllMusicID"):
            metadata.links.add(
                MediaItemLink(type=LinkType.ALLMUSIC, url=f"https://www.allmusic.com/album/{link}")
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
                    metadata.images.append(MediaItemImage(type=img_type, path=img))
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
                    metadata.images.append(MediaItemImage(type=img_type, path=img))
                else:
                    break
        return metadata

    @use_cache(86400 * 14)
    async def _get_data(self, endpoint, **kwargs) -> dict | None:
        """Get data from api."""
        url = f"https://theaudiodb.com/api/v1/json/{app_var(3)}/{endpoint}"
        async with (
            self.throttler,
            self.mass.http_session.get(url, params=kwargs, ssl=False) as response,
        ):
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
