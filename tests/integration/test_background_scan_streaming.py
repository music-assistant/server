"""End-to-end integration test for the streaming background scan."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.controllers.streams.audio_analysis import AudioAnalysisController
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.providers.loudness_analysis.provider import (
    CONF_WRITE_REPLAYGAIN_TAGS,
    LoudnessAnalysisProvider,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.streamdetails import StreamDetails


FIXTURE_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio" / "short_test.flac"


async def _real_get_media_stream(
    sd: StreamDetails, pcm_format: AudioFormat, **_kwargs: object
) -> AsyncGenerator[bytes]:
    """
    Real-ffmpeg stand-in for mass.streams.audio.get_media_stream.

    Mirrors the wait-then-close pattern in audio.py:466-528 so close() doesn't
    hit the SIGINT path on Windows when the process is still running.
    """
    assert isinstance(sd.path, str)
    proc = FFMpeg(
        audio_input=sd.path,
        input_format=sd.audio_format,
        output_format=pcm_format,
        collect_log_history=True,
    )
    try:
        await proc.start()
        async for chunk in proc.iter_chunked(pcm_format.pcm_sample_size):
            yield chunk
        await proc.wait_with_timeout(5)
    finally:
        await proc.close()


@pytest.mark.skipif(not FIXTURE_AUDIO.exists(), reason="fixture FLAC missing")
async def test_streaming_background_scan_loudness_end_to_end() -> None:
    """
    Drive a real LoudnessAnalysisProvider through _run_background_streaming_for_track.

    Verifies:
    - An audio_analysis row is written (captured via mocked set_audio_analysis)
    - The analysis contains a plausible loudness value
    - The session is removed from _active_sessions
    """
    captured_rows: list[tuple[str, str, AudioAnalysisData]] = []

    async def _capture_set(**kwargs: object) -> None:
        captured_rows.append(
            (
                str(kwargs["item_id"]),
                str(kwargs["aa_provider_domain"]),
                kwargs["analysis"],  # type: ignore[arg-type]
            )
        )

    mass = MagicMock()
    mass.streams.audio_analysis.set_audio_analysis = AsyncMock(side_effect=_capture_set)
    mass.streams.audio_analysis.get_audio_analysis_version = AsyncMock(return_value=None)
    mass.streams.audio_analysis.get_audio_analysis = AsyncMock(return_value=None)

    manifest = MagicMock()
    manifest.domain = "loudness_analysis"
    config = MagicMock()
    config.instance_id = "loudness_analysis_test"
    config.values = {}
    # write_replaygain_tags=False so post_analysis is a no-op; log_level must be a valid string
    config.get_value = MagicMock(
        side_effect=lambda key: {
            CONF_LOG_LEVEL: "GLOBAL",
            CONF_WRITE_REPLAYGAIN_TAGS: False,
        }.get(key, "GLOBAL")
    )

    provider = LoudnessAnalysisProvider(mass, manifest, config, supported_features=set())
    # domain comes from manifest.domain, instance_id from config.instance_id (both already set)
    provider.available = True

    # Wire get_provider so the controller can look up the provider by instance_id
    mass.get_provider = MagicMock(return_value=provider)

    # create_task schedules real asyncio tasks so finalize can run
    created_tasks: list[asyncio.Task[None]] = []

    def _create_task(coro: object) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        created_tasks.append(task)
        return task

    mass.create_task = MagicMock(side_effect=_create_task)
    mass.logger.getChild = MagicMock(return_value=MagicMock())

    mass.streams.audio.get_media_stream = _real_get_media_stream

    streams = MagicMock()
    streams.mass = mass
    controller = AudioAnalysisController(streams)

    streamdetails = MagicMock()
    streamdetails.path = str(FIXTURE_AUDIO)
    streamdetails.uri = f"track://test/{FIXTURE_AUDIO.name}"
    streamdetails.audio_format = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=44100,
        bit_depth=16,
        channels=1,
    )
    streamdetails.item_id = "fixture-track"
    streamdetails.provider = "filesystem_local"
    streamdetails.media_type = MediaType.TRACK
    # None is not VolumeNormalizationMode.DISABLED, so loudness analysis proceeds
    streamdetails.volume_normalization_mode = None

    try:
        await controller._run_background_streaming_for_track(streamdetails, [provider])
        finalize_tasks = [
            task for task in created_tasks if task is not controller._idle_unload_task
        ]
        await asyncio.wait_for(asyncio.gather(*finalize_tasks), timeout=5)
    finally:
        await controller.close()

    # Assertions
    assert len(captured_rows) == 1, f"expected exactly one analysis row; got {len(captured_rows)}"
    item_id, aa_domain, analysis = captured_rows[0]
    assert item_id == "fixture-track"
    assert aa_domain == "loudness_analysis"
    assert analysis.loudness_integrated is not None, "loudness_integrated must be populated"
    measured = analysis.loudness_integrated
    assert -70.0 < measured < 0.0, (
        f"loudness {measured} LUFS is outside the plausible range (-70, 0)"
    )

    assert streamdetails.uri not in controller._active_sessions, (
        "session must be cleaned up from _active_sessions"
    )
