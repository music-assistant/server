"""Tests for reloading providers when the streamserver network changes."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.controllers.streams.controller import StreamsController

BIND_IP = "0.0.0.0"
PUBLISH_IP = "192.168.1.5"
PUBLISH_PORT = 8097
PUBLISH_ADDRESSES = ["192.168.1.5", "10.0.0.5"]


def _provider(instance_id: str, *, follows_network: bool) -> MagicMock:
    """Build a stand-in provider that does or does not capture the streamserver network."""
    provider = MagicMock()
    provider.instance_id = instance_id
    provider.reload_on_streams_network_change = follows_network
    return provider


def _provider_config(instance_id: str) -> MagicMock:
    """Build the stored config a provider would be reloaded from."""
    config = MagicMock()
    config.name = instance_id
    config.domain = instance_id
    return config


def _streams_controller(*providers: MagicMock) -> StreamsController:
    """Build a streams controller with only the network state populated."""
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.providers = list(providers)
    mass.config.get_provider_config = AsyncMock(side_effect=_provider_config)
    mass.load_provider_config = AsyncMock()
    controller = StreamsController(mass)
    controller._bind_ip = BIND_IP
    controller.publish_ip = PUBLISH_IP
    controller.publish_port = PUBLISH_PORT
    controller.publish_addresses = list(PUBLISH_ADDRESSES)
    return controller


def _mass(controller: StreamsController) -> MagicMock:
    """Return the mocked server object the controller was built with."""
    return cast("MagicMock", controller.mass)


def _reloaded(controller: StreamsController) -> list[str]:
    """Return the names of the providers that were reloaded."""
    return [call.args[0].name for call in _mass(controller).load_provider_config.await_args_list]


@pytest.mark.asyncio
async def test_startup_network_is_only_recorded() -> None:
    """The network at startup is the baseline, not a change to react to."""
    controller = _streams_controller(_provider("sendspin", follows_network=True))
    await controller._reload_network_dependent_providers()
    assert _reloaded(controller) == []


@pytest.mark.asyncio
async def test_reload_without_network_change_reloads_nothing() -> None:
    """Any other streams setting is reloadable on its own, so nobody else is disturbed."""
    controller = _streams_controller(_provider("sendspin", follows_network=True))
    await controller._reload_network_dependent_providers()
    await controller._reload_network_dependent_providers()
    assert _reloaded(controller) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute", "new_value"),
    [
        ("_bind_ip", "192.168.1.9"),
        ("publish_ip", "10.45.0.20"),
        ("publish_port", 8098),
        # narrowing "auto" to one literal address leaves publish_ip itself untouched
        ("publish_addresses", ["192.168.1.5"]),
    ],
)
async def test_changed_network_reloads_only_dependent_providers(
    attribute: str, new_value: str | int | list[str]
) -> None:
    """Every part of the network is captured while a provider loads, so each must count."""
    controller = _streams_controller(
        _provider("sendspin", follows_network=True),
        _provider("spotify", follows_network=False),
    )
    await controller._reload_network_dependent_providers()
    setattr(controller, attribute, new_value)
    await controller._reload_network_dependent_providers()
    assert _reloaded(controller) == ["sendspin"]


@pytest.mark.asyncio
async def test_applied_network_is_not_reloaded_again() -> None:
    """A later reload compares against the applied network, not the one seen at startup."""
    controller = _streams_controller(_provider("sendspin", follows_network=True))
    await controller._reload_network_dependent_providers()
    controller.publish_ip = "10.45.0.20"
    await controller._reload_network_dependent_providers()
    await controller._reload_network_dependent_providers()
    assert _reloaded(controller) == ["sendspin"]


@pytest.mark.asyncio
async def test_interrupted_run_is_retried_on_the_next_reload() -> None:
    """A second config change cancels the reload, so the new network must not count as applied."""
    controller = _streams_controller(_provider("sendspin", follows_network=True))
    await controller._reload_network_dependent_providers()
    controller.publish_ip = "10.45.0.20"
    _mass(controller).load_provider_config = AsyncMock(side_effect=[asyncio.CancelledError, None])
    with pytest.raises(asyncio.CancelledError):
        await controller._reload_network_dependent_providers()
    await controller._reload_network_dependent_providers()
    # the cancelled attempt plus the retry that the stale fingerprint triggered
    assert _reloaded(controller) == ["sendspin", "sendspin"]


@pytest.mark.asyncio
async def test_failing_reload_does_not_block_the_others() -> None:
    """One provider that cannot come back up must not strand the rest on the old network."""
    controller = _streams_controller(
        _provider("snapcast", follows_network=True),
        _provider("sendspin", follows_network=True),
    )
    _mass(controller).load_provider_config = AsyncMock(side_effect=[RuntimeError("boom"), None])
    await controller._reload_network_dependent_providers()
    controller.publish_ip = "10.45.0.20"
    await controller._reload_network_dependent_providers()
    assert _reloaded(controller) == ["snapcast", "sendspin"]
