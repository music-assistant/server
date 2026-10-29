"""Tests for ListenBrainz response parsers."""

from music_assistant_models.enums import ExternalID

from music_assistant.providers.listenbrainz.parsers import parse_artists


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
