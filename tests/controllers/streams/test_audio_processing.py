"""Tests for runtime audio processing plans and snapshots."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.audio_processing import (
    AudioChannelMode,
    AudioFidelity,
    AudioInputDetails,
    AudioLimiterDetails,
    AudioOutputPath,
    AudioProcessingState,
    AudioQuality,
)
from music_assistant_models.dsp import DSPConfig, DSPState, ToneControlFilter
from music_assistant_models.enums import ContentType, MediaType, VolumeNormalizationMode
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_processing import (
    AudioProcessingRegistry,
    get_audio_quality,
    get_normalization_details,
)


@pytest.mark.parametrize(
    ("audio_format", "expected"),
    [
        (
            AudioFormat(
                content_type=ContentType.FLAC,
                codec_type=ContentType.FLAC,
                sample_rate=44100,
                bit_depth=16,
            ),
            AudioQuality.LOSSLESS,
        ),
        (
            AudioFormat(
                content_type=ContentType.FLAC,
                codec_type=ContentType.FLAC,
                sample_rate=96000,
                bit_depth=24,
            ),
            AudioQuality.HI_RES,
        ),
        (
            AudioFormat(
                content_type=ContentType.MP3,
                codec_type=ContentType.MP3,
                bit_rate=320,
            ),
            AudioQuality.STANDARD,
        ),
        (
            AudioFormat(
                content_type=ContentType.AAC,
                codec_type=ContentType.AAC,
                bit_rate=128,
            ),
            AudioQuality.LOW,
        ),
        (
            AudioFormat(
                content_type=ContentType.MP3,
                codec_type=ContentType.MP3,
                bit_rate=128000,
            ),
            AudioQuality.LOW,
        ),
        (
            AudioFormat(
                content_type=ContentType.AAC,
                codec_type=ContentType.AAC,
                bit_rate=None,
            ),
            AudioQuality.UNKNOWN,
        ),
    ],
)
def test_get_audio_quality(audio_format: AudioFormat, expected: AudioQuality) -> None:
    """Audio quality classification uses codec, resolution and bitrate facts."""
    assert get_audio_quality(audio_format) == expected


def test_get_normalization_details_uses_album_measurement() -> None:
    """Album normalization reports the selected measurement and applied gain."""
    streamdetails = _streamdetails()
    streamdetails.volume_normalization_mode = VolumeNormalizationMode.MEASUREMENT_ONLY
    streamdetails.prefer_album_loudness = True
    streamdetails.loudness = -12.0
    streamdetails.loudness_album = -14.5
    streamdetails.target_loudness = -17.0

    details = get_normalization_details(streamdetails, applied_gain_db=-2.5)

    assert details is not None
    assert details.measurement_source.value == "album"
    assert details.measured_lufs == -14.5
    assert details.target_lufs == -17.0
    assert details.applied_gain_db == -2.5


def test_get_dynamic_normalization_details() -> None:
    """Dynamic normalization reports its live targets without a static gain."""
    streamdetails = _streamdetails()
    streamdetails.volume_normalization_mode = VolumeNormalizationMode.DYNAMIC
    streamdetails.target_loudness = -17.0

    details = get_normalization_details(streamdetails, applied_gain_db=None)

    assert details is not None
    assert details.measurement_source.value == "live"
    assert details.target_true_peak_dbtp == -2.0
    assert details.target_loudness_range_lu == 10.0
    assert details.applied_gain_db is None


def test_audio_processing_registry_groups_outputs() -> None:
    """Snapshots group identical paths and summarize mixed output quality."""
    registry, _mass, _queue_data, output_path, lossy_output_path = _registry_context()
    assert registry.update_output(
        "player-2",
        output_path,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    assert registry.update_output(
        "player-1",
        output_path,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    assert registry.update_output(
        "player-3",
        lossy_output_path,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    snapshot = registry.get("queue-1")

    assert snapshot is not None
    assert snapshot.state == AudioProcessingState.READY
    assert snapshot.outputs[0].player_ids == ["player-1", "player-2"]
    assert snapshot.outputs[0].fidelity == AudioFidelity(
        quality=AudioQuality.HI_RES,
        bit_perfect=True,
    )
    assert snapshot.outputs[1].player_ids == ["player-3"]
    assert snapshot.outputs[1].fidelity == AudioFidelity(
        quality=AudioQuality.LOW,
        bit_perfect=False,
    )
    assert snapshot.fidelity is not None
    assert snapshot.fidelity.min_output_quality == AudioQuality.LOW
    assert snapshot.fidelity.max_output_quality == AudioQuality.HI_RES


def test_registry_detects_bitrate_changes_without_using_prefetched_output() -> None:
    """Serialized format changes update fidelity without replacing the current item."""
    registry, _mass, _queue_data, output_path, lossy_output_path = _registry_context()
    registry.update_output(
        "player-1",
        output_path,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    registry.update_output(
        "player-3",
        lossy_output_path,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    assert lossy_output_path.output_format is not None
    lossy_output_path.output_format.bit_rate = 320
    assert registry.update_output(
        "player-3",
        lossy_output_path,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    updated_snapshot = registry.get("queue-1")
    assert updated_snapshot is not None
    assert updated_snapshot.outputs[1].fidelity.quality == AudioQuality.STANDARD
    assert updated_snapshot.fidelity is not None
    assert updated_snapshot.fidelity.min_output_quality == AudioQuality.STANDARD
    assert registry.update_output(
        "player-1",
        lossy_output_path,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-2",
    )
    current_snapshot = registry.get("queue-1")
    assert current_snapshot is not None
    assert current_snapshot.outputs[0].fidelity.quality == AudioQuality.HI_RES


def test_hidden_fade_in_prevents_bit_perfect_claim() -> None:
    """A fade-in affects fidelity without becoming a public chain stage."""
    registry, _mass, _queue_data, output_path, _lossy_output_path = _registry_context()
    registry.update_output(
        "player-1",
        output_path,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    snapshot = registry.get("queue-1")
    assert snapshot is not None
    assert snapshot.input is not None
    assert snapshot.queue_processing is not None
    assert snapshot.queue_processing.input_format is not None
    assert snapshot.queue_processing.output_format is not None

    registry.update_item_context(
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
        input_details=snapshot.input,
        input_format=snapshot.queue_processing.input_format,
        output_format=snapshot.queue_processing.output_format,
        alters_audio=True,
    )

    updated = registry.get("queue-1")
    assert updated is not None
    assert updated.outputs[0].fidelity.bit_perfect is False


def test_intermediate_format_conversion_prevents_bit_perfect_claim() -> None:
    """A restored final resolution does not hide a lower-resolution handoff."""
    registry, _mass, _queue_data, output_path, _lossy_output_path = _registry_context()
    output_path.handoff_format = AudioFormat(
        content_type=ContentType.PCM_S24LE,
        sample_rate=48000,
        bit_depth=24,
        channels=2,
    )
    registry.update_output(
        "player-1",
        output_path,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    snapshot = registry.get("queue-1")

    assert snapshot is not None
    assert snapshot.outputs[0].fidelity.bit_perfect is False


def test_registry_rejects_superseded_and_cleared_sessions() -> None:
    """Late producers cannot update or recreate a replacement queue session."""
    registry, mass, queue_data, output_path, _lossy_output_path = _registry_context()
    registry.start_session("queue-1", "session-2")
    assert mass.signal_event.call_args.kwargs["data"] is None
    queue_data.session_id = "session-2"
    assert not registry.update_output(
        "stale-player",
        output_path,
        queue_id="queue-1",
        session_id="session-1",
    )
    assert registry.update_output(
        "current-player",
        output_path,
        queue_id="queue-1",
        session_id="session-2",
    )
    registry.clear("queue-1", session_id="session-1")
    assert registry.get_player_output("stale-player") is None
    registry.clear("queue-1", session_id="session-2")
    assert not registry.update_output(
        "late-player",
        output_path,
        queue_id="queue-1",
        session_id="session-2",
    )
    assert registry.get_player_output("late-player") is None


def test_player_output_plan_matches_ffmpeg_filters() -> None:
    """The typed output path describes the FFmpeg filters returned to callers."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    mass.config.get_player_dsp_config.return_value = DSPConfig(
        enabled=True,
        input_gain=-1.0,
        filters=[ToneControlFilter(enabled=True, bass_level=2.0)],
        output_gain=-0.5,
    )
    mass.config.get_raw_player_config_value.return_value = "left"
    audio = StreamsAudio(cast("Any", mass))
    input_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        codec_type=ContentType.PCM_F32LE,
        sample_rate=96000,
        bit_depth=32,
        channels=2,
    )
    output_format = AudioFormat(
        content_type=ContentType.FLAC,
        codec_type=ContentType.FLAC,
        sample_rate=48000,
        bit_depth=16,
        channels=1,
    )

    plan = audio.get_player_output_plan(
        "player-1",
        input_format,
        output_format,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert plan.filter_params[0] == "volume=-1.0dB"
    assert "pan=mono|c0=FL" in plan.filter_params
    assert plan.filter_params[-1] == "alimiter=limit=-2dB:level=false:asc=true"
    assert plan.output_path.dsp.state == DSPState.ENABLED
    assert plan.output_path.channels is not None
    assert plan.output_path.channels.mode == AudioChannelMode.LEFT
    assert plan.output_path.limiter == AudioLimiterDetails(
        enabled=True,
        threshold_dbfs=-2.0,
    )
    assert plan.output_path.resampling is not None
    assert plan.output_path.resampling.method.value in {"soxr", "swr"}
    assert plan.output_path.dithering is not None
    assert plan.output_path.output_format == output_format
    mass.streams.audio_processing.update_output.assert_called_once()


def test_protocol_output_is_registered_for_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A protocol transport contributes output details to its user-facing parent."""
    mass = MagicMock()
    player = MagicMock(player_id="protocol-1", protocol_parent_id="player-1")
    player.state.active_group = None
    player.state.synced_to = None
    mass.players.get_player.return_value = player
    mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    mass.config.get_raw_player_config_value.side_effect = lambda _player_id, _key, default: (
        False if isinstance(default, bool) else "left"
    )
    audio = StreamsAudio(cast("Any", mass))
    monkeypatch.setattr(audio, "_resolve_player_dsp_config", lambda _player: DSPConfig())
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE,
        codec_type=ContentType.PCM_S16LE,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
    )

    plan = audio.get_player_output_plan(
        "protocol-1",
        pcm_format,
        pcm_format,
        queue_id="queue-1",
        session_id="session-1",
    )

    assert plan.output_path.player_ids == ["player-1"]
    assert plan.output_path.channels is not None
    assert plan.output_path.channels.mode == AudioChannelMode.LEFT
    assert not plan.output_path.limiter.enabled
    assert {call.args[0] for call in mass.config.get_raw_player_config_value.call_args_list} == {
        "player-1"
    }
    assert mass.streams.audio_processing.update_output.call_args.args[0] == "player-1"


@pytest.mark.asyncio
async def test_protocol_output_format_uses_parent_channel_config() -> None:
    """Output format channel selection reads the user-facing parent config."""
    mass = MagicMock()
    mass.config.get_raw_player_config_value.return_value = "left"
    player = MagicMock(player_id="protocol-1", protocol_parent_id="player-1")
    player.get_supported_sample_rates.return_value = [(44100, 16)]
    audio = StreamsAudio(cast("Any", mass))

    output_format = await audio.get_output_format(
        "flac",
        player,
        content_sample_rate=44100,
        content_bit_depth=16,
    )

    assert output_format.channels == 1
    mass.config.get_raw_player_config_value.assert_called_once_with(
        "player-1",
        "output_channels",
        "stereo",
    )


@pytest.mark.asyncio
async def test_stale_flow_generator_does_not_mutate_active_session() -> None:
    """A deferred flow generator exits before clearing newer session state."""
    mass = MagicMock()
    queue_data = SimpleNamespace(
        session_id="session-2",
        flow_mode_stream_log=["current"],
    )
    mass.player_queues.queue_data.return_value = queue_data
    audio = StreamsAudio(cast("Any", mass))
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
    )
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_F32LE,
        sample_rate=48000,
        bit_depth=32,
        channels=2,
    )
    stream = audio.get_queue_flow_stream(
        cast("Any", queue),
        MagicMock(),
        pcm_format,
        session_id="session-1",
    )

    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    assert not queue.flow_mode
    assert queue_data.flow_mode_stream_log == ["current"]


def _registry_context() -> tuple[
    AudioProcessingRegistry,
    MagicMock,
    SimpleNamespace,
    AudioOutputPath,
    AudioOutputPath,
]:
    """Return a registry with one hi-res queue item and two output path templates."""
    mass = MagicMock()
    streamdetails = _streamdetails()
    queue_item = SimpleNamespace(queue_item_id="item-1", streamdetails=streamdetails)
    queue = SimpleNamespace(current_item=queue_item, next_item=None, current_index=0)
    queue_data = SimpleNamespace(session_id="session-1", items=[queue_item])
    mass.player_queues.get.return_value = queue
    mass.player_queues.queue_data_or_none.return_value = queue_data
    mass.streams.audio.get_stream_dsp_details.return_value = {}
    registry = AudioProcessingRegistry(mass)
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S24LE,
        codec_type=ContentType.PCM_S24LE,
        sample_rate=96000,
        bit_depth=24,
        channels=2,
    )
    registry.start_session("queue-1", "session-1")
    registry.update_item_context(
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
        input_details=AudioInputDetails(
            source_format=streamdetails.audio_format,
            server_input_format=streamdetails.audio_format,
            fidelity=AudioFidelity(quality=AudioQuality.HI_RES),
        ),
        input_format=pcm_format,
        output_format=pcm_format,
    )
    lossless_output = AudioOutputPath(
        input_format=pcm_format,
        limiter=AudioLimiterDetails(enabled=False),
        output_format=AudioFormat(
            content_type=ContentType.FLAC,
            codec_type=ContentType.FLAC,
            sample_rate=96000,
            bit_depth=24,
            channels=2,
        ),
    )
    lossy_output = AudioOutputPath(
        input_format=pcm_format,
        limiter=AudioLimiterDetails(enabled=False),
        output_format=AudioFormat(
            content_type=ContentType.MP3,
            codec_type=ContentType.MP3,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
            bit_rate=128,
        ),
    )
    return registry, mass, queue_data, lossless_output, lossy_output


def _streamdetails() -> StreamDetails:
    """Return hi-res lossless stream details."""
    return StreamDetails(
        provider="provider",
        item_id="track",
        audio_format=AudioFormat(
            content_type=ContentType.FLAC,
            codec_type=ContentType.FLAC,
            sample_rate=96000,
            bit_depth=24,
            channels=2,
            bit_rate=3200,
        ),
        media_type=MediaType.TRACK,
    )
