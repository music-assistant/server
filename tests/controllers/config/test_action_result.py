"""
Tests for the ``ConfigActionResult`` return contract of ``ConfigEntryType.ACTION`` handlers.

A handler returning a result reports the outcome of its one-shot side effect: ``config/*/
invoke_action`` must hand that result straight to the caller instead of rendering entries,
and stamp the owning namespace its ``translation_key`` resolves under.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.config_entries import ConfigActionResult

from music_assistant.constants import CONF_PROTOCOL_KEY_SPLITTER

from .helpers import build_bare_config_controller


async def test_provider_action_result_is_returned_with_the_provider_owner() -> None:
    """A provider action result is passed through, stamped with the provider's namespace."""
    mock_mass = MagicMock()
    provider = MagicMock()
    provider.domain = "some_provider"
    action_result = ConfigActionResult(translation_key="some_action.result")
    provider.handle_config_action = AsyncMock(return_value=action_result)
    mock_mass.get_provider.return_value = provider
    controller = build_bare_config_controller(mock_mass)

    result = await controller.invoke_provider_config_action("some_instance", "some_action")

    assert result is action_result
    assert action_result.translation_owner == "provider.some_provider"


async def test_provider_action_result_keeps_a_preset_owner() -> None:
    """A result that already declares its owner keeps it."""
    mock_mass = MagicMock()
    provider = MagicMock()
    provider.domain = "some_provider"
    action_result = ConfigActionResult(
        translation_key="some_action.result", translation_owner="common"
    )
    provider.handle_config_action = AsyncMock(return_value=action_result)
    mock_mass.get_provider.return_value = provider
    controller = build_bare_config_controller(mock_mass)

    result = await controller.invoke_provider_config_action("some_instance", "some_action")

    assert result is action_result
    assert action_result.translation_owner == "common"


async def test_core_action_result_is_returned_with_the_core_owner() -> None:
    """A core-module action result is passed through, stamped with the module's namespace."""
    mock_mass = MagicMock()
    module = MagicMock()
    action_result = ConfigActionResult(translation_key="some_action.result")
    module.handle_config_action = AsyncMock(return_value=action_result)
    mock_mass.some_module = module
    controller = build_bare_config_controller(mock_mass)

    result = await controller.invoke_core_config_action("some_module", "some_action")

    assert result is action_result
    assert action_result.translation_owner == "core.some_module"


async def test_core_action_result_keeps_a_preset_owner() -> None:
    """A core-module result that already declares its owner keeps it."""
    mock_mass = MagicMock()
    module = MagicMock()
    action_result = ConfigActionResult(
        translation_key="some_action.result", translation_owner="common"
    )
    module.handle_config_action = AsyncMock(return_value=action_result)
    mock_mass.some_module = module
    controller = build_bare_config_controller(mock_mass)

    result = await controller.invoke_core_config_action("some_module", "some_action")

    assert result is action_result
    assert action_result.translation_owner == "common"


async def test_player_action_result_is_returned_without_rerender() -> None:
    """A player action result is passed through and skips the full-entries re-render."""
    mock_mass = MagicMock()
    player = MagicMock()
    player.provider = "some_player_provider"
    action_result = ConfigActionResult(translation_key="some_action.result")
    player.handle_config_action = AsyncMock(return_value=action_result)
    mock_mass.players.get_player.return_value = player
    controller = build_bare_config_controller(mock_mass)

    with patch.object(controller, "get_player_config_entries", AsyncMock()) as mock_rerender:
        result = await controller.invoke_player_config_action("player_1", "some_action")

    assert result is action_result
    assert action_result.translation_owner == "provider.some_player_provider"
    mock_rerender.assert_not_awaited()


async def test_player_action_result_keeps_a_preset_owner() -> None:
    """A player result that already declares its owner keeps it."""
    mock_mass = MagicMock()
    player = MagicMock()
    player.provider = "some_player_provider"
    action_result = ConfigActionResult(
        translation_key="some_action.result", translation_owner="common"
    )
    player.handle_config_action = AsyncMock(return_value=action_result)
    mock_mass.players.get_player.return_value = player
    controller = build_bare_config_controller(mock_mass)

    with patch.object(controller, "get_player_config_entries", AsyncMock()):
        await controller.invoke_player_config_action("player_1", "some_action")

    assert action_result.translation_owner == "common"


async def test_protocol_action_result_is_returned_without_rerendering_the_parent() -> None:
    """A protocol-routed result is passed through, owned by the protocol player's provider."""
    mock_mass = MagicMock()
    parent = MagicMock()
    parent.provider = "host_provider"
    parent.handle_config_action = AsyncMock()
    protocol_player = MagicMock()
    protocol_player.provider = "protocol_provider"
    action_result = ConfigActionResult(translation_key="some_action.result")
    protocol_player.handle_config_action = AsyncMock(return_value=action_result)
    mock_mass.players.get_player.side_effect = lambda player_id, *_args: {
        "player_1": parent,
        "protocol_1": protocol_player,
    }[player_id]
    controller = build_bare_config_controller(mock_mass)
    action = f"protocol_1{CONF_PROTOCOL_KEY_SPLITTER}some_action"

    with patch.object(controller, "get_player_config_entries", AsyncMock()) as mock_rerender:
        result = await controller.invoke_player_config_action("player_1", action)

    assert result is action_result
    assert action_result.translation_owner == "provider.protocol_provider"
    protocol_player.handle_config_action.assert_awaited_once_with("some_action")
    parent.handle_config_action.assert_not_awaited()
    mock_rerender.assert_not_awaited()
