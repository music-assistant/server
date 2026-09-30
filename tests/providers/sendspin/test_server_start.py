"""Tests for how the Sendspin provider handles its server failing to start."""

from __future__ import annotations

import errno
import pathlib
import socket
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, SENDSPIN_SERVER_PORT
from music_assistant.providers.sendspin import provider as sendspin_provider
from music_assistant.providers.sendspin.provider import SendspinProvider
from tests.conftest import full_mass_context


def _make_provider(start_server: AsyncMock) -> tuple[SendspinProvider, MagicMock, MagicMock]:
    mass = MagicMock()
    mass.players.iter_players.return_value = []
    manifest = MagicMock(domain="sendspin")
    manifest.name = "Sendspin"
    config = MagicMock(instance_id="sendspin")
    config.name = "Sendspin"
    config.get_value.side_effect = lambda key, default=None: (
        ["192.168.1.50"] if key == CONF_ENTRY_MANUAL_DISCOVERY_IPS.key else default
    )
    provider = SendspinProvider(mass, manifest, config)
    server_api = MagicMock(start_server=start_server, close=AsyncMock())
    provider.server_api = server_api
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


async def test_unload_after_failed_start_skips_server_close() -> None:
    """Unloading after a failed start does not close the server that never started."""
    start_server = AsyncMock(side_effect=OSError(errno.EADDRINUSE, "Address already in use"))
    provider, _mass, server_api = _make_provider(start_server)
    await provider.loaded_in_mass()

    await provider.unload()

    server_api.close.assert_not_called()
    assert provider.unregister_cbs == []


async def test_server_start_success_connects_manual_clients() -> None:
    """A started server keeps the provider loaded, connects manual clients and closes on unload."""
    provider, mass, server_api = _make_provider(AsyncMock())

    await provider.loaded_in_mass()
    await provider.unload()

    mass.call_later.assert_not_called()
    server_api.connect_to_client.assert_called_once()
    server_api.close.assert_awaited_once()


async def test_full_boot_does_not_depend_on_the_default_sendspin_port(
    tmp_path: pathlib.Path,
) -> None:
    """
    A booted test server keeps its Sendspin provider even when the default port is taken.

    Parallel test workers each boot a server; if they all bound the default port, the
    losers' Sendspin providers would unload themselves mid-test and take their players
    (and queues) with them.
    """
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            blocker.bind(("127.0.0.1", SENDSPIN_SERVER_PORT))
            blocker.listen()
        except OSError as err:
            # a port already held by something else is the very condition under test;
            # any other failure to set up the blocker must not pass as that condition
            if err.errno != errno.EADDRINUSE:
                raise
        async with full_mass_context(tmp_path) as mass:
            sendspin = mass.get_provider("sendspin")
            assert sendspin is not None
            assert isinstance(sendspin, SendspinProvider)
            assert not sendspin._server_start_failed
            # the URL the server hands its own Sendspin clients names the port it listens on
            # (the fixture patches the provider module's copy of the constant)
            bound_port = getattr(sendspin_provider, "SENDSPIN_SERVER_PORT")  # noqa: B009
            assert bound_port != SENDSPIN_SERVER_PORT
            assert mass.webserver.internal_sendspin_url.endswith(f":{bound_port}/sendspin")
    finally:
        blocker.close()
