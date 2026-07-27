"""Helper functions for the Smart Fades audio analysis provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt
import torch
from music_assistant_models.enums import ContentType

from music_assistant.controllers.streams.smart_fades.models import BAND_RMS_BANDS

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat


def calculate_overall_bpm(beats: np.ndarray, n_segments: int = 5) -> float:
    """
    Calculate overall BPM.

    Splits the beat array into N segments, computes a BPM per segment, then
    discards outlier segments (those deviating more than 3 BPM from the median)
    before averaging the consistent remainder. This prevents a single poorly-tracked
    section from pulling the final BPM away from the true value.

    :param beats: Array of beat timestamps in seconds.
    :param n_segments: Number of equal segments to split the beats into.
    """
    if len(beats) < n_segments * 2:
        return float(60.0 / np.mean(np.diff(beats)))

    segment_bpms = []
    for idx in np.array_split(np.arange(len(beats)), n_segments):
        if len(idx) < 2:
            continue
        segment_bpms.append(60.0 / float(np.mean(np.diff(beats[idx]))))

    if len(segment_bpms) < 2:
        return float(60.0 / np.mean(np.diff(beats)))

    seg_arr = np.array(segment_bpms)
    median_bpm = float(np.median(seg_arr))
    consistent = seg_arr[np.abs(seg_arr - median_bpm) <= 3.0]

    if len(consistent) < 2:
        # All segments too spread out — fall back to unfiltered mean
        return float(np.mean(seg_arr))

    return float(np.mean(consistent))


def aggregate_series_to_bins(
    values: npt.NDArray[np.float32],
    n_bins: int,
    *,
    power: bool = False,
) -> npt.NDArray[np.float32]:
    """
    Resample a frame series to a fixed bin count by averaging over each bin's span.

    Every bin is the exact fractional-overlap mean of the frames it covers
    (mean of squares then square root when ``power`` is True, appropriate for
    RMS/energy series).  Unlike point sampling this acts as a boxcar low-pass,
    so periodic frame-rate detail (e.g. beat-level energy ripple) cannot alias
    into the bin grid.

    :param values: Input frame series (uniform frame spacing).
    :param n_bins: Number of output bins.
    :param power: Average squared values and return the root (RMS-correct).
    """
    x = values.astype(np.float64)
    if power:
        x = x * x
    # interpolating the cumulative sum at fractional bin edges = exact boxcar average in O(n)
    cum = np.concatenate(([0.0], np.cumsum(x)))
    edges = np.linspace(0.0, len(values), n_bins + 1)
    cum_at_edges = np.interp(edges, np.arange(len(values) + 1, dtype=np.float64), cum)
    binned = np.diff(cum_at_edges) / np.diff(edges)
    if power:
        binned = np.sqrt(binned)
    return binned.astype(np.float32)


def compute_band_rms_frames(
    pcm: npt.NDArray[np.float32],
    sample_rate: int,
    window_samples: int,
) -> dict[str, npt.NDArray[np.float32]]:
    """
    Compute per-band RMS frames over fixed windows of a mono PCM block.

    Returns one RMS value per window (including a trailing partial window) per
    band in ``BAND_RMS_BANDS``, at the same frame cadence as the full-band RMS
    frames, so both series can be aggregated to the same bin grid.

    :param pcm: Mono float32 PCM samples.
    :param sample_rate: Sample rate of ``pcm`` in Hz.
    :param window_samples: Frame length in samples (e.g. 100 ms worth).
    """
    out: dict[str, list[float]] = {name: [] for name in BAND_RMS_BANDS}
    n_full = len(pcm) // window_samples
    windows: list[npt.NDArray[np.float32]] = []
    if n_full > 0:
        windows.extend(pcm[: n_full * window_samples].reshape(n_full, window_samples))
    if len(pcm) - n_full * window_samples > 0:
        windows.append(pcm[n_full * window_samples :])
    for window in windows:
        spectrum = np.fft.rfft(window.astype(np.float64))
        freqs = np.fft.rfftfreq(len(window), 1.0 / sample_rate)
        # Parseval, single-sided: double every bin except DC (and Nyquist for even N),
        # which are not mirrored in the negative-frequency half.
        power = np.abs(spectrum) ** 2 / len(window) ** 2
        power[1:] *= 2.0
        if len(window) % 2 == 0:
            power[-1] /= 2.0
        for name, (lo, hi) in BAND_RMS_BANDS.items():
            mask = (freqs >= lo) & (freqs < hi if hi is not None else np.ones_like(freqs, bool))
            out[name].append(float(np.sqrt(power[mask].sum())))
    return {name: np.array(vals, dtype=np.float32) for name, vals in out.items()}


def decode_pcm_chunk_to_mono(audio_format: AudioFormat, pcm_chunk: bytes) -> np.ndarray:
    """
    Decode a raw PCM chunk to a mono float32 numpy array.

    :param audio_format: The audio format describing the PCM data.
    :param pcm_chunk: Raw PCM audio data.
    """
    content_type = audio_format.content_type
    writable = bytearray(pcm_chunk)

    if content_type == ContentType.PCM_F32LE:
        audio = torch.frombuffer(writable, dtype=torch.float32).clone()
    elif content_type == ContentType.PCM_F64LE:
        audio = torch.frombuffer(writable, dtype=torch.float64).clone().to(torch.float32)
    elif content_type == ContentType.PCM_S32LE:
        audio = (
            torch.frombuffer(writable, dtype=torch.int32).clone().to(torch.float32) / 2147483648.0
        )
    elif content_type == ContentType.PCM_S24LE:
        raw = torch.frombuffer(writable, dtype=torch.uint8).clone()
        raw = raw[: (raw.numel() // 3) * 3].reshape(-1, 3).to(torch.int32)
        audio = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        audio = torch.where(audio & 0x800000 != 0, audio - 0x1000000, audio)
        audio = audio.to(torch.float32) / 8388608.0
    else:
        audio = torch.frombuffer(writable, dtype=torch.int16).clone().to(torch.float32) / 32768.0

    channels = audio_format.channels
    if channels > 1:
        frame_samples = (audio.numel() // channels) * channels
        audio = audio[:frame_samples].reshape(-1, channels).mean(dim=1)

    return audio.numpy()
