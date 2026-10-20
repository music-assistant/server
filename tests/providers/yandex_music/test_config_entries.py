"""Unit tests for the provider options surface (get_config_entries)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.yandex_music.constants import (
    CONF_ACTION_SAVE_WAVE_PRESET,
    CONF_BASE_URL,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_QUALITY,
    CONF_RESTRICTIVE_RATE_LIMITS,
    CONF_WAVE_PRESET_DRAFT_NAME,
    CONF_WAVE_PRESETS_DATA,
)
from music_assistant.providers.yandex_music.provider import YandexMusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

_AUTH_KEYS = frozenset(
    {
        "auth_device",
        "auth_qr",
        "clear_auth",
        "remember_session",
        "token",
        "x_token",
        "refresh_token",
        "label_text",
        "session_id",
    }
)


def _provider(stored: dict[str, ConfigValueType] | None = None) -> Mock:
    """Build a provider stub backed by a mutable config dict."""
    values = stored if stored is not None else {}
    provider = Mock(spec=YandexMusicProvider)
    provider.get_config_value = Mock(
        side_effect=lambda key, default=None, **_kw: values.get(key, default)
    )

    def _update(key: str, value: ConfigValueType, **_kw: object) -> None:
        values[key] = value

    provider._update_config_value = Mock(side_effect=_update)
    return provider


async def test_get_config_entries_has_no_auth_entries_or_actions() -> None:
    """Authentication moved to the setup flow: no auth entries/actions are emitted."""
    entries = await YandexMusicProvider.get_config_entries(_provider())
    keys = {e.key for e in entries}
    assert keys.isdisjoint(_AUTH_KEYS)
    actions = {e.action for e in entries if e.action}
    assert "auth_device" not in actions
    assert "auth_qr" not in actions
    assert "clear_auth" not in actions


async def test_get_config_entries_has_advanced_token_replacement() -> None:
    """A new token can be supplied once without exposing the stored credential."""
    entries = await YandexMusicProvider.get_config_entries(_provider())
    entry = next(item for item in entries if item.key == "manual_token")

    assert entry.type == ConfigEntryType.SECURE_STRING
    assert entry.required is False
    assert entry.advanced is True
    assert entry.requires_reload is True
    assert entry.value is None


async def test_get_config_entries_keeps_genuine_options() -> None:
    """The genuine playback options remain on the options surface."""
    entries = await YandexMusicProvider.get_config_entries(_provider())
    keys = {e.key for e in entries}
    assert {
        CONF_QUALITY,
        CONF_MY_WAVE_MAX_TRACKS,
        CONF_LIKED_TRACKS_MAX_TRACKS,
        CONF_BASE_URL,
        CONF_RESTRICTIVE_RATE_LIMITS,
    } <= keys


async def test_get_config_entries_keeps_wave_preset_builder() -> None:
    """The My Wave preset builder stays on the options surface."""
    entries = await YandexMusicProvider.get_config_entries(_provider())
    actions = {e.action for e in entries if e.action}
    assert "save_wave_preset" in actions
    assert "delete_wave_preset" in actions


async def test_save_wave_preset_action_still_handled() -> None:
    """The save action persists the draft and clears its name."""
    stored: dict[str, ConfigValueType] = {
        CONF_WAVE_PRESET_DRAFT_NAME: "Focus",
        CONF_WAVE_PRESETS_DATA: "",
    }
    provider = _provider(stored)
    provider.get_config_entries = AsyncMock(return_value=())

    await YandexMusicProvider.handle_config_action(provider, CONF_ACTION_SAVE_WAVE_PRESET)

    saved = json.loads(str(stored[CONF_WAVE_PRESETS_DATA]))
    assert [p["name"] for p in saved] == ["Focus"]
    assert stored[CONF_WAVE_PRESET_DRAFT_NAME] is None
