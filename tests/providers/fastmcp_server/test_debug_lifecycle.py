"""Lifecycle tests for the provider-owned debug event subscription."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.commands import ProviderCommandSet
from music_assistant.providers.fastmcp_server.constants import CONF_DEFAULT_POLICY
from music_assistant.providers.fastmcp_server.policy import (
    PolicyProfile,
    PolicySnapshot,
    policy_snapshot,
)
from music_assistant.providers.fastmcp_server.policy_config import policy_mode_key


def _policy(_bearer: str | None) -> PolicySnapshot:
    """Return a complete policy for lifecycle-only command tests."""
    return policy_snapshot(PolicyProfile.TRUSTED)


def test_command_set_subscribes_when_debug_events_enabled(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """When v2 debug:events is allowed, the provider command set starts the buffer."""
    mock_config.get_value.side_effect = lambda key, default=None: {
        CONF_DEFAULT_POLICY: "Custom",
        policy_mode_key(Capability.DEBUG_EVENTS): "allow",
        "debug_event_buffer_capacity": 100,
    }.get(key, default if default is not None else False)

    commands = ProviderCommandSet(mock_mass, mock_config, policy_provider=_policy)
    commands.start()
    assert mock_mass.subscribe.called
    commands.stop()


def test_command_set_does_not_subscribe_when_events_disabled(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """With missing v2 policy, no event subscription is created and stop is safe."""
    mock_config.get_value.return_value = False
    commands = ProviderCommandSet(mock_mass, mock_config, policy_provider=_policy)
    commands.start()
    commands.stop()  # must not raise
    assert mock_mass.subscribe.called is False


def test_event_buffer_stop_is_idempotent_during_command_unload(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """Double-stop must not raise."""
    mock_config.get_value.side_effect = lambda key, default=None: {
        CONF_DEFAULT_POLICY: "Custom",
        policy_mode_key(Capability.DEBUG_EVENTS): "allow",
        "debug_event_buffer_capacity": 100,
    }.get(key, default if default is not None else False)

    commands = ProviderCommandSet(mock_mass, mock_config, policy_provider=_policy)
    commands.start()
    commands.stop()
    commands.stop()  # must not raise — second call is a no-op
