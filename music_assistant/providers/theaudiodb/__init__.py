"""The AudioDB Metadata provider for Music Assistant."""

from __future__ import annotations

from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, cast

import aiohttp.client_exceptions
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ExternalID,
    ImageType,
    LinkType,
    ProviderFeature,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    Track,
    UniqueList,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
}

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

CONF_ENABLE_IMAGES = "enable_images"
CONF_ENABLE_ARTIST_METADATA = "enable_artist_metadata"
CONF_ENABLE_ALBUM_METADATA = "enable_album_metadata"
CONF_ENABLE_TRACK_METADATA = "enable_track_metadata"

# TheAudioDB field suffix -> ISO 639-1 language code. CN/JP/SE/NO/IL use country-style
# codes that don't match the ISO language code, so the mapping is explicit.
TADB_SUFFIX_TO_ISO: dict[str, str] = {
    "DE": "de",
    "FR": "fr",
    "IT": "it",
    "ES": "es",
    "PT": "pt",
    "NL": "nl",
    "RU": "ru",
    "PL": "pl",
    "HU": "hu",
    "CN": "zh",
    "JP": "ja",
    "SE": "sv",
    "NO": "nb",
    "IL": "he",
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AudioDbMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


class AudioDbMetadataProvider(MetadataProvider):
    """The AudioDB Metadata provider."""

    throttler: Throttler

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        return 20

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key=CONF_ENABLE_ARTIST_METADATA,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_ENABLE_ALBUM_METADATA,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_ENABLE_TRACK_METADATA,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
            ),
            ConfigEntry(
                key=CONF_ENABLE_IMAGES,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache
        self.throttler = Throttler(rate_limit=1, period=1)

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for artist on theaudiodb."""
        if not self.config.get_value(CONF_ENABLE_ARTIST_METADATA):
            return None
        if not artist.mbid:
            # for 100% accuracy we require the musicbrainz id for all lookups
            return None
        self.logger.debug("Fetching metadata for Artist %s on The Audio DB", artist.name)
        if data := await self._get_data("artist-mb.php", i=artist.mbid):
            if data.get("artists"):
                metadata = self.__parse_artist(data["artists"][0])
                if metadata.description:
                    self.logger.debug(
                        "Found bio for %s on TheAudioDB in %s",
                        artist.name,
                        metadata.description_language or "unknown",
                    )
                return metadata
        return None

    async def get_album_metadata(self, album: Album) -> MediaItemMetadata | None:
        """Retrieve metadata for album on theaudiodb."""
        if not self.config.get_value(CONF_ENABLE_ALBUM_METADATA):
            return None
        self.logger.debug("Fetching metadata for Album %s on The Audio DB", album.name)
        if mbid := album.get_external_id(ExternalID.MB_RELEASEGROUP):
            result = await self._get_data("album-mb.php", i=mbid)
            if result and result.get("album"):
                adb_album = result["album"][0]
                return await self.__parse_album(album, adb_album)
            # if there was no match on mbid, there will certainly be no match by name
            return None
        # fallback if no musicbrainzid: lookup by name
        for album_artist in album.artists:
            # make sure to include the version in the album name
            album_name = f"{album.name} {album.version}" if album.version else album.name
            result = await self._get_data("searchalbum.php?", s=album_artist.name, a=album_name)
            if result and result.get("album"):
                for item in result["album"]:
                    # some safety checks
                    if album_artist.mbid:
                        if album_artist.mbid != item["strMusicBrainzArtistID"]:
                            continue
                    elif not compare_strings(album_artist.name, item["strArtist"]):
                        continue
                    if compare_strings(album_name, item["strAlbum"], strict=False):
                        # match found !
                        return await self.__parse_album(album, item)
        return None

    async def get_track_metadata(self, track: Track) -> MediaItemMetadata | None:
        """Retrieve metadata for track on theaudiodb."""
        if not self.config.get_value(CONF_ENABLE_TRACK_METADATA):
            return None
        if track.mbid:
            result = await self._get_data("track-mb.php", i=track.mbid)
            if result and result.get("track"):
                return await self.__parse_track(track, result["track"][0])
            # if there was no match on mbid, there will certainly be no match by name
            return None
        # fallback if no musicbrainzid: lookup by name
        for track_artist in track.artists:
            # make sure to include the version in the album name
            track_name = f"{track.name} {track.version}" if track.version else track.name
            result = await self._get_data("searchtrack.php?", s=track_artist.name, t=track_name)
            if result and result.get("track"):
                for item in result["track"]:
                    # some safety checks
                    if track_artist.mbid:
                        if track_artist.mbid != item["strMusicBrainzArtistID"]:
                            continue
                    elif not compare_strings(track_artist.name, item["strArtist"]):
                        continue
                    if (
                        track.album
                        and (mb_rgid := track.album.get_external_id(ExternalID.MB_RELEASEGROUP))
                        # AudioDb swapped MB Album ID and ReleaseGroup ID ?!
                        and mb_rgid != item["strMusicBrainzAlbumID"]
                    ):
                        continue
                    if track.album and not compare_strings(
                        track.album.name, item["strAlbum"], strict=False
                    ):
                        continue
                    if not compare_strings(track_name, item["strTrack"], strict=False):
                        continue
                    return await self.__parse_track(track, item)
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
        metadata.description, metadata.description_language = self._localized_field(
            artist_obj, "strBiography"
        )
        # images
        if not self.config.get_value(CONF_ENABLE_IMAGES):
            return metadata
        metadata.images = UniqueList()
        for key, img_type in IMG_MAPPING.items():
            for postfix in ("", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
                if img := artist_obj.get(f"{key}{postfix}"):
                    metadata.images.append(
                        MediaItemImage(
                            type=img_type,
                            path=img,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    )
                else:
                    break
        return metadata

    async def __parse_album(self, album: Album, adb_album: dict[str, Any]) -> MediaItemMetadata:
        """Parse audiodb album object to MediaItemMetadata."""
        metadata = MediaItemMetadata()
        # generic data
        metadata.label = adb_album.get("strLabel")
        metadata.style = adb_album.get("strStyle")
        if genre := adb_album.get("strGenre"):
            metadata.genres = {genre}
        metadata.mood = adb_album.get("strMood")
        # links
        metadata.links = set()
        if link := adb_album.get("strWikipediaID"):
            metadata.links.add(
                MediaItemLink(type=LinkType.WIKIPEDIA, url=f"https://wikipedia.org/wiki/{link}")
            )
        if link := adb_album.get("strAllMusicID"):
            metadata.links.add(
                MediaItemLink(type=LinkType.ALLMUSIC, url=f"https://www.allmusic.com/album/{link}")
            )

        # description
        metadata.description, metadata.description_language = self._localized_field(
            adb_album, "strDescription"
        )
        metadata.review = adb_album.get("strReview")
        # images
        if not self.config.get_value(CONF_ENABLE_IMAGES):
            return metadata
        metadata.images = UniqueList()
        for key, img_type in IMG_MAPPING.items():
            for postfix in ("", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
                if img := adb_album.get(f"{key}{postfix}"):
                    metadata.images.append(
                        MediaItemImage(
                            type=img_type,
                            path=img,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    )
                else:
                    break
        # fill in some missing album info if needed
        if not album.year:
            album.year = int(adb_album.get("intYearReleased", "0"))
        if album.album_type == AlbumType.UNKNOWN and adb_album.get("strReleaseFormat"):
            releaseformat = cast("str", adb_album.get("strReleaseFormat"))
            album.album_type = ALBUMTYPE_MAPPING.get(releaseformat, AlbumType.UNKNOWN)
        # update the artist mbid while at it
        for album_artist in album.artists:
            if not compare_strings(album_artist.name, adb_album["strArtist"]):
                continue
            if not album_artist.mbid and album_artist.provider == "library":
                if isinstance(album_artist, ItemMapping):
                    album_artist = self.mass.music.artists.artist_from_item_mapping(album_artist)  # noqa: PLW2901
                album_artist.mbid = adb_album["strMusicBrainzArtistID"]
                await self.mass.music.artists.update_item_in_library(
                    album_artist.item_id,
                    album_artist,
                )
        return metadata

    async def __parse_track(self, track: Track, adb_track: dict[str, Any]) -> MediaItemMetadata:
        """Parse audiodb track object to MediaItemMetadata."""
        metadata = MediaItemMetadata()
        # generic data
        metadata.lyrics = adb_track.get("strTrackLyrics")
        metadata.style = adb_track.get("strStyle")
        if genre := adb_track.get("strGenre"):
            metadata.genres = {genre}
        metadata.mood = adb_track.get("strMood")
        # description
        metadata.description, metadata.description_language = self._localized_field(
            adb_track, "strDescription"
        )
        # images
        if not self.config.get_value(CONF_ENABLE_IMAGES):
            return metadata
        metadata.images = UniqueList([])
        for key, img_type in IMG_MAPPING.items():
            for postfix in ("", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
                if img := adb_track.get(f"{key}{postfix}"):
                    metadata.images.append(
                        MediaItemImage(
                            type=img_type,
                            path=img,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    )
                else:
                    break
        # update the artist mbid while at it
        for album_artist in track.artists:
            if not compare_strings(album_artist.name, adb_track["strArtist"]):
                continue
            if not album_artist.mbid and album_artist.provider == "library":
                if isinstance(album_artist, ItemMapping):
                    album_artist = self.mass.music.artists.artist_from_item_mapping(album_artist)  # noqa: PLW2901
                album_artist.mbid = adb_track["strMusicBrainzArtistID"]
                await self.mass.music.artists.update_item_in_library(
                    album_artist.item_id,
                    album_artist,
                )
        # update the album mbid while at it
        if (
            track.album
            and not track.album.get_external_id(ExternalID.MB_RELEASEGROUP)
            and track.album.provider == "library"
            and isinstance(track.album, Album)
        ):
            track.album.add_external_id(
                ExternalID.MB_RELEASEGROUP, adb_track["strMusicBrainzAlbumID"]
            )
            await self.mass.music.albums.update_item_in_library(track.album.item_id, track.album)
        return metadata

    def _localized_field(self, obj: dict[str, Any], prefix: str) -> tuple[str | None, str | None]:
        """
        Return the best-matching localized text for ``prefix`` and its ISO 639-1 language.

        :param obj: TheAudioDB response object to read fields from.
        :param prefix: Field name prefix (e.g. ``"strBiography"`` or ``"strDescription"``).
        """
        # region-first covers TheAudioDB's CN/JP/SE/NO/IL country-style suffixes, then the
        # language code, and finally the suffix-less field (TheAudioDB's English default)
        parts = self.mass.metadata.locale.split("_", 1)
        lang_code = parts[0].upper()
        region_code = parts[1].upper() if len(parts) > 1 else ""
        for suffix in (region_code, lang_code):
            if not suffix:
                continue
            if value := obj.get(f"{prefix}{suffix}"):
                return value, TADB_SUFFIX_TO_ISO.get(suffix)
        # bare field is the English default
        if value := obj.get(prefix):
            return value, "en"
        return None, None

    # None here only signals a failed request (a miss still returns a body), so don't cache it
    @use_cache(86400 * 90, persistent=True, cache_none=False)  # Cache for 90 days
    async def _get_data(self, endpoint: str, **kwargs: Any) -> dict[str, Any] | None:
        """Get data from api."""
        url = f"https://theaudiodb.com/api/v1/json/{app_var('theaudiodb_api_key')}/{endpoint}"
        async with (
            self.throttler,
            self.mass.http_session.get(url, params=kwargs, ssl=False) as response,
        ):
            try:
                result = cast("dict[str, Any]", await response.json())
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
                TimeoutError,
            ):
                self.logger.warning("Failed to retrieve %s", endpoint)
                return None
            if "error" in result and "limit" in result["error"]:
                self.logger.warning(result["error"])
                return None
            return result
