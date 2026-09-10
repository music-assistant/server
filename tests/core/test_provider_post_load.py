"""Tests for the post load steps that run once a provider is registered."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ProviderType

from music_assistant.mass import MusicAssistant
from music_assistant.models.plugin import PluginProvider
from tests.common import use_real_create_task


class _Provider(PluginProvider):
    """Provider whose post load step raises what its test asks for."""

    post_load_error: BaseException | None = None

    async def loaded_in_mass(self) -> None:
        """Fail the way a provider does when a post load step hits a problem."""
        if self.post_load_error:
            raise self.post_load_error


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
