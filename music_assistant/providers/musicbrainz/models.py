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
class MusicBrainzBarcodeRelease(DataClassDictMixin):
    """
    Slim release identity from a barcode search result.

    A barcode search only needs each hit's release and release-group id, so this
    deliberately ignores the summary fields a search response carries (media without a
    tracklist, artist credits, ...) that the full release model cannot parse.
    """

    id: str
    release_group: MusicBrainzReleaseGroup

    @classmethod
    def from_raw(cls, data: Any) -> MusicBrainzBarcodeRelease:
        """Instantiate object from raw api data."""
        alt_data = replace_hyphens(data)
        if TYPE_CHECKING:
            alt_data = cast("dict[str, Any]", alt_data)
        return MusicBrainzBarcodeRelease.from_dict(alt_data)


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
