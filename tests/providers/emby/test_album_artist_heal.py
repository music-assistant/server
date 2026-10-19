"""Tests for the one-time Emby album artist heal."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ArtistType, ExternalID, ImageType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    UniqueList,
)

from music_assistant.constants import CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING
from music_assistant.controllers.music.media.base import SUPPRESS_MEDIA_ITEM_UPDATES
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.emby import SUPPORTED_FEATURES, EmbyProvider
from tests.conftest import _music_mass_context

if TYPE_CHECKING:
    import pathlib

    from music_assistant.mass import MusicAssistant

INSTANCE_ID = "emby--test123"
MARKER_KEY = f"providers/{INSTANCE_ID}/values/{CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING}"
SPOTIFY_ARTIST = ProviderMapping(
    item_id="feat", provider_domain="spotify", provider_instance="spotify_1"
)
SPOTIFY_ALBUM = ProviderMapping(
    item_id="album", provider_domain="spotify", provider_instance="spotify_1"
)


def _artist(item_id: str, name: str) -> Artist:
    """Return an artist as parsed by the Emby provider."""
    return Artist(
        item_id=item_id,
        provider=INSTANCE_ID,
        name=name,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain="emby", provider_instance=INSTANCE_ID)
        },
    )


def _album(*artists: Artist) -> Album:
    """Return the test album as parsed by the Emby provider."""
    return Album(
        item_id="album1",
        provider=INSTANCE_ID,
        name="Album",
        provider_mappings={
            ProviderMapping(item_id="album1", provider_domain="emby", provider_instance=INSTANCE_ID)
        },
        artists=UniqueList(artists),
    )


@pytest.fixture
async def music_mass(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant]:
    """Create a fresh database-only Music Assistant instance for each test."""
    async with _music_mass_context(tmp_path) as mass_instance:
        yield mass_instance


@pytest.fixture
async def provider(music_mass: MusicAssistant) -> EmbyProvider:
    """Create an Emby provider on a real library with an album linked to a featured artist."""
    manifest = Mock()
    manifest.domain = "emby"
    config = Mock()
    config.instance_id = INSTANCE_ID
    config.name = "Emby Test"
    config.enabled = True
    config.get_value.return_value = "GLOBAL"
    result = EmbyProvider(music_mass, manifest, config, SUPPORTED_FEATURES)

    async def _library_albums() -> AsyncGenerator[Album]:
        yield _album(_artist("a1", "Main"))

    result.get_library_albums = Mock(side_effect=_library_albums)  # type: ignore[method-assign]
    main = _artist("a1", "Main")
    main.artist_type = ArtistType.NARRATOR
    main.metadata = MediaItemMetadata(description="Bio", genres={"Jazz"})
    main.external_ids = {(ExternalID.MB_ARTIST, "mb-main")}
    album = _album(main, _artist("a2", "Feat"))
    album.metadata = MediaItemMetadata(
        genres={"Rock"},
        images=UniqueList(
            [MediaItemImage(type=ImageType.THUMB, path="cover.jpg", provider=INSTANCE_ID)]
        ),
    )
    album.external_ids = {(ExternalID.MB_RELEASEGROUP, "mb-album")}
    db_album = await music_mass.music.albums.add_item_to_library(album)
    await music_mass.music.albums.set_favorite(db_album.item_id, True)
    music_mass.config.set(MARKER_KEY, True)
    return result


async def _sync_albums(provider: EmbyProvider, media_type: MediaType = MediaType.ALBUM) -> None:
    """Run the provider sync the way the music controller does, without the base sync."""
    token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
    try:
        await provider.sync_library(media_type)
    finally:
        SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)


async def _library_album(mass: MusicAssistant) -> Album:
    album = await mass.music.albums.get_library_item_by_prov_id("album1", INSTANCE_ID)
    assert album is not None
    return album


async def _linked_artist_names(mass: MusicAssistant) -> list[str]:
    return sorted(artist.name for artist in (await _library_album(mass)).artists)


async def test_heal_unlinks_featured_emby_artist(
    music_mass: MusicAssistant, provider: EmbyProvider
) -> None:
    """A featured artist only known to Emby is unlinked and the rest of the album is kept."""
    before = await _library_album(music_mass)
    assert before.favorite is True
    assert before.metadata.genres == {"Rock"}
    assert before.metadata.images
    assert (ExternalID.MB_RELEASEGROUP, "mb-album") in before.external_ids

    with patch.object(MusicProvider, "sync_library", AsyncMock()):
        await _sync_albums(provider)

    after = await _library_album(music_mass)
    assert sorted(artist.name for artist in after.artists) == ["Main"]
    assert music_mass.config.get(MARKER_KEY) is None
    assert after.name == before.name
    assert after.favorite is True
    assert after.metadata.genres == before.metadata.genres
    assert after.metadata.images == before.metadata.images
    assert after.external_ids == before.external_ids
    assert after.provider_mappings == before.provider_mappings


async def test_heal_links_missing_album_artist(
    music_mass: MusicAssistant, provider: EmbyProvider
) -> None:
    """An album artist that the old sync never linked replaces the track artists."""

    async def _compilation() -> AsyncGenerator[Album]:
        yield _album(_artist("va", "Various Artists"))

    provider.get_library_albums.side_effect = _compilation  # type: ignore[attr-defined]

    with patch.object(MusicProvider, "sync_library", AsyncMock()):
        await _sync_albums(provider)

    assert await _linked_artist_names(music_mass) == ["Various Artists"]


async def test_heal_keeps_artist_linked_by_other_provider(
    music_mass: MusicAssistant, provider: EmbyProvider
) -> None:
    """A featured artist stays linked when another provider maps both it and the album."""
    album = await _library_album(music_mass)
    feat = await music_mass.music.artists.get_library_item_by_prov_id("a2", INSTANCE_ID)
    assert feat is not None
    await music_mass.music.albums.add_provider_mapping(album.item_id, SPOTIFY_ALBUM)
    await music_mass.music.artists.add_provider_mapping(feat.item_id, SPOTIFY_ARTIST)

    with patch.object(MusicProvider, "sync_library", AsyncMock()):
        await _sync_albums(provider)

    assert await _linked_artist_names(music_mass) == ["Feat", "Main"]


async def test_heal_unlinks_shared_artist_from_emby_only_album(
    music_mass: MusicAssistant, provider: EmbyProvider
) -> None:
    """A featured artist known to another provider is unlinked from an Emby-only album."""
    feat = await music_mass.music.artists.get_library_item_by_prov_id("a2", INSTANCE_ID)
    assert feat is not None
    await music_mass.music.artists.add_provider_mapping(feat.item_id, SPOTIFY_ARTIST)

    with patch.object(MusicProvider, "sync_library", AsyncMock()):
        await _sync_albums(provider)

    assert await _linked_artist_names(music_mass) == ["Main"]


async def test_heal_unlinks_artist_shared_with_other_emby_instance(
    music_mass: MusicAssistant, provider: EmbyProvider
) -> None:
    """A featured artist that a second Emby instance also maps is unlinked."""
    album = await _library_album(music_mass)
    feat = await music_mass.music.artists.get_library_item_by_prov_id("a2", INSTANCE_ID)
    assert feat is not None
    await music_mass.music.albums.add_provider_mapping(
        album.item_id,
        ProviderMapping(item_id="album1", provider_domain="emby", provider_instance="emby--other"),
    )
    await music_mass.music.artists.add_provider_mapping(
        feat.item_id,
        ProviderMapping(item_id="a2", provider_domain="emby", provider_instance="emby--other"),
    )

    with patch.object(MusicProvider, "sync_library", AsyncMock()):
        await _sync_albums(provider)

    assert await _linked_artist_names(music_mass) == ["Main"]


async def test_heal_skips_correct_album(music_mass: MusicAssistant, provider: EmbyProvider) -> None:
    """An album whose artists are already correct is not written."""
    with patch.object(MusicProvider, "sync_library", AsyncMock()):
        await _sync_albums(provider)
        music_mass.config.set(MARKER_KEY, True)
        with patch.object(
            music_mass.music.albums,
            "update_item_in_library",
            wraps=music_mass.music.albums.update_item_in_library,
        ) as update:
            await _sync_albums(provider)

    update.assert_not_called()
    assert music_mass.config.get(MARKER_KEY) is None


async def test_heal_keeps_artist_details(
    music_mass: MusicAssistant, provider: EmbyProvider
) -> None:
    """A kept album artist keeps its type, metadata, external ids and mappings."""
    before = await music_mass.music.artists.get_library_item_by_prov_id("a1", INSTANCE_ID)
    assert before is not None
    assert before.artist_type == ArtistType.NARRATOR
    assert before.metadata.description == "Bio"

    with patch.object(MusicProvider, "sync_library", AsyncMock()):
        await _sync_albums(provider)

    assert await _linked_artist_names(music_mass) == ["Main"]
    after = await music_mass.music.artists.get_library_item(before.item_id)
    assert after.artist_type == ArtistType.NARRATOR
    assert after.metadata.description == "Bio"
    assert after.metadata.genres == {"Jazz"}
    assert after.external_ids == before.external_ids
    assert after.provider_mappings == before.provider_mappings


@pytest.mark.parametrize(
    ("media_type", "marker"), [(MediaType.ALBUM, False), (MediaType.TRACK, True)]
)
async def test_heal_skipped(
    music_mass: MusicAssistant, provider: EmbyProvider, media_type: MediaType, marker: bool
) -> None:
    """The heal only runs on an album sync of a marked instance."""
    if not marker:
        music_mass.config.remove(MARKER_KEY)

    with patch.object(MusicProvider, "sync_library", AsyncMock()) as base_sync:
        await _sync_albums(provider, media_type)

    base_sync.assert_awaited_once_with(media_type)
    provider.get_library_albums.assert_not_called()  # type: ignore[attr-defined]
    assert await _linked_artist_names(music_mass) == ["Feat", "Main"]


async def test_heal_failure_keeps_marker(
    music_mass: MusicAssistant, provider: EmbyProvider
) -> None:
    """A failed heal is retried on the next album sync."""
    provider.get_library_albums.side_effect = InvalidDataError("boom")  # type: ignore[attr-defined]

    with patch.object(MusicProvider, "sync_library", AsyncMock()):
        await _sync_albums(provider)

    assert music_mass.config.get(MARKER_KEY) is True
