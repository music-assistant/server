"""
Audio stems for the Party plugin: turning one part of the music down or off.

The decoded audio of a track is split into overlapping segments, a source separation
model (Demucs) estimates one stem (the vocals, the drums, ...) of each segment, and
the estimate is subtracted from the original audio. Everything that is not recognized
as that stem is thus kept exactly as it was. Karaoke mode removes the vocals this way.

The audio is processed as a stream on its way into the audio buffer, so a track starts
playing as soon as its first segment is done and the next track is processed while the
current one plays.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

import numpy as np
from music_assistant_models.enums import ContentType

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator

    import numpy.typing as npt
    from music_assistant_models.media_items import AudioFormat

    type FloatAudio = npt.NDArray[np.float32]

DEMUCS_MODEL: Final[str] = "htdemucs"
STEM_VOCALS: Final[str] = "vocals"
# Length of a segment until the model is loaded; the value Demucs was trained with.
DEFAULT_SEGMENT_SECONDS: Final[float] = 7.8
# Share of a segment that overlaps the next one. The estimates of both are blended
# there, which hides the less accurate edges of a segment.
SEGMENT_OVERLAP: Final[float] = 0.25
# Below this level a segment is silence and has no stem to look for.
SILENCE_THRESHOLD: Final[float] = 1e-5

_INT_DTYPES: Final[dict[int, str]] = {16: "<i2", 32: "<i4"}
_FLOAT_DTYPES: Final[dict[ContentType, str]] = {
    ContentType.PCM_F32LE: "<f4",
    ContentType.PCM_F64LE: "<f8",
}


class StemSeparator(Protocol):
    """Estimates a stem in a segment of audio."""

    @property
    def segment_seconds(self) -> float:
        """Return the longest segment the separator accepts, in seconds."""

    async def extract_stem(self, audio: FloatAudio, sample_rate: int, stem: str) -> FloatAudio:
        """
        Return one stem of the given audio.

        :param audio: Samples in the range -1..1, shaped (channels, frames).
        :param sample_rate: Sample rate of the audio.
        :param stem: Name of the stem, such as "vocals".
        :return: The stem, in the shape and sample rate of the given audio.
        """


def supports_pcm_format(pcm_format: AudioFormat) -> bool:
    """
    Return whether stems can be separated from audio in the given PCM format.

    :param pcm_format: The format of the decoded audio.
    """
    if pcm_format.channels not in (1, 2):
        return False
    if pcm_format.content_type in _FLOAT_DTYPES:
        return True
    return pcm_format.content_type.is_pcm() and pcm_format.bit_depth in (16, 24, 32)


def pcm_to_float(data: bytes | bytearray | memoryview, pcm_format: AudioFormat) -> FloatAudio:
    """
    Decode interleaved PCM to float samples shaped (channels, frames).

    :param data: Whole frames of PCM audio.
    :param pcm_format: The format of the PCM audio.
    """
    samples: FloatAudio
    if dtype := _FLOAT_DTYPES.get(pcm_format.content_type):
        samples = np.frombuffer(data, dtype=dtype).astype(np.float32)
    elif pcm_format.bit_depth == 24:
        packed = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        padded = np.zeros((len(packed), 4), dtype=np.uint8)
        padded[:, 1:] = packed
        # the arithmetic shift drops the padding byte and keeps the sign
        samples = (padded.view("<i4").reshape(-1) >> 8).astype(np.float32)
        samples /= 2**23
    else:
        bit_depth = pcm_format.bit_depth
        samples = np.frombuffer(data, dtype=_INT_DTYPES[bit_depth]).astype(np.float32)
        samples /= 2 ** (bit_depth - 1)
    return np.ascontiguousarray(samples.reshape(-1, pcm_format.channels).T)


def float_to_pcm(audio: FloatAudio, pcm_format: AudioFormat) -> bytes:
    """
    Encode float samples shaped (channels, frames) to interleaved PCM.

    :param audio: Samples in the range -1..1.
    :param pcm_format: The format of the PCM audio to produce.
    """
    interleaved = audio.T.reshape(-1)
    if dtype := _FLOAT_DTYPES.get(pcm_format.content_type):
        return interleaved.astype(dtype).tobytes()
    bit_depth = pcm_format.bit_depth
    full_scale = 2 ** (bit_depth - 1)
    # float64 keeps the rounding exact for every integer depth
    scaled = np.rint(interleaved.astype(np.float64) * full_scale)
    clipped = np.clip(scaled, -full_scale, full_scale - 1)
    encoded: bytes
    if bit_depth == 24:
        as_bytes = clipped.astype("<i4").view(np.uint8).reshape(-1, 4)
        encoded = as_bytes[:, :3].tobytes()
    else:
        encoded = clipped.astype(_INT_DTYPES[bit_depth]).tobytes()
    return encoded


async def attenuate_stem(  # noqa: PLR0915
    source: AsyncGenerator[bytes],
    pcm_format: AudioFormat,
    separator: StemSeparator,
    logger: logging.Logger,
    stem: str = STEM_VOCALS,
    volume: float = 0.0,
) -> AsyncGenerator[bytes]:
    """
    Yield the audio of the source with one stem turned down, in 1-second chunks.

    The output has the format and the length of the source. When the separator fails,
    the rest of the audio is passed on untouched so playback carries on.

    :param source: Decoded PCM audio of a track.
    :param pcm_format: The PCM format of the source.
    :param separator: Estimates the stem of each segment.
    :param logger: Logger for progress and failures.
    :param stem: Name of the stem to turn down, such as "vocals".
    :param volume: Share of the stem to keep, from 0 (none) to 1 (all).
    """
    removed_share = np.float32(1 - min(max(volume, 0.0), 1.0))
    sample_rate = pcm_format.sample_rate
    frame_size = pcm_format.channels * pcm_format.bit_depth // 8
    chunk_size = pcm_format.pcm_sample_size
    segment_frames = max(2, int(separator.segment_seconds * sample_rate))
    hop_frames = max(1, int(segment_frames * (1 - SEGMENT_OVERLAP)))
    overlap_frames = segment_frames - hop_frames
    weights = _segment_weights(segment_frames)

    pending = bytearray()
    output = bytearray()
    # weighted stem audio, and the weights it carries, that the previous segment
    # contributed to the start of the pending audio
    carried_stem = np.zeros((pcm_format.channels, overlap_frames), dtype=np.float32)
    carried_weights = np.zeros(overlap_frames, dtype=np.float32)
    failed = False
    audio_seconds = 0.0
    busy_seconds = 0.0

    async def _blend(frames: int) -> tuple[FloatAudio, FloatAudio, FloatAudio]:
        """Return the mix of the first pending frames with its blended stem and weights."""
        nonlocal audio_seconds, busy_seconds
        mix = pcm_to_float(memoryview(pending)[: frames * frame_size], pcm_format)
        started = time.monotonic()
        estimate = await separator.extract_stem(mix, sample_rate, stem)
        busy_seconds += time.monotonic() - started
        blended = estimate * weights[:frames]
        blended_weights = weights[:frames].copy()
        carried = min(frames, overlap_frames)
        blended[:, :carried] += carried_stem[:, :carried]
        blended_weights[:carried] += carried_weights[:carried]
        return mix, blended, blended_weights

    async with aclosing(source):
        async for chunk in source:
            if failed:
                output += chunk
            else:
                pending += chunk
            while not failed and len(pending) >= segment_frames * frame_size:
                try:
                    mix, blended, blended_weights = await _blend(segment_frames)
                except Exception:
                    logger.exception("Stem separation failed, playing the rest of the track as is")
                    failed = True
                    output += pending
                    pending.clear()
                    break
                remainder = mix[:, :hop_frames] - (
                    blended[:, :hop_frames] / blended_weights[:hop_frames] * removed_share
                )
                output += float_to_pcm(remainder, pcm_format)
                carried_stem = blended[:, hop_frames:]
                carried_weights = blended_weights[hop_frames:]
                del pending[: hop_frames * frame_size]
                audio_seconds += hop_frames / sample_rate
            while len(output) >= chunk_size:
                yield bytes(output[:chunk_size])
                del output[:chunk_size]

    remaining_frames = len(pending) // frame_size
    if remaining_frames and not failed:
        try:
            if remaining_frames == overlap_frames and carried_weights.all():
                # the previous segment covered all of what is left
                mix = pcm_to_float(memoryview(pending)[: remaining_frames * frame_size], pcm_format)
                estimate = carried_stem / carried_weights
            else:
                mix, blended, blended_weights = await _blend(remaining_frames)
                estimate = blended / blended_weights
                audio_seconds += remaining_frames / sample_rate
            output += float_to_pcm(mix - estimate * removed_share, pcm_format)
            output += pending[remaining_frames * frame_size :]
        except Exception:
            logger.exception("Stem separation failed, playing the end of the track as is")
            output += pending
    elif not failed:
        output += pending

    for offset in range(0, len(output), chunk_size):
        yield bytes(output[offset : offset + chunk_size])

    if busy_seconds and audio_seconds:
        speed = audio_seconds / busy_seconds
        if speed < 1:
            logger.warning(
                "Stem separation ran at %.1fx playback speed, which is too slow for "
                "uninterrupted playback on this system",
                speed,
            )
        else:
            logger.debug("Stem separation ran at %.1fx playback speed", speed)


@dataclass(frozen=True)
class StemAttenuation:
    """
    Turning one stem down, as a transform of the audio of a stream.

    Two of them are equal when they give the same audio, whichever model instance runs
    them, so buffered audio stays valid across a reload of the plugin.
    """

    separator: StemSeparator = field(compare=False)
    logger: logging.Logger = field(compare=False)
    stem: str = STEM_VOCALS
    volume: float = 0.0

    def __call__(
        self, source: AsyncGenerator[bytes], pcm_format: AudioFormat
    ) -> AsyncGenerator[bytes]:
        """
        Return the audio of the source with the stem turned down.

        :param source: Decoded PCM audio of a track.
        :param pcm_format: The PCM format of the source.
        """
        return attenuate_stem(
            source, pcm_format, self.separator, self.logger, self.stem, self.volume
        )


class DemucsStemSeparator:
    """Estimates stems with the Demucs source separation model, on the CPU."""

    def __init__(self, logger: logging.Logger) -> None:
        """
        Initialize the separator; the model is loaded by `load`.

        :param logger: Logger for progress and failures.
        """
        self.logger = logger
        self._model: Any = None
        self._segment_seconds = DEFAULT_SEGMENT_SECONDS
        # one worker: segments of the playing and the next track take turns
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="audio-stems",
        )

    @property
    def ready(self) -> bool:
        """Return whether the model is loaded."""
        return self._model is not None

    @property
    def segment_seconds(self) -> float:
        """Return the longest segment the model accepts, in seconds."""
        return self._segment_seconds

    async def load(self) -> None:
        """Load the model, downloading it on first use."""
        await asyncio.get_running_loop().run_in_executor(self._executor, self._load)

    async def extract_stem(self, audio: FloatAudio, sample_rate: int, stem: str) -> FloatAudio:
        """
        Return one stem of the given audio.

        :param audio: Samples in the range -1..1, shaped (channels, frames).
        :param sample_rate: Sample rate of the audio.
        :param stem: Name of the stem, one of the sources of the model.
        :return: The stem, in the shape and sample rate of the given audio.
        """
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._extract_stem, audio, sample_rate, stem
        )

    def close(self) -> None:
        """Release the model and stop the worker once its current segment is done."""
        self._model = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _load(self) -> None:
        """Load the model in the worker thread."""
        from demucs.pretrained import get_model  # noqa: PLC0415

        model = get_model(DEMUCS_MODEL)
        model.eval()
        self._segment_seconds = min(DEFAULT_SEGMENT_SECONDS, float(model.max_allowed_segment))
        self._model = model
        self.logger.info("Stem separation model %s loaded", DEMUCS_MODEL)

    def _extract_stem(self, audio: FloatAudio, sample_rate: int, stem: str) -> FloatAudio:
        """
        Run the model on a segment in the worker thread.

        :param audio: Samples in the range -1..1, shaped (channels, frames).
        :param sample_rate: Sample rate of the audio.
        :param stem: Name of the stem, one of the sources of the model.
        """
        import torch  # noqa: PLC0415
        from demucs.apply import apply_model  # noqa: PLC0415
        from demucs.audio import convert_audio  # noqa: PLC0415
        from julius.resample import resample_frac  # noqa: PLC0415

        model = self._model
        if model is None:
            msg = "The stem separation model is not loaded"
            raise RuntimeError(msg)
        channels, frames = audio.shape
        result = np.zeros_like(audio)
        wav = convert_audio(
            torch.from_numpy(audio), sample_rate, model.samplerate, model.audio_channels
        )
        wav = wav[..., : int(self._segment_seconds * model.samplerate)]
        reference = wav.mean(0)
        mean = reference.mean()
        std = reference.std()
        if not wav.shape[-1] or float(std) < SILENCE_THRESHOLD:
            return result
        with torch.no_grad():
            sources = apply_model(
                model, ((wav - mean) / std)[None], shifts=0, split=False, device="cpu"
            )
            estimate = sources[0, model.sources.index(stem)] * std
            if channels == 1:
                estimate = estimate.mean(0, keepdim=True)
            estimate = resample_frac(estimate, model.samplerate, sample_rate)
        length = min(frames, estimate.shape[-1])
        result[:, :length] = estimate[:, :length].numpy()
        return result


def _segment_weights(frames: int) -> FloatAudio:
    """
    Return the triangular blending weights of a segment, largest in its middle.

    :param frames: Length of the segment in frames.
    """
    rising = np.arange(1, frames // 2 + 1, dtype=np.float32)
    falling = np.arange(frames - frames // 2, 0, -1, dtype=np.float32)
    weights: FloatAudio = np.concatenate([rising, falling])
    weights /= weights.max()
    return weights
