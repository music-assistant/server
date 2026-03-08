"""Tests for the DLNA player provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.dlna.provider import DLNAPlayerProvider


@pytest.fixture
def provider() -> DLNAPlayerProvider:
    """Return a minimal DLNA provider instance for discovery tests."""
    prov = DLNAPlayerProvider.__new__(DLNAPlayerProvider)
    prov._discovery_running = False
    prov.config = MagicMock()
    prov.config.get_value.return_value = False
    prov.logger = MagicMock()
    prov.mass = MagicMock()
    prov.mass.create_task = MagicMock()
    prov.mass.loop = MagicMock()
    return prov


async def test_discover_players_logs_warning_on_socket_error(
    provider: DLNAPlayerProvider,
) -> None:
    """Socket setup failures during SSDP discovery should only log a warning."""
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
