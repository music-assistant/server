"""Tests for Soundcloud provider setup and configuration."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.providers.soundcloud import (
    SUPPORTED_FEATURES,
    SoundcloudMusicProvider,
    setup,
)


def _setup_values(provider: SoundcloudMusicProvider, **values: str) -> None:
    """Make the setup flow answer with the given credentials."""
    provider.get_setup_value = Mock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: values.get(key, default)
    )


async def test_setup_returns_provider() -> None:
    """Setting up returns a provider instance."""
    manifest = Mock()
    manifest.domain = "soundcloud"
    config = Mock()
    config.get_value.return_value = "GLOBAL"

    provider = await setup(Mock(), manifest, config)

    assert isinstance(provider, SoundcloudMusicProvider)
    assert provider.supported_features == SUPPORTED_FEATURES


async def test_config_entries(provider: SoundcloudMusicProvider) -> None:
    """The provider warns that it is unofficial."""
    assert await provider.get_config_entries() == (CONF_ENTRY_UNOFFICIAL_PROVIDER,)


async def test_async_init_requires_credentials(provider: SoundcloudMusicProvider) -> None:
    """Initialization without credentials fails with a login error."""
    _setup_values(provider)

    with pytest.raises(LoginFailed):
        await provider.handle_async_init()


async def test_async_init_stores_user_id(provider: SoundcloudMusicProvider) -> None:
    """Initialization logs in and remembers the account id."""
    _setup_values(provider, client_id="test-client-id", authorization="OAuth test-token")
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
