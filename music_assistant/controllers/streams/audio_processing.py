"""Runtime audio processing details for queue streams."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from music_assistant_models.audio_processing import (
    AudioCrossfadeDetails,
    AudioCrossfadeState,
    AudioFidelity,
    AudioFidelitySummary,
    AudioInputDetails,
    AudioNormalizationDetails,
    AudioNormalizationMeasurementSource,
    AudioOutputPath,
    AudioOverlayDetails,
    AudioProcessingChain,
    AudioProcessingState,
    AudioQuality,
    AudioQueueProcessing,
    AudioTempoDetails,
)
from music_assistant_models.dsp import DSPState
from music_assistant_models.enums import ContentType, EventType, VolumeNormalizationMode

from music_assistant.helpers.audio import get_bit_rate

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import PlayerMedia


_QUALITY_RANK = {
    AudioQuality.UNKNOWN: 0,
    AudioQuality.LOW: 1,
    AudioQuality.STANDARD: 2,
    AudioQuality.LOSSLESS: 3,
    AudioQuality.HI_RES: 4,
}


@dataclass(slots=True)
class AudioOutputPlan:
    """Executable filters and matching client-facing output details."""

    filter_params: list[str]
    output_path: AudioOutputPath


@dataclass(slots=True)
class _AudioProcessingItem:
    """Processing details cached for one queue item."""

    input: AudioInputDetails | None = None
    queue_processing: AudioQueueProcessing | None = None
    alters_audio: bool = False


@dataclass(slots=True)
class _AudioProcessingSession:
    """Runtime processing state for one queue playback session."""

    session_id: str
    items: dict[str, _AudioProcessingItem] = field(default_factory=dict)
    outputs: dict[str | None, dict[str, AudioOutputPath]] = field(default_factory=dict)
    last_snapshot: AudioProcessingChain | None = None


class AudioProcessingRegistry:
    """Store and publish effective audio processing details per queue session."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the audio processing registry.

        :param mass: Music Assistant instance.
        """
        self.mass = mass
        self._sessions: dict[str, _AudioProcessingSession] = {}
        self._revisions: dict[str, int] = {}

    def get(self, queue_id: str) -> AudioProcessingChain | None:
        """
        Return the latest processing snapshot for a queue.

        :param queue_id: Queue identifier.
        """
        self.refresh(queue_id)
        session = self._sessions.get(queue_id)
        return deepcopy(session.last_snapshot) if session and session.last_snapshot else None

    def start_session(self, queue_id: str, session_id: str) -> None:
        """
        Start tracking a queue playback session.

        :param queue_id: Queue identifier.
        :param session_id: Internal queue playback session identifier.
        """
        current = self._sessions.get(queue_id)
        if current and current.session_id == session_id:
            return
        if current and current.last_snapshot is not None:
            self.mass.signal_event(
                EventType.AUDIO_PROCESSING_UPDATED,
                object_id=queue_id,
                data=None,
            )
        self._sessions[queue_id] = _AudioProcessingSession(session_id=session_id)

    def update_item_context(
        self,
        queue_id: str,
        session_id: str,
        queue_item_id: str,
        input_details: AudioInputDetails,
        input_format: AudioFormat,
        output_format: AudioFormat,
        *,
        crossfade: AudioCrossfadeDetails | None = None,
        overlay: AudioOverlayDetails | None = None,
        alters_audio: bool = False,
    ) -> None:
        """
        Store formats and configured shared processing for a queue item.

        :param queue_id: Queue identifier.
        :param session_id: Internal queue playback session identifier.
        :param queue_item_id: Queue item identifier.
        :param input_details: Source and server input details.
        :param input_format: PCM format entering shared queue processing.
        :param output_format: PCM format leaving shared queue processing.
        :param crossfade: Effective crossfade details.
        :param overlay: Effective overlay details.
        :param alters_audio: Whether an intentionally hidden transform alters samples.
        """
        session = self._get_session(queue_id, session_id)
        if session is None:
            return
        item = session.items.setdefault(queue_item_id, _AudioProcessingItem())
        previous = item.queue_processing
        item.input = deepcopy(input_details)
        item.alters_audio = alters_audio
        item.queue_processing = AudioQueueProcessing(
            input_format=deepcopy(input_format),
            output_format=deepcopy(output_format),
            normalization=deepcopy(previous.normalization) if previous else None,
            tempo=deepcopy(previous.tempo) if previous else None,
            crossfade=deepcopy(crossfade),
            overlay=deepcopy(overlay),
        )
        self.refresh(queue_id)

    def update_item_runtime(
        self,
        queue_id: str,
        session_id: str,
        queue_item_id: str,
        input_details: AudioInputDetails,
        input_format: AudioFormat,
        output_format: AudioFormat,
        normalization: AudioNormalizationDetails | None,
        tempo: AudioTempoDetails | None,
        alters_audio: bool = False,
    ) -> None:
        """
        Store the shared processing that was constructed for a queue item.

        :param queue_id: Queue identifier.
        :param session_id: Internal queue playback session identifier.
        :param queue_item_id: Queue item identifier.
        :param input_details: Source and server input details.
        :param input_format: PCM format entering shared queue processing.
        :param output_format: PCM format leaving shared queue processing.
        :param normalization: Effective normalization details.
        :param tempo: Active tempo processing details.
        :param alters_audio: Whether an intentionally hidden transform alters samples.
        """
        session = self._get_session(queue_id, session_id)
        if session is None:
            return
        item = session.items.setdefault(queue_item_id, _AudioProcessingItem())
        previous = item.queue_processing
        item.input = deepcopy(input_details)
        item.alters_audio = item.alters_audio or alters_audio
        item.queue_processing = AudioQueueProcessing(
            input_format=deepcopy(input_format),
            output_format=deepcopy(output_format),
            normalization=deepcopy(normalization),
            tempo=deepcopy(tempo),
            crossfade=deepcopy(previous.crossfade) if previous else None,
            overlay=deepcopy(previous.overlay) if previous else None,
        )
        self.refresh(queue_id)

    def update_crossfade(
        self,
        queue_id: str,
        session_id: str,
        queue_item_id: str,
        crossfade: AudioCrossfadeDetails | None,
    ) -> None:
        """
        Update the runtime crossfade result for a queue item.

        :param queue_id: Queue identifier.
        :param session_id: Internal queue playback session identifier.
        :param queue_item_id: Queue item identifier.
        :param crossfade: Effective crossfade details.
        """
        session = self._get_session(queue_id, session_id)
        if session is None:
            return
        item = session.items.setdefault(queue_item_id, _AudioProcessingItem())
        previous = item.queue_processing or AudioQueueProcessing()
        item.queue_processing = replace(previous, crossfade=deepcopy(crossfade))
        self.refresh(queue_id)

    def update_output(
        self,
        player_id: str,
        output_path: AudioOutputPath,
        *,
        queue_id: str,
        session_id: str,
        queue_item_id: str | None = None,
    ) -> bool:
        """
        Store an effective player output path.

        :param player_id: Destination player identifier.
        :param output_path: Effective processing and format for the destination.
        :param queue_id: Queue identifier that owns the output.
        :param session_id: Queue session identifier that owns the output.
        :param queue_item_id: Queue item for single-item output, or None for a shared flow output.
        :return: Whether the active processing snapshot changed.
        """
        session = self._get_session(queue_id, session_id)
        if session is None:
            return False
        output_path = deepcopy(output_path)
        output_path.player_ids = [player_id]
        item_outputs = session.outputs.setdefault(queue_item_id, {})
        if (current := item_outputs.get(player_id)) and _output_paths_equal(
            current,
            output_path,
        ):
            return False
        item_outputs[player_id] = output_path
        self.refresh(queue_id)
        self._sync_legacy_streamdetails(queue_id)
        return True

    def retain_outputs(self, queue_id: str, player_ids: set[str]) -> None:
        """
        Drop output paths for players no longer attached to a queue.

        :param queue_id: Queue identifier.
        :param player_ids: Player identifiers that still belong to the output.
        """
        session = self._sessions.get(queue_id)
        if session is None:
            return
        changed = False
        for queue_item_id, outputs in list(session.outputs.items()):
            retained = {
                player_id: output
                for player_id, output in outputs.items()
                if player_id in player_ids
            }
            if retained != outputs:
                changed = True
                if retained:
                    session.outputs[queue_item_id] = retained
                else:
                    del session.outputs[queue_item_id]
        if not changed:
            return
        self.refresh(queue_id)

    def get_player_output(
        self,
        player_id: str,
        queue_item_id: str | None = None,
    ) -> AudioOutputPath | None:
        """
        Return the current output path for a player.

        :param player_id: Destination player identifier.
        :param queue_item_id: Queue item to inspect, defaulting to the current item.
        """
        if queue := self.mass.player_queues.get_active_queue(player_id):
            session = self._sessions.get(queue.queue_id)
            if queue_item_id is None and queue.current_item:
                queue_item_id = queue.current_item.queue_item_id
            outputs = self._get_outputs(session, queue_item_id) if session else {}
            output = outputs.get(player_id)
            return deepcopy(output) if output else None
        for session in self._sessions.values():
            for outputs in session.outputs.values():
                if output := outputs.get(player_id):
                    return deepcopy(output)
        return None

    def refresh(self, queue_id: str) -> None:
        """
        Publish a replacement snapshot when the current queue path changed.

        :param queue_id: Queue identifier.
        """
        session = self._sessions.get(queue_id)
        if session is None:
            return
        snapshot = self._build_snapshot(queue_id, session)
        if snapshot is None:
            return
        previous = session.last_snapshot
        if previous and _processing_chains_equal(previous, snapshot):
            return
        revision = self._revisions.get(queue_id, 0) + 1
        self._revisions[queue_id] = revision
        snapshot.revision = revision
        session.last_snapshot = snapshot
        self.mass.signal_event(
            EventType.AUDIO_PROCESSING_UPDATED,
            object_id=queue_id,
            data=deepcopy(snapshot),
        )

    def clear(self, queue_id: str, session_id: str | None = None) -> None:
        """
        Clear processing details for a queue.

        :param queue_id: Queue identifier.
        :param session_id: Only clear when this playback session is still active.
        """
        session = self._sessions.get(queue_id)
        if session is None or (session_id is not None and session.session_id != session_id):
            return
        del self._sessions[queue_id]
        self.mass.signal_event(
            EventType.AUDIO_PROCESSING_UPDATED,
            object_id=queue_id,
            data=None,
        )

    def _get_session(self, queue_id: str, session_id: str) -> _AudioProcessingSession | None:
        """Return a session only when the producer still owns the queue."""
        queue_data = self.mass.player_queues.queue_data_or_none(queue_id)
        if queue_data is None or queue_data.session_id != session_id:
            return None
        session = self._sessions.get(queue_id)
        if session is None or session.session_id != session_id:
            return None
        return session

    def _build_snapshot(
        self,
        queue_id: str,
        session: _AudioProcessingSession,
    ) -> AudioProcessingChain | None:
        """Build the current queue item's full processing snapshot."""
        queue = self.mass.player_queues.get(queue_id)
        if queue is None or queue.current_item is None:
            return None
        queue_data = self.mass.player_queues.queue_data_or_none(queue_id)
        if queue_data and queue.current_index is not None:
            played_item_ids = {
                item.queue_item_id for item in queue_data.items[: queue.current_index]
            }
            for played_item_id in played_item_ids:
                session.items.pop(played_item_id, None)
                session.outputs.pop(played_item_id, None)
        queue_item_id = queue.current_item.queue_item_id
        item = session.items.get(queue_item_id)
        input_details = deepcopy(item.input) if item else None
        queue_processing = deepcopy(item.queue_processing) if item else None
        outputs = self._group_outputs(
            self._get_outputs(session, queue_item_id),
            input_details,
            queue_processing,
            item.alters_audio if item else False,
        )
        state = (
            AudioProcessingState.READY
            if input_details is not None and queue_processing is not None and outputs
            else AudioProcessingState.PENDING
        )
        fidelity = None
        if outputs:
            qualities = [output.fidelity.quality for output in outputs]
            fidelity = AudioFidelitySummary(
                min_output_quality=min(qualities, key=_QUALITY_RANK.__getitem__),
                max_output_quality=max(qualities, key=_QUALITY_RANK.__getitem__),
            )
        return AudioProcessingChain(
            queue_id=queue_id,
            queue_item_id=queue_item_id,
            state=state,
            input=input_details,
            queue_processing=queue_processing,
            outputs=outputs,
            fidelity=fidelity,
        )

    def _sync_legacy_streamdetails(self, queue_id: str) -> None:
        """Update legacy StreamDetails DSP fields from the authoritative output plans."""
        queue = self.mass.player_queues.get(queue_id)
        if queue is None:
            return
        if queue.current_item and queue.current_item.streamdetails:
            queue.current_item.streamdetails.dsp = self.mass.streams.audio.get_stream_dsp_details(
                queue_id,
                queue.current_item.queue_item_id,
            )
        if queue.next_item and queue.next_item.streamdetails:
            queue.next_item.streamdetails.dsp = self.mass.streams.audio.get_stream_dsp_details(
                queue_id,
                queue.next_item.queue_item_id,
            )
        self.mass.player_queues.signal_update(queue_id)

    @staticmethod
    def _get_outputs(
        session: _AudioProcessingSession,
        queue_item_id: str | None,
    ) -> dict[str, AudioOutputPath]:
        """Return shared outputs overlaid with queue-item-specific outputs."""
        outputs = dict(session.outputs.get(None, {}))
        if queue_item_id is not None:
            outputs.update(session.outputs.get(queue_item_id, {}))
        return outputs

    def _group_outputs(
        self,
        player_outputs: dict[str, AudioOutputPath],
        input_details: AudioInputDetails | None,
        queue_processing: AudioQueueProcessing | None,
        alters_audio: bool,
    ) -> list[AudioOutputPath]:
        """Group players with identical effective output processing."""
        grouped: list[AudioOutputPath] = []
        for player_id, raw_output in sorted(player_outputs.items()):
            output = deepcopy(raw_output)
            output.player_ids = [player_id]
            output.fidelity = _get_output_fidelity(
                input_details,
                queue_processing,
                output,
                alters_audio,
            )
            for existing in grouped:
                if _output_paths_equal(existing, output, ignore_players=True):
                    existing.player_ids.append(player_id)
                    break
            else:
                grouped.append(output)
        return grouped


def get_audio_quality(audio_format: AudioFormat | None) -> AudioQuality:
    """
    Classify an audio format using server-owned codec semantics.

    :param audio_format: Audio format to classify.
    """
    if audio_format is None:
        return AudioQuality.UNKNOWN
    content_type = (
        audio_format.codec_type
        if audio_format.codec_type != ContentType.UNKNOWN
        else audio_format.content_type
    )
    if content_type == ContentType.UNKNOWN:
        return AudioQuality.UNKNOWN
    if content_type.is_lossless():
        if audio_format.bit_depth > 16 or audio_format.sample_rate > 48000:
            return AudioQuality.HI_RES
        return AudioQuality.LOSSLESS
    if not audio_format.bit_rate:
        return AudioQuality.UNKNOWN
    return AudioQuality.STANDARD if get_bit_rate(audio_format) >= 256 else AudioQuality.LOW


def get_input_details(streamdetails: StreamDetails) -> AudioInputDetails:
    """
    Return client-facing source and server input details.

    :param streamdetails: Effective stream details for a queue item.
    """
    return AudioInputDetails(
        source_format=deepcopy(streamdetails.audio_format),
        server_input_format=deepcopy(
            streamdetails.decoded_audio_format or streamdetails.audio_format
        ),
        fidelity=AudioFidelity(quality=get_audio_quality(streamdetails.audio_format)),
    )


def get_media_session_id(media: PlayerMedia) -> str | None:
    """
    Return the queue session carried by player media.

    :param media: Player media that started the stream.
    """
    value = (media.custom_data or {}).get("session_id")
    return value if isinstance(value, str) else None


def get_normalization_details(
    streamdetails: StreamDetails,
    applied_gain_db: float | None,
) -> AudioNormalizationDetails | None:
    """
    Return the effective normalization applied to a queue item.

    :param streamdetails: Effective stream details for a queue item.
    :param applied_gain_db: Static gain applied by the selected mode.
    """
    mode = streamdetails.volume_normalization_mode
    if mode in (None, VolumeNormalizationMode.DISABLED, VolumeNormalizationMode.UNKNOWN):
        return None
    assert mode is not None
    measurement_source = AudioNormalizationMeasurementSource.UNKNOWN
    measured_lufs: float | None = None
    reason_code: str | None = None
    if mode == VolumeNormalizationMode.DYNAMIC:
        measurement_source = AudioNormalizationMeasurementSource.LIVE
    elif mode == VolumeNormalizationMode.FIXED_GAIN:
        measurement_source = AudioNormalizationMeasurementSource.FALLBACK
        reason_code = "fixed_gain"
    elif streamdetails.prefer_album_loudness and streamdetails.loudness_album is not None:
        measurement_source = AudioNormalizationMeasurementSource.ALBUM
        measured_lufs = streamdetails.loudness_album
    elif streamdetails.loudness is not None:
        measurement_source = AudioNormalizationMeasurementSource.TRACK
        measured_lufs = streamdetails.loudness
    else:
        measurement_source = AudioNormalizationMeasurementSource.FALLBACK
        reason_code = "measurement_unavailable"
    return AudioNormalizationDetails(
        mode=mode,
        measurement_source=measurement_source,
        target_lufs=streamdetails.target_loudness,
        measured_lufs=measured_lufs,
        applied_gain_db=applied_gain_db,
        target_true_peak_dbtp=-2.0 if mode == VolumeNormalizationMode.DYNAMIC else None,
        target_loudness_range_lu=10.0 if mode == VolumeNormalizationMode.DYNAMIC else None,
        reason_code=reason_code,
    )


def _get_output_fidelity(
    input_details: AudioInputDetails | None,
    queue_processing: AudioQueueProcessing | None,
    output: AudioOutputPath,
    alters_audio: bool,
) -> AudioFidelity:
    """Return effective quality and bit-perfect state for an output path."""
    if input_details is None:
        return AudioFidelity()
    input_quality = input_details.fidelity.quality
    output_quality = get_audio_quality(output.output_format)
    if AudioQuality.UNKNOWN in (input_quality, output_quality):
        quality = AudioQuality.UNKNOWN
    else:
        quality = min((input_quality, output_quality), key=_QUALITY_RANK.__getitem__)
    return AudioFidelity(
        quality=quality,
        bit_perfect=_is_bit_perfect(
            input_details,
            queue_processing,
            output,
            alters_audio,
        ),
    )


def _is_bit_perfect(
    input_details: AudioInputDetails,
    queue_processing: AudioQueueProcessing | None,
    output: AudioOutputPath,
    alters_audio: bool,
) -> bool | None:
    """Return whether an output preserves the decoded source samples."""
    source_format = input_details.source_format
    output_format = output.output_format
    if source_format is None or output_format is None or queue_processing is None:
        return None
    if alters_audio:
        return False
    if get_audio_quality(source_format) not in (AudioQuality.LOSSLESS, AudioQuality.HI_RES):
        return False
    if get_audio_quality(output_format) not in (AudioQuality.LOSSLESS, AudioQuality.HI_RES):
        return False
    formats = [
        source_format,
        input_details.server_input_format,
        queue_processing.input_format,
        queue_processing.output_format,
        output.input_format,
        output.handoff_format or output_format,
        output_format,
    ]
    if any(audio_format is None for audio_format in formats):
        return None
    reference = source_format
    if any(
        audio_format.sample_rate != reference.sample_rate
        or audio_format.bit_depth != reference.bit_depth
        or audio_format.channels != reference.channels
        for audio_format in formats
        if audio_format is not None
    ):
        return False
    if (
        queue_processing.normalization is not None
        or queue_processing.tempo is not None
        or (
            queue_processing.crossfade is not None
            and queue_processing.crossfade.state
            in (AudioCrossfadeState.PENDING, AudioCrossfadeState.APPLIED)
        )
        or queue_processing.overlay is not None
    ):
        return False
    return not (
        output.dsp.state == DSPState.ENABLED
        or output.channels is not None
        or output.limiter.enabled
        or output.resampling is not None
        or output.dithering is not None
    )


def _processing_chains_equal(
    left: AudioProcessingChain,
    right: AudioProcessingChain,
) -> bool:
    """Compare all serialized processing fields except the revision."""
    left_data = left.to_dict()
    right_data = right.to_dict()
    left_data["revision"] = 0
    right_data["revision"] = 0
    return left_data == right_data


def _output_paths_equal(
    left: AudioOutputPath,
    right: AudioOutputPath,
    *,
    ignore_players: bool = False,
) -> bool:
    """Compare every serialized output field, optionally excluding destinations."""
    left_data: dict[str, Any] = left.to_dict()
    right_data: dict[str, Any] = right.to_dict()
    if ignore_players:
        left_data["player_ids"] = []
        right_data["player_ids"] = []
    return left_data == right_data
