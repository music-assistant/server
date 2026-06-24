"""Helper functions for the Smart Fades audio analysis provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import torch
from music_assistant_models.enums import ContentType

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
