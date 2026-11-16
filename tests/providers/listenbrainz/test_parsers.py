"""Tests for converting ListenBrainz response models to Music Assistant items."""

import pytest
from music_assistant_models.enums import ExternalID

from music_assistant.constants import EXTERNAL_ID_PROVIDER
from music_assistant.helpers.external_ids import external_item_id
from music_assistant.providers.listenbrainz.models import (
    ListenBrainzArtist,
    ListenBrainzPlaylist,
    ListenBrainzTrack,
)
from music_assistant.providers.listenbrainz.parsers import (
    parse_artists,
    parse_playlist,
    parse_track,
)

PLAYLIST_MBID = "88888888-8888-4888-8888-888888888888"
RECORDING_MBID = "77777777-7777-4777-8777-777777777777"
ARTIST_MBID = "55555555-5555-4555-8555-555555555555"


def test_parse_playlist() -> None:
    """A response model becomes a provider-owned playlist with a sanitized description."""
    data = ListenBrainzPlaylist(
        playlist_id=PLAYLIST_MBID,
        title="Synthetic Playlist",
        annotation="<p>Synthetic &amp; descriptive</p>\n text",
    )

    playlist = parse_playlist(data, "listenbrainz--test", "listenbrainz")

    assert playlist.item_id == PLAYLIST_MBID
    assert playlist.name == "Synthetic Playlist"
    assert playlist.owner == "listenbrainz"
    assert playlist.is_editable is False
    assert playlist.provider == "listenbrainz--test"
    assert playlist.metadata.description == "Synthetic & descriptive text"
    assert playlist.available is True


def test_parse_artists() -> None:
    """Artist credits retain names, IDs and credit order without becoming provider tracks."""
    artist_credits = (
        ListenBrainzArtist(artist_mbid=ARTIST_MBID, name="Synthetic Artist One"),
        ListenBrainzArtist(
            artist_mbid="66666666-6666-4666-8666-666666666666", name="Synthetic Artist Two"
        ),
    )

    artists = parse_artists(artist_credits, "listenbrainz--test", "listenbrainz")

    assert [artist.name for artist in artists] == ["Synthetic Artist One", "Synthetic Artist Two"]
    assert artists[0].get_external_id(ExternalID.MB_ARTIST) == ARTIST_MBID
    assert all(artist.available is False for artist in artists)


@pytest.mark.parametrize("populated", [False, True])
def test_parse_track(populated: bool) -> None:
    """Source metadata becomes an external track; absent display fields have safe defaults."""
    data = ListenBrainzTrack(
        recording_mbid=RECORDING_MBID,
        title="Synthetic Track" if populated else None,
        duration_ms=185000 if populated else None,
        album_name="Synthetic Album" if populated else None,
        artists=(ListenBrainzArtist(artist_mbid=ARTIST_MBID, name="Synthetic Artist"),),
    )

    track = parse_track(data, "listenbrainz--test", "listenbrainz")

    assert track.item_id == external_item_id(ExternalID.MB_RECORDING, RECORDING_MBID)
    assert track.provider == EXTERNAL_ID_PROVIDER
    assert track.provider_mappings == set()
    assert track.name == ("Synthetic Track" if populated else RECORDING_MBID)
    assert track.duration == (185 if populated else 0)
    if populated:
        assert track.album is not None
        assert track.album.name == "Synthetic Album"
    else:
        assert track.album is None
    assert [artist.name for artist in track.artists] == ["Synthetic Artist"]
    assert track.get_external_id(ExternalID.MB_RECORDING) == RECORDING_MBID
    assert track.is_playable is True
