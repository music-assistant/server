"""Streaming Feature Extractor - Extracts audio features incrementally during playback."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import librosa
import numpy as np
import numpy.typing as npt
from scipy import signal

from music_assistant.controllers.streams.smart_fades.feature_accumulator import (
    FeatureAccumulator,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

    from music_assistant.controllers.streams.streams_controller import StreamsController


class StreamingFeatureExtractor:
    """Extracts audio features incrementally as PCM audio streams.

    This class processes PCM audio chunks and extracts lightweight features
    (onset envelope, chroma, RMS, spectral centroid, low-frequency RMS,
    mid-frequency RMS) that can be accumulated without holding the entire
    audio in memory.

    Key implementation details:
    - Accumulates raw audio bytes, processes in batches via asyncio.to_thread
    - Uses librosa with center=False for streaming compatibility
    - Maintains overlap buffer between processing batches for STFT continuity
    - Supports abort detection when user seeks or skips
    - queue_chunk is 100% non-blocking (just appends to a list)
    """

    # Process accumulated audio every N seconds worth of data
    PROCESS_INTERVAL_SECONDS = 10.0
    # Maximum audio buffer size in bytes (~20 seconds at 48kHz stereo float32)
    MAX_BUFFER_BYTES = 48000 * 2 * 4 * 20

    def __init__(
        self,
        streams: StreamsController,
        sample_rate: int = 48000,
        hop_length: int = 512,
        n_fft: int = 2048,
    ) -> None:
        """Initialize the streaming feature extractor.

        :param streams: Reference to the StreamsController for queue state access.
        :param sample_rate: Audio sample rate in Hz.
        :param hop_length: Number of samples between successive frames.
        :param n_fft: FFT window size.
        """
        self.streams = streams
        self.sample_rate = sample_rate
        self.hop_length = hop_length
        self.n_fft = n_fft
        self.logger = streams.logger.getChild("streaming_extractor")

        # Feature accumulator
        self.accumulator = FeatureAccumulator()
        self.accumulator.sample_rate = sample_rate
        self.accumulator.hop_length = hop_length

        # Overlap buffer for STFT continuity (n_fft - hop_length samples)
        self._overlap_size = n_fft - hop_length
        self._overlap_buffer: npt.NDArray[np.float32] | None = None

        # Session context for abort detection
        self._track_id: str = ""
        self._provider_id: str = ""
        self._queue_id: str = ""
        self._session_id: str = ""
        self._queue_item_id: str = ""

        # State
        self._aborted: bool = False
        self._started: bool = False
        self._finalized: bool = False

        # Audio buffer for batch processing (simple list, no threading)
        self._audio_buffer: list[bytes] = []
        self._audio_buffer_bytes: int = 0
        self._pcm_format: AudioFormat | None = None
        self._processing: bool = False

        # Calculate bytes threshold for processing
        # PROCESS_INTERVAL_SECONDS worth of stereo float32 audio
        self._bytes_threshold = int(sample_rate * 2 * 4 * self.PROCESS_INTERVAL_SECONDS)

    def start_extraction(
        self,
        track_id: str,
        provider_id: str,
        queue_id: str,
        session_id: str,
        queue_item_id: str,
    ) -> None:
        """Initialize extraction for a new track.

        :param track_id: The track's item ID.
        :param provider_id: The provider instance ID or domain.
        :param queue_id: The queue ID.
        :param session_id: The queue session ID for abort detection.
        :param queue_item_id: The queue item ID for abort detection.
        """
        self._track_id = track_id
        self._provider_id = provider_id
        self._queue_id = queue_id
        self._session_id = session_id
        self._queue_item_id = queue_item_id

        self._aborted = False
        self._started = True
        self._finalized = False
        self._overlap_buffer = None
        self._audio_buffer.clear()
        self._audio_buffer_bytes = 0
        self._processing = False
        self.accumulator.clear()

        self.logger.debug(
            "Started feature extraction for track %s (provider: %s, queue: %s)",
            track_id,
            provider_id,
            queue_id,
        )

    def queue_chunk(self, chunk: bytes, pcm_format: AudioFormat) -> None:
        """Queue a chunk for later batch processing.

        This method is 100% non-blocking - it just appends to a list.
        Processing happens asynchronously when process_pending() is called.

        :param chunk: Raw PCM audio bytes.
        :param pcm_format: Audio format specification.
        """
        if self._aborted or not self._started or self._finalized:
            return

        # Skip if buffer is too large (prevent unbounded memory growth)
        if self._audio_buffer_bytes >= self.MAX_BUFFER_BYTES:
            return

        # Store format for processing
        self._pcm_format = pcm_format

        # Add to buffer (this is the only operation - no locks, no threads)
        self._audio_buffer.append(chunk)
        self._audio_buffer_bytes += len(chunk)

    def should_process(self) -> bool:
        """Check if there's enough accumulated audio to process.

        :return: True if processing should be triggered.
        """
        # Check _processing to avoid creating redundant tasks.
        # When processing finishes, buffer is cleared (bytes=0), so it will take
        # another PROCESS_INTERVAL_SECONDS to reach threshold again.
        return (
            self._audio_buffer_bytes >= self._bytes_threshold
            and not self._processing
            and not self._finalized
            and not self._aborted
        )

    async def process_pending(self) -> None:
        """Process accumulated audio buffer asynchronously.

        This should be called periodically from the streaming loop.
        Each librosa call runs in a separate to_thread with yields between them.
        """
        if self._processing or not self._audio_buffer or self._pcm_format is None:
            return

        if self._aborted or self._finalized:
            return

        self._processing = True
        try:
            # Take the current buffer
            buffer_to_process = self._audio_buffer
            self._audio_buffer = []
            self._audio_buffer_bytes = 0

            # Combine chunks
            combined = b"".join(buffer_to_process)
            pcm_format = self._pcm_format

            # Prepare mono audio (quick, no need for thread)
            mono_audio = self._prepare_mono_audio(combined, pcm_format)
            if mono_audio is None or len(mono_audio) < self.n_fft:
                return

            # Extract each feature type in separate to_thread calls with yields between
            # This prevents blocking the event loop for too long

            # 1. Onset envelope
            await asyncio.sleep(0)  # Yield to event loop
            await asyncio.to_thread(self._extract_onset, mono_audio)

            # 2. Chroma
            await asyncio.sleep(0)  # Yield to event loop
            await asyncio.to_thread(self._extract_chroma, mono_audio)

            # 3. RMS
            await asyncio.sleep(0)  # Yield to event loop
            await asyncio.to_thread(self._extract_rms, mono_audio)

            # 4. Spectral centroid
            await asyncio.sleep(0)  # Yield to event loop
            await asyncio.to_thread(self._extract_centroid, mono_audio)

            # 5. Low-frequency RMS (for kick drum / downbeat detection)
            await asyncio.sleep(0)  # Yield to event loop
            await asyncio.to_thread(self._extract_low_freq_rms, mono_audio)

            # 6. Mid-frequency RMS (for snare detection / kick-snare pattern)
            await asyncio.sleep(0)  # Yield to event loop
            await asyncio.to_thread(self._extract_mid_freq_rms, mono_audio)
        except Exception as e:
            self.logger.warning("Feature extraction failed: %s", e)
        finally:
            self._processing = False

    def _prepare_mono_audio(
        self, chunk: bytes, pcm_format: AudioFormat
    ) -> npt.NDArray[np.float32] | None:
        """Prepare mono audio from PCM chunk."""
        # Convert bytes to numpy array
        audio_array = np.frombuffer(chunk, dtype=np.float32)

        # Convert to mono if stereo
        if pcm_format.channels > 1:
            samples_per_channel = len(audio_array) // pcm_format.channels
            valid_samples = samples_per_channel * pcm_format.channels
            if valid_samples != len(audio_array):
                audio_array = audio_array[:valid_samples]
            audio_array = audio_array.reshape(-1, pcm_format.channels)
            mono_audio: npt.NDArray[np.float32] = np.asarray(
                np.mean(audio_array, axis=1), dtype=np.float32
            )
        else:
            mono_audio = np.asarray(audio_array, dtype=np.float32)

        # Track samples for duration calculation
        self.accumulator.add_samples_processed(len(mono_audio))

        # Prepend overlap buffer if we have one
        if self._overlap_buffer is not None and len(self._overlap_buffer) > 0:
            mono_audio = np.concatenate([self._overlap_buffer, mono_audio])

        # Need at least n_fft samples to compute features
        if len(mono_audio) < self.n_fft:
            self._overlap_buffer = mono_audio.copy()
            return None

        # Save overlap for next chunk
        self._overlap_buffer = mono_audio[-self._overlap_size :].copy()
        return mono_audio

    def _extract_onset(self, mono_audio: npt.NDArray[np.float32]) -> None:
        """Extract onset envelope (runs in thread pool)."""
        try:
            onset_env = librosa.onset.onset_strength(
                y=np.asarray(mono_audio),
                sr=self.sample_rate,
                hop_length=self.hop_length,
                center=False,
            )
            self.accumulator.add_onset_envelope(np.asarray(onset_env, dtype=np.float32))
        except Exception as e:
            self.logger.debug("Onset extraction failed: %s", e)

    def _extract_chroma(self, mono_audio: npt.NDArray[np.float32]) -> None:
        """Extract chroma features (runs in thread pool)."""
        try:
            chroma = librosa.feature.chroma_stft(
                y=np.asarray(mono_audio),
                sr=self.sample_rate,
                hop_length=self.hop_length,
                n_fft=self.n_fft,
                center=False,
            )
            self.accumulator.add_chroma(np.asarray(chroma, dtype=np.float32))
        except Exception as e:
            self.logger.debug("Chroma extraction failed: %s", e)

    def _extract_rms(self, mono_audio: npt.NDArray[np.float32]) -> None:
        """Extract RMS energy (runs in thread pool)."""
        try:
            rms = librosa.feature.rms(
                y=np.asarray(mono_audio),
                frame_length=self.n_fft,
                hop_length=self.hop_length,
                center=False,
            )
            self.accumulator.add_rms(np.asarray(rms, dtype=np.float32))
        except Exception as e:
            self.logger.debug("RMS extraction failed: %s", e)

    def _extract_centroid(self, mono_audio: npt.NDArray[np.float32]) -> None:
        """Extract spectral centroid (runs in thread pool)."""
        try:
            centroid = librosa.feature.spectral_centroid(
                y=np.asarray(mono_audio),
                sr=self.sample_rate,
                hop_length=self.hop_length,
                n_fft=self.n_fft,
                center=False,
            )
            self.accumulator.add_spectral_centroid(np.asarray(centroid, dtype=np.float64))
        except Exception as e:
            self.logger.debug("Spectral centroid extraction failed: %s", e)

    def _extract_low_freq_rms(self, mono_audio: npt.NDArray[np.float32]) -> None:
        """Extract low-frequency RMS energy for kick drum detection (runs in thread pool).

        Applies a low-pass filter at 200Hz to isolate bass content, then computes
        frame-wise RMS. Kick drums have high energy below 200Hz, snares do not.
        """
        try:
            # Design low-pass Butterworth filter at 200Hz
            nyquist = self.sample_rate / 2
            cutoff = 200 / nyquist
            sos = signal.butter(4, cutoff, btype="low", output="sos")

            # Apply filter
            filtered = signal.sosfilt(sos, mono_audio).astype(np.float32)

            # Compute RMS of filtered signal
            low_freq_rms = librosa.feature.rms(
                y=filtered,
                frame_length=self.n_fft,
                hop_length=self.hop_length,
                center=False,
            )
            self.accumulator.add_low_freq_rms(np.asarray(low_freq_rms, dtype=np.float32))
        except Exception as e:
            self.logger.debug("Low-freq RMS extraction failed: %s", e)

    def _extract_mid_freq_rms(self, mono_audio: npt.NDArray[np.float32]) -> None:
        """Extract mid-frequency RMS energy for snare detection (runs in thread pool).

        Applies a band-pass filter at 200-2000Hz to isolate mid-frequency content,
        then computes frame-wise RMS. Snare drums have high energy in this band.
        """
        try:
            # Design band-pass Butterworth filter at 200-2000Hz
            nyquist = self.sample_rate / 2
            low_cutoff = 200 / nyquist
            high_cutoff = min(2000 / nyquist, 0.99)  # Ensure below Nyquist
            sos = signal.butter(4, [low_cutoff, high_cutoff], btype="band", output="sos")

            # Apply filter
            filtered = signal.sosfilt(sos, mono_audio).astype(np.float32)

            # Compute RMS of filtered signal
            mid_freq_rms = librosa.feature.rms(
                y=filtered,
                frame_length=self.n_fft,
                hop_length=self.hop_length,
                center=False,
            )
            self.accumulator.add_mid_freq_rms(np.asarray(mid_freq_rms, dtype=np.float32))
        except Exception as e:
            self.logger.debug("Mid-freq RMS extraction failed: %s", e)

    def should_abort(self) -> bool:
        """Check if extraction should be aborted due to user action.

        Detects if the user has seeked or skipped by checking if the queue
        session or current item has changed.

        :return: True if extraction should be aborted.
        """
        if self._aborted:
            return True

        # Get current queue state
        try:
            player_queue = self.streams.mass.player_queues.get(self._queue_id)
            if player_queue is None:
                return True

            # Check if session changed (user seeked or queue was reset)
            if player_queue.session_id != self._session_id:
                return True

            # Check if current item changed (user skipped)
            if (
                player_queue.current_item
                and player_queue.current_item.queue_item_id != self._queue_item_id
            ):
                return True

        except Exception:
            # If we can't check, don't abort
            return False

        return False

    @property
    def is_aborted(self) -> bool:
        """Check if extraction was aborted."""
        return self._aborted

    @property
    def is_started(self) -> bool:
        """Check if extraction has started."""
        return self._started

    @property
    def is_finalized(self) -> bool:
        """Check if extraction has been finalized."""
        return self._finalized

    @property
    def track_id(self) -> str:
        """Get the track ID being extracted."""
        return self._track_id

    @property
    def provider_id(self) -> str:
        """Get the provider ID."""
        return self._provider_id

    async def finalize(self) -> None:
        """Finalize extraction by processing any remaining audio."""
        if self._finalized:
            return

        self._finalized = True

        # Process any remaining audio in buffer
        if self._audio_buffer and self._pcm_format is not None:
            try:
                combined = b"".join(self._audio_buffer)
                self._audio_buffer.clear()
                self._audio_buffer_bytes = 0

                self.logger.debug(
                    "Finalizing: processing remaining %d bytes of audio",
                    len(combined),
                )

                # Prepare mono audio
                mono_audio = self._prepare_mono_audio(combined, self._pcm_format)
                if mono_audio is not None and len(mono_audio) >= self.n_fft:
                    # Extract features with yields between each
                    await asyncio.to_thread(self._extract_onset, mono_audio)
                    await asyncio.sleep(0)
                    await asyncio.to_thread(self._extract_chroma, mono_audio)
                    await asyncio.sleep(0)
                    await asyncio.to_thread(self._extract_rms, mono_audio)
                    await asyncio.sleep(0)
                    await asyncio.to_thread(self._extract_centroid, mono_audio)
                    await asyncio.sleep(0)
                    await asyncio.to_thread(self._extract_low_freq_rms, mono_audio)
                    await asyncio.sleep(0)
                    await asyncio.to_thread(self._extract_mid_freq_rms, mono_audio)
            except Exception as e:
                self.logger.warning("Final feature extraction failed: %s", e)
        else:
            self.logger.debug("Finalizing: no remaining audio to process")

    def mark_finalized(self) -> None:
        """Mark extraction as finalized (sync version, doesn't process remaining)."""
        self._finalized = True
        self._audio_buffer.clear()
        self._audio_buffer_bytes = 0

    def get_logger(self) -> logging.Logger:
        """Get the logger instance."""
        return self.logger
