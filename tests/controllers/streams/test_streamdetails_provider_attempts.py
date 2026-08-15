"""
Tests that resolving streamdetails asks each provider mapping at most once.

``get_stream_details`` walks the provider mappings twice: the first pass is limited to the
providers the user's provider filter steers to, the second widens to the rest. Without a
provider filter every mapping counts as preferred, so both passes cover the same set and a
mapping that failed would be asked again -- doubling the cost of every failure, which for a
just-in-time renderer like AI Radio means a second full text-to-speech render.

The mappings below are given distinct qualities wherever order matters, so the pass the
loop reaches them in is fixed rather than left to the iteration order of a set.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ProviderMapping, SoundEffect
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio

INSTANCE = "ai_radio--abc"
OTHER_INSTANCE = "tidal--xyz"
ITEM_ID = "session123_0"


def _mapping(
    instance: str, item_id: str = ITEM_ID, content_type: ContentType = ContentType.MP3
) -> ProviderMapping:
    """
    Build a provider mapping.

    :param instance: The provider instance the mapping points at.
    :param item_id: The item id on that provider.
    :param content_type: Drives the mapping's quality score, which decides the order the
        mappings are tried in. Pass a lossless type to have a mapping tried first.
    """
    return ProviderMapping(
        item_id=item_id,
        provider_domain=instance.split("--", maxsplit=1)[0],
        provider_instance=instance,
        audio_format=AudioFormat(content_type=content_type),
    )


def _queue_item(*mappings: ProviderMapping) -> QueueItem:
    """Build a queue item whose media item carries the given provider mappings."""
    media_item = SoundEffect(
        item_id=ITEM_ID,
        provider=mappings[0].provider_instance,
        name="Intro",
        provider_mappings=set(mappings),
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
    providers: dict[str, MagicMock], provider_filter: list[str] | None = None
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
        await audio.get_stream_details(queue_item=_queue_item(_mapping(INSTANCE)))

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

    streamdetails = await audio.get_stream_details(
        # the steered mapping also sorts first, so a repeat of it would land before the
        # widened one rather than depending on how the mapping set happens to iterate
        queue_item=_queue_item(
            _mapping(INSTANCE, content_type=ContentType.FLAC), _mapping(OTHER_INSTANCE)
        )
    )

    assert streamdetails.provider == OTHER_INSTANCE
    assert calls == [INSTANCE, OTHER_INSTANCE]


async def test_two_mappings_on_one_provider_are_both_attempted() -> None:
    """One provider carrying two items for the media item still gets asked for each."""
    calls: list[str] = []

    async def _by_item_id(item_id: str, media_type: MediaType) -> StreamDetails:
        calls.append(item_id)
        if item_id == "bad":
            raise MediaNotFoundError(f"clip {item_id} failed TTS")
        return _streamdetails(item_id, media_type, INSTANCE)

    provider = MagicMock()
    provider.get_stream_details = _by_item_id
    audio = _audio({INSTANCE: provider})

    streamdetails = await audio.get_stream_details(
        # the failing mapping sorts first, so the good one is only reached by carrying on
        # through the mappings rather than by a repeat attempt
        queue_item=_queue_item(
            _mapping(INSTANCE, item_id="bad", content_type=ContentType.FLAC),
            _mapping(INSTANCE, item_id="good"),
        )
    )

    assert streamdetails.item_id == "good"
    assert calls == ["bad", "good"]
