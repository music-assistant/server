"""Tests for the DLNA player provider."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.dlna.provider import DLNAPlayerProvider


async def test_discover_players_logs_warning_on_socket_error() -> None:
    """Socket setup failures during SSDP discovery should only log a warning."""
    provider = cast("Any", DLNAPlayerProvider.__new__(DLNAPlayerProvider))
    provider._discovery_running = False
    provider.config = MagicMock()
    provider.config.get_value.return_value = False
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.create_task = MagicMock()
    provider.mass.loop = MagicMock()
    err = OSError(49, "Can't assign requested address")

    with patch(
        "music_assistant.providers.dlna.provider.async_search",
        new=AsyncMock(side_effect=err),
    ) as mock_async_search:
        await provider.discover_players()

    mock_async_search.assert_awaited_once()
    provider.logger.warning.assert_called_once_with("DLNA SSDP discovery failed: %s", err)
    provider.mass.loop.call_later.assert_called_once()
    assert provider._discovery_running is False
