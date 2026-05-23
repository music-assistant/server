"""Tests for user provider filter handling on MusicController."""

from __future__ import annotations

from unittest.mock import Mock, patch

from music_assistant_models.auth import UserRole
from music_assistant_models.enums import ProviderFeature, ProviderType

from music_assistant.controllers.music import MusicController


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


@patch("music_assistant.controllers.music.get_current_user")
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


@patch("music_assistant.controllers.music.get_current_user")
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


@patch("music_assistant.controllers.music.get_current_user")
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


@patch("music_assistant.controllers.music.get_current_user")
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
