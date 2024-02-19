"""The Musicbrainz Metadata provider for Music Assistant.

At this time only used for retrieval of ID's but to be expanded to fetch metadata too.
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any

import aiohttp.client_exceptions
from asyncio_throttle import Throttler
from mashumaro import DataClassDictMixin
from mashumaro.exceptions import MissingField

from music_assistant.common.helpers.util import parse_title_and_version
from music_assistant.common.models.enums import ExternalID, ProviderFeature
from music_assistant.common.models.errors import InvalidDataError
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant.common.models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant.common.models.media_items import Album, Artist, Track
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'

SUPPORTED_FEATURES = ()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = MusicbrainzProvider(mass, manifest, config)
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


def replace_hyphens(data: dict[str, Any]) -> dict[str, Any]:
    """Change all hyphens to underscores."""
    new_values = {}
    for key, value in data.items():
        new_key = key.replace("-", "_")
        if isinstance(value, dict):
            new_values[new_key] = replace_hyphens(value)
        elif isinstance(value, list):
            new_values[new_key] = [replace_hyphens(x) if isinstance(x, dict) else x for x in value]
        else:
            new_values[new_key] = value
    return new_values


@dataclass
class MusicBrainzTag(DataClassDictMixin):
    """Model for a (basic) Tag object as received from the MusicBrainz API."""

    count: int
    name: str


@dataclass
class MusicBrainzAlias(DataClassDictMixin):
    """Model for a (basic) Alias object from MusicBrainz."""

    name: str
    sort_name: str

    # optional fields
    locale: str | None = None
    type: str | None = None
    primary: bool | None = None
    begin_date: str | None = None
    end_date: str | None = None


@dataclass
class MusicBrainzArtist(DataClassDictMixin):
    """Model for a (basic) Artist object from MusicBrainz."""

    id: str
    name: str
    sort_name: str

    # optional fields
    aliases: list[MusicBrainzAlias] | None = None
    tags: list[MusicBrainzTag] | None = None


@dataclass
class MusicBrainzArtistCredit(DataClassDictMixin):
    """Model for a (basic) ArtistCredit object from MusicBrainz."""

    name: str
    artist: MusicBrainzArtist


@dataclass
class MusicBrainzReleaseGroup(DataClassDictMixin):
    """Model for a (basic) ReleaseGroup object from MusicBrainz."""

    id: str
    primary_type_id: str
    title: str
    primary_type: str

    # optional fields
    secondary_types: list[str] | None = None
    secondary_type_ids: list[str] | None = None
    artist_credit: list[MusicBrainzArtistCredit] | None = None


@dataclass
class MusicBrainzTrack(DataClassDictMixin):
    """Model for a (basic) Track object from MusicBrainz."""

    id: str
    number: str
    title: str
    length: int | None = None


@dataclass
class MusicBrainzMedia(DataClassDictMixin):
    """Model for a (basic) Media object from MusicBrainz."""

    format: str
    track: list[MusicBrainzTrack]
    position: int = 0
    track_count: int = 0
    track_offset: int = 0


@dataclass
class MusicBrainzRelease(DataClassDictMixin):
    """Model for a (basic) Release object from MusicBrainz."""

    id: str
    status_id: str
    count: int
    title: str
    status: str
    artist_credit: list[MusicBrainzArtistCredit]
    release_group: MusicBrainzReleaseGroup
    track_count: int = 0

    # optional fields
    media: list[MusicBrainzMedia] = field(default_factory=list)
    date: str | None = None
    country: str | None = None
    disambiguation: str | None = None  # version
    # TODO (if needed): release-events


@dataclass
class MusicBrainzRecording(DataClassDictMixin):
    """Model for a (basic) Recording object as received from the MusicBrainz API."""

    id: str
    title: str
    artist_credit: list[MusicBrainzArtistCredit] = field(default_factory=list)
    # optional fields
    length: int | None = None
    first_release_date: str | None = None
    isrcs: list[str] | None = None
    tags: list[MusicBrainzTag] | None = None
    disambiguation: str | None = None  # version (e.g. live, karaoke etc.)


class MusicbrainzProvider(MetadataProvider):
    """The Musicbrainz Metadata provider."""

    throttler: Throttler

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache
        self.throttler = Throttler(rate_limit=1, period=1)

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def get_musicbrainz_artist_id(
        self, artist: Artist, ref_albums: Iterable[Album], ref_tracks: Iterable[Track]
    ) -> str | None:
        """Discover MusicBrainzArtistId for an artist given some reference albums/tracks."""
        if artist.mbid:
            return artist.mbid
        # try with (strict) ref track(s), using recording id or isrc
        for ref_track in ref_tracks:
            if mb_artist := await self.get_artist_details_by_track(artist.name, ref_track):
                return mb_artist.id
        # try with (strict) ref album(s), using releasegroup id or barcode
        for ref_album in ref_albums:
            if mb_artist := await self.get_artist_details_by_album(artist.name, ref_album):
                return mb_artist.id
        # last restort: track matching by name
        for ref_track in ref_tracks:
            if not ref_track.album:
                continue
            if result := await self.search(
                artistname=artist.name,
                albumname=ref_track.album.name,
                trackname=ref_track.name,
                trackversion=ref_track.version,
            ):
                return result[0].id
        return None

    async def search(
        self, artistname: str, albumname: str, trackname: str, trackversion: str | None = None
    ) -> tuple[MusicBrainzArtist, MusicBrainzReleaseGroup, MusicBrainzRecording] | None:
        """
        Search MusicBrainz details by providing the artist, album and track name.

        NOTE: The MusicBrainz objects returned are simplified objects without the optional data.
        """
        trackname, trackversion = parse_title_and_version(trackname, trackversion)
        searchartist = re.sub(LUCENE_SPECIAL, r"\\\1", artistname)
        searchalbum = re.sub(LUCENE_SPECIAL, r"\\\1", albumname)
        searchtracks: list[str] = []
        if trackversion:
            searchtracks.append(f"{trackname} ({trackversion})")
        searchtracks.append(trackname)
        # the version is sometimes appended to the title and sometimes stored
        # in disambiguation, so we try both
        for strict in (True, False):
            for searchtrack in searchtracks:
                searchstr = re.sub(LUCENE_SPECIAL, r"\\\1", searchtrack)
                result = await self.get_data(
                    "recording",
                    query=f'"{searchstr}" AND artist:"{searchartist}" AND release:"{searchalbum}"',
                )
                if not result or "recordings" not in result:
                    continue
                for item in result["recordings"]:
                    # compare track title
                    if not compare_strings(item["title"], searchtrack, strict):
                        continue
                    # compare track version if needed
                    if (
                        trackversion
                        and trackversion not in searchtrack
                        and not compare_strings(item.get("disambiguation"), trackversion, strict)
                    ):
                        continue
                    # match (primary) track artist
                    artist_match: MusicBrainzArtist | None = None
                    for artist in item["artist-credit"]:
                        if compare_strings(artist["artist"]["name"], artistname, strict):
                            artist_match = MusicBrainzArtist.from_dict(
                                replace_hyphens(artist["artist"])
                            )
                        else:
                            for alias in artist["artist"].get("aliases", []):
                                if compare_strings(alias["name"], artistname, strict):
                                    artist_match = MusicBrainzArtist.from_dict(
                                        replace_hyphens(artist["artist"])
                                    )
                    if not artist_match:
                        continue
                    # match album/release
                    album_match: MusicBrainzReleaseGroup | None = None
                    for release in item["releases"]:
                        if compare_strings(release["title"], albumname, strict) or compare_strings(
                            release["release-group"]["title"], albumname, strict
                        ):
                            album_match = MusicBrainzReleaseGroup.from_dict(
                                replace_hyphens(release["release-group"])
                            )
                            break
                    else:
                        continue
                    # if we reach this point, we got a match on recording,
                    # artist and release(group)
                    recording = MusicBrainzRecording.from_dict(replace_hyphens(item))
                    return (artist_match, album_match, recording)

        return None

    async def get_artist_details(self, artist_id: str) -> MusicBrainzArtist:
        """Get (full) Artist details by providing a MusicBrainz artist id."""
        endpoint = (
            f"artist/{artist_id}?inc=aliases+annotation+tags+ratings+genres+url-rels+work-rels"
        )
        if result := await self.get_data(endpoint):
            if "id" not in result:
                result["id"] = artist_id
            # TODO: Parse all the optional data like relations and such
            try:
                return MusicBrainzArtist.from_dict(replace_hyphens(result))
            except MissingField as err:
                raise InvalidDataError from err
        msg = "Invalid MusicBrainz Artist ID provided"
        raise InvalidDataError(msg)

    async def get_recording_details(
        self, recording_id: str | None = None, isrsc: str | None = None
    ) -> MusicBrainzRecording:
        """Get Recording details by providing a MusicBrainz recording id OR isrc."""
        assert recording_id or isrsc, "Provider either Recording ID or ISRC"
        if not recording_id:
            # lookup recording id first by isrc
            if (result := await self.get_data(f"isrc/{isrsc}")) and result.get("recordings"):
                recording_id = result["recordings"][0]["id"]
            else:
                msg = "Invalid ISRC provided"
                raise InvalidDataError(msg)
        if result := await self.get_data(f"recording/{recording_id}?inc=artists+releases"):
            if "id" not in result:
                result["id"] = recording_id
            try:
                return MusicBrainzRecording.from_dict(replace_hyphens(result))
            except MissingField as err:
                raise InvalidDataError from err
        msg = "Invalid ISRC provided"
        raise InvalidDataError(msg)

    async def get_releasegroup_details(
        self, releasegroup_id: str | None = None, barcode: str | None = None
    ) -> MusicBrainzReleaseGroup:
        """Get ReleaseGroup details by providing a MusicBrainz ReleaseGroup id OR barcode."""
        assert releasegroup_id or barcode, "Provider either ReleaseGroup ID or barcode"
        if not releasegroup_id:
            # lookup releasegroup id first by barcode
            endpoint = f"release?query=barcode:{barcode}"
            if (result := await self.get_data(endpoint)) and result.get("releases"):
                releasegroup_id = result["releases"][0]["release-group"]["id"]
            else:
                msg = "Invalid barcode provided"
                raise InvalidDataError(msg)
        endpoint = f"release-group/{releasegroup_id}?inc=artists+aliases"
        if result := await self.get_data(endpoint):
            if "id" not in result:
                result["id"] = releasegroup_id
            try:
                return MusicBrainzReleaseGroup.from_dict(replace_hyphens(result))
            except MissingField as err:
                raise InvalidDataError from err
        msg = "Invalid MusicBrainz ReleaseGroup ID or barcode provided"
        raise InvalidDataError(msg)

    async def get_artist_details_by_album(
        self, artistname: str, ref_album: Album
    ) -> MusicBrainzArtist | None:
        """
        Get musicbrainz artist details by providing the artist name and a reference album.

        MusicBrainzArtist object that is returned does not contain the optional data.
        """
        barcodes = [x[1] for x in ref_album.external_ids if x[0] == ExternalID.BARCODE]
        if not (ref_album.mbid or barcodes):
            return None
        for barcode in barcodes:
            result = None
            with suppress(InvalidDataError):
                result = await self.get_releasegroup_details(ref_album.mbid, barcode)
            if not (result and result.artist_credit):
                return None
            for strict in (True, False):
                for artist_credit in result.artist_credit:
                    if compare_strings(artist_credit.artist.name, artistname, strict):
                        return artist_credit.artist
                    for alias in artist_credit.artist.aliases or []:
                        if compare_strings(alias.name, artistname, strict):
                            return artist_credit.artist
        return None

    async def get_artist_details_by_track(
        self, artistname: str, ref_track: Track
    ) -> MusicBrainzArtist | None:
        """
        Get musicbrainz artist details by providing the artist name and a reference track.

        MusicBrainzArtist object that is returned does not contain the optional data.
        """
        isrcs = [x[1] for x in ref_track.external_ids if x[0] == ExternalID.ISRC]
        if not (ref_track.mbid or isrcs):
            return None
        for isrc in isrcs:
            result = None
            with suppress(InvalidDataError):
                result = await self.get_recording_details(ref_track.mbid, isrc)
            if not (result and result.artist_credit):
                return None
            for strict in (True, False):
                for artist_credit in result.artist_credit:
                    if compare_strings(artist_credit.artist.name, artistname, strict):
                        return artist_credit.artist
                    for alias in artist_credit.artist.aliases or []:
                        if compare_strings(alias.name, artistname, strict):
                            return artist_credit.artist
        return None

    @use_cache(86400 * 30)
    async def get_data(self, endpoint: str, **kwargs: dict[str, Any]) -> Any:
        """Get data from api."""
        url = f"http://musicbrainz.org/ws/2/{endpoint}"
        headers = {
            "User-Agent": f"Music Assistant/{self.mass.version} ( https://github.com/music-assistant )"  # noqa: E501
        }
        kwargs["fmt"] = "json"  # type: ignore[assignment]
        async with (
            self.throttler,
            self.mass.http_session.get(url, headers=headers, params=kwargs, ssl=False) as response,
        ):
            try:
                result = await response.json()
            except (
                aiohttp.client_exceptions.ContentTypeError,
                JSONDecodeError,
            ) as exc:
                msg = await response.text()
                self.logger.warning("%s - %s", str(exc), msg)
                result = None
            return result
