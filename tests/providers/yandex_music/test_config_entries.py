"""Unit tests for the provider options surface (get_config_entries)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest import mock

from music_assistant.providers.yandex_music import get_config_entries
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

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

# auth keys/actions that used to live on the options surface and now belong to the
# interactive setup flow — none of these may reappear in get_config_entries
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


async def test_get_config_entries_has_no_auth_entries_or_actions() -> None:
    """Authentication moved to the setup flow: no auth entries/actions are emitted."""
    entries = await get_config_entries(mock.MagicMock(), None, None, {})
    keys = {e.key for e in entries}
    assert keys.isdisjoint(_AUTH_KEYS)
    # no config entry carries an auth action
    actions = {e.action for e in entries if e.action}
    assert "auth_device" not in actions
    assert "auth_qr" not in actions
    assert "clear_auth" not in actions


async def test_get_config_entries_keeps_genuine_options() -> None:
    """The genuine playback options remain on the options surface."""
    entries = await get_config_entries(mock.MagicMock(), None, None, {})
    keys = {e.key for e in entries}
    assert {
        CONF_QUALITY,
        CONF_MY_WAVE_MAX_TRACKS,
        CONF_LIKED_TRACKS_MAX_TRACKS,
        CONF_BASE_URL,
        CONF_RESTRICTIVE_RATE_LIMITS,
    } <= keys


async def test_get_config_entries_keeps_wave_preset_builder() -> None:
    """The My Wave preset builder (with its save/delete actions) stays on the surface."""
    entries = await get_config_entries(mock.MagicMock(), None, None, {})
    actions = {e.action for e in entries if e.action}
    assert "save_wave_preset" in actions
    assert "delete_wave_preset" in actions


async def test_save_wave_preset_action_still_handled() -> None:
    """The save-wave-preset action is still processed by get_config_entries."""
    values: dict[str, ConfigValueType] = {
        CONF_WAVE_PRESET_DRAFT_NAME: "Focus",
        CONF_WAVE_PRESETS_DATA: "",
    }
    await get_config_entries(mock.MagicMock(), None, CONF_ACTION_SAVE_WAVE_PRESET, values)
    stored = json.loads(str(values[CONF_WAVE_PRESETS_DATA]))
    assert [p["name"] for p in stored] == ["Focus"]
    # draft name cleared after a successful save
    assert values[CONF_WAVE_PRESET_DRAFT_NAME] is None
