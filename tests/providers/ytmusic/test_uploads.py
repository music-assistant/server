"""Test that YouTube Music uploads are treated as library items."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import ytmusicapi
from music_assistant_models.enums import MediaType

from music_assistant.providers.ytmusic import YoutubeMusicProvider, helpers
from tests.common import use_real_create_task

UPLOAD_ARTIST_ID = "FEmusic_library_privately_owned_artist_detaila_po_CPncyLuHovWS1AESFW1hIHVwbG9hZCB0ZXN0IGFydGlzdA"
UPLOAD_ALBUM_ID = "FEmusic_library_privately_owned_release_detailb_po_CPncyLuHovWS1AESFG1hIHVwbG9hZCB0ZXN0IGFsYnVt"
UPLOAD_ARTIST_CREDIT = {"name": "MA Upload Test Artist", "id": UPLOAD_ARTIST_ID}
THUMBNAILS = [
    {
        "url": "https://www.gstatic.com/youtube/media/ytm/images/cover_track_default@1200.png",
        "width": 120,
        "height": 120,
    }
]

# Trimmed responses of ytmusicapi 1.12.3 for one tagged and one untagged upload
TAGGED_UPLOAD_SONG: dict[str, Any] = {
    "entityId": "t_po_CPncyLuHovWS1AEQlZrVw_______AQ",
    "videoId": "2AcuBnPRv9s",
    "title": "MA Upload Test Track",
    "duration": "0:41",
    "duration_seconds": 41,
    "artists": [UPLOAD_ARTIST_CREDIT],
    "album": {"name": "MA Upload Test Album", "id": UPLOAD_ALBUM_ID},
    "likeStatus": "INDIFFERENT",
    "thumbnails": THUMBNAILS,
}
UNTAGGED_UPLOAD_SONG: dict[str, Any] = {
    "entityId": "t_po_CPncyLuHovWS1AEQsa_ttfj_____AQ",
    "videoId": "herz2_yiP-w",
    "title": "leva-eternity-149473.mp3",
    "duration": "2:25",
    "duration_seconds": 145,
    "artists": [],
    "album": None,
    "likeStatus": "INDIFFERENT",
    "thumbnails": THUMBNAILS,
}
UPLOAD_ALBUM: dict[str, Any] = {
    "browseId": UPLOAD_ALBUM_ID,
    "playlistId": "MLPRb_po_CPncyLuHovWS1AESFG1hIHVwbG9hZCB0ZXN0IGFsYnVt",
    "title": "MA Upload Test Album",
    "thumbnails": THUMBNAILS,
    "type": "Album",
    "artists": [UPLOAD_ARTIST_CREDIT],
    "year": "2026",
}
UPLOAD_ARTIST: dict[str, Any] = {
    "browseId": UPLOAD_ARTIST_ID,
    "artist": "MA Upload Test Artist",
    "type": "artist",
    "shuffleId": "MLPAa_po_CPncyLuHovWS1AESFW1hIHVwbG9hZCB0ZXN0IGFydGlzdA",
    "radioId": "MLPAa_po_CPncyLuHovWS1AESFW1hIHVwbG9hZCB0ZXN0IGFydGlzdA",
    "subscribers": "1",
    "thumbnails": THUMBNAILS,
}


@pytest.fixture
def provider() -> YoutubeMusicProvider:
    """Return a YoutubeMusicProvider instance with mocked dependencies."""
    mass = AsyncMock()
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    use_real_create_task(mass)
    manifest = MagicMock()
    manifest.domain = "ytmusic"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    prov = YoutubeMusicProvider(mass, manifest, config)
    prov._headers = {}
    prov._yt_user = None
    prov.language = "en"
    return prov


@pytest.fixture
def ytm() -> MagicMock:
    """Return a ytmusicapi client with an empty library besides the uploads."""
    client = MagicMock()
    client.get_library_songs.return_value = []
    client.get_library_albums.return_value = []
    client.get_library_subscriptions.return_value = []
    client.get_library_upload_songs.return_value = [
        dict(TAGGED_UPLOAD_SONG),
        dict(UNTAGGED_UPLOAD_SONG),
    ]
    client.get_library_upload_albums.return_value = [dict(UPLOAD_ALBUM)]
    client.get_library_upload_artists.return_value = [dict(UPLOAD_ARTIST)]
    client.get_library_upload_artist.return_value = [dict(TAGGED_UPLOAD_SONG)]
    return client


async def test_library_tracks_include_uploads(
    provider: YoutubeMusicProvider, ytm: MagicMock
) -> None:
    """Uploaded songs are synced, the tagged one with its upload artist and album."""
    # what get_song resolves an untagged upload to
    untagged_track = provider._parse_track(
        {
            "videoId": "herz2_yiP-w",
            "title": "leva-eternity-149473.mp3",
            "artists": [{"channelId": "UCE_krt2fFtgx3nJbI8HGAEg", "name": "Music Library Uploads"}],
        }
    )
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=ytm),
        patch.object(provider, "get_track", AsyncMock(return_value=untagged_track)) as get_track,
    ):
        tracks = [track async for track in provider.get_library_tracks()]

    assert len(tracks) == 2
    tagged = tracks[0]
    assert tagged.item_id == "2AcuBnPRv9s"
    assert [(a.item_id, a.name) for a in tagged.artists] == [
        (UPLOAD_ARTIST_ID, "MA Upload Test Artist")
    ]
    assert tagged.album is not None
    assert tagged.album.item_id == UPLOAD_ALBUM_ID
    assert all(m.is_unique for track in tracks for m in track.provider_mappings)
    # an untagged upload has no artist credit, so it is resolved by its video id
    get_track.assert_awaited_once_with("herz2_yiP-w")
    assert tracks[1] is untagged_track


async def test_library_albums_and_artists_include_uploads(
    provider: YoutubeMusicProvider, ytm: MagicMock
) -> None:
    """Upload albums and upload artists are synced with their upload ids."""
    with patch.object(ytmusicapi, "YTMusic", return_value=ytm):
        albums = [album async for album in provider.get_library_albums()]
        artists = [artist async for artist in provider.get_library_artists()]

    assert [(a.item_id, a.name) for a in albums] == [(UPLOAD_ALBUM_ID, "MA Upload Test Album")]
    assert [a.item_id for a in albums[0].artists] == [UPLOAD_ARTIST_ID]
    assert [(a.item_id, a.name) for a in artists] == [(UPLOAD_ARTIST_ID, "MA Upload Test Artist")]
    assert all(m.is_unique for item in (*albums, *artists) for m in item.provider_mappings)


async def test_get_artist_resolves_upload_artist(ytm: MagicMock) -> None:
    """An upload artist id is resolved from the artist's uploads, not as a channel."""
    with patch.object(ytmusicapi, "YTMusic", return_value=ytm) as ytm_cls:
        artist = await helpers.get_artist(UPLOAD_ARTIST_ID, headers={}, user="brand-id")

    assert artist == {"channelId": UPLOAD_ARTIST_ID, "name": "MA Upload Test Artist"}
    ytm_cls.assert_called_once_with(auth={}, language="en", user="brand-id")
    ytm.get_library_upload_artist.assert_called_once_with(browseId=UPLOAD_ARTIST_ID, limit=1)
    ytm.get_artist.assert_not_called()


async def test_upload_artist_links_to_browse_page(
    provider: YoutubeMusicProvider, ytm: MagicMock
) -> None:
    """An upload artist links to its browse page and has no albums or top tracks to crash on."""
    with patch.object(ytmusicapi, "YTMusic", return_value=ytm):
        artist = await provider.get_artist(UPLOAD_ARTIST_ID)
        albums = await provider.get_artist_albums(UPLOAD_ARTIST_ID)
        top_tracks = await provider.get_artist_toptracks(UPLOAD_ARTIST_ID)

    assert artist.media_type == MediaType.ARTIST
    assert artist.name == "MA Upload Test Artist"
    mapping = next(iter(artist.provider_mappings))
    assert mapping.url == f"https://music.youtube.com/browse/{UPLOAD_ARTIST_ID}"
    assert albums == []
    assert top_tracks == []
