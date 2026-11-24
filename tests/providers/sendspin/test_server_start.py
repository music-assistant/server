"""Tests for how the Sendspin provider handles its server failing to start."""

from __future__ import annotations

import errno
import pathlib
import socket
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.providers.sendspin import provider as provider_module
from music_assistant.providers.sendspin.constants import DEFAULT_SERVER_PORT
from music_assistant.providers.sendspin.provider import SendspinProvider
from music_assistant.testing import bound_sendspin_port, full_mass_context


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
    provider._remove_orphan_virtual_player_configs = AsyncMock()  # type: ignore[method-assign]
    return provider, mass, server_api


async def test_the_server_runs_on_the_timeline_clock(tmp_path: pathlib.Path) -> None:
    """The Sendspin server timestamps on the clock every audio timeline of the server shares."""
    provider = MagicMock()
    provider.mass.storage_path = str(tmp_path)

    with (
        patch.object(provider_module, "get_or_create_server_identity", MagicMock()),
        patch("aiosendspin.noise.trust_store.FileServerPairingStore.open", AsyncMock()),
        patch.object(provider_module, "_evict_stale_pairings", AsyncMock(return_value=(0, 0))),
        patch.object(provider_module, "SendspinServer", MagicMock()) as server_cls,
        patch.object(provider_module, "is_port_in_use", AsyncMock(return_value=False)),
    ):
        await SendspinProvider.handle_async_init(provider)

    clock = provider.mass.timeline_clock
    assert server_cls.call_args.kwargs["clock"] is clock
    # holding the rate of that clock while it streams, until the provider unloads
    clock.add_rate_listener.assert_called_once_with(pinned=provider._streams_audio)
    assert clock.add_rate_listener.return_value in provider.unregister_cbs


def test_a_streaming_group_pins_the_rate_of_the_timeline_clock() -> None:
    """The rate of the timeline clock waits while any group of the server has a stream."""
    provider = MagicMock()
    idle = MagicMock()
    idle.group.has_active_stream = False
    streaming = MagicMock()
    streaming.group.has_active_stream = True

    provider.server_api.clients = [idle, idle]
    assert not SendspinProvider._streams_audio(provider)
    provider.server_api.clients = [idle, streaming]
    assert SendspinProvider._streams_audio(provider)


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
    assert str(DEFAULT_SERVER_PORT) in str(error)
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
            blocker.bind(("127.0.0.1", DEFAULT_SERVER_PORT))
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
            # the listener took a kernel-picked port, and the URL the server hands its own
            # Sendspin clients names that port rather than the default
            bound_port = bound_sendspin_port(mass)
            assert bound_port not in (None, 0, DEFAULT_SERVER_PORT)
            assert mass.webserver.internal_sendspin_url == f"ws://127.0.0.1:{bound_port}/sendspin"
    finally:
        blocker.close()


async def test_taken_port_falls_back_to_a_free_port(tmp_path: pathlib.Path) -> None:
    """A taken default port starts the Sendspin server on a free port instead of unloading."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    with (
        patch(
            "music_assistant.providers.sendspin.provider.is_port_in_use",
            AsyncMock(return_value=True),
        ),
        patch(
            "music_assistant.providers.sendspin.provider.select_free_port",
            AsyncMock(return_value=free_port),
        ),
    ):
        async with full_mass_context(tmp_path) as mass:
            sendspin = mass.get_provider("sendspin")
            assert isinstance(sendspin, SendspinProvider)
            assert sendspin.server_port == free_port
            assert bound_sendspin_port(mass) == free_port


async def test_no_free_port_keeps_the_default_port(tmp_path: pathlib.Path) -> None:
    """A failed search for a free port leaves the bind to the default port instead of failing setup."""
    with (
        patch(
            "music_assistant.providers.sendspin.provider.is_port_in_use",
            AsyncMock(return_value=True),
        ),
        patch(
            "music_assistant.providers.sendspin.provider.select_free_port",
            AsyncMock(side_effect=OSError("No free port available")),
        ),
    ):
        async with full_mass_context(tmp_path) as mass:
            assert isinstance(mass.get_provider("sendspin"), SendspinProvider)
            assert bound_sendspin_port(mass) not in (None, 0)
