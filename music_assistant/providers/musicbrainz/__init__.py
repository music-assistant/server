"""The Musicbrainz Metadata provider for Music Assistant.

At this time only used for retrieval of ID's but to be expanded to fetch metadata too.
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from mashumaro import DataClassDictMixin
from mashumaro.exceptions import MissingField
from music_assistant_models.enums import ExternalID, ProviderFeature
from music_assistant_models.errors import InvalidDataError, ResourceTemporarilyUnavailable

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import Album, Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'

SUPPORTED_FEATURES: set[ProviderFeature] = (
    set()
)  # we don't have any special supported features (yet)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MusicbrainzProvider(mass, manifest, config, SUPPORTED_FEATURES)


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


def replace_hyphens(
    data: dict[str, Any] | list[dict[str, Any]] | Any,
) -> dict[str, Any] | list[dict[str, Any]] | Any:
    """Change all hyphened keys to underscores."""
    if isinstance(data, dict):
        return {key.replace("-", "_"): replace_hyphens(value) for key, value in data.items()}

    if isinstance(data, list):
        return [replace_hyphens(x) for x in data]

    return data


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

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzArtist:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzArtist.from_dict(alt_data)


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

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzReleaseGroup:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzReleaseGroup.from_dict(alt_data)


@dataclass
class MusicBrainzTrack(DataClassDictMixin):
    """Model for a (basic) Track object from MusicBrainz."""

    id: str
    number: str
    title: str
    length: int | None = None

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzTrack:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzTrack.from_dict(alt_data)


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

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzRelease:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzRelease.from_dict(alt_data)


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

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzRecording:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzRecording.from_dict(alt_data)


class MusicbrainzProvider(MetadataProvider):
    """The Musicbrainz Metadata provider."""

    throttler = ThrottlerManager(rate_limit=5, period=1)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache

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
                            artist_match = MusicBrainzArtist.from_raw(artist["artist"])
                        else:
                            for alias in artist["artist"].get("aliases", []):
                                if compare_strings(alias["name"], artistname, strict):
                                    artist_match = MusicBrainzArtist.from_raw(artist["artist"])
                    if not artist_match:
                        continue
                    # match album/release
                    album_match: MusicBrainzReleaseGroup | None = None
                    for release in item["releases"]:
                        if compare_strings(release["title"], albumname, strict) or compare_strings(
                            release["release-group"]["title"], albumname, strict
                        ):
                            album_match = MusicBrainzReleaseGroup.from_raw(release["release-group"])
                            break
                    else:
                        continue
                    # if we reach this point, we got a match on recording,
                    # artist and release(group)
                    recording = MusicBrainzRecording.from_raw(item)
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
                return MusicBrainzArtist.from_raw(result)
            except MissingField as err:
                raise InvalidDataError from err
        msg = "Invalid MusicBrainz Artist ID provided"
        raise InvalidDataError(msg)

    async def get_recording_details(self, recording_id: str) -> MusicBrainzRecording:
        """Get Recording details by providing a MusicBrainz Recording Id."""
        if result := await self.get_data(f"recording/{recording_id}?inc=artists+releases+isrcs"):
            if "id" not in result:
                result["id"] = recording_id
            try:
                return MusicBrainzRecording.from_raw(result)
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
                return MusicBrainzRelease.from_raw(result)
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
                return MusicBrainzReleaseGroup.from_raw(result)
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
        result: MusicBrainzRelease | MusicBrainzReleaseGroup | None = None
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
                return MusicBrainzArtist.from_raw(artist)
        return None

    async def get_release_group_by_track_name(
        self, artist_name: str, track_name: str
    ) -> tuple[MusicBrainzArtist, list[MusicBrainzReleaseGroup]] | None:
        """Find release groups for a track by searching MusicBrainz recordings.

        Returns matching release groups sorted by release date,
        prioritizing the earliest original recording to find the correct releases.

        :param artist_name: Artist name to search for.
        :param track_name: Track name to search for.
        :returns: Tuple of (artist, release_groups) or None.
        """
        search_artist = re.sub(LUCENE_SPECIAL, r"\\\1", artist_name)
        search_track = re.sub(LUCENE_SPECIAL, r"\\\1", track_name)
        result = await self.get_data(
            "recording",
            query=f'"{search_track}" AND artist:"{search_artist}"',
            limit="100",
        )
        if not result or "recordings" not in result:
            return None

        # Collect all matching recordings with their artist and first-release-date
        matches: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for strict in (True, False):
            for item in result["recordings"]:
                if not compare_strings(item["title"], track_name, strict):
                    continue
                for artist_credit in item.get("artist-credit", []):
                    artist = artist_credit.get("artist", {})
                    artist_matches = compare_strings(artist.get("name", ""), artist_name, strict)
                    if not artist_matches:
                        for alias in artist.get("aliases", []):
                            if compare_strings(alias.get("name", ""), artist_name, strict):
                                artist_matches = True
                                break
                    if artist_matches:
                        first_release = item.get("first-release-date", "") or ""
                        matches.append((item, artist, first_release))
                        break
            if matches:
                break

        if not matches:
            return None

        # Sort by first-release-date to find the earliest (likely original studio recording)
        matches.sort(key=lambda x: x[2] if x[2] else "9999")

        # Aggregate release groups from ALL matching recordings
        # This ensures we find albums even if the first recording only has singles
        all_release_groups: dict[str, tuple[MusicBrainzReleaseGroup, str]] = {}
        first_artist = None
        for recording, artist, first_release_date in matches:
            if first_artist is None:
                first_artist = artist
            for rg, release_date in self._get_release_groups_with_dates(recording, track_name):
                rg_id = rg.id
                if rg_id in all_release_groups:
                    _, existing_date = all_release_groups[rg_id]
                    if release_date and (not existing_date or release_date < existing_date):
                        all_release_groups[rg_id] = (rg, release_date)
                else:
                    all_release_groups[rg_id] = (rg, release_date)

        if all_release_groups:
            # Sort by release date
            sorted_groups = sorted(
                all_release_groups.values(), key=lambda x: x[1] if x[1] else "9999"
            )
            return (MusicBrainzArtist.from_raw(first_artist), [rg for rg, _ in sorted_groups])

        # Fall back to the earliest recording (for artist lookup at least)
        recording, artist, _ = matches[0]
        return (MusicBrainzArtist.from_raw(artist), [])

    def _get_release_groups_with_dates(
        self, recording: dict[str, Any], track_name: str
    ) -> list[tuple[MusicBrainzReleaseGroup, str]]:
        """Collect release groups for a recording with their release dates.

        Filters out compilations and other secondary-type releases.
        For singles, only includes those where the title matches the track name.
        Returns list of (release_group, release_date) tuples for singles and studio albums.

        :param recording: MusicBrainz recording dict.
        :param track_name: Track name to match against single titles.
        """
        releases = recording.get("releases", [])
        if not releases:
            return []

        # Collect release groups with their earliest release date, deduplicating by ID
        seen: dict[str, tuple[MusicBrainzReleaseGroup, str]] = {}

        for release in releases:
            rg = release.get("release-group", {})
            rg_id = rg.get("id")
            if not rg_id:
                continue

            primary_type = rg.get("primary-type")
            secondary_types = rg.get("secondary-types", [])

            # Only include singles and studio albums (no compilations, live, etc.)
            if primary_type not in ("Album", "Single"):
                continue
            if secondary_types:
                continue

            # For singles, only include if the title matches the track name
            # (avoid B-sides and bonus tracks on unrelated singles)
            if primary_type == "Single":
                if not compare_strings(rg.get("title", ""), track_name, strict=False):
                    continue

            release_date = release.get("date", "") or ""

            # Keep the earliest release date per release group
            if rg_id in seen:
                _, existing_date = seen[rg_id]
                if release_date and (not existing_date or release_date < existing_date):
                    seen[rg_id] = (MusicBrainzReleaseGroup.from_raw(rg), release_date)
            else:
                seen[rg_id] = (MusicBrainzReleaseGroup.from_raw(rg), release_date)

        return list(seen.values())

    @use_cache(86400 * 30)  # Cache for 30 days
    @throttle_with_retries
    async def get_data(self, endpoint: str, **kwargs: str) -> Any:
        """Get data from api."""
        url = f"https://musicbrainz-mirror.music-assistant.io/ws/2/{endpoint}"
        headers = {
            "User-Agent": f"Music Assistant/{self.mass.version} (https://music-assistant.io)"
        }
        kwargs["fmt"] = "json"
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
