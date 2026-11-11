"""
Tests for ChromecastPlayer flow-stream EOF detection.

Regression tests for https://github.com/music-assistant/support/issues/5732.

A Cast that underruns the LIVE flow stream at end-of-queue keeps reporting
buffering/playing instead of idle. ``_flow_stream_underrun`` recognises that
condition by asking the queue-owning player whether its flow stream is finished.
The queue owner is the player the Cast is attached to, whose id is not the id of
the Cast itself, or for a Cast that plays through a Cast group, the player of that
group; asking for the wrong id means the recovery never fires and the player is
stuck "playing" forever.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from music_assistant.providers.chromecast.player import ChromecastPlayer

CAST_ID = "cast_id"
CAST_PLAYER_ID = "pl0123456789ab"
GROUP_ID = "cast_group"
GROUP_PLAYER_ID = "plfedcba987654"


def _underrun(fake: MagicMock) -> bool:
    return ChromecastPlayer._flow_stream_underrun(cast("ChromecastPlayer", fake))


def _fake_cast(*, active_cast_group: str | None, finished_queue_id: str | None) -> MagicMock:
    """Build a MagicMock Cast, attached to a player, whose queue reports the given id finished."""
    fake = MagicMock()
    fake.protocol_id = CAST_ID
    fake.player.player_id = CAST_PLAYER_ID
    fake.active_cast_group = active_cast_group
    group = MagicMock()
    group.player.player_id = GROUP_PLAYER_ID
    fake.provider.get_protocol.side_effect = {GROUP_ID: group}.get
    fake.mass.player_queues.flow_stream_finished = MagicMock(
        side_effect=lambda qid: qid == finished_queue_id
    )
    return fake


def test_underrun_uses_the_queue_of_the_player_of_the_cast() -> None:
    """A Cast plays the queue of the player it is attached to."""
    fake = _fake_cast(active_cast_group=None, finished_queue_id=CAST_PLAYER_ID)

    assert _underrun(fake) is True
    fake.mass.player_queues.flow_stream_finished.assert_called_once_with(CAST_PLAYER_ID)


def test_underrun_uses_the_queue_of_the_cast_group() -> None:
    """A Cast group child mirrors the queue of the group's player."""
    fake = _fake_cast(active_cast_group=GROUP_ID, finished_queue_id=GROUP_PLAYER_ID)

    assert _underrun(fake) is True
    fake.mass.player_queues.flow_stream_finished.assert_called_once_with(GROUP_PLAYER_ID)


def test_underrun_false_when_wrong_queue_would_be_used() -> None:
    """
    Guard against querying the Cast's own id.

    That would miss the finished flow stream (owned by the player) and never recover.
    """
    # only the Cast's own id is (wrongly) marked finished -> player lookup returns False
    fake = _fake_cast(active_cast_group=None, finished_queue_id=CAST_ID)

    assert _underrun(fake) is False


def test_underrun_false_for_a_cast_without_a_player() -> None:
    """A Cast that is not attached plays no queue."""
    fake = _fake_cast(active_cast_group=None, finished_queue_id=CAST_ID)
    fake.player = None

    assert _underrun(fake) is False
    fake.mass.player_queues.flow_stream_finished.assert_not_called()
