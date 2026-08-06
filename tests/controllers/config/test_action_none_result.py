"""
Tests for the ``None``-return contract of ``ConfigEntryType.ACTION`` handlers.

An action handler returning ``None`` means the one-shot side effect ran with nothing
to re-render: ``config/*/invoke_action`` must surface that as an empty list, distinct
from the default-wrapped entries a non-empty result gets, so the frontend can tell
"ran fine, nothing changed" apart from "here is the refreshed form".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PROTOCOL_KEY_SPLITTER
from music_assistant.controllers.config.controller import ConfigController


def _controller(mock_mass: MagicMock) -> ConfigController:
    """
    Build a bare ConfigController wired to the given mocked mass.

    Mock setup must go through the returned ``mock_mass`` (not ``controller.mass``):
    ``ConfigController.mass`` is statically typed as ``MusicAssistant``, so reading
    it back would resolve real (non-mock) attribute/overload types instead of the
    mock's, even though the object assigned at runtime is the mock.
    """
    controller = ConfigController.__new__(ConfigController)
    controller.mass = mock_mass
    return controller


async def test_provider_action_returning_none_yields_empty_list() -> None:
    """A provider action returning None surfaces as [], not the default-wrapped entries."""
    mock_mass = MagicMock()
    provider = MagicMock()
    provider.domain = "some_provider"
    provider.handle_config_action = AsyncMock(return_value=None)
    mock_mass.get_provider.return_value = provider
    controller = _controller(mock_mass)

    result = await controller.invoke_provider_config_action("some_instance", "some_action")

    assert result == []


async def test_provider_action_returning_entries_still_wraps_them() -> None:
    """A provider action that still returns entries keeps the default-wrapped rendering."""
    mock_mass = MagicMock()
    provider = MagicMock()
    provider.domain = "some_provider"
    own_entry = ConfigEntry(key="foo", type=ConfigEntryType.LABEL)
    provider.handle_config_action = AsyncMock(return_value=(own_entry,))
    mock_mass.get_provider.return_value = provider
    controller = _controller(mock_mass)

    result = await controller.invoke_provider_config_action("some_instance", "some_action")

    assert any(entry.key == "foo" for entry in result)
    # the server default entries are still appended around the provider's own entry
    assert len(result) > 1


async def test_core_action_returning_none_yields_empty_list() -> None:
    """A core-module action returning None surfaces as [], not the default-wrapped entries."""
    mock_mass = MagicMock()
    module = MagicMock()
    module.handle_config_action = AsyncMock(return_value=None)
    mock_mass.some_module = module
    controller = _controller(mock_mass)

    result = await controller.invoke_core_config_action("some_module", "some_action")

    assert result == []


async def test_core_action_returning_entries_still_wraps_them() -> None:
    """A core-module action that still returns entries keeps the default-wrapped rendering."""
    mock_mass = MagicMock()
    module = MagicMock()
    own_entry = ConfigEntry(key="foo", type=ConfigEntryType.LABEL)
    module.handle_config_action = AsyncMock(return_value=(own_entry,))
    mock_mass.some_module = module
    controller = _controller(mock_mass)

    result = await controller.invoke_core_config_action("some_module", "some_action")

    assert any(entry.key == "foo" for entry in result)
    assert len(result) > 1


async def test_player_action_returning_none_yields_empty_list_without_rerender() -> None:
    """A player action returning None surfaces as [] and skips the full-entries re-render."""
    mock_mass = MagicMock()
    player = MagicMock()
    player.handle_config_action = AsyncMock(return_value=None)
    mock_mass.players.get_player.return_value = player
    controller = _controller(mock_mass)

    with patch.object(controller, "get_player_config_entries", AsyncMock()) as mock_rerender:
        result = await controller.invoke_player_config_action("player_1", "some_action")

    assert result == []
    mock_rerender.assert_not_awaited()


async def test_player_action_returning_entries_still_rerenders() -> None:
    """A player action that still returns entries keeps the existing full re-render."""
    mock_mass = MagicMock()
    player = MagicMock()
    player.handle_config_action = AsyncMock(
        return_value=[ConfigEntry(key="foo", type=ConfigEntryType.LABEL)]
    )
    mock_mass.players.get_player.return_value = player
    controller = _controller(mock_mass)
    rerendered = [ConfigEntry(key="bar", type=ConfigEntryType.LABEL)]

    with patch.object(
        controller, "get_player_config_entries", AsyncMock(return_value=rerendered)
    ) as mock_rerender:
        result = await controller.invoke_player_config_action("player_1", "some_action")

    assert result == rerendered
    mock_rerender.assert_awaited_once_with("player_1")


async def test_protocol_action_returning_none_yields_empty_list_without_rerender() -> None:
    """A protocol-prefixed action returning None surfaces as [] without re-rendering the parent."""
    mock_mass = MagicMock()
    parent = MagicMock()
    parent.handle_config_action = AsyncMock()
    protocol_player = MagicMock()
    protocol_player.handle_config_action = AsyncMock(return_value=None)
    mock_mass.players.get_player.side_effect = lambda player_id, *_args: {
        "player_1": parent,
        "protocol_1": protocol_player,
    }[player_id]
    controller = _controller(mock_mass)
    action = f"protocol_1{CONF_PROTOCOL_KEY_SPLITTER}some_action"

    with patch.object(controller, "get_player_config_entries", AsyncMock()) as mock_rerender:
        result = await controller.invoke_player_config_action("player_1", action)

    assert result == []
    protocol_player.handle_config_action.assert_awaited_once_with("some_action")
    parent.handle_config_action.assert_not_awaited()
    mock_rerender.assert_not_awaited()


async def test_protocol_action_returning_entries_rerenders_the_parent() -> None:
    """A protocol-prefixed action returning entries re-renders the parent, not the target."""
    mock_mass = MagicMock()
    parent = MagicMock()
    parent.handle_config_action = AsyncMock()
    protocol_player = MagicMock()
    protocol_player.handle_config_action = AsyncMock(
        return_value=[ConfigEntry(key="foo", type=ConfigEntryType.LABEL)]
    )
    mock_mass.players.get_player.side_effect = lambda player_id, *_args: {
        "player_1": parent,
        "protocol_1": protocol_player,
    }[player_id]
    controller = _controller(mock_mass)
    rerendered = [ConfigEntry(key="bar", type=ConfigEntryType.LABEL)]
    action = f"protocol_1{CONF_PROTOCOL_KEY_SPLITTER}some_action"

    with patch.object(
        controller, "get_player_config_entries", AsyncMock(return_value=rerendered)
    ) as mock_rerender:
        result = await controller.invoke_player_config_action("player_1", action)

    assert result == rerendered
    mock_rerender.assert_awaited_once_with("player_1")
