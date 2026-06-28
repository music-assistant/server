"""MusicBrainz metadata provider implementation."""

from __future__ import annotations

import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from mashumaro.exceptions import MissingField
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ExternalID, LinkType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import MediaItemLink, MediaItemMetadata

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.metadata_provider import MetadataProvider

from .api_client import MusicBrainzAPIClient
from .constants import (
    LUCENE_SPECIAL,
    SOCIAL_HOST_MAPPING,
    SUPPORTED_FEATURES,
    URL_RELATION_TYPE_MAPPING,
)
from .models import (
    MusicBrainzArtist,
    MusicBrainzRecording,
    MusicBrainzRelation,
    MusicBrainzRelease,
    MusicBrainzReleaseGroup,
)
from .recommendations import MusicBrainzRecommendationManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import Album, Artist, RecommendationFolder, Track
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Config keys
CONF_RECOMMENDATION_DAYS = "recommendation_days"


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
    return (
        ConfigEntry(
            key=CONF_RECOMMENDATION_DAYS,
            type=ConfigEntryType.INTEGER,
            default_value=3,
            range=(1, 15),
            advanced=True,
        ),
    )


class MusicbrainzProvider(MetadataProvider):
    """The Musicbrainz Metadata provider."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.cache = self.mass.cache
        self._api_client = MusicBrainzAPIClient(self.mass)
        self._recommendations = MusicBrainzRecommendationManager(self)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # Warm the recommendation cache in the background so the discover page is never
        # blocked by the (rate-limited) initial MusicBrainz library scan.
        self._recommendations.schedule_refresh()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        self._recommendations.cancel()

    async def recommendations(self) -> list[RecommendationFolder]:
        """Return birthday/memorial recommendation folders."""
        return await self._recommendations.get_recommendations()

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
                result = await self._api_client.get_data(
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
        if result := await self._api_client.get_data(endpoint):
            if "id" not in result:
                result["id"] = artist_id
            try:
                return MusicBrainzArtist.from_raw(result)
            except MissingField as err:
                raise InvalidDataError from err
        msg = "Invalid MusicBrainz Artist ID provided"
        raise InvalidDataError(msg)

    async def resolve_artists_from_mbids(
        self, mbids: tuple[str, ...]
    ) -> list[tuple[str, str, str] | None]:
        """
        Look up canonical artist names for a sequence of MusicBrainz artist IDs.

        Transient failures (MusicBrainz unreachable, retries exhausted) are left
        to propagate so the caller can retry later rather than persist degraded
        data; only a genuinely unresolvable MBID yields ``None``.

        :param mbids: MusicBrainz artist IDs to look up.
        :return: One entry per input MBID, in the same order, as a
            ``(name, mbid, sort_name)`` tuple. ``None`` at a position means
            that MBID could not be resolved.
        """
        results: list[tuple[str, str, str] | None] = []
        for mbid in mbids:
            try:
                artist = await self.get_artist_details(mbid)
                results.append((artist.name, mbid, artist.sort_name))
            except InvalidDataError as err:
                self.logger.warning("Failed to lookup MusicBrainz artist %s: %s", mbid, err)
                results.append(None)
        return results

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Surface MusicBrainz URL relations (Wikipedia, official site, socials, ...)."""
        if not artist.mbid:
            return None
        try:
            details = await self.get_artist_details(artist.mbid)
        except InvalidDataError:
            return None
        if not details.relations:
            return None
        links: set[MediaItemLink] = set()
        for relation in details.relations:
            if not relation.url:
                continue
            if link_type := self._link_type_for_relation(relation):
                links.add(MediaItemLink(type=link_type, url=relation.url.resource))
        if not links:
            return None
        return MediaItemMetadata(links=links)

    async def get_recording_details(self, recording_id: str) -> MusicBrainzRecording:
        """Get Recording details by providing a MusicBrainz Recording Id."""
        if result := await self._api_client.get_data(
            f"recording/{recording_id}?inc=artists+releases+isrcs"
        ):
            if "id" not in result:
                result["id"] = recording_id
            try:
                return MusicBrainzRecording.from_raw(result)
            except MissingField as err:
                raise InvalidDataError from err
        msg = "Invalid MusicBrainz recording ID provided"
        raise InvalidDataError(msg)

    @use_cache(86400 * 30)
    async def get_isrcs_for_recording(self, recording_id: str) -> list[str]:
        """
        Get ISRCs for a MusicBrainz Recording ID.

        :param recording_id: MusicBrainz recording ID, or a track ID as
            handed out by e.g. Last.fm.
        :return: List of ISRCs, or empty list if not found.
        """
        # the search response includes the ISRCs, so either ID kind costs one call
        safe_id = re.sub(LUCENE_SPECIAL, r"\\\1", recording_id)
        query = f"rid:{safe_id} OR tid:{safe_id}"
        if (result := await self._api_client.get_data("recording", query=query)) and (
            recordings := result.get("recordings")
        ):
            return recordings[0].get("isrcs") or []
        # merged (redirected) recording MBIDs are absent from the search
        # index but still resolve via direct lookup
        with suppress(InvalidDataError):
            recording = await self.get_recording_details(recording_id)
            return recording.isrcs or []
        return []

    async def get_release_details(self, album_id: str) -> MusicBrainzRelease:
        """Get Release/Album details by providing a MusicBrainz Album id."""
        endpoint = f"release/{album_id}?inc=artist-credits+aliases+labels"
        if result := await self._api_client.get_data(endpoint):
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
        if result := await self._api_client.get_data(endpoint):
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
        if result := await self._api_client.get_data(
            "url", resource=resource_url, inc="artist-rels"
        ):
            for relation in result.get("relations", []):
                if not (artist := relation.get("artist")):
                    continue
                return MusicBrainzArtist.from_raw(artist)
        return None

    async def get_release_group_by_track_name(
        self, artist_name: str, track_name: str
    ) -> tuple[MusicBrainzArtist, list[MusicBrainzReleaseGroup]] | None:
        """
        Find release groups for a track by searching MusicBrainz recordings.

        Returns matching release groups sorted by release date,
        prioritizing the earliest original recording to find the correct releases.

        :param artist_name: Artist name to search for.
        :param track_name: Track name to search for.
        :returns: Tuple of (artist, release_groups) or None.
        """
        search_artist = re.sub(LUCENE_SPECIAL, r"\\\1", artist_name)
        search_track = re.sub(LUCENE_SPECIAL, r"\\\1", track_name)
        result = await self._api_client.get_data(
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
                    existing_rg, existing_date = all_release_groups[rg_id]
                    if release_date and (not existing_date or release_date < existing_date):
                        if not rg.barcode:
                            rg.barcode = existing_rg.barcode
                        all_release_groups[rg_id] = (rg, release_date)
                    elif rg.barcode and not existing_rg.barcode:
                        existing_rg.barcode = rg.barcode
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

    @staticmethod
    def _link_type_for_relation(relation: MusicBrainzRelation) -> LinkType | None:
        if link_type := URL_RELATION_TYPE_MAPPING.get(relation.type):
            return link_type
        if relation.type == "social network" and relation.url:
            url_lower = relation.url.resource.lower()
            for host, link_type in SOCIAL_HOST_MAPPING:
                if host in url_lower:
                    return link_type
        return None

    def _get_release_groups_with_dates(
        self, recording: dict[str, Any], track_name: str
    ) -> list[tuple[MusicBrainzReleaseGroup, str]]:
        """
        Collect release groups for a recording with their release dates.

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
            # Skip bootleg and pseudo-releases
            release_status = release.get("status", "")
            if release_status in ("Bootleg", "Pseudo-Release"):
                continue

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
            barcode = release.get("barcode") or None

            # Keep the earliest release date per release group
            if rg_id in seen:
                existing_rg, existing_date = seen[rg_id]
                if release_date and (not existing_date or release_date < existing_date):
                    mb_rg = MusicBrainzReleaseGroup.from_raw(rg)
                    mb_rg.barcode = barcode or existing_rg.barcode
                    seen[rg_id] = (mb_rg, release_date)
                elif barcode and not existing_rg.barcode:
                    existing_rg.barcode = barcode
            else:
                mb_rg = MusicBrainzReleaseGroup.from_raw(rg)
                mb_rg.barcode = barcode
                seen[rg_id] = (mb_rg, release_date)

        return list(seen.values())
