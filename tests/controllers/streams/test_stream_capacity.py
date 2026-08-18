"""Tests for the capacity-aware source selection behind ``get_audio_buffer``."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.models.music_provider import MusicProvider, ProviderStreamLimitError

BUSY_INSTANCE = "service--busy"
FALLBACK_INSTANCE = "service--fallback"
ITEM_ID = "item-1"


def _mapping(instance: str, quality: ContentType = ContentType.MP3) -> ProviderMapping:
    """Build a streamable provider mapping."""
    return ProviderMapping(
        item_id=ITEM_ID,
        provider_domain=instance.split("--", maxsplit=1)[0],
        provider_instance=instance,
        audio_format=AudioFormat(content_type=quality),
    )


def _streamdetails(instance: str) -> StreamDetails:
    """Build HTTP stream details for one provider instance."""
    return StreamDetails(
        provider=instance,
        item_id=ITEM_ID,
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=MediaType.SOUND_EFFECT,
        stream_type=StreamType.HTTP,
        path="http://test.invalid/item.mp3",
        duration=30,
    )


def _queue_item(*mappings: ProviderMapping) -> QueueItem:
    """Build a queue item with the given provider mappings."""
    media_item = SoundEffect(
        item_id=ITEM_ID,
        provider=mappings[0].provider_instance,
        name="Effect",
        provider_mappings=set(mappings),
    )
    return QueueItem(
        queue_id="queue-1",
        queue_item_id="queue-item-1",
        name="Effect",
        duration=30,
        media_item=media_item,
    )


def _limit_error(instance: str) -> ProviderStreamLimitError:
    """Build a typed source-capacity error for a provider instance."""
    provider = MagicMock(spec=MusicProvider)
    provider.max_concurrent_streams = 1
    provider.name = "Limited"
    provider.instance_id = instance
    return ProviderStreamLimitError(provider, 0)


def _music_provider(instance: str, has_slot: bool) -> MagicMock:
    """Build a loaded streaming provider instance that resolves its own stream details."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance
    provider.domain = instance.split("--", maxsplit=1)[0]
    provider.available = True
    provider.is_streaming_provider = True
    provider.has_available_stream_slot = has_slot
    provider.get_stream_details = AsyncMock(return_value=_streamdetails(instance))
    return provider


def _mass(providers: dict[str, MagicMock] | None = None) -> MagicMock:
    """Build a mass double that resolves the given provider instances."""
    mass = MagicMock()
    if providers is None:
        mass.get_provider.return_value = MagicMock()
    else:
        mass.providers = list(providers.values())
        mass.get_provider.side_effect = lambda instance, **_kwargs: providers.get(instance)
    mass.player_queues.queue_data_or_none.return_value = None
    mass.streams.get_config_value.return_value = -17
    return mass


async def test_reselects_another_mapping_after_a_capacity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source that has no free slot is replaced with another compatible mapping."""
    queue_item = _queue_item(
        _mapping(BUSY_INSTANCE, ContentType.FLAC),
        _mapping(FALLBACK_INSTANCE),
    )
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    audio = StreamsAudio(_mass())
    fallback_details = _streamdetails(FALLBACK_INSTANCE)
    audio.get_stream_details = AsyncMock(return_value=fallback_details)  # type: ignore[method-assign]
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(side_effect=[_limit_error(BUSY_INSTANCE), expected_buffer])
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    assert queue_item.streamdetails is fallback_details
    assert audio.get_stream_details.await_args is not None
    assert audio.get_stream_details.await_args.kwargs["excluded_provider_instances"] == {
        BUSY_INSTANCE
    }


async def test_falls_back_to_a_compatible_instance_of_the_same_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One mapping is retried on another loaded instance of its streaming catalog."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, ContentType.FLAC))
    primary = _music_provider(BUSY_INSTANCE, has_slot=False)
    fallback = _music_provider(FALLBACK_INSTANCE, has_slot=True)
    audio = StreamsAudio(_mass({BUSY_INSTANCE: primary, FALLBACK_INSTANCE: fallback}))
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(side_effect=[_limit_error(BUSY_INSTANCE), expected_buffer])
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    assert queue_item.streamdetails is not None
    assert queue_item.streamdetails.provider == FALLBACK_INSTANCE
    # a saturated provider with alternatives left is probed, not waited on
    assert get_buffer.await_args_list[0].kwargs["source_wait_timeout"] == 0
    assert get_buffer.await_args_list[1].kwargs["source_wait_timeout"] > 0
    fallback.get_stream_details.assert_awaited_once_with(ITEM_ID, MediaType.SOUND_EFFECT)


async def test_all_candidates_busy_ends_in_one_blocking_pass_on_the_best_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With every candidate saturated, the budget is spent waiting on the preferred mapping."""
    queue_item = _queue_item(
        _mapping(BUSY_INSTANCE, ContentType.FLAC),
        _mapping(FALLBACK_INSTANCE),
    )
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        FALLBACK_INSTANCE: _music_provider(FALLBACK_INSTANCE, has_slot=False),
    }
    audio = StreamsAudio(_mass(providers))
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(
        side_effect=[
            _limit_error(BUSY_INSTANCE),
            _limit_error(FALLBACK_INSTANCE),
            expected_buffer,
        ]
    )
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    assert get_buffer.await_count == 3
    # every saturated candidate is only probed, so the budget survives for the final pass
    waits = [call.kwargs["source_wait_timeout"] for call in get_buffer.await_args_list]
    assert waits[0] == 0
    assert waits[1] == 0
    assert waits[2] > 0
    # the single blocking wait is spent on the highest quality mapping, not the last tried
    probed = [call.kwargs["streamdetails"].provider for call in get_buffer.await_args_list]
    assert probed == [BUSY_INSTANCE, FALLBACK_INSTANCE, BUSY_INSTANCE]


async def test_exhausted_budget_raises_typed_error_and_keeps_the_item_playable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spent capacity budget surfaces the typed error without revoking availability."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, ContentType.FLAC))
    original_details = _streamdetails(BUSY_INSTANCE)
    queue_item.streamdetails = original_details
    audio = StreamsAudio(_mass())
    get_buffer = AsyncMock(side_effect=_limit_error(BUSY_INSTANCE))
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    with pytest.raises(ProviderStreamLimitError):
        await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=0)

    assert queue_item.available
    assert queue_item.streamdetails is original_details
    assert get_buffer.await_count == 1


async def test_capacity_reselection_is_shared_by_concurrent_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Speculative and playback waiters on one item share the single replacement source."""
    queue_item = _queue_item(
        _mapping(BUSY_INSTANCE, ContentType.FLAC),
        _mapping(FALLBACK_INSTANCE),
    )
    busy_details = _streamdetails(BUSY_INSTANCE)
    queue_item.streamdetails = busy_details
    audio = StreamsAudio(_mass())
    fallback_details = _streamdetails(FALLBACK_INSTANCE)
    audio.get_stream_details = AsyncMock(return_value=fallback_details)  # type: ignore[method-assign]
    fallback_buffer = MagicMock(spec=AudioBuffer)

    async def _get_buffer(**kwargs: object) -> MagicMock:
        # yield control so both waiters would race without the per-item lock
        await asyncio.sleep(0)
        if kwargs["streamdetails"] is busy_details:
            raise _limit_error(BUSY_INSTANCE)
        return fallback_buffer

    monkeypatch.setattr(AudioBuffer, "get_buffer", _get_buffer)

    results = await asyncio.gather(
        audio.get_audio_buffer(queue_item, reason="prepare_next", capacity_wait_timeout=1),
        audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1),
    )

    assert results[0] is fallback_buffer
    assert results[1] is fallback_buffer
    assert queue_item.streamdetails is fallback_details
    assert audio.get_stream_details.await_count == 1


async def test_a_failed_reselection_spends_the_budget_on_the_blocked_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unplayable alternative falls back to waiting out the budget on the busy source."""
    queue_item = _queue_item(
        _mapping(BUSY_INSTANCE, ContentType.FLAC),
        _mapping(FALLBACK_INSTANCE),
    )
    busy_details = _streamdetails(BUSY_INSTANCE)
    queue_item.streamdetails = busy_details
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        FALLBACK_INSTANCE: _music_provider(FALLBACK_INSTANCE, has_slot=True),
    }
    audio = StreamsAudio(_mass(providers))
    # the alternative mapping exists but can not be resolved (e.g. region locked)
    audio.get_stream_details = AsyncMock(side_effect=MediaNotFoundError("not here"))  # type: ignore[method-assign]
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(side_effect=[_limit_error(BUSY_INSTANCE), expected_buffer])
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    # the capacity budget is spent on the blocked provider instead of being abandoned
    assert result is expected_buffer
    assert get_buffer.await_count == 2
    assert get_buffer.await_args_list[0].kwargs["source_wait_timeout"] == 0
    assert get_buffer.await_args_list[1].kwargs["source_wait_timeout"] > 0
    assert get_buffer.await_args_list[1].kwargs["streamdetails"] is busy_details
    assert queue_item.streamdetails is busy_details


async def test_a_broken_alternate_falls_back_to_the_capacity_blocked_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing alternate source must not turn a transient capacity miss into a hard failure."""
    queue_item = _queue_item(
        _mapping(BUSY_INSTANCE, ContentType.FLAC),
        _mapping(FALLBACK_INSTANCE),
    )
    busy_details = _streamdetails(BUSY_INSTANCE)
    queue_item.streamdetails = busy_details
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        FALLBACK_INSTANCE: _music_provider(FALLBACK_INSTANCE, has_slot=True),
    }
    audio = StreamsAudio(_mass(providers))
    audio.get_stream_details = AsyncMock(return_value=_streamdetails(FALLBACK_INSTANCE))  # type: ignore[method-assign]
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(
        side_effect=[
            _limit_error(BUSY_INSTANCE),
            AudioError("alternate source is broken"),
            expected_buffer,
        ]
    )
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    assert get_buffer.await_count == 3
    # the budget is returned to the blocked source instead of surfacing the alternate's error
    assert get_buffer.await_args_list[2].kwargs["streamdetails"] is busy_details
    assert get_buffer.await_args_list[2].kwargs["source_wait_timeout"] > 0
    assert queue_item.streamdetails is busy_details


async def test_the_final_pass_surfaces_the_blocked_sources_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the budget is spent on the preferred source, its real failure is the answer."""
    queue_item = _queue_item(
        _mapping(BUSY_INSTANCE, ContentType.FLAC),
        _mapping(FALLBACK_INSTANCE),
    )
    busy_details = _streamdetails(BUSY_INSTANCE)
    queue_item.streamdetails = busy_details
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        FALLBACK_INSTANCE: _music_provider(FALLBACK_INSTANCE, has_slot=True),
    }
    audio = StreamsAudio(_mass(providers))
    audio.get_stream_details = AsyncMock(return_value=_streamdetails(FALLBACK_INSTANCE))  # type: ignore[method-assign]
    monkeypatch.setattr(
        AudioBuffer,
        "get_buffer",
        AsyncMock(
            side_effect=[
                _limit_error(BUSY_INSTANCE),
                AudioError("alternate source is broken"),
                AudioError("preferred source is broken"),
            ]
        ),
    )

    with pytest.raises(AudioError, match="preferred source is broken"):
        await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert queue_item.streamdetails is busy_details


@pytest.mark.parametrize(
    "reselection_error",
    [ProviderUnavailableError("gone"), asyncio.CancelledError()],
    ids=["provider_unavailable", "cancelled"],
)
async def test_streamdetails_survive_an_unexpected_reselection_failure(
    monkeypatch: pytest.MonkeyPatch,
    reselection_error: BaseException,
) -> None:
    """No exit path may leave the queue item without stream details."""
    queue_item = _queue_item(
        _mapping(BUSY_INSTANCE, ContentType.FLAC),
        _mapping(FALLBACK_INSTANCE),
    )
    busy_details = _streamdetails(BUSY_INSTANCE)
    queue_item.streamdetails = busy_details
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        FALLBACK_INSTANCE: _music_provider(FALLBACK_INSTANCE, has_slot=True),
    }
    audio = StreamsAudio(_mass(providers))
    audio.get_stream_details = AsyncMock(side_effect=reselection_error)  # type: ignore[method-assign]
    monkeypatch.setattr(
        AudioBuffer, "get_buffer", AsyncMock(side_effect=_limit_error(BUSY_INSTANCE))
    )

    with pytest.raises(type(reselection_error)):
        await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    # a None here crashes the flow stream's end-of-track bookkeeping
    assert queue_item.streamdetails is busy_details


async def test_flow_mode_skips_the_item_on_capacity_exhaustion() -> None:
    """Flow mode drops an item it can not open a source for, leaving it playable."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, ContentType.FLAC))
    streamdetails = _streamdetails(BUSY_INSTANCE)
    streamdetails.loudness = -10.0  # skip the audio-analysis hydration call
    queue_item.streamdetails = streamdetails
    audio = StreamsAudio(_mass())
    audio.get_audio_buffer = AsyncMock(  # type: ignore[method-assign]
        side_effect=_limit_error(BUSY_INSTANCE)
    )
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=8000,
        bit_depth=16,
        channels=2,
    )

    chunks = [
        chunk
        async for chunk in audio.get_queue_item_stream(queue_item, pcm_format, raise_on_error=False)
    ]

    assert chunks == []
    assert queue_item.available
    assert streamdetails.stream_error is True
