"""Tests for reading a path of the stored config through values of another shape."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.mark.parametrize("stored", [["a", "b"], "scalar", 5, True])
async def test_a_path_through_a_value_that_is_no_mapping_gives_the_default(
    mass: MusicAssistant, stored: Any
) -> None:
    """A value where a mapping is expected (a hand-edited or damaged config) reads as absent."""
    mass.config.set("players/player_a/values", stored)

    assert mass.config.get("players/player_a/values/name", "default") == "default"
    assert mass.config.get("players/player_a/values/name/deeper") is None
    assert mass.config.get("players/player_a/values") == stored
