"""
Vocal activity inference using FireRed AED.

The model architecture and weights are adapted from FireRedVAD:
https://github.com/FireRedTeam/FireRedVAD
Copyright 2026 Xiaohongshu. Licensed under Apache-2.0.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np
import numpy.typing as npt
import torch
from torch import nn
from torch.nn import functional

FIRERED_SAMPLE_RATE = 16000
FIRERED_FRAME_DURATION = 0.1
FIRERED_MEL_BINS = 80
FIRERED_OUTPUT_CLASSES = 3
FIRERED_PARAMETER_COUNT = 588_931
FIRERED_MAX_INFERENCE_FRAMES = 30_000
# Covers the eight-layer FSMN receptive field in both directions.
FIRERED_INFERENCE_CONTEXT_FRAMES = 160

_MODEL_PATH = Path(__file__).parent / "resources" / "firered_aed.pt"
_CMVN_PATH = Path(__file__).parent / "resources" / "firered_aed_cmvn.npz"


class FireRedFbank:
    """Streaming FireRed AED feature extractor for 16 kHz mono PCM."""

    def __init__(
        self,
        cmvn_means: npt.NDArray[np.float64],
        cmvn_inverse_std: npt.NDArray[np.float64],
    ) -> None:
        """
        Initialize the feature extractor.

        :param cmvn_means: Fixed FireRed feature means.
        :param cmvn_inverse_std: Fixed FireRed inverse standard deviations.
        """
        if cmvn_means.shape != (FIRERED_MEL_BINS,) or cmvn_inverse_std.shape != (FIRERED_MEL_BINS,):
            raise ValueError("invalid FireRed CMVN dimensions")
        options = knf.FbankOptions()
        options.frame_opts.samp_freq = FIRERED_SAMPLE_RATE
        options.frame_opts.frame_length_ms = 25
        options.frame_opts.frame_shift_ms = 10
        options.frame_opts.dither = 0
        options.frame_opts.snip_edges = True
        options.mel_opts.num_bins = FIRERED_MEL_BINS
        options.mel_opts.debug_mel = False
        self._fbank = knf.OnlineFbank(options)
        self._cmvn_means = cmvn_means
        self._cmvn_inverse_std = cmvn_inverse_std
        self._next_frame = 0
        self._finished = False

    def process(self, pcm_16k: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        """
        Consume normalized 16 kHz mono PCM and return newly available features.

        :param pcm_16k: Normalized mono float32 samples at 16 kHz.
        """
        if self._finished:
            raise RuntimeError("FireRed feature extraction is already finalized")
        if pcm_16k.size:
            quantized = quantize_pcm_for_firered(pcm_16k)
            self._fbank.accept_waveform(FIRERED_SAMPLE_RATE, quantized.tolist())
        return self._collect_ready_frames()

    def finalize(self) -> npt.NDArray[np.float32]:
        """Finish the stream and return any final features."""
        if not self._finished:
            self._fbank.input_finished()
            self._finished = True
        return self._collect_ready_frames()

    def _collect_ready_frames(self) -> npt.NDArray[np.float32]:
        ready = self._fbank.num_frames_ready
        if ready <= self._next_frame:
            return np.empty((0, FIRERED_MEL_BINS), dtype=np.float32)
        features = np.vstack(
            [self._fbank.get_frame(index) for index in range(self._next_frame, ready)]
        )
        self._fbank.pop(ready - self._next_frame)
        self._next_frame = ready
        normalized = (features.astype(np.float64) - self._cmvn_means) * self._cmvn_inverse_std
        return normalized.astype(np.float32)


def load_firered_components(
    device: str = "cpu",
) -> tuple[nn.Module, npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """
    Load the FireRed AED model and fixed CMVN statistics.

    :param device: Device used for model inference.
    """
    model = _FireRedAedModel().to(device)
    state_dict = torch.load(_MODEL_PATH, map_location=device, weights_only=True)
    model.load_state_dict(state_dict, strict=True)
    model.eval()
    parameter_count = sum(parameter.numel() for parameter in model.parameters())
    if parameter_count != FIRERED_PARAMETER_COUNT:
        raise RuntimeError(
            f"invalid FireRed AED parameter count: {parameter_count} "
            f"(expected {FIRERED_PARAMETER_COUNT})"
        )
    with np.load(_CMVN_PATH) as cmvn:
        means = cmvn["means"].astype(np.float64)
        inverse_std = cmvn["inverse_std"].astype(np.float64)
    if means.shape != (FIRERED_MEL_BINS,) or inverse_std.shape != (FIRERED_MEL_BINS,):
        raise RuntimeError("invalid FireRed AED CMVN resource")
    return model, means, inverse_std


def quantize_pcm_for_firered(
    pcm: npt.NDArray[np.float32],
) -> npt.NDArray[np.float32]:
    """
    Convert normalized float PCM to the signed 16-bit scale expected by FireRed.

    :param pcm: Normalized mono float PCM.
    """
    scaled = np.rint(pcm.astype(np.float64) * 32768.0)
    np.clip(scaled, -32768.0, 32767.0, out=scaled)
    return scaled.astype(np.float32)


def split_firered_features(
    features: npt.NDArray[np.float32],
) -> Iterator[tuple[npt.NDArray[np.float32], int, int]]:
    """
    Yield inference chunks with context and the core output range.

    :param features: CMVN-normalized FireRed features shaped ``(frames, 80)``.
    """
    total_frames = len(features)
    for core_start in range(0, total_frames, FIRERED_MAX_INFERENCE_FRAMES):
        core_end = min(core_start + FIRERED_MAX_INFERENCE_FRAMES, total_frames)
        chunk_start = max(0, core_start - FIRERED_INFERENCE_CONTEXT_FRAMES)
        chunk_end = min(total_frames, core_end + FIRERED_INFERENCE_CONTEXT_FRAMES)
        yield features[chunk_start:chunk_end], core_start - chunk_start, core_end - core_start


def infer_firered_chunk(
    model: nn.Module,
    features: npt.NDArray[np.float32],
    device: str = "cpu",
) -> npt.NDArray[np.float32]:
    """
    Run one FireRed AED model call.

    :param model: Loaded FireRed AED model.
    :param features: Feature chunk shaped ``(frames, 80)``.
    :param device: Device used for model inference.
    """
    if features.ndim != 2 or features.shape[1] != FIRERED_MEL_BINS:
        raise ValueError("invalid FireRed feature shape")
    if not len(features):
        return np.empty((0, FIRERED_OUTPUT_CLASSES), dtype=np.float32)
    tensor = torch.from_numpy(features).unsqueeze(0).to(device)
    with torch.inference_mode():
        probabilities = model(tensor)
    result: npt.NDArray[np.float32] = np.asarray(
        probabilities.squeeze(0).cpu().numpy(),
        dtype=np.float32,
    )
    if result.shape != (len(features), FIRERED_OUTPUT_CLASSES):
        raise RuntimeError("invalid FireRed AED output shape")
    return result


def vocal_activity_probabilities(
    frame_probabilities: npt.NDArray[np.float32],
    duration: float,
) -> npt.NDArray[np.float32]:
    """
    Convert 10 ms FireRed outputs to the persisted 100 ms vocal timeline.

    :param frame_probabilities: Speech, singing, and music probabilities per fbank frame.
    :param duration: Canonical analysis duration in seconds.
    """
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("invalid vocal activity duration")
    output_frames = math.ceil(duration / FIRERED_FRAME_DURATION)
    if output_frames == 0:
        return np.empty(0, dtype=np.float32)
    if frame_probabilities.ndim != 2 or frame_probabilities.shape[1] != (FIRERED_OUTPUT_CLASSES):
        raise ValueError("invalid FireRed AED probability shape")
    if not np.isfinite(frame_probabilities).all():
        raise ValueError("FireRed AED produced non-finite probabilities")
    if not len(frame_probabilities):
        return np.zeros(output_frames, dtype=np.float32)

    clipped = np.clip(frame_probabilities, 0.0, 1.0)
    vocal_frames = np.maximum(clipped[:, 0], clipped[:, 1])
    starts = np.arange(0, len(vocal_frames), 10)
    sums = np.add.reduceat(vocal_frames.astype(np.float64), starts)
    counts = np.minimum(10, len(vocal_frames) - starts)
    probabilities = (sums / counts).astype(np.float32)
    if len(probabilities) < output_frames:
        probabilities = np.pad(
            probabilities,
            (0, output_frames - len(probabilities)),
            mode="edge",
        )
    else:
        probabilities = probabilities[:output_frames]
    if not np.isfinite(probabilities).all():
        raise ValueError("invalid vocal activity probabilities")
    result: npt.NDArray[np.float32] = np.asarray(
        np.clip(probabilities, 0.0, 1.0),
        dtype=np.float32,
    )
    return result


class _FireRedAedModel(nn.Module):
    """FireRed AED detection model."""

    def __init__(self) -> None:
        super().__init__()
        self.dfsmn = _Dfsmn()
        self.out = nn.Linear(256, FIRERED_OUTPUT_CLASSES)

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        """Return speech, singing, and music probabilities."""
        hidden: torch.Tensor = self.dfsmn(features)
        logits: torch.Tensor = self.out(hidden)
        return torch.sigmoid(logits)


class _Dfsmn(nn.Module):
    """Deep feedforward sequential memory network used by FireRed AED."""

    def __init__(self) -> None:
        super().__init__()
        self.fc1 = nn.Sequential(nn.Linear(FIRERED_MEL_BINS, 256), nn.ReLU(), nn.Dropout(0.05))
        self.fc2 = nn.Sequential(nn.Linear(256, 128), nn.ReLU(), nn.Dropout(0.05))
        self.fsmn1 = _Fsmn()
        self.fsmns = nn.ModuleList([_DfsmnBlock() for _ in range(7)])
        self.dnns = nn.Sequential(nn.Linear(128, 256), nn.ReLU(), nn.Dropout(0.05))

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        memory = self.fsmn1(self.fc2(self.fc1(features)))
        for block in self.fsmns:
            memory = block(memory)
        output: torch.Tensor = self.dnns(memory)
        return output


class _DfsmnBlock(nn.Module):
    """Residual DFSMN block."""

    def __init__(self) -> None:
        super().__init__()
        self.fc1 = nn.Sequential(nn.Linear(128, 256), nn.ReLU(), nn.Dropout(0.05))
        self.fc2 = nn.Linear(256, 128, bias=False)
        self.fsmn = _Fsmn()

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        hidden: torch.Tensor = self.fc1(features)
        projection: torch.Tensor = self.fc2(hidden)
        memory: torch.Tensor = self.fsmn(projection)
        return features + memory


class _Fsmn(nn.Module):
    """Depthwise bidirectional memory block."""

    def __init__(self) -> None:
        super().__init__()
        self.lookback_filter = nn.Conv1d(
            128,
            128,
            kernel_size=20,
            padding=19,
            groups=128,
            bias=False,
        )
        self.lookahead_filter = nn.Conv1d(
            128,
            128,
            kernel_size=20,
            padding=19,
            groups=128,
            bias=False,
        )

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        transposed = features.permute(0, 2, 1).contiguous()
        lookback: torch.Tensor = self.lookback_filter(transposed)[:, :, :-19]
        memory: torch.Tensor = transposed + lookback
        if features.size(1) > 1:
            lookahead: torch.Tensor = self.lookahead_filter(transposed)
            memory += functional.pad(lookahead[:, :, 20:], (0, 1))
        return memory.permute(0, 2, 1).contiguous()
