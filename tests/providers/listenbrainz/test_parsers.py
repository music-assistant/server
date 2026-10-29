"""Tests for ListenBrainz response parsers."""

from music_assistant_models.enums import ExternalID

from music_assistant.providers.listenbrainz.parsers import (
    parse_artists,
    parse_playlist,
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
