"""Test that numba compiles its kernels into a cache private to this test process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import librosa
import numpy as np

from tests.conftest import NUMBA_CACHE_DIR

if TYPE_CHECKING:
    import pytest


def test_numba_kernel_cache_is_process_private(pytestconfig: pytest.Config) -> None:
    """Verify librosa's compiled kernels are cached in this process's own directory."""
    # a TemporaryDirectory of this session's own, so no other process can be writing here
    session_cache_dir = pytestconfig.stash[NUMBA_CACHE_DIR]
    cache_dir = Path(os.environ["NUMBA_CACHE_DIR"])
    assert cache_dir.is_dir()
    assert str(cache_dir) == session_cache_dir.name

    librosa.onset.onset_detect(onset_envelope=np.abs(np.sin(np.arange(256.0))), sr=22050)

    assert list(cache_dir.rglob("*peak_pick*.nbi"))
