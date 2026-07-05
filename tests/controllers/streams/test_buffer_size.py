"""Tests for RAM-gated buffer-size presets."""

from unittest.mock import patch

import pytest

from music_assistant.controllers.streams import constants
from music_assistant.controllers.streams.constants import BufferSize


@pytest.mark.parametrize(
    ("total_gb", "expected"),
    [
        (2.0, [BufferSize.MINIMAL]),
        (3.6, [BufferSize.MINIMAL]),
        # "4GB" hosts that report slightly less (kernel-reserved RAM, ~3.8GB) still reach
        # Balanced: the 4GB target is checked with the reporting tolerance (floor ~3.68GB).
        (3.7, [BufferSize.MINIMAL, BufferSize.BALANCED]),
        (3.8, [BufferSize.MINIMAL, BufferSize.BALANCED]),
        (4.0, [BufferSize.MINIMAL, BufferSize.BALANCED]),
        (6.0, [BufferSize.MINIMAL, BufferSize.BALANCED]),
        (6.9, [BufferSize.MINIMAL, BufferSize.BALANCED]),
        # Maximum is an 8GB nominal target on the same tolerance (floor ~7.36GB), so a 7.0GB
        # host gets Balanced only while an "8GB" box reporting ~7.4GB+ reaches Maximum.
        (7.0, [BufferSize.MINIMAL, BufferSize.BALANCED]),
        (7.4, [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]),
        (7.7, [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]),
        (8.0, [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]),
        (16.0, [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]),
        # unknown memory (0.0) -> offer everything (fail open)
        (0.0, [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]),
    ],
)
def test_get_available_buffer_sizes(total_gb: float, expected: list[BufferSize]) -> None:
    """Balanced needs ~4GB (floor ~3.68GB), Maximum ~8GB (floor ~7.36GB); unknown RAM offers all."""
    with patch.object(constants, "TOTAL_SYSTEM_MEMORY_GB", total_gb):
        assert constants.get_available_buffer_sizes() == expected


@pytest.mark.parametrize(
    ("total_gb", "expected"),
    [
        (2.0, BufferSize.MINIMAL),
        (3.6, BufferSize.MINIMAL),
        (3.7, BufferSize.BALANCED),
        (4.0, BufferSize.BALANCED),
        (6.9, BufferSize.BALANCED),
        (7.0, BufferSize.BALANCED),
        (7.4, BufferSize.MAXIMUM),
        (7.7, BufferSize.MAXIMUM),
        (16.0, BufferSize.MAXIMUM),
        # unknown memory (0.0) -> Minimal default (conservative)
        (0.0, BufferSize.MINIMAL),
    ],
)
def test_default_buffer_size(total_gb: float, expected: BufferSize) -> None:
    """The auto-selected default follows the same ~4GB/~8GB cutoffs as the available presets."""
    with patch.object(constants, "TOTAL_SYSTEM_MEMORY_GB", total_gb):
        assert constants._get_default_buffer_size() == expected
