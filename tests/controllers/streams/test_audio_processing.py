"""Tests for effective audio processing plans and stream details."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.audio_processing import (
    ActiveSourceAudioDetails,
    AudioDSPDetails,
    AudioFidelity,
    AudioNormalizationDetails,
    AudioOutputDetails,
    AudioProcessingChain,
    AudioQuality,
    AudioQueueProcessing,
)
from music_assistant_models.dsp import (
    AudioChannel,
    ConvolutionFilter,
    DSPConfig,
    DSPState,
    ToneControlFilter,
)
from music_assistant_models.enums import (
    ContentType,
    CrossfadeMode,
    MediaType,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import QueueEmpty
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.controllers.streams.audio_processing import (
    AudioOutputPlan,
    AudioProcessingManager,
    get_audio_quality,
    get_normalization_details,
)
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.helpers.dsp import ComplexFilter


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


def test_lossy_source_can_have_bit_perfect_lossless_output() -> None:
    """Lossy source quality does not prevent preserving its decoded PCM samples."""
    manager, _mass, _queue_data, streamdetails, lossless_plan, lossy_plan = _manager_context()
    streamdetails.audio_format = AudioFormat(
        content_type=ContentType.OGG,
        codec_type=ContentType.VORBIS,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        bit_rate=320,
    )
    pcm_format = _format(ContentType.PCM_S16LE)
    manager.update_item_runtime(
        "queue-1",
        "session-1",
        "item-1",
        input_format=pcm_format,
        pcm_format=pcm_format,
        normalization=None,
        playback_speed=1.0,
    )
    lossless_plan.input_format = pcm_format
    lossless_plan.output_details.output_format = _format(ContentType.FLAC)
    lossy_plan.input_format = pcm_format
    lossy_plan.output_details.output_format = _format(ContentType.MP3, bit_rate=320)

    manager.update_output(
        "lossless-player",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    manager.update_output(
        "lossy-player",
        lossy_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    chain = cast("AudioProcessingChain", streamdetails.audio_processing)
    outputs = {output.player_ids[0]: output for output in chain.outputs}
    assert chain.input_fidelity.quality == AudioQuality.STANDARD
    assert outputs["lossless-player"].fidelity == AudioFidelity(
        quality=AudioQuality.STANDARD,
        bit_perfect=True,
    )
    assert outputs["lossy-player"].fidelity == AudioFidelity(
        quality=AudioQuality.STANDARD,
        bit_perfect=False,
    )
    serialized = streamdetails.to_dict()
    serialized_outputs = {
        output["player_ids"][0]: output for output in serialized["audio_processing"]["outputs"]
    }
    assert serialized_outputs["lossless-player"]["fidelity"]["bit_perfect"] is True


def test_a_wider_provider_handoff_preserves_the_source_samples() -> None:
    """A provider that decodes upstream into wider PCM does not lose the source samples."""
    streamdetails = _source_handled_soloist_item(_format(ContentType.FLAC, 44100, 24))

    chain = cast("AudioProcessingChain", streamdetails.audio_processing)
    assert chain.input_fidelity.quality == AudioQuality.HI_RES
    assert chain.outputs[0].fidelity.bit_perfect is True


def test_an_output_narrower_than_the_source_is_not_bit_perfect() -> None:
    """Dropping a 24-bit source to a 16-bit output loses bits, wide handoff or not."""
    streamdetails = _source_handled_soloist_item(_format(ContentType.FLAC, 44100, 16))

    chain = cast("AudioProcessingChain", streamdetails.audio_processing)
    assert chain.outputs[0].fidelity.bit_perfect is False


def test_an_internal_stage_narrower_than_the_source_is_not_bit_perfect() -> None:
    """A narrowed internal stage loses bits the output cannot bring back."""
    manager, _mass, _queue_data, streamdetails, lossless_plan, _lossy_plan = _manager_context()
    streamdetails.audio_format = _format(ContentType.FLAC, 44100, 24)
    narrowed = _format(ContentType.PCM_S16LE, 44100, 16)
    manager.update_item_runtime(
        "queue-1",
        "session-1",
        "item-1",
        input_format=narrowed,
        pcm_format=narrowed,
        normalization=None,
        playback_speed=1.0,
    )
    lossless_plan.input_format = narrowed
    lossless_plan.output_details.output_format = _format(ContentType.FLAC, 44100, 24)
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    chain = cast("AudioProcessingChain", streamdetails.audio_processing)
    assert chain.outputs[0].fidelity.bit_perfect is False


def test_float_headroom_alone_does_not_break_the_bit_perfect_claim() -> None:
    """DSP enabled with no filters gets F32 headroom but leaves the samples alone."""
    manager, _mass, _queue_data, streamdetails, lossless_plan, _lossy_plan = _manager_context()
    streamdetails.audio_format = _format(ContentType.FLAC, 44100, 24)
    headroom = _format(ContentType.PCM_F32LE, 44100, 32)
    manager.update_item_runtime(
        "queue-1",
        "session-1",
        "item-1",
        input_format=headroom,
        pcm_format=headroom,
        normalization=None,
        playback_speed=1.0,
    )
    lossless_plan.input_format = headroom
    lossless_plan.output_details.dsp = AudioDSPDetails(state=DSPState.ENABLED)
    lossless_plan.output_details.output_format = _format(ContentType.FLAC, 44100, 24)
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    chain = cast("AudioProcessingChain", streamdetails.audio_processing)
    assert chain.outputs[0].fidelity.bit_perfect is True


def test_shared_output_destinations_are_registered_atomically() -> None:
    """One shared output publishes all destinations in a single queue update."""
    manager, mass, _queue_data, streamdetails, output_plan, _lossy_plan = _manager_context()
    mass.player_queues.signal_update.reset_mock()

    assert manager.update_output(
        "leader",
        output_plan,
        shared_player_ids={"leader", "sync-child"},
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert streamdetails.audio_processing is not None
    assert len(streamdetails.audio_processing.outputs) == 1
    assert streamdetails.audio_processing.outputs[0].player_ids == ["leader", "sync-child"]
    mass.player_queues.signal_update.assert_called_once_with("queue-1")

    mass.player_queues.signal_update.reset_mock()
    assert not manager.update_output(
        "leader",
        output_plan,
        shared_player_ids={"sync-child"},
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    mass.player_queues.signal_update.assert_not_called()


def test_live_source_context_publishes_input_and_source_processing() -> None:
    """A live source publishes its input and the processing it applies itself."""
    manager, mass, source_session, _lossless_plan, pcm_format = _source_manager_context()

    manager.update_source_context(
        "source-player",
        "source-session",
        pcm_format=pcm_format,
        crossfade_enabled=True,
        volume_normalization_enabled=True,
    )

    details = cast(
        "ActiveSourceAudioDetails | None",
        source_session.active_source_audio,
    )
    assert details is not None
    assert details.input_format == source_session.streamdetails.audio_format
    assert details.input_fidelity.quality == AudioQuality.HI_RES
    assert details.crossfade_mode is CrossfadeMode.SOURCE
    assert details.volume_normalization_mode is VolumeNormalizationMode.SOURCE
    assert details.outputs == []
    mass.players.trigger_player_update.assert_called_once_with("source-player")


def test_live_source_output_registered_before_context_is_published() -> None:
    """An output prepared before source details arrive is retained and published."""
    manager, _mass, source_session, lossless_plan, pcm_format = _source_manager_context()

    assert manager.update_output(
        "player-1",
        lossless_plan,
        shared_player_ids={"player-2"},
        queue_id="source-player",
        session_id="source-session",
    )
    assert source_session.active_source_audio is None

    manager.update_source_context(
        "source-player",
        "source-session",
        pcm_format=pcm_format,
        crossfade_enabled=False,
        volume_normalization_enabled=None,
    )

    details = cast(
        "ActiveSourceAudioDetails | None",
        source_session.active_source_audio,
    )
    assert details is not None
    assert details.crossfade_mode is CrossfadeMode.DISABLED
    assert details.volume_normalization_mode is VolumeNormalizationMode.UNKNOWN
    assert len(details.outputs) == 1
    assert details.outputs[0].player_ids == ["player-1", "player-2"]
    assert details.outputs[0].output_format == lossless_plan.output_details.output_format
    assert details.outputs[0].fidelity.quality == AudioQuality.HI_RES
    assert details.outputs[0].fidelity.bit_perfect is True


def test_a_source_that_crossfades_itself_stays_bit_perfect() -> None:
    """A fade the source mixed itself reaches us already mixed, so nothing is lost."""
    manager, _mass, source_session, lossless_plan, pcm_format = _source_manager_context()
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="source-player",
        session_id="source-session",
    )

    manager.update_source_context(
        "source-player",
        "source-session",
        pcm_format=pcm_format,
        crossfade_enabled=True,
        volume_normalization_enabled=True,
    )

    details = cast(
        "ActiveSourceAudioDetails | None",
        source_session.active_source_audio,
    )
    assert details is not None
    assert details.crossfade_mode is CrossfadeMode.SOURCE
    assert details.outputs[0].fidelity.bit_perfect is True


def test_unreported_source_processing_does_not_cost_the_bit_perfect_badge() -> None:
    """A source that never says what it applies still hands us its samples untouched."""
    manager, _mass, source_session, lossless_plan, pcm_format = _source_manager_context()
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="source-player",
        session_id="source-session",
    )

    manager.update_source_context(
        "source-player",
        "source-session",
        pcm_format=pcm_format,
        crossfade_enabled=None,
        volume_normalization_enabled=None,
    )

    details = cast(
        "ActiveSourceAudioDetails | None",
        source_session.active_source_audio,
    )
    assert details is not None
    assert details.crossfade_mode is CrossfadeMode.UNKNOWN
    assert details.volume_normalization_mode is VolumeNormalizationMode.UNKNOWN
    assert details.outputs[0].fidelity.bit_perfect is True


def test_a_player_that_cannot_take_the_source_rate_is_not_bit_perfect() -> None:
    """A source rate the player cannot take is snapped down, which loses samples."""
    manager, _mass, source_session, lossless_plan, _pcm_format = _source_manager_context()
    # the source arrives at 96 kHz but the player tops out at 48 kHz
    snapped = _format(ContentType.PCM_S24LE, 48000, 24)
    lossless_plan.input_format = snapped
    lossless_plan.output_details.output_format = _format(ContentType.FLAC, 48000, 24)
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="source-player",
        session_id="source-session",
    )

    manager.update_source_context(
        "source-player",
        "source-session",
        pcm_format=snapped,
        crossfade_enabled=False,
        volume_normalization_enabled=False,
    )

    details = cast(
        "ActiveSourceAudioDetails | None",
        source_session.active_source_audio,
    )
    assert details is not None
    assert details.outputs[0].fidelity.bit_perfect is False


def test_a_live_source_output_narrower_than_the_source_is_not_bit_perfect() -> None:
    """Dropping a 24-bit source to a 16-bit output loses bits for a live source too."""
    manager, _mass, source_session, lossless_plan, pcm_format = _source_manager_context()
    lossless_plan.output_details.output_format = _format(ContentType.FLAC, 96000, 16)
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="source-player",
        session_id="source-session",
    )

    manager.update_source_context(
        "source-player",
        "source-session",
        pcm_format=pcm_format,
        crossfade_enabled=False,
        volume_normalization_enabled=False,
    )

    details = cast(
        "ActiveSourceAudioDetails | None",
        source_session.active_source_audio,
    )
    assert details is not None
    assert details.outputs[0].fidelity.bit_perfect is False


def test_stale_live_source_updates_are_rejected() -> None:
    """A superseded source session cannot publish context or outputs."""
    manager, _mass, source_session, lossless_plan, pcm_format = _source_manager_context()

    manager.update_source_context(
        "source-player",
        "stale-session",
        pcm_format=pcm_format,
        crossfade_enabled=True,
        volume_normalization_enabled=True,
    )

    assert not manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="source-player",
        session_id="stale-session",
    )
    assert source_session.active_source_audio is None


def test_clearing_live_source_processing_removes_the_snapshot() -> None:
    """Ending a source selection clears its published audio details."""
    manager, mass, source_session, _lossless_plan, pcm_format = _source_manager_context()
    manager.update_source_context(
        "source-player",
        "source-session",
        pcm_format=pcm_format,
        crossfade_enabled=False,
        volume_normalization_enabled=False,
    )
    mass.players.trigger_player_update.reset_mock()

    manager.clear_source("source-player", "source-session")

    assert source_session.active_source_audio is None
    mass.players.trigger_player_update.assert_called_once_with("source-player")


def test_live_source_outputs_follow_current_group_members() -> None:
    """A departed group member is removed from the live source output snapshot."""
    manager, mass, source_session, lossless_plan, pcm_format = _source_manager_context()
    manager.update_output(
        "player-1",
        lossless_plan,
        shared_player_ids={"player-2"},
        queue_id="source-player",
        session_id="source-session",
    )
    manager.update_source_context(
        "source-player",
        "source-session",
        pcm_format=pcm_format,
        crossfade_enabled=False,
        volume_normalization_enabled=False,
    )
    mass.players.trigger_player_update.reset_mock()

    manager.retain_outputs("source-player", {"player-1"})

    details = cast(
        "ActiveSourceAudioDetails | None",
        source_session.active_source_audio,
    )
    assert details is not None
    assert len(details.outputs) == 1
    assert details.outputs[0].player_ids == ["player-1"]
    mass.players.trigger_player_update.assert_called_once_with("source-player")


def test_shared_output_adds_member_without_stream_restart() -> None:
    """A late native-sync member inherits the active shared output path."""
    manager, mass, _queue_data, streamdetails, output_plan, _lossy_plan = _manager_context()
    manager.update_output(
        "leader",
        output_plan,
        shared_player_ids=(),
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    mass.player_queues.signal_update.reset_mock()

    assert manager.retain_outputs("queue-1", {"queue-1", "leader", "late-member"})

    assert streamdetails.audio_processing is not None
    assert streamdetails.audio_processing.outputs[0].player_ids == ["late-member", "leader"]
    mass.player_queues.signal_update.assert_called_once_with("queue-1")


def test_independent_output_does_not_add_group_member() -> None:
    """Membership changes do not expand an independently processed output."""
    manager, mass, _queue_data, streamdetails, output_plan, _lossy_plan = _manager_context()
    manager.update_output(
        "leader",
        output_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    mass.player_queues.signal_update.reset_mock()

    assert not manager.retain_outputs("queue-1", {"leader", "independent-member"})

    assert streamdetails.audio_processing is not None
    assert streamdetails.audio_processing.outputs[0].player_ids == ["leader"]
    mass.player_queues.signal_update.assert_not_called()


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


def test_retain_outputs_signals_current_chain_change() -> None:
    """Pruning a current output publishes the reduced chain."""
    manager, mass, _queue_data, streamdetails, output_plan, _lossy_plan = _manager_context()
    manager.update_output(
        "player-1",
        output_plan,
        shared_player_ids={"player-2"},
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    mass.player_queues.signal_update.reset_mock()

    assert manager.retain_outputs("queue-1", {"player-1"})

    assert streamdetails.audio_processing is not None
    assert streamdetails.audio_processing.outputs[0].player_ids == ["player-1"]
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
        shared_player_ids={"stale-child"},
        queue_id="queue-1",
        session_id="session-1",
    )
    assert streamdetails.audio_processing is None

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
    assert plan.filter_params[-1] == "pan=mono|c0=FL"
    assert plan.output_details.dsp == AudioDSPDetails(
        state=DSPState.ENABLED,
        input_gain=-1.0,
        filters=[ToneControlFilter(enabled=True, bass_level=2.0)],
        output_gain=-0.5,
        preset_id="night",
    )
    assert plan.dsp_config_id == "player-1"
    assert plan.output_details.source_channel == AudioChannel.FL
    assert plan.output_details.output_format == output_format
    mass.streams.audio_processing.update_output.assert_called_once_with(
        "player-1",
        plan,
        shared_player_ids=None,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )


def test_player_output_plan_downmixes_to_mono() -> None:
    """The mono output mode folds both source channels into a single channel."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    mass.config.get_raw_player_config_value.return_value = "mono"
    audio = StreamsAudio(cast("Any", mass))
    input_format = _format(ContentType.PCM_F32LE, 48000, 32)
    output_format = _format(ContentType.FLAC, 48000, 16, channels=1)

    plan = audio.get_player_output_plan(
        "player-1",
        input_format,
        output_format,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert plan.filter_params == ["pan=mono|c0=0.5*FL+0.5*FR"]
    assert plan.output_details.source_channel == AudioChannel.ALL


def test_player_output_plan_feeds_every_output_channel() -> None:
    """A stereo output carries the downmix on both channels instead of being upmixed."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    mass.config.get_raw_player_config_value.return_value = "mono"
    audio = StreamsAudio(cast("Any", mass))
    audio_format = _format(ContentType.PCM_F32LE, 48000, 32)

    plan = audio.get_player_output_plan(
        "player-1",
        audio_format,
        audio_format,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert plan.filter_params == ["pan=stereo|c0=0.5*FL+0.5*FR|c1=0.5*FL+0.5*FR"]
    assert plan.output_details.source_channel == AudioChannel.ALL


def test_player_output_plan_skips_channel_selection_for_mono_source() -> None:
    """A single channel source has no channels to select, so it is left untouched."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    mass.config.get_raw_player_config_value.return_value = "mono"
    audio = StreamsAudio(cast("Any", mass))
    audio_format = _format(ContentType.PCM_F32LE, 48000, 32, channels=1)

    plan = audio.get_player_output_plan(
        "player-1",
        audio_format,
        audio_format,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert plan.filter_params == []
    assert plan.output_details.source_channel is None


def test_player_output_plan_pans_for_the_handoff_format() -> None:
    """The pan follows the format FFmpeg emits, not a later provider side encode."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    mass.config.get_raw_player_config_value.return_value = "mono"
    audio = StreamsAudio(cast("Any", mass))
    pcm_format = _format(ContentType.PCM_F32LE, 48000, 32)

    plan = audio.get_player_output_plan(
        "player-1",
        pcm_format,
        _format(ContentType.FLAC, 48000, 16, channels=1),
        handoff_format=pcm_format,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert plan.filter_params == ["pan=stereo|c0=0.5*FL+0.5*FR|c1=0.5*FL+0.5*FR"]


def test_mono_downmix_prevents_bit_perfect_claim() -> None:
    """A mono downmix alters the samples, even when every format stays stereo."""
    manager, _mass, _queue_data, streamdetails, output_plan, _lossy_plan = _manager_context()
    output_plan.output_details.source_channel = AudioChannel.ALL

    manager.update_output(
        "player-1",
        output_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert streamdetails.audio_processing is not None
    assert streamdetails.audio_processing.outputs[0].fidelity.bit_perfect is False


def test_player_output_plan_excludes_neutral_filters() -> None:
    """A filter that emits no FFmpeg params is left out of the reported chain."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    mass.config.get_player_dsp_config.return_value = DSPConfig(
        enabled=True,
        filters=[ToneControlFilter(enabled=True)],
    )
    mass.config.get_raw_player_config_value.return_value = "stereo"
    audio = StreamsAudio(cast("Any", mass))
    audio_format = _format(ContentType.PCM_F32LE, 48000, 32)

    plan = audio.get_player_output_plan(
        "player-1",
        audio_format,
        audio_format,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert plan.output_details.dsp.filters == []
    assert not any(
        isinstance(param, str) and param.startswith("equalizer=") for param in plan.filter_params
    )


def _convolution_plan(known_ir_ids: list[str]) -> AudioOutputPlan:
    """Build an output plan for a player convolving with impulse response "abc123"."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    mass.storage_path = "/storage"
    mass.config.get_player_dsp_config.return_value = DSPConfig(
        enabled=True,
        filters=[ConvolutionFilter(enabled=True, ir_id="abc123")],
    )
    mass.config.get_dsp_irs.return_value = [{"ir_id": ir_id} for ir_id in known_ir_ids]
    mass.config.get_raw_player_config_value.return_value = "stereo"
    audio = StreamsAudio(cast("Any", mass))
    audio_format = _format(ContentType.PCM_F32LE, 48000, 32)
    return audio.get_player_output_plan(
        "player-1",
        audio_format,
        audio_format,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )


def test_player_output_plan_drops_convolution_with_unknown_ir() -> None:
    """An impulse response with no stored record is left out rather than failing ffmpeg."""
    plan = _convolution_plan(known_ir_ids=["other"])

    assert plan.output_details.dsp.filters == []
    assert not any(isinstance(param, ComplexFilter) for param in plan.filter_params)


def test_player_output_plan_keeps_convolution_with_known_ir() -> None:
    """An impulse response that is still stored convolves as configured."""
    plan = _convolution_plan(known_ir_ids=["abc123"])

    assert len(plan.output_details.dsp.filters) == 1
    complex_filters = [param for param in plan.filter_params if isinstance(param, ComplexFilter)]
    assert [f.inputs[0].path for f in complex_filters] == ["/storage/dsp_irs/abc123.wav"]


def test_player_output_plan_prefers_rendering_player_channels() -> None:
    """Output channels stored on the rendering player win over the parent's value."""
    mass = MagicMock()
    player = MagicMock(player_id="child-1", protocol_parent_id="parent-1")
    player.state.active_group = None
    player.state.synced_to = None
    mass.players.get_player.return_value = player
    mass.config.get_player_dsp_config.return_value = DSPConfig(enabled=False)
    mass.config.get_raw_player_config_value.side_effect = lambda player_id, _key, default: (
        "left" if player_id == "child-1" else default
    )
    audio = StreamsAudio(cast("Any", mass))
    audio_format = _format(ContentType.PCM_F32LE, 48000, 32)

    plan = audio.get_player_output_plan(
        "child-1",
        audio_format,
        audio_format,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )

    assert plan.output_details.source_channel == AudioChannel.FL
    assert "pan=stereo|c0=FL|c1=FL" in plan.filter_params
    # processing attribution still points at the visible parent player
    assert mass.streams.audio_processing.update_output.call_args.args[0] == "parent-1"


@pytest.mark.asyncio
async def test_output_format_prefers_rendering_player_channels() -> None:
    """The output format channel count follows the rendering player's own stored value."""
    mass = MagicMock()
    player = MagicMock(player_id="child-1", protocol_parent_id="parent-1")
    player.get_supported_sample_rates.return_value = [(48000, 24)]
    mass.config.get_raw_player_config_value.side_effect = lambda player_id, _key, default: (
        "left" if player_id == "child-1" else default
    )
    audio = StreamsAudio(cast("Any", mass))

    fmt = await audio.get_output_format("flac", player, 48000, 24, MediaType.TRACK)

    assert fmt.channels == 1


@pytest.mark.asyncio
async def test_single_stream_handler_shares_native_group_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regular single-item HTTP stream registers native group members."""
    controller, request, group_members = _native_stream_handler_context(monkeypatch)

    with pytest.raises(_OutputPlanRequested):
        await controller.serve_queue_item_stream(request)

    assert controller.audio.get_player_output_plan.call_args.kwargs["shared_player_ids"] is (
        group_members
    )


@pytest.mark.asyncio
async def test_flow_stream_handler_shares_native_group_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regular flow HTTP stream registers native group members."""
    controller, request, group_members = _native_stream_handler_context(monkeypatch)

    with pytest.raises(_OutputPlanRequested):
        await controller.serve_queue_flow_stream(request)

    assert controller.audio.get_player_output_plan.call_args.kwargs["shared_player_ids"] is (
        group_members
    )


def test_protocol_output_uses_parent_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Protocol output details use the user-facing parent configuration."""
    mass = MagicMock()
    player = MagicMock(player_id="protocol-1", protocol_parent_id="player-1")
    player.state.active_group = None
    player.state.synced_to = None
    shared_player = MagicMock(player_id="protocol-2", protocol_parent_id="player-2")
    mass.players.get_player.side_effect = lambda player_id: {
        "protocol-1": player,
        "protocol-2": shared_player,
    }.get(player_id)
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
        shared_player_ids={"protocol-1", "protocol-2"},
        queue_id="queue-1",
        session_id="session-1",
    )

    assert plan.output_details.player_ids == ["player-1", "player-2"]
    assert plan.output_details.dsp.preset_id == "parent-preset"
    assert plan.dsp_config_id == "player-1"
    assert plan.output_details.source_channel == AudioChannel.FR
    # the output channels are looked up on the rendering player first (no value
    # stored there in this scenario), then resolved from the user-facing parent
    assert {call.args[0] for call in mass.config.get_raw_player_config_value.call_args_list} == {
        "player-1",
        "protocol-1",
    }
    mass.streams.audio_processing.update_output.assert_called_once_with(
        "player-1",
        plan,
        shared_player_ids={"player-2"},
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id=None,
    )


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


@pytest.mark.asyncio
async def test_duplicate_flow_producer_does_not_interleave_the_play_log() -> None:
    """A second flow request for one session keeps the play log of the first out of the queue."""

    def _flow_item(item_id: str) -> Any:
        return SimpleNamespace(
            queue_item_id=item_id,
            name=item_id,
            media_type=MediaType.TRACK,
            duration=300,
            extra_attributes={},
            streamdetails=SimpleNamespace(
                fade_in=False,
                stream_error=False,
                uri=f"test://{item_id}",
                seek_position=0,
                duration=300,
                buffer=None,
                seconds_streamed=None,
                is_realtime=False,
                audio_format=_format(ContentType.PCM_F32LE, 48000, 32),
            ),
        )

    items = {item_id: _flow_item(item_id) for item_id in ("item-1", "item-2")}

    async def _load_next(_queue_id: str, current_id: str) -> Any:
        if current_id == "item-1":
            return items["item-2"]
        raise QueueEmpty

    mass = MagicMock()
    queue_data = SimpleNamespace(session_id="session-1", flow_mode_stream_log=[])
    mass.player_queues.queue_data.return_value = queue_data
    mass.player_queues.load_next_queue_item = _load_next
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    mass.config.get_raw_core_config_value.return_value = 0
    mass.player_queues.get_active_queue.return_value = None
    audio = StreamsAudio(cast("Any", mass))

    async def _one_chunk(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        yield b"\x00" * 16

    audio.get_queue_item_stream = _one_chunk  # type: ignore[method-assign]
    queue = cast(
        "Any",
        SimpleNamespace(
            queue_id="queue-1",
            display_name="Queue",
            flow_mode=False,
            overlay_enabled=False,
            overlay_source=None,
        ),
    )
    pcm_format = _format(ContentType.PCM_F32LE, 48000, 32)

    # the probing connection opens the flow url and logs its first track
    first = audio.get_queue_flow_stream(queue, items["item-1"], pcm_format, session_id="session-1")
    await anext(first)
    assert [entry.queue_item_id for entry in queue_data.flow_mode_stream_log] == ["item-1"]

    # the connection that really plays opens the same url and publishes its own play log
    second = audio.get_queue_flow_stream(queue, items["item-1"], pcm_format, session_id="session-1")
    await anext(second)
    live_log = queue_data.flow_mode_stream_log

    # the first producer moves on to its next track; that entry must not reach the live log
    await anext(first)
    assert queue_data.flow_mode_stream_log is live_log
    assert [entry.queue_item_id for entry in live_log] == ["item-1"]

    await first.aclose()
    await second.aclose()


@pytest.mark.asyncio
async def test_flow_source_error_skips_item_without_completing_it() -> None:
    """An item-stream error skips to the next queue item; the flow itself continues."""
    mass = MagicMock()
    streamdetails = SimpleNamespace(
        fade_in=False,
        stream_error=False,
        uri="audiobookshelf://book",
        seek_position=0,
        duration=3600,
        is_realtime=False,
    )
    queue_item = SimpleNamespace(
        queue_item_id="item-1",
        name="book",
        media_type=MediaType.AUDIOBOOK,
        streamdetails=streamdetails,
        extra_attributes={},
    )
    queue_data = SimpleNamespace(session_id="session-1", flow_mode_stream_log=[])
    mass.player_queues.queue_data.return_value = queue_data
    mass.player_queues.load_next_queue_item.side_effect = QueueEmpty
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    mass.config.get_raw_core_config_value.return_value = 0
    mass.streams.audio_processing.update_item_context = MagicMock()
    mass.player_queues.queue_buffer_completed = MagicMock()
    mass.player_queues.get_active_queue.return_value = None
    audio = StreamsAudio(cast("Any", mass))

    async def _failed_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        yield b"buffered audio"
        streamdetails.stream_error = True

    audio.get_queue_item_stream = _failed_stream  # type: ignore[method-assign]
    stream = audio.get_queue_flow_stream(
        cast(
            "Any",
            SimpleNamespace(
                queue_id="queue-1",
                display_name="Queue",
                flow_mode=False,
                overlay_enabled=False,
                overlay_source=None,
            ),
        ),
        cast("Any", queue_item),
        _format(ContentType.PCM_F32LE, 48000, 32),
        session_id="session-1",
    )

    chunks = [chunk async for chunk in stream]

    assert chunks == [b"buffered audio"]
    # the flow ran to natural completion (next item lookup raised QueueEmpty)
    mass.player_queues.queue_buffer_completed.assert_called_once()
    # the play log entry is kept, honest about the partial amount actually sent
    assert len(queue_data.flow_mode_stream_log) == 1
    entry = queue_data.flow_mode_stream_log[0]
    assert entry.queue_item_id == "item-1"
    assert entry.seconds_streamed is not None
    assert entry.seconds_streamed > 0


@pytest.mark.asyncio
async def test_flow_zero_audio_skip_restores_seek_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-audio item keeps its original seek position when its crossfade is skipped."""
    mass = MagicMock()
    pcm_format = _format(ContentType.PCM_S16LE, 8000, 16)
    first_streamdetails = SimpleNamespace(
        audio_format=pcm_format,
        fade_in=False,
        stream_error=False,
        uri="test://first",
        seek_position=0,
        seconds_streamed=0,
        duration=120,
        buffer=SimpleNamespace(eof=True, cancelled=False, has_error=False, max_size_seconds=300),
        is_realtime=False,
    )
    first_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-1",
        name="first",
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=first_streamdetails,
        extra_attributes={},
    )
    raw_seek_position = 12
    skipped_streamdetails = SimpleNamespace(
        audio_format=pcm_format,
        buffer=SimpleNamespace(
            has_error=False,
            is_valid=lambda *_args: True,
            duration_available=16,
            eof=False,
            ready=SimpleNamespace(is_set=lambda: True),
        ),
        fade_in=False,
        stream_error=False,
        uri="test://skipped",
        seek_position=raw_seek_position,
        seconds_streamed=0,
        duration=120,
        is_realtime=False,
    )
    skipped_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-2",
        name="skipped",
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=skipped_streamdetails,
        extra_attributes={"playback_speed": 2.0},
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    queue_data = SimpleNamespace(session_id="session-1", flow_mode_stream_log=[])
    mass.player_queues.queue_data.return_value = queue_data
    mass.player_queues.load_next_queue_item = AsyncMock(side_effect=[skipped_item, QueueEmpty])
    mass.player_queues.get.return_value = queue
    mass.player_queues.get_next_item.return_value = skipped_item
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.STANDARD_CROSSFADE
    mass.streams.get_source_crossfade_mode.return_value = CrossfadeMode.DISABLED
    mass.config.get_raw_core_config_value.return_value = 8
    mass.streams.audio_processing.update_item_context = MagicMock()
    mass.player_queues.queue_buffer_completed = MagicMock()
    player = MagicMock()
    player.config.get_value.return_value = "fixed_48000"
    player.get_supported_sample_rates.return_value = []
    mass.players.get_player.return_value = player
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()
    build = AsyncMock(
        return_value=SimpleNamespace(
            timing_info=SimpleNamespace(
                fadein_trimmed_duration=2,
                crossfade_duration=8,
            )
        )
    )
    monkeypatch.setattr(audio.smart_fades_mixer, "build", build)
    eager_seek_positions: list[float] = []

    async def _item_stream(
        queue_item: SimpleNamespace,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncGenerator[bytes]:
        if queue_item is first_item:
            # warmup worth of audio, then a full crossfade tail
            yield bytes(pcm_format.pcm_sample_size * 8)
            yield bytes(pcm_format.pcm_sample_size * 8)
        else:
            eager_seek_positions.append(queue_item.streamdetails.seek_position)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue),
        cast("Any", first_item),
        pcm_format,
        session_id="session-1",
    )

    async for _ in stream:
        pass

    build.assert_awaited_once()
    # the prefetcher's early open sees the raw position; the reopen after the failed
    # handover sees the eager (crossfade-adjusted) one
    assert len(eager_seek_positions) == 2
    assert eager_seek_positions[-1] == 32
    # ... and the zero-audio skip restores the raw position afterwards
    assert skipped_streamdetails.seek_position == raw_seek_position


@pytest.mark.parametrize(
    ("source_cancelled", "expected_duration"),
    [(True, 300), (False, 3)],
    ids=["aborted_source", "clean_source"],
)
@pytest.mark.asyncio
async def test_flow_does_not_write_back_a_duration_for_an_aborted_source(
    monkeypatch: pytest.MonkeyPatch, source_cancelled: bool, expected_duration: int
) -> None:
    """An externally cancelled buffer ends in a clean EOF that must not shorten the item."""
    mass = MagicMock()
    pcm_format = _format(ContentType.PCM_S16LE, 8000, 16)
    streamdetails = SimpleNamespace(
        audio_format=pcm_format,
        buffer=SimpleNamespace(cancelled=source_cancelled),
        fade_in=False,
        stream_error=False,
        uri="test://track",
        seek_position=0,
        seconds_streamed=0,
        duration=300,
        is_realtime=False,
    )
    queue_track = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-1",
        name="track",
        media_type=MediaType.TRACK,
        media_item=None,
        streamdetails=streamdetails,
        duration=300,
        extra_attributes={},
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        flow_mode=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    queue_data = SimpleNamespace(session_id="session-1", flow_mode_stream_log=[])
    mass.player_queues.queue_data.return_value = queue_data
    mass.player_queues.load_next_queue_item = AsyncMock(side_effect=QueueEmpty)
    mass.player_queues.get.return_value = queue
    mass.streams.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    mass.config.get_raw_core_config_value.return_value = 8
    mass.streams.audio_processing.update_item_context = MagicMock()
    mass.player_queues.queue_buffer_completed = MagicMock()
    player = MagicMock()
    player.config.get_value.return_value = "fixed_48000"
    player.get_supported_sample_rates.return_value = []
    mass.players.get_player.return_value = player
    audio = StreamsAudio(cast("Any", mass))
    audio.setup()

    async def _item_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[bytes]:
        # a cancelled buffer stops yielding without an error, exactly like a real EOF
        for _ in range(3):
            yield bytes(pcm_format.pcm_sample_size)

    monkeypatch.setattr(audio, "get_queue_item_stream", _item_stream)
    stream = audio.get_queue_flow_stream(
        cast("Any", queue), cast("Any", queue_track), pcm_format, session_id="session-1"
    )

    chunks = [chunk async for chunk in stream]

    assert len(chunks) == 3
    assert streamdetails.duration == expected_duration
    assert queue_track.duration == expected_duration
    # the honest streamed amount is always recorded, only the duration is protected
    assert streamdetails.seconds_streamed == 3
    entry = queue_data.flow_mode_stream_log[0]
    assert entry.seconds_streamed == 3
    assert entry.duration == (None if source_cancelled else 3)


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


def _source_manager_context() -> tuple[
    AudioProcessingManager,
    MagicMock,
    Any,
    AudioOutputPlan,
    AudioFormat,
]:
    """Return one active live source, a lossless output plan and its PCM format."""
    mass = MagicMock()
    streamdetails = StreamDetails(
        provider="source-provider",
        item_id="main",
        audio_format=_format(ContentType.FLAC, 96000, 24, bit_rate=3200),
        media_type=MediaType.AUDIO_SOURCE,
    )
    source_session: Any = SimpleNamespace(
        playback_session_id="source-session",
        streamdetails=streamdetails,
        active_source_audio=None,
    )
    mass.players.get_audio_source_session.side_effect = lambda player_id: (
        source_session if player_id == "source-player" else None
    )
    manager = AudioProcessingManager(mass)
    pcm_format = _format(ContentType.PCM_S24LE, 96000, 24)
    output_plan = AudioOutputPlan(
        filter_params=[],
        output_details=AudioOutputDetails(
            dsp=AudioDSPDetails(state=DSPState.DISABLED),
            output_format=_format(ContentType.FLAC, 96000, 24),
        ),
        input_format=pcm_format,
    )
    return manager, mass, source_session, output_plan, pcm_format


def _source_handled_soloist_item(
    output_format: AudioFormat,
) -> StreamDetails:
    """Prepare a Spotify-soloist-shaped item: a 24-bit tier delivered as 32-bit PCM."""
    manager, _mass, _queue_data, streamdetails, lossless_plan, _lossy_plan = _manager_context()
    streamdetails.audio_format = _format(ContentType.FLAC, 44100, 24)
    streamdetails.decoded_audio_format = _format(ContentType.PCM_S32LE, 44100, 32)
    pcm_format = _format(ContentType.PCM_S32LE, 44100, 32)
    manager.update_item_runtime(
        "queue-1",
        "session-1",
        "item-1",
        input_format=pcm_format,
        pcm_format=pcm_format,
        normalization=AudioNormalizationDetails(mode=VolumeNormalizationMode.SOURCE),
        playback_speed=1.0,
    )
    manager.update_item_context(
        "queue-1",
        "session-1",
        "item-1",
        AudioQueueProcessing(pcm_format=pcm_format, crossfade_mode=CrossfadeMode.SOURCE),
    )
    lossless_plan.input_format = pcm_format
    lossless_plan.output_details.output_format = output_format
    manager.update_output(
        "player-1",
        lossless_plan,
        queue_id="queue-1",
        session_id="session-1",
        queue_item_id="item-1",
    )
    return streamdetails


def _streamdetails(item_id: str = "item-1") -> StreamDetails:
    """Return hi-res lossless stream details."""
    return StreamDetails(
        provider="provider",
        item_id=item_id,
        audio_format=_format(ContentType.FLAC, 96000, 24, bit_rate=3200),
        media_type=MediaType.TRACK,
    )


class _OutputPlanRequested(Exception):
    """Signal that a stream handler reached output planning."""


def _native_stream_handler_context(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, MagicMock, list[str]]:
    """Return a native HTTP stream handler prepared to stop at output planning."""
    mass = MagicMock()
    streamdetails = _streamdetails()
    queue_item = SimpleNamespace(
        queue_id="queue-1",
        queue_item_id="item-1",
        name="Track",
        duration=180,
        streamdetails=streamdetails,
        media_item=None,
        media_type=MediaType.TRACK,
        extra_attributes={},
        image=None,
    )
    queue = SimpleNamespace(
        queue_id="queue-1",
        display_name="Queue",
        current_item=queue_item,
        crossfade_enabled=False,
        overlay_enabled=False,
        overlay_source=None,
    )
    queue_data = SimpleNamespace(session_id="session-1")
    mass.player_queues.get.return_value = queue
    mass.player_queues.queue_data.return_value = queue_data
    mass.player_queues.get_item.return_value = queue_item
    mass.config.get_raw_core_config_value.return_value = 8
    mass.config.get_raw_player_config_value.return_value = "disabled"

    group_members = ["player-1", "player-2"]
    player = MagicMock(player_id="player-1", protocol_parent_id=None)
    player.state.group_members = group_members
    player.state.supported_features = set()
    player.state.name = "Player"
    player.get_config_value.return_value = "default"
    mass.players.get_player.return_value = player

    pcm_format = _format(ContentType.PCM_F32LE, 48000, 32)
    output_format = _format(ContentType.FLAC, 48000, 24)
    audio = MagicMock()
    audio.select_pcm_format = AsyncMock(return_value=pcm_format)
    audio.select_flow_pcm_format = AsyncMock(return_value=pcm_format)
    audio.get_output_format = AsyncMock(return_value=output_format)
    audio.get_player_output_plan.side_effect = _OutputPlanRequested

    controller = cast("Any", object.__new__(StreamsController))
    controller.mass = mass
    controller.audio = audio
    controller.logger = MagicMock()
    controller._log_request = MagicMock()
    controller._update_audio_processing_context = MagicMock()
    controller._active_output_streams = 0

    response = MagicMock()
    response.prepare = AsyncMock()
    monkeypatch.setattr(
        "music_assistant.controllers.streams.controller.web.StreamResponse",
        MagicMock(return_value=response),
    )
    request = MagicMock()
    request.method = "GET"
    request.headers = {}
    request.match_info = {
        "queue_id": "queue-1",
        "session_id": "session-1",
        "queue_item_id": "item-1",
        "player_id": "player-1",
        "fmt": "flac",
    }
    return controller, request, group_members
