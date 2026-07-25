"""Tests for the Fully Kiosk player setup flow."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.enums import FlowStepType

from music_assistant.constants import CONF_PASSWORD, CONF_PLAYERS
from music_assistant.mass import MusicAssistant
from music_assistant.providers.fully_kiosk.player import FullyKioskPlayer
from tests.common import MockProvider, create_mock_config


@pytest.fixture
async def flow_mass(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicAssistant]:
    """
    Provide a minimal server suitable for driving player setup flows through the engine.

    Builds on mass_minimal (config controller only) and stubs the players controller
    surface the flow engine touches, mirroring the flow_mass fixture in
    tests/controllers/config/test_setup_flows.py.
    """
    mass_minimal.players = MagicMock()
    try:
        yield mass_minimal
    finally:
        for flow in list(mass_minimal.config._setup_flows.values()):
            await mass_minimal.config._abort_flow(flow, reason="aborted")
        if (sweep_handle := mass_minimal.config._flow_sweep_handle) is not None:
            sweep_handle.cancel()


def _make_player(player_id: str = "fully_kiosk_test") -> FullyKioskPlayer:
    """Build a real FullyKioskPlayer with a decoupled (mocked) provider/mass."""
    provider = MockProvider("fully_kiosk", instance_id="fully_kiosk--1")
    provider.mass.config.get_base_player_config.return_value = create_mock_config(
        "Fully Kiosk Test"
    )
    return FullyKioskPlayer(provider=provider, player_id=player_id, host="10.0.0.5")  # type: ignore[arg-type]


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


async def test_setup_flow_collects_and_persists_password(flow_mass: MusicAssistant) -> None:
    """The setup flow shows a single password field and persists it encrypted."""
    player_id = "fully_kiosk_test"
    player = _make_player(player_id)
    flow_mass.config.set(
        f"{CONF_PLAYERS}/{player_id}",
        {"player_id": player_id, "provider": player.provider_id, "enabled": True},
    )
    player_config = PlayerConfig(values={}, provider=player.provider_id, player_id=player_id)
    with (
        patch.object(flow_mass.players, "get_player", return_value=player),
        patch.object(flow_mass.config, "get_player_config", AsyncMock(return_value=player_config)),
    ):
        step = await flow_mass.config.setup_player(player_id)
        assert step.type == FlowStepType.FORM
        assert {entry.key for entry in step.entries} == {CONF_PASSWORD}
        finish_step = await flow_mass.config.submit_setup_flow(
            step.flow_id, {CONF_PASSWORD: "secret"}
        )
    assert finish_step.type == FlowStepType.FINISH
    assert finish_step.result == {"player_id": player_id}
    raw_conf = flow_mass.config.get(f"{CONF_PLAYERS}/{player_id}")
    assert flow_mass.config.decrypt_string(raw_conf["setup_data"][CONF_PASSWORD]) == "secret"
