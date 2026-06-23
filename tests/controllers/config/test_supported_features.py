"""Tests for dynamic supported-features resolution in the config controller."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ProviderFeature

from music_assistant.controllers.config import ConfigController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType


def _controller(provider: Any = None) -> ConfigController:
    """Build a bare ConfigController whose mass.get_provider returns the given provider."""
    controller = ConfigController.__new__(ConfigController)
    controller.mass = cast("Any", SimpleNamespace(get_provider=lambda _instance_id: provider))
    return controller


def test_resolve_prefers_dynamic_callable_for_new_instance() -> None:
    """A module-level get_supported_features(values) callable drives the feature set."""
    seen: dict[str, Any] = {}

    def get_supported_features(values: dict[str, ConfigValueType] | None) -> set[ProviderFeature]:
        seen["values"] = values
        return {ProviderFeature.LIBRARY_PODCASTS}

    prov_mod: Any = SimpleNamespace(get_supported_features=get_supported_features)
    values: dict[str, ConfigValueType] = {"library_type": "podcasts"}
    features, provider = _controller()._resolve_supported_features(prov_mod, None, values)

    assert features == {ProviderFeature.LIBRARY_PODCASTS}
    assert provider is None
    # the callable receives the in-progress form values so the UI can react to them
    assert seen["values"] == values


def test_resolve_falls_back_to_static_set_for_new_instance() -> None:
    """Without a callable, the static SUPPORTED_FEATURES set is used."""
    static = {ProviderFeature.LIBRARY_TRACKS, ProviderFeature.LIBRARY_ALBUMS}
    prov_mod: Any = SimpleNamespace(SUPPORTED_FEATURES=static)

    features, provider = _controller()._resolve_supported_features(prov_mod, None, None)

    assert features == static
    assert provider is None


def test_resolve_returns_empty_when_features_undeclared() -> None:
    """A module declaring neither a callable nor a static set yields an empty feature set."""
    prov_mod: Any = SimpleNamespace()

    features, provider = _controller()._resolve_supported_features(prov_mod, None, None)

    assert features == set()
    assert provider is None


def test_resolve_uses_dynamic_features_when_editing_loaded_provider_type() -> None:
    """Editing a loaded provider with a changed library_type uses the form's dynamic features."""
    provider = SimpleNamespace(
        _get_library_type=lambda: "music",
        supported_features={ProviderFeature.LIBRARY_TRACKS},
    )
    prov_mod: Any = SimpleNamespace(
        get_supported_features=lambda _values: {ProviderFeature.LIBRARY_AUDIOBOOKS}
    )
    values: dict[str, ConfigValueType] = {"library_type": "audiobooks"}

    features, returned = _controller(provider)._resolve_supported_features(
        prov_mod, "demo--1", values
    )

    assert features == {ProviderFeature.LIBRARY_AUDIOBOOKS}
    assert returned is provider


def test_resolve_keeps_loaded_features_when_type_unchanged() -> None:
    """A loaded provider with no library_type change keeps its own supported_features."""
    provider = SimpleNamespace(
        _get_library_type=lambda: "music",
        supported_features={ProviderFeature.LIBRARY_TRACKS},
    )
    prov_mod: Any = SimpleNamespace(
        get_supported_features=lambda _values: {ProviderFeature.LIBRARY_RADIOS}
    )

    features, returned = _controller(provider)._resolve_supported_features(
        prov_mod, "demo--1", None
    )

    assert features == {ProviderFeature.LIBRARY_TRACKS}
    assert returned is provider
