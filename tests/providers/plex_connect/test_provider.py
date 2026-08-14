"""Tests for the plex.tv registration handling of the Plex Connect provider."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from music_assistant.providers import plex_connect
from music_assistant.providers.plex_connect import PlexConnectProvider
from music_assistant.providers.plex_connect.plextv import PlexTvAuthError, compute_client_id


def _make_provider(token: str | None = "devtoken") -> Any:  # noqa: S107
    """Create a minimal PlexConnectProvider instance for testing."""
    mock_mass = MagicMock()
    mock_mass.version = "2.10.0"
    mock_mass.streams.publish_ip = "192.168.1.10"
    player = MagicMock()
    player.display_name = "Living Room"
    mock_mass.players.get_player.return_value = player

    # setup_data holds the one-time selections plus the (encrypted) plex.tv token;
    # the same dict is returned by mass.config.get and mirrored on config.setup_data so
    # get_setup_value and _update_setup_data stay consistent (encrypt/decrypt are identity)
    setup_data: dict[str, Any] = {
        "mass_player_id": "player1",
        "plex_provider_id": "plexprov1",
        "plextv_token": token,
    }
    mock_mass.config.get = MagicMock(return_value=setup_data)
    mock_mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    mock_mass.config.encrypt_string = MagicMock(side_effect=lambda value: value)

    option_values: dict[str, Any] = {
        "player_name": None,
        "device_class": "speaker",
        "log_level": "INFO",
    }
    mock_config = MagicMock()
    mock_config.instance_id = "plex_connect_instance_1"
    mock_config.get_value = lambda key, default=None: option_values.get(key, default)
    mock_config.setup_data = setup_data

    mock_manifest = MagicMock()
    mock_manifest.type = "plugin"
    mock_manifest.domain = "plex_connect"

    provider = PlexConnectProvider(mock_mass, mock_manifest, mock_config)
    provider._allocated_port = 32500
    return provider


@pytest.fixture
def plextv_client() -> Generator[MagicMock]:
    """Patch the PlexTvClient used by the provider and return its instance mock."""
    client = MagicMock()
    client.get_device_id = AsyncMock(return_value="222")
    client.publish_connection = AsyncMock()
    client.delete_device = AsyncMock()
    with patch.object(plex_connect, "PlexTvClient", return_value=client) as client_cls:
        client.cls = client_cls
        yield client


async def test_register_on_plextv_publishes_connection_uri(plextv_client: MagicMock) -> None:
    """Registration verifies the device and publishes the advertised connection URI."""
    provider = _make_provider()

    await provider._register_on_plextv()

    plextv_client.get_device_id.assert_awaited_once_with("devtoken")
    plextv_client.publish_connection.assert_awaited_once_with(
        "devtoken", "222", "http://192.168.1.10:32500"
    )


async def test_register_on_plextv_identity_matches_gdm(plextv_client: MagicMock) -> None:
    """The identity presented to plex.tv matches the GDM/companion identity."""
    provider = _make_provider()

    await provider._register_on_plextv()

    identity = plextv_client.cls.call_args.args[1]
    assert identity.client_id == compute_client_id("plexprov1", "player1")
    assert identity.name == "Living Room"
    assert identity.version == "2.10.0"


async def test_register_on_plextv_without_token_skips_api(plextv_client: MagicMock) -> None:
    """Without a stored token no plex.tv calls are made at all."""
    provider = _make_provider(token=None)

    await provider._register_on_plextv()

    plextv_client.cls.assert_not_called()


async def test_register_on_plextv_401_logs_warning_and_does_not_raise(
    plextv_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A revoked token logs a re-link warning and never raises."""
    plextv_client.get_device_id = AsyncMock(side_effect=PlexTvAuthError("401"))
    provider = _make_provider()

    await provider._register_on_plextv()

    plextv_client.publish_connection.assert_not_called()
    assert "re-link" in caplog.text


async def test_register_on_plextv_network_error_does_not_raise(
    plextv_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreachable plex.tv logs a warning and never raises (GDM unaffected)."""
    plextv_client.get_device_id = AsyncMock(side_effect=aiohttp.ClientError("offline"))
    provider = _make_provider()

    await provider._register_on_plextv()

    plextv_client.publish_connection.assert_not_called()
    assert "unreachable" in caplog.text


async def test_register_on_plextv_missing_device_logs_warning(
    plextv_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A device missing from the registry logs a re-link warning, no publish."""
    plextv_client.get_device_id = AsyncMock(return_value=None)
    provider = _make_provider()

    await provider._register_on_plextv()

    plextv_client.publish_connection.assert_not_called()
    assert "re-link" in caplog.text


async def test_unload_removed_deletes_device_best_effort(plextv_client: MagicMock) -> None:
    """Removing the instance tries to deregister the device from plex.tv."""
    provider = _make_provider()

    await provider.unload(is_removed=True)

    plextv_client.delete_device.assert_awaited_once_with("devtoken", "222")


async def test_unload_removed_delete_failure_does_not_propagate(
    plextv_client: MagicMock,
) -> None:
    """A failing device removal (endpoint is best-effort) never breaks unload."""
    plextv_client.delete_device = AsyncMock(side_effect=aiohttp.ClientError("nope"))
    provider = _make_provider()

    await provider.unload(is_removed=True)


async def test_unload_without_removal_keeps_registration(plextv_client: MagicMock) -> None:
    """A regular unload (e.g. reload/shutdown) must not touch the plex.tv registry."""
    provider = _make_provider()

    await provider.unload(is_removed=False)

    plextv_client.delete_device.assert_not_called()
