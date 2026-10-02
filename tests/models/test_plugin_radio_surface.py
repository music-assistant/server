"""Tests for the plugin provider radio surface."""

from __future__ import annotations

import pytest

from music_assistant.models.plugin import PluginProvider


def _bare() -> PluginProvider:
    """Instantiate the provider model without running its __init__."""
    return PluginProvider.__new__(PluginProvider)


async def test_get_radio_default_raises() -> None:
    """Test that get_radio raises NotImplementedError by default."""
    with pytest.raises(NotImplementedError):
        await _bare().get_radio("some_station")


async def test_get_dynamic_radio_tracks_default_raises() -> None:
    """Test that get_dynamic_radio_tracks raises NotImplementedError by default."""
    with pytest.raises(NotImplementedError):
        await _bare().get_dynamic_radio_tracks("some_station")
