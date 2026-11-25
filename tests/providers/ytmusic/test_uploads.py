"""Test that YouTube Music uploads are treated as library items."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import ytmusicapi
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from ytmusicapi.exceptions import YTMusicServerError

from music_assistant.models.music_provider import SYNC_RUN_STATE, SyncRunState
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


async def test_an_upload_that_cannot_be_resolved_is_skipped(
    provider: YoutubeMusicProvider, ytm: MagicMock
) -> None:
    """An untagged upload that YouTube can not resolve is skipped, not the rest of the sync."""
    error = MediaNotFoundError("Item herz2_yiP-w not found")
    state = SyncRunState()
    token = SYNC_RUN_STATE.set(state)
    try:
        with (
            patch.object(ytmusicapi, "YTMusic", return_value=ytm),
            patch.object(provider, "get_track", AsyncMock(side_effect=error)),
        ):
            tracks = [track async for track in provider.get_library_tracks()]
    finally:
        SYNC_RUN_STATE.reset(token)

    assert [track.item_id for track in tracks] == ["2AcuBnPRv9s"]
    assert state.skipped_item_ids == {MediaType.TRACK: {"herz2_yiP-w"}}


async def test_get_track_resolves_as_the_brand_account(ytm: MagicMock) -> None:
    """A track is resolved as the brand account, which owns that account's uploads."""
    ytm.get_song.return_value = {
        "videoDetails": {
            "videoId": "herz2_yiP-w",
            "title": "leva-eternity-149473.mp3",
            "channelId": "UCE_krt2fFtgx3nJbI8HGAEg",
            "author": "Music Library Uploads",
            "lengthSeconds": "145",
        },
        "microformat": {"microformatDataRenderer": {"thumbnail": {"thumbnails": THUMBNAILS}}},
        "playabilityStatus": {"status": "OK"},
    }
    with patch.object(ytmusicapi, "YTMusic", return_value=ytm) as ytm_cls:
        track = await helpers.get_track("herz2_yiP-w", headers={}, user="brand-id")

    assert track is not None
    assert track["videoId"] == "herz2_yiP-w"
    ytm_cls.assert_called_once_with(auth={}, language="en", user="brand-id")


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


# Results of ytmusicapi 1.12.3 search(scope="uploads"), which carry no entityId
UNTAGGED_UPLOAD_SEARCH_SONG: dict[str, Any] = {
    "category": None,
    "resultType": "song",
    "title": "leva-eternity-149473.mp3",
    "videoId": "herz2_yiP-w",
    "playlistId": None,
    "thumbnails": THUMBNAILS,
}
TAGGED_UPLOAD_SEARCH_SONG: dict[str, Any] = {
    **UNTAGGED_UPLOAD_SEARCH_SONG,
    "title": "MA Upload Test Track",
    "videoId": "2AcuBnPRv9s",
    "artists": [UPLOAD_ARTIST_CREDIT],
    "album": {"name": "MA Upload Test Album", "id": UPLOAD_ALBUM_ID},
}
UPLOAD_SEARCH_ALBUM: dict[str, Any] = {
    "category": None,
    "resultType": "album",
    "title": "MA Upload Test Album",
    "browseId": UPLOAD_ALBUM_ID,
    "artist": "MA Upload Test Artist",
    "thumbnails": THUMBNAILS,
}
UPLOAD_SEARCH_ARTIST: dict[str, Any] = {
    "category": None,
    "resultType": "artist",
    "title": "MA Upload Test Artist",
    "browseId": UPLOAD_ARTIST_ID,
    "thumbnails": THUMBNAILS,
}


def _search_ytm(authenticated: bool) -> MagicMock:
    """Return a ytmusicapi client with one catalog song and the uploads."""
    client = MagicMock()

    def _search(**kwargs: Any) -> list[dict[str, Any]]:
        if kwargs.get("scope") == "uploads":
            assert authenticated, "uploads can only be searched with the account"
            return [
                dict(TAGGED_UPLOAD_SEARCH_SONG),
                dict(UNTAGGED_UPLOAD_SEARCH_SONG),
                dict(UPLOAD_SEARCH_ALBUM),
                dict(UPLOAD_SEARCH_ARTIST),
            ]
        return [
            {
                "resultType": "song",
                "videoId": "catalog1",
                "title": "Catalog Track",
                "artists": [{"id": "UC_artist", "name": "Catalog Artist"}],
            }
        ]

    client.search.side_effect = _search
    return client


async def test_authenticated_search_includes_uploads(provider: YoutubeMusicProvider) -> None:
    """Uploads come first in an authenticated search and exist on this account only."""
    cast("MagicMock", provider.config.get_value).side_effect = lambda key: (
        key == "authenticated_search"
    )
    untagged_track = provider._parse_track(
        {
            "videoId": "herz2_yiP-w",
            "title": "leva-eternity-149473.mp3",
            "artists": [{"channelId": "UCE_krt2fFtgx3nJbI8HGAEg", "name": "Music Library Uploads"}],
        }
    )
    search = cast("Any", YoutubeMusicProvider.search).__wrapped__
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=_search_ytm(authenticated=True)),
        patch.object(provider, "get_track", AsyncMock(return_value=untagged_track)) as get_track,
    ):
        results = await search(provider, "upload", [MediaType.TRACK, MediaType.ARTIST], 5)

    assert [track.item_id for track in results.tracks] == ["2AcuBnPRv9s", "herz2_yiP-w", "catalog1"]
    assert [(a.item_id, a.name) for a in results.artists] == [
        (UPLOAD_ARTIST_ID, "MA Upload Test Artist")
    ]
    # albums were not asked for
    assert results.albums == []
    get_track.assert_awaited_once_with("herz2_yiP-w")
    uploads = [*results.tracks[:2], *results.artists]
    assert all(m.is_unique for item in uploads for m in item.provider_mappings)
    assert not any(m.is_unique for m in results.tracks[2].provider_mappings)


@pytest.mark.parametrize(
    ("authenticated", "media_types"),
    [(False, [MediaType.TRACK]), (True, [MediaType.PLAYLIST])],
)
async def test_search_skips_uploads(
    provider: YoutubeMusicProvider, authenticated: bool, media_types: list[MediaType]
) -> None:
    """Uploads are not searched signed out, nor for media types an upload can not be."""
    cast("MagicMock", provider.config.get_value).side_effect = lambda key: (
        key == "authenticated_search" and authenticated
    )
    ytm = _search_ytm(authenticated=authenticated)
    search = cast("Any", YoutubeMusicProvider.search).__wrapped__
    with patch.object(ytmusicapi, "YTMusic", return_value=ytm):
        await search(provider, "upload", media_types, 5)

    assert [call.kwargs.get("scope") for call in ytm.search.call_args_list] == [None]


async def test_search_skips_an_upload_that_cannot_be_resolved(
    provider: YoutubeMusicProvider,
) -> None:
    """An untagged upload YouTube can not resolve is left out, not the rest of the search."""
    cast("MagicMock", provider.config.get_value).side_effect = lambda key: (
        key == "authenticated_search"
    )
    search = cast("Any", YoutubeMusicProvider.search).__wrapped__
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=_search_ytm(authenticated=True)),
        patch.object(provider, "get_track", AsyncMock(side_effect=YTMusicServerError("400"))),
    ):
        results = await search(provider, "upload", [MediaType.TRACK], 5)

    assert [track.item_id for track in results.tracks] == ["2AcuBnPRv9s", "catalog1"]
