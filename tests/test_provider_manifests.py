"""Tests for provider manifest loading: the has_setup_flow flag, the tier and the media types."""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderTier
from music_assistant_models.provider import ProviderManifest

from music_assistant import mass as mass_module
from music_assistant.models.music_provider import LIBRARY_FEATURE_BY_MEDIA_TYPE, MusicProvider
from music_assistant.providers.plex import helpers as plex_helpers

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# the feature that makes a music provider deliver a media type in every config
_FEATURE_BY_MEDIA_TYPE = {
    **LIBRARY_FEATURE_BY_MEDIA_TYPE,
    MediaType.SOUND_EFFECT: ProviderFeature.SOUND_EFFECTS,
}
# the methods that deliver items of each media type a music provider can declare
_METHODS_BY_MEDIA_TYPE = {
    MediaType.ARTIST: ("get_artist", "get_library_artists"),
    MediaType.ALBUM: ("get_album", "get_library_albums"),
    MediaType.TRACK: ("get_track", "get_library_tracks"),
    MediaType.PLAYLIST: ("get_playlist", "get_library_playlists"),
    MediaType.RADIO: ("get_radio", "get_library_radios"),
    MediaType.AUDIOBOOK: ("get_audiobook", "get_library_audiobooks"),
    MediaType.PODCAST: ("get_podcast", "get_library_podcasts"),
    MediaType.SOUND_EFFECT: ("get_sound_effect", "get_sound_effects"),
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


@pytest.mark.parametrize(
    "domain",
    [
        "airplay_receiver",
        "ariacast_receiver",
        "sendspin",
        "spotify_connect",
        "vban_receiver",
        "yandex_ynison",
    ],
)
async def test_audio_source_providers_declare_the_media_type(domain: str) -> None:
    """A provider that always offers an audio source declares it in its manifest."""
    manifest_path = pathlib.Path(mass_module.PROVIDERS_PATH, domain, "manifest.json")
    manifest = await ProviderManifest.parse(str(manifest_path))
    assert MediaType.AUDIO_SOURCE in manifest.media_types


def _music_provider_domains() -> list[str]:
    """Return the domains of the built-in music providers."""
    return sorted(
        path.parent.name
        for path in pathlib.Path(mass_module.PROVIDERS_PATH).glob("*/manifest.json")
        if json.loads(path.read_text())["type"] == "music"
    )


def _provider_class(domain: str) -> type[MusicProvider]:
    """
    Return the class the setup of a built-in music provider builds.

    :param domain: Domain of the music provider.
    """
    package = importlib.import_module(f"music_assistant.providers.{domain}")
    classes = {
        value
        for value in vars(package).values()
        if isinstance(value, type) and issubclass(value, MusicProvider)
    }
    # setup builds the most derived one, the package may also import the classes it builds on
    (provider_class,) = (
        cls
        for cls in classes
        if not any(other is not cls and issubclass(other, cls) for other in classes)
    )
    return provider_class


def _features_in_every_config(domain: str) -> set[ProviderFeature]:
    """
    Return the features the code of a built-in music provider offers in every configuration.

    :param domain: Domain of the music provider.
    """
    if domain == "plex":
        # the library type chosen at setup picks one of these sets as a whole
        return (
            plex_helpers.SUPPORTED_FEATURES
            & plex_helpers.AUDIOBOOK_FEATURES
            & plex_helpers.PODCAST_FEATURES
        )
    provider_class = _provider_class(domain)
    package = sys.modules[f"music_assistant.providers.{domain}"]
    # the provider is created with SUPPORTED_FEATURES, passed by its setup or by its class (or a
    # class it builds on); its config or account can only add to these
    for module in (package, *(sys.modules[cls.__module__] for cls in provider_class.__mro__)):
        if (features := vars(module).get("SUPPORTED_FEATURES")) is not None:
            return set(features)
    pytest.fail(f"no SUPPORTED_FEATURES found for {domain}")


@pytest.mark.parametrize("domain", _music_provider_domains())
async def test_music_provider_manifest_media_types_agree_with_its_code(domain: str) -> None:
    """
    A built-in music provider's manifest agrees with its code on the media types it offers.

    It declares the media type of each library feature it offers in every config, and no media
    type it has no method to deliver items of.
    """
    manifest_path = pathlib.Path(mass_module.PROVIDERS_PATH, domain, "manifest.json")
    manifest = await ProviderManifest.parse(str(manifest_path))
    provider_class = _provider_class(domain)
    features = _features_in_every_config(domain)
    undeclared = {
        media_type
        for media_type, feature in _FEATURE_BY_MEDIA_TYPE.items()
        if feature in features and media_type not in manifest.media_types
    }
    assert not undeclared
    undeliverable = {
        media_type
        for media_type in manifest.media_types
        if not any(
            getattr(provider_class, method) is not getattr(MusicProvider, method)
            for method in _METHODS_BY_MEDIA_TYPE.get(media_type, ())
        )
    }
    assert not undeliverable


async def test_provider_manifests_api_hands_out_the_media_types(
    mass_minimal: MusicAssistant,
) -> None:
    """The manifests the server hands out carry the media types they declare, also when none."""
    await _load_manifests(mass_minimal)
    manifests = {
        manifest.domain: manifest.to_dict() for manifest in mass_minimal.get_provider_manifests()
    }
    assert manifests["somafm"]["media_types"] == ["radio"]
    assert manifests["filesystem_local"]["media_types"] == []
