"""Provider-local models for the consumed fields of ListenBrainz JSPF responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from music_assistant.helpers.external_ids import is_valid_mbid

JSPF_PLAYLIST_EXTENSION = "https://musicbrainz.org/doc/jspf#playlist"
JSPF_TRACK_EXTENSION = "https://musicbrainz.org/doc/jspf#track"


@dataclass(frozen=True, kw_only=True)
class ListenBrainzArtist:
    """An artist credit in a ListenBrainz recording."""

    artist_mbid: str
    name: str

    @classmethod
    def from_jspf(cls, data: dict[str, Any]) -> ListenBrainzArtist:
        """Decode a structured JSPF artist credit."""
        return cls(
            artist_mbid=_required_mbid(data.get("artist_mbid")),
            name=_required_text(data.get("artist_credit_name")),
        )


@dataclass(frozen=True, kw_only=True)
class ListenBrainzTrack:
    """Recording identity and optional display metadata supplied by ListenBrainz."""

    recording_mbid: str
    title: str | None = None
    duration_ms: int | None = None
    album_name: str | None = None
    artists: tuple[ListenBrainzArtist, ...] = ()

    @classmethod
    def from_jspf(cls, data: dict[str, Any]) -> ListenBrainzTrack:
        """Decode a JSPF recording and its artist credits."""
        identifiers = data.get("identifier")
        if not isinstance(identifiers, list) or not identifiers:
            raise ValueError("ListenBrainz track has no recording identifier")
        identifier = _required_text(identifiers[0])
        metadata = _nested_dict(data, "extension", JSPF_TRACK_EXTENSION, "additional_metadata")
        artists = metadata.get("artists")
        if artists is None:
            artists = []
        if not isinstance(artists, list):
            raise TypeError("ListenBrainz artist credits must be a list")
        duration = data.get("duration")
        if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool)):
            raise ValueError("ListenBrainz duration must be an integer or null")
        return cls(
            recording_mbid=_required_mbid(identifier.rsplit("/", 1)[-1]),
            title=_optional_text(data.get("title")),
            duration_ms=duration,
            album_name=_optional_text(data.get("album")),
            artists=tuple(ListenBrainzArtist.from_jspf(artist) for artist in artists),
        )


@dataclass(frozen=True, kw_only=True)
class ListenBrainzPlaylist:
    """Playlist metadata with optional contents, absent in listing responses."""

    playlist_id: str
    title: str
    annotation: str | None = None
    source_patch: str | None = None
    modified_at: str | None = None
    tracks: tuple[ListenBrainzTrack, ...] | None = None

    @classmethod
    def from_jspf(cls, data: dict[str, Any]) -> ListenBrainzPlaylist:
        """Decode a JSPF playlist and flatten its algorithm and version metadata."""
        identifier = _required_text(data.get("identifier"))
        extension = _nested_dict(data, "extension", JSPF_PLAYLIST_EXTENSION)
        algorithm = _nested_dict(extension, "additional_metadata", "algorithm_metadata")
        tracks = data.get("track")
        if tracks is not None and not isinstance(tracks, list):
            raise ValueError("ListenBrainz playlist tracks must be a list or null")
        return cls(
            playlist_id=_required_mbid(identifier.rsplit("/", 1)[-1]),
            title=_required_text(data.get("title")),
            annotation=_optional_text(data.get("annotation")),
            source_patch=_optional_text(algorithm.get("source_patch")),
            modified_at=_optional_text(extension.get("last_modified_at")),
            tracks=(
                tuple(ListenBrainzTrack.from_jspf(track) for track in tracks)
                if tracks is not None
                else None
            ),
        )


def _nested_dict(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Return an optional nested object, or an empty object when absent."""
    for key in keys:
        value = data.get(key)
        if not isinstance(value, dict):
            return {}
        data = value
    return data


def _required_text(value: Any) -> str:
    """Read a nonempty required string without coercing malformed data."""
    if not isinstance(value, str) or not value:
        raise ValueError("ListenBrainz response is missing a required string")
    return value


def _required_mbid(value: Any) -> str:
    """Read a required MusicBrainz identifier."""
    mbid = _required_text(value)
    if not is_valid_mbid(mbid):
        raise ValueError("ListenBrainz response has an invalid MusicBrainz identifier")
    return mbid


def _optional_text(value: Any) -> str | None:
    """Read optional text, accepting omitted and explicit null values."""
    if value is None or isinstance(value, str):
        return value
    raise ValueError("ListenBrainz response has an invalid optional string")
