"""Test Deezer personal metadata retrieval and standard library enrichment."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from music_assistant_models.enums import ImageType, MediaType, ProviderType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    Track,
)

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS,
    UNKNOWN_ARTIST,
)
from music_assistant.controllers.metadata.enrichment import MetadataEnrichmentMixin
from music_assistant.providers.deezer.media import DeezerMediaManager
from music_assistant.providers.deezer.parsers import parse_gw_track
from music_assistant.providers.deezer.provider import SUPPORTED_FEATURES, DeezerProvider
from tests.conftest import _music_mass_context
from tests.providers.deezer.test_personal_tracks import COVER_MD5, _upload

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant


def _gw_client(songs: list[dict[str, Any]]) -> Mock:
    """Build a GW client that returns the requested page of personal songs."""

    async def paged(start: int = 0, nb: int = 500) -> dict[str, Any]:
        return {"data": songs[start : start + nb]}

    return Mock(get_personal_songs=AsyncMock(side_effect=paged))


def _build_provider(mass: MusicAssistant, instance_id: str = "deezer--sync") -> DeezerProvider:
    """Build a Deezer provider with real storage and stubbed remote responses."""
    manifest = Mock(domain="deezer", type=ProviderType.MUSIC)
    config = Mock(instance_id=instance_id, name="Deezer", enabled=True)
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS.key: False,
    }.get(key, default)
    provider = DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider.gql_client = Mock(
        get_favorite_artists=AsyncMock(return_value=None),
        get_favorite_albums=AsyncMock(return_value=None),
        get_favorite_tracks=AsyncMock(return_value=None),
    )
    provider.gw_client = _gw_client([_upload()])
    provider.media_manager = DeezerMediaManager(provider)
    return provider


@pytest.fixture
async def sync_provider(tmp_path: Path) -> AsyncGenerator[DeezerProvider]:
    """Create an isolated provider with a real library, config and cache."""
    async with _music_mass_context(tmp_path) as mass:
        await mass.cache._setup_database()
        yield _build_provider(mass)


async def _seed_legacy_track(provider: DeezerProvider, song: dict[str, Any]) -> Track:
    """Store the metadata an older parser produced for an upload with artwork."""
    track = parse_gw_track(provider, {**song, "ALB_PICTURE": ""})
    for item in (track, track.album, *track.artists):
        if isinstance(item, (Track, Album, Artist)):
            for mapping in item.provider_mappings:
                mapping.in_library = True
    if isinstance(track.album, Album):
        await provider.mass.music.albums.add_item_to_library(track.album)
    return await provider.mass.music.tracks.add_item_to_library(track)


@pytest.mark.parametrize("media_type", [MediaType.TRACK, MediaType.ALBUM])
@pytest.mark.parametrize("existing_cover", [False, True])
async def test_metadata_enrichment_backfills_personal_artwork(
    sync_provider: DeezerProvider, media_type: MediaType, existing_cover: bool
) -> None:
    """Standard enrichment adds embedded artwork while preserving existing selections."""
    provider = sync_provider
    mass = provider.mass
    song = _upload(ALB_TITLE="MA Test Album" if media_type == MediaType.ALBUM else "")
    provider.gw_client = _gw_client([song])
    track = await _seed_legacy_track(provider, song)
    if media_type == MediaType.ALBUM:
        assert track.album
        item: Track | Album = await mass.music.albums.get_library_item(track.album.item_id)
    else:
        item = track
    other_image = MediaItemImage(
        type=ImageType.THUMB, path="https://example.com/selected.jpg", provider="url"
    )
    if existing_cover:
        item.metadata.add_image(other_image)
    item.metadata.description = "Existing description"
    mappings = set(item.provider_mappings)
    enrichment = MetadataEnrichmentMixin()
    enrichment.mass = mass
    enrichment.logger = provider.logger
    enrichment.config = Mock()
    enrichment.config.get_value.return_value = False

    with patch.object(mass, "get_provider", return_value=provider):
        if isinstance(item, Album):
            await mass.music.albums.update_item_in_library(item.item_id, item, overwrite=True)
            await enrichment._update_album_metadata(item)
            updated: Track | Album = await mass.music.albums.get_library_item(item.item_id)
        else:
            await mass.music.tracks.update_item_in_library(item.item_id, item, overwrite=True)
            await enrichment._update_track_metadata(item)
            updated = await mass.music.tracks.get_library_item(item.item_id)

    assert updated.metadata.images
    assert any(COVER_MD5 in image.path for image in updated.metadata.images)
    assert updated.metadata.description == "Existing description"
    assert updated.provider_mappings == mappings
    assert updated.metadata.last_refresh
    assert updated.image
    if existing_cover:
        assert updated.image == other_image
    else:
        assert COVER_MD5 in updated.image.path


@pytest.mark.parametrize("album_title", ["MA Test Album", ""])
async def test_sync_imports_upload_without_artist(
    sync_provider: DeezerProvider, album_title: str
) -> None:
    """The normal sync imports an untagged artist with the upload's embedded cover."""
    provider = sync_provider
    song = _upload(ART_NAME="", ALB_TITLE=album_title)
    provider.gw_client = _gw_client([song])

    await provider.sync_library(MediaType.TRACK)

    track = await provider.mass.music.tracks.get_library_item_by_prov_id(
        str(song["SNG_ID"]), provider.instance_id
    )
    assert track
    assert [artist.name for artist in track.artists] == [UNKNOWN_ARTIST]
    assert track.image
    assert COVER_MD5 in track.image.path


@pytest.mark.parametrize("getter", ["get_track", "get_album", "get_artist"])
@pytest.mark.parametrize("checksum", [None, "0", "1"])
async def test_old_parsed_cache_is_invalidated(
    sync_provider: DeezerProvider, getter: str, checksum: str | None
) -> None:
    """Versioning must discard stale parsed models, including the artist fallback."""
    provider = sync_provider
    song = _upload(ART_NAME="")
    provider.gw_client = _gw_client([song])
    old_track = parse_gw_track(provider, {**song, "ALB_PICTURE": "", "ART_NAME": "Old artist"})
    old_item = {
        "get_track": old_track,
        "get_album": old_track.album,
        "get_artist": old_track.artists[0],
    }[getter]
    assert isinstance(old_item, (Track, Album, Artist))
    await provider.mass.cache.set(
        f"{getter}.{old_item.item_id}",
        old_item.to_dict(),
        provider=provider.instance_id,
        expiration=3600 * 24 * 30,
        checksum=checksum,
    )

    item = await getattr(provider, getter)(old_item.item_id)
    assert item.item_id == old_item.item_id
    assert {mapping.item_id for mapping in item.provider_mappings} == {item.item_id}
    if isinstance(item, Artist):
        assert item.name == UNKNOWN_ARTIST
    else:
        assert item.image
        assert COVER_MD5 in item.image.path
        assert item.artists[0].name == UNKNOWN_ARTIST
    assert await getattr(provider, getter)(old_item.item_id) == item


@pytest.mark.parametrize("second_artist", ["Other artist", "MA Test Artist"])
async def test_import_albums_with_the_same_title(
    sync_provider: DeezerProvider, second_artist: str
) -> None:
    """Group albums by artist and retain artwork supplied by any of their tracks."""
    provider = sync_provider
    second_cover = "1234567890abcdef1234567890abcdef"
    songs = [
        _upload(ALB_PICTURE="" if second_artist == "MA Test Artist" else COVER_MD5),
        _upload(
            SNG_ID=-2, SNG_TITLE="Second song", ART_NAME=second_artist, ALB_PICTURE=second_cover
        ),
    ]
    provider.gw_client = _gw_client(songs)
    albums = [album async for album in provider.get_library_albums()]
    assert len(albums) == (1 if second_artist == "MA Test Artist" else 2)
    await provider.sync_library(MediaType.ALBUM)
    await provider.sync_library(MediaType.TRACK)

    for song in songs:
        album = await provider.mass.music.albums.get_library_item_by_prov_id(
            f"personal_album_{song['SNG_ID']}", provider.instance_id
        )
        assert album
        assert album.image
        expected_cover = second_cover if second_artist == "MA Test Artist" else song["ALB_PICTURE"]
        assert expected_cover in album.image.path


@pytest.mark.parametrize("song_id", ["-1", "-2"])
async def test_album_getter_matches_grouped_listing(
    sync_provider: DeezerProvider, song_id: str
) -> None:
    """Each album alias retains its identity while sharing the group's artwork."""
    provider = sync_provider
    songs = [_upload(SNG_ID=-1, ALB_PICTURE=""), _upload(SNG_ID=-2)]
    provider.gw_client = _gw_client(songs)
    albums = [album async for album in provider.get_library_albums()]

    album = await provider.get_album(f"personal_album_{song_id}")

    assert len(albums) == 1
    assert album.item_id == f"personal_album_{song_id}"
    assert {mapping.item_id for mapping in album.provider_mappings} == {album.item_id}
    assert album.metadata.images == albums[0].metadata.images
    assert album.image
    assert COVER_MD5 in album.image.path


@pytest.mark.parametrize("removed", [False, True])
async def test_album_refresh_uses_one_snapshot(
    sync_provider: DeezerProvider, removed: bool
) -> None:
    """A changed remote listing cannot invalidate the album lookup within one refresh."""
    provider = sync_provider
    songs = [_upload()]
    provider.gw_client = _gw_client(songs)
    get_page = provider.gw_client.get_personal_songs

    async def change_after_fetch(start: int = 0, nb: int = 500) -> dict[str, Any]:
        result: dict[str, Any] = await get_page(start=start, nb=nb)
        songs[:] = [] if removed else [_upload(ALB_TITLE="Retagged album")]
        return result

    with patch.object(
        provider.gw_client, "get_personal_songs", side_effect=change_after_fetch
    ) as fetch:
        async with provider.mass.cache.handle_refresh(True):
            album = await provider.get_album("personal_album_-3167960901")

    fetch.assert_awaited_once_with(start=0, nb=500)
    assert album.name == "MA Test Album"
    assert album.image
    assert COVER_MD5 in album.image.path


async def test_missing_personal_album_raises_not_found(sync_provider: DeezerProvider) -> None:
    """An upload absent from the fetched list raises the normal not-found error."""
    sync_provider.gw_client = _gw_client([])

    with pytest.raises(MediaNotFoundError):
        await sync_provider.get_album("personal_album_-3167960901")


@pytest.mark.parametrize("song_count", [500, 501])
@pytest.mark.parametrize("force_refresh", [False, True])
async def test_personal_album_fetches_each_page_once(
    sync_provider: DeezerProvider, song_count: int, force_refresh: bool
) -> None:
    """Cold lookups and forced refreshes fetch each page once and retain album aliases."""
    provider = sync_provider
    songs = [_upload(SNG_ID=-i, ALB_PICTURE="") for i in range(1, song_count + 1)]
    songs[-1]["ALB_PICTURE"] = COVER_MD5
    provider.gw_client = _gw_client(songs)
    if force_refresh:
        await provider.mass.cache.set(
            "_get_personal_songs", [], provider=provider.instance_id, expiration=3600 * 24
        )

    async with provider.mass.cache.handle_refresh(force_refresh):
        album = await provider.get_album(f"personal_album_-{song_count}")

    assert provider.gw_client.get_personal_songs.await_args_list == [
        call(start=0, nb=500),
        call(start=500, nb=500),
    ]
    assert album.item_id == f"personal_album_-{song_count}"
    assert {mapping.item_id for mapping in album.provider_mappings} == {album.item_id}
    assert album.image
    assert COVER_MD5 in album.image.path
