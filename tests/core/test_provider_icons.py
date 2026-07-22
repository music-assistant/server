"""Tests for the in-memory provider icon store and accessor."""

import base64

from music_assistant_models.enums import ProviderIconVariant

from music_assistant.mass import MusicAssistant


async def test_get_provider_icon_by_domain(mass_minimal: MusicAssistant) -> None:
    """Test that a stored icon is returned by provider domain."""
    mass_minimal._provider_icons["dummy"] = {
        ProviderIconVariant.DEFAULT: ("image/svg+xml", b"<svg/>"),
    }
    assert mass_minimal.get_provider_icon("dummy") == ("image/svg+xml", b"<svg/>")


async def test_get_provider_icon_missing_variant(mass_minimal: MusicAssistant) -> None:
    """Test that a missing icon variant returns None."""
    mass_minimal._provider_icons["dummy"] = {
        ProviderIconVariant.DEFAULT: ("image/svg+xml", b"<svg/>"),
    }
    assert mass_minimal.get_provider_icon("dummy", ProviderIconVariant.DARK) is None


async def test_get_provider_icon_unknown_provider(mass_minimal: MusicAssistant) -> None:
    """Test that an unknown provider returns None."""
    assert mass_minimal.get_provider_icon("does_not_exist") is None


async def test_provider_icon_data_uri(mass_minimal: MusicAssistant) -> None:
    """Test that a stored icon is returned as a base64 data URI."""
    mass_minimal._provider_icons["dummy"] = {
        ProviderIconVariant.DEFAULT: ("image/svg+xml", b"<svg/>"),
    }
    expected = "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode()
    assert mass_minimal.get_provider_icon_data("dummy") == expected


async def test_provider_icon_data_uri_missing(mass_minimal: MusicAssistant) -> None:
    """Test that a missing icon variant returns None as a data URI."""
    assert mass_minimal.get_provider_icon_data("dummy", ProviderIconVariant.DARK) is None
