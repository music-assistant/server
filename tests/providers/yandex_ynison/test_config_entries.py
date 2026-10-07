"""Tests for get_config_entries (the genuine playback options surface)."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant.providers.yandex_ynison.constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
)
from music_assistant.providers.yandex_ynison.provider import YandexYnisonProvider

# auth / account-source keys that moved to the interactive setup flow — none of
# these may reappear on the options surface
_FLOW_ONLY_KEYS = frozenset(
    {
        "auth_qr",
        "clear_auth",
        "remember_session",
        "token",
        "x_token",
        "account_login",
        "ym_instance",
        "label_text",
        "mass_player_id",
        "publish_name",
        "session_id",
    }
)


def _make_provider() -> YandexYnisonProvider:
    """Build a provider instance for inspecting its options."""
    provider = YandexYnisonProvider.__new__(YandexYnisonProvider)
    provider.mass = MagicMock()
    return provider


async def test_no_auth_or_account_source_entries() -> None:
    """Authentication, account and device identity live only in the setup flow."""
    entries = await _make_provider().get_config_entries()
    keys = {e.key for e in entries}
    assert keys.isdisjoint(_FLOW_ONLY_KEYS)
    actions = {e.action for e in entries if e.action}
    assert not actions


async def test_genuine_options_present() -> None:
    """The genuine playback options remain on the options surface."""
    entries = await _make_provider().get_config_entries()
    keys = {e.key for e in entries}
    assert {
        CONF_ALLOW_PLAYER_SWITCH,
        CONF_OUTPUT_SAMPLE_RATE,
        CONF_OUTPUT_BIT_DEPTH,
        CONF_DEVICE_ID,
    } == keys
