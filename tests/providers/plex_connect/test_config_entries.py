"""Tests for the plex.tv link config flow of the Plex Connect provider."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import ActionUnavailable

from music_assistant.providers import plex_connect
from music_assistant.providers.plex_connect import (
    CONF_ACTION_COMPLETE_LINK,
    CONF_ACTION_START_LINK,
    CONF_ACTION_UNLINK,
    PlexConnectProvider,
)
from music_assistant.providers.plex_connect.plextv import (
    PlexPin,
    PlexTvError,
    PlexTvPinExpiredError,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


def _make_provider(token: str | None = None) -> Any:
    """Create a minimal PlexConnectProvider instance for config-flow testing."""
    mock_mass = MagicMock()
    mock_mass.version = "2.10.0"
    player = MagicMock()
    player.display_name = "Living Room"
    mock_mass.players.get_player.return_value = player

    setup_data: dict[str, Any] = {
        "mass_player_id": "player1",
        "plex_provider_id": "plexprov1",
        "plextv_token": token,
    }
    mock_mass.config.get = MagicMock(return_value=setup_data)
    mock_mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    mock_mass.config.encrypt_string = MagicMock(side_effect=lambda value: value)

    option_values: dict[str, Any] = {
        "player_name": None,
        "device_class": "speaker",
        "log_level": "INFO",
    }
    mock_config = MagicMock()
    mock_config.instance_id = "plex_connect_instance_1"
    mock_config.get_value = lambda key, default=None: option_values.get(key, default)
    mock_config.setup_data = setup_data

    mock_manifest = MagicMock()
    mock_manifest.type = "plugin"
    mock_manifest.domain = "plex_connect"

    return PlexConnectProvider(mock_mass, mock_manifest, mock_config)


def _entry(entries: tuple[ConfigEntry, ...], key: str) -> ConfigEntry | None:
    """Return the config entry with the given key, if present."""
    return next((entry for entry in entries if entry.key == key), None)


@pytest.fixture
def plextv_client() -> Generator[MagicMock]:
    """Patch the PlexTvClient used by the provider and return its instance mock."""
    client = MagicMock()
    client.create_pin = AsyncMock(return_value=PlexPin(id=12345, code="ABCD"))
    client.check_pin = AsyncMock(return_value="devtoken")
    with patch.object(plex_connect, "PlexTvClient", return_value=client):
        yield client


async def test_no_token_shows_link_action(plextv_client: MagicMock) -> None:
    """Without a token the intro label and start action are shown, alongside options."""
    provider = _make_provider(token=None)

    entries = await provider.get_config_entries()

    assert _entry(entries, "player_name")  # base option entries still present
    assert _entry(entries, "plextv_link_intro")
    assert _entry(entries, CONF_ACTION_START_LINK)
    assert _entry(entries, CONF_ACTION_UNLINK) is None
    assert _entry(entries, CONF_ACTION_COMPLETE_LINK) is None
    plextv_client.create_pin.assert_not_called()


async def test_saved_token_shows_linked_state(plextv_client: MagicMock) -> None:
    """A stored token renders the linked state without any API call."""
    provider = _make_provider(token="devtoken")

    entries = await provider.get_config_entries()

    assert _entry(entries, "plextv_linked")
    assert _entry(entries, CONF_ACTION_UNLINK)
    assert _entry(entries, CONF_ACTION_START_LINK) is None
    plextv_client.create_pin.assert_not_called()


async def test_start_link_creates_pin_and_shows_code(plextv_client: MagicMock) -> None:
    """The start action requests a PIN and shows the code plus a complete button."""
    provider = _make_provider(token=None)

    entries = await provider.handle_config_action(CONF_ACTION_START_LINK)

    plextv_client.create_pin.assert_awaited_once()
    assert provider._plextv_pin == PlexPin(id=12345, code="ABCD")
    code_label = _entry(entries, "plextv_link_code")
    assert code_label is not None
    assert code_label.translation_params == ["ABCD"]
    assert _entry(entries, CONF_ACTION_COMPLETE_LINK)
    start_action = _entry(entries, CONF_ACTION_START_LINK)
    assert start_action is not None
    assert start_action.translation_key == "start_link_new"


async def test_start_link_unreachable_shows_status(plextv_client: MagicMock) -> None:
    """A plex.tv error while requesting a PIN surfaces a status label, no pin held."""
    plextv_client.create_pin = AsyncMock(side_effect=PlexTvError("boom"))
    provider = _make_provider(token=None)

    entries = await provider.handle_config_action(CONF_ACTION_START_LINK)

    assert provider._plextv_pin is None
    status = _entry(entries, "plextv_link_status")
    assert status is not None
    assert status.translation_key == "plextv_status_unreachable"


async def test_complete_link_without_pin_shows_status(plextv_client: MagicMock) -> None:
    """Completing without a pending PIN asks the user to request a code first."""
    provider = _make_provider(token=None)

    entries = await provider.handle_config_action(CONF_ACTION_COMPLETE_LINK)

    status = _entry(entries, "plextv_link_status")
    assert status is not None
    assert status.translation_key == "plextv_status_no_pin"
    plextv_client.check_pin.assert_not_called()


async def test_complete_link_stores_token_and_registers(plextv_client: MagicMock) -> None:
    """A confirmed PIN persists the token, clears the pin and kicks off registration."""
    provider = _make_provider(token=None)
    provider._plextv_pin = PlexPin(id=12345, code="ABCD")

    entries = await provider.handle_config_action(CONF_ACTION_COMPLETE_LINK)

    plextv_client.check_pin.assert_awaited()
    assert provider._plextv_pin is None
    assert provider.config.setup_data["plextv_token"] == "devtoken"
    provider.mass.create_task.assert_called_once()
    assert _entry(entries, "plextv_linked")
    assert _entry(entries, CONF_ACTION_UNLINK)


async def test_complete_link_pending_keeps_pin(
    plextv_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfirmed PIN keeps the pin state so the user can try again."""
    monkeypatch.setattr(plex_connect, "PIN_CHECK_INTERVAL", 0)
    plextv_client.check_pin = AsyncMock(return_value=None)
    provider = _make_provider(token=None)
    provider._plextv_pin = PlexPin(id=12345, code="ABCD")

    entries = await provider.handle_config_action(CONF_ACTION_COMPLETE_LINK)

    assert provider._plextv_pin == PlexPin(id=12345, code="ABCD")
    assert _entry(entries, "plextv_link_code")
    status = _entry(entries, "plextv_link_status")
    assert status is not None
    assert status.translation_key == "plextv_status_not_confirmed"


async def test_complete_link_expired_resets(plextv_client: MagicMock) -> None:
    """An expired PIN clears the pin state and returns to the start action."""
    plextv_client.check_pin = AsyncMock(side_effect=PlexTvPinExpiredError("expired"))
    provider = _make_provider(token=None)
    provider._plextv_pin = PlexPin(id=12345, code="ABCD")

    entries = await provider.handle_config_action(CONF_ACTION_COMPLETE_LINK)

    assert provider._plextv_pin is None
    assert _entry(entries, CONF_ACTION_START_LINK)
    status = _entry(entries, "plextv_link_status")
    assert status is not None
    assert status.translation_key == "plextv_status_expired"


async def test_unlink_clears_token() -> None:
    """The unlink action clears the stored token and returns to the intro state."""
    provider = _make_provider(token="devtoken")

    entries = await provider.handle_config_action(CONF_ACTION_UNLINK)

    assert provider.config.setup_data["plextv_token"] is None
    assert _entry(entries, "plextv_link_intro")
    assert _entry(entries, CONF_ACTION_START_LINK)


async def test_unknown_action_raises() -> None:
    """An unknown action id raises ActionUnavailable per the base contract."""
    provider = _make_provider(token=None)

    with pytest.raises(ActionUnavailable):
        await provider.handle_config_action("bogus")
