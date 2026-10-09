"""Tests for the playback restart that re-applies DSP when a player's group changes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType

from music_assistant.controllers.players import PlayerController
from tests.common import scheduled_call

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def mock_mass() -> Iterator[MagicMock]:
    """Create a mock MusicAssistant instance whose DSP config reports DSP enabled."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.get_player_dsp_config = MagicMock(return_value=SimpleNamespace(enabled=True))
    mass.player_queues.is_playing_queue = MagicMock(return_value=True)
    yield mass
    for scheduled in mass.create_task.call_args_list:
        target = scheduled.args[0] if scheduled.args else None
        if target is not None and hasattr(target, "close"):
            target.close()


def _player(
    player_id: str = "player_1",
    *,
    playback_state: PlaybackState = PlaybackState.PLAYING,
    synced_to: str | None = None,
) -> MagicMock:
    """Build a solo (or synced) player that resolves to its own queue."""
    player = MagicMock()
    player.player_id = player_id
    player.display_name = player_id
    player.type = PlayerType.PLAYER
    player.extra_data = {}
    player.state.type = PlayerType.PLAYER
    player.state.playback_state = playback_state
    player.state.synced_to = synced_to
    player.state.active_group = None
    player.state.active_source = player_id
    player.state.supported_features = set()
    return player


def _controller(mock_mass: MagicMock, *players: MagicMock) -> PlayerController:
    """Register the given players on a fresh controller."""
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    controller._players = {player.player_id: player for player in players}
    controller.cmd_stop = AsyncMock()  # type: ignore[method-assign]
    controller.cmd_play = AsyncMock()  # type: ignore[method-assign]
    return controller


def _stop_and_play(controller: PlayerController) -> tuple[AsyncMock, AsyncMock]:
    """Return the mocked stop and play commands of a controller built by ``_controller``."""
    return cast("AsyncMock", controller.cmd_stop), cast("AsyncMock", controller.cmd_play)


class TestDspChangeRestart:
    """on_player_dsp_change restarts only a queue the player is actually rendering."""

    async def test_restarts_the_queue_the_player_is_playing(self, mock_mass: MagicMock) -> None:
        """A playing player whose reported media belongs to its queue is restarted."""
        player = _player()
        controller = _controller(mock_mass, player)
        queue = MagicMock(queue_id="player_1")
        mock_mass.player_queues.get = MagicMock(return_value=queue)

        await controller.on_player_dsp_change("player_1", after_group_change=True)

        mock_mass.player_queues.is_playing_queue.assert_called_once_with("player_1", player)
        mock_mass.call_later.assert_called_once_with(
            0, mock_mass.player_queues.resume, "player_1", False
        )
        _stop_and_play(controller)[0].assert_not_awaited()

    async def test_group_change_leaves_a_queue_the_player_is_not_playing_alone(
        self, mock_mass: MagicMock
    ) -> None:
        """After a regroup, a player handed a queue it does not render is not restarted."""
        player = _player()
        controller = _controller(mock_mass, player)
        mock_mass.player_queues.get = MagicMock(return_value=MagicMock(queue_id="player_1"))
        mock_mass.player_queues.is_playing_queue = MagicMock(return_value=False)

        await controller.on_player_dsp_change("player_1", after_group_change=True)

        mock_mass.call_later.assert_not_called()
        stop, play = _stop_and_play(controller)
        stop.assert_not_awaited()
        play.assert_not_awaited()

    async def test_settings_change_restarts_without_checking_the_media(
        self, mock_mass: MagicMock
    ) -> None:
        """An edit of the DSP settings restarts as before, even when the media is unknown."""
        controller = _controller(mock_mass, _player())
        mock_mass.player_queues.get = MagicMock(return_value=MagicMock(queue_id="player_1"))
        mock_mass.player_queues.is_playing_queue = MagicMock(return_value=False)

        await controller.on_player_dsp_change("player_1")

        mock_mass.player_queues.is_playing_queue.assert_not_called()
        mock_mass.call_later.assert_called_once_with(
            0, mock_mass.player_queues.resume, "player_1", False
        )

    async def test_does_nothing_when_not_playing(self, mock_mass: MagicMock) -> None:
        """An idle player has nothing to restart."""
        controller = _controller(mock_mass, _player(playback_state=PlaybackState.IDLE))
        mock_mass.player_queues.get = MagicMock(return_value=MagicMock(queue_id="player_1"))

        await controller.on_player_dsp_change("player_1")

        mock_mass.call_later.assert_not_called()
        mock_mass.player_queues.is_playing_queue.assert_not_called()

    async def test_restarts_a_player_without_a_queue_by_stop_and_play(
        self, mock_mass: MagicMock
    ) -> None:
        """A player playing something that is not a queue is stopped and started again."""
        controller = _controller(mock_mass, _player())
        mock_mass.player_queues.get = MagicMock(return_value=None)

        await controller.on_player_dsp_change("player_1")

        mock_mass.call_later.assert_not_called()
        stop, play = _stop_and_play(controller)
        stop.assert_awaited_once_with("player_1")
        play.assert_awaited_once_with("player_1")

    async def test_group_change_leaves_an_external_source_alone(self, mock_mass: MagicMock) -> None:
        """A leader playing a tv input or connect session has no stream of ours to rebuild."""
        player = _player()
        player.state.active_source = "tv"
        controller = _controller(mock_mass, player)
        controller.is_live_audio_source = MagicMock(return_value=False)  # type: ignore[method-assign]
        mock_mass.player_queues.get = MagicMock(return_value=None)

        await controller.on_player_dsp_change("player_1", after_group_change=True)

        mock_mass.call_later.assert_not_called()
        stop, play = _stop_and_play(controller)
        stop.assert_not_awaited()
        play.assert_not_awaited()

    async def test_group_change_restarts_a_live_audio_source(self, mock_mass: MagicMock) -> None:
        """A live audio source is streamed by the server, so its DSP is rebuilt as before."""
        player = _player()
        player.state.active_source = "audiosource-1"
        controller = _controller(mock_mass, player)
        controller.is_live_audio_source = MagicMock(return_value=True)  # type: ignore[method-assign]
        mock_mass.player_queues.get = MagicMock(return_value=None)

        await controller.on_player_dsp_change("player_1", after_group_change=True)

        stop, play = _stop_and_play(controller)
        stop.assert_awaited_once_with("player_1")
        play.assert_awaited_once_with("player_1")

    async def test_resumes_the_leader_queue_for_a_synced_player(self, mock_mass: MagicMock) -> None:
        """A synced player's DSP change restarts the queue it renders: its leader's."""
        leader = _player("leader")
        child = _player("child", synced_to="leader")
        controller = _controller(mock_mass, leader, child)
        mock_mass.player_queues.get = MagicMock(return_value=MagicMock(queue_id="leader"))

        await controller.on_player_dsp_change("child", after_group_change=True)

        mock_mass.player_queues.is_playing_queue.assert_called_once_with("leader", child)
        mock_mass.call_later.assert_called_once_with(
            0, mock_mass.player_queues.resume, "leader", False
        )


class TestGroupDspChange:
    """_handle_group_dsp_change schedules the restart only for a leader that changes shape."""

    def test_leader_going_from_solo_to_grouped_is_restarted(self, mock_mass: MagicMock) -> None:
        """A DSP-enabled leader that gains its first member is scheduled for a restart."""
        player = _player("leader")
        controller = _controller(mock_mass, player)

        controller._handle_group_dsp_change(player, [], ["leader", "member"])

        mock_mass.create_task.assert_called_once()
        name, arguments = scheduled_call(mock_mass.create_task.call_args.args[0])
        assert name.endswith("on_player_dsp_change")
        assert arguments["player_id"] == "leader"
        assert arguments["after_group_change"] is True

    def test_sync_child_is_left_alone(self, mock_mass: MagicMock) -> None:
        """A player that became a sync child renders its leader's stream: no restart."""
        player = _player("child", synced_to="leader")
        controller = _controller(mock_mass, player)

        controller._handle_group_dsp_change(player, ["child", "other"], [])

        mock_mass.create_task.assert_not_called()

    def test_unchanged_shape_is_left_alone(self, mock_mass: MagicMock) -> None:
        """Adding a third member keeps the group multi-device: nothing to re-apply."""
        player = _player("leader")
        controller = _controller(mock_mass, player)

        controller._handle_group_dsp_change(player, ["leader", "a"], ["leader", "a", "b"])

        mock_mass.create_task.assert_not_called()
