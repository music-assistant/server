"""Sonic Analysis provider for Music Assistant.

Extracts audio features from PCM audio streams during playback and
stores them as semantic AudioAnalysisData fields.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from music_assistant.models.audio_analysis_provider import (
    AnalysisSessionData,
    AudioAnalysisProvider,
)

from .helpers import (
    BlockFeatures,
    collapse_to_analysis,
    extract_block_features,
    merge_block_features,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.models.audio_analysis import AudioAnalysisData

ANALYZE_FILE_SAMPLE_RATE: int = 22050
# Minimum audio length (1 second) required for meaningful feature extraction.
ANALYZE_FILE_MIN_SAMPLES: int = 22050


BLOCK_SECONDS: int = 10
OVERLAP_SAMPLES: int = 2048


@dataclass
class SonicSessionData(AnalysisSessionData):
    """Per-session state: PCM block buffer and accumulated per-block features."""

    pcm_buffer: bytearray = field(default_factory=bytearray)
    block_samples: int = 0
    accumulated: BlockFeatures = field(default_factory=BlockFeatures)
    total_samples: int = 0
    overlap: np.ndarray | None = None
    start_time: float = 0.0
    peak_absolute: float = 0.0
    waveform_peaks: list[float] = field(default_factory=list)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return SonicAnalysisProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    return ()


def _pcm_bytes_to_audio(
    pcm_data: bytes,
    sample_rate: int,
    bit_depth: int,
    channels: int,
) -> np.ndarray:
    """Convert raw PCM bytes to a mono float32 numpy array.

    :param pcm_data: Raw PCM audio bytes.
    :param sample_rate: Sample rate in Hz (unused in conversion, kept for API symmetry).
    :param bit_depth: Bits per sample (16, 24, or 32).
    :param channels: Number of audio channels.
    """
    _ = sample_rate
    if bit_depth == 16:
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        samples /= 32768.0
    elif bit_depth == 24:
        num_samples = len(pcm_data) // 3
        raw = np.frombuffer(pcm_data[: num_samples * 3], dtype=np.uint8).reshape(-1, 3)
        i32 = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int32) << 16)
        )
        i32[i32 >= 0x800000] -= 0x1000000
        samples = i32.astype(np.float32) / 8388608.0
    elif bit_depth == 32:
        samples = np.frombuffer(pcm_data, dtype=np.int32).astype(np.float32)
        samples /= 2147483648.0
    else:
        msg = f"Unsupported bit depth: {bit_depth}"
        raise ValueError(msg)

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples


class SonicAnalysisProvider(AudioAnalysisProvider):
    """Provider that extracts sonic features from audio streams."""

    analysis_version: int = 1

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""

    async def _start_analysis(
        self,
        session_id: str,
        streamdetails: StreamDetails,
        audio_format: AudioFormat,
    ) -> bool:
        """Initialize a new sonic analysis session.

        :param session_id: Unique session ID created by the controller.
        :param streamdetails: Details about the stream being analyzed.
        :param audio_format: PCM format of the audio stream.
        """
        bytes_per_sample = audio_format.bit_depth // 8
        block_bytes = (
            audio_format.sample_rate * bytes_per_sample * audio_format.channels * BLOCK_SECONDS
        )
        if block_bytes <= 0:
            self.logger.warning(
                "Invalid audio format for session %s (sample_rate=%d, bit_depth=%d, channels=%d)"
                " — skipping analysis",
                session_id,
                audio_format.sample_rate,
                audio_format.bit_depth,
                audio_format.channels,
            )
            return False
        base = self._sessions[session_id]
        self._sessions[session_id] = SonicSessionData(
            streamdetails=base.streamdetails,
            audio_format=base.audio_format,
            block_samples=block_bytes,
            start_time=time.monotonic(),
        )
        self.logger.debug(
            "Started sonic analysis for %s/%s", streamdetails.provider, streamdetails.item_id
        )
        return True

    async def process_pcm_chunk(
        self,
        session_id: str,
        pcm_chunk: bytes,
    ) -> None:
        """Accumulate PCM and extract features when a 10-second block is full.

        :param session_id: The analysis session ID.
        :param pcm_chunk: Raw PCM audio data.
        """
        if session_id not in self._sessions:
            return
        session = self._sessions[session_id]
        assert isinstance(session, SonicSessionData)
        session.pcm_buffer.extend(pcm_chunk)
        af = session.audio_format
        while len(session.pcm_buffer) >= session.block_samples:
            block_bytes = bytes(session.pcm_buffer[: session.block_samples])
            del session.pcm_buffer[: session.block_samples]
            audio = _pcm_bytes_to_audio(block_bytes, af.sample_rate, af.bit_depth, af.channels)
            session.total_samples += len(audio)
            block_peak = float(np.max(np.abs(audio)))
            session.peak_absolute = max(session.peak_absolute, block_peak)
            session.waveform_peaks.append(block_peak)
            if session.overlap is not None:
                audio = np.concatenate([session.overlap, audio])
            session.overlap = audio[-OVERLAP_SAMPLES:].copy()
            bf = await asyncio.to_thread(extract_block_features, audio, af.sample_rate)
            if bf is not None:
                merge_block_features(session.accumulated, bf)

    async def _finalize(self, session_id: str) -> None:
        """Process remaining PCM, collapse features, and store analysis data.

        :param session_id: The analysis session ID.
        """
        if session_id not in self._sessions:
            return
        session = self._sessions[session_id]
        assert isinstance(session, SonicSessionData)
        sd = session.streamdetails
        af = session.audio_format

        # Flush any remaining PCM as a final partial block
        if session.pcm_buffer:
            audio = _pcm_bytes_to_audio(
                bytes(session.pcm_buffer), af.sample_rate, af.bit_depth, af.channels
            )
            session.total_samples += len(audio)
            block_peak = float(np.max(np.abs(audio)))
            session.peak_absolute = max(session.peak_absolute, block_peak)
            session.waveform_peaks.append(block_peak)
            if session.overlap is not None:
                audio = np.concatenate([session.overlap, audio])
            bf = await asyncio.to_thread(extract_block_features, audio, af.sample_rate)
            if bf is not None:
                merge_block_features(session.accumulated, bf)
            session.pcm_buffer.clear()

        if not session.accumulated.mfcc_frames:
            self.logger.debug("No feature blocks for session %s, skipping", session_id)
            return

        analysis = await asyncio.to_thread(
            collapse_to_analysis, session.accumulated, af.sample_rate
        )

        # Fill in fields that need session-level state
        analysis.duration = session.total_samples / af.sample_rate
        if session.peak_absolute > 0:
            analysis.true_peak = float(20.0 * np.log10(session.peak_absolute))
        else:
            analysis.true_peak = -96.0

        # Build 800-bin waveform from per-block peaks
        if session.waveform_peaks:
            peaks = np.array(session.waveform_peaks, dtype=np.float32)
            if len(peaks) >= 800:
                bin_edges = np.linspace(0, len(peaks), 801, dtype=int)
                waveform = np.array(
                    [peaks[bin_edges[i] : bin_edges[i + 1]].max() for i in range(800)],
                    dtype=np.float32,
                )
            else:
                indices = np.linspace(0, len(peaks) - 1, 800, dtype=int)
                waveform = peaks[indices]
            wf_max = waveform.max()
            if wf_max > 0:
                waveform = waveform / wf_max
            analysis.wave_form = waveform

        await self.mass.streams.audio_analysis.set_audio_analysis(
            item_id=sd.item_id,
            provider_instance_id_or_domain=sd.provider,
            aa_provider_domain=self.domain,
            analysis=analysis,
            analysis_version=self.analysis_version,
            media_type=sd.media_type,
        )
        elapsed = time.monotonic() - session.start_time
        self.logger.debug(
            "Stored analysis for %s/%s (%.1fs elapsed)",
            sd.provider,
            sd.item_id,
            elapsed,
        )

    async def analyze_file(
        self, streamdetails: StreamDetails
    ) -> AudioAnalysisData | None:
        """Run librosa analysis directly on a local audio file for background scan.

        :param streamdetails: StreamDetails pointing at a local file path.
        """
        if not isinstance(streamdetails.path, str) or not streamdetails.path:
            return None
        try:
            import librosa  # noqa: PLC0415
        except ImportError:
            return None
        try:
            audio, _sr = await asyncio.to_thread(
                librosa.load,
                streamdetails.path,
                sr=ANALYZE_FILE_SAMPLE_RATE,
                mono=True,
            )
        except Exception as err:
            self.logger.debug(
                "analyze_file: load failed for %s/%s: %s",
                streamdetails.provider,
                streamdetails.item_id,
                err,
            )
            return None
        if len(audio) < ANALYZE_FILE_MIN_SAMPLES:
            return None

        bf = await asyncio.to_thread(
            extract_block_features, audio, ANALYZE_FILE_SAMPLE_RATE
        )
        if bf is None:
            return None
        analysis = await asyncio.to_thread(
            collapse_to_analysis, bf, ANALYZE_FILE_SAMPLE_RATE
        )
        analysis.duration = len(audio) / ANALYZE_FILE_SAMPLE_RATE
        peak = float(np.max(np.abs(audio)))
        analysis.true_peak = (
            float(20.0 * np.log10(peak)) if peak > 0 else -96.0
        )
        return analysis
