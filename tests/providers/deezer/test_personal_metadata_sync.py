"""Test upgrades of cached and stored Deezer personal-item metadata."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ImageType, MediaType, ProviderType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS,
    DB_TABLE_PLAYLOG,
    UNKNOWN_ARTIST,
)
from music_assistant.controllers.config import ConfigController
from music_assistant.providers.deezer.constants import (
    CONF_PERSONAL_METADATA_VERSION,
    PERSONAL_METADATA_VERSION,
)
from music_assistant.providers.deezer.media import DeezerMediaManager
from music_assistant.providers.deezer.parsers import parse_gw_track
from music_assistant.providers.deezer.provider import SUPPORTED_FEATURES, DeezerProvider
from tests.conftest import _music_mass_context
from tests.providers.deezer.test_personal_tracks import COVER_MD5, _upload

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant


def _build_provider(mass: MusicAssistant, instance_id: str = "deezer--sync") -> DeezerProvider:
    """Build a Deezer provider with real storage and stubbed remote responses."""
    manifest = Mock(domain="deezer", type=ProviderType.MUSIC)
    config = Mock(instance_id=instance_id, name="Deezer", enabled=True)
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS.key: False,
    }.get(key, default)
    if not mass.config.get(f"providers/{instance_id}"):
        mass.config.set(f"providers/{instance_id}", {"domain": "deezer", "values": {}})
    provider = DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider.gql_client = Mock(
        get_favorite_artists=AsyncMock(return_value=None),
        get_favorite_albums=AsyncMock(return_value=None),
        get_favorite_tracks=AsyncMock(return_value=None),
    )
    provider.gw_client = Mock(get_personal_songs=AsyncMock(return_value={"data": [_upload()]}))
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


def _stored_version(provider: DeezerProvider, media_type: MediaType) -> Any:
    """Return the completed refresh version for a media type."""
    return provider.mass.config.get_raw_provider_config_value(
        provider.instance_id, f"{CONF_PERSONAL_METADATA_VERSION}{media_type.value}"
    )


@pytest.mark.parametrize("album_title", ["MA Test Album", ""])
@pytest.mark.parametrize("first_type", [MediaType.TRACK, MediaType.ALBUM])
async def test_upgrade_refreshes_once(
    sync_provider: DeezerProvider, album_title: str, first_type: MediaType
) -> None:
    """Upgrade existing uploads, then keep later syncs cheap even after a cache clear."""
    provider = sync_provider
    mass = provider.mass
    song = _upload(ALB_TITLE=album_title)
    provider.gw_client = Mock(get_personal_songs=AsyncMock(return_value={"data": [song]}))
    old_track = await _seed_legacy_track(provider, song)
    assert not old_track.image
    second_type = MediaType.ALBUM if first_type == MediaType.TRACK else MediaType.TRACK

    await provider.sync_library(first_type)
    assert _stored_version(provider, first_type) == PERSONAL_METADATA_VERSION
    assert _stored_version(provider, second_type) is None
    await provider.sync_library(second_type)

    track = await mass.music.tracks.get_library_item(old_track.item_id)
    assert track.image
    assert COVER_MD5 in track.image.path
    assert track.favorite == old_track.favorite
    assert track.provider_mappings == old_track.provider_mappings
    assert track.artists == old_track.artists
    if album_title:
        assert track.album
        album = await mass.music.albums.get_library_item(track.album.item_id)
        assert album.image
        assert COVER_MD5 in album.image.path
    else:
        assert track.album is None

    await mass.cache.clear()
    await mass.config.close()
    mass.config = ConfigController(mass)
    with patch.object(mass, "register_api_command"):
        await mass.config.setup()
    provider = _build_provider(mass)
    provider.gw_client = Mock(get_personal_songs=AsyncMock(return_value={"data": [song]}))
    with (
        patch.object(
            mass.music.tracks,
            "update_item_in_library",
            wraps=mass.music.tracks.update_item_in_library,
        ) as tracks,
        patch.object(
            mass.music.albums,
            "update_item_in_library",
            wraps=mass.music.albums.update_item_in_library,
        ) as albums,
    ):
        await provider.sync_library(first_type)
        await provider.sync_library(second_type)
        tracks.assert_not_awaited()
        albums.assert_not_awaited()


@pytest.mark.parametrize("media_type", [MediaType.TRACK, MediaType.ALBUM])
async def test_partial_refresh_retries_failed_items(
    sync_provider: DeezerProvider, media_type: MediaType
) -> None:
    """A swallowed item failure must leave the refresh pending for the next sync."""
    provider = sync_provider
    songs = [_upload(), _upload(SNG_ID=-2, SNG_TITLE="Second song", ALB_TITLE="Second album")]
    provider.gw_client = Mock(get_personal_songs=AsyncMock(return_value={"data": songs}))
    for song in songs:
        await _seed_legacy_track(provider, song)
    controller = provider.mass.music.get_controller(media_type)
    original_update = controller.update_item_in_library

    async def fail_second(item_id: str | int, item: Any, **kwargs: Any) -> Any:
        if item.item_id.endswith("-2"):
            raise InvalidDataError("Invalid item during refresh")
        return await original_update(item_id, item, **kwargs)

    with patch.object(controller, "update_item_in_library", side_effect=fail_second) as updates:
        await provider.sync_library(media_type)
        assert updates.await_count == 2
    assert _stored_version(provider, media_type) is None

    await provider.sync_library(media_type)
    assert _stored_version(provider, media_type) == PERSONAL_METADATA_VERSION
    for song in songs:
        item_id = str(song["SNG_ID"])
        if media_type == MediaType.ALBUM:
            item_id = f"personal_album_{item_id}"
        item = await controller.get_library_item_by_prov_id(item_id, provider.instance_id)
        assert item
        assert item.image
        assert COVER_MD5 in item.image.path


@pytest.mark.parametrize("error", [InvalidDataError, asyncio.CancelledError])
async def test_interrupted_refresh_is_not_completed(
    sync_provider: DeezerProvider, error: type[BaseException]
) -> None:
    """An interrupted listing remains eligible for refresh when the provider reloads."""
    provider = sync_provider
    await _seed_legacy_track(provider, _upload())

    async def interrupted_listing() -> AsyncGenerator[Track]:
        yield parse_gw_track(provider, _upload())
        raise error("Interrupted listing")

    with (
        patch.object(provider, "get_library_tracks", interrupted_listing),
        pytest.raises(error),
    ):
        await provider.sync_library(MediaType.TRACK)
    assert _stored_version(provider, MediaType.TRACK) is None

    provider = _build_provider(provider.mass)
    await provider.sync_library(MediaType.TRACK)
    assert _stored_version(provider, MediaType.TRACK) == PERSONAL_METADATA_VERSION


@pytest.mark.parametrize("getter", ["get_track", "get_album", "get_artist"])
@pytest.mark.parametrize("checksum", [None, "0"])
async def test_old_parsed_cache_is_invalidated(
    sync_provider: DeezerProvider, getter: str, checksum: str | None
) -> None:
    """Versioning must discard stale parsed models, including the artist fallback."""
    provider = sync_provider
    song = _upload(ART_NAME="")
    provider.gw_client = Mock(get_personal_songs=AsyncMock(return_value={"data": [song]}))
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
    if isinstance(item, Artist):
        assert item.name == UNKNOWN_ARTIST
    else:
        assert item.image
        assert COVER_MD5 in item.image.path
        assert item.artists[0].name == UNKNOWN_ARTIST
    assert await getattr(provider, getter)(old_item.item_id) == item


async def test_refresh_versions_are_instance_scoped(sync_provider: DeezerProvider) -> None:
    """Completing one account must not suppress another account's metadata refresh."""
    await sync_provider.sync_library(MediaType.TRACK)
    other = _build_provider(sync_provider.mass, "deezer--other")
    old_track = await _seed_legacy_track(other, _upload(SNG_TITLE="Other account's song"))
    assert _stored_version(other, MediaType.TRACK) is None

    await other.sync_library(MediaType.TRACK)
    item = await other.mass.music.tracks.get_library_item(old_track.item_id)
    assert item.image
    assert COVER_MD5 in item.image.path
    assert _stored_version(other, MediaType.TRACK) == PERSONAL_METADATA_VERSION


async def test_refresh_preserves_existing_library_data(sync_provider: DeezerProvider) -> None:
    """The refresh merges artwork without replacing user metadata, mappings or history."""
    provider = sync_provider
    mass = provider.mass
    track = await _seed_legacy_track(provider, _upload())
    custom_image = MediaItemImage(
        type=ImageType.THUMB, path="https://example.com/custom.jpg", provider="url"
    )
    track.metadata.images = UniqueList([custom_image])
    track.metadata.lyrics = "User-supplied lyrics"
    track.name = "User-supplied title"
    track.provider_mappings.add(
        ProviderMapping(
            item_id="other-track", provider_domain="test", provider_instance="test--other"
        )
    )
    await mass.music.tracks.update_item_in_library(track.item_id, track, overwrite=True)
    await mass.music.database.insert(
        DB_TABLE_PLAYLOG,
        {
            "item_id": track.item_id,
            "provider": "library",
            "media_type": "track",
            "name": track.name,
            "timestamp": 1234,
            "seconds_played": 6,
            "userid": "test-user",
        },
    )
    history = await mass.music.database.get_rows(DB_TABLE_PLAYLOG)

    await provider.sync_library(MediaType.TRACK)

    updated = await mass.music.tracks.get_library_item(track.item_id)
    assert updated.name == track.name
    assert updated.metadata.lyrics == track.metadata.lyrics
    assert updated.metadata.images
    assert custom_image in updated.metadata.images
    assert any(COVER_MD5 in image.path for image in updated.metadata.images)
    assert updated.provider_mappings == track.provider_mappings
    assert await mass.music.database.get_rows(DB_TABLE_PLAYLOG) == history


async def test_catalog_items_are_not_refreshed(sync_provider: DeezerProvider) -> None:
    """A pending personal metadata refresh does not rewrite unchanged catalog items."""
    provider = sync_provider
    track = parse_gw_track(provider, _upload())
    track.item_id = "123"
    for mapping in track.provider_mappings:
        mapping.item_id = track.item_id
        mapping.in_library = True
    await provider.mass.music.tracks.add_item_to_library(track)

    async def catalog_listing() -> AsyncGenerator[Track]:
        yield track

    controller = provider.mass.music.tracks
    with (
        patch.object(provider, "get_library_tracks", catalog_listing),
        patch.object(
            controller, "update_item_in_library", wraps=controller.update_item_in_library
        ) as updates,
    ):
        await provider.sync_library(MediaType.TRACK)
        updates.assert_not_awaited()


async def test_version_bump_refreshes_existing_uploads(sync_provider: DeezerProvider) -> None:
    """A stored older metadata version triggers another refresh."""
    provider = sync_provider
    track = await _seed_legacy_track(provider, _upload())
    provider.mass.config.set_raw_provider_config_value(
        provider.instance_id, f"{CONF_PERSONAL_METADATA_VERSION}track", "0"
    )

    await provider.sync_library(MediaType.TRACK)

    updated = await provider.mass.music.tracks.get_library_item(track.item_id)
    assert updated.image
    assert COVER_MD5 in updated.image.path
    assert _stored_version(provider, MediaType.TRACK) == PERSONAL_METADATA_VERSION


@pytest.mark.parametrize("second_artist", ["Other artist", "MA Test Artist"])
async def test_refresh_albums_with_the_same_title(
    sync_provider: DeezerProvider, second_artist: str
) -> None:
    """Group albums by artist and retain artwork supplied by any of their tracks."""
    provider = sync_provider
    songs = [
        _upload(ALB_PICTURE="" if second_artist == "MA Test Artist" else COVER_MD5),
        _upload(SNG_ID=-2, SNG_TITLE="Second song", ART_NAME=second_artist),
    ]
    provider.gw_client = Mock(get_personal_songs=AsyncMock(return_value={"data": songs}))
    for song in songs:
        await _seed_legacy_track(provider, song)

    await provider.sync_library(MediaType.ALBUM)
    await provider.sync_library(MediaType.TRACK)

    for song in songs:
        album = await provider.mass.music.albums.get_library_item_by_prov_id(
            f"personal_album_{song['SNG_ID']}", provider.instance_id
        )
        assert album
        assert album.image
        assert COVER_MD5 in album.image.path
