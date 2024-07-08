"""Tests for mediaitem compare helper functions."""

from music_assistant.common.models import media_items
from music_assistant.server.helpers import compare


def test_compare_version() -> None:
    """Test the version compare helper."""
    assert compare.compare_version("Remaster", "remaster") is True
    assert compare.compare_version("Remastered", "remaster") is True
    assert compare.compare_version("Remaster", "") is False
    assert compare.compare_version("Remaster", "Remix") is False
    assert compare.compare_version("", "Deluxe") is False
    assert compare.compare_version("", "Live") is False
    assert compare.compare_version("Live", "live") is True
    assert compare.compare_version("Live", "live version") is True
    assert compare.compare_version("Live version", "live") is True
    assert compare.compare_version("Deluxe Edition", "Deluxe") is True
    assert compare.compare_version("Deluxe Karaoke Edition", "Deluxe") is False
    assert compare.compare_version("Deluxe Karaoke Edition", "Karaoke") is False
    assert compare.compare_version("Deluxe Edition", "Edition Deluxe") is True
    assert compare.compare_version("", "Karaoke Version") is False
    assert compare.compare_version("Karaoke", "Karaoke Version") is True
    assert compare.compare_version("Remaster", "Remaster Edition Deluxe") is False
    assert compare.compare_version("Remastered Version", "Deluxe Version") is False


def test_compare_artist() -> None:
    """Test artist comparison."""
    artist_a = media_items.Artist(
        item_id="1",
        provider="test1",
        name="Artist A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
    )
    artist_b = media_items.Artist(
        item_id="1",
        provider="test2",
        name="Artist A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
    )
    # test match on name match
    assert compare.compare_artist(artist_a, artist_b) is True
    # test match on name mismatch
    artist_b.name = "Artist B"
    assert compare.compare_artist(artist_a, artist_b) is False
    # test on exact item_id match
    artist_b.item_id = artist_a.item_id
    artist_b.provider = artist_a.provider
    assert compare.compare_artist(artist_a, artist_b) is True
    # test on external id match
    artist_b.name = "Artist B"
    artist_b.item_id = "2"
    artist_b.provider = "test2"
    artist_a.external_ids = {(media_items.ExternalID.MUSICBRAINZ, "123")}
    artist_b.external_ids = artist_a.external_ids
    assert compare.compare_artist(artist_a, artist_b) is True
    # test on external id mismatch
    artist_b.name = artist_a.name
    artist_b.external_ids = {(media_items.ExternalID.MUSICBRAINZ, "1234")}
    assert compare.compare_artist(artist_a, artist_b) is False


def test_compare_album() -> None:
    """Test album comparison."""
    album_a = media_items.Album(
        item_id="1",
        provider="test1",
        name="Album A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
    )
    album_b = media_items.Album(
        item_id="1",
        provider="test2",
        name="Album A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
    )
    # test match on name match
    assert compare.compare_album(album_a, album_b) is True
    # test match on name mismatch
    album_b.name = "Album B"
    assert compare.compare_album(album_a, album_b) is False
    # test on version mismatch
    album_b.name = album_a.name
    album_b.version = "Deluxe"
    assert compare.compare_album(album_a, album_b) is False
    album_b.version = "Remix"
    assert compare.compare_album(album_a, album_b) is False
    # test on version match
    album_b.name = album_a.name
    album_a.version = "Deluxe"
    album_b.version = "Deluxe Edition"
    assert compare.compare_album(album_a, album_b) is True
    # test on exact item_id match
    album_b.item_id = album_a.item_id
    album_b.provider = album_a.provider
    assert compare.compare_album(album_a, album_b) is True
    # test on external id match
    album_b.name = "Album B"
    album_b.item_id = "2"
    album_b.provider = "test2"
    album_a.external_ids = {(media_items.ExternalID.MUSICBRAINZ, "123")}
    album_b.external_ids = album_a.external_ids
    assert compare.compare_album(album_a, album_b) is True
    # test on external id mismatch
    album_b.name = album_a.name
    album_b.external_ids = {(media_items.ExternalID.MUSICBRAINZ, "1234")}
    assert compare.compare_album(album_a, album_b) is False
    album_a.external_ids = set()
    album_b.external_ids = set()
    # fail on year mismatch
    album_b.external_ids = set()
    album_a.year = 2021
    album_b.year = 2020
    assert compare.compare_album(album_a, album_b) is False
    # pass on year match
    album_b.year = 2021
    assert compare.compare_album(album_a, album_b) is True
    # fail on artist mismatch
    album_a.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="1", provider="test1", name="Artist A")]
    )
    album_b.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="2", provider="test1", name="Artist B")]
    )
    assert compare.compare_album(album_a, album_b) is False
    # pass on partial artist match (if first artist matches)
    album_a.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="1", provider="test1", name="Artist A")]
    )
    album_b.artists = media_items.UniqueList(
        [
            media_items.ItemMapping(item_id="1", provider="test1", name="Artist A"),
            media_items.ItemMapping(item_id="2", provider="test1", name="Artist B"),
        ]
    )
    assert compare.compare_album(album_a, album_b) is True
    # fail on partial artist match in strict mode
    album_b.artists = media_items.UniqueList(
        [
            media_items.ItemMapping(item_id="2", provider="test1", name="Artist B"),
            media_items.ItemMapping(item_id="1", provider="test1", name="Artist A"),
        ]
    )
    assert compare.compare_album(album_a, album_b) is False
    # partial artist match is allowed in non-strict mode
    assert compare.compare_album(album_a, album_b, False) is True


def test_compare_track() -> None:  # noqa: PLR0915
    """Test track comparison."""
    track_a = media_items.Track(
        item_id="1",
        provider="test1",
        name="Track A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="1", provider_domain="test", provider_instance="test1"
            )
        },
    )
    track_b = media_items.Track(
        item_id="1",
        provider="test2",
        name="Track A",
        provider_mappings={
            media_items.ProviderMapping(
                item_id="2", provider_domain="test", provider_instance="test2"
            )
        },
    )
    # test match on name match
    assert compare.compare_track(track_a, track_b) is True
    # test match on name mismatch
    track_b.name = "Track B"
    assert compare.compare_track(track_a, track_b) is False
    # test on version mismatch
    track_b.name = track_a.name
    track_b.version = "Deluxe"
    assert compare.compare_track(track_a, track_b) is False
    track_b.version = "Remix"
    assert compare.compare_track(track_a, track_b) is False
    # test on version mismatch
    track_b.name = track_a.name
    track_a.version = ""
    track_b.version = "Remaster"
    assert compare.compare_track(track_a, track_b) is False
    track_b.version = "Remix"
    assert compare.compare_track(track_a, track_b) is False
    # test on version match
    track_b.name = track_a.name
    track_a.version = "Deluxe"
    track_b.version = "Deluxe Edition"
    assert compare.compare_track(track_a, track_b) is True
    # test on exact item_id match
    track_b.item_id = track_a.item_id
    track_b.provider = track_a.provider
    assert compare.compare_track(track_a, track_b) is True
    # test on external id match
    track_b.name = "Track B"
    track_b.item_id = "2"
    track_b.provider = "test2"
    track_a.external_ids = {(media_items.ExternalID.MUSICBRAINZ, "123")}
    track_b.external_ids = track_a.external_ids
    assert compare.compare_track(track_a, track_b) is True
    # test on external id mismatch
    track_b.name = track_a.name
    track_b.external_ids = {(media_items.ExternalID.MUSICBRAINZ, "1234")}
    assert compare.compare_track(track_a, track_b) is False
    track_a.external_ids = set()
    track_b.external_ids = set()
    # fail on artist mismatch
    track_a.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="1", provider="test1", name="Artist A")]
    )
    track_b.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="2", provider="test1", name="Artist B")]
    )
    assert compare.compare_track(track_a, track_b) is False
    # pass on partial artist match (if first artist matches)
    track_a.artists = media_items.UniqueList(
        [media_items.ItemMapping(item_id="1", provider="test1", name="Artist A")]
    )
    track_b.artists = media_items.UniqueList(
        [
            media_items.ItemMapping(item_id="1", provider="test1", name="Artist A"),
            media_items.ItemMapping(item_id="2", provider="test1", name="Artist B"),
        ]
    )
    assert compare.compare_track(track_a, track_b) is True
    # fail on partial artist match in strict mode
    track_b.artists = media_items.UniqueList(
        [
            media_items.ItemMapping(item_id="2", provider="test1", name="Artist B"),
            media_items.ItemMapping(item_id="1", provider="test1", name="Artist A"),
        ]
    )
    assert compare.compare_track(track_a, track_b) is False
    # partial artist match is allowed in non-strict mode
    assert compare.compare_track(track_a, track_b, False) is True
    track_b.artists = track_a.artists
    # fail on album mismatch
    track_a.album = media_items.ItemMapping(item_id="1", provider="test1", name="Album A")
    track_b.album = media_items.ItemMapping(item_id="2", provider="test1", name="Album B")
    assert compare.compare_track(track_a, track_b) is False
    # pass on exact album(track) match (regardless duration)
    track_b.album = track_a.album
    track_a.disc_number = 1
    track_a.track_number = 1
    track_b.disc_number = track_a.disc_number
    track_b.track_number = track_a.track_number
    track_a.duration = 300
    track_b.duration = 310
    assert compare.compare_track(track_a, track_b) is True
    # pass on album(track) mismatch
    track_b.album = track_a.album
    track_a.disc_number = 1
    track_a.track_number = 1
    track_b.disc_number = track_a.disc_number
    track_b.track_number = 2
    track_b.duration = track_a.duration
    assert compare.compare_track(track_a, track_b) is False
    # test special case - ISRC match but MusicBrainz ID mismatch
    # this can happen for some classical music albums
    track_a.external_ids = {
        (media_items.ExternalID.ISRC, "123"),
        (media_items.ExternalID.MUSICBRAINZ, "abc"),
    }
    track_b.external_ids = {
        (media_items.ExternalID.ISRC, "123"),
        (media_items.ExternalID.MUSICBRAINZ, "abcd"),
    }
    assert compare.compare_track(track_a, track_b) is False
