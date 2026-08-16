"""Tests for Soundcloud provider setup and configuration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.soundcloud import (
    CONF_AUTHORIZATION,
    CONF_CLIENT_ID,
    SoundcloudMusicProvider,
    get_config_entries,
    setup,
)


async def test_setup_requires_credentials() -> None:
    """Setting up without credentials fails with a login error."""
    config = MagicMock()
    config.get_value.return_value = None

    with pytest.raises(LoginFailed):
        await setup(AsyncMock(), MagicMock(), config)


async def test_setup_returns_provider() -> None:
    """With credentials present a provider instance is returned."""
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    manifest = MagicMock()
    manifest.domain = "soundcloud"

    provider = await setup(AsyncMock(), manifest, config)

    assert isinstance(provider, SoundcloudMusicProvider)


async def test_config_entries_are_secure_and_required() -> None:
    """Both credentials are stored as secure strings and are mandatory."""
    entries = {entry.key: entry for entry in await get_config_entries(AsyncMock())}

    for key in (CONF_CLIENT_ID, CONF_AUTHORIZATION):
        assert entries[key].type == ConfigEntryType.SECURE_STRING
        assert entries[key].required is True


async def test_async_init_stores_user_id(provider: SoundcloudMusicProvider) -> None:
    """Initialization logs in and remembers the account id."""
    api = AsyncMock()
    api.get_account_details.return_value = {"id": 42}

    with patch(
        "music_assistant.providers.soundcloud.SoundcloudAsyncAPI", return_value=api
    ) as api_cls:
        await provider.handle_async_init()

    api_cls.assert_called_once()
    api.login.assert_awaited_once()
    # the API returns a number, which is stored as the string it is used as
    assert provider._user_id == "42"
