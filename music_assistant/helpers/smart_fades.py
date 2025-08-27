"""Smart Fades - BPM matching crossfade analysis using pyCrossfade approach."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import aiofiles
import madmom
import numpy as np
import shortuuid
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.smart_fades import SmartFadesAnalysis

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.audio import communicate, crossfade_pcm_parts
from music_assistant.helpers.util import remove_file

if TYPE_CHECKING:
    from music_assistant_models.queue_item import QueueItem
    from music_assistant_models.streamdetails import StreamDetails

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.smart_fades")

# Maximum duration for smart crossfades in seconds
MAX_SMART_CROSSFADE_DURATION = 30


async def _analyze_track_beats(
    audio_data: bytes,
    sample_rate: int = 44100,
) -> SmartFadesAnalysis | None:
    """
    Analyze track for beat tracking (following pyCrossfade approach).

    Args:
        audio_data: Raw PCM audio data
        sample_rate: Audio sample rate

    Returns:
        Beat tracking analysis results or None if failed
    """
    LOGGER.debug("Starting beat tracking analysis (pyCrossfade style)")

    try:
        # Convert audio data to numpy array for madmom
        audio_array = await asyncio.to_thread(_prepare_audio_for_madmom, audio_data, sample_rate)

        # Perform beat tracking analysis using madmom
        return await asyncio.to_thread(_madmom_beat_analysis, audio_array, sample_rate)

    except Exception as e:
        LOGGER.error("Beat tracking analysis failed: %s", e, exc_info=True)
        return None


def _prepare_audio_for_madmom(pcm_data: bytes, sample_rate: int) -> np.ndarray:
    """Convert PCM bytes to numpy array for madmom (pyCrossfade style)."""
    # The audio format from streams.py is PCM_F32LE (32-bit float, little endian, 2 channels)
    # Each sample is 4 bytes (32-bit) * 2 channels = 8 bytes per frame

    # Convert from 32-bit float PCM to numpy array
    audio_array = np.frombuffer(pcm_data, dtype=np.float32)

    # Audio is stereo (2 channels), convert to mono by averaging channels
    if len(audio_array) % 2 == 0:  # Ensure even number of samples for stereo
        audio_array = audio_array.reshape(-1, 2)  # Reshape to [samples, channels]
        audio_array = np.mean(audio_array, axis=1)  # Average stereo to mono

    # Calculate actual duration from original bytes and format
    # PCM_F32LE: 4 bytes per sample * 2 channels = 8 bytes per frame
    actual_duration = len(pcm_data) / (sample_rate * 4 * 2)  # 4 bytes * 2 channels
    LOGGER.debug(
        "Prepared %.2fs of audio for madmom analysis (%d samples)",
        actual_duration,
        len(audio_array),
    )

    return audio_array


def _madmom_beat_analysis(audio_array: np.ndarray, sample_rate: int) -> SmartFadesAnalysis:
    """Perform beat analysis using madmom (following pyCrossfade approach)."""
    LOGGER.debug("Running madmom beat analysis on %d samples", len(audio_array))

    # Beat tracking (pyCrossfade approach)
    # This follows the madmom pattern used in pyCrossfade
    beat_processor = madmom.features.beats.RNNBeatProcessor()
    beat_activations = beat_processor.process(audio_array, sample_rate=sample_rate)

    beat_tracker = madmom.features.beats.BeatTrackingProcessor(fps=100)
    beats = beat_tracker.process(beat_activations)

    # Downbeat tracking (pyCrossfade approach)
    try:
        downbeat_processor = madmom.features.downbeats.RNNDownBeatProcessor()
        downbeat_activations = downbeat_processor.process(audio_array, sample_rate=sample_rate)

        # DBNDownBeatTrackingProcessor processes activations only
        downbeat_tracker = madmom.features.downbeats.DBNDownBeatTrackingProcessor(
            beats_per_bar=4, fps=100
        )
        downbeat_output = downbeat_tracker.process(downbeat_activations)

        # Extract only the downbeats (beat_number == 1)
        if len(downbeat_output) > 0 and downbeat_output.ndim == 2:
            downbeats = downbeat_output[downbeat_output[:, 1] == 1][:, 0]
        else:
            # Fallback if output format is unexpected
            downbeats = beats[::4] if len(beats) >= 4 else beats

    except Exception as e:
        LOGGER.warning("Downbeat analysis failed: %s", e)
        # Fallback: estimate downbeats every 4 beats
        downbeats = beats[::4] if len(beats) >= 4 else beats

    # BPM estimation from beats
    if len(beats) > 1:
        beat_intervals = np.diff(beats)
        avg_interval = np.mean(beat_intervals)
        bpm = 60.0 / avg_interval if avg_interval > 0 else 120.0
        LOGGER.debug(
            "BPM calculation: %d beats, avg_interval=%.4fs, raw_bpm=%.1f",
            len(beats),
            avg_interval,
            bpm,
        )
    else:
        bpm = 120.0  # Default BPM
        LOGGER.debug("BPM calculation: Not enough beats, using default 120.0")

    # Confidence based on beat consistency
    if len(beats) > 4:
        beat_intervals = np.diff(beats)
        interval_std = np.std(beat_intervals)
        avg_interval = np.mean(beat_intervals)
        confidence = 1.0 - min(interval_std / avg_interval, 1.0) if avg_interval > 0 else 0.0
    else:
        confidence = 0.0

    analysis = SmartFadesAnalysis(
        bpm=bpm,
        beats=beats,
        downbeats=downbeats,
        confidence=confidence,
    )

    LOGGER.info(
        "Beat analysis complete (pyCrossfade style): BPM=%.1f, %d beats, %d downbeats, confidence=%.2f",
        bpm,
        len(beats),
        len(downbeats),
        confidence,
    )

    return analysis


async def analyze_track_for_smart_fades(
    queue_item: QueueItem,
    audio_stream: AsyncGenerator[bytes, None],
    streamdetails: StreamDetails,
    audio_format: AudioFormat,
) -> SmartFadesAnalysis | None:
    """
    Analyze a track's beats for BPM matching crossfade (pyCrossfade approach).

    This is the main entry point for beat analysis.
    """
    start_time = time.perf_counter()
    LOGGER.info("Starting beat analysis for track (pyCrossfade style): %s", queue_item.name)

    try:
        # Collect audio data from the separate analysis stream
        audio_data = b""
        sample_rate = audio_format.sample_rate
        # Remove limit - analyze entire track for complete BPM matching (pyCrossfade style)
        max_bytes = float("inf")

        async for chunk in audio_stream:
            audio_data += chunk
            if len(audio_data) >= max_bytes:
                break

        if len(audio_data) == 0:
            LOGGER.warning("No audio data received for analysis: %s", queue_item.name)
            return None

        # Use duration directly from streamdetails instead of calculating from bytes
        LOGGER.debug(
            "Collected %.2fs of audio data for analysis (%d bytes, format: %s, %dHz, %d-bit, %d channels)",
            streamdetails.duration,
            len(audio_data),
            audio_format.content_type,
            audio_format.sample_rate,
            audio_format.bit_depth,
            audio_format.channels,
        )

        # Perform beat analysis
        analysis = await _analyze_track_beats(audio_data, sample_rate)

        if analysis and analysis.confidence > 0.3:  # Minimum confidence threshold
            total_time = time.perf_counter() - start_time
            LOGGER.info(
                "Beat analysis successful for %s: BPM=%.1f, %d beats, confidence=%.2f (took %.2fs)",
                queue_item.name,
                analysis.bpm,
                len(analysis.beats),
                analysis.confidence,
                total_time,
            )
            return analysis
        else:
            total_time = time.perf_counter() - start_time
            LOGGER.warning(
                "Beat analysis failed or low confidence for %s (took %.2fs)",
                queue_item.name,
                total_time,
            )
            return None

    except Exception as e:
        total_time = time.perf_counter() - start_time
        LOGGER.error(
            "Beat analysis error for %s: %s (took %.2fs)",
            queue_item.name,
            e,
            total_time,
            exc_info=True,
        )
        return None


def _validate_crossfade_compatibility(
    fade_out_analysis: SmartFadesAnalysis, fade_in_analysis: SmartFadesAnalysis
) -> tuple[bool, str]:
    """Check if tracks are suitable for smart crossfading."""
    # Check confidence levels
    if fade_out_analysis.confidence < 0.4 or fade_in_analysis.confidence < 0.4:
        return False, "Low confidence in beat analysis"

    # Check BPM compatibility (avoid extreme stretching)
    bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm
    if bpm_ratio < 0.7 or bpm_ratio > 1.4:
        return False, f"BPM difference too extreme ({bpm_ratio:.2f}x)"

    # Check if we have enough beat data
    if len(fade_out_analysis.beats) < 8 or len(fade_in_analysis.beats) < 8:
        return False, "Insufficient beat data for smart crossfade"

    # Check beat consistency (avoid tracks with tempo changes)
    if len(fade_out_analysis.beats) > 1:
        fade_out_intervals = np.diff(fade_out_analysis.beats)
        fade_out_consistency = (
            np.std(fade_out_intervals) / np.mean(fade_out_intervals)
            if np.mean(fade_out_intervals) > 0
            else 1.0
        )
    else:
        fade_out_consistency = 1.0

    if len(fade_in_analysis.beats) > 1:
        fade_in_intervals = np.diff(fade_in_analysis.beats)
        fade_in_consistency = (
            np.std(fade_in_intervals) / np.mean(fade_in_intervals)
            if np.mean(fade_in_intervals) > 0
            else 1.0
        )
    else:
        fade_in_consistency = 1.0

    # Log consistency values for debugging
    LOGGER.debug(
        "Tempo consistency: fade_out=%.3f, fade_in=%.3f", fade_out_consistency, fade_in_consistency
    )

    # Relax tempo consistency threshold - real music has natural tempo variations
    if fade_out_consistency > 0.30 or fade_in_consistency > 0.30:
        return (
            False,
            f"Tracks have inconsistent tempo (CV: {fade_out_consistency:.3f}, {fade_in_consistency:.3f})",
        )

    return True, "Compatible for smart crossfade"


def _calculate_optimal_crossfade_timing(
    fade_out_analysis: SmartFadesAnalysis,
    fade_in_analysis: SmartFadesAnalysis,
    crossfade_bars: int = 4,
    max_fallback_duration: float = 15.0,
) -> float:
    """Calculate precise crossfade timing based on actual beat positions."""
    # PRIMARY: Try to use downbeats for more musical timing (NO CAP)
    if (
        len(fade_out_analysis.downbeats) >= crossfade_bars
        and len(fade_in_analysis.downbeats) >= crossfade_bars
    ):
        # Use actual downbeat spacing for precise timing
        fade_out_downbeats = fade_out_analysis.downbeats[-crossfade_bars:]
        fade_in_downbeats = fade_in_analysis.downbeats[:crossfade_bars]

        fade_out_duration = (
            fade_out_downbeats[-1] - fade_out_downbeats[0] if len(fade_out_downbeats) > 1 else 0
        )
        fade_in_duration = (
            fade_in_downbeats[-1] - fade_in_downbeats[0] if len(fade_in_downbeats) > 1 else 0
        )

        if fade_out_duration > 0 and fade_in_duration > 0:
            # Use the average for balanced crossfade
            optimal_duration = (fade_out_duration + fade_in_duration) / 2
            # Apply reasonable absolute limit (much higher than user cap)
            smart_duration = min(
                optimal_duration, MAX_SMART_CROSSFADE_DURATION
            )  # Reasonable absolute max
            LOGGER.debug("Smart timing from downbeats: %.2fs", smart_duration)
            return smart_duration

    # SECONDARY: Use beats if downbeats insufficient (NO CAP)
    beats_per_bar = 4
    required_beats = crossfade_bars * beats_per_bar

    if (
        len(fade_out_analysis.beats) >= required_beats
        and len(fade_in_analysis.beats) >= required_beats
    ):
        fade_out_beats_duration = (
            fade_out_analysis.beats[-1] - fade_out_analysis.beats[-required_beats]
        )
        fade_in_beats_duration = (
            fade_in_analysis.beats[required_beats - 1] - fade_in_analysis.beats[0]
        )
        optimal_duration = (fade_out_beats_duration + fade_in_beats_duration) / 2
        # Apply reasonable absolute limit
        smart_duration = min(optimal_duration, MAX_SMART_CROSSFADE_DURATION)
        LOGGER.debug("Smart timing from beats: %.2fs", smart_duration)
        return smart_duration

    # FALLBACK: Calculate from BPM (APPLY USER CAP HERE)
    seconds_per_beat = 60.0 / fade_out_analysis.bpm
    fallback_duration = crossfade_bars * beats_per_bar * seconds_per_beat
    capped_duration = min(fallback_duration, max_fallback_duration)

    if capped_duration < fallback_duration:
        LOGGER.debug(
            "BPM fallback duration capped: calculated=%.2fs, capped=%.2fs",
            fallback_duration,
            capped_duration,
        )

    LOGGER.debug("Using BPM fallback timing: %.2fs", capped_duration)
    return capped_duration




def _create_adaptive_frequency_filters(
    fade_out_analysis: SmartFadesAnalysis,
    fade_in_analysis: SmartFadesAnalysis,
    fade_out_input: str,
    fade_in_input: str,
) -> list[str]:
    """Create frequency filters adapted to BPM and track characteristics."""
    # Adjust filter frequencies based on BPM and energy
    # Higher BPM typically = higher energy = need different filter characteristics
    fade_out_energy_factor = min(
        2.0, max(0.8, fade_out_analysis.bpm / 120.0)
    )  # Normalize around 120 BPM
    fade_in_energy_factor = min(2.0, max(0.8, fade_in_analysis.bpm / 120.0))

    # Base frequencies adjusted by energy
    high_pass_freq = int(80 * fade_out_energy_factor)  # 64-160 Hz range
    low_pass_freq = int(8000 - (500 * fade_in_energy_factor))  # 7000-7600 Hz range

    # Additional filtering based on BPM difference
    bpm_ratio = fade_in_analysis.bpm / fade_out_analysis.bpm
    if bpm_ratio > 1.1:  # Incoming track is faster
        # Slightly more aggressive low-pass to smooth the transition
        low_pass_freq = int(low_pass_freq * 0.9)
    elif bpm_ratio < 0.9:  # Incoming track is slower
        # Slightly more aggressive high-pass on outgoing to reduce muddy transition
        high_pass_freq = int(high_pass_freq * 1.1)

    return [
        f"{fade_out_input}highpass=f={high_pass_freq}:poles=2[fadeout_hp]",
        f"{fade_in_input}lowpass=f={low_pass_freq}:poles=2[fadein_lp]",
    ]


async def smart_crossfade_pcm_parts(
    fade_in_part: bytes,
    fade_out_part: bytes,
    fade_in_analysis: SmartFadesAnalysis,
    fade_out_analysis: SmartFadesAnalysis,
    pcm_format: AudioFormat,
    fade_out_pcm_format: AudioFormat | None = None,
    crossfade_bars: int = 8,
    max_fallback_duration: float = 15.0,
) -> bytes:
    """
    Apply basic smart crossfade with dynamic duration and frequency filtering.

    Args:
        fade_in_part: Audio data for incoming track
        fade_out_part: Audio data for outgoing track
        fade_in_analysis: Beat analysis for incoming track
        fade_out_analysis: Beat analysis for outgoing track
        pcm_format: Audio format for incoming track
        fade_out_pcm_format: Audio format for outgoing track (if different)
        crossfade_bars: Number of bars to crossfade over
        max_fallback_duration: Maximum duration for BPM-based fallback timing

    Returns:
        Crossfaded audio data with frequency filtering applied
    """
    if fade_out_pcm_format is None:
        fade_out_pcm_format = pcm_format

    LOGGER.info(
        "Applying smart crossfade: fade_out_bpm=%.1f, fade_in_bpm=%.1f, %d bars",
        fade_out_analysis.bpm,
        fade_in_analysis.bpm,
        crossfade_bars,
    )

    # Validate compatibility first
    is_compatible, reason = _validate_crossfade_compatibility(fade_out_analysis, fade_in_analysis)
    if not is_compatible:
        LOGGER.info("Smart crossfade not suitable: %s. Using standard crossfade.", reason)
        return await crossfade_pcm_parts(
            fade_in_part, fade_out_part, pcm_format, fade_out_pcm_format
        )

    try:
        # BPM information for logging (no tempo adjustment in basic implementation)
        LOGGER.debug("Track BPMs: fade_out=%.1f, fade_in=%.1f", fade_out_analysis.bpm, fade_in_analysis.bpm)

        # Calculate optimal crossfade duration using beat analysis
        # For smart analysis: allow longer durations, only cap BPM fallback
        crossfade_duration = _calculate_optimal_crossfade_timing(
            fade_out_analysis, fade_in_analysis, crossfade_bars, max_fallback_duration
        )

        LOGGER.debug(
            "Smart crossfade duration: %.2fs (%d bars, based on beat analysis)",
            crossfade_duration,
            crossfade_bars,
        )

        # Write the fade_out_part to a temporary file (following crossfade_pcm_parts pattern)
        fadeout_filename = f"/tmp/{shortuuid.random(20)}.pcm"  # noqa: S108
        async with aiofiles.open(fadeout_filename, "wb") as outfile:
            await outfile.write(fade_out_part)

        # Build FFmpeg command for enhanced smart crossfade
        args = [
            # Generic args
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "quiet",
            # fadeout part (as file)
            "-acodec",
            fade_out_pcm_format.content_type.name.lower(),
            "-ac",
            str(fade_out_pcm_format.channels),
            "-ar",
            str(fade_out_pcm_format.sample_rate),
            "-channel_layout",
            "mono" if fade_out_pcm_format.channels == 1 else "stereo",
            "-f",
            fade_out_pcm_format.content_type.value,
            "-i",
            fadeout_filename,
            # fade_in part (stdin)
            "-acodec",
            pcm_format.content_type.name.lower(),
            "-ac",
            str(pcm_format.channels),
            "-channel_layout",
            "mono" if pcm_format.channels == 1 else "stereo",
            "-ar",
            str(pcm_format.sample_rate),
            "-f",
            pcm_format.content_type.value,
            "-i",
            "-",
        ]

        # Build basic filter chain with frequency filtering only
        filter_complex = []

        # Create adaptive frequency filters based on track characteristics  
        frequency_filters = _create_adaptive_frequency_filters(
            fade_out_analysis, fade_in_analysis, "[0]", "[1]"
        )
        filter_complex.extend(frequency_filters)

        # Basic crossfade with standard linear curve
        filter_complex.append(
            f"[fadeout_hp][fadein_lp]acrossfade=d={crossfade_duration}"
        )

        args.extend(
            [
                "-filter_complex",
                ";".join(filter_complex),
                # output args
                "-acodec",
                pcm_format.content_type.name.lower(),
                "-ac",
                str(pcm_format.channels),
                "-channel_layout",
                "mono" if pcm_format.channels == 1 else "stereo",
                "-ar",
                str(pcm_format.sample_rate),
                "-f",
                pcm_format.content_type.value,
                "-",
            ]
        )

        LOGGER.debug("Enhanced smart crossfade FFmpeg command: %s", " ".join(args))

        # Execute the enhanced smart crossfade
        _, crossfaded_audio, _ = await communicate(args, fade_in_part)
        await remove_file(fadeout_filename)

        if crossfaded_audio:
            LOGGER.info(
                "Basic smart crossfade successful: duration=%.2fs with frequency filtering",
                crossfade_duration,
            )
            return crossfaded_audio
        else:
            LOGGER.warning("Basic smart crossfade failed, falling back to standard crossfade")
            # Fallback to standard crossfade
            return await crossfade_pcm_parts(
                fade_in_part, fade_out_part, pcm_format, fade_out_pcm_format
            )

    except Exception as e:
        LOGGER.error("Basic smart crossfade error: %s", e, exc_info=True)
        # Fallback to standard crossfade
        return await crossfade_pcm_parts(
            fade_in_part, fade_out_part, pcm_format, fade_out_pcm_format
        )
