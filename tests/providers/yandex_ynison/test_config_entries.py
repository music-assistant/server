"""Tests for Ynison runtime configuration entries."""

from __future__ import annotations

from music_assistant.providers.yandex_ynison.constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
    CONF_STREAM_MODE,
    STREAM_MODE_STABLE,
)
from music_assistant.providers.yandex_ynison.provider import YandexYnisonProvider


async def test_runtime_config_contains_playback_options_only() -> None:
    """Reintroducing account or identity fields must not bypass the setup flow."""
    provider = object.__new__(YandexYnisonProvider)

    entries = await provider.get_config_entries()

    assert [entry.key for entry in entries] == [
        CONF_ALLOW_PLAYER_SWITCH,
        CONF_STREAM_MODE,
        CONF_OUTPUT_SAMPLE_RATE,
        CONF_OUTPUT_BIT_DEPTH,
        CONF_DEVICE_ID,
    ]

    stream_mode = next(entry for entry in entries if entry.key == CONF_STREAM_MODE)
    assert stream_mode.default_value == STREAM_MODE_STABLE


async def test_device_id_is_the_only_hidden_runtime_entry() -> None:
    """Exposing device identity must not let users accidentally replace it."""
    provider = object.__new__(YandexYnisonProvider)

    entries = await provider.get_config_entries()
    hidden = [entry.key for entry in entries if entry.hidden]

    assert hidden == [CONF_DEVICE_ID]
