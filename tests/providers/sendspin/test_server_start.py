"""Tests for how the Sendspin provider handles its server failing to start."""

from __future__ import annotations

import errno
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import SENDSPIN_SERVER_PORT
from music_assistant.providers.sendspin.provider import SendspinProvider


def _make_provider(start_server: AsyncMock) -> tuple[SendspinProvider, MagicMock, MagicMock]:
    mass = MagicMock()
    server_api = MagicMock(start_server=start_server)
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = mass
    provider.logger = MagicMock()
    provider.config = MagicMock(instance_id="sendspin")
    provider.server_api = server_api
    provider.unregister_cbs = []
    provider._manual_ip_config = ("192.168.1.50",)
    provider._remove_orphan_virtual_player_configs = MagicMock()  # type: ignore[method-assign]
    return provider, mass, server_api


async def test_server_start_failure_unloads_with_error() -> None:
    """A port conflict unloads the provider with an error naming the port."""
    start_server = AsyncMock(side_effect=OSError(errno.EADDRINUSE, "Address already in use"))
    provider, mass, server_api = _make_provider(start_server)

    await provider.loaded_in_mass()

    mass.call_later.assert_called_once()
    _delay, target, instance_id, error = mass.call_later.call_args.args
    assert target is mass.unload_provider_with_error
    assert instance_id == "sendspin"
    assert isinstance(error, SetupFailedError)
    assert str(SENDSPIN_SERVER_PORT) in str(error)
    server_api.connect_to_client.assert_not_called()


async def test_server_start_success_connects_manual_clients() -> None:
    """A started server keeps the provider loaded and connects manual clients."""
    provider, mass, server_api = _make_provider(AsyncMock())

    await provider.loaded_in_mass()

    mass.call_later.assert_not_called()
    server_api.connect_to_client.assert_called_once()
