"""Tests for the AlbumsController.versions method."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from music_assistant_models.enums import MediaType, ProviderFeature

from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.models.music_provider import MusicProvider

from .helpers import create_album


def _make_provider(
    instance_id: str,
    domain: str | None = None,
    features: set[ProviderFeature] | None = None,
    is_streaming: bool = True,
) -> Mock:
    """
    Return a mock MusicProvider with the specified features and media types.

    :param instance_id: The provider instance ID.
    :param domain: The provider domain.
    :param features: Supported provider features.
    :param is_streaming: Whether this is a streaming provider.
    """
    prov = Mock(spec=MusicProvider)
    prov.instance_id = instance_id
    prov.domain = domain or instance_id.split("_", 1)[0]
    prov.supported_features = features or set()
    prov.supported_media_types = {MediaType.ALBUM}
    prov.is_streaming_provider = is_streaming
    prov.get_album_versions = AsyncMock(return_value=[])
    return prov


def _make_controller(providers: list[MusicProvider]) -> AlbumsController:
    """
    Return an AlbumsController wired to the given providers.

    :param providers: List of music providers to register.
    """
    mass = Mock()
    mass.music.get_unique_providers = Mock(return_value=[p.instance_id for p in providers])
    mass.get_provider = Mock(
        side_effect=lambda prov_id, **_kwargs: next(
            (p for p in providers if p.instance_id == prov_id), None
        )
    )
    ctrl = AlbumsController.__new__(AlbumsController)
    ctrl.mass = mass
    return ctrl


async def test_versions_provider_without_album_versions_feature() -> None:
    """
    A provider without ALBUM_VERSIONS retrieves versions via search only.

    The base album must be excluded, alternate search matches returned, and
    provider.get_album_versions must never be called.
    """
    spotify_provider = _make_provider(
        "spotify_1",
        domain="spotify",
        features={ProviderFeature.SEARCH},
    )
    ctrl = _make_controller([spotify_provider])

    base_album = create_album("spotify_1", "album_1", name="Test Album", artist_name="Test Artist")
    other_version = create_album(
        "spotify_1", "album_2", name="Test Album (Deluxe)", artist_name="Test Artist"
    )

    search_mock = AsyncMock(return_value=[base_album, other_version])
    with (
        patch.object(ctrl, "get_provider_item", AsyncMock(return_value=base_album)),
        patch.object(ctrl, "search", search_mock),
    ):
        versions = await ctrl.versions("album_1", "spotify_1")

    search_mock.assert_awaited_once_with("Test Artist - Test Album", "spotify_1")
    spotify_provider.get_album_versions.assert_not_called()
    assert len(versions) == 1
    assert versions[0].item_id == "album_2"
    assert versions[0].name == "Test Album (Deluxe)"


async def test_versions_provider_with_album_versions_feature() -> None:
    """A provider with ALBUM_VERSIONS calls get_album_versions with the mapped id."""
    ytm_provider = _make_provider(
        "ytmusic_1",
        domain="ytmusic",
        features={ProviderFeature.SEARCH, ProviderFeature.ALBUM_VERSIONS},
    )
    ctrl = _make_controller([ytm_provider])

    base_album = create_album("ytmusic_1", "ytm_1", name="Test Album", artist_name="Test Artist")
    deluxe_album = create_album(
        "ytmusic_1", "ytm_deluxe", name="Test Album (Deluxe)", artist_name="Test Artist"
    )

    ytm_provider.get_album_versions = AsyncMock(return_value=[deluxe_album])
    with (
        patch.object(ctrl, "get_provider_item", AsyncMock(return_value=base_album)),
        patch.object(ctrl, "search", AsyncMock(return_value=[])),
    ):
        versions = await ctrl.versions("ytm_1", "ytmusic_1")

    ytm_provider.get_album_versions.assert_awaited_once_with("ytm_1")
    assert len(versions) == 1
    assert versions[0].item_id == "ytm_deluxe"


async def test_versions_provider_with_album_versions_feature_unmapped_album() -> None:
    """A provider with ALBUM_VERSIONS is not called if the album has no mapping for it."""
    ytm_provider = _make_provider(
        "ytmusic_1",
        domain="ytmusic",
        features={ProviderFeature.SEARCH, ProviderFeature.ALBUM_VERSIONS},
    )
    ctrl = _make_controller([ytm_provider])

    # Album only has a Spotify mapping, no YouTube Music mapping
    base_album = create_album("spotify_1", "spot_1", name="Test Album", artist_name="Test Artist")

    search_mock = AsyncMock(return_value=[])
    with (
        patch.object(ctrl, "get_provider_item", AsyncMock(return_value=base_album)),
        patch.object(ctrl, "search", search_mock),
    ):
        versions = await ctrl.versions("spot_1", "spotify_1")

    search_mock.assert_awaited_once_with("Test Artist - Test Album", "ytmusic_1")
    ytm_provider.get_album_versions.assert_not_called()
    assert versions == []


async def test_versions_deduplicates_search_and_specialized_versions() -> None:
    """Versions returned from both search and get_album_versions are deduplicated."""
    ytm_provider = _make_provider(
        "ytmusic_1",
        domain="ytmusic",
        features={ProviderFeature.SEARCH, ProviderFeature.ALBUM_VERSIONS},
    )
    ctrl = _make_controller([ytm_provider])

    base_album = create_album("ytmusic_1", "ytm_1", name="Test Album", artist_name="Test Artist")
    deluxe_album = create_album(
        "ytmusic_1", "ytm_deluxe", name="Test Album (Deluxe)", artist_name="Test Artist"
    )

    ytm_provider.get_album_versions = AsyncMock(return_value=[deluxe_album])
    with (
        patch.object(ctrl, "get_provider_item", AsyncMock(return_value=base_album)),
        patch.object(ctrl, "search", AsyncMock(return_value=[deluxe_album])),
    ):
        versions = await ctrl.versions("ytm_1", "ytmusic_1")

    assert len(versions) == 1
    assert versions[0].item_id == "ytm_deluxe"


async def test_versions_returns_variants_from_both_search_and_specialized_versions() -> None:
    """Distinct album variants returned by search and get_album_versions are both present."""
    ytm_provider = _make_provider(
        "ytmusic_1",
        domain="ytmusic",
        features={ProviderFeature.SEARCH, ProviderFeature.ALBUM_VERSIONS},
    )
    ctrl = _make_controller([ytm_provider])

    base_album = create_album("ytmusic_1", "ytm_1", name="Test Album", artist_name="Test Artist")
    search_variant = create_album(
        "ytmusic_1", "ytm_deluxe", name="Test Album (Deluxe)", artist_name="Test Artist"
    )
    provider_variant = create_album(
        "ytmusic_1", "ytm_remaster", name="Test Album (Remaster)", artist_name="Test Artist"
    )

    ytm_provider.get_album_versions = AsyncMock(return_value=[provider_variant])
    with (
        patch.object(ctrl, "get_provider_item", AsyncMock(return_value=base_album)),
        patch.object(ctrl, "search", AsyncMock(return_value=[search_variant])),
    ):
        versions = await ctrl.versions("ytm_1", "ytmusic_1")

    assert len(versions) == 2
    assert {v.item_id for v in versions} == {"ytm_deluxe", "ytm_remaster"}
