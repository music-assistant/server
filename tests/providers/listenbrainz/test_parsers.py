"""Tests for ListenBrainz response parsers."""

from typing import Any

import pytest
from music_assistant_models.enums import ExternalID

from music_assistant.providers.listenbrainz.parsers import (
    parse_artists,
    parse_playlist,
    parse_playlist_last_modified,
    parse_playlist_source_patch,
    parse_track,
)


def test_parse_playlist_from_jspf_response() -> None:
    """A JSPF playlist response becomes a provider-owned playlist."""
    playlist_mbid = "88888888-8888-4888-8888-888888888888"
    playlist_data = {
        "identifier": f"https://listenbrainz.org/playlist/{playlist_mbid}",
        "title": "Synthetic Playlist",
    }

    playlist = parse_playlist(playlist_data, "listenbrainz--test", "listenbrainz")

    assert playlist.item_id == playlist_mbid
    assert playlist.name == "Synthetic Playlist"
    assert playlist.owner == "listenbrainz"
    assert playlist.is_editable is False
    assert playlist.provider == "listenbrainz--test"
    assert playlist.available is True


def test_parse_playlist_source_patch_from_algorithm_metadata() -> None:
    """The playlist algorithm source is read from its JSPF extension."""
    playlist_data = {
        "extension": {
            "https://musicbrainz.org/doc/jspf#playlist": {
                "additional_metadata": {
                    "algorithm_metadata": {"source_patch": "synthetic-playlist-type"}
                }
            }
        }
    }

    assert parse_playlist_source_patch(playlist_data) == "synthetic-playlist-type"


def test_parse_playlist_source_patch_returns_none_when_metadata_is_missing() -> None:
    """A playlist without algorithm metadata has no supported source patch."""
    assert parse_playlist_source_patch({}) is None


def test_parse_playlist_last_modified_from_extension() -> None:
    """The playlist modification timestamp is read from its JSPF extension."""
    playlist_data = {
        "extension": {
            "https://musicbrainz.org/doc/jspf#playlist": {
                "last_modified_at": "2026-10-20T22:18:49.794546+00:00"
            }
        }
    }

    assert parse_playlist_last_modified(playlist_data) == "2026-10-20T22:18:49.794546+00:00"


def test_parse_playlist_last_modified_returns_none_when_missing() -> None:
    """A playlist without a last_modified_at timestamp returns None."""
    assert parse_playlist_last_modified({}) is None
    assert (
        parse_playlist_last_modified(
            {"extension": {"https://musicbrainz.org/doc/jspf#playlist": {}}}
        )
        is None
    )


def test_parse_artists_from_jspf_track_extension() -> None:
    """Structured JSPF artist credits become MBID-backed artists."""
    track_data = {
        "extension": {
            "https://musicbrainz.org/doc/jspf#track": {
                "additional_metadata": {
                    "artists": [
                        {
                            "artist_credit_name": "Artist One",
                            "artist_mbid": "55555555-5555-4555-8555-555555555555",
                            "join_phrase": " feat. ",
                        },
                        {
                            "artist_credit_name": "Artist Two",
                            "artist_mbid": "66666666-6666-4666-8666-666666666666",
                            "join_phrase": "",
                        },
                    ]
                }
            }
        }
    }

    artists = parse_artists(track_data, "listenbrainz--test", "listenbrainz")

    assert [artist.name for artist in artists] == ["Artist One", "Artist Two"]
    assert artists[0].get_external_id(ExternalID.MB_ARTIST) == (
        "55555555-5555-4555-8555-555555555555"
    )
    assert all(artist.available is False for artist in artists)


@pytest.mark.parametrize("explicit_null", [False, True])
def test_parse_track_without_optional_metadata(explicit_null: bool) -> None:
    """Missing display metadata does not discard a recording's resolvable identity."""
    recording_mbid = "77777777-7777-4777-8777-777777777777"
    track_data: dict[str, Any] = {
        "identifier": [f"https://musicbrainz.org/recording/{recording_mbid}"],
        "extension": {
            "https://musicbrainz.org/doc/jspf#track": {"additional_metadata": {"artists": []}}
        },
    }
    if explicit_null:
        track_data.update(title=None, duration=None, album=None)

    track = parse_track(track_data, "listenbrainz--test", "listenbrainz")

    assert track.name == recording_mbid
    assert track.duration == 0
    assert track.album is None
    assert track.get_external_id(ExternalID.MB_RECORDING) == recording_mbid
    assert track.available is False


def test_parse_track_from_jspf_playlist_entry() -> None:
    """A JSPF playlist entry becomes an unavailable MBID-backed track."""
    recording_mbid = "77777777-7777-4777-8777-777777777777"
    track_data = {
        "album": "Synthetic Album",
        "duration": 185000,
        "extension": {
            "https://musicbrainz.org/doc/jspf#track": {
                "additional_metadata": {
                    "artists": [
                        {
                            "artist_credit_name": "Synthetic Artist",
                            "artist_mbid": "55555555-5555-4555-8555-555555555555",
                            "join_phrase": "",
                        }
                    ]
                }
            }
        },
        "identifier": [f"https://musicbrainz.org/recording/{recording_mbid}"],
        "title": "Synthetic Track",
    }

    track = parse_track(track_data, "listenbrainz--test", "listenbrainz")

    assert track.item_id == recording_mbid
    assert track.name == "Synthetic Track"
    assert track.duration == 185
    assert track.album is not None
    assert track.album.name == "Synthetic Album"
    assert [artist.name for artist in track.artists] == ["Synthetic Artist"]
    assert track.get_external_id(ExternalID.MB_RECORDING) == recording_mbid
    assert track.available is False
