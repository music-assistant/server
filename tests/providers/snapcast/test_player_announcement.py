"""Tests for the Snapcast player's native announcement."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.snapcast.player import SnapCastPlayer

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.snapcast.provider import SnapCastProvider
    from music_assistant.providers.snapcast.snap_cntrl_proto import SnapclientProto


def _make_player(queue: SimpleNamespace) -> tuple[SnapCastPlayer, MagicMock]:
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._extra_data = {}
    player._snap_ma_stream = None
    player._announcement_clip_playing = False
    player.poke_player_update = MagicMock()  # type: ignore[method-assign]
    group = MagicMock(stream="music", set_stream=AsyncMock())
    player.snap_client = cast(
        "SnapclientProto", SimpleNamespace(group=group, identifier="kitchen-client", group_id=None)
    )
    provider = MagicMock(_use_builtin_server=False)
    provider.get_snap_ma_stream.return_value = MagicMock(stream_id="music")
    streams = {
        "announce.mp3": MagicMock(
            stream_id="announcement", start_stream=AsyncMock(), wait_for_stopped=AsyncMock()
        ),
        "music.flac": MagicMock(stream_id="music-restarted", start_stream=AsyncMock()),
    }
    provider.get_snapcast_media_stream = AsyncMock(
        side_effect=lambda media, **_: streams[media.uri]
    )
    provider.ensure_player_owned_group = AsyncMock(return_value=group)
    player._provider = cast("SnapCastProvider", provider)
    mass = MagicMock()
    mass.player_queues.get.return_value = queue

    async def play_index(*_: object) -> None:
        await player.play_media(PlayerMedia(uri="music.flac"))

    mass.player_queues.play_index = AsyncMock(side_effect=play_index)
    player.mass = cast("MusicAssistant", mass)
    player._player_id = "kitchen"
    return player, group


def _queue(allow_seek: bool = True, elapsed: float = 5.4) -> SimpleNamespace:
    return SimpleNamespace(
        queue_id="kitchen",
        active=True,
        state=PlaybackState.PLAYING,
        corrected_elapsed_time=elapsed,
        current_item=SimpleNamespace(
            queue_item_id="item-1",
            streamdetails=SimpleNamespace(allow_seek=allow_seek, duration=180),
        ),
    )


@pytest.mark.asyncio
async def test_announcement_resumes_music_where_it_began() -> None:
    """The music restarts at the position the clip began, so nothing is skipped."""
    player, group = _make_player(_queue())

    await player.play_announcement(PlayerMedia(uri="announce.mp3"))

    player.mass.player_queues.play_index.assert_awaited_once_with(  # type: ignore[attr-defined]
        "kitchen", "item-1", 5
    )
    assert group.set_stream.await_args_list[-1].args == ("music-restarted",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "queue",
    [_queue(allow_seek=False), _queue(elapsed=181)],
    ids=["unseekable", "position-past-item-end"],
)
async def test_announcement_rejoins_live_stream(queue: SimpleNamespace) -> None:
    """Music that cannot be restarted at the clip's start rejoins the live stream."""
    player, group = _make_player(queue)

    await player.play_announcement(PlayerMedia(uri="announce.mp3"))

    player.mass.player_queues.play_index.assert_not_awaited()  # type: ignore[attr-defined]
    assert group.set_stream.await_args_list[-1].args == ("music",)


@pytest.mark.asyncio
async def test_announcement_rejoins_live_stream_when_item_left_queue() -> None:
    """An item removed from the queue during the clip is not restarted."""
    player, group = _make_player(_queue())
    player.mass.player_queues.index_by_id.return_value = None  # type: ignore[attr-defined]

    await player.play_announcement(PlayerMedia(uri="announce.mp3"))

    player.mass.player_queues.play_index.assert_not_awaited()  # type: ignore[attr-defined]
    assert group.set_stream.await_args_list[-1].args == ("music",)
