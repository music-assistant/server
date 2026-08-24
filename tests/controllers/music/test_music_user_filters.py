"""Tests for user provider filter handling on MusicController."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from music_assistant_models.auth import UserRole
from music_assistant_models.enums import (
    AlbumType,
    ArtistType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.music import MusicController
from music_assistant.mass import MusicAssistant

GET_CURRENT_USER = "music_assistant.controllers.music.media.base.get_current_user"
PROV_A = "prov_a_inst"
PROV_B = "prov_b_inst"


def _make_prov(
    instance_id: str,
    prov_type: ProviderType,
    features: set[ProviderFeature] | None = None,
) -> Mock:
    prov = Mock()
    prov.instance_id = instance_id
    prov.type = prov_type
    prov.supported_features = features or set()
    return prov


@patch("music_assistant.controllers.music.controller.get_current_user")
def test_apply_user_provider_filter_filters_music_providers_for_admin(
    mock_get_user: Mock,
) -> None:
    """Admin's provider_filter must still narrow music providers (issue #5509)."""
    mock_get_user.return_value = Mock(role=UserRole.ADMIN, provider_filter=["m_a"])
    music_a = _make_prov("m_a", ProviderType.MUSIC)
    music_b = _make_prov("m_b", ProviderType.MUSIC)

    controller = MusicController.__new__(MusicController)
    result = controller._apply_user_provider_filter([music_a, music_b])

    assert [p.instance_id for p in result] == ["m_a"]


@patch("music_assistant.controllers.music.controller.get_current_user")
def test_apply_user_provider_filter_passes_non_music_providers(
    mock_get_user: Mock,
) -> None:
    """Metadata and plugin providers bypass the user's music provider filter."""
    mock_get_user.return_value = Mock(role=UserRole.ADMIN, provider_filter=["m_a"])
    music_a = _make_prov("m_a", ProviderType.MUSIC)
    metadata = _make_prov("meta_a", ProviderType.METADATA)
    plugin = _make_prov("plug_a", ProviderType.PLUGIN)

    controller = MusicController.__new__(MusicController)
    result = controller._apply_user_provider_filter([music_a, metadata, plugin])

    assert [p.instance_id for p in result] == ["m_a", "meta_a", "plug_a"]


@patch("music_assistant.controllers.music.controller.get_current_user")
def test_apply_user_provider_filter_no_filter_returns_all(
    mock_get_user: Mock,
) -> None:
    """An empty provider_filter passes every provider through."""
    mock_get_user.return_value = Mock(role=UserRole.ADMIN, provider_filter=[])
    music_a = _make_prov("m_a", ProviderType.MUSIC)
    music_b = _make_prov("m_b", ProviderType.MUSIC)

    controller = MusicController.__new__(MusicController)
    result = controller._apply_user_provider_filter([music_a, music_b])

    assert [p.instance_id for p in result] == ["m_a", "m_b"]


@patch("music_assistant.controllers.music.controller.get_current_user")
async def test_browse_root_honors_admin_provider_filter(mock_get_user: Mock) -> None:
    """Regression for issue #5509: browse must honor an admin's provider_filter."""
    mock_get_user.return_value = Mock(role=UserRole.ADMIN, provider_filter=["m_a"])
    mass = Mock()
    music_a = _make_prov("m_a", ProviderType.MUSIC, {ProviderFeature.BROWSE})
    music_a.domain = "music_a"
    music_a.name = "Music A"
    music_b = _make_prov("m_b", ProviderType.MUSIC, {ProviderFeature.BROWSE})
    music_b.domain = "music_b"
    music_b.name = "Music B"
    mass.get_providers_supporting_feature.side_effect = lambda feature: (
        [music_a, music_b] if feature == ProviderFeature.BROWSE else []
    )

    controller = MusicController.__new__(MusicController)
    controller.mass = mass

    result = await controller.browse(path=None)

    assert [folder.path for folder in result] == ["m_a://"]  # type: ignore[union-attr]


def _mapping(provider_instance: str) -> ProviderMapping:
    """Create an in-library provider mapping with a unique provider item id."""
    return ProviderMapping(
        item_id=uuid4().hex,
        provider_domain=provider_instance.removesuffix("_inst"),
        provider_instance=provider_instance,
        in_library=True,
    )


@pytest.fixture(scope="module")
async def counted_mass(music_mass_module: MusicAssistant) -> MusicAssistant:
    """
    Return a database-only instance seeded with items spread over two providers.

    Every media type is seeded so that a PROV_A filter excludes at least one item but
    keeps at least one, for each of the filter combinations the counts support.
    """
    mass = music_mass_module
    artists: list[Artist] = []
    for name, providers, favorite in (
        ("Artist 01", [PROV_A], True),
        ("Artist 02", [PROV_B], True),
        ("Artist 03", [PROV_A, PROV_B], False),
    ):
        artist = Artist(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={_mapping(prov) for prov in providers},
            favorite=favorite,
        )
        artists.append(await mass.music.artists.add_item_to_library(artist))

    albums: list[Album] = []
    for idx, (name, providers, album_type, favorite) in enumerate(
        (
            ("Album 01", [PROV_A], AlbumType.ALBUM, True),
            ("Album 02", [PROV_B], AlbumType.ALBUM, True),
            ("Album 03", [PROV_A, PROV_B], AlbumType.SINGLE, False),
            ("Album 04", [PROV_B], AlbumType.SINGLE, False),
        )
    ):
        album = Album(
            item_id="0",
            provider="library",
            name=name,
            album_type=album_type,
            provider_mappings={_mapping(prov) for prov in providers},
            # Artist 03 gets no album, so album_artists_only excludes something
            artists=UniqueList([artists[idx % 2]]),
            favorite=favorite,
        )
        albums.append(await mass.music.albums.add_item_to_library(album))

    for idx, (name, providers, favorite) in enumerate(
        (
            ("Track 01", [PROV_A], True),
            ("Track 02", [PROV_B], True),
            ("Track 03", [PROV_A, PROV_B], False),
            ("Track 04", [PROV_B], False),
            ("Track 05", [PROV_A], False),
        )
    ):
        track = Track(
            item_id="0",
            provider="library",
            name=name,
            provider_mappings={_mapping(prov) for prov in providers},
            artists=UniqueList([artists[idx % len(artists)]]),
            album=albums[idx % len(albums)],
            disc_number=1,
            track_number=idx + 1,
            favorite=favorite,
        )
        await mass.music.tracks.add_item_to_library(track)

    # a track whose only PROV_A mapping left the provider's library: it must be excluded
    # from both the filtered listing and the filtered count
    hidden = Track(
        item_id="0",
        provider="library",
        name="Track hidden",
        provider_mappings={_mapping(PROV_A)},
        artists=UniqueList([artists[0]]),
        album=albums[0],
        disc_number=1,
        track_number=99,
        favorite=True,
    )
    db_hidden = await mass.music.tracks.add_item_to_library(hidden)
    await mass.music.database.execute(
        f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 0 "
        "WHERE item_id = :item_id AND media_type = 'track'",
        {"item_id": int(db_hidden.item_id)},
    )
    await mass.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name="Test Genre", provider_mappings=set())
    )
    await mass.music.database.commit()
    return mass


@pytest.mark.parametrize(
    ("media_type", "count_kwargs"),
    [
        ("tracks", {}),
        ("tracks", {"favorite_only": True}),
        ("albums", {}),
        ("albums", {"favorite_only": True}),
        ("albums", {"album_types": [AlbumType.ALBUM]}),
        ("artists", {}),
        ("artists", {"favorite_only": True}),
        ("artists", {"album_artists_only": True}),
        ("artists", {"artist_type": ArtistType.SINGER}),
    ],
)
async def test_library_count_matches_list_for_filtered_user(
    counted_mass: MusicAssistant,
    media_type: str,
    count_kwargs: dict[str, Any],
) -> None:
    """A user's provider_filter must narrow library_count the same way it narrows the list."""
    controller = getattr(counted_mass.music, media_type)
    # library_items spells the favorite filter 'favorite', library_count 'favorite_only'
    list_kwargs = {
        ("favorite" if key == "favorite_only" else key): value
        for key, value in count_kwargs.items()
    }
    with patch(GET_CURRENT_USER, return_value=None):
        unfiltered_count = await controller.library_count(**count_kwargs)
    with patch(GET_CURRENT_USER, return_value=Mock(provider_filter=[PROV_A])):
        filtered_count = await controller.library_count(**count_kwargs)
        # the limit must exceed the seeded row count, so the list is never truncated
        filtered_items = await controller.library_items(limit=500, **list_kwargs)

    assert filtered_count == len(filtered_items)
    # guard against a vacuous pass: the filter must actually exclude something
    assert 0 < filtered_count < unfiltered_count


async def test_library_count_unchanged_without_user(counted_mass: MusicAssistant) -> None:
    """Without an authenticated user the counts stay the true library totals."""
    for media_type in ("tracks", "albums", "artists"):
        controller = getattr(counted_mass.music, media_type)
        total_rows = await counted_mass.music.database.get_count(controller.db_table)
        with patch(GET_CURRENT_USER, return_value=None):
            assert await controller.library_count() == total_rows


async def test_library_count_unchanged_for_user_without_filter(
    counted_mass: MusicAssistant,
) -> None:
    """A user without a provider_filter set sees the true library totals."""
    total_rows = await counted_mass.music.database.get_count("tracks")
    with patch(GET_CURRENT_USER, return_value=Mock(provider_filter=[])):
        assert await counted_mass.music.tracks.library_count() == total_rows


async def test_genre_library_count_ignores_provider_filter(
    counted_mass: MusicAssistant,
) -> None:
    """Genres have no provider mappings, so a provider_filter must not zero their count."""
    with patch(GET_CURRENT_USER, return_value=None):
        unfiltered_count = await counted_mass.music.genres.library_count()
    with patch(GET_CURRENT_USER, return_value=Mock(provider_filter=[PROV_A])):
        assert await counted_mass.music.genres.library_count() == unfiltered_count
    assert unfiltered_count > 0
