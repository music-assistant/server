"""Tests for RAM-gated buffer-size presets."""

from unittest.mock import patch

import pytest

from music_assistant.controllers.streams import constants
from music_assistant.controllers.streams.constants import BufferSize


@pytest.mark.parametrize(
    ("total_gb", "expected"),
    [
        (2.0, [BufferSize.MINIMAL]),
        (3.9, [BufferSize.MINIMAL]),
        (4.0, [BufferSize.MINIMAL, BufferSize.BALANCED]),
        (6.0, [BufferSize.MINIMAL, BufferSize.BALANCED]),
        (8.0, [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]),
        (16.0, [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]),
        # unknown memory (0.0) -> offer everything (fail open)
        (0.0, [BufferSize.MINIMAL, BufferSize.BALANCED, BufferSize.MAXIMUM]),
    ],
)
def test_get_available_buffer_sizes(total_gb: float, expected: list[BufferSize]) -> None:
    """Balanced requires >= 4GB, Maximum >= 8GB; unknown RAM offers all (fail open)."""
    with patch.object(constants, "TOTAL_SYSTEM_MEMORY_GB", total_gb):
        assert constants.get_available_buffer_sizes() == expected
