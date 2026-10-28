"""Tests for turning BluOS inputs into MA sources."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pyblu.entities import Input

from music_assistant.providers.bluesound.player import BluesoundPlayer

OPTICAL_URL = "Capture:hw:imxspdif,0/1/25/2?id=input2"


async def _input_names(bluos_input: Input) -> list[str]:
    """Return the source names MA builds for the player's inputs."""
    fake = MagicMock()
    fake.client.presets = AsyncMock(return_value=[])
    fake.client.inputs = AsyncMock(return_value=[bluos_input])
    sources = await BluesoundPlayer._get_bluesound_sources(cast("BluesoundPlayer", fake))
    return [source.name for source in sources]


@pytest.mark.parametrize(
    ("text", "input_id", "expected"),
    [
        ("Optical", "input2", "Input: Optical"),
        (None, "input2", "Input: input2"),
        (None, None, f"Input: {OPTICAL_URL}"),
    ],
    ids=["text", "no_text", "no_text_no_id"],
)
async def test_input_has_a_name(text: str | None, input_id: str | None, expected: str) -> None:
    """An input without a text attribute is named after its id or URL, not 'None'."""
    bluos_input = Input(id=input_id, text=text, image="", url=OPTICAL_URL)

    assert await _input_names(bluos_input) == [expected]
