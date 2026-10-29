"""Tests for the HEOS player provider."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pyheos import CommandError
from pyheos import const as heos_const

from music_assistant.providers.heos.provider import HeosPlayerProvider


async def test_populate_sources_skips_unresponsive_device() -> None:
    """A device whose inputs cannot be browsed must not fail the source population."""
    healthy_input = MagicMock(source_id=1, media_id="inputs/aux_in_1")
    healthy_device = MagicMock()
    healthy_device.name = "Living Room"
    healthy_device.browse = AsyncMock(return_value=SimpleNamespace(items=[healthy_input]))
    unresponsive_device = MagicMock()
    unresponsive_device.name = "Bedroom"
    unresponsive_device.browse = AsyncMock(
        side_effect=CommandError("browse/browse", "Command timed out")
    )

    heos = MagicMock()
    heos.browse = AsyncMock(
        return_value=SimpleNamespace(items=[unresponsive_device, healthy_device])
    )
    heos.get_music_sources = AsyncMock(return_value={})

    provider = HeosPlayerProvider.__new__(HeosPlayerProvider)
    provider._heos = heos
    provider._music_source_list = []
    provider._input_source_list = []
    provider.logger = MagicMock()

    await provider._populate_sources()

    heos.browse.assert_awaited_once_with(heos_const.MUSIC_SOURCE_AUX_INPUT)
    assert provider.input_source_list == [healthy_input]
    provider.logger.warning.assert_called_once()
