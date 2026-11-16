"""Tests for which streams the AudioAnalysisController offers to each analysis provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.controllers.streams.audio_analysis as audio_analysis_mod
from music_assistant.controllers.streams.audio_analysis import AudioAnalysisController
from music_assistant.controllers.streams.audio_buffer import AudioBuffer
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
)
FILESYSTEM_ID = "filesystem_local--abc"


def _aa_provider(
    instance_id: str,
    media_types: frozenset[MediaType] = AudioAnalysisProvider.media_types,
) -> MagicMock:
    prov = MagicMock(spec=AudioAnalysisProvider)
    prov.instance_id = instance_id
    prov.name = instance_id
    prov.domain = instance_id
    prov.available = True
    prov.unloading = False
    prov.media_types = media_types
    prov.start_analysis = AsyncMock(return_value=True)
    prov.process_pcm_chunk = AsyncMock()
    prov.finalize = AsyncMock()
    prov.cancel = AsyncMock()
    return prov


def _streamdetails(media_type: MediaType) -> StreamDetails:
    return StreamDetails(
        provider=FILESYSTEM_ID,
        item_id="item-1",
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        media_type=media_type,
        stream_type=StreamType.LOCAL_FILE,
        path="/music/item-1.flac",
    )


@pytest.fixture
def registry() -> dict[str, MagicMock]:
    """Return the providers mass resolves by instance id, a test adds its analysis providers."""
    filesystem_prov = MagicMock(instance_id=FILESYSTEM_ID, available=True)
    return {FILESYSTEM_ID: filesystem_prov}


@pytest.fixture
def controller(registry: dict[str, MagicMock]) -> AudioAnalysisController:
    """Return a controller whose mass resolves the providers in the registry."""
    streams = MagicMock()
    mass = streams.mass

    # the analysis worker, finalize and idle monitor tasks are not under test here
    def _create_task(coro: Coroutine[Any, Any, Any]) -> MagicMock:
        coro.close()
        return MagicMock(spec=asyncio.Task)

    mass.create_task = MagicMock(side_effect=_create_task)
    mass.get_provider = MagicMock(side_effect=lambda pid, **_kw: registry.get(pid))
    controller = AudioAnalysisController(streams)
    controller._database_ready = True
    return controller


async def _play(
    controller: AudioAnalysisController, streamdetails: StreamDetails, *providers: MagicMock
) -> None:
    cast("MagicMock", controller.mass).get_providers = MagicMock(return_value=list(providers))
    await controller.start_analysis(AudioBuffer(PCM_FORMAT), streamdetails)


@pytest.mark.asyncio
async def test_provider_is_offered_declared_media_type(
    controller: AudioAnalysisController,
) -> None:
    """A provider is offered a media type it declared."""
    prov = _aa_provider("radio", frozenset({MediaType.RADIO}))

    await _play(controller, _streamdetails(MediaType.RADIO), prov)

    prov.start_analysis.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_is_not_offered_undeclared_media_type(
    controller: AudioAnalysisController,
) -> None:
    """A provider on the default (tracks) is not offered an audiobook."""
    prov = _aa_provider("tracks_only")

    await _play(controller, _streamdetails(MediaType.AUDIOBOOK), prov)

    prov.start_analysis.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_scan_feeds_only_providers_that_declared_tracks(
    controller: AudioAnalysisController,
    registry: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan streams a track only to the providers that declared tracks."""
    tracks = _aa_provider("tracks")
    radio_only = _aa_provider("radio_only", frozenset({MediaType.RADIO}))
    providers = [tracks, radio_only]
    registry.update({p.instance_id: p for p in providers})
    registry[FILESYSTEM_ID].get_stream_details = AsyncMock(
        return_value=_streamdetails(MediaType.TRACK)
    )
    monkeypatch.setattr(AudioAnalysisController, "providers", property(lambda _self: providers))
    monkeypatch.setattr(audio_analysis_mod, "BACKGROUND_PACE_INTERVAL_SECONDS_FLOOR", 0.0)
    monkeypatch.setattr(
        controller,
        "_find_candidates_missing_analysis",
        AsyncMock(
            return_value=[
                {
                    "item_id": "item-1",
                    "provider_instance": FILESYSTEM_ID,
                    "missing_domains": [p.domain for p in providers],
                }
            ]
        ),
    )
    chunks = [b"\x00" * 64] * 3

    async def _media_stream(
        _streamdetails: StreamDetails, _pcm_format: AudioFormat, **_kwargs: object
    ) -> AsyncGenerator[bytes]:
        for chunk in chunks:
            yield chunk

    cast("MagicMock", controller.mass).streams.audio.get_media_stream = _media_stream

    await controller._run_background_scan()

    tracks.start_analysis.assert_awaited_once()
    assert tracks.process_pcm_chunk.await_count == len(chunks)
    radio_only.start_analysis.assert_not_awaited()
    radio_only.process_pcm_chunk.assert_not_awaited()
