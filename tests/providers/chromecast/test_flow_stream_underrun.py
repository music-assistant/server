"""
Tests for ChromecastPlayer flow-stream EOF detection.

Regression tests for https://github.com/music-assistant/support/issues/5732.

A Cast that underruns the LIVE flow stream at end-of-queue keeps reporting
buffering/playing instead of idle. ``_flow_stream_underrun`` recognises that
condition by asking the queue-owning player whether its flow stream is finished.
The queue owner is NOT always the Cast itself: a Cast exposed as a protocol
player is wrapped by a universal player that owns the queue, so the lookup must
use ``protocol_parent_id`` — otherwise the recovery never fires and the player
is stuck "playing" forever.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from music_assistant.providers.chromecast.player import ChromecastPlayer


def _underrun(fake: MagicMock) -> bool:
    return ChromecastPlayer._flow_stream_underrun(cast("ChromecastPlayer", fake))


def _fake_cast(
    *,
    player_id: str,
    protocol_parent_id: str | None,
    active_cast_group: str | None,
    finished_queue_id: str | None,
) -> MagicMock:
    """Build a MagicMock Cast whose queue reports the given queue_id as finished."""
    fake = MagicMock()
    fake.player_id = player_id
    fake.protocol_parent_id = protocol_parent_id
    fake.active_cast_group = active_cast_group
    fake.mass.player_queues.flow_stream_finished = MagicMock(
        side_effect=lambda qid: qid == finished_queue_id
    )
    return fake


def test_underrun_uses_protocol_parent_for_wrapped_cast() -> None:
    """A non-native Cast resolves its queue via the wrapping universal player."""
    fake = _fake_cast(
        player_id="cast_id",
        protocol_parent_id="up_universal",
        active_cast_group=None,
        finished_queue_id="up_universal",
    )

    assert _underrun(fake) is True
    fake.mass.player_queues.flow_stream_finished.assert_called_once_with("up_universal")


def test_underrun_uses_player_id_for_native_cast() -> None:
    """A native/standalone Cast (no parent) owns its own queue."""
    fake = _fake_cast(
        player_id="cast_id",
        protocol_parent_id=None,
        active_cast_group=None,
        finished_queue_id="cast_id",
    )

    assert _underrun(fake) is True
    fake.mass.player_queues.flow_stream_finished.assert_called_once_with("cast_id")


def test_underrun_prefers_cast_group_over_parent() -> None:
    """A Cast group child mirrors the group's queue, taking precedence."""
    fake = _fake_cast(
        player_id="cast_id",
        protocol_parent_id="up_universal",
        active_cast_group="cast_group",
        finished_queue_id="cast_group",
    )

    assert _underrun(fake) is True
    fake.mass.player_queues.flow_stream_finished.assert_called_once_with("cast_group")


def test_underrun_false_when_wrong_queue_would_be_used() -> None:
    """
    Guard against querying the Cast's own id for a wrapped Cast.

    That would miss the finished flow stream (owned by the universal player)
    and never recover.
    """
    fake = _fake_cast(
        player_id="cast_id",
        protocol_parent_id="up_universal",
        active_cast_group=None,
        # only the Cast's own id is (wrongly) marked finished -> parent lookup returns False
        finished_queue_id="cast_id",
    )

    assert _underrun(fake) is False
