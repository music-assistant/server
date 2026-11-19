"""Tests for provider manifest loading: the has_setup_flow flag, the tier and what it provides."""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import ProviderFeature, ProviderOffering, ProviderTier
from music_assistant_models.provider import ProviderManifest

from music_assistant import mass as mass_module
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.models.plugin import PluginProvider
from music_assistant.models.provider import Provider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# the methods that deliver items of each media type a music provider can declare
_METHODS_BY_MEDIA_TYPE = {
    ProviderOffering.ARTIST: ("get_artist", "get_library_artists"),
    ProviderOffering.ALBUM: ("get_album", "get_library_albums"),
    ProviderOffering.TRACK: ("get_track", "get_library_tracks"),
    ProviderOffering.PLAYLIST: ("get_playlist", "get_library_playlists"),
    ProviderOffering.RADIO: ("get_radio", "get_library_radios"),
    ProviderOffering.AUDIOBOOK: ("get_audiobook", "get_library_audiobooks"),
    ProviderOffering.PODCAST: ("get_podcast", "get_library_podcasts"),
    ProviderOffering.SOUND_EFFECT: ("get_sound_effect", "get_sound_effects"),
}
# the feature in the code of a provider for each offering it declares exactly when it has that
# feature, plus the engine methods it overrides at least one of
_FEATURE_BY_OFFERING = {
    ProviderOffering.AI: (ProviderFeature.AI_QUERY, ("get_ai_engines", "get_image_engines")),
    ProviderOffering.TTS: (ProviderFeature.TTS, ("get_tts_engines",)),
    ProviderOffering.SCROBBLE: (ProviderFeature.SCROBBLE, ()),
    ProviderOffering.AUDIO_SOURCE: (ProviderFeature.AUDIO_SOURCE, ()),
}
_BASE_BY_TYPE: dict[str, type[Provider]] = {
    "music": MusicProvider,
    "player": PlayerProvider,
    "plugin": PluginProvider,
}


async def _load_manifests(mass_minimal: MusicAssistant) -> None:
    """Run the (name-mangled, private) manifest loader against PROVIDERS_PATH."""
    mass_minimal._provider_manifests.clear()
    await mass_minimal._MusicAssistant__load_provider_manifests()  # type: ignore[attr-defined]


def _write_provider(
    providers_dir: pathlib.Path,
    domain: str,
    *,
    setup_flow_source: str | None,
    extra_manifest_keys: dict[str, object] | None = None,
) -> None:
    """Write a minimal provider package with an optional setup_flow.py module."""
    provider_dir = providers_dir / domain
    provider_dir.mkdir()
    (provider_dir / "__init__.py").write_text("")
    (provider_dir / "manifest.json").write_text(
        json.dumps(
            {
                "type": "music",
                "domain": domain,
                "name": domain,
                "description": "test provider",
                "codeowners": [],
                **(extra_manifest_keys or {}),
            }
        )
    )
    if setup_flow_source is not None:
        (provider_dir / "setup_flow.py").write_text(setup_flow_source)


@pytest.fixture
def fake_providers_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect PROVIDERS_PATH to a throwaway directory for the duration of the test."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    monkeypatch.setattr(mass_module, "PROVIDERS_PATH", str(providers_dir))
    return providers_dir


async def test_has_setup_flow_true_when_module_present(
    mass_minimal: MusicAssistant, fake_providers_dir: pathlib.Path
) -> None:
    """A provider shipping a setup_flow.py module is flagged as having one, including over the API."""
    _write_provider(fake_providers_dir, "with_flow", setup_flow_source="")
    await _load_manifests(mass_minimal)
    manifest = mass_minimal.get_provider_manifest("with_flow")
    assert manifest.has_setup_flow is True
    assert manifest.to_dict()["has_setup_flow"] is True
    assert manifest in mass_minimal.get_provider_manifests()


async def test_has_setup_flow_false_when_module_absent(
    mass_minimal: MusicAssistant, fake_providers_dir: pathlib.Path
) -> None:
    """A provider without a setup_flow.py module is flagged as not having one, including over the API."""
    _write_provider(fake_providers_dir, "without_flow", setup_flow_source=None)
    await _load_manifests(mass_minimal)
    manifest = mass_minimal.get_provider_manifest("without_flow")
    assert manifest.has_setup_flow is False
    assert manifest.to_dict()["has_setup_flow"] is False


async def test_has_setup_flow_ignores_internal_import_errors(
    mass_minimal: MusicAssistant, fake_providers_dir: pathlib.Path
) -> None:
    """
    A setup_flow.py that itself fails to import still counts as present.

    The flag is derived from the file's mere existence, not from actually
    importing it, so a bug inside the module must not produce a false negative
    here; that failure only ever surfaces later, when the flow is started.
    """
    _write_provider(
        fake_providers_dir, "broken_flow", setup_flow_source="raise ImportError('boom')"
    )
    await _load_manifests(mass_minimal)
    assert mass_minimal.get_provider_manifest("broken_flow").has_setup_flow is True


async def test_shipped_provider_is_core_whatever_its_manifest_says(
    mass_minimal: MusicAssistant, fake_providers_dir: pathlib.Path
) -> None:
    """A provider loaded from the server's own providers folder is always marked core."""
    _write_provider(
        fake_providers_dir,
        "claims_community",
        setup_flow_source=None,
        extra_manifest_keys={"tier": "community"},
    )
    await _load_manifests(mass_minimal)
    manifest = mass_minimal.get_provider_manifest("claims_community")
    assert manifest.tier == ProviderTier.CORE
    assert manifest.to_dict()["tier"] == "core"


def _provider_domains(*provider_types: str) -> list[str]:
    """
    Return the domains of the built-in providers of the given types.

    :param provider_types: The provider types to return the domains of.
    """
    return sorted(
        path.parent.name
        for path in pathlib.Path(mass_module.PROVIDERS_PATH).glob("*/manifest.json")
        if json.loads(path.read_text())["type"] in provider_types
    )


def _provider_class(domain: str, base: type[Provider]) -> type[Provider] | None:
    """
    Return the class the setup of a built-in provider builds, None when its package has none.

    :param domain: Domain of the provider.
    :param base: The base class of providers of its type.
    """
    package_name = f"music_assistant.providers.{domain}"
    package = importlib.import_module(package_name)
    # a package may also import the classes it builds on or those of another provider, and a
    # plugin may only import its own class in its setup
    classes = {
        value
        for value in vars(package).values()
        if isinstance(value, type)
        and issubclass(value, base)
        and f"{value.__module__}.".startswith(f"{package_name}.")
    }
    # setup builds the most derived one
    most_derived = [
        cls
        for cls in classes
        if not any(other is not cls and issubclass(other, cls) for other in classes)
    ]
    if not most_derived:
        return None
    (provider_class,) = most_derived
    return provider_class


def _features_in_every_config(
    domain: str, provider_class: type[Provider] | None
) -> set[ProviderFeature]:
    """
    Return the features the code of a built-in provider offers in every configuration.

    :param domain: Domain of the provider.
    :param provider_class: The class the setup of the provider builds, if known.
    """
    package = sys.modules[f"music_assistant.providers.{domain}"]
    classes = provider_class.__mro__ if provider_class else ()
    # the provider is created with SUPPORTED_FEATURES, passed by its setup or by its class (or a
    # class it builds on); its config or account can only add to these, and without them it
    # only has the features it adds itself
    for module in (package, *(sys.modules[cls.__module__] for cls in classes)):
        if (features := vars(module).get("SUPPORTED_FEATURES")) is not None:
            return set(features)
    return set()


async def _parse_manifest(domain: str) -> ProviderManifest:
    """
    Return the manifest of a built-in provider.

    :param domain: Domain of the provider.
    """
    return await ProviderManifest.parse(
        str(pathlib.Path(mass_module.PROVIDERS_PATH, domain, "manifest.json"))
    )


@pytest.mark.parametrize("domain", _provider_domains("music"))
async def test_music_provider_manifest_media_types_agree_with_its_code(domain: str) -> None:
    """A built-in music provider declares no media type it has no method to deliver items of."""
    manifest = await _parse_manifest(domain)
    provider_class = _provider_class(domain, MusicProvider)
    assert provider_class is not None
    undeliverable = {
        offering
        for offering in manifest.provides
        if not any(
            getattr(provider_class, method) is not getattr(MusicProvider, method)
            for method in _METHODS_BY_MEDIA_TYPE.get(offering, ())
        )
    }
    assert not undeliverable


@pytest.mark.parametrize("domain", _provider_domains("player", "plugin"))
async def test_provider_manifest_features_agree_with_its_code(domain: str) -> None:
    """
    A built-in player or plugin provider's manifest agrees with its code on its main features.

    It declares ai, tts, scrobble and audio_source when its code offers the matching feature in
    every config. It declares scrobble and audio_source only then, and ai and tts only with a
    method that hands out its engines, as those can depend on the connected service.
    """
    manifest = await _parse_manifest(domain)
    provider_class = _provider_class(domain, _BASE_BY_TYPE[manifest.type])
    features = _features_in_every_config(domain, provider_class)
    for offering, (feature, methods) in _FEATURE_BY_OFFERING.items():
        declared = offering in manifest.provides
        assert declared or feature not in features, offering
        if declared and not methods:
            assert feature in features, offering
        elif declared:
            assert provider_class is not None, offering
            assert any(
                getattr(provider_class, method, None) is not getattr(PluginProvider, method)
                for method in methods
            ), offering


async def test_provider_manifests_api_hands_out_what_they_provide(
    mass_minimal: MusicAssistant,
) -> None:
    """The manifests the server hands out carry what they declare to provide, also when nothing."""
    await _load_manifests(mass_minimal)
    manifests = {
        manifest.domain: manifest.to_dict() for manifest in mass_minimal.get_provider_manifests()
    }
    assert manifests["somafm"]["provides"] == ["radio"]
    assert manifests["lastfm_scrobble"]["provides"] == ["scrobble"]
    assert manifests["filesystem_local"]["provides"] == []
