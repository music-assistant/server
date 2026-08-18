"""Tests for the cross-provider track match when every known source is capacity-saturated."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.models.music_provider import MusicProvider, ProviderStreamLimitError

BUSY_INSTANCE = "spotify--busy"
MATCH_INSTANCE = "tidal--match"
FAILING_INSTANCE = "qobuz--failing"
ITEM_ID = "item-1"
MATCHED_ITEM_ID = "item-on-tidal"


def _mapping(
    instance: str, item_id: str = ITEM_ID, quality: ContentType = ContentType.MP3
) -> ProviderMapping:
    """Build a streamable provider mapping."""
    return ProviderMapping(
        item_id=item_id,
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
        media_type=MediaType.TRACK,
        stream_type=StreamType.HTTP,
        path="http://test.invalid/item.mp3",
        duration=30,
    )


def _queue_item(
    *mappings: ProviderMapping, provider: str | None = None, item_id: str = ITEM_ID
) -> QueueItem:
    """Build a queue item whose track carries the given provider mappings."""
    media_item = Track(
        item_id=item_id,
        provider=provider or mappings[0].provider_instance,
        name="Song",
        provider_mappings=set(mappings),
    )
    return QueueItem(
        queue_id="queue-1",
        queue_item_id="queue-item-1",
        name="Song",
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


def _music_provider(instance: str, has_slot: bool = True) -> MagicMock:
    """Build a loaded, match-eligible streaming provider instance."""
    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance
    provider.domain = instance.split("--", maxsplit=1)[0]
    provider.available = True
    provider.is_streaming_provider = True
    provider.has_available_stream_slot = has_slot
    provider.supported_features = {ProviderFeature.SEARCH}
    provider.supported_media_types = {MediaType.TRACK}
    provider.get_stream_details = AsyncMock(return_value=_streamdetails(instance))
    return provider


def _mass(providers: dict[str, MagicMock]) -> MagicMock:
    """Build a mass double that resolves the given provider instances."""
    mass = MagicMock()
    mass.providers = list(providers.values())
    mass.get_provider.side_effect = lambda instance, **_kwargs: providers.get(instance)
    mass.player_queues.queue_data_or_none.return_value = None
    mass.streams.get_config_value.return_value = -17
    mass.music.providers = list(providers.values())
    mass.music.tracks.match_provider = AsyncMock(return_value=[])
    mass.music.tracks.add_provider_mappings = AsyncMock()

    def _schedule(coro: Any, *_args: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return task

    mass.create_task.side_effect = _schedule
    return mass


async def test_saturated_single_mapping_is_rescued_by_a_cross_provider_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A track with one saturated source plays through a match found on another provider."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, quality=ContentType.FLAC))
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        MATCH_INSTANCE: _music_provider(MATCH_INSTANCE),
    }
    mass = _mass(providers)
    matched_mapping = _mapping(MATCH_INSTANCE, item_id=MATCHED_ITEM_ID)
    mass.music.tracks.match_provider = AsyncMock(return_value=[matched_mapping])
    audio = StreamsAudio(mass)
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(side_effect=[_limit_error(BUSY_INSTANCE), expected_buffer])
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    assert queue_item.streamdetails is not None
    assert queue_item.streamdetails.provider == MATCH_INSTANCE
    # the discovered mapping is kept on the media item, so the reselection loop can use it
    assert isinstance(queue_item.media_item, Track)
    assert matched_mapping in queue_item.media_item.provider_mappings
    # the saturated source is only probed, never granted a blocking wait
    waits = [call.kwargs["source_wait_timeout"] for call in get_buffer.await_args_list]
    assert waits == [0, 0]
    mass.music.tracks.match_provider.assert_awaited_once()
    assert mass.music.tracks.match_provider.await_args.args[1] is providers[MATCH_INSTANCE]
    # strictness is what prevents ever playing the wrong track
    assert mass.music.tracks.match_provider.await_args.kwargs["strict"] is True
    # a non-library track keeps the mapping in memory only
    mass.music.tracks.add_provider_mappings.assert_not_awaited()


async def test_a_provider_without_track_support_is_never_searched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching only consults providers that declare support for tracks."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, quality=ContentType.FLAC))
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    radio_only = _music_provider("radioprov--1")
    radio_only.supported_media_types = {MediaType.RADIO}
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        "radioprov--1": radio_only,
        MATCH_INSTANCE: _music_provider(MATCH_INSTANCE),
    }
    mass = _mass(providers)
    mass.music.tracks.match_provider = AsyncMock(
        return_value=[_mapping(MATCH_INSTANCE, item_id=MATCHED_ITEM_ID)]
    )
    audio = StreamsAudio(mass)
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(side_effect=[_limit_error(BUSY_INSTANCE), expected_buffer])
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    # the radio-only provider is skipped; the track-capable provider gets the search
    mass.music.tracks.match_provider.assert_awaited_once()
    assert mass.music.tracks.match_provider.await_args.args[1] is providers[MATCH_INSTANCE]


async def test_a_matchless_search_falls_back_to_the_blocking_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a match, the budget is still spent waiting on the known source."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, quality=ContentType.FLAC))
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        MATCH_INSTANCE: _music_provider(MATCH_INSTANCE),
    }
    # match_provider finds nothing (the _mass default)
    mass = _mass(providers)
    audio = StreamsAudio(mass)
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(side_effect=[_limit_error(BUSY_INSTANCE), expected_buffer])
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    mass.music.tracks.match_provider.assert_awaited_once()
    waits = [call.kwargs["source_wait_timeout"] for call in get_buffer.await_args_list]
    assert waits[0] == 0
    assert waits[1] > 0
    assert get_buffer.await_args_list[1].kwargs["streamdetails"].provider == BUSY_INSTANCE


async def test_a_matchless_search_still_raises_when_the_slot_never_frees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a match and without a freed slot, the typed capacity error surfaces."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, quality=ContentType.FLAC))
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        MATCH_INSTANCE: _music_provider(MATCH_INSTANCE),
    }
    mass = _mass(providers)
    audio = StreamsAudio(mass)
    get_buffer = AsyncMock(side_effect=_limit_error(BUSY_INSTANCE))
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    with pytest.raises(ProviderStreamLimitError):
        await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=0.2)

    mass.music.tracks.match_provider.assert_awaited_once()
    assert get_buffer.await_count == 2


async def test_disallowed_provider_match_is_never_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that opts out of matching keeps the plain blocking behavior."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, quality=ContentType.FLAC))
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        MATCH_INSTANCE: _music_provider(MATCH_INSTANCE),
    }
    mass = _mass(providers)
    mass.music.tracks.match_provider = AsyncMock(
        return_value=[_mapping(MATCH_INSTANCE, item_id=MATCHED_ITEM_ID)]
    )
    audio = StreamsAudio(mass)
    get_buffer = AsyncMock(side_effect=_limit_error(BUSY_INSTANCE))
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    with pytest.raises(ProviderStreamLimitError):
        await audio.get_audio_buffer(
            queue_item,
            reason="prepare_next",
            capacity_wait_timeout=0.2,
            allow_provider_match=False,
        )

    mass.music.tracks.match_provider.assert_not_awaited()
    # without a pending match, the sole candidate gets the blocking wait right away
    assert get_buffer.await_args_list[0].kwargs["source_wait_timeout"] > 0


async def test_a_library_track_persists_the_discovered_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A match for a library track is written back to the library."""
    queue_item = _queue_item(
        _mapping(BUSY_INSTANCE, quality=ContentType.FLAC), provider="library", item_id="42"
    )
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        MATCH_INSTANCE: _music_provider(MATCH_INSTANCE),
    }
    mass = _mass(providers)
    matched_mapping = _mapping(MATCH_INSTANCE, item_id=MATCHED_ITEM_ID)
    mass.music.tracks.match_provider = AsyncMock(return_value=[matched_mapping])
    audio = StreamsAudio(mass)
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(side_effect=[_limit_error(BUSY_INSTANCE), expected_buffer])
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    # the write-back runs as a background task, detached from this playback request
    await asyncio.sleep(0)
    mass.music.tracks.add_provider_mappings.assert_awaited_once_with("42", [matched_mapping])


async def test_one_failing_provider_does_not_end_the_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that errors while searching is skipped in favor of the next one."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, quality=ContentType.FLAC))
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        FAILING_INSTANCE: _music_provider(FAILING_INSTANCE),
        MATCH_INSTANCE: _music_provider(MATCH_INSTANCE),
    }
    mass = _mass(providers)
    matched_mapping = _mapping(MATCH_INSTANCE, item_id=MATCHED_ITEM_ID)
    # a raw (non MusicAssistantError) provider bug must be contained as well
    mass.music.tracks.match_provider = AsyncMock(
        side_effect=[KeyError("unexpected api response"), [matched_mapping]]
    )
    audio = StreamsAudio(mass)
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(side_effect=[_limit_error(BUSY_INSTANCE), expected_buffer])
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    assert mass.music.tracks.match_provider.await_count == 2
    assert queue_item.streamdetails is not None
    assert queue_item.streamdetails.provider == MATCH_INSTANCE


async def test_a_failed_mapping_write_back_does_not_fail_the_rescue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A library write-back error still leaves the discovered mapping serving playback."""
    queue_item = _queue_item(
        _mapping(BUSY_INSTANCE, quality=ContentType.FLAC), provider="library", item_id="42"
    )
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        MATCH_INSTANCE: _music_provider(MATCH_INSTANCE),
    }
    mass = _mass(providers)
    mass.music.tracks.match_provider = AsyncMock(
        return_value=[_mapping(MATCH_INSTANCE, item_id=MATCHED_ITEM_ID)]
    )
    mass.music.tracks.add_provider_mappings = AsyncMock(
        side_effect=MediaNotFoundError("Track not found: 42")
    )
    audio = StreamsAudio(mass)
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(side_effect=[_limit_error(BUSY_INSTANCE), expected_buffer])
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    assert queue_item.streamdetails is not None
    assert queue_item.streamdetails.provider == MATCH_INSTANCE
    await asyncio.sleep(0)
    mass.music.tracks.add_provider_mappings.assert_awaited_once()


async def test_discovery_runs_at_most_once_per_buffer_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discovered provider that is itself saturated does not trigger a second search."""
    queue_item = _queue_item(_mapping(BUSY_INSTANCE, quality=ContentType.FLAC))
    queue_item.streamdetails = _streamdetails(BUSY_INSTANCE)
    providers = {
        BUSY_INSTANCE: _music_provider(BUSY_INSTANCE, has_slot=False),
        MATCH_INSTANCE: _music_provider(MATCH_INSTANCE),
    }
    mass = _mass(providers)
    mass.music.tracks.match_provider = AsyncMock(
        return_value=[_mapping(MATCH_INSTANCE, item_id=MATCHED_ITEM_ID)]
    )
    audio = StreamsAudio(mass)
    expected_buffer = MagicMock(spec=AudioBuffer)
    get_buffer = AsyncMock(
        side_effect=[
            _limit_error(BUSY_INSTANCE),
            _limit_error(MATCH_INSTANCE),
            expected_buffer,
        ]
    )
    monkeypatch.setattr(AudioBuffer, "get_buffer", get_buffer)

    result = await audio.get_audio_buffer(queue_item, reason="streaming", capacity_wait_timeout=1)

    assert result is expected_buffer
    mass.music.tracks.match_provider.assert_awaited_once()
    assert get_buffer.await_count == 3
    # after the one search, saturation everywhere ends in the final blocking pass as before
    waits = [call.kwargs["source_wait_timeout"] for call in get_buffer.await_args_list]
    assert waits[0] == 0
    assert waits[1] == 0
    assert waits[2] > 0
    assert get_buffer.await_args_list[2].kwargs["streamdetails"].provider == BUSY_INSTANCE
