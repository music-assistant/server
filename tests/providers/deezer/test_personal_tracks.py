"""
Test that user-uploaded ("personal") Deezer tracks keep their metadata.

The payloads below are shaped after real ``personal_song.getList`` responses.
Deezer derives an upload's title/artist/album from the file's ID3 tags and only
fills ``ALB_PICTURE`` when the file carried embedded artwork, so every one of
these fields can legitimately come back empty.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

from music_assistant_models.media_items import Album, Artist, ItemMapping

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.providers.deezer.media import DeezerMediaManager
from music_assistant.providers.deezer.parsers import parse_gw_track

COVER_MD5 = "7bce541cac7d6a8cc7a73a4cc75eb485"


def _provider() -> Mock:
    return Mock(domain="deezer", instance_id="deezer--test", lookup_key="deezer--test")


def _upload(**overrides: Any) -> dict[str, Any]:
    """Build a user upload as personal_song.getList returns it (negative SNG_ID)."""
    song: dict[str, Any] = {
        "SNG_ID": -3167960901,
        "SNG_TITLE": "MA Cover Test",
        "ART_ID": "0",
        "ART_NAME": "MA Test Artist",
        "ALB_ID": 0,
        "ALB_TITLE": "MA Test Album",
        "ALB_PICTURE": COVER_MD5,
        "DURATION": "6",
    }
    song.update(overrides)
    return song


def test_upload_with_embedded_artwork_gets_a_cover() -> None:
    """An upload whose file had artwork exposes it on the track and its album."""
    track = parse_gw_track(_provider(), _upload())

    assert track.metadata.images
    assert COVER_MD5 in track.metadata.images[0].path
    assert isinstance(track.album, Album)
    assert track.album.metadata.images
    assert track.album.metadata.images[0].path == track.metadata.images[0].path


def test_upload_without_embedded_artwork_has_no_cover() -> None:
    """ALB_PICTURE is present but empty when the file carried no artwork."""
    track = parse_gw_track(_provider(), _upload(ALB_PICTURE=""))

    assert not track.metadata.images
    assert isinstance(track.album, Album)
    assert not track.album.metadata.images


def test_upload_without_artist_tag_still_has_an_artist() -> None:
    """Without an artist the library refuses the track, so fall back to a placeholder."""
    track = parse_gw_track(_provider(), _upload(ART_NAME=""))

    assert [artist.name for artist in track.artists] == [UNKNOWN_ARTIST]


def test_upload_without_album_tag_keeps_the_cover_on_the_track() -> None:
    """With no album there is no Album object to carry the image."""
    track = parse_gw_track(_provider(), _upload(ALB_TITLE=""))

    assert track.album is None
    assert track.metadata.images
    assert COVER_MD5 in track.metadata.images[0].path


def test_uploads_do_not_share_artist_or_album_identity() -> None:
    """Two uploads that happen to share tags must stay separate provider items."""
    first = parse_gw_track(_provider(), _upload(SNG_ID=-1))
    second = parse_gw_track(_provider(), _upload(SNG_ID=-2))

    assert isinstance(first.album, Album)
    assert isinstance(second.album, Album)
    assert first.album.item_id != second.album.item_id
    assert first.artists[0].item_id != second.artists[0].item_id


def test_catalog_track_still_maps_artist_and_album_by_id() -> None:
    """A regular GW track (positive id) keeps using the real Deezer ids."""
    song = _upload(SNG_ID=3135556, ART_ID="27", ALB_ID=302127, ART_NAME="Daft Punk")
    track = parse_gw_track(_provider(), song)

    assert isinstance(track.album, ItemMapping)
    assert track.album.item_id == "302127"
    assert track.album.image is not None
    assert isinstance(track.artists[0], ItemMapping)
    assert track.artists[0].item_id == "27"
    assert not isinstance(track.artists[0], Artist)


async def _personal_getter(manager_method: str, item_id: str, song: dict[str, Any]) -> Any:
    """Call one of the manager's personal-item getters with a single canned upload."""
    manager = Mock(spec=DeezerMediaManager)
    manager.provider = _provider()
    manager.instance_id = "deezer--test"
    manager._get_personal_song = AsyncMock(return_value=song)
    method = getattr(DeezerMediaManager, manager_method).__wrapped__
    return await method(manager, item_id)


async def test_personal_artist_getter_matches_the_track_it_came_from() -> None:
    """get_artist must not rebuild the artist differently than parse_gw_track does."""
    song = _upload(ART_NAME="")
    artist = await _personal_getter("get_artist", "personal_artist_-3167960901", song)
    from_track = parse_gw_track(_provider(), song).artists[0]

    assert artist.name == from_track.name == UNKNOWN_ARTIST
    assert artist.item_id == from_track.item_id


async def test_personal_album_getter_carries_the_cover() -> None:
    """get_album must expose the same album (incl. artwork) as the track's own."""
    album = await _personal_getter("get_album", "personal_album_-3167960901", _upload())

    assert isinstance(album, Album)
    assert album.name == "MA Test Album"
    assert album.metadata.images
    assert COVER_MD5 in album.metadata.images[0].path
    assert [a.name for a in album.artists] == ["MA Test Artist"]
