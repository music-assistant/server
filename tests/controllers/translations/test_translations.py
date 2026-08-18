"""Tests for the translations controller: catalog loading and key resolution."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from music_assistant_models.api import ErrorResultMessage
from music_assistant_models.background_task import BackgroundTask
from music_assistant_models.config_entries import (
    ConfigActionResult,
    ConfigEntry,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    FlowStepType,
    MediaType,
    ProviderType,
)
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError
from music_assistant_models.media_items.media_item import BrowseFolder, RecommendationFolder
from music_assistant_models.provider import ProviderManifest
from music_assistant_models.setup_flow import SetupFlowStep
from music_assistant_models.translations import TRANSLATION_RESOLVER

from music_assistant.controllers import translations as translations_module
from music_assistant.controllers.config.helpers import _with_translation_owner
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.tasks.controller import _namespaced_translation_key
from music_assistant.controllers.translations import (
    SOURCE_LANGUAGE,
    TranslationController,
    _candidate_keys,
    _format,
    _locale_candidates,
)
from scripts import build_translations as build_translations_module
from scripts.build_translations import (
    _find_duplicate_keys,
    _flatten_into,
    _resolve_references,
    build_translations_source,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


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


def test_namespaced_translation_key() -> None:
    """A bare task key is namespaced under background_task; any dotted key is left as-is."""
    assert _namespaced_translation_key("database_cleanup") == "background_task.database_cleanup"
    assert _namespaced_translation_key(None) is None
    # any key that already carries a namespace (a ".") is returned unchanged
    assert _namespaced_translation_key("background_task.x") == "background_task.x"
    assert _namespaced_translation_key("settings.sync") == "settings.sync"
    assert (
        _namespaced_translation_key("core.metadata.background_task.x")
        == "core.metadata.background_task.x"
    )


def test_resolve_references_validated_and_omitted() -> None:
    """A reference reuses an existing string: its target is validated and the key is omitted."""
    resolved = _resolve_references(
        {
            "common.media.recommendations.recommended_tracks.name": "Recommended tracks",
            "provider.deezer.media.recommendations.recommended_tracks.name": (
                "[%key:common::media::recommendations::recommended_tracks::name%]"
            ),
        }
    )
    # the shared (target) string stays; the referencing key is dropped (resolved via the fallback)
    assert resolved == {
        "common.media.recommendations.recommended_tracks.name": "Recommended tracks",
    }


def test_resolve_references_missing_target_raises() -> None:
    """A reference whose target does not exist fails the build with a clear error."""
    with pytest.raises(ValueError, match="Unresolved translation reference"):
        _resolve_references(
            {"provider.deezer.media.x.name": "[%key:common::media::does::not::exist%]"}
        )


def test_find_duplicate_keys() -> None:
    """Duplicated object keys are reported with their full key path, at any nesting depth."""
    assert _find_duplicate_keys(b'{"a": "1", "b": {"c": "2"}}') == []
    # two blocks with the same name at the top level (parsers keep only the last one)
    assert _find_duplicate_keys(b'{"errors": {"a": "1"}, "other": {}, "errors": {"b": "2"}}') == [
        "errors"
    ]
    assert _find_duplicate_keys(b'{"errors": {"pin": "a", "pin": "b"}}') == ["errors.pin"]
    assert _find_duplicate_keys(b'{"a": [{"k": "1", "k": "2"}, {"k": "3"}]}') == ["a[0].k"]
    assert _find_duplicate_keys(b'{"a": "1", "a": "2", "b": {"x": "1", "x": "2"}}') == [
        "a",
        "b.x",
    ]


def test_build_translations_duplicate_key_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicated key in an authoring file fails the build, naming the file and key path."""
    strings_file = tmp_path / "strings.json"
    strings_file.write_bytes(b'{"errors": {"pin": "a"}, "errors": {"pin": "b"}}')
    monkeypatch.setattr(
        build_translations_module,
        "_collect_source_files",
        lambda: [("provider.foo.", str(strings_file))],
    )
    with pytest.raises(ValueError, match=r"Duplicate strings\.json key\(s\):[\s\S]*: errors"):
        build_translations_source()


def test_build_translations_malformed_file_names_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An authoring file that fails to parse is reported with its file path."""
    strings_file = tmp_path / "strings.json"
    strings_file.write_bytes(b'{"errors": ')
    monkeypatch.setattr(
        build_translations_module,
        "_collect_source_files",
        lambda: [("provider.foo.", str(strings_file))],
    )
    with pytest.raises(ValueError, match=r"strings\.json: "):
        build_translations_source()


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
        # a genre description (resolved into a Genre's nested metadata.description)
        "common.media.genre.classical.description": "Classical music is art music.",
        # error messages: a shared default (common.errors.*) + a provider-specific override
        "common.errors.provider_unavailable": "The provider is currently unavailable.",
        "common.errors.setup_required": "Music Assistant is not set up yet.",
        "common.errors.insufficient_permissions": "You do not have permission to perform this action.",
        "provider.spotify.errors.token_expired": "Your Spotify session expired.",
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
            "common.media.genre.classical.description": "Klassieke muziek is kunstmuziek.",
            "common.errors.provider_unavailable": "De provider is niet beschikbaar.",
            "common.errors.setup_required": "Music Assistant is nog niet ingesteld.",
            "common.errors.insufficient_permissions": "Je hebt geen toestemming voor deze actie.",
            "provider.spotify.errors.token_expired": "Je Spotify-sessie is verlopen.",
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
    """
    The get_*_config_entries handlers return owner-stamped copies, so a bare list localizes.

    The api_commands (config/providers|players|core/get_entries) return a list of ConfigEntry
    rather than a Config object; _with_translation_owner is what gives each entry its owner so it
    resolves the same way embedded entries do.
    """
    ctrl = _nl_controller()
    original = ConfigEntry(key="api_key", type=ConfigEntryType.STRING, label="API key")
    stamped = _with_translation_owner([original], "provider.spotify")
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


def test_provider_sync_task_localized_serialization() -> None:
    """
    Provider-sync BackgroundTasks resolve their name from the built catalog with the provider name.

    Guards that every key returned by MusicController._get_sync_task_translation_key resolves to a
    core.music.background_task.* entry authored in strings.json (the tasks controller namespaces the
    bare key under the background_task group), that the provider name fills the {0} placeholder, and
    that the translation machinery is stripped from the wire under a resolver.
    """
    ctrl = _make_controller()
    ctrl._source = build_translations_source()
    expected = {
        MediaType.ARTIST: "Sync Artists for Spotify",
        MediaType.ALBUM: "Sync Albums for Spotify",
        MediaType.TRACK: "Sync Tracks for Spotify",
        MediaType.PLAYLIST: "Sync Playlists for Spotify",
        MediaType.RADIO: "Sync Radios for Spotify",
        MediaType.AUDIOBOOK: "Sync Audiobooks for Spotify",
        MediaType.PODCAST: "Sync Podcasts for Spotify",
    }
    # the method does not use self, so call it on the class without instantiating the controller
    get_key = MusicController._get_sync_task_translation_key
    with _active_resolver(ctrl, None):
        for media_type, name in expected.items():
            key = get_key(None, media_type)  # type: ignore[arg-type]
            task = BackgroundTask(
                name=f"Sync Spotify {media_type.value}s",  # in-code English fallback
                translation_key=_namespaced_translation_key(key),
                translation_args=["Spotify"],
                translation_owner="core.music",
            )
            serialized = task.to_dict()
            assert serialized["name"] == name
            assert "translation_key" not in serialized
            assert "translation_args" not in serialized


def test_core_owned_strings_moved_out_of_common() -> None:
    """
    Owner-specific strings live under their owner namespace, not in the shared common space.

    Background-task names and the stream server's network/normalization config entries each belong
    to a single core module (or provider), so they are authored there rather than in common; only
    genuinely shared strings (e.g. the bind address, used by several modules) stay in common.
    """
    source = build_translations_source()
    # background-task names moved to their owning module/provider
    assert "core.music.background_task.sync_provider_artists" in source
    assert "core.music.background_task.database_cleanup" in source
    assert "core.cache.background_task.cache_database_cleanup" in source
    assert (
        "provider.lastfm_recommendations.background_task.refresh_lastfm_recommendations" in source
    )
    # stream-server config entries moved to core.streams
    assert "core.streams.config_entries.publish_ip.label" in source
    assert "core.streams.config_entries.background_scan_concurrency.label" in source
    assert "core.streams.config_entries.volume_normalization_radio.label" in source
    # none of the relocated keys remain in common
    assert not any(key.startswith("common.background_task.") for key in source)
    assert "common.config_entries.publish_ip.label" not in source
    assert "common.config_entries.volume_normalization_radio.label" not in source
    # genuinely shared network config (built by several modules) stays in common
    assert "common.config_entries.bind_ip.label" in source
    assert "common.config_entries.bind_port.label" in source


def test_setup_flow_finish_library_sync_is_shared() -> None:
    """
    The FINISH step explaining the initial library import resolves for any music provider.

    The copy is authored once in common, so a provider that ships no strings of its own still
    gets it via the owner -> common fallback.
    """
    ctrl = _make_controller()
    ctrl._source = build_translations_source()
    step = SetupFlowStep(
        flow_id="test",
        step_id="finish_library_sync",
        type=FlowStepType.FINISH,
        translation_owner="provider.qobuz",
    )
    with _active_resolver(ctrl, None):
        serialized = step.to_dict()
    assert (
        serialized["description"]
        == ctrl._source["common.setup_flow.finish_library_sync.description"]
    )


def test_config_action_result_localized_serialization() -> None:
    """ConfigActionResult resolves its message from the owner's config_actions group."""
    ctrl = _make_controller()
    ctrl._source = build_translations_source()
    result = ConfigActionResult(
        translation_key="clear_cache.result", translation_owner="core.cache"
    )
    # no resolver -> no message yet, machinery kept for internal round-trips
    plain = result.to_dict()
    assert plain["message"] is None
    assert plain["translation_key"] == "clear_cache.result"
    # resolver bound -> message filled from the owner's strings, machinery stripped
    with _active_resolver(ctrl, None):
        localized = result.to_dict()
    assert localized["message"] == "The cache has been cleared"
    for machinery_key in ("translation_key", "translation_args", "translation_owner"):
        assert machinery_key not in localized


def test_config_action_result_keys_are_authored() -> None:
    """Every migrated action result key resolves under its owning core module."""
    ctrl = _make_controller()
    ctrl._source = build_translations_source()
    cases = [
        ("clear_cache.result", "core.cache", "The cache has been cleared"),
        ("reset_db.result", "core.music", "The database has been reset."),
    ]
    for key, owner, expected in cases:
        assert ctrl.get_translation(f"config_actions.{key}", owner=owner) == expected


def test_error_result_message_localized_serialization() -> None:
    """ErrorResultMessage localizes `details` from a MusicAssistantError's default key."""
    ctrl = _nl_controller()
    err = ProviderUnavailableError("WebDAV PROPFIND failed with status 503")
    msg = ErrorResultMessage(
        "msg-1",
        err.error_code,
        str(err),
        translation_key=err.translation_key,
        translation_args=err.translation_args,
        translation_owner=err.translation_owner,
    )
    # no resolver -> raw English details + machinery kept (internal round-trips stay localizable)
    plain = msg.to_dict()
    assert plain["details"] == "WebDAV PROPFIND failed with status 503"
    assert plain["translation_key"] == "provider_unavailable"
    # nl resolver -> localized details; error_code/message_id kept, machinery stripped
    with _active_resolver(ctrl, "nl"):
        localized = msg.to_dict()
    assert localized["details"] == "De provider is niet beschikbaar."
    assert localized["error_code"] == err.error_code
    assert localized["message_id"] == "msg-1"
    assert "translation_key" not in localized
    assert "translation_args" not in localized
    # any bound locale (incl. an untranslated one) resolves to the English source string,
    # not the original specific message (the specific text stays in the server log)
    with _active_resolver(ctrl, "fr"):
        fallback = msg.to_dict()
    assert fallback["details"] == "The provider is currently unavailable."


def test_error_result_message_provider_specific_override() -> None:
    """A provider can override translation_key to localize a provider-specific message."""
    ctrl = _nl_controller()
    err = LoginFailed(
        "token exchange failed",
        translation_key="token_expired",
        translation_owner="provider.spotify",
    )
    msg = ErrorResultMessage(
        "m",
        err.error_code,
        str(err),
        translation_key=err.translation_key,
        translation_args=err.translation_args,
        translation_owner=err.translation_owner,
    )
    with _active_resolver(ctrl, "nl"):
        assert msg.to_dict()["details"] == "Je Spotify-sessie is verlopen."


def test_error_result_message_unresolved_key_keeps_details() -> None:
    """An unresolvable or absent translation_key leaves the raw `details` string intact."""
    ctrl = _nl_controller()
    # key not in the catalog -> details kept as-is
    typed = ErrorResultMessage("m", 2, "Track 123 not found", translation_key="media_not_found")
    with _active_resolver(ctrl, "nl"):
        assert typed.to_dict()["details"] == "Track 123 not found"
    # no key at all (e.g. an unexpected non-MA error) -> details kept
    untyped = ErrorResultMessage("m", 999, "boom")
    with _active_resolver(ctrl, "nl"):
        out = untyped.to_dict()
    assert out["details"] == "boom"
    assert out["error_code"] == 999


def test_error_result_message_protocol_error_localization() -> None:
    """The hard-coded auth/command error responses localize via their translation_key too."""
    ctrl = _nl_controller()
    # the new connection-time setup_required key resolves under nl
    setup = ErrorResultMessage(
        "connection", 503, "Setup required", translation_key="setup_required"
    )
    # a reused generic key for the admin/role error
    admin = ErrorResultMessage(
        "m", 22, "Admin access required", translation_key="insufficient_permissions"
    )
    with _active_resolver(ctrl, "nl"):
        assert setup.to_dict()["details"] == "Music Assistant is nog niet ingesteld."
        assert admin.to_dict()["details"] == "Je hebt geen toestemming voor deze actie."


def test_provider_specific_error_keys_resolve_with_params() -> None:
    """Provider-specific error keys resolve from the built source and fill their {0} param."""
    ctrl = _make_controller()
    ctrl._source = build_translations_source()
    cases = [
        ("provider.audiobookshelf.errors.login_failed", "https://abs.local"),
        ("provider.chromecast.errors.app_launch_timeout", "Living Room TV"),
        ("provider.opensubsonic.errors.connect_failed", "subsonic.local"),
    ]
    for key, arg in cases:
        resolved = ctrl.get_translation(key, params=[arg])
        assert resolved is not None, f"{key} did not resolve"
        assert arg in resolved, f"{key} did not fill its param: {resolved!r}"


def test_media_item_without_translation_key_is_untouched() -> None:
    """A media item with no translation_key keeps its in-code name even under a resolver."""
    ctrl = _nl_controller()
    folder = BrowseFolder(item_id="x", provider="library", name="My Folder")
    with _active_resolver(ctrl, "nl"):
        assert folder.to_dict()["name"] == "My Folder"


@pytest.mark.asyncio
async def test_build_translations_matches_runtime_source() -> None:
    """
    The standalone Lokalise source generator yields the same strings as the runtime loader.

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
    # built-in playlists are provider-specific, so they live under the builtin provider
    assert source["provider.builtin.media.playlist.random_album.name"]
    assert source["common.media.folder.albums.name"] == "Albums"  # browse-folder titles
    assert source["common.media.recommendations.recommended_tracks.name"] == "Recommended tracks"
    # the old flat keys are gone (would silently break localization if left behind)
    for stale in (
        "common.media.jazz.name",  # genre was flat
        "common.media.albums.name",  # browse noun was flat
        "common.media.recently_played.name",  # loose recommendation key was flat
        "common.media.builtin_playlist.random_album.name",  # built-in playlist sub-dict
        "common.media.playlist.random_album.name",  # built-in playlists moved to provider.builtin
    ):
        assert stale not in source


def test_genre_descriptions_are_authored() -> None:
    """
    Genre descriptions are authored centrally under common.media.genre.<slug>.description.

    Migrated from the frontend's genre_descriptions.* keys; the model resolver fills them into
    a Genre's nested metadata.description, the same way names fill the top-level name field.
    """
    source = build_translations_source()
    assert source["common.media.genre.jazz.description"].startswith("Jazz")
    # every authored genre name carries a paired description (parity with the migrated set)
    name_slugs = {
        key[len("common.media.genre.") : -len(".name")]
        for key in source
        if key.startswith("common.media.genre.") and key.endswith(".name")
    }
    description_slugs = {
        key[len("common.media.genre.") : -len(".description")]
        for key in source
        if key.startswith("common.media.genre.") and key.endswith(".description")
    }
    assert name_slugs == description_slugs


def test_genre_description_resolves_via_controller() -> None:
    """
    The resolver maps a genre's media.genre.<slug>.description key to the common entry.

    Mirrors how the model's _resolve_translation looks up a genre's nested metadata.description:
    a relative key plus the item's provider as owner, resolved via the common.* rewrite.
    """
    ctrl = _nl_controller()
    assert (
        ctrl.get_translation("media.genre.classical.description", "nl", owner="library")
        == "Klassieke muziek is kunstmuziek."
    )
    # a locale without a translation falls back to the English source
    assert (
        ctrl.get_translation("media.genre.classical.description", "fr", owner="library")
        == "Classical music is art music."
    )


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
