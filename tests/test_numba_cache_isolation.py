"""Test that numba compiles its kernels into a cache private to this test process."""

import os
from pathlib import Path

import librosa
import numpy as np


def test_numba_kernel_cache_is_process_private() -> None:
    """Verify librosa's compiled kernels are cached in this process's own directory."""
    cache_dir = Path(os.environ["NUMBA_CACHE_DIR"])
    assert cache_dir.is_dir()

    librosa.onset.onset_detect(onset_envelope=np.abs(np.sin(np.arange(256.0))), sr=22050)

    assert list(cache_dir.rglob("*peak_pick*.nbi"))
