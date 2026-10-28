"""Tests for starting playback on a Snapcast player."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.snapcast.player import SnapCastPlayer


async def test_play_media_on_the_attached_stream_detaches_until_the_new_run_started() -> None:
    """Play on the stream the group is on detaches the group until the new run started."""
    group = SimpleNamespace(name="player_1", stream="music-1", clients=[])
    set_streams: list[str] = []

    async def set_stream(stream_id: str) -> None:
        set_streams.append(stream_id)
        group.stream = stream_id

    group.set_stream = set_stream
    stream_on_start: list[str] = []

    async def wait_for_started(**_: float) -> None:
        stream_on_start.append(group.stream)

    ma_stream = MagicMock(stream_id="music-1")
    ma_stream.start_stream = AsyncMock()
    ma_stream.wait_for_started = wait_for_started
    provider = MagicMock()
    provider.get_snapcast_media_stream = AsyncMock(return_value=ma_stream)
    provider.ensure_player_owned_group = AsyncMock(return_value=group)
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._player_id = "player_1"
    player._provider = provider
    player._announcement_clip_playing = False
    player.snap_client = SimpleNamespace(group=group)
    player.poke_player_update = MagicMock()  # type: ignore[method-assign]

    await player.play_media(MagicMock())

    assert stream_on_start == ["default"]
    assert set_streams == ["default", "music-1"]
