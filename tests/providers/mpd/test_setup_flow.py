"""Tests for the MPD player setup flow."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

from music_assistant_models.enums import FlowStepType

from music_assistant.constants import CONF_PASSWORD

from .conftest import FIRST, HOST, FakeMPDClient, add_provider

if TYPE_CHECKING:
    from collections.abc import Callable
    from unittest.mock import MagicMock

    from music_assistant.providers.mpd.player import MPDPlayer
    from tests.scenarios.harness import Scenario


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
    player.config.setup_data = {CONF_PASSWORD: "encrypted-blob"}
    cast("MagicMock", player.mass.config.decrypt_string).return_value = "secret"
    with patch.object(player, "_connect", AsyncMock()):
        await player.on_config_updated()
    assert player.password == "secret"
    assert player.needs_setup is False
    assert player.setup_reason is None


async def test_setup_flow_collects_and_persists_password(scenario: Scenario) -> None:
    """The setup flow of a new player stores the password for its server."""
    provider = await add_provider(scenario, [HOST])
    protocol = provider.get_protocol(FIRST)
    assert protocol is not None
    assert protocol.player is not None
    player_id = protocol.player.player_id

    with patch.object(FakeMPDClient, "password", AsyncMock()) as password:
        step = await scenario.mass.config.setup_player(player_id)
        assert step.type == FlowStepType.FORM
        assert {entry.key for entry in step.entries} == {CONF_PASSWORD}
        finish_step = await scenario.mass.config.submit_setup_flow(
            step.flow_id, {CONF_PASSWORD: "secret"}
        )
        await scenario.settle()

    assert finish_step.type == FlowStepType.FINISH
    assert finish_step.result == {"player_id": player_id}
    record = scenario.mass.config.get_protocol_record(FIRST)
    assert record is not None
    assert scenario.mass.config.decrypt_string(record["setup_data"][CONF_PASSWORD]) == "secret"
    password.assert_awaited_with("secret")
    # the config controller leaves its sweep of idle flows armed when the server stops
    if (sweep_handle := scenario.mass.config._flow_sweep_handle) is not None:
        sweep_handle.cancel()
