"""Tests for player queue sort functionality."""

from typing import cast

from music_assistant_models.media_items import Album, Artist, Track
from music_assistant_models.media_items.provider_mapping import ProviderMapping
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import PlaylistPlayableItem
from music_assistant.controllers.player_queues.helpers import sort_tracks


def _make_provider_mappings(provider: str = "test") -> set[ProviderMapping]:
    return {ProviderMapping(item_id="x", provider_domain=provider, provider_instance=provider)}


def _make_artist(name: str) -> Artist:
    return Artist(
        item_id=name.lower(),
        provider="test",
        name=name,
        provider_mappings=_make_provider_mappings(),
    )


def _make_album(name: str) -> Album:
    return Album(
        item_id=name.lower(),
        provider="test",
        name=name,
        provider_mappings=_make_provider_mappings(),
    )


def _make_track(
    name: str,
    *,
    duration: int = 200,
    position: int | None = None,
    artist_name: str = "Artist",
    album_name: str = "Album",
    track_number: int = 1,
    disc_number: int = 1,
    item_id: str | None = None,
) -> Track:
    artist = _make_artist(artist_name)
    album = _make_album(album_name)
    return Track(
        item_id=item_id or name.lower().replace(" ", "_"),
        provider="test",
        name=name,
        duration=duration,
        position=position,
        artists=UniqueList([artist]),
        album=album,
        track_number=track_number,
        disc_number=disc_number,
        provider_mappings=_make_provider_mappings(),
    )


# --- _sort_tracks tests ---


class TestSortTracks:
    """Tests for the _sort_tracks static method."""

    def test_sort_by_name(self) -> None:
        """Tracks are sorted alphabetically by sort_name."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("Charlie"),
            _make_track("Alpha"),
            _make_track("Bravo"),
        ]
        result = sort_tracks(tracks, "name")
        assert [t.name for t in result] == ["Alpha", "Bravo", "Charlie"]

    def test_sort_by_name_ignores_articles(self) -> None:
        """Sort by name moves leading articles to the end (via sort_name)."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("The Zebra"),
            _make_track("Alpha"),
        ]
        # sort_name for "The Zebra" becomes "zebra, the" so it sorts after "alpha"
        result = sort_tracks(tracks, "name")
        assert [t.name for t in result] == ["Alpha", "The Zebra"]

    def test_sort_by_artist(self) -> None:
        """Tracks are sorted by the first artist's name."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("Song1", artist_name="Zeppelin"),
            _make_track("Song2", artist_name="ABBA"),
            _make_track("Song3", artist_name="Metallica"),
        ]
        result = sort_tracks(tracks, "artist")
        assert [cast("Track", t).artists[0].name for t in result] == [
            "ABBA",
            "Metallica",
            "Zeppelin",
        ]

    def test_sort_by_album(self) -> None:
        """Tracks are sorted by album name."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("Song1", album_name="Thriller"),
            _make_track("Song2", album_name="Abbey Road"),
            _make_track("Song3", album_name="Nevermind"),
        ]
        result = sort_tracks(tracks, "album")
        albums = [cast("Track", t).album for t in result]
        assert all(a is not None for a in albums)
        assert [a.name for a in albums if a is not None] == [
            "Abbey Road",
            "Nevermind",
            "Thriller",
        ]

    def test_sort_by_duration_ascending(self) -> None:
        """Tracks are sorted by duration, shortest first."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("Long", duration=500),
            _make_track("Short", duration=100),
            _make_track("Medium", duration=300),
        ]
        result = sort_tracks(tracks, "duration")
        assert [t.duration for t in result] == [100, 300, 500]

    def test_sort_by_duration_descending(self) -> None:
        """Tracks are sorted by duration, longest first."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("Long", duration=500),
            _make_track("Short", duration=100),
            _make_track("Medium", duration=300),
        ]
        result = sort_tracks(tracks, "duration_desc")
        assert [t.duration for t in result] == [500, 300, 100]

    def test_sort_by_position_desc(self) -> None:
        """Tracks are sorted by position in reverse."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("First", position=1),
            _make_track("Third", position=3),
            _make_track("Second", position=2),
        ]
        result = sort_tracks(tracks, "position_desc")
        assert [t.position for t in result] == [3, 2, 1]

    def test_sort_by_track_number(self) -> None:
        """Tracks are sorted by disc then track number."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("D2T1", disc_number=2, track_number=1),
            _make_track("D1T3", disc_number=1, track_number=3),
            _make_track("D1T1", disc_number=1, track_number=1),
        ]
        result = sort_tracks(tracks, "track_number")
        assert [t.name for t in result] == ["D1T1", "D1T3", "D2T1"]

    def test_unknown_sort_key_returns_original_order(self) -> None:
        """An unrecognized sort key returns the tracks unchanged."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("C"),
            _make_track("A"),
            _make_track("B"),
        ]
        result = sort_tracks(tracks, "nonexistent")
        assert [t.name for t in result] == ["C", "A", "B"]

    def test_sort_is_stable(self) -> None:
        """Tracks with equal sort keys preserve their relative order."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("Song1", duration=200),
            _make_track("Song2", duration=200),
            _make_track("Song3", duration=200),
        ]
        result = sort_tracks(tracks, "duration")
        assert [t.name for t in result] == ["Song1", "Song2", "Song3"]

    def test_sort_with_missing_album(self) -> None:
        """Tracks without an album sort to the beginning (empty string)."""
        track_no_album = _make_track("NoAlbum", album_name="Zoo")
        track_no_album.album = None
        track_with_album = _make_track("WithAlbum", album_name="Abc")
        tracks: list[PlaylistPlayableItem] = [track_with_album, track_no_album]
        result = sort_tracks(tracks, "album")
        # empty string sorts before "abc"
        assert [t.name for t in result] == ["NoAlbum", "WithAlbum"]

    def test_sort_with_empty_artists(self) -> None:
        """Tracks without artists sort to the beginning (empty string)."""
        track_no_artist = _make_track("NoArtist")
        track_no_artist.artists = UniqueList()
        track_with_artist = _make_track("WithArtist", artist_name="Beta")
        tracks: list[PlaylistPlayableItem] = [track_with_artist, track_no_artist]
        result = sort_tracks(tracks, "artist")
        assert [t.name for t in result] == ["NoArtist", "WithArtist"]

    def test_sort_does_not_mutate_input(self) -> None:
        """Sorting returns a new list without modifying the original."""
        tracks: list[PlaylistPlayableItem] = [
            _make_track("Charlie"),
            _make_track("Alpha"),
        ]
        original_order = [t.name for t in tracks]
        sort_tracks(tracks, "name")
        assert [t.name for t in tracks] == original_order

    def test_empty_list(self) -> None:
        """Sorting an empty list returns an empty list."""
        tracks: list[PlaylistPlayableItem] = []
        result = sort_tracks(tracks, "name")
        assert result == []
