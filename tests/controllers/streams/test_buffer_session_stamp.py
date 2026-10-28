"""
Tests that an audio buffer records the playback session that asked for it.

A queue stop releases only the buffers of the session it is tearing down, so the claim has
to be made where the buffer is attached. Stream details outlive a stop and are reused as
they are, so the session they were originally resolved for says nothing about who is
filling them now.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer

QUEUE_ID = "queue-1"
INSTANCE = "service--1"
ITEM_ID = "item-1"


def _queue_item(stamped_with: str | None) -> QueueItem:
    """
    Build a queue item that already carries stream details, so none are resolved.

    :param stamped_with: Session recorded on those details, as an earlier session would
        have left them, or None for details that never backed a buffer.
    """
    media_item = SoundEffect(
        item_id=ITEM_ID,
        provider=INSTANCE,
        name="Effect",
        provider_mappings={
            ProviderMapping(
                item_id=ITEM_ID,
                provider_domain=INSTANCE.split("--", maxsplit=1)[0],
                provider_instance=INSTANCE,
                audio_format=AudioFormat(content_type=ContentType.MP3),
            )
        },
    )
    queue_item = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id="queue-item-1",
        name="Effect",
        duration=30,
        media_item=media_item,
    )
    queue_item.streamdetails = StreamDetails(
        provider=INSTANCE,
        item_id=ITEM_ID,
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.SOUND_EFFECT,
        stream_type=StreamType.HTTP,
        path="http://test.invalid/item.mp3",
        duration=30,
        queue_id=QUEUE_ID,
    )
    queue_item.streamdetails.queue_session_id = stamped_with
    return queue_item


def _audio(session_id: str | None) -> StreamsAudio:
    """Build a streams-audio controller whose queue is in the given playback session."""
    mass = MagicMock()
    mass.player_queues.queue_data_or_none.return_value = (
        PlayerQueueData(queue=MagicMock(), session_id=session_id)
        if session_id is not None
        else None
    )
    mass.get_provider.return_value = MagicMock()
    return StreamsAudio(mass)


async def test_the_session_asking_for_a_buffer_claims_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Details left stamped by an earlier session move to the session filling them now."""
    queue_item = _queue_item(stamped_with="sess-1")
    monkeypatch.setattr(
        AudioBuffer, "get_buffer", AsyncMock(return_value=MagicMock(spec=AudioBuffer))
    )

    await _audio("sess-2").get_audio_buffer(queue_item, reason="streaming")

    assert queue_item.streamdetails is not None
    assert queue_item.streamdetails.queue_session_id == "sess-2"


async def test_a_superseded_request_cannot_take_a_live_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Audio the playing session is filling stays its own, whoever asks for it next.

    The single-item stream route serves a request for a session that is no longer the
    queue's without rejecting it, and reusing a live buffer must not hand that session
    the power to release it out from under the one still playing.
    """
    queue_item = _queue_item(stamped_with="sess-2")
    live_buffer = MagicMock(spec=AudioBuffer)
    queue_item.streamdetails.buffer = live_buffer  # type: ignore[union-attr]
    monkeypatch.setattr(AudioBuffer, "get_buffer", AsyncMock(return_value=live_buffer))

    await _audio("sess-2").get_audio_buffer(queue_item, reason="streaming")

    assert queue_item.streamdetails is not None
    assert queue_item.streamdetails.queue_session_id == "sess-2"


async def test_an_unregistered_queue_claims_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A queue the controller no longer holds a record for leaves no claim behind.

    An unclaimed buffer is released by any stop, which is what has to happen: leaving it
    would keep its producer - and the provider's stream slot - alive.
    """
    queue_item = _queue_item(stamped_with="sess-1")
    monkeypatch.setattr(
        AudioBuffer, "get_buffer", AsyncMock(return_value=MagicMock(spec=AudioBuffer))
    )

    await _audio(None).get_audio_buffer(queue_item, reason="streaming")

    assert queue_item.streamdetails is not None
    assert queue_item.streamdetails.queue_session_id is None


async def test_the_claim_is_made_before_the_buffer_is_filled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer that starts during the fill is already covered by its session's stop."""
    queue_item = _queue_item(stamped_with=None)
    seen: list[str | None] = []

    async def _record_the_claim(**kwargs: Any) -> MagicMock:
        streamdetails: StreamDetails = kwargs["streamdetails"]
        seen.append(streamdetails.queue_session_id)
        return MagicMock(spec=AudioBuffer)

    monkeypatch.setattr(AudioBuffer, "get_buffer", _record_the_claim)

    await _audio("sess-1").get_audio_buffer(queue_item, reason="streaming")

    assert seen == ["sess-1"]


async def test_a_filled_buffer_reports_its_queue_item_as_fully_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completion signal of a new buffer names the queue item it was attached for."""
    queue_item = _queue_item(stamped_with=None)
    get_buffer = AsyncMock(return_value=MagicMock(spec=AudioBuffer))
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)
    audio = _audio("sess-1")
    mass = cast("MagicMock", audio.mass)

    await audio.get_audio_buffer(queue_item, reason="streaming")
    assert get_buffer.await_args is not None
    get_buffer.await_args.kwargs["on_complete"]()

    mass.player_queues.track_fully_buffered.assert_called_once_with(QUEUE_ID, "queue-item-1")


@pytest.mark.parametrize("teardown", ["stop", "removal", "replace"])
async def test_a_buffer_attached_after_its_queue_was_torn_down_is_released(
    monkeypatch: pytest.MonkeyPatch, teardown: str
) -> None:
    """
    A request still resolving its source when the queue tears down its audio keeps none.

    The teardown releases what is attached at that moment, so a buffer this request attaches
    afterwards would hold its provider stream slot until the inactivity timeout.
    """
    queue_item = _queue_item(stamped_with=None)
    details = queue_item.streamdetails
    assert details is not None
    queue_item.streamdetails = None
    audio = _audio("sess-1")
    mass = cast("MagicMock", audio.mass)
    queue_data = mass.player_queues.queue_data_or_none.return_value

    async def _resolve_while_torn_down(*_args: Any, **_kwargs: Any) -> StreamDetails:
        if teardown == "stop":
            queue_data.session_id = None
        else:
            mass.player_queues.get_item.return_value = None
            if teardown == "removal":
                mass.player_queues.queue_data_or_none.return_value = None
        return details

    audio.get_stream_details = _resolve_while_torn_down  # type: ignore[method-assign]
    late_buffer = MagicMock(spec=AudioBuffer)

    async def _attach(**kwargs: Any) -> MagicMock:
        kwargs["streamdetails"].buffer = late_buffer
        return late_buffer

    monkeypatch.setattr(AudioBuffer, "get_buffer", _attach)

    result = await audio.get_audio_buffer(queue_item, reason="streaming")

    assert result is None
    assert details.buffer is None
    late_buffer.clear.assert_awaited_once()


async def test_a_skip_during_the_request_keeps_the_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that rotated (a skip) still plays the queue and owns the audio prepared for it."""
    queue_item = _queue_item(stamped_with=None)
    details = queue_item.streamdetails
    assert details is not None
    queue_item.streamdetails = None
    audio = _audio("sess-1")
    mass = cast("MagicMock", audio.mass)
    queue_data = mass.player_queues.queue_data_or_none.return_value

    async def _resolve_while_skipped(*_args: Any, **_kwargs: Any) -> StreamDetails:
        queue_data.session_id = "sess-2"
        return details

    audio.get_stream_details = _resolve_while_skipped  # type: ignore[method-assign]
    buffer = MagicMock(spec=AudioBuffer)
    monkeypatch.setattr(AudioBuffer, "get_buffer", AsyncMock(return_value=buffer))

    assert await audio.get_audio_buffer(queue_item, reason="streaming") is buffer
    buffer.clear.assert_not_awaited()
