"""Tests for cross-type feature dispatch on MusicAssistant."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant_models.enums import ProviderFeature, ProviderType

from music_assistant.mass import MusicAssistant


def _make_prov(
    instance_id: str,
    prov_type: ProviderType,
    features: set[ProviderFeature],
    priority: int = 50,
    available: bool = True,
) -> Mock:
    prov = Mock()
    prov.instance_id = instance_id
    prov.type = prov_type
    prov.available = available
    prov.supported_features = features
    prov.priority = priority
    return prov


def test_get_providers_supporting_feature_orders_by_type_then_priority() -> None:
    """Providers should be grouped by type tier then sorted by provider priority inside each tier."""
    mass = Mock(spec=MusicAssistant)
    music_a = _make_prov("m_a", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS}, priority=20)
    music_b = _make_prov("m_b", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS}, priority=10)
    meta_a = _make_prov("meta_a", ProviderType.METADATA, {ProviderFeature.SIMILAR_TRACKS})
    plugin_a = _make_prov("plug_a", ProviderType.PLUGIN, {ProviderFeature.SIMILAR_TRACKS})
    unrelated = _make_prov("u", ProviderType.MUSIC, {ProviderFeature.SEARCH})
    mass._providers = {
        "m_a": music_a,
        "m_b": music_b,
        "meta_a": meta_a,
        "plug_a": plugin_a,
        "u": unrelated,
    }

    result = MusicAssistant.get_providers_supporting_feature(mass, ProviderFeature.SIMILAR_TRACKS)

    assert [p.instance_id for p in result] == ["m_b", "m_a", "meta_a", "plug_a"]


def test_get_providers_supporting_feature_skips_unavailable() -> None:
    """Unavailable providers should be skipped."""
    mass = Mock(spec=MusicAssistant)
    alive = _make_prov("a", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS})
    dead = _make_prov("d", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS}, available=False)
    mass._providers = {"a": alive, "d": dead}

    result = MusicAssistant.get_providers_supporting_feature(mass, ProviderFeature.SIMILAR_TRACKS)

    assert [p.instance_id for p in result] == ["a"]


def test_get_providers_supporting_feature_respects_custom_priority() -> None:
    """Caller can restrict / re-order tiers with the priority argument."""
    mass = Mock(spec=MusicAssistant)
    music = _make_prov("m", ProviderType.MUSIC, {ProviderFeature.SIMILAR_TRACKS})
    plugin = _make_prov("p", ProviderType.PLUGIN, {ProviderFeature.SIMILAR_TRACKS})
    mass._providers = {"m": music, "p": plugin}

    result = MusicAssistant.get_providers_supporting_feature(
        mass,
        ProviderFeature.SIMILAR_TRACKS,
        priority=(ProviderType.PLUGIN,),
    )

    assert [p.instance_id for p in result] == ["p"]
