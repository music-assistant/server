"""MusicBrainz data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from mashumaro import DataClassDictMixin


def replace_hyphens(
    data: dict[str, Any] | list[dict[str, Any]] | Any,
) -> dict[str, Any] | list[dict[str, Any]] | Any:
    """Change all hyphened keys to underscores."""
    if isinstance(data, dict):
        return {key.replace("-", "_"): replace_hyphens(value) for key, value in data.items()}

    if isinstance(data, list):
        return [replace_hyphens(x) for x in data]

    return data


def release_year(date: str | None) -> int | None:
    """
    Read the year off a MusicBrainz date of any precision.

    :param date: MusicBrainz date, as a year, year-month or full date, if known.
    :return: The year, or None if the date is absent or unparsable.
    """
    return int(year) if date and (year := date[:4]).isdigit() else None


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
class MusicBrainzLifeSpan(DataClassDictMixin):
    """Model for a LifeSpan object from MusicBrainz."""

    begin: str | None = None
    end: str | None = None
    ended: bool = False


@dataclass
class MusicBrainzUrl(DataClassDictMixin):
    """Model for a Url object embedded in a MusicBrainz relation."""

    resource: str


@dataclass
class MusicBrainzRelation(DataClassDictMixin):
    """Model for a Relation object from MusicBrainz."""

    type: str

    # optional - only populated on url-rels (work-rels and friends have other targets)
    url: MusicBrainzUrl | None = None
    ended: bool = False


@dataclass
class MusicBrainzArtist(DataClassDictMixin):
    """Model for a (basic) Artist object from MusicBrainz."""

    id: str
    name: str
    sort_name: str

    # optional fields
    type: str | None = None
    aliases: list[MusicBrainzAlias] | None = None
    tags: list[MusicBrainzTag] | None = None
    genres: list[MusicBrainzTag] | None = None
    relations: list[MusicBrainzRelation] | None = None
    life_span: MusicBrainzLifeSpan | None = None

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
    barcode: str | None = None
    first_release_date: str | None = None
    genres: list[MusicBrainzTag] | None = None

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzReleaseGroup:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzReleaseGroup.from_dict(alt_data)

    @property
    def first_release_year(self) -> int | None:
        """Return the year the release group was first released, if MusicBrainz knows it."""
        return release_year(self.first_release_date)


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
    genres: list[MusicBrainzTag] | None = None
    relations: list[MusicBrainzRelation] | None = None
    disambiguation: str | None = None  # version (e.g. live, karaoke etc.)

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzRecording:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzRecording.from_dict(alt_data)


@dataclass
class MusicBrainzTrack(DataClassDictMixin):
    """Model for a (basic) Track object from MusicBrainz."""

    id: str
    number: str
    title: str
    length: int | None = None
    position: int | None = None
    recording: MusicBrainzRecording | None = None

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

    format: str | None = None
    # a search result lists the tracks under "track", a release lookup under "tracks"
    track: list[MusicBrainzTrack] = field(default_factory=list)
    tracks: list[MusicBrainzTrack] = field(default_factory=list)
    position: int = 0
    track_count: int = 0
    track_offset: int = 0


@dataclass
class MusicBrainzLabel(DataClassDictMixin):
    """Model for a (basic) Label object from MusicBrainz."""

    id: str
    name: str


@dataclass
class MusicBrainzLabelInfo(DataClassDictMixin):
    """Model for a LabelInfo object (label and catalog number) of a MusicBrainz release."""

    label: MusicBrainzLabel | None = None
    catalog_number: str | None = None


@dataclass
class MusicBrainzRelease(DataClassDictMixin):
    """Model for a (basic) Release object from MusicBrainz."""

    id: str
    title: str

    # optional fields
    status: str | None = None
    status_id: str | None = None
    count: int | None = None
    artist_credit: list[MusicBrainzArtistCredit] = field(default_factory=list)
    release_group: MusicBrainzReleaseGroup | None = None
    track_count: int = 0
    media: list[MusicBrainzMedia] = field(default_factory=list)
    date: str | None = None
    country: str | None = None
    disambiguation: str | None = None  # version
    barcode: str | None = None
    asin: str | None = None
    label_info: list[MusicBrainzLabelInfo] | None = None
    relations: list[MusicBrainzRelation] | None = None
    genres: list[MusicBrainzTag] | None = None
    # TODO (if needed): release-events

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzRelease:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzRelease.from_dict(alt_data)


@dataclass
class MusicBrainzBarcodeRelease(DataClassDictMixin):
    """
    Release summary as listed by a barcode search or a release group browse.

    Carries the release and release-group identity plus the edition data (status,
    country, date, media formats and URL relations) a listing includes, but no tracklist.
    """

    id: str
    release_group: MusicBrainzReleaseGroup

    # optional fields
    title: str | None = None
    status: str | None = None
    country: str | None = None
    date: str | None = None
    artist_credit: list[MusicBrainzArtistCredit] | None = None
    media: list[MusicBrainzMedia] = field(default_factory=list)
    relations: list[MusicBrainzRelation] | None = None

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzBarcodeRelease:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzBarcodeRelease.from_dict(alt_data)
