"""Tests for effective audio processing plans and stream details."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.audio_processing import (
    AudioDSPDetails,
    AudioFidelity,
    AudioNormalizationDetails,
    AudioOutputDetails,
    AudioProcessingChain,
    AudioQuality,
    AudioQueueProcessing,
)
from music_assistant_models.dsp import AudioChannel, DSPConfig, DSPState, ToneControlFilter
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    VolumeNormalizationMode,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_processing import (
    AudioOutputPlan,
    AudioProcessingManager,
    get_audio_quality,
    get_normalization_details,
)


def _format(
    content_type: ContentType,
    sample_rate: int = 44100,
    bit_depth: int = 16,
    *,
    channels: int = 2,
    bit_rate: int | None = None,
) -> AudioFormat:
    """Return an AudioFormat with matching container and codec."""
    return AudioFormat(
        content_type=content_type,
        codec_type=content_type,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        channels=channels,
        bit_rate=bit_rate,
    )


@pytest.mark.parametrize(
    ("audio_format", "expected"),
    [
        (_format(ContentType.FLAC, 44100, 16), AudioQuality.LOSSLESS),
        (_format(ContentType.FLAC, 96000, 24), AudioQuality.HI_RES),
        (_format(ContentType.MP3, bit_rate=320), AudioQuality.STANDARD),
        (_format(ContentType.AAC, bit_rate=128), AudioQuality.LOW),
        (_format(ContentType.MP3, bit_rate=128000), AudioQuality.LOW),
        (_format(ContentType.AAC), AudioQuality.UNKNOWN),
    ],
)
def test_get_audio_quality(audio_format: AudioFormat, expected: AudioQuality) -> None:
    """Quality classification uses codec, resolution and normalized bitrate."""
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


def test_audio_processing_manager_attaches_grouped_chain() -> None:
    """A complete chain is attached to StreamDetails with grouped outputs."""
    manager, _mass, _queue_data, streamdetails, lossless_plan, lossy_plan = _manager_context()
    assert streamdetails.audio_processing is None

    assert manager.update_output(
        "player-2",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    assert manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    assert manager.update_output(
        "player-3",
        lossy_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    chain = cast("AudioProcessingChain", streamdetails.audio_processing)
    assert chain.input_fidelity.quality == AudioQuality.HI_RES
    assert chain.queue_processing is not None
    assert chain.outputs[0].player_ids == ["player-1", "player-2"]
    assert chain.outputs[0].fidelity == AudioFidelity(
        quality=AudioQuality.HI_RES,
        bit_perfect=True,
    )
    assert chain.outputs[1].player_ids == ["player-3"]
    assert chain.outputs[1].fidelity == AudioFidelity(
        quality=AudioQuality.LOW,
        bit_perfect=False,
    )


def test_preset_identity_update_republishes_current_chain() -> None:
    """Preset updates follow a changed config owner even when output details match."""
    manager, mass, _queue_data, streamdetails, output_plan, _lossy_plan = _manager_context()
    output_plan.dsp_config_id = "old-config-player"
    output_plan.output_details.dsp.preset_id = "night"
    manager.update_output(
        "player-1",
        output_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    replacement = deepcopy(output_plan)
    replacement.dsp_config_id = "configured-player"
    assert manager.update_output(
        "player-1",
        replacement,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    mass.player_queues.signal_update.reset_mock()

    manager.update_player_dsp_preset("configured-player", None)

    assert streamdetails.audio_processing is not None
    assert streamdetails.audio_processing.outputs[0].dsp.preset_id is None
    mass.player_queues.signal_update.assert_called_once_with("queue-1")


def test_prefetched_output_does_not_replace_current_chain() -> None:
    """An output prepared for the next item does not change the current item."""
    manager, mass, queue_data, streamdetails, lossless_plan, lossy_plan = _manager_context()
    next_streamdetails = _streamdetails(item_id="item-2")
    next_item = SimpleNamespace(queue_item_id="item-2", streamdetails=next_streamdetails)
    queue_data.items.append(next_item)
    mass.player_queues.get_item.side_effect = lambda _queue_id, item_id: (
        next_item if item_id == "item-2" else queue_data.items[0]
    )
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    current_chain = streamdetails.audio_processing

    manager.update_item_context(
        "queue-1",
        "session-1",
        "item-2",
        AudioQueueProcessing(pcm_format=lossless_plan.input_format),
    )
    manager.update_output(
        "player-1",
        lossy_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-2",
    )

    assert streamdetails.audio_processing == current_chain
    assert next_streamdetails.audio_processing is not None
    assert next_streamdetails.audio_processing.outputs[0].fidelity.quality == AudioQuality.LOW


def test_context_refresh_preserves_runtime_normalization() -> None:
    """A second consumer does not erase normalization resolved at stream time."""
    manager, _mass, _queue_data, streamdetails, lossless_plan, _lossy_plan = _manager_context()
    normalization = AudioNormalizationDetails(
        mode=VolumeNormalizationMode.DYNAMIC,
        target_lufs=-17.0,
    )
    manager.update_item_runtime(
        "queue-1",
        "session-1",
        "item-1",
        input_format=lossless_plan.input_format,
        pcm_format=lossless_plan.input_format,
        normalization=normalization,
        playback_speed=1.0,
    )
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    manager.update_item_context(
        "queue-1",
        "session-1",
        "item-1",
        AudioQueueProcessing(
            pcm_format=lossless_plan.input_format,
            crossfade_mode=CrossfadeMode.SMART_CROSSFADE,
        ),
    )

    chain = cast("AudioProcessingChain", streamdetails.audio_processing)
    assert chain.queue_processing is not None
    assert chain.queue_processing.normalization == normalization
    assert chain.outputs[0].fidelity.bit_perfect is False


def test_manager_rejects_superseded_and_cleared_sessions() -> None:
    """Late producers cannot update or recreate a replacement queue session."""
    manager, mass, queue_data, streamdetails, lossless_plan, _lossy_plan = _manager_context()
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    assert streamdetails.to_dict()["audio_processing"] is not None

    mass.player_queues.signal_update.reset_mock()
    manager.start_session("queue-1", "session-2")
    queue_data.session_id = "session-2"
    assert streamdetails.to_dict()["audio_processing"] is None
    mass.player_queues.signal_update.assert_called_once_with("queue-1")
    assert not manager.update_output(
        "stale-player",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
    )

    manager.update_item_context(
        "queue-1",
        "session-2",
        "item-1",
        AudioQueueProcessing(pcm_format=lossless_plan.input_format),
    )
    manager.update_output(
        "current-player",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-2",
        queue_item_id="item-1",
    )
    assert streamdetails.to_dict()["audio_processing"] is not None
    mass.player_queues.signal_update.reset_mock()
    manager.clear("queue-1", "session-2")
    assert streamdetails.to_dict()["audio_processing"] is None
    mass.player_queues.signal_update.assert_called_once_with("queue-1")
    assert not manager.update_output(
        "late-player",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-2",
    )


def test_manager_prunes_played_item_chains() -> None:
    """Advancing the queue drops processing state from completed items."""
    manager, mass, queue_data, streamdetails, lossless_plan, _lossy_plan = _manager_context()
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    assert streamdetails.audio_processing is not None
    next_streamdetails = _streamdetails(item_id="item-2")
    next_item = SimpleNamespace(queue_item_id="item-2", streamdetails=next_streamdetails)
    queue_data.items.append(next_item)
    queue_data.queue.current_index = 1
    queue_data.queue.current_item = next_item
    mass.player_queues.get_item.side_effect = lambda _queue_id, item_id: (
        next_item if item_id == "item-2" else queue_data.items[0]
    )

    manager.update_item_context(
        "queue-1",
        "session-1",
        "item-2",
        AudioQueueProcessing(pcm_format=lossless_plan.input_format),
    )
    manager.update_item_runtime(
        "queue-1",
        "session-1",
        "item-1",
        input_format=lossless_plan.input_format,
        pcm_format=lossless_plan.input_format,
        normalization=None,
        playback_speed=1.0,
    )

    assert streamdetails.audio_processing is None


def test_hidden_and_intermediate_processing_prevents_bit_perfect_claim() -> None:
    """Hidden fades and lower-resolution handoffs prevent bit-perfect output."""
    manager, _mass, _queue_data, streamdetails, output_plan, _lossy_plan = _manager_context(
        alters_audio=True
    )
    output_plan.handoff_format = _format(ContentType.PCM_S24LE, 48000, 24)

    manager.update_output(
        "player-1",
        output_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert streamdetails.audio_processing is not None
    assert streamdetails.audio_processing.outputs[0].fidelity.bit_perfect is False


def test_player_output_plan_matches_ffmpeg_filters() -> None:
    """Typed output details describe the FFmpeg filters returned to callers."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    mass.config.get_player_dsp_config.return_value = DSPConfig(
        enabled=True,
        input_gain=-1.0,
        filters=[ToneControlFilter(enabled=True, bass_level=2.0)],
        output_gain=-0.5,
        preset_id="night",
    )
    mass.config.get_raw_player_config_value.return_value = "left"
    audio = StreamsAudio(cast("Any", mass))
    input_format = _format(ContentType.PCM_F32LE, 96000, 32)
    output_format = _format(ContentType.FLAC, 48000, 16, channels=1)

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
    assert plan.output_details.dsp == AudioDSPDetails(
        state=DSPState.ENABLED,
        input_gain=-1.0,
        filters=[ToneControlFilter(enabled=True, bass_level=2.0)],
        output_gain=-0.5,
        output_limiter=True,
        preset_id="night",
    )
    assert plan.dsp_config_id == "player-1"
    assert plan.output_details.source_channel == AudioChannel.FL
    assert plan.output_details.output_format == output_format
    mass.streams.audio_processing.update_output.assert_called_once()


def test_protocol_output_uses_parent_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Protocol output details use the user-facing parent configuration."""
    mass = MagicMock()
    player = MagicMock(player_id="protocol-1", protocol_parent_id="player-1")
    player.state.active_group = None
    player.state.synced_to = None
    mass.players.get_player.return_value = player
    mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    mass.config.get_raw_player_config_value.side_effect = lambda _player_id, _key, default: (
        False if isinstance(default, bool) else "right"
    )
    audio = StreamsAudio(cast("Any", mass))
    monkeypatch.setattr(
        audio,
        "_resolve_player_dsp_config",
        lambda _player: DSPConfig(preset_id="parent-preset"),
    )
    pcm_format = _format(ContentType.PCM_S16LE, 44100, 16)

    plan = audio.get_player_output_plan(
        "protocol-1",
        pcm_format,
        pcm_format,
        queue_id="queue-1",
        session_id="session-1",
    )

    assert plan.output_details.player_ids == ["player-1"]
    assert plan.output_details.dsp.preset_id == "parent-preset"
    assert plan.dsp_config_id == "player-1"
    assert plan.output_details.source_channel == AudioChannel.FR
    assert not plan.output_details.dsp.output_limiter
    assert {call.args[0] for call in mass.config.get_raw_player_config_value.call_args_list} == {
        "player-1"
    }


def test_single_member_group_uses_child_dsp_preset() -> None:
    """A single-member player group reports the child's effective preset."""
    mass = MagicMock()
    player = MagicMock(player_id="group-1", protocol_parent_id=None)
    player.provider.domain = "player_group"
    player.state.active_group = None
    player.state.synced_to = None
    player.state.group_members = ["child-1"]
    player.state.supported_features = set()
    child = MagicMock(player_id="child-1")
    mass.players.get_player.side_effect = lambda player_id: (
        player if player_id == "group-1" else child
    )
    mass.config.get_player_dsp_config.side_effect = lambda player_id: (
        DSPConfig(enabled=True, preset_id="child-preset")
        if player_id == "child-1"
        else DSPConfig(enabled=False)
    )
    mass.config.get_raw_player_config_value.return_value = "stereo"
    audio = StreamsAudio(cast("Any", mass))
    pcm_format = _format(ContentType.PCM_F32LE, 48000, 32)

    plan = audio.get_player_output_plan("group-1", pcm_format, pcm_format)

    assert plan.dsp_config_id == "child-1"
    assert plan.output_details.dsp.state == DSPState.ENABLED
    assert plan.output_details.dsp.preset_id == "child-preset"


def test_unsupported_group_preserves_configured_preset() -> None:
    """Runtime DSP suppression retains the selected preset identity."""
    mass = MagicMock()
    player = MagicMock(player_id="leader-1", protocol_parent_id=None)
    player.provider.domain = "test"
    player.state.active_group = None
    player.state.synced_to = None
    player.state.group_members = ["child-1"]
    player.state.supported_features = set()
    mass.players.get_player.return_value = player
    mass.config.get_player_dsp_config.side_effect = lambda _player_id: DSPConfig(
        enabled=True,
        preset_id="group-preset",
    )
    mass.config.get_raw_player_config_value.return_value = "stereo"
    audio = StreamsAudio(cast("Any", mass))
    pcm_format = _format(ContentType.PCM_F32LE, 48000, 32)

    plan = audio.get_player_output_plan("leader-1", pcm_format, pcm_format)

    assert plan.dsp_config_id == "leader-1"
    assert plan.output_details.dsp.state == DSPState.DISABLED_BY_UNSUPPORTED_GROUP
    assert plan.output_details.dsp.preset_id == "group-preset"


@pytest.mark.asyncio
async def test_stale_flow_generator_does_not_mutate_active_session() -> None:
    """A deferred flow generator exits before clearing newer session state."""
    mass = MagicMock()
    queue_data = SimpleNamespace(session_id="session-2", flow_mode_stream_log=["current"])
    mass.player_queues.queue_data.return_value = queue_data
    audio = StreamsAudio(cast("Any", mass))
    queue = SimpleNamespace(queue_id="queue-1", display_name="Queue", flow_mode=False)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue),
        MagicMock(),
        _format(ContentType.PCM_F32LE, 48000, 32),
        session_id="session-1",
    )

    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    assert not queue.flow_mode
    assert queue_data.flow_mode_stream_log == ["current"]


def _manager_context(
    *,
    alters_audio: bool = False,
) -> tuple[
    AudioProcessingManager,
    MagicMock,
    SimpleNamespace,
    StreamDetails,
    AudioOutputPlan,
    AudioOutputPlan,
]:
    """Return one prepared queue item and two output plan templates."""
    mass = MagicMock()
    streamdetails = _streamdetails()
    queue_item = SimpleNamespace(queue_item_id="item-1", streamdetails=streamdetails)
    queue = SimpleNamespace(
        queue_id="queue-1",
        current_item=queue_item,
        next_item=None,
        current_index=0,
    )
    queue_data = SimpleNamespace(session_id="session-1", items=[queue_item], queue=queue)
    mass.player_queues.get.return_value = queue
    mass.player_queues.get_active_queue.return_value = queue
    mass.player_queues.get_item.return_value = queue_item
    mass.player_queues.queue_data_or_none.return_value = queue_data
    mass.streams.audio.get_stream_dsp_details.return_value = {}
    manager = AudioProcessingManager(mass)
    pcm_format = _format(ContentType.PCM_S24LE, 96000, 24)
    manager.start_session("queue-1", "session-1")
    manager.update_item_context(
        "queue-1",
        "session-1",
        "item-1",
        AudioQueueProcessing(pcm_format=pcm_format),
        alters_audio=alters_audio,
    )
    lossless_plan = AudioOutputPlan(
        filter_params=[],
        output_details=AudioOutputDetails(
            dsp=AudioDSPDetails(state=DSPState.DISABLED),
            output_format=_format(ContentType.FLAC, 96000, 24),
        ),
        input_format=pcm_format,
    )
    lossy_plan = AudioOutputPlan(
        filter_params=[],
        output_details=AudioOutputDetails(
            dsp=AudioDSPDetails(state=DSPState.DISABLED),
            output_format=_format(ContentType.MP3, 48000, 16, bit_rate=128),
        ),
        input_format=pcm_format,
    )
    return manager, mass, queue_data, streamdetails, lossless_plan, lossy_plan


def _streamdetails(item_id: str = "item-1") -> StreamDetails:
    """Return hi-res lossless stream details."""
    return StreamDetails(
        provider="provider",
        item_id=item_id,
        audio_format=_format(ContentType.FLAC, 96000, 24, bit_rate=3200),
        media_type=MediaType.TRACK,
    )
