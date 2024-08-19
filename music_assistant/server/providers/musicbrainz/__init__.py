"""The Musicbrainz Metadata provider for Music Assistant.

At this time only used for retrieval of ID's but to be expanded to fetch metadata too.
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mashumaro import DataClassDictMixin
from mashumaro.exceptions import MissingField

from music_assistant.common.helpers.json import json_loads
from music_assistant.common.helpers.util import parse_title_and_version
from music_assistant.common.models.enums import ExternalID, ProviderFeature
from music_assistant.common.models.errors import InvalidDataError, ResourceTemporarilyUnavailable
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.server.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant.common.models.media_items import Album, Track
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'

SUPPORTED_FEATURES = ()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MusicbrainzProvider(mass, manifest, config)


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
    title: str

    # optional fields
    primary_type: str | None = None
    primary_type_id: str | None = None
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

    throttler = ThrottlerManager(rate_limit=1, period=30)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

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

    async def get_recording_details(self, recording_id: str) -> MusicBrainzRecording:
        """Get Recording details by providing a MusicBrainz Recording Id."""
        if result := await self.get_data(f"recording/{recording_id}?inc=artists+releases"):
            if "id" not in result:
                result["id"] = recording_id
            try:
                return MusicBrainzRecording.from_dict(replace_hyphens(result))
            except MissingField as err:
                raise InvalidDataError from err
        msg = "Invalid MusicBrainz recording ID provided"
        raise InvalidDataError(msg)

    async def get_release_details(self, album_id: str) -> MusicBrainzRelease:
        """Get Release/Album details by providing a MusicBrainz Album id."""
        endpoint = f"release/{album_id}?inc=artist-credits+aliases+labels"
        if result := await self.get_data(endpoint):
            if "id" not in result:
                result["id"] = album_id
            try:
                return MusicBrainzRelease.from_dict(replace_hyphens(result))
            except MissingField as err:
                raise InvalidDataError from err
        msg = "Invalid MusicBrainz Album ID provided"
        raise InvalidDataError(msg)

    async def get_releasegroup_details(self, releasegroup_id: str) -> MusicBrainzReleaseGroup:
        """Get ReleaseGroup details by providing a MusicBrainz ReleaseGroup id."""
        endpoint = f"release-group/{releasegroup_id}?inc=artists+aliases"
        if result := await self.get_data(endpoint):
            if "id" not in result:
                result["id"] = releasegroup_id
            try:
                return MusicBrainzReleaseGroup.from_dict(replace_hyphens(result))
            except MissingField as err:
                raise InvalidDataError from err
        msg = "Invalid MusicBrainz ReleaseGroup ID provided"
        raise InvalidDataError(msg)

    async def get_artist_details_by_album(
        self, artistname: str, ref_album: Album
    ) -> MusicBrainzArtist | None:
        """
        Get musicbrainz artist details by providing the artist name and a reference album.

        MusicBrainzArtist object that is returned does not contain the optional data.
        """
        result = None
        if mb_id := ref_album.get_external_id(ExternalID.MB_RELEASEGROUP):
            with suppress(InvalidDataError):
                result = await self.get_releasegroup_details(mb_id)
        elif mb_id := ref_album.get_external_id(ExternalID.MB_ALBUM):
            with suppress(InvalidDataError):
                result = await self.get_release_details(mb_id)
        else:
            return None
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
        if not ref_track.mbid:
            return None
        result = None
        with suppress(InvalidDataError):
            result = await self.get_recording_details(ref_track.mbid)
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

    async def get_artist_details_by_resource_url(
        self, resource_url: str
    ) -> MusicBrainzArtist | None:
        """
        Get musicbrainz artist details by providing a resource URL (e.g. Spotify share URL).

        MusicBrainzArtist object that is returned does not contain the optional data.
        """
        if result := await self.get_data("url", resource=resource_url, inc="artist-rels"):
            for relation in result.get("relations", []):
                if not (artist := relation.get("artist")):
                    continue
                return MusicBrainzArtist.from_dict(replace_hyphens(artist))
        return None

    @use_cache(86400 * 30)
    @throttle_with_retries
    async def get_data(self, endpoint: str, **kwargs: dict[str, Any]) -> Any:
        """Get data from api."""
        url = f"http://musicbrainz.org/ws/2/{endpoint}"
        headers = {
            "User-Agent": f"Music Assistant/{self.mass.version} (https://music-assistant.io)"
        }
        kwargs["fmt"] = "json"  # type: ignore[assignment]
        async with (
            self.mass.http_session.get(url, headers=headers, params=kwargs) as response,
        ):
            # handle rate limiter
            if response.status == 429:
                backoff_time = int(response.headers.get("Retry-After", 0))
                raise ResourceTemporarilyUnavailable("Rate Limiter", backoff_time=backoff_time)
            # handle temporary server error
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            # handle 404 not found
            if response.status in (400, 401, 404):
                return None
            response.raise_for_status()
            return await response.json(loads=json_loads)
