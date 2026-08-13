"""Unit tests for AI Radio provider config entries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from music_assistant_models.config_entries import Config

from music_assistant.providers.ai_radio.config import get_config_entries
from music_assistant.providers.ai_radio.provider import AIRadioProvider


def _make_mass(
    base_url: str, locale: str = "en_US", setup_data: dict[str, Any] | None = None
) -> Any:
    """Build a minimal mass stub exposing webserver.base_url, metadata.locale and config."""
    config = SimpleNamespace(
        get=lambda key: setup_data if key.endswith("/setup_data") else None,
        # the real config controller encrypts/decrypts setup_data strings transparently;
        # the stub's "encrypted" values are already plain, so decryption is a no-op
        decrypt_string=lambda value: value,
    )
    return cast(
        "Any",
        SimpleNamespace(
            webserver=SimpleNamespace(base_url=base_url),
            metadata=SimpleNamespace(locale=locale),
            config=config,
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


def test_setup_collected_city_surfaces_via_the_config_default() -> None:
    """A city typed into the setup flow becomes usable without a trip to settings."""
    mass = _make_mass(
        "http://localhost:8095",
        setup_data={"weather_city": "Amsterdam", "weather_country": "NL"},
    )

    entries = asyncio.run(get_config_entries(mass, "ai_radio--1"))

    city_entry = next(e for e in entries if e.key == "weather_city")
    country_entry = next(e for e in entries if e.key == "weather_country")
    assert city_entry.default_value == "Amsterdam"
    assert country_entry.default_value == "NL"
    # no stored config value yet, so a fresh parse resolves straight to the setup answer
    parsed = Config.parse(entries, {"values": {}})
    assert parsed.get_value("weather_city") == "Amsterdam"
    assert parsed.get_value("weather_country") == "NL"


def test_stored_config_value_overrides_the_setup_answer() -> None:
    """A later settings edit wins over whatever was collected at setup."""
    mass = _make_mass(
        "http://localhost:8095",
        setup_data={"weather_city": "Amsterdam", "weather_country": "NL"},
    )

    entries = asyncio.run(get_config_entries(mass, "ai_radio--1"))
    parsed = Config.parse(entries, {"values": {"weather_city": "Rotterdam"}})

    assert parsed.get_value("weather_city") == "Rotterdam"


def test_weather_country_falls_back_to_the_locale_region_without_a_setup_answer() -> None:
    """No setup answer for the country: it still defaults to the server's locale region."""
    mass = _make_mass("http://localhost:8095", locale="nl_NL", setup_data={})

    entries = asyncio.run(get_config_entries(mass, "ai_radio--1"))

    entry = next(e for e in entries if e.key == "weather_country")
    assert entry.default_value == "NL"
