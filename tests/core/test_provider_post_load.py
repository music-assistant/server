"""Tests for the post load steps that run once a provider is registered."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import EventType, ProviderType

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.mass import MusicAssistant
from music_assistant.models.plugin import PluginProvider
from tests.common import use_real_create_task


class _Provider(PluginProvider):
    """Provider whose post load step raises what its test asks for."""

    post_load_error: BaseException | None = None
    load_gate: asyncio.Event | None = None
    loaded_in_mass_done: bool = False

    async def loaded_in_mass(self) -> None:
        """Fail the way a provider does when a post load step hits a problem."""
        if self.load_gate is not None:
            # suspend the way a real post load step (e.g. a config migration) does
            # before it gets to registering the provider's API commands
            await self.load_gate.wait()
        if self.post_load_error:
            raise self.post_load_error
        self.loaded_in_mass_done = True


def _mass() -> MusicAssistant:
    """Return a bare MusicAssistant (bypassing __init__) able to register a provider."""
    mass = object.__new__(MusicAssistant)
    mass._providers = {}
    mass._provider_ready_events = {}
    mass.cache = MagicMock()
    mass.config = MagicMock()
    mass.discovery = MagicMock()
    mass.signal_event = MagicMock()  # type: ignore[method-assign]
    mass._update_available_providers_cache = AsyncMock()  # type: ignore[method-assign]
    mass.run_provider_discovery = AsyncMock()  # type: ignore[method-assign]
    use_real_create_task(mass)
    return mass


def _provider(mass: MusicAssistant, post_load_error: BaseException | None = None) -> _Provider:
    """Return a provider instance for the 'test' domain."""
    manifest = MagicMock()
    manifest.domain = "test"
    manifest.name = "Test Provider"
    manifest.type = ProviderType.PLUGIN
    config = MagicMock()
    config.instance_id = "test--1"
    config.name = "Test"
    config.get_value.return_value = "GLOBAL"
    provider = _Provider(mass, manifest, config)
    provider.post_load_error = post_load_error
    return provider


async def test_post_load_runs_every_step() -> None:
    """A provider that loaded runs all of its post load steps."""
    mass = _mass()
    provider = _provider(mass)

    await mass._register_loaded_provider(provider, provider.config)
    # the post load steps run as a task of their own, so let it run to completion
    await asyncio.sleep(0)

    assert provider.initialized.is_set()
    assert mass.get_provider_ready_event("test").is_set()
    cast("AsyncMock", mass.run_provider_discovery).assert_awaited_once_with("test--1")


async def test_failing_post_load_still_runs_the_remaining_steps() -> None:
    """A failed post load step must not leave the waiters of a provider hanging."""
    mass = _mass()
    provider = _provider(mass, RuntimeError("post load failed"))

    await mass._register_loaded_provider(provider, provider.config)
    await asyncio.sleep(0)

    assert provider.available is True
    assert provider.initialized.is_set()
    assert mass.get_provider_ready_event("test").is_set()
    # discovery and the default name are unrelated to whatever failed, so they still run
    cast("AsyncMock", mass.run_provider_discovery).assert_awaited_once_with("test--1")
    cast("MagicMock", mass.config).set_provider_default_name.assert_called_once_with(
        "test--1", "Test Provider"
    )


async def test_cancelled_post_load_reports_nothing() -> None:
    """A provider that is torn down mid load must not be announced as ready."""
    mass = _mass()
    provider = _provider(mass, asyncio.CancelledError())

    await mass._register_loaded_provider(provider, provider.config)
    await asyncio.sleep(0)

    assert not provider.initialized.is_set()
    assert not mass.get_provider_ready_event("test").is_set()
    cast("AsyncMock", mass.run_provider_discovery).assert_not_awaited()
    cast("MagicMock", mass.signal_event).assert_not_called()
    # the load succeeded before the post-load task, so the previous error is cleared
    # synchronously: it must not be left to a task that a later unload/reload can cancel
    cast("MagicMock", mass.config).set.assert_any_call(f"{CONF_PROVIDERS}/test--1/last_error", None)


async def test_providers_updated_announced_only_after_post_load() -> None:
    """
    The PROVIDERS_UPDATED event must wait until loaded_in_mass has finished.

    loaded_in_mass is where a provider registers its API commands, so a client that
    reacts to the event by calling one of them would otherwise hit an unknown command.
    """
    mass = _mass()
    provider = _provider(mass)
    provider.load_gate = asyncio.Event()

    await mass._register_loaded_provider(provider, provider.config)
    await asyncio.sleep(0)  # let the post load task reach the gate

    # the gate holds loaded_in_mass mid-run, so the provider is registered and available
    # but its post load step has not finished; clients must not have been told about it
    cast("MagicMock", mass.signal_event).assert_not_called()

    # release the gate; only once loaded_in_mass has finished may the event go out
    provider.load_gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert provider.loaded_in_mass_done
    cast("MagicMock", mass.signal_event).assert_called_once_with(
        EventType.PROVIDERS_UPDATED, data=[provider]
    )


async def test_providers_updated_announced_even_when_post_load_fails() -> None:
    """A provider stays available when its post load step fails, so it is still announced."""
    mass = _mass()
    provider = _provider(mass, RuntimeError("post load failed"))

    await mass._register_loaded_provider(provider, provider.config)
    await asyncio.sleep(0)

    cast("MagicMock", mass.signal_event).assert_called_once_with(
        EventType.PROVIDERS_UPDATED, data=[provider]
    )


async def test_provider_withheld_from_clients_until_initialized() -> None:
    """A provider is not offered to clients until its post load registered its commands."""
    mass = _mass()
    provider = _provider(mass)
    provider.load_gate = asyncio.Event()

    await mass._register_loaded_provider(provider, provider.config)
    await asyncio.sleep(0)  # let the post load task reach the gate

    # registered and available, but loaded_in_mass has not run: withheld from clients
    assert mass.get_providers_for_user(None) == []
    # and tracked under a per-instance id so unload_provider can cancel it
    assert "post_load_provider_test--1" in mass._tracked_tasks

    provider.load_gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert mass.get_providers_for_user(None) == [provider]
