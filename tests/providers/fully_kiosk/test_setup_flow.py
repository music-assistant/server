"""Tests for the Fully Kiosk player setup flow."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import FlowStepType

from music_assistant.constants import CONF_PASSWORD
from music_assistant.providers.fully_kiosk.player import FullyKioskPlayer
from tests.common import MockProvider

from .conftest import KITCHEN, mock_protocol_config

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from music_assistant.providers.fully_kiosk.provider import FullyKioskProvider
    from tests.scenarios.harness import Scenario


def _make_player(protocol_id: str = "fully_kiosk_test") -> FullyKioskPlayer:
    """Build a real FullyKioskPlayer with a decoupled (mocked) provider/mass."""
    provider = MockProvider("fully_kiosk", instance_id="fully_kiosk--1")
    provider.mass.config.get_base_protocol_config.return_value = mock_protocol_config(
        "Fully Kiosk Test"
    )
    return FullyKioskPlayer(provider=provider, protocol_id=protocol_id, host="10.0.0.5")  # type: ignore[arg-type]


async def test_get_config_entries_excludes_password() -> None:
    """The password is flow-managed now and must not appear in the options config entries."""
    player = _make_player()
    entries = await player.get_config_entries()
    assert CONF_PASSWORD not in {entry.key for entry in entries}


async def test_on_config_updated_without_password_needs_setup() -> None:
    """With no password in setup_data, on_config_updated short-circuits (no network I/O)."""
    player = _make_player()
    await player.on_config_updated()
    assert player.needs_setup is True
    assert player.setup_reason == "password_required"
    assert player.fully_kiosk is None
    assert player.available is False


async def test_setup_flow_collects_and_persists_password(
    scenario: Scenario, provider: FullyKioskProvider, device_client: MagicMock
) -> None:
    """The setup flow of a new player stores the password for its device, which connects."""
    protocol = provider.get_protocol(KITCHEN)
    assert protocol is not None
    assert protocol.player is not None
    player_id = protocol.player.player_id
    assert (protocol.needs_setup, protocol.setup_reason) == (True, "password_required")

    step = await scenario.mass.config.setup_player(player_id)
    assert step.type == FlowStepType.FORM
    assert {entry.key for entry in step.entries} == {CONF_PASSWORD}
    finish_step = await scenario.mass.config.submit_setup_flow(
        step.flow_id, {CONF_PASSWORD: "secret"}
    )
    await scenario.settle()

    assert finish_step.type == FlowStepType.FINISH
    assert finish_step.result == {"player_id": player_id}
    record = scenario.mass.config.get_protocol_record(KITCHEN)
    assert record is not None
    assert scenario.mass.config.decrypt_string(record["setup_data"][CONF_PASSWORD]) == "secret"
    device_client.getDeviceInfo.assert_awaited()
    assert (protocol.needs_setup, protocol.setup_reason) == (False, None)
    assert scenario.player(player_id).available is True  # type: ignore[union-attr]
    # the config controller leaves its sweep of idle flows armed when the server stops
    if (sweep_handle := scenario.mass.config._flow_sweep_handle) is not None:
        sweep_handle.cancel()
