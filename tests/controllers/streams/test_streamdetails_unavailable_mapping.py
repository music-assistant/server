"""Tests for marking a library mapping unavailable when its provider no longer finds the item."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from music_assistant_models.media_items import ProviderMapping, Track
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

INSTANCE = "tidal--abc"
SIBLING_INSTANCE = "tidal--xyz"


def _mapping(item_id: str, instance: str = INSTANCE) -> ProviderMapping:
    """Build a mapping of the given item on the given provider instance."""
    return ProviderMapping(item_id=item_id, provider_domain="tidal", provider_instance=instance)


def _provider(instance: str, error: Exception | None = None) -> MagicMock:
    """Build a provider instance that raises the given error, or resolves streamdetails."""

    async def _get_stream_details(item_id: str, media_type: MediaType) -> StreamDetails:
        if error:
            raise error
        return StreamDetails(
            provider=instance,
            item_id=item_id,
            audio_format=MagicMock(),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path="http://localhost/track.flac",
        )

    provider = MagicMock(spec=MusicProvider)
    provider.instance_id = instance
    provider.get_stream_details = _get_stream_details
    return provider


def _audio() -> tuple[StreamsAudio, MagicMock, list[Coroutine[Any, Any, None]]]:
    """Build a StreamsAudio, handing back the mark mock and the background tasks scheduled."""
    mass = MagicMock()
    mark = AsyncMock()
    mass.music.mark_provider_mapping_unavailable = mark
    scheduled: list[Coroutine[Any, Any, None]] = []
    mass.create_task.side_effect = lambda coro, **_kwargs: scheduled.append(coro)
    return StreamsAudio(mass), mark, scheduled


async def test_a_dead_mapping_is_marked_and_the_next_one_plays() -> None:
    """A mapping its provider does not find is marked; the next candidate still serves."""
    dead, alive = _mapping("dead"), _mapping("alive")
    track = Track(item_id="42", provider="library", name="15 Step", provider_mappings={dead, alive})
    audio, mark, scheduled = _audio()

    streamdetails = await audio._request_streamdetails(
        [
            (dead, _provider(INSTANCE, MediaNotFoundError("gone"))),
            (alive, _provider(INSTANCE)),
        ],
        track,
    )

    assert streamdetails is not None
    assert streamdetails.item_id == "alive"
    assert len(scheduled) == 1
    await scheduled[0]
    mark.assert_awaited_once_with(track, dead)


async def test_a_sibling_instance_miss_does_not_mark_the_mapping() -> None:
    """Another account of the same service lacking the item says nothing about the mapping."""
    mapping = _mapping("item")
    track = Track(item_id="42", provider="library", name="15 Step", provider_mappings={mapping})
    audio, _, scheduled = _audio()

    await audio._request_streamdetails(
        [(mapping, _provider(SIBLING_INSTANCE, MediaNotFoundError("gone")))], track
    )

    assert scheduled == []


async def test_a_transient_error_does_not_mark_the_mapping() -> None:
    """A provider that is merely unreachable leaves the mapping available."""
    mapping = _mapping("item")
    track = Track(item_id="42", provider="library", name="15 Step", provider_mappings={mapping})
    audio, _, scheduled = _audio()

    await audio._request_streamdetails(
        [(mapping, _provider(INSTANCE, ProviderUnavailableError("offline")))], track
    )

    assert scheduled == []


@pytest.mark.parametrize(
    "error",
    [
        RateLimited("slow down", backoff_time=3600),
        RetriesExhausted("Not retrying, asked to wait 3600 seconds"),
    ],
)
async def test_a_provider_asking_to_back_off_surfaces_its_error(
    error: MusicAssistantError,
) -> None:
    """A rate limited or temporarily unavailable provider is reported, not taken for a miss."""
    mapping, other = _mapping("item"), _mapping("other")
    track = Track(item_id="42", provider="library", name="15 Step", provider_mappings={mapping})
    audio, _, scheduled = _audio()

    with pytest.raises(type(error)) as exc_info:
        await audio._request_streamdetails(
            [
                (mapping, _provider(INSTANCE, error)),
                # an actionable audio error of another candidate does not hide the back-off
                (other, _provider(SIBLING_INSTANCE, AudioError("not on this tier"))),
            ],
            track,
        )

    assert exc_info.value is error
    assert scheduled == []


async def test_a_track_its_provider_cannot_serve_is_a_miss() -> None:
    """A provider raising a temporary error for the item itself does not stop playback."""
    mapping = _mapping("item")
    track = Track(item_id="42", provider="library", name="15 Step", provider_mappings={mapping})
    audio, _, scheduled = _audio()

    streamdetails = await audio._request_streamdetails(
        [
            (
                mapping,
                _provider(INSTANCE, ResourceTemporarilyUnavailable("Track item is not available")),
            )
        ],
        track,
    )

    assert streamdetails is None
    assert scheduled == []


async def test_another_candidate_plays_while_a_provider_backs_off() -> None:
    """A rate limited provider does not keep another candidate from serving the item."""
    limited, alive = _mapping("limited"), _mapping("alive", SIBLING_INSTANCE)
    track = Track(item_id="42", provider="library", name="15 Step", provider_mappings={limited})
    audio, _, _ = _audio()

    streamdetails = await audio._request_streamdetails(
        [
            (limited, _provider(INSTANCE, RetriesExhausted("Not retrying"))),
            (alive, _provider(SIBLING_INSTANCE)),
        ],
        track,
    )

    assert streamdetails is not None
    assert streamdetails.item_id == "alive"
