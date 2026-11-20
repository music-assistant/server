"""Tests for decoding ListenBrainz JSPF response models."""

from typing import Any

import pytest

from music_assistant.providers.listenbrainz.models import (
    ListenBrainzArtist,
    ListenBrainzPlaylist,
    ListenBrainzTrack,
)

PLAYLIST_MBID = "11111111-1111-4111-8111-111111111111"
RECORDING_MBID = "22222222-2222-4222-8222-222222222222"
ARTIST_MBID = "33333333-3333-4333-8333-333333333333"


def test_playlist_decodes_consumed_jspf_fields() -> None:
    """A response exposes playlist metadata and ordered track/artist data as attributes."""
    track = {
        "identifier": [f"https://musicbrainz.org/recording/{RECORDING_MBID}"],
        "title": "Synthetic Track",
        "album": "Synthetic Album",
        "duration": 123000,
        "extension": {
            "https://musicbrainz.org/doc/jspf#track": {
                "additional_metadata": {
                    "artists": [
                        {"artist_mbid": ARTIST_MBID, "artist_credit_name": "Synthetic Artist"}
                    ]
                }
            }
        },
    }
    response = {
        "identifier": f"https://listenbrainz.org/playlist/{PLAYLIST_MBID}",
        "title": "Synthetic Playlist",
        "annotation": "<p>Synthetic description</p>",
        "extension": {
            "https://musicbrainz.org/doc/jspf#playlist": {
                "last_modified_at": "2026-01-01T00:00:00+00:00",
                "additional_metadata": {"algorithm_metadata": {"source_patch": "weekly-jams"}},
            }
        },
        "track": [track, track],
    }

    playlist = ListenBrainzPlaylist.from_jspf(response)

    assert playlist.playlist_id == PLAYLIST_MBID
    assert playlist.title == "Synthetic Playlist"
    assert playlist.annotation == "<p>Synthetic description</p>"
    assert playlist.source_patch == "weekly-jams"
    assert playlist.modified_at == "2026-01-01T00:00:00+00:00"
    assert playlist.tracks is not None
    assert len(playlist.tracks) == 2
    assert [track.recording_mbid for track in playlist.tracks] == [RECORDING_MBID, RECORDING_MBID]
    recording = playlist.tracks[0]
    assert recording.title == "Synthetic Track"
    assert recording.duration_ms == 123000
    assert recording.album_name == "Synthetic Album"
    assert recording.artists[0].artist_mbid == ARTIST_MBID
    assert recording.artists[0].name == "Synthetic Artist"


@pytest.mark.parametrize("explicit_null", [False, True])
def test_optional_track_metadata_accepts_missing_and_null(explicit_null: bool) -> None:
    """Missing metadata never discards a recording with a valid identity."""
    data: dict[str, Any] = {"identifier": [f"https://musicbrainz.org/recording/{RECORDING_MBID}"]}
    if explicit_null:
        data.update(title=None, duration=None, album=None, extension=None)

    assert ListenBrainzTrack.from_jspf(data) == ListenBrainzTrack(recording_mbid=RECORDING_MBID)


@pytest.mark.parametrize("contents", [None, []])
def test_listing_and_fetched_empty_playlist_remain_distinct(contents: list[Any] | None) -> None:
    """Omitted contents are not mistaken for a fetched empty playlist."""
    data: dict[str, Any] = {
        "identifier": f"https://listenbrainz.org/playlist/{PLAYLIST_MBID}",
        "title": "Synthetic Playlist",
    }
    if contents is not None:
        data["track"] = contents

    playlist = ListenBrainzPlaylist.from_jspf(data)

    assert playlist.tracks == (() if contents is not None else None)
    assert playlist.source_patch is None
    assert playlist.modified_at is None
    assert playlist.annotation is None


@pytest.mark.parametrize("model", [ListenBrainzPlaylist, ListenBrainzTrack, ListenBrainzArtist])
def test_missing_required_identity_is_rejected(
    model: type[ListenBrainzPlaylist | ListenBrainzTrack | ListenBrainzArtist],
) -> None:
    """Models reject responses without the identity needed to address their items."""
    with pytest.raises(ValueError, match=r"ListenBrainz.*(required string|recording identifier)"):
        model.from_jspf({})


@pytest.mark.parametrize("identifier", ["", "not-a-recording-mbid"])
def test_invalid_recording_identity_is_rejected(identifier: str) -> None:
    """An invalid source identity must not become an unresolvable external URI."""
    with pytest.raises(
        ValueError, match=r"ListenBrainz.*(required string|invalid MusicBrainz identifier)"
    ):
        ListenBrainzTrack.from_jspf({"identifier": [identifier]})
