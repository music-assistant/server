"""Unit tests for the provider options surface (get_config_entries)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

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


def _provider(stored: dict[str, ConfigValueType] | None = None) -> Mock:
    """
    Build a provider stub backed by a mutable config dict.

    ``get_config_value`` reads from *stored*; ``_update_config_value`` writes back
    into it, mirroring how the real provider persists drafts and the presets JSON.
    """
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
    # no config entry carries an auth action
    actions = {e.action for e in entries if e.action}
    assert "auth_device" not in actions
    assert "auth_qr" not in actions
    assert "clear_auth" not in actions


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
    """The My Wave preset builder (with its save/delete actions) stays on the surface."""
    entries = await YandexMusicProvider.get_config_entries(_provider())
    actions = {e.action for e in entries if e.action}
    assert "save_wave_preset" in actions
    assert "delete_wave_preset" in actions


async def test_save_wave_preset_action_still_handled() -> None:
    """The save-wave-preset action persists the draft and clears the draft name."""
    stored: dict[str, ConfigValueType] = {
        CONF_WAVE_PRESET_DRAFT_NAME: "Focus",
        CONF_WAVE_PRESETS_DATA: "",
    }
    provider = _provider(stored)
    provider.get_config_entries = AsyncMock(return_value=())

    await YandexMusicProvider.handle_config_action(provider, CONF_ACTION_SAVE_WAVE_PRESET)

    saved = json.loads(str(stored[CONF_WAVE_PRESETS_DATA]))
    assert [p["name"] for p in saved] == ["Focus"]
    # draft name cleared after a successful save
    assert stored[CONF_WAVE_PRESET_DRAFT_NAME] is None
