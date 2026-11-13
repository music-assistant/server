"""Tests for the texts of providers that are loaded outside the built-in translations."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from music_assistant.controllers import translations as translations_module
from music_assistant.controllers.translations import TranslationController

if TYPE_CHECKING:
    from pathlib import Path

BUILTIN_EN = {
    "common.settings.username.label": "Username",
    "provider.other.settings.host.label": "Host",
}
BUILTIN_NL = {
    "common.settings.username.label": "Gebruikersnaam",
    "provider.other.settings.host.label": "Host (nl)",
}
BUILTIN_DE = {"common.settings.username.label": "Benutzername"}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON file, creating its folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
async def ctrl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TranslationController:
    """Return a set-up controller with built-in English, Dutch and German texts."""
    translations = tmp_path / "builtin"
    _write_json(translations / "en.json", BUILTIN_EN)
    _write_json(translations / "nl.json", BUILTIN_NL)
    _write_json(translations / "de.json", BUILTIN_DE)
    monkeypatch.setattr(translations_module, "TRANSLATIONS_PATH", str(translations))
    monkeypatch.setattr(translations_module, "SOURCE_FILE", str(translations / "en.json"))
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    controller = TranslationController(mass)
    await controller.setup(None)  # type: ignore[arg-type]
    return controller


@pytest.fixture
def provider_path(tmp_path: Path) -> Path:
    """Return a provider folder with strings.json and a Dutch and German locale file."""
    path = tmp_path / "community" / "demo"
    _write_json(
        path / "strings.json",
        {
            "manifest": {"name": "Demo"},
            "settings": {
                "token": {"label": "Token"},
                "username": {"label": "[%key:common::settings::username::label%]"},
            },
        },
    )
    _write_json(path / "translations" / "nl.json", {"settings": {"token": {"label": "Sleutel"}}})
    _write_json(path / "translations" / "de.json", {"settings": {"token": {"label": "Schlüssel"}}})
    return path


async def test_keys_match_the_built_in_flattening(
    ctrl: TranslationController, provider_path: Path
) -> None:
    """The texts land under provider.<domain>. with the keys the build script produces."""
    await ctrl.load_provider_strings("demo", str(provider_path))

    assert ctrl._source == {
        **BUILTIN_EN,
        "provider.demo.manifest.name": "Demo",
        "provider.demo.settings.token.label": "Token",
    }
    assert ctrl.get_translation("manifest.name", owner="demo") == "Demo"


async def test_prefix_is_forced(ctrl: TranslationController, tmp_path: Path) -> None:
    """A provider cannot change common texts or the texts of another provider."""
    path = tmp_path / "evil"
    _write_json(
        path / "strings.json",
        {
            "common": {"settings": {"username": {"label": "Hijacked"}}},
            "provider": {"other": {"settings": {"host": {"label": "Hijacked"}}}},
        },
    )
    _write_json(
        path / "translations" / "nl.json",
        {"common": {"settings": {"username": {"label": "Gekaapt"}}}},
    )
    await ctrl.ensure_locale_loaded("nl")

    await ctrl.load_provider_strings("evil", str(path))

    assert ctrl.get_translation("common.settings.username.label") == "Username"
    assert ctrl.get_translation("provider.other.settings.host.label") == "Host"
    assert ctrl.get_translation("common.settings.username.label", "nl") == "Gebruikersnaam"
    assert ctrl._source["provider.evil.common.settings.username.label"] == "Hijacked"


async def test_reference_falls_back_to_common(
    ctrl: TranslationController, provider_path: Path
) -> None:
    """A reference value is not loaded, so the key resolves to the common text."""
    await ctrl.ensure_locale_loaded("nl")

    await ctrl.load_provider_strings("demo", str(provider_path))

    assert "provider.demo.settings.username.label" not in ctrl._source
    assert ctrl.get_translation("provider.demo.settings.username.label") == "Username"
    assert ctrl.get_translation("provider.demo.settings.username.label", "nl") == "Gebruikersnaam"


async def test_locale_file_merged_before_and_after_the_locale_loads(
    ctrl: TranslationController, provider_path: Path
) -> None:
    """A provider locale file applies to a locale loaded earlier and to one loaded later."""
    await ctrl.ensure_locale_loaded("nl")

    await ctrl.load_provider_strings("demo", str(provider_path))
    assert "de" not in ctrl._locales
    await ctrl.ensure_locale_loaded("de_AT")

    key = "provider.demo.settings.token.label"
    assert ctrl.get_translation(key, "nl") == "Sleutel"
    assert ctrl.get_translation(key, "de_AT") == "Schlüssel"
    assert ctrl.get_translation("common.settings.username.label", "de") == "Benutzername"
    # a locale without a provider file falls back to the provider's English text
    assert ctrl.get_translation(key, "fr") == "Token"


async def test_unload_removes_everything(ctrl: TranslationController, provider_path: Path) -> None:
    """Unload removes exactly what load added, also from locales loaded in between."""
    await ctrl.ensure_locale_loaded("nl")
    await ctrl.load_provider_strings("demo", str(provider_path))
    await ctrl.ensure_locale_loaded("de")

    ctrl.unload_provider_strings("demo")

    assert ctrl._source == BUILTIN_EN
    assert ctrl._locales == {"nl": BUILTIN_NL, "de": BUILTIN_DE}
    assert ctrl._provider_strings == {}
    # a locale loaded after the unload gets no provider texts either
    ctrl._locales.pop("de")
    await ctrl.ensure_locale_loaded("de")
    assert ctrl._locales["de"] == BUILTIN_DE


async def test_load_again_replaces(ctrl: TranslationController, provider_path: Path) -> None:
    """Loading a provider again drops texts the new version no longer has."""
    await ctrl.load_provider_strings("demo", str(provider_path))
    _write_json(provider_path / "strings.json", {"manifest": {"name": "Demo 2"}})
    (provider_path / "translations" / "nl.json").unlink()
    (provider_path / "translations" / "de.json").unlink()

    await ctrl.load_provider_strings("demo", str(provider_path))

    assert ctrl._source == {**BUILTIN_EN, "provider.demo.manifest.name": "Demo 2"}


@pytest.mark.parametrize("broken", ["strings.json", "translations/nl.json"])
async def test_broken_file_loads_nothing(
    ctrl: TranslationController,
    provider_path: Path,
    broken: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A file that is not valid JSON logs a warning and loads none of the provider's texts."""
    await ctrl.ensure_locale_loaded("nl")
    (provider_path / broken).write_text("{not json")

    with caplog.at_level(logging.WARNING):
        await ctrl.load_provider_strings("demo", str(provider_path))

    assert "Failed to load the texts of provider demo" in caplog.text
    assert ctrl._source == BUILTIN_EN
    assert ctrl._locales == {"nl": BUILTIN_NL}
    assert ctrl._provider_strings == {}


async def test_provider_without_texts(ctrl: TranslationController, tmp_path: Path) -> None:
    """A provider folder without strings.json loads nothing and does not warn."""
    await ctrl.load_provider_strings("demo", str(tmp_path / "empty"))

    assert ctrl._source == BUILTIN_EN
    assert ctrl._provider_strings == {}
