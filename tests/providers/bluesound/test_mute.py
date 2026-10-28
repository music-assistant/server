"""Tests for the mute state MA reads from a BluOS player."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState
from pyblu.entities import PairedPlayer

from music_assistant.providers.bluesound.const import POLL_STATE_STATIC
from music_assistant.providers.bluesound.player import BluesoundPlayer

LEADER = PairedPlayer(ip="192.0.2.1", port=11000)


def _player(status_mute: bool, mute_volume: int | None, leader: PairedPlayer | None) -> MagicMock:
    """Create a player whose next poll reports the given /Status mute and /SyncStatus."""
    fake = MagicMock()
    fake.dynamic_poll_count = 0
    fake.poll_state = POLL_STATE_STATIC
    fake._resolve_playback_state.return_value = PlaybackState.PLAYING
    fake._get_bluesound_sources = AsyncMock(return_value=[])
    fake.provider.player_map = {(LEADER.ip, LEADER.port): "leader"}
    fake.client.status = AsyncMock(
        return_value=MagicMock(input_id=None, service=None, mute=status_mute, seconds=0)
    )
    fake.client.sync_status = AsyncMock(
        return_value=MagicMock(volume=30, mute_volume=mute_volume, leader=leader, followers=None)
    )
    return fake


@pytest.mark.parametrize(
    ("leader_muted", "follower_muted"),
    [(True, False), (False, True), (True, True), (False, False)],
)
async def test_follower_reports_its_own_mute(leader_muted: bool, follower_muted: bool) -> None:
    """A follower's /Status is its leader's, so its own mute must come from /SyncStatus."""
    fake = _player(
        status_mute=leader_muted, mute_volume=30 if follower_muted else None, leader=LEADER
    )

    await BluesoundPlayer.update_attributes(cast("BluesoundPlayer", fake))

    assert fake._attr_volume_muted is follower_muted


@pytest.mark.parametrize("muted", [True, False])
async def test_standalone_player_reports_its_status_mute(muted: bool) -> None:
    """A player that follows no leader keeps reading the mute from its own /Status."""
    fake = _player(status_mute=muted, mute_volume=30 if muted else None, leader=None)

    await BluesoundPlayer.update_attributes(cast("BluesoundPlayer", fake))

    assert fake._attr_volume_muted is muted
