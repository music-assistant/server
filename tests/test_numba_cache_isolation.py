"""Test that numba compiles its kernels into a cache private to this test process."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from numba.core import config as numba_config

from tests.conftest import NUMBA_CACHE_DIR

if TYPE_CHECKING:
    import pytest


def test_numba_kernel_cache_is_process_private(pytestconfig: pytest.Config) -> None:
    """Verify numba caches its compiled kernels in a directory this process owns alone."""
    # a TemporaryDirectory of this session's own, so no other process can be writing there
    assert os.environ["NUMBA_CACHE_DIR"] == pytestconfig.stash[NUMBA_CACHE_DIR].name
    # numba snapshots the variable as it is imported, so a mismatch means it got in first
    # (numba ships type info only on newer versions, where CACHE_DIR is set at runtime)
    assert os.environ["NUMBA_CACHE_DIR"] == numba_config.CACHE_DIR  # type: ignore[attr-defined, unused-ignore]
