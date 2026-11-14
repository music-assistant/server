"""Tests for the Storytel provider's initial-setup content-language defaults."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.storytel import Storytel
from music_assistant.providers.storytel.constants import CONF_LANGUAGES, DEFAULT_LANGUAGES
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


def _make_helper_with_account(
    available_languages: dict[str, str], default_languages: dict[str, str]
) -> Any:
    """Return a StorytelHelper factory whose login reports the given account languages."""

    def _factory(*args: Any, **kwargs: Any) -> StorytelHelper:
        helper = StorytelHelper(*args, **kwargs)

        async def _login(_username: str, _password: str) -> StorytelAuth:
            helper._auth = StorytelAuth(jwt="jwt-token", single_sign_token="sso-token")
            helper._available_languages = available_languages
            helper._default_languages = default_languages
            return helper._auth

        helper.login = _login  # type: ignore[method-assign,assignment]
        helper.fetch_resource_version = AsyncMock(return_value=None)  # type: ignore[method-assign]
        return helper

    return _factory


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


async def test_stale_language_selection_falls_back_to_account_default_languages(
    provider: Storytel,
) -> None:
    """
    A persisted selection no longer available to the account falls back to its own default.

    The fallback should be the account's own checked-by-default languages, not the static
    (possibly unavailable) default.
    """
    cast("MagicMock", provider.mass.config.get).return_value = {
        "username": "user",
        "password": "pass",
    }
    cast("MagicMock", provider.mass.config.decrypt_string).side_effect = lambda value: value
    # "Danish" was persisted previously but this account no longer has access to it.
    cast("MagicMock", provider.mass.config.get_raw_provider_config_value).return_value = ["Danish"]

    helper_factory = _make_helper_with_account(
        available_languages={"Norwegian": "nn", "English": "en"},
        default_languages={"Norwegian": "nn"},
    )
    with patch("music_assistant.providers.storytel.StorytelHelper", side_effect=helper_factory):
        await provider.handle_async_init()

    # The account's own default is "Norwegian", not the static default ("English").
    assert provider._languages == {"Norwegian": "nn"}
    cast("MagicMock", provider.mass.config.set_raw_provider_config_value).assert_called_once_with(
        "storytel--test", CONF_LANGUAGES, ["Norwegian"], encrypted=False, immediate=True
    )


async def test_stale_language_selection_falls_back_to_any_available_language(
    provider: Storytel,
) -> None:
    """With no account default-checked languages either, any available language is used."""
    cast("MagicMock", provider.mass.config.get).return_value = {
        "username": "user",
        "password": "pass",
    }
    cast("MagicMock", provider.mass.config.decrypt_string).side_effect = lambda value: value
    cast("MagicMock", provider.mass.config.get_raw_provider_config_value).return_value = ["Danish"]

    helper_factory = _make_helper_with_account(
        available_languages={"Swedish": "sv"}, default_languages={}
    )
    with patch("music_assistant.providers.storytel.StorytelHelper", side_effect=helper_factory):
        await provider.handle_async_init()

    assert provider._languages == {"Swedish": "sv"}
    cast("MagicMock", provider.mass.config.set_raw_provider_config_value).assert_called_once_with(
        "storytel--test", CONF_LANGUAGES, ["Swedish"], encrypted=False, immediate=True
    )


async def test_missing_account_language_data_falls_back_to_static_default(
    provider: Storytel,
) -> None:
    """Only when the account returns no language list at all is the static default used."""
    cast("MagicMock", provider.mass.config.get).return_value = {
        "username": "user",
        "password": "pass",
    }
    cast("MagicMock", provider.mass.config.decrypt_string).side_effect = lambda value: value
    cast("MagicMock", provider.mass.config.get_raw_provider_config_value).return_value = None

    helper_factory = _make_helper_with_account(available_languages={}, default_languages={})
    with patch("music_assistant.providers.storytel.StorytelHelper", side_effect=helper_factory):
        await provider.handle_async_init()

    assert provider._languages == DEFAULT_LANGUAGES


async def test_partially_stale_language_selection_persists_the_normalized_set(
    provider: Storytel,
) -> None:
    """
    A selection with one valid and one no-longer-available language is normalized on save.

    Only filtering in memory (without persisting) would leave the UI showing the stale,
    unavailable language even though requests only use the still-valid one.
    """
    cast("MagicMock", provider.mass.config.get).return_value = {
        "username": "user",
        "password": "pass",
    }
    cast("MagicMock", provider.mass.config.decrypt_string).side_effect = lambda value: value
    # "Swedish" is still available, but "Danish" is no longer accessible to this account.
    cast("MagicMock", provider.mass.config.get_raw_provider_config_value).return_value = [
        "Swedish",
        "Danish",
    ]

    helper_factory = _make_helper_with_account(
        available_languages={"Swedish": "sv", "English": "en"},
        default_languages={"English": "en"},
    )
    with patch("music_assistant.providers.storytel.StorytelHelper", side_effect=helper_factory):
        await provider.handle_async_init()

    assert provider._languages == {"Swedish": "sv"}
    cast("MagicMock", provider.mass.config.set_raw_provider_config_value).assert_called_once_with(
        "storytel--test", CONF_LANGUAGES, ["Swedish"], encrypted=False, immediate=True
    )
