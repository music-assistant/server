"""Tests for SyncGroupPlayer edge cases."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.constants import CONF_DYNAMIC_GROUP_MEMBERS, CONF_GROUP_MEMBERS
from music_assistant.providers.sync_group.constants import CONF_MEMBERS_FILTER, SGP_PREFIX
from music_assistant.providers.sync_group.player import SyncGroupPlayer
from tests.common import MockProvider


def _create_dynamic_sync_group_player() -> tuple[SyncGroupPlayer, MagicMock]:
    """Create a minimal dynamic SyncGroupPlayer for unit tests."""
    mass = MagicMock()
    mass.closing = False
    mass.config = MagicMock()
    mass.signal_event = MagicMock()
    mass.create_task = MagicMock()

    config = MagicMock()
    config.name = "broadcast"
    config.default_name = "broadcast"
    config.enabled = True

    def _get_value(key: str, default: object | None = None) -> object | None:
        if key == CONF_DYNAMIC_GROUP_MEMBERS:
            return True
        if key in (CONF_GROUP_MEMBERS, CONF_MEMBERS_FILTER):
            return []
        return default

    config.get_value.side_effect = _get_value
    mass.config.get_base_player_config.return_value = config
    mass.config.create_default_player_config = MagicMock()
    mass.players = MagicMock()
    mass.players._handle_cmd_stop = AsyncMock()
    mass.players.cmd_set_members = AsyncMock()
    mass.players.trigger_player_update = MagicMock()

    provider = cast("Any", MockProvider("sync_group", instance_id="sync_group", mass=mass))
    player = SyncGroupPlayer(provider, f"{SGP_PREFIX}abc12345")
    return player, mass


@pytest.mark.asyncio
async def test_remove_last_sync_leader_clears_runtime_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the final sync leader should leave the dynamic group empty."""
    player, mass = _create_dynamic_sync_group_player()
    player._attr_group_members = ["thor-speakers"]
    player._attr_static_group_members = []
    cast("Any", player).sync_leader = SimpleNamespace(
        player_id="thor-speakers",
        display_name="Thor",
        state=SimpleNamespace(
            playback_state=PlaybackState.IDLE,
            can_group_with=set(),
            group_members=["thor-speakers"],
        ),
    )

    async def _skip_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        "music_assistant.providers.sync_group.player.asyncio.sleep",
        _skip_sleep,
    )

    await player.set_members(player_ids_to_remove=["thor-speakers"])

    assert player._attr_group_members == []
    assert player.sync_leader is None
    mass.players._handle_cmd_stop.assert_awaited_once_with("thor-speakers")
    mass.players.cmd_set_members.assert_not_awaited()
    mass.players.trigger_player_update.assert_called_once_with(player.player_id)
