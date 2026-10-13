"""Tests for the provider entry point and config entries."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant_models.enums import ProviderFeature

from music_assistant.providers.web_kiosk import setup
from music_assistant.providers.web_kiosk.constants import (
    CONF_ENABLE_SENDSPIN_BRIDGE,
    CONF_HTTP_PORT,
    CONF_KIOSK_URL,
    CONF_PLAYER_IDLE_TIMEOUT,
    CONF_SHOW_STOP_NOTIFICATION,
    CONF_URL_BUILDER_REQUIRE_AUTH,
)
from music_assistant.providers.web_kiosk.provider import WebKioskProvider


async def test_setup_returns_provider_with_remove_player(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """setup() returns a WebKioskProvider exposing REMOVE_PLAYER."""
    prov = await setup(mass_mock, manifest_mock, config_mock)

    assert isinstance(prov, WebKioskProvider)
    assert ProviderFeature.REMOVE_PLAYER in prov.supported_features


async def test_get_config_entries_exposes_expected_keys(provider: WebKioskProvider) -> None:
    """The provider config surface matches the documented keys."""
    entries = await provider.get_config_entries()

    keys = {entry.key for entry in entries}
    assert keys == {
        CONF_HTTP_PORT,
        CONF_PLAYER_IDLE_TIMEOUT,
        CONF_SHOW_STOP_NOTIFICATION,
        CONF_ENABLE_SENDSPIN_BRIDGE,
        CONF_KIOSK_URL,
        CONF_URL_BUILDER_REQUIRE_AUTH,
    }


async def test_config_url_is_canonical_kiosk_address(
    provider: WebKioskProvider, mass_mock: Mock
) -> None:
    """The copyable URL opens the minimal player on the MA host."""
    mass_mock.webserver.base_url = "http://192.168.10.10:8095"
    entries = {entry.key: entry for entry in await provider.get_config_entries()}
    assert entries[CONF_KIOSK_URL].default_value == "http://192.168.10.10:8098/kiosk"
    assert entries[CONF_URL_BUILDER_REQUIRE_AUTH].default_value is False
