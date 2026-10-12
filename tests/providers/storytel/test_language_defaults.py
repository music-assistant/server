"""Tests for the Storytel provider's initial-setup content-language defaults."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.storytel import Storytel
from music_assistant.providers.storytel.constants import CONF_LANGUAGES
from music_assistant.providers.storytel.storytel_helper import StorytelAuth, StorytelHelper


def _make_helper(*args: Any, **kwargs: Any) -> StorytelHelper:
    """Build a real StorytelHelper whose login simulates a successful account authentication."""
    helper = StorytelHelper(*args, **kwargs)

    async def _login(_username: str, _password: str) -> StorytelAuth:
        helper._auth = StorytelAuth(jwt="jwt-token", single_sign_token="sso-token")
        helper._available_languages = {"Norwegian": "nn", "English": "en", "Swedish": "sv"}
        helper._default_languages = {"Norwegian": "nn", "English": "en"}
        return helper._auth

    helper.login = _login  # type: ignore[method-assign,assignment]
    helper.fetch_resource_version = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return helper


async def test_initial_setup_adopts_and_persists_account_default_languages(
    provider: Storytel,
) -> None:
    """With no persisted selection, the account's own checked-by-default languages are used."""
    cast("MagicMock", provider.mass.config.get).return_value = {
        "username": "user",
        "password": "pass",
    }
    cast("MagicMock", provider.mass.config.decrypt_string).side_effect = lambda value: value
    # No value has ever been persisted for CONF_LANGUAGES (truly unconfigured, as opposed to
    # the config entry's own display-only default_value).
    cast("MagicMock", provider.mass.config.get_raw_provider_config_value).return_value = None

    with patch("music_assistant.providers.storytel.StorytelHelper", side_effect=_make_helper):
        await provider.handle_async_init()

    assert provider._languages == {"Norwegian": "nn", "English": "en"}
    cast("MagicMock", provider.mass.config.set_raw_provider_config_value).assert_called_once_with(
        "storytel--test", CONF_LANGUAGES, ["English", "Norwegian"], encrypted=False, immediate=True
    )


async def test_existing_language_selection_is_not_overwritten_by_account_defaults(
    provider: Storytel,
) -> None:
    """An already-persisted language selection is kept, ignoring the account's own defaults."""
    cast("MagicMock", provider.mass.config.get).return_value = {
        "username": "user",
        "password": "pass",
    }
    cast("MagicMock", provider.mass.config.decrypt_string).side_effect = lambda value: value
    cast("MagicMock", provider.mass.config.get_raw_provider_config_value).return_value = ["Swedish"]

    with patch("music_assistant.providers.storytel.StorytelHelper", side_effect=_make_helper):
        await provider.handle_async_init()

    assert provider._languages == {"Swedish": "sv"}
    cast("MagicMock", provider.mass.config.set_raw_provider_config_value).assert_not_called()
