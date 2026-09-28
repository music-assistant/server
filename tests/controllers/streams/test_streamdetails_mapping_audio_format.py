"""
Tests for filling in the audio format of a library mapping from the stream it resolves to.

A provider mapping added without fetching the provider item, such as one built from a
MusicBrainz link, carries no audio format and ranks last among the item's sources. Once
the mapping serves a stream, the format the provider declares on its streamdetails is
stored on the mapping, without any extra provider request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, Track
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from tests.common import set_music_source_access

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

INSTANCE = "tidal--abc"
ITEM_ID = "79280548"
LIBRARY_ID = "42"
FLAC = AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=16)
OGG = AudioFormat(content_type=ContentType.OGG, bit_rate=320)


def _mapping(audio_format: AudioFormat | None = None) -> ProviderMapping:
    """Build a mapping on the provider, with no audio format unless one is given."""
    return ProviderMapping(
        item_id=ITEM_ID,
        provider_domain="tidal",
        provider_instance=INSTANCE,
        audio_format=audio_format or AudioFormat(),
    )


def _library_track(mapping: ProviderMapping) -> Track:
    """Build a library track carrying the given mapping."""
    return Track(
        item_id=LIBRARY_ID, provider="library", name="15 Step", provider_mappings={mapping}
    )


def _queue_item(media_item: Track) -> QueueItem:
    """Build a queue item playing the given media item."""
    return QueueItem(
        queue_id="q1",
        queue_item_id="qi1",
        name=media_item.name,
        duration=None,
        media_item=media_item,
    )


def _provider(audio_format: AudioFormat, item_id: str = ITEM_ID) -> MagicMock:
    """Build a provider whose streamdetails declare the given audio format."""

    async def _get_stream_details(_item_id: str, media_type: MediaType) -> StreamDetails:
        return StreamDetails(
            provider=INSTANCE,
            item_id=item_id,
            audio_format=audio_format,
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path="http://localhost/track.flac",
            duration=237,
        )

    provider = MagicMock()
    provider.instance_id = INSTANCE
    provider.domain = "tidal"
    provider.available = True
    provider.get_stream_details = _get_stream_details
    return provider


def _audio(
    provider: MagicMock,
) -> tuple[StreamsAudio, AsyncMock, list[Coroutine[Any, Any, None]]]:
    """
    Build a StreamsAudio resolving the provider, plus the mapping update and its scheduling.

    The scheduled coroutines are handed back unstarted, so a test decides when the
    mapping write runs and sees exactly how many were scheduled.
    """
    mass = MagicMock()
    mass.get_provider.side_effect = lambda instance, **_kwargs: (
        provider if instance == INSTANCE else None
    )
    mass.providers = []
    mass.player_queues.queue_data_or_none.return_value = None
    mass.webserver.auth.get_user = AsyncMock(return_value=None)
    set_music_source_access(mass, {INSTANCE: None})
    mass.streams.get_config_value.return_value = -17
    update_provider_mapping = AsyncMock()
    mass.music.update_provider_mapping = update_provider_mapping
    scheduled: list[Coroutine[Any, Any, None]] = []
    mass.create_task.side_effect = lambda coro, **_kwargs: scheduled.append(coro)
    return StreamsAudio(mass), update_provider_mapping, scheduled


async def test_a_mapping_without_format_takes_the_format_of_its_stream() -> None:
    """The format the provider declares is stored on the mapping that had none."""
    mapping = _mapping()
    audio, update_provider_mapping, scheduled = _audio(_provider(FLAC))

    await audio.get_stream_details(queue_item=_queue_item(_library_track(mapping)))

    assert len(scheduled) == 1
    await scheduled[0]
    update_provider_mapping.assert_awaited_once_with(
        MediaType.TRACK, LIBRARY_ID, INSTANCE, ITEM_ID, audio_format=FLAC
    )
    assert mapping.audio_format == FLAC


async def test_the_mapping_keeps_its_own_copy_of_the_format() -> None:
    """Fields ffmpeg later fills in on the streamdetails do not reach the mapping."""
    mapping = _mapping()
    audio, _, _ = _audio(_provider(FLAC))

    streamdetails = await audio.get_stream_details(queue_item=_queue_item(_library_track(mapping)))

    assert mapping.audio_format is not streamdetails.audio_format


async def test_a_mapping_with_a_format_is_left_alone() -> None:
    """A mapping that already carries a format is not overwritten by the stream's."""
    mapping = _mapping(OGG)
    audio, _, scheduled = _audio(_provider(FLAC))

    await audio.get_stream_details(queue_item=_queue_item(_library_track(mapping)))

    assert scheduled == []
    assert mapping.audio_format == OGG


async def test_a_stream_without_format_fills_in_nothing() -> None:
    """Streamdetails that do not declare a format leave the mapping as it is."""
    mapping = _mapping()
    audio, _, scheduled = _audio(_provider(AudioFormat()))

    await audio.get_stream_details(queue_item=_queue_item(_library_track(mapping)))

    assert scheduled == []
    assert mapping.audio_format.content_type == ContentType.UNKNOWN


async def test_a_stream_for_another_item_fills_in_nothing() -> None:
    """Streamdetails naming a different item than the mapping are not trusted for it."""
    mapping = _mapping()
    audio, _, scheduled = _audio(_provider(FLAC, item_id="other"))

    await audio.get_stream_details(queue_item=_queue_item(_library_track(mapping)))

    assert scheduled == []


async def test_a_provider_item_is_not_written_back() -> None:
    """Only a library item has a mapping row to store the format on."""
    mapping = _mapping()
    audio, _, scheduled = _audio(_provider(FLAC))
    track = Track(item_id=ITEM_ID, provider=INSTANCE, name="15 Step", provider_mappings={mapping})

    await audio.get_stream_details(queue_item=_queue_item(track))

    assert scheduled == []


async def test_the_format_is_written_once_per_media_item() -> None:
    """A second selection over the same media item ranks on the format without writing it."""
    track = _library_track(_mapping())
    audio, _, scheduled = _audio(_provider(FLAC))

    await audio.get_stream_details(queue_item=_queue_item(track))
    await audio.get_stream_details(queue_item=_queue_item(track))

    assert len(scheduled) == 1
    await scheduled[0]


async def test_a_mapping_removed_meanwhile_does_not_fail_the_write() -> None:
    """A mapping gone by the time the write runs is a no-op, not an error."""
    audio, update_provider_mapping, scheduled = _audio(_provider(FLAC))
    update_provider_mapping.side_effect = MediaNotFoundError("gone")

    await audio.get_stream_details(queue_item=_queue_item(_library_track(_mapping())))

    await scheduled[0]
