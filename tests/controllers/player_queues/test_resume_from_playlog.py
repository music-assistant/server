"""
Tests for resuming an empty queue from the playlog.

Covers the scope of the playlog fallback used when a queue is resumed while
empty. A resume must only continue *this queue's/user's* own recently-played
history. It must never pull a globally-recent item that was played elsewhere,
which is how a restore after an announcement on an idle (sync group) player
ended up starting an unrelated track. See support #5913.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from music_assistant.controllers.player_queues import PlayerQueuesController


def _make_controller() -> tuple[PlayerQueuesController, AsyncMock]:
    """Bare controller instance with only the dependencies the resume path touches."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.logger = Mock()
    handle_play_media = AsyncMock()
    controller._handle_play_media = handle_play_media  # type: ignore[method-assign]
    return controller, handle_play_media


async def test_resume_from_playlog_ignores_globally_recent_item() -> None:
    """A resume must not start a track that only exists in the unscoped/global playlog."""
    controller, handle_play_media = _make_controller()
    foreign = Mock(uri="spotify://foreign", name="Foreign")

    async def recently_played(**kwargs: object) -> list[Mock]:
        # the track exists only in the global (unscoped) playlog,
        # not in this queue's or user's own history
        if kwargs.get("queue_id") is None and kwargs.get("userid") is None:
            return [foreign]
        return []

    controller.mass = Mock()
    controller.mass.music.recently_played = AsyncMock(side_effect=recently_played)

    queue = Mock(queue_id="kitchen", display_name="Kitchen")
    controller._queue_data = {"kitchen": Mock(userid=None)}

    started = await controller._try_resume_from_playlog(queue)

    assert started is False
    handle_play_media.assert_not_called()


async def test_resume_from_playlog_uses_queue_specific_item() -> None:
    """A resume continues a track from this queue's own playlog history."""
    controller, handle_play_media = _make_controller()
    own = Mock(uri="spotify://own", name="Own")

    controller.mass = Mock()
    controller.mass.music.recently_played = AsyncMock(return_value=[own])

    queue = Mock(queue_id="kitchen", display_name="Kitchen")
    controller._queue_data = {"kitchen": Mock(userid=None)}

    started = await controller._try_resume_from_playlog(queue)

    assert started is True
    handle_play_media.assert_called_once_with("kitchen", own)
