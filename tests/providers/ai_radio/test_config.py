"""Unit tests for AI Radio provider config entries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from music_assistant.providers.ai_radio.config import get_config_entries
from music_assistant.providers.ai_radio.provider import AIRadioProvider


def _make_mass(base_url: str, locale: str = "en_US") -> Any:
    """Build a minimal mass stub exposing webserver.base_url and metadata.locale."""
    return cast(
        "Any",
        SimpleNamespace(
            webserver=SimpleNamespace(base_url=base_url),
            metadata=SimpleNamespace(locale=locale),
        ),
    )


def test_optional_entries_use_the_advanced_flag_not_the_advanced_category() -> None:
    """
    Advanced options must set ``advanced``, not ``category="advanced"``.

    The frontend drops the deprecated "advanced" category from its panel list, so entries
    filed under it never render on the settings page.
    """
    mass = _make_mass("http://localhost:8095")

    entries = asyncio.run(get_config_entries(mass))

    assert entries, "provider must expose config entries"
    for entry in entries:
        assert entry.category != "advanced", f"{entry.key} uses the deprecated advanced category"
    advanced = {entry.key for entry in entries if entry.advanced}
    assert advanced == {"timezone", "weather_provider", "weather_timeout_seconds"}


def test_weather_country_is_a_dropdown_defaulting_to_the_server_region() -> None:
    """The weather country is picked from a list and seeded from the server locale."""
    entries = asyncio.run(get_config_entries(_make_mass("http://localhost:8095", "nl_NL")))

    entry = next(entry for entry in entries if entry.key == "weather_country")
    assert entry.default_value == "NL"
    assert entry.options
    assert any(option.value == "NL" for option in entry.options)


def test_config_entries_include_weather_provider_and_timeout() -> None:
    """Weather provider selection and its request timeout are advanced provider options."""
    mass = _make_mass("http://localhost:8095")

    entries = asyncio.run(get_config_entries(mass))

    keys = {entry.key for entry in entries}
    assert "weather_provider" in keys
    assert "weather_timeout_seconds" in keys
    provider_entry = next(e for e in entries if e.key == "weather_provider")
    assert provider_entry.default_value == "open_meteo"
    assert provider_entry.advanced is True
    timeout_entry = next(e for e in entries if e.key == "weather_timeout_seconds")
    assert timeout_entry.default_value == 20
    assert timeout_entry.advanced is True


def test_provider_instance_exposes_the_same_config_entries() -> None:
    """
    The options page reads the instance method, not the module-level setup hook.

    Without the override the base class returns an empty tuple, so the provider's own
    options silently vanish from the settings UI.
    """
    mass = _make_mass("http://localhost:8095")
    provider = cast("Any", AIRadioProvider.__new__(AIRadioProvider))
    provider.mass = mass
    # instance_id is a read-only property backed by the provider config
    provider.config = SimpleNamespace(instance_id="ai_radio")

    entries = asyncio.run(provider.get_config_entries())

    assert {entry.key for entry in entries} == {
        entry.key for entry in asyncio.run(get_config_entries(mass, "ai_radio"))
    }
    assert "timezone" in {entry.key for entry in entries}
