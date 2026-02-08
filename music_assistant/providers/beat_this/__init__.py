"""Beat This audio analysis provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import numpy as np
from music_assistant_models.config_entries import ConfigEntry

from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from music_assistant.models.smart_fades import SmartFadesAnalysis, SmartFadesAnalysisFragment

from .processor import BeatThisStreamingProcessor

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant


SUPPORTED_FEATURES: set[ProviderFeature] = set()


@dataclass
class BeatThisSession:
    """Tracks an active beat tracking session."""

    processor: BeatThisStreamingProcessor
    item_id: str
    provider: str


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> BeatThisProvider:
    """Set up the Beat This provider."""
    return BeatThisProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return config entries for this provider."""
    return ()


class BeatThisProvider(AudioAnalysisProvider):
    """Beat This beat tracking audio analysis provider."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self._sessions: dict[str, BeatThisSession] = {}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.available = True

    async def start_analysis(
        self,
        queue_item: QueueItem,
        audio_format: AudioFormat,
    ) -> str:
        """Start beat tracking analysis for a new track.

        :param queue_item: The queue item being analyzed.
        :param audio_format: PCM format of the audio stream.
        :return: A unique session ID for this analysis.
        """
        if not queue_item.streamdetails:
            msg = "Queue item has no stream details"
            raise ValueError(msg)

        session_id = uuid4().hex

        processor = BeatThisStreamingProcessor(
            audio_format=audio_format,
            model_name="final0",
            device="cpu",
            block_seconds=10.0,
        )

        session = BeatThisSession(
            processor=processor,
            item_id=queue_item.streamdetails.item_id,
            provider=queue_item.streamdetails.provider,
        )

        self._sessions[session_id] = session
        self.logger.debug("Started beat tracking session %s", session_id)
        return session_id

    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """Process a PCM chunk for beat tracking.

        :param session_id: The analysis session ID.
        :param pcm_chunk: Raw PCM audio data.
        """
        session = self._sessions.get(session_id)
        if not session:
            return
        await session.processor.process_pcm_chunk(pcm_chunk)

    async def finalize(self, session_id: str) -> dict[str, Any]:
        """Finalize beat tracking, store results, and return them.

        :param session_id: The analysis session ID.
        :return: Dictionary with beats, downbeats, and duration.
        """
        session = self._sessions.pop(session_id, None)
        if not session:
            return {}

        result = await session.processor.finalize()
        if not result:
            return {}

        # Store the analysis results
        beats = result.get("beats", [])
        downbeats = result.get("downbeats", [])
        duration = result.get("duration", 0.0)

        if len(beats) < 2:
            self.logger.debug("Not enough beats detected, skipping storage")
            return result

        # Convert to numpy arrays if needed
        beats_arr = np.array(beats) if not isinstance(beats, np.ndarray) else beats
        downbeats_arr = np.array(downbeats) if not isinstance(downbeats, np.ndarray) else downbeats

        # Calculate BPM from beat intervals
        beat_intervals = np.diff(beats_arr)
        bpm = 60.0 / np.mean(beat_intervals)

        # Confidence based on beat interval consistency
        cv = float(np.std(beat_intervals) / np.mean(beat_intervals))
        confidence = max(0.0, min(1.0, 1.0 - cv))

        analysis = SmartFadesAnalysis(
            fragment=SmartFadesAnalysisFragment.FULL_SONG,
            bpm=float(bpm),
            beats=beats_arr,
            downbeats=downbeats_arr,
            confidence=confidence,
            duration=duration,
        )

        await self.mass.music.set_smart_fades_analysis(
            session.item_id,
            session.provider,
            analysis,
        )

        self.logger.info(
            "Stored beat analysis for %s: BPM=%.1f, %d beats, %d downbeats",
            session.item_id,
            bpm,
            len(beats_arr),
            len(downbeats_arr),
        )

        return result

    async def cancel(self, session_id: str) -> None:
        """Cancel a beat tracking session.

        :param session_id: The analysis session ID.
        """
        session = self._sessions.pop(session_id, None)
        if session:
            await session.processor.reset()
