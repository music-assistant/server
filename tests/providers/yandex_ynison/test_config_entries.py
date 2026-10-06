"""Tests for Ynison runtime configuration entries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.config_entries import ProviderConfig
from music_assistant_models.enums import ProviderType

from music_assistant.providers.yandex_ynison.config_helpers import list_yandex_music_instances
from music_assistant.providers.yandex_ynison.constants import (
    CONF_ALLOW_PLAYER_SWITCH,
    CONF_DEVICE_ID,
    CONF_OUTPUT_BIT_DEPTH,
    CONF_OUTPUT_SAMPLE_RATE,
)
from music_assistant.providers.yandex_ynison.provider import YandexYnisonProvider


async def test_runtime_config_contains_playback_options_only() -> None:
    """Reintroducing account or identity fields must not bypass the setup flow."""
    provider = object.__new__(YandexYnisonProvider)

    entries = await provider.get_config_entries()

    assert [entry.key for entry in entries] == [
        CONF_ALLOW_PLAYER_SWITCH,
        CONF_OUTPUT_SAMPLE_RATE,
        CONF_OUTPUT_BIT_DEPTH,
        CONF_DEVICE_ID,
    ]


async def test_device_id_is_the_only_hidden_runtime_entry() -> None:
    """Exposing device identity must not let users accidentally replace it."""
    provider = object.__new__(YandexYnisonProvider)

    entries = await provider.get_config_entries()
    hidden = [entry.key for entry in entries if entry.hidden]

    assert hidden == [CONF_DEVICE_ID]


async def test_account_selector_uses_public_provider_config_api() -> None:
    """Disabled accounts are filtered without reading the raw provider storage."""
    mass = MagicMock()
    mass.config.get.side_effect = AssertionError("raw provider storage was read")
    mass.config.get_provider_configs = AsyncMock(
        return_value=[
            ProviderConfig(
                type=ProviderType.MUSIC,
                domain="yandex_music",
                instance_id="ym-disabled",
                name="Disabled",
                enabled=False,
                values={},
            ),
            ProviderConfig(
                type=ProviderType.MUSIC,
                domain="yandex_music",
                instance_id="ym-enabled",
                name="Enabled",
                enabled=True,
                values={},
            ),
        ]
    )
    assert await list_yandex_music_instances(mass) == [("ym-enabled", "Enabled")]
    mass.config.get_provider_configs.assert_awaited_once_with(provider_domain="yandex_music")
