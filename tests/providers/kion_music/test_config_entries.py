"""Unit tests for the provider options surface (get_config_entries)."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant_models.config_entries import UI_ONLY

from music_assistant.providers.kion_music.constants import (
    CONF_BASE_URL,
    CONF_CODECS,
    CONF_LIKED_TRACKS_MAX_TRACKS,
    CONF_MY_WAVE_MAX_TRACKS,
    CONF_QUALITY,
    CONF_TRANSPORT,
)
from music_assistant.providers.kion_music.provider import KionMusicProvider

# auth keys/actions that used to live on the options surface and now belong to the
# interactive setup flow — none of these may reappear in get_config_entries
_AUTH_KEYS = frozenset({"token", "auth", "clear_auth"})


async def test_get_config_entries_has_no_auth_entries_or_actions() -> None:
    """Authentication moved to the setup flow: no auth entries/actions are emitted."""
    entries = await KionMusicProvider.get_config_entries(Mock(spec=KionMusicProvider))
    assert {entry.key for entry in entries}.isdisjoint(_AUTH_KEYS)
    assert not [entry for entry in entries if entry.action]


async def test_get_config_entries_keeps_genuine_options() -> None:
    """The genuine playback options remain on the options surface."""
    entries = await KionMusicProvider.get_config_entries(Mock(spec=KionMusicProvider))
    assert {
        CONF_QUALITY,
        CONF_MY_WAVE_MAX_TRACKS,
        CONF_LIKED_TRACKS_MAX_TRACKS,
        CONF_TRANSPORT,
        CONF_CODECS,
        CONF_BASE_URL,
    } <= {entry.key for entry in entries}


async def test_get_config_entries_resolve_without_user_input() -> None:
    """Every options entry resolves on its own, so the instance loads with empty values."""
    entries = await KionMusicProvider.get_config_entries(Mock(spec=KionMusicProvider))
    assert not [
        entry.key
        for entry in entries
        if entry.required and entry.default_value is None and entry.type not in UI_ONLY
    ]
