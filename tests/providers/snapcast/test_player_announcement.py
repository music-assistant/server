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
    from music_assistant.models.player import Player
    from music_assistant.providers.snapcast.provider import SnapCastProvider
    from music_assistant.providers.snapcast.snap_cntrl_proto import SnapclientProto


def _make_player(queue: SimpleNamespace) -> tuple[SnapCastPlayer, MagicMock]:
    player = SnapCastPlayer.__new__(SnapCastPlayer)
    player._snap_ma_stream = None
    player._announcement_clip_playing = False
    player.poke_player_update = MagicMock()  # type: ignore[method-assign]
    group = MagicMock(stream="music", set_stream=AsyncMock())
    player.snap_client = cast(
        "SnapclientProto", SimpleNamespace(group=group, identifier="kitchen-client", group_id=None)
    )
    provider = MagicMock(_use_builtin_server=False, _snapcast_server_buffer_size=0)
    provider.get_snap_ma_stream.return_value = MagicMock(stream_id="music")
    provider._snapserver.streams = [SimpleNamespace(identifier="music")]
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
    player._protocol_id = "ma_kitchen"
    player.logger = MagicMock()
    # the queue of the player the protocol is attached to
    player._player = cast("Player", SimpleNamespace(player_id="kitchen", name="Kitchen"))
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

    # the queue is the one of the player, whose id differs from the protocol id
    player.mass.player_queues.get.assert_called_once_with("kitchen")  # type: ignore[attr-defined]
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_streams", "expected"),
    [
        (["librespot", "announcement"], ("librespot",)),
        (["default", "announcement"], ("default",)),
    ],
    ids=["previous-external-stream", "previous-stream-gone"],
)
async def test_announcement_restores_previous_snapserver_stream(
    server_streams: list[str], expected: tuple[str]
) -> None:
    """A group on a non-MA source goes back to it, else to 'default' when the server has one."""
    player, group = _make_player(_queue())
    group.stream = "librespot"
    provider = cast("MagicMock", player.snap_provider)
    provider.get_snap_ma_stream.return_value = None
    provider._snapserver.streams = [SimpleNamespace(identifier=s) for s in server_streams]
    player.mass.player_queues.get.return_value = None  # type: ignore[attr-defined]

    await player.play_announcement(PlayerMedia(uri="announce.mp3"))

    assert group.set_stream.await_args_list[-1].args == expected


@pytest.mark.asyncio
async def test_announcement_skips_restore_without_target_stream() -> None:
    """Without the previous or a 'default' stream, no invalid stream is requested."""
    player, group = _make_player(_queue())
    group.stream = "librespot"
    provider = cast("MagicMock", player.snap_provider)
    provider.get_snap_ma_stream.return_value = None
    provider._snapserver.streams = [SimpleNamespace(identifier="announcement")]
    player.mass.player_queues.get.return_value = None  # type: ignore[attr-defined]

    await player.play_announcement(PlayerMedia(uri="announce.mp3"))

    assert group.set_stream.await_args_list[-1].args == ("announcement",)


def _make_leader() -> tuple[SnapCastPlayer, MagicMock, list[tuple[object, ...]]]:
    """
    Return a player that leads a group with a member, and the log of what changes the group.

    Each entry of the log records the clients and the stream of the group at that moment.
    """
    player, group = _make_player(_queue())
    group.name = "ma_kitchen"
    group.clients = ["kitchen-client", "living-client"]
    events: list[tuple[object, ...]] = []

    async def set_stream(stream_id: str) -> None:
        group.stream = stream_id
        events.append(("set_stream", stream_id, list(group.clients)))

    async def add_client(client_id: str) -> None:
        group.clients.append(client_id)
        events.append(("add_client", client_id, group.stream))

    async def isolate(player_id: str, **kwargs: object) -> None:
        group.clients.remove("living-client")
        events.append(("isolate", player_id, kwargs))

    group.set_stream = AsyncMock(side_effect=set_stream)
    group.add_client = AsyncMock(side_effect=add_client)
    cast("MagicMock", player.snap_provider).isolate_player_to_dedicated_group = AsyncMock(
        side_effect=isolate
    )
    return player, group, events


@pytest.mark.asyncio
async def test_leader_plays_the_announcement_alone() -> None:
    """
    A leader plays the clip alone; its (muted) member plays on and joins it again afterwards.

    The member leaves the group before the clip is set on it, so it never gets the clip and
    its mute is left alone. Its music is heard meanwhile, so the leader rejoins it on the live
    stream instead of restarting the music where the clip began.
    """
    player, _group, events = _make_leader()
    provider = cast("MagicMock", player.snap_provider)

    await player.play_announcement(PlayerMedia(uri="announce.mp3"))

    assert events == [
        ("isolate", "ma_kitchen", {"others_stream_id": "music"}),
        ("set_stream", "announcement", ["kitchen-client"]),
        ("set_stream", "music", ["kitchen-client"]),
        ("add_client", "living-client", "music"),
    ]
    player.mass.player_queues.play_index.assert_not_awaited()  # type: ignore[attr-defined]
    provider._snapserver.client_volume.assert_not_called()
    provider._update_group_callbacks.assert_called_once_with(poke=True)


@pytest.mark.asyncio
async def test_leader_gets_its_members_back_when_the_announcement_fails() -> None:
    """A clip that fails still puts the leader back on the music and lets its member rejoin."""
    player, _group, events = _make_leader()
    provider = cast("MagicMock", player.snap_provider)
    clip = provider.get_snapcast_media_stream.side_effect(PlayerMedia(uri="announce.mp3"))
    clip.wait_for_stopped.side_effect = RuntimeError("clip stream died")

    with pytest.raises(RuntimeError, match="clip stream died"):
        await player.play_announcement(PlayerMedia(uri="announce.mp3"))

    assert events == [
        ("isolate", "ma_kitchen", {"others_stream_id": "music"}),
        ("set_stream", "announcement", ["kitchen-client"]),
        ("set_stream", "music", ["kitchen-client"]),
        ("add_client", "living-client", "music"),
    ]
    provider._update_group_callbacks.assert_called_once_with(poke=True)
