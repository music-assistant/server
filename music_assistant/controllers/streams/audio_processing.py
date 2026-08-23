"""Runtime audio processing details for queue streams."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from music_assistant_models.audio_processing import (
    AudioFidelity,
    AudioNormalizationDetails,
    AudioNormalizationMeasurementSource,
    AudioOutputDetails,
    AudioProcessingChain,
    AudioQuality,
    AudioQueueProcessing,
)
from music_assistant_models.dsp import DSPState
from music_assistant_models.enums import ContentType, CrossfadeMode, VolumeNormalizationMode

from music_assistant.helpers.audio import get_bit_rate

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.helpers.dsp import ComplexFilter
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

    filter_params: list[str | ComplexFilter]
    output_details: AudioOutputDetails
    input_format: AudioFormat
    handoff_format: AudioFormat | None = None
    dsp_config_id: str | None = None


@dataclass(slots=True)
class _AudioProcessingItem:
    """Processing details cached for one queue item."""

    queue_processing: AudioQueueProcessing | None = None
    input_format: AudioFormat | None = None
    alters_audio: bool = False


@dataclass(slots=True)
class _AudioOutputEntry:
    """Client-facing output details with private intermediate formats."""

    details: AudioOutputDetails
    input_format: AudioFormat
    handoff_format: AudioFormat | None = None
    dsp_config_id: str | None = None


@dataclass(slots=True)
class _AudioProcessingSession:
    """Runtime processing state for one queue playback session."""

    session_id: str
    items: dict[str, _AudioProcessingItem] = field(default_factory=dict)
    outputs: dict[str | None, dict[str, _AudioOutputEntry]] = field(default_factory=dict)
    shared_output_templates: dict[str | None, _AudioOutputEntry] = field(default_factory=dict)


class AudioProcessingManager:
    """Build and attach effective audio processing chains to stream details."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the audio processing manager.

        :param mass: Music Assistant instance.
        """
        self.mass = mass
        self._sessions: dict[str, _AudioProcessingSession] = {}

    def start_session(self, queue_id: str, session_id: str) -> None:
        """
        Start tracking a queue playback session.

        :param queue_id: Queue identifier.
        :param session_id: Internal queue playback session identifier.
        """
        current = self._sessions.get(queue_id)
        if current and current.session_id == session_id:
            return
        if self._clear_streamdetails(queue_id):
            self.mass.player_queues.signal_update(queue_id)
        self._sessions[queue_id] = _AudioProcessingSession(session_id=session_id)

    def update_item_context(
        self,
        queue_id: str,
        session_id: str,
        queue_item_id: str,
        queue_processing: AudioQueueProcessing,
        *,
        alters_audio: bool = False,
    ) -> None:
        """
        Store shared processing selected for a queue item.

        :param queue_id: Queue identifier.
        :param session_id: Internal queue playback session identifier.
        :param queue_item_id: Queue item identifier.
        :param queue_processing: Effective shared processing.
        :param alters_audio: Whether an intentionally hidden transform alters samples.
        """
        session = self._get_session(queue_id, session_id)
        if session is None:
            return
        self._prune_played_items(queue_id, session)
        if self._is_played_item(queue_id, queue_item_id):
            return
        item = session.items.setdefault(queue_item_id, _AudioProcessingItem())
        previous = item.queue_processing
        item.queue_processing = AudioQueueProcessing(
            pcm_format=deepcopy(queue_processing.pcm_format),
            normalization=deepcopy(
                previous.normalization if previous else queue_processing.normalization
            ),
            playback_speed=queue_processing.playback_speed,
            crossfade_mode=queue_processing.crossfade_mode,
            overlay_active=queue_processing.overlay_active,
        )
        item.alters_audio = alters_audio
        self._publish_item(queue_id, queue_item_id, session)

    def update_item_runtime(
        self,
        queue_id: str,
        session_id: str,
        queue_item_id: str,
        input_format: AudioFormat,
        pcm_format: AudioFormat,
        normalization: AudioNormalizationDetails | None,
        playback_speed: float,
        *,
        alters_audio: bool = False,
    ) -> None:
        """
        Store shared processing constructed for a queue item.

        :param queue_id: Queue identifier.
        :param session_id: Internal queue playback session identifier.
        :param queue_item_id: Queue item identifier.
        :param input_format: PCM format entering shared queue processing.
        :param pcm_format: Internal PCM format leaving shared processing.
        :param normalization: Effective normalization details.
        :param playback_speed: Effective playback-speed conversion.
        :param alters_audio: Whether an intentionally hidden transform alters samples.
        """
        session = self._get_session(queue_id, session_id)
        if session is None:
            return
        self._prune_played_items(queue_id, session)
        if self._is_played_item(queue_id, queue_item_id):
            return
        item = session.items.setdefault(queue_item_id, _AudioProcessingItem())
        previous = item.queue_processing or AudioQueueProcessing()
        item.input_format = deepcopy(input_format)
        item.queue_processing = AudioQueueProcessing(
            pcm_format=deepcopy(pcm_format),
            normalization=deepcopy(normalization),
            playback_speed=playback_speed,
            crossfade_mode=previous.crossfade_mode,
            overlay_active=previous.overlay_active,
        )
        item.alters_audio = item.alters_audio or alters_audio
        self._publish_item(queue_id, queue_item_id, session)

    def update_output(
        self,
        player_id: str,
        output_plan: AudioOutputPlan,
        *,
        shared_player_ids: Iterable[str] | None = None,
        queue_id: str,
        session_id: str,
        queue_item_id: str | None = None,
    ) -> bool:
        """
        Store an effective player output.

        :param player_id: Destination player identifier.
        :param output_plan: Effective output processing and private intermediate formats.
        :param shared_player_ids: Additional players receiving this identical output path.
            An empty iterable marks a path that can gain shared destinations later.
        :param queue_id: Queue identifier that owns the output.
        :param session_id: Queue session identifier that owns the output.
        :param queue_item_id: Queue item for single-item output, or None for flow output.
        :return: Whether the effective output changed.
        """
        session = self._get_session(queue_id, session_id)
        if session is None:
            return False
        self._prune_played_items(queue_id, session)
        if queue_item_id is not None and self._is_played_item(queue_id, queue_item_id):
            return False

        destination_player_ids = {player_id}
        if shared_player_ids is not None:
            destination_player_ids.update(shared_player_ids)

        entries: dict[str, _AudioOutputEntry] = {}
        for destination_player_id in destination_player_ids:
            entry = _AudioOutputEntry(
                details=deepcopy(output_plan.output_details),
                input_format=deepcopy(output_plan.input_format),
                handoff_format=deepcopy(output_plan.handoff_format),
                dsp_config_id=output_plan.dsp_config_id,
            )
            entry.details.player_ids = [destination_player_id]
            entries[destination_player_id] = entry

        if shared_player_ids is None:
            session.shared_output_templates.pop(queue_item_id, None)
        else:
            session.shared_output_templates[queue_item_id] = deepcopy(entries[player_id])

        item_outputs = session.outputs.setdefault(queue_item_id, {})
        changed_entries = {
            destination_player_id: entry
            for destination_player_id, entry in entries.items()
            if item_outputs.get(destination_player_id) != entry
        }
        if not changed_entries:
            return False
        item_outputs.update(changed_entries)
        current_changed = self._publish_all(queue_id, session)
        queue = self.mass.player_queues.get(queue_id)
        if current_changed or (
            queue
            and queue.current_item
            and queue_item_id in (None, queue.current_item.queue_item_id)
        ):
            self.mass.player_queues.signal_update(queue_id)
        return True

    def retain_outputs(self, queue_id: str, player_ids: set[str]) -> bool:
        """
        Reconcile outputs with players attached to a queue.

        :param queue_id: Queue identifier.
        :param player_ids: Player identifiers that belong to the output.
        :return: Whether reconciliation published an updated current chain.
        """
        session = self._sessions.get(queue_id)
        if session is None:
            return False
        changed = False
        for queue_item_id, outputs in list(session.outputs.items()):
            retained = {
                player_id: output
                for player_id, output in outputs.items()
                if player_id in player_ids
            }
            if template := session.shared_output_templates.get(queue_item_id):
                added_player_ids = player_ids - retained.keys()
                if queue_id not in outputs:
                    added_player_ids.discard(queue_id)
                for player_id in sorted(added_player_ids):
                    retained[player_id] = deepcopy(template)
                    retained[player_id].details.player_ids = [player_id]
            if retained == outputs:
                continue
            changed = True
            if retained:
                session.outputs[queue_item_id] = retained
            else:
                del session.outputs[queue_item_id]
                session.shared_output_templates.pop(queue_item_id, None)
        if not changed:
            return False
        current_changed = self._publish_all(queue_id, session)
        if current_changed:
            self.mass.player_queues.signal_update(queue_id)
        return current_changed

    def update_player_dsp_preset(self, player_id: str, preset_id: str | None) -> None:
        """
        Update preset identity where the effective DSP config remains unchanged.

        :param player_id: Player whose persisted DSP config changed.
        :param preset_id: Selected preset identifier, or None when cleared.
        """
        for queue_id, session in tuple(self._sessions.items()):
            changed = False
            for outputs in session.outputs.values():
                for entry in outputs.values():
                    if (
                        entry.dsp_config_id == player_id
                        and entry.details.dsp.preset_id != preset_id
                    ):
                        entry.details.dsp.preset_id = preset_id
                        changed = True
            for entry in session.shared_output_templates.values():
                if entry.dsp_config_id == player_id:
                    entry.details.dsp.preset_id = preset_id
            if changed and self._publish_all(queue_id, session):
                self.mass.player_queues.signal_update(queue_id)

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
        if self._clear_streamdetails(queue_id):
            self.mass.player_queues.signal_update(queue_id)

    def prune(self, queue_id: str) -> None:
        """
        Drop processing state for completed queue items.

        :param queue_id: Queue identifier.
        """
        if session := self._sessions.get(queue_id):
            self._prune_played_items(queue_id, session)

    def _get_session(self, queue_id: str, session_id: str) -> _AudioProcessingSession | None:
        """Return a session only when the producer still owns the queue."""
        queue_data = self.mass.player_queues.queue_data_or_none(queue_id)
        if queue_data is None or queue_data.session_id != session_id:
            return None
        session = self._sessions.get(queue_id)
        if session is None or session.session_id != session_id:
            return None
        return session

    def _publish_all(self, queue_id: str, session: _AudioProcessingSession) -> bool:
        """Attach complete chains for every prepared item."""
        queue = self.mass.player_queues.get(queue_id)
        self._prune_played_items(queue_id, session)
        current_item_id = queue.current_item.queue_item_id if queue and queue.current_item else None
        current_changed = False
        for queue_item_id in tuple(session.items):
            if self._publish_item(queue_id, queue_item_id, session, signal_update=False):
                current_changed |= queue_item_id == current_item_id
        return current_changed

    def _publish_item(
        self,
        queue_id: str,
        queue_item_id: str,
        session: _AudioProcessingSession,
        *,
        signal_update: bool = True,
    ) -> bool:
        """Attach one complete chain to its StreamDetails."""
        queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
        if queue_item is None or queue_item.streamdetails is None:
            return False
        item = session.items.get(queue_item_id)
        output_entries = self._get_outputs(session, queue_item_id)
        chain = None
        if item and item.queue_processing and output_entries:
            chain = AudioProcessingChain(
                input_fidelity=AudioFidelity(
                    quality=get_audio_quality(queue_item.streamdetails.audio_format)
                ),
                queue_processing=deepcopy(item.queue_processing),
                outputs=self._group_outputs(
                    queue_item.streamdetails,
                    item,
                    output_entries,
                ),
            )
        previous = queue_item.streamdetails.audio_processing
        if previous == chain:
            return False
        queue_item.streamdetails.audio_processing = chain
        queue = self.mass.player_queues.get(queue_id)
        if (
            signal_update
            and queue
            and queue.current_item
            and queue.current_item.queue_item_id == queue_item_id
        ):
            self.mass.player_queues.signal_update(queue_id)
        return True

    def _group_outputs(
        self,
        streamdetails: StreamDetails,
        item: _AudioProcessingItem,
        player_outputs: dict[str, _AudioOutputEntry],
    ) -> list[AudioOutputDetails]:
        """Group players with identical effective output processing."""
        grouped: list[AudioOutputDetails] = []
        for player_id, entry in sorted(player_outputs.items()):
            output = deepcopy(entry.details)
            output.player_ids = [player_id]
            output.fidelity = _get_output_fidelity(streamdetails, item, entry)
            for existing in grouped:
                if _output_details_equal_ignoring_players(existing, output):
                    existing.player_ids.append(player_id)
                    break
            else:
                grouped.append(output)
        return grouped

    @staticmethod
    def _get_outputs(
        session: _AudioProcessingSession,
        queue_item_id: str | None,
    ) -> dict[str, _AudioOutputEntry]:
        """Return shared outputs overlaid with queue-item-specific outputs."""
        outputs = dict(session.outputs.get(None, {}))
        if queue_item_id is not None:
            outputs.update(session.outputs.get(queue_item_id, {}))
        return outputs

    def _prune_played_items(
        self,
        queue_id: str,
        session: _AudioProcessingSession,
    ) -> None:
        """Drop processing state for items before the current queue index."""
        queue_data = self.mass.player_queues.queue_data_or_none(queue_id)
        if queue_data is None or queue_data.queue.current_index is None:
            return
        for queue_item in queue_data.items[: queue_data.queue.current_index]:
            session.items.pop(queue_item.queue_item_id, None)
            session.outputs.pop(queue_item.queue_item_id, None)
            session.shared_output_templates.pop(queue_item.queue_item_id, None)
            if queue_item.streamdetails:
                queue_item.streamdetails.audio_processing = None

    def _is_played_item(self, queue_id: str, queue_item_id: str) -> bool:
        """Return whether an item precedes the queue's current index."""
        queue_data = self.mass.player_queues.queue_data_or_none(queue_id)
        if queue_data is None or queue_data.queue.current_index is None:
            return False
        return any(
            item.queue_item_id == queue_item_id
            for item in queue_data.items[: queue_data.queue.current_index]
        )

    def _clear_streamdetails(self, queue_id: str) -> bool:
        """Clear attached chains and return whether the current item changed."""
        queue_data = self.mass.player_queues.queue_data_or_none(queue_id)
        if queue_data is None:
            return False
        current_item = queue_data.queue.current_item
        current_changed = bool(
            current_item
            and current_item.streamdetails
            and current_item.streamdetails.audio_processing
        )
        for queue_item in queue_data.items:
            if queue_item.streamdetails:
                queue_item.streamdetails.audio_processing = None
        return current_changed


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


def get_media_session_id(media: PlayerMedia) -> str | None:
    """
    Return the queue session carried by player media.

    :param media: Player media that started the stream.
    """
    return media.queue_session_id


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
    if mode == VolumeNormalizationMode.SOURCE:
        # the source set the level without saying to what, and a measurement of our
        # own would describe audio it already levelled, so only the mode is known
        return AudioNormalizationDetails(mode=mode)
    measurement_source = AudioNormalizationMeasurementSource.UNKNOWN
    measured_lufs: float | None = None
    if mode == VolumeNormalizationMode.DYNAMIC:
        measurement_source = AudioNormalizationMeasurementSource.LIVE
    elif mode == VolumeNormalizationMode.FIXED_GAIN:
        measurement_source = AudioNormalizationMeasurementSource.FALLBACK
    elif streamdetails.prefer_album_loudness and streamdetails.loudness_album is not None:
        measurement_source = AudioNormalizationMeasurementSource.ALBUM
        measured_lufs = streamdetails.loudness_album
    elif streamdetails.loudness is not None:
        measurement_source = AudioNormalizationMeasurementSource.TRACK
        measured_lufs = streamdetails.loudness
    else:
        measurement_source = AudioNormalizationMeasurementSource.FALLBACK
    return AudioNormalizationDetails(
        mode=mode,
        measurement_source=measurement_source,
        target_lufs=streamdetails.target_loudness,
        measured_lufs=measured_lufs,
        applied_gain_db=applied_gain_db,
    )


def _get_output_fidelity(
    streamdetails: StreamDetails,
    item: _AudioProcessingItem,
    output: _AudioOutputEntry,
) -> AudioFidelity:
    """Return effective quality and bit-perfect state for an output."""
    input_quality = get_audio_quality(streamdetails.audio_format)
    output_quality = get_audio_quality(output.details.output_format)
    if AudioQuality.UNKNOWN in (input_quality, output_quality):
        quality = AudioQuality.UNKNOWN
    else:
        quality = min((input_quality, output_quality), key=_QUALITY_RANK.__getitem__)
    return AudioFidelity(
        quality=quality,
        bit_perfect=_is_bit_perfect(streamdetails, item, output),
    )


def _is_bit_perfect(
    streamdetails: StreamDetails,
    item: _AudioProcessingItem,
    output: _AudioOutputEntry,
) -> bool | None:
    """Return whether an output preserves the decoded source samples."""
    source_format = streamdetails.audio_format
    queue_processing = item.queue_processing
    output_format = output.details.output_format
    if output_format is None or queue_processing is None or queue_processing.pcm_format is None:
        return None
    if item.alters_audio:
        return False
    if get_audio_quality(output_format) not in (AudioQuality.LOSSLESS, AudioQuality.HI_RES):
        return False
    formats = [source_format]
    if streamdetails.decoded_audio_format:
        formats.append(streamdetails.decoded_audio_format)
    formats.extend(
        (
            item.input_format or queue_processing.pcm_format,
            queue_processing.pcm_format,
            output.input_format,
            output.handoff_format or output_format,
            output_format,
        )
    )
    reference = source_format
    if any(
        audio_format.sample_rate != reference.sample_rate
        or audio_format.bit_depth != reference.bit_depth
        or audio_format.channels != reference.channels
        for audio_format in formats
    ):
        return False
    # a step the source performed is reported for context but leaves our path untouched
    if (
        (
            queue_processing.normalization is not None
            and queue_processing.normalization.mode != VolumeNormalizationMode.SOURCE
        )
        or queue_processing.playback_speed != 1.0
        or queue_processing.crossfade_mode not in (CrossfadeMode.DISABLED, CrossfadeMode.SOURCE)
        or queue_processing.overlay_active
    ):
        return False
    details = output.details
    # an enabled DSP with no effective filters and no gain leaves samples untouched
    dsp_alters_audio = details.dsp.state == DSPState.ENABLED and (
        bool(details.dsp.filters) or details.dsp.input_gain != 0 or details.dsp.output_gain != 0
    )
    return not (dsp_alters_audio or details.source_channel is not None)


def _output_details_equal_ignoring_players(
    left: AudioOutputDetails,
    right: AudioOutputDetails,
) -> bool:
    """Compare output details without their destination players."""
    return replace(left, player_ids=[]) == replace(right, player_ids=[])
