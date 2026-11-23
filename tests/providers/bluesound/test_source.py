"""Tests for the Bluesound player sources."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

from pyblu.parse import parse_presets

from music_assistant.providers.bluesound.player import BluesoundPlayer


async def test_an_empty_preset_list_leaves_the_inputs_as_sources() -> None:
    """The secondary of a stereo pair answers its presets with an empty body, which is no error."""
    fake = MagicMock()
    fake.client.presets = AsyncMock(side_effect=lambda **_: parse_presets(b""))
    fake.client.inputs = AsyncMock(return_value=[MagicMock(url="Capture:hw:1", text="Optical")])

    sources = await BluesoundPlayer._get_bluesound_sources(cast("BluesoundPlayer", fake))

    assert [source.id for source in sources] == ["input-Capture:hw:1"]
