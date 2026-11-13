"""Tests for the Storytel provider's dynamic content-language config options."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant.providers.storytel import Storytel
from music_assistant.providers.storytel.constants import (
    ALL_LANGUAGES,
    CONF_LANGUAGES,
    DEFAULT_LANGUAGES,
)
from music_assistant.providers.storytel.storytel_helper import StorytelHelper


async def test_get_config_entries_offers_static_languages_before_authentication(
    provider: Storytel,
) -> None:
    """Before any account is authenticated, the static fallback language list is offered."""
    entries = await provider.get_config_entries()
    languages_entry = next(entry for entry in entries if entry.key == CONF_LANGUAGES)
    assert [option.value for option in languages_entry.options] == sorted(ALL_LANGUAGES)
    assert languages_entry.default_value == list(DEFAULT_LANGUAGES)


async def test_get_config_entries_offers_account_languages_once_authenticated(
    provider: Storytel,
) -> None:
    """Once authenticated, only the account's own available languages are offered."""
    provider._api = StorytelHelper(
        session=Mock(),
        provider_instance=provider,
        provider_id=provider.instance_id,
        provider_domain=provider.manifest.domain,
    )
    provider._api._available_languages = {"Norwegian": "nn", "English": "en"}

    entries = await provider.get_config_entries()

    languages_entry = next(entry for entry in entries if entry.key == CONF_LANGUAGES)
    assert [option.value for option in languages_entry.options] == ["English", "Norwegian"]


async def test_get_config_entries_default_value_is_account_default_not_current_selection(
    provider: Storytel,
) -> None:
    """
    The entry default mirrors the account's own checked-by-default languages.

    It must not mirror whatever is currently selected, since a stored value equal to the
    entry default is treated as unset and dropped on save (see ``ProviderConfig.to_raw``),
    which would silently discard a deliberately made custom selection on the next save.
    """
    provider._api = StorytelHelper(
        session=Mock(),
        provider_instance=provider,
        provider_id=provider.instance_id,
        provider_domain=provider.manifest.domain,
    )
    provider._api._available_languages = {"Norwegian": "nn", "English": "en", "Swedish": "sv"}
    provider._api._default_languages = {"Norwegian": "nn", "English": "en"}
    # Simulate a custom, already-persisted selection that differs from the account's defaults.
    provider._languages = {"Swedish": "sv"}

    entries = await provider.get_config_entries()

    languages_entry = next(entry for entry in entries if entry.key == CONF_LANGUAGES)
    assert languages_entry.default_value == ["Norwegian", "English"]
