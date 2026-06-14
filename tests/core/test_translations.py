"""Tests for the translations controller: catalog loading and key resolution."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, ProviderType
from music_assistant_models.media_items.media_item import BrowseFolder, RecommendationFolder
from music_assistant_models.provider import ProviderManifest
from music_assistant_models.translations import TRANSLATION_RESOLVER

from music_assistant.controllers import translations as translations_module
from music_assistant.controllers.config import _with_translation_owner
from music_assistant.controllers.translations import (
    SOURCE_LANGUAGE,
    TranslationController,
    _candidate_keys,
    _format,
    _locale_candidates,
)
from scripts.build_translations import _flatten_into, build_translations_source

if TYPE_CHECKING:
    from collections.abc import Iterator


def _make_controller() -> TranslationController:
    """Build a TranslationController without the full CoreController init (no mass needed)."""
    ctrl = TranslationController.__new__(TranslationController)
    ctrl.logger = logging.getLogger("test.translations")
    ctrl._source = {}
    ctrl._locales = {}
    ctrl._locale_files = {}
    ctrl._locale_locks = {}
    ctrl._available_locales = {SOURCE_LANGUAGE}
    return ctrl


def test_flatten_into() -> None:
    """Nested authoring is flattened into dotted, prefixed keys with string leaves."""
    out: dict[str, str] = {}
    _flatten_into(
        {
            "config_entries": {
                "cookie": {"label": "Login Cookie", "description": "From a session."}
            },
            "media": {"mixes": "Your Mixes"},
            "ignored_non_string": {"nested_number": 5},
        },
        "provider.ytmusic.",
        out,
    )
    assert out == {
        "provider.ytmusic.config_entries.cookie.label": "Login Cookie",
        "provider.ytmusic.config_entries.cookie.description": "From a session.",
        "provider.ytmusic.media.mixes": "Your Mixes",
    }


def test_candidate_keys_common_rewrite() -> None:
    """An owner-namespaced key falls back to the shared common namespace."""
    assert _candidate_keys("provider.ytmusic.config_entries.username.label") == [
        "provider.ytmusic.config_entries.username.label",
        "common.config_entries.username.label",
    ]
    assert _candidate_keys("core.metadata.config_entries.language.label") == [
        "core.metadata.config_entries.language.label",
        "common.config_entries.language.label",
    ]


def test_candidate_keys_name_fallback() -> None:
    """A trailing .name falls back to the bare key, in both owner and common namespaces."""
    assert _candidate_keys("provider.ytmusic.media.mixes.name") == [
        "provider.ytmusic.media.mixes.name",
        "provider.ytmusic.media.mixes",
        "common.media.mixes.name",
        "common.media.mixes",
    ]


def test_candidate_keys_multi_instance_domain_fallback() -> None:
    """A multi-instance owner (<domain>--<id>) also tries the bare-domain prefix."""
    assert _candidate_keys("media.folder.blah.name", "provider.spotify--ab12cd") == [
        "provider.spotify--ab12cd.media.folder.blah.name",
        "provider.spotify--ab12cd.media.folder.blah",
        "provider.spotify.media.folder.blah.name",
        "provider.spotify.media.folder.blah",
        "common.media.folder.blah.name",
        "common.media.folder.blah",
        "media.folder.blah.name",
        "media.folder.blah",
    ]


def test_locale_candidates() -> None:
    """Locale collapses to its base language and normalizes separators."""
    assert _locale_candidates("nl") == ["nl"]
    assert _locale_candidates("de_DE") == ["de_DE", "de"]
    assert _locale_candidates("pt-BR") == ["pt_BR", "pt"]


def test_format_positional_args() -> None:
    """Positional placeholders are substituted; bad/missing args leave the template intact."""
    assert _format("Liked Songs {0}", ["Bob"]) == "Liked Songs Bob"
    assert _format("no params here", None) == "no params here"
    # missing positional arg -> IndexError -> template returned unchanged (never crashes)
    assert _format("missing {0} {1}", ["only"]) == "missing {0} {1}"


def test_get_translation_source_fallback() -> None:
    """With no locale, resolution returns the English source (incl. common rewrite) or None."""
    ctrl = _make_controller()
    ctrl._source = {"common.config_entries.username.label": "Username"}
    # exact common key
    assert ctrl.get_translation("common.config_entries.username.label") == "Username"
    # provider-namespaced key resolves via the common fallback
    assert ctrl.get_translation("provider.ytmusic.config_entries.username.label") == "Username"
    # unknown key -> None (so the caller keeps any existing value rather than a raw key)
    assert ctrl.get_translation("provider.ytmusic.config_entries.nope.label") is None


def test_get_translation_locale_precedence_and_params() -> None:
    """A loaded locale wins over the source; base-language and source act as fallbacks."""
    ctrl = _make_controller()
    ctrl._source = {"provider.ytmusic.media.liked": "Liked Songs {0}"}
    ctrl._locales = {"nl": {"provider.ytmusic.media.liked": "Nummers die je leuk vindt {0}"}}
    assert (
        ctrl.get_translation("provider.ytmusic.media.liked", "nl", params=["Bob"])
        == "Nummers die je leuk vindt Bob"
    )
    # locale not loaded -> falls back to English source
    assert (
        ctrl.get_translation("provider.ytmusic.media.liked", "de", params=["Bob"])
        == "Liked Songs Bob"
    )


def test_get_translation_name_bare_fallback() -> None:
    """A media name key resolves from a bare common entry."""
    ctrl = _make_controller()
    ctrl._source = {"common.media.mixes": "Your Mixes"}
    assert ctrl.get_translation("provider.ytmusic.media.mixes.name") == "Your Mixes"


@pytest.mark.asyncio
async def test_catalog_loading_and_lazy_locale(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """English source (translations/en.json) loads eagerly; a downloaded locale loads lazily."""
    translations = tmp_path / "translations"  # type: ignore[operator]
    translations.mkdir()
    (translations / "en.json").write_text(
        '{"common.settings.username.label": "Username", "provider.demo.media.mixes": "Your Mixes"}'
    )
    (translations / "nl.json").write_text(
        '{"common.settings.username.label": "Gebruikersnaam", '
        '"provider.demo.media.mixes": "Jouw mixes"}'
    )

    monkeypatch.setattr(translations_module, "TRANSLATIONS_PATH", str(translations))
    monkeypatch.setattr(translations_module, "SOURCE_FILE", str(translations / "en.json"))

    ctrl = _make_controller()
    await ctrl.setup(None)  # type: ignore[arg-type]

    # English source loaded eagerly from translations/en.json
    assert ctrl.get_translation("common.settings.username.label") == "Username"
    assert ctrl.get_translation("provider.demo.media.mixes") == "Your Mixes"

    # nl discovered but not parsed yet
    assert "nl" in ctrl.available_locales
    assert "nl" not in ctrl._locales
    # before warm-up, an nl lookup falls back to the English source
    assert ctrl.get_translation("provider.demo.media.mixes", "nl") == "Your Mixes"

    # warm up the locale, then lookups return the translated strings
    await ctrl.ensure_locale_loaded("nl")
    assert "nl" in ctrl._locales
    assert ctrl.get_translation("provider.demo.media.mixes", "nl") == "Jouw mixes"
    assert ctrl.get_translation("common.settings.username.label", "nl") == "Gebruikersnaam"


def _nl_controller() -> TranslationController:
    """Build a controller pre-populated with English source + an nl translation bundle."""
    ctrl = _make_controller()
    ctrl._source = {
        "common.config_entries.username.label": "Username",
        "common.config_categories.generic": "Generic",
        "provider.spotify.config_entries.api_key.label": "API key",
        "provider.demo.manifest.name": "Demo Music Provider",
        "provider.demo.manifest.description": "A demo provider.",
        # recommendation folders key under media.recommendations.*
        "common.media.recommendations.recently_played.name": "Recently played",
        "common.media.recommendations.recently_played.subtitle": "Pick up where you left off",
        # a genre name (searchable, so used by the reverse-lookup test)
        "common.media.genre.classical.name": "Classical",
    }
    ctrl._locales = {
        "nl": {
            "common.config_entries.username.label": "Gebruikersnaam",
            "common.config_categories.generic": "Algemeen",
            "provider.spotify.config_entries.api_key.label": "API-sleutel",
            "provider.demo.manifest.name": "Demo-muziekprovider",
            "provider.demo.manifest.description": "Een demoprovider.",
            "common.media.recommendations.recently_played.name": "Onlangs afgespeeld",
            "common.media.recommendations.recently_played.subtitle": "Ga verder waar je gebleven was",
            "common.media.genre.classical.name": "Klassiek",
        }
    }
    ctrl._available_locales = {"en", "nl"}
    return ctrl


@contextmanager
def _active_resolver(ctrl: TranslationController, locale: str | None) -> Iterator[None]:
    """Bind the controller as the active TRANSLATION_RESOLVER for the given locale."""
    token = TRANSLATION_RESOLVER.set(partial(ctrl.get_translation, locale=locale))
    try:
        yield
    finally:
        TRANSLATION_RESOLVER.reset(token)


def test_config_entry_localized_serialization() -> None:
    """ConfigEntry serialization injects localized label/category_label when a resolver is set."""
    ctrl = _nl_controller()
    entry = ConfigEntry(key="username", type=ConfigEntryType.STRING, label="Username")
    # no resolver -> in-code English; category_label (server-filled) stays null
    plain = entry.to_dict()
    assert plain["label"] == "Username"
    assert plain["category_label"] is None
    # nl resolver -> localized label + injected category_label
    with _active_resolver(ctrl, "nl"):
        localized = entry.to_dict()
    assert localized["label"] == "Gebruikersnaam"
    assert localized["category_label"] == "Algemeen"
    # a locale without translations -> English source
    with _active_resolver(ctrl, "fr"):
        fallback = entry.to_dict()
    assert fallback["label"] == "Username"
    # the translation machinery is never serialized to the client
    for machinery_key in (
        "translation_key",
        "translation_params",
        "category_translation_key",
        "category_translation_params",
    ):
        assert machinery_key not in plain
        assert machinery_key not in localized


def test_config_entry_provider_specific_owner() -> None:
    """A config entry resolves provider-specific strings under its stamped owner namespace."""
    ctrl = _nl_controller()
    entry = ConfigEntry(key="api_key", type=ConfigEntryType.STRING, label="API key")
    entry.translation_owner = "provider.spotify"
    with _active_resolver(ctrl, "nl"):
        assert entry.to_dict()["label"] == "API-sleutel"


def test_provider_config_parse_stamps_owner() -> None:
    """ProviderConfig.parse stamps the provider owner so embedded entries resolve correctly."""
    ctrl = _nl_controller()
    entry = ConfigEntry(key="api_key", type=ConfigEntryType.STRING, label="API key")
    config = ProviderConfig.parse(
        [entry],
        {"type": ProviderType.MUSIC, "domain": "spotify", "instance_id": "spotify--1"},
    )
    assert config.values["api_key"].translation_owner == "provider.spotify"
    with _active_resolver(ctrl, "nl"):
        serialized = config.to_dict()
    assert serialized["values"]["api_key"]["label"] == "API-sleutel"


def test_bare_list_config_entries_stamp_owner() -> None:
    """The get_*_config_entries handlers return owner-stamped copies, so a bare list localizes.

    The api_commands (config/providers|players|core/get_entries) return a list of ConfigEntry
    rather than a Config object; _with_translation_owner is what gives each entry its owner so it
    resolves the same way embedded entries do.
    """
    ctrl = _nl_controller()
    original = ConfigEntry(key="api_key", type=ConfigEntryType.STRING, label="API key")
    stamped = _with_translation_owner([original], "provider.spotify", None, None)
    # the originals (often module-level CONF_ENTRY_* singletons) must not be mutated
    assert original.translation_owner is None
    assert stamped[0].translation_owner == "provider.spotify"
    with _active_resolver(ctrl, "nl"):
        assert stamped[0].to_dict()["label"] == "API-sleutel"


def test_provider_manifest_localized_serialization() -> None:
    """ProviderManifest name/description are localized from provider.<domain>.manifest.*."""
    ctrl = _nl_controller()
    manifest = ProviderManifest(
        type=ProviderType.MUSIC,
        domain="demo",
        name="Demo Music Provider",
        description="A demo provider.",
        codeowners=[],
    )
    assert manifest.to_dict()["name"] == "Demo Music Provider"
    with _active_resolver(ctrl, "nl"):
        localized = manifest.to_dict()
    assert localized["name"] == "Demo-muziekprovider"
    assert localized["description"] == "Een demoprovider."


def test_recommendation_folder_localized_serialization() -> None:
    """A media item with a translation_key gets its name and subtitle localized."""
    ctrl = _nl_controller()
    rec = RecommendationFolder(
        item_id="recently_played",
        provider="library",
        name="Recently played",
        translation_key="recently_played",
        subtitle="Pick up where you left off",
    )
    assert rec.to_dict()["name"] == "Recently played"
    with _active_resolver(ctrl, "nl"):
        localized = rec.to_dict()
    assert localized["name"] == "Onlangs afgespeeld"
    assert localized["subtitle"] == "Ga verder waar je gebleven was"


def test_media_item_without_translation_key_is_untouched() -> None:
    """A media item with no translation_key keeps its in-code name even under a resolver."""
    ctrl = _nl_controller()
    folder = BrowseFolder(item_id="x", provider="library", name="My Folder")
    with _active_resolver(ctrl, "nl"):
        assert folder.to_dict()["name"] == "My Folder"


@pytest.mark.asyncio
async def test_build_translations_matches_runtime_source() -> None:
    """The standalone Lokalise source generator yields the same strings as the runtime loader.

    Guards against the key scheme drifting between the (standalone) build script and the
    controller's live source scan.
    """
    ctrl = _make_controller()
    await ctrl.setup(None)  # type: ignore[arg-type]  # scans the real repo authoring files
    assert ctrl._source == build_translations_source()


def test_media_names_are_keyed_by_media_type() -> None:
    """Media names live under media.<media_type>.*; folders/recommendations are namespaced apart."""
    source = build_translations_source()
    assert source["common.media.genre.jazz.name"] == "Jazz"
    assert source["common.media.playlist.random_album.name"]  # built-in playlists -> playlist.*
    assert source["common.media.folder.albums.name"] == "Albums"  # browse-folder titles
    assert source["common.media.recommendations.made_for_you.name"] == "Made for you"
    # the old flat keys are gone (would silently break localization if left behind)
    for stale in (
        "common.media.jazz.name",  # genre was flat
        "common.media.albums.name",  # browse noun was flat
        "common.media.recently_played.name",  # loose recommendation key was flat
        "common.media.builtin_playlist.random_album.name",  # built-in playlist sub-dict
    ):
        assert stale not in source


async def test_reverse_lookup_media_names() -> None:
    """A localized media name maps back to its canonical English name for the search fallback."""
    ctrl = _nl_controller()
    ctrl.mass = MagicMock()
    # reverse lookups always use the metadata controller's configured language
    ctrl.mass.metadata.locale = "nl"
    # a localized (nl) genre name resolves back to its canonical English name
    assert await ctrl.reverse_lookup_media_names("klassiek") == {"Classical"}
    # recommendation/folder names are NOT searchable, so a localized one yields nothing
    assert await ctrl.reverse_lookup_media_names("onlangs afgespeeld") == set()
    # a non-matching query yields nothing
    assert await ctrl.reverse_lookup_media_names("zzznomatch") == set()
    # English (source) locale: nothing to reverse-translate (literal search already covers it)
    ctrl.mass.metadata.locale = "en"
    assert await ctrl.reverse_lookup_media_names("klassiek") == set()
