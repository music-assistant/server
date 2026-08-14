"""
Tests that resolving streamdetails asks each provider mapping at most once.

``get_stream_details`` walks the provider mappings twice: the first pass is limited to the
providers the user's provider filter steers to, the second widens to the rest. Without a
provider filter every mapping counts as preferred, so both passes cover the same set and a
mapping that failed would be asked again -- doubling the cost of every failure, which for a
just-in-time renderer like AI Radio means a second full text-to-speech render.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio

if TYPE_CHECKING:
    from collections.abc import Callable

INSTANCE = "ai_radio--abc"
OTHER_INSTANCE = "tidal--xyz"
ITEM_ID = "session123_0"


def _queue_item(*instances: str) -> QueueItem:
    """Build a queue item whose media item maps to each of the given provider instances."""
    media_item = SoundEffect(
        item_id=ITEM_ID,
        provider=instances[0],
        name="Intro",
        provider_mappings={
            ProviderMapping(
                item_id=ITEM_ID,
                provider_domain=instance.split("--")[0],
                provider_instance=instance,
            )
            for instance in instances
        },
    )
    return QueueItem(
        queue_id="q1",
        queue_item_id="qi1",
        name="Intro",
        duration=None,
        media_item=media_item,
    )


def _streamdetails(item_id: str, media_type: MediaType, provider: str) -> StreamDetails:
    """Build the streamdetails a healthy provider would hand back."""
    return StreamDetails(
        provider=provider,
        item_id=item_id,
        audio_format=AudioFormat(content_type=ContentType.MP3),
        media_type=media_type,
        stream_type=StreamType.HTTP,
        path="http://localhost/clip.mp3",
        duration=45,
    )


def _audio(
    providers: dict[str, Callable[..., object]], provider_filter: list[str] | None = None
) -> StreamsAudio:
    """
    Build a StreamsAudio whose mass resolves the given provider instances.

    :param providers: The provider instances the mass should hand back, by instance id.
    :param provider_filter: The playback user's provider steering, omit for no playback user
        (which makes every mapping on the item count as preferred).
    """
    mass = MagicMock()
    mass.get_provider.side_effect = lambda instance: providers.get(instance)
    mass.player_queues.queue_data_or_none.return_value = (
        MagicMock(userid="user1") if provider_filter else None
    )
    mass.webserver.auth.get_user = AsyncMock(
        return_value=MagicMock(provider_filter=provider_filter) if provider_filter else None
    )
    mass.streams.get_config_value.return_value = -17
    return StreamsAudio(mass)


async def test_a_failing_provider_is_asked_only_once() -> None:
    """A mapping that fails is not asked again by the widening pass."""
    calls: list[str] = []

    async def _fail(item_id: str, _media_type: MediaType) -> StreamDetails:
        calls.append(item_id)
        raise MediaNotFoundError(f"clip {item_id} failed TTS")

    provider = MagicMock()
    provider.get_stream_details = _fail
    audio = _audio({INSTANCE: provider})

    with pytest.raises(MediaNotFoundError):
        await audio.get_stream_details(queue_item=_queue_item(INSTANCE))

    assert calls == [ITEM_ID]


async def test_the_widening_pass_still_reaches_a_provider_the_filter_held_back() -> None:
    """A mapping the steering skipped in the first pass is tried by the second."""
    calls: list[str] = []

    async def _fail(item_id: str, _media_type: MediaType) -> StreamDetails:
        calls.append(INSTANCE)
        raise MediaNotFoundError(f"clip {item_id} failed TTS")

    async def _succeed(item_id: str, media_type: MediaType) -> StreamDetails:
        calls.append(OTHER_INSTANCE)
        return _streamdetails(item_id, media_type, OTHER_INSTANCE)

    failing = MagicMock()
    failing.get_stream_details = _fail
    working = MagicMock()
    working.get_stream_details = _succeed
    # steer to the failing instance so the working one is only reachable via the second pass
    audio = _audio({INSTANCE: failing, OTHER_INSTANCE: working}, provider_filter=[INSTANCE])

    streamdetails = await audio.get_stream_details(queue_item=_queue_item(INSTANCE, OTHER_INSTANCE))

    assert streamdetails.provider == OTHER_INSTANCE
    assert calls == [INSTANCE, OTHER_INSTANCE]
