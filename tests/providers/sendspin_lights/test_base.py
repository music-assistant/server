"""Tests for the shared Sendspin light bridge base."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiosendspin.models.visualizer import ClientHelloVisualizerSupport
from music_assistant_models.enums import PlayerType

from music_assistant.providers.sendspin_lights.base import SendspinLightBridge


class _Bridge(SendspinLightBridge):
    rendered = 0

    def _on_frame(self, frame: Any) -> None:
        pass

    def _render(self) -> None:
        self.rendered += 1


def _make() -> tuple[_Bridge, Mock, Mock, Mock]:
    call_later = Mock(return_value=Mock())
    client = Mock(client_id="light")
    client.roles_by_family = Mock(return_value=[])
    server = SimpleNamespace(
        register_external_player=Mock(return_value=client), remove_client=AsyncMock()
    )
    sendspin: Any = SimpleNamespace(server_api=server, register_bridge_player_type=Mock())
    provider: Any = SimpleNamespace(
        mass=SimpleNamespace(loop=SimpleNamespace(call_later=call_later)), logger=Mock()
    )
    support = ClientHelloVisualizerSupport(buffer_capacity=1, rate_max=10, types=["peak"])
    bridge = _Bridge(provider, sendspin, "light", "Light", "Acme", "Lamp", support, 10)
    return bridge, call_later, sendspin.register_bridge_player_type, server.remove_client


def test_register_client_marks_player_as_light() -> None:
    """Registering claims the client id as a LIGHT player and attaches roles."""
    bridge, _, register_type, _ = _make()
    bridge.register_client()
    register_type.assert_called_once_with("light", PlayerType.LIGHT)
    bridge._sendspin_client.attach_preinitialized_roles.assert_called_once()  # type: ignore[union-attr]


def test_render_tick_reschedules_only_while_streaming() -> None:
    """The loop keeps running while streaming and ends when it stops."""
    bridge, call_later, _, _ = _make()
    bridge._is_streaming = True
    bridge._render_tick()
    assert bridge.rendered == 1
    call_later.assert_called_once()
    bridge._is_streaming = False
    bridge._render_tick()
    assert bridge.rendered == 1
    call_later.assert_called_once()


def test_render_tick_survives_a_failing_render() -> None:
    """One failed render is logged and the loop keeps running."""
    bridge, call_later, _, _ = _make()
    bridge._is_streaming = True
    bridge._render = Mock(side_effect=OSError("send failed"))  # type: ignore[method-assign]
    bridge._render_tick()
    cast("Mock", bridge.logger.exception).assert_called_once()
    call_later.assert_called_once()


def test_subclass_must_implement_frame_and_render_hooks() -> None:
    """A subclass missing a required hook cannot be instantiated."""

    class _Incomplete(SendspinLightBridge):
        pass

    with pytest.raises(TypeError, match="abstract"):
        _Incomplete()  # type: ignore[abstract,call-arg]


async def test_unregister_removes_client() -> None:
    """Unregistering stops the loop and removes the Sendspin client."""
    bridge, _, _, remove_client = _make()
    bridge.register_client()
    bridge._start_render_loop()
    await bridge.unregister_client()
    remove_client.assert_awaited_once_with("light")
    assert bridge._render_handle is None
