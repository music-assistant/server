"""Tests for the MPD player setup flow."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.config_entries import PlayerConfig
from music_assistant_models.enums import FlowStepType

from music_assistant.constants import CONF_PASSWORD, CONF_PLAYERS
from music_assistant.mass import MusicAssistant
from music_assistant.providers.mpd.player import MPDPlayer


@pytest.fixture
async def flow_mass(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicAssistant]:
    """
    Provide a minimal server suitable for driving player setup flows through the engine.

    Builds on mass_minimal (config controller only) and stubs the players controller
    surface the flow engine touches, mirroring the flow_mass fixture in
    tests/controllers/config/test_setup_flows.py.
    """
    mass_minimal.players = MagicMock()
    mass_minimal.players.on_player_config_change = AsyncMock()
    try:
        yield mass_minimal
    finally:
        for flow in list(mass_minimal.config._setup_flows.values()):
            await mass_minimal.config._abort_flow(flow, reason="aborted")
        if (sweep_handle := mass_minimal.config._flow_sweep_handle) is not None:
            sweep_handle.cancel()


async def test_get_config_entries_excludes_password(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """The password is flow-managed now and must not appear in the options config entries."""
    player = make_mpd_player()
    entries = await player.get_config_entries()
    assert CONF_PASSWORD not in {entry.key for entry in entries}


async def test_on_config_updated_reads_password_via_setup_value(
    make_mpd_player: Callable[..., MPDPlayer],
) -> None:
    """on_config_updated resolves the password from setup_data, not the options config."""
    player = make_mpd_player()
    cast("MagicMock", player.mass.config.get).return_value = {CONF_PASSWORD: "encrypted-blob"}
    cast("MagicMock", player.mass.config.decrypt_string).return_value = "secret"
    with patch.object(player, "_connect", AsyncMock()):
        await player.on_config_updated()
    assert player.password == "secret"
    assert player.needs_setup is False
    assert player.setup_reason is None


async def test_setup_flow_collects_and_persists_password(
    flow_mass: MusicAssistant, make_mpd_player: Callable[..., MPDPlayer]
) -> None:
    """The setup flow shows a single password field and persists it encrypted."""
    player_id = "mpd_test"
    player = make_mpd_player(player_id)
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
