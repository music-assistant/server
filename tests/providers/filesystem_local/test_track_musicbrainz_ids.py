"""Tests for the MusicBrainz ids the Local files provider stores for a track."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from music_assistant_models.enums import ExternalID

from music_assistant.helpers.tags import AudioTags
from tests.providers.filesystem_local.conftest import make_provider

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant

RECORDING_MBID = "11111111-1111-1111-1111-111111111111"
RELEASE_TRACK_MBID = "22222222-2222-2222-2222-222222222222"
TRACK_FILE = "01 - Come Together.flac"


def _make_tags(**musicbrainz_tags: str) -> AudioTags:
    """
    Build the tags of a file outside an album folder.

    :param musicbrainz_tags: The parsed MusicBrainz tags of the file, by normalized tag name.
    """
    return AudioTags(
        raw={},
        sample_rate=44100,
        channels=2,
        bits_per_sample=16,
        format="flac",
        bit_rate=1000,
        duration=259.0,
        tags={"title": "Come Together", "artist": "The Beatles", "track": "1"} | musicbrainz_tags,
        has_cover_image=False,
        filename=TRACK_FILE,
    )


async def test_a_track_stores_its_release_track_id(
    mass_minimal: MusicAssistant, tmp_path: Path
) -> None:
    """The release-track id is stored next to the recording id, which a remaster shares."""
    (tmp_path / TRACK_FILE).write_bytes(b"\x00" * 128)
    provider = make_provider(mass_minimal, tmp_path)
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[])  # type: ignore[method-assign]
    file_item = await provider.resolve(TRACK_FILE)

    track = await provider._parse_track(
        file_item,
        _make_tags(
            musicbrainzrecordingid=RECORDING_MBID,
            musicbrainzreleasetrackid=RELEASE_TRACK_MBID.upper(),
        ),
    )

    assert track.mbid == RECORDING_MBID
    assert (ExternalID.MB_TRACK, RELEASE_TRACK_MBID) in track.external_ids


async def test_a_track_without_a_release_track_tag_stores_none(
    mass_minimal: MusicAssistant, tmp_path: Path
) -> None:
    """A recording id alone never ends up as the release-track id."""
    (tmp_path / TRACK_FILE).write_bytes(b"\x00" * 128)
    provider = make_provider(mass_minimal, tmp_path)
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[])  # type: ignore[method-assign]
    file_item = await provider.resolve(TRACK_FILE)

    track = await provider._parse_track(
        file_item,
        _make_tags(musicbrainzrecordingid=RECORDING_MBID, musicbrainztrackid=RECORDING_MBID),
    )

    assert track.mbid == RECORDING_MBID
    assert not any(id_type == ExternalID.MB_TRACK for id_type, _ in track.external_ids)
