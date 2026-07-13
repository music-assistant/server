"""Tests for the strings.json localization contract (spec 0003)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from unittest import mock

from music_assistant.providers.yandex_music import get_config_entries
from music_assistant.providers.yandex_music.auth import PAGE_CONFIG
from music_assistant.providers.yandex_music.constants import (
    QUALITY_BALANCED,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_SUPERB,
)
from music_assistant.providers.yandex_music.provider import YandexMusicProvider

from .conftest import provider_dir

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

_PROVIDER_DIR = provider_dir()

# The auth status label is dynamic (three states); it keeps an English
# fallback in code and localizes via per-state translation keys. The
# unofficial-provider note is a shared entry owned by the MA server and
# authored in its common strings, not per provider.
_DYNAMIC_LABEL_KEYS = {"label_text", "unofficial_provider_note"}


def _load_strings() -> dict[str, dict[str, object]]:
    """Load the provider's strings.json authoring file."""
    data = json.loads((_PROVIDER_DIR / "strings.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


async def _get_entries() -> tuple[ConfigEntry, ...]:
    """Collect config entries with auth flows stubbed out."""
    mock_mass = mock.MagicMock()
    with mock.patch(
        "music_assistant.providers.yandex_music.perform_device_auth",
        new=mock.AsyncMock(return_value=("x", "m", "r")),
    ):
        return await get_config_entries(mock_mass, None, None, {})


async def test_strings_json_covers_config_entries() -> None:
    """Every config entry key is authored in strings.json config_entries."""
    strings = _load_strings()
    authored = strings["config_entries"]
    entries = await _get_entries()
    missing = [e.key for e in entries if e.key not in _DYNAMIC_LABEL_KEYS and e.key not in authored]
    assert not missing, f"config entries missing from strings.json: {missing}"


async def test_strings_json_has_media_and_manifest_sections() -> None:
    """strings.json ships the media and manifest sections upstream authored."""
    strings = _load_strings()
    assert "folder" in strings["media"]
    assert "playlist" in strings["media"]
    assert "recommendations" in strings["media"]
    assert strings["manifest"]["description"]


async def test_config_entries_have_no_hardcoded_labels() -> None:
    """Static entries author their text in strings.json, not in code."""
    entries = await _get_entries()
    offenders = [
        e.key
        for e in entries
        if e.key not in _DYNAMIC_LABEL_KEYS and (e.label or e.description or e.action_label)
    ]
    assert not offenders, f"entries with hardcoded user-facing text: {offenders}"


async def test_quality_options_use_value_first_signature() -> None:
    """
    Quality options store the QUALITY_* constants as their values.

    Guards against the legacy (title, value) positional order, which the
    current models interpret as value=title — silently corrupting the
    stored setting.
    """
    entries = await _get_entries()
    quality = next(e for e in entries if e.key == "quality")
    assert quality.options is not None
    values = {o.value for o in quality.options}
    assert values == {
        QUALITY_EFFICIENT,
        QUALITY_BALANCED,
        QUALITY_HIGH,
        QUALITY_SUPERB,
    }


async def test_auth_status_label_localized_via_translation_key() -> None:
    """
    The dynamic auth status label carries a per-state translation key.

    The post-login "click Save" warning (spec 0002) must be authored in
    strings.json so Lokalise localizes it like everything else.
    """
    strings = _load_strings()
    mock_mass = mock.MagicMock()
    with mock.patch(
        "music_assistant.providers.yandex_music.perform_device_auth",
        new=mock.AsyncMock(return_value=("x", "m", "r")),
    ):
        entries = await get_config_entries(mock_mass, None, "auth_device", {"session_id": "s1"})
    label = next(e for e in entries if e.key == "label_text")
    assert label.translation_key is not None
    authored = strings["config_entries"][label.translation_key]
    assert isinstance(authored, dict)
    assert "label" in authored


async def test_strings_json_authors_device_page_keys() -> None:
    """
    Every device-code page string is authored under page.device_code.

    The HTML ``lang`` attribute is presentation plumbing, not a
    translatable string, so it stays code-side.
    """
    strings = _load_strings()
    page = strings["page"]
    assert isinstance(page, dict)
    authored = page["device_code"]
    assert isinstance(authored, dict)
    # "context" is the optional provider paragraph — unused (empty) for this
    # provider, so it is not authored in strings.json either.
    expected = set(PAGE_CONFIG.strings_for("en")) - {"lang", "context"}
    assert set(authored) == expected


def _media_label_provider(authored: str | None) -> mock.MagicMock:
    """Build a provider stand-in whose translations lookup returns *authored*."""
    provider = mock.MagicMock()
    provider.domain = "yandex_music"
    provider.mass.translations.get_translation.return_value = authored
    return provider


async def test_media_label_falls_back_verbatim() -> None:
    """Unauthored media keys keep their (already localized) name verbatim."""
    provider = _media_label_provider(None)
    name, translation_key = YandexMusicProvider._media_label(
        cast("YandexMusicProvider", provider), "folder", "landing_tag_rock", "Рок"
    )
    assert name == "Рок"
    assert translation_key is None


async def test_media_label_returns_authored_name_and_key() -> None:
    """Authored media keys resolve to the English source name plus the key."""
    provider = _media_label_provider("My Wave")
    name, translation_key = YandexMusicProvider._media_label(
        cast("YandexMusicProvider", provider), "playlist", "my_wave", "fallback"
    )
    assert name == "My Wave"
    assert translation_key == "my_wave"
