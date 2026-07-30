# mypy: ignore-errors
# ruff: noqa
"""Vendored S-KEY model for musical key detection.

Source: https://github.com/deezer/skey (MIT License)
Paper: S-KEY (ICASSP 2025), STONE (ISMIR 2024)
ConvNeXt blocks originally from Meta FAIR (https://github.com/facebookresearch/ConvNeXt)
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import torch
import torchaudio
from einops import rearrange
from nnAudio.features.vqt import VQT as _VQT
from torch import nn

KEY_MAP: dict[int, str] = {
    0: "A Major",
    1: "Bb Major",
    2: "B Major",
    3: "C Major",
    4: "C# Major",
    5: "D Major",
    6: "D# Major",
    7: "E Major",
    8: "F Major",
    9: "F# Major",
    10: "G Major",
    11: "G# Major",
    12: "B minor",
    13: "C minor",
    14: "C# minor",
    15: "D minor",
    16: "D# minor",
    17: "E minor",
    18: "F minor",
    19: "F# minor",
    20: "G minor",
    21: "G# minor",
    22: "A minor",
    23: "Bb minor",
}

_CHECKPOINT_PATH = Path(__file__).parent / "skey.pt"


# ---------------------------------------------------------------------------
# ConvNeXt components
# ---------------------------------------------------------------------------


class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample."""

    def __init__(self, drop_prob: float = 0.0, scale_by_keep: bool = True) -> None:
        super().__init__()
        self.drop_prob = drop_prob
        self.scale_by_keep = scale_by_keep

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Apply stochastic depth."""
        if self.drop_prob == 0.0 or not self.training:
            return x
        keep_prob = 1 - self.drop_prob
        shape = (x.shape[0],) + (1,) * (x.ndim - 1)
        random_tensor = x.new_empty(shape).bernoulli_(keep_prob)
        if keep_prob > 0.0 and self.scale_by_keep:
            random_tensor.div_(keep_prob)
        return x * random_tensor


class ConvNeXtBlock(nn.Module):
    """ConvNeXt Block: DwConv -> Permute -> LayerNorm -> Linear -> GELU -> Linear -> Permute."""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: int = 7,
        padding: int = 3,
        drop_path: float = 0.1,
        layer_scale_init_value: float = 1e-1,
    ) -> None:
        super().__init__()
        self.dwconv = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            padding=padding,
            groups=in_channels,
            padding_mode="replicate",
        )
        self.norm = nn.functional.layer_norm
        self.pwconv1 = nn.Linear(out_channels, 4 * out_channels)
        self.act = nn.GELU()
        self.pwconv2 = nn.Linear(4 * out_channels, in_channels)
        self.gamma = (
            nn.Parameter(layer_scale_init_value * torch.ones(out_channels), requires_grad=True)
            if layer_scale_init_value > 0
            else None
        )
        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass."""
        input_x = x
        x = self.dwconv(x)
        x = self.norm(x, x.shape[1:])
        x = x.permute(0, 2, 3, 1)
        x = self.pwconv1(x)
        x = self.act(x)
        x = self.pwconv2(x)
        if self.gamma is not None:
            x = self.gamma * x
        x = x.permute(0, 3, 1, 2)
        return input_x + self.drop_path(x)


class TimeDownsamplingBlock(nn.Module):
    """Time Downsampling Block: LayerNorm -> 1x2 strided Conv -> GELU."""

    def __init__(self, in_channels: int, out_channels: int, bias: bool = True) -> None:
        super().__init__()
        self.norm = nn.functional.layer_norm
        self.conv = nn.Conv2d(
            in_channels, out_channels, kernel_size=(1, 2), stride=(1, 2), bias=bias
        )
        self.act = nn.GELU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass."""
        x = self.norm(x, x.shape[1:])
        x = self.conv(x)
        return self.act(x)


# ---------------------------------------------------------------------------
# Octave pooling and ChromaNet
# ---------------------------------------------------------------------------


class OctavePool(nn.Module):
    """Average log-frequency axis across octaves, producing a chromagram."""

    def __init__(self, bins_per_octave: int) -> None:
        super().__init__()
        self.bins_per_octave = bins_per_octave

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass."""
        x = rearrange(x, "B C (j k) W -> B C k j W", k=self.bins_per_octave)
        return x.mean(dim=3)


class ChromaNet(nn.Module):
    """ChromaNet neural network from STONE (ISMIR 2024)."""

    def __init__(
        self,
        n_bins: int,
        n_harmonics: int,
        out_channels: list[int],
        kernels: list[int],
        temperature: float,
    ) -> None:
        super().__init__()
        assert len(kernels) == len(out_channels)
        self.n_harmonics = n_harmonics
        self.n_bins = n_bins
        in_channel = self.n_harmonics
        self.out_channels = out_channels
        self.kernels = kernels
        self.temperature = temperature
        self.drop_path = 0.1
        time_downsampling_blocks = []
        convnext_blocks = []
        for i, out_channel in enumerate(self.out_channels):
            time_downsampling_blocks.append(TimeDownsamplingBlock(in_channel, out_channel))
            kernel = self.kernels[i]
            convnext_blocks.append(
                ConvNeXtBlock(
                    out_channel,
                    out_channel,
                    kernel_size=kernel,
                    padding=kernel // 2,
                    drop_path=self.drop_path,
                )
            )
            in_channel = out_channel
        self.convnext_blocks = nn.ModuleList(convnext_blocks)
        self.time_downsampling_blocks = nn.ModuleList(time_downsampling_blocks)
        self.octave_pool = OctavePool(12)
        self.global_average_pool = nn.AdaptiveAvgPool2d((12, 1))
        self.classifier = nn.Conv2d(out_channel, 2, kernel_size=(1, 1))
        self.flatten = nn.Flatten()
        self.batch_norm = nn.BatchNorm2d(2, affine=False)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass returning shape (batch_size, 24) key probabilities."""
        for convnext_block, time_downsampling_block in zip(
            self.convnext_blocks, self.time_downsampling_blocks, strict=False
        ):
            x = time_downsampling_block(x)
            x = convnext_block(x)
        x = self.octave_pool(x)
        x = self.global_average_pool(x)
        x = self.classifier(x)
        x = self.batch_norm(x)
        x = self.flatten(x)
        return self.softmax(x / self.temperature)


# ---------------------------------------------------------------------------
# Harmonic VQT and CQT cropping
# ---------------------------------------------------------------------------


class VQT(_VQT):  # type: ignore[misc]
    """Harmonic VQT: a collection of VQTs with different harmonic shifts."""

    def __init__(
        self,
        *,
        harmonics: list[float],
        fmin: float,
        n_bins: int,
        bins_per_octave: int = 12,
        **kwargs: Any,
    ) -> None:
        self.harmonics = harmonics
        self.bin_shifts: list[int] = []
        self.n_bins_per_slice = n_bins
        self.fmin = fmin
        for harmonic in harmonics:
            shift = round(bins_per_octave * math.log2(harmonic))
            self.bin_shifts.append(shift)
        low_octave_shift = min([0, *self.bin_shifts]) / bins_per_octave
        fmin = fmin * (2**low_octave_shift)
        n_bins = n_bins + max([0, *self.bin_shifts]) - min([0, *self.bin_shifts])
        super().__init__(fmin=fmin, n_bins=n_bins, bins_per_octave=bins_per_octave, **kwargs)

    def forward(
        self,
        x: torch.Tensor,
        output_format: str = "Magnitude",
        normalization_type: str = "librosa",
    ) -> torch.Tensor:
        """Compute harmonic VQT and return log-scaled output."""
        vqt = super().forward(x, output_format, normalization_type)
        hvqt = []
        for shift in self.bin_shifts:
            bin_start = shift - min([0, *self.bin_shifts])
            bin_stop = bin_start + self.n_bins_per_slice
            hvqt.append(vqt[:, bin_start:bin_stop, ...])
        hvqt_tensor = torch.stack(hvqt, dim=1)
        return (  # type: ignore[no-any-return]
            (1.0 / 80.0) * torchaudio.transforms.AmplitudeToDB(top_db=80)(hvqt_tensor)
        ) + 1.0


class CropCQT(nn.Module):
    """Crop Constant-Q Transform spectrograms to a fixed height."""

    def __init__(self, height: int) -> None:
        super().__init__()
        self.height = height

    def forward(self, spectrograms: torch.Tensor, transpose: torch.Tensor) -> torch.Tensor:
        """Crop spectrograms based on transpose values."""
        return torch.stack(
            [
                s[:, int(start_idx) : int(start_idx) + self.height, :]
                for s, start_idx in zip(spectrograms, transpose, strict=False)
            ]
        )


# ---------------------------------------------------------------------------
# Checkpoint loading
# ---------------------------------------------------------------------------


def load_skey_components(
    device: str = "cpu",
) -> tuple[VQT, ChromaNet, CropCQT]:
    """Load VQT, ChromaNet, and CropCQT from the bundled S-KEY checkpoint.

    :param device: Device to load the models onto.
    """
    ckpt = torch.load(_CHECKPOINT_PATH, map_location=device, weights_only=False)

    hcqt = VQT(harmonics=[1], fmin=27.5, n_bins=99, verbose=False).to(device)
    chromanet = ChromaNet(
        n_bins=84,
        n_harmonics=1,
        out_channels=[2, 3, 40, 40, 30, 10, 3],
        kernels=[7, 7, 7, 7, 7, 5, 5],
        temperature=1,
    ).to(device)

    hcqt.load_state_dict(
        {k.replace("hcqt.", ""): v for k, v in ckpt["stone"].items() if "hcqt" in k}
    )
    chromanet.load_state_dict(
        {k.replace("chromanet.", ""): v for k, v in ckpt["stone"].items() if "chromanet" in k}
    )

    hcqt.eval()
    chromanet.eval()

    return hcqt, chromanet, CropCQT(84)
