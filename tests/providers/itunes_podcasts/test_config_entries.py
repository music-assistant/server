"""Test the iTunes Podcasts config entries."""

from unittest.mock import Mock

import pytest

from music_assistant.providers.itunes_podcasts import (
    CONF_LOCALE,
    DEFAULT_LOCALE,
    SUPPORTED_FEATURES,
    ITunesPodcastsProvider,
)


def _provider(server_locale: str) -> ITunesPodcastsProvider:
    """Return a provider whose server is configured with the given metadata locale."""
    mass = Mock()
    mass.metadata.locale = server_locale
    manifest = Mock(domain="itunes_podcasts")
    config = Mock(instance_id="itunes_podcasts", enabled=True)
    config.get_value.side_effect = lambda key, default=None: (
        "INFO" if key == "log_level" else default
    )
    return ITunesPodcastsProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def _locale_entry_default(server_locale: str) -> str:
    """Return the default value of the locale entry for the given server locale."""
    entries = await _provider(server_locale).get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_LOCALE)
    assert isinstance(entry.default_value, str)
    return entry.default_value


@pytest.mark.parametrize(
    ("server_locale", "expected"),
    [("nl_NL", "nl"), ("en_GB", "gb"), ("de_DE", "de")],
)
async def test_locale_defaults_to_server_region(server_locale: str, expected: str) -> None:
    """The store to search defaults to the region of the server's configured language."""
    assert await _locale_entry_default(server_locale) == expected


@pytest.mark.parametrize("server_locale", ["en", "en_XX"])
async def test_locale_falls_back_when_region_has_no_storefront(server_locale: str) -> None:
    """A language without a matching iTunes storefront falls back to the default store."""
    assert await _locale_entry_default(server_locale) == DEFAULT_LOCALE


async def test_locale_default_is_a_selectable_option() -> None:
    """The resolved default is one of the offered options, so the picker shows it selected."""
    entries = await _provider("nl_NL").get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_LOCALE)
    assert entry.default_value in [option.value for option in entry.options]
