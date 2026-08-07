"""Tests for the post load steps that run once a provider is registered."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import ProviderType

from music_assistant.mass import MusicAssistant
from music_assistant.models.plugin import PluginProvider
from tests.common import use_real_create_task


class _FailingProvider(PluginProvider):
    """Provider whose post load step fails."""

    async def loaded_in_mass(self) -> None:
        """Fail the way a provider does when a post load step hits a problem."""
        raise RuntimeError("post load failed")


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


def _provider(mass: MusicAssistant) -> _FailingProvider:
    """Return a provider instance for the 'test' domain."""
    manifest = MagicMock()
    manifest.domain = "test"
    manifest.type = ProviderType.PLUGIN
    config = MagicMock()
    config.instance_id = "test--1"
    config.name = "Test"
    config.get_value.return_value = "GLOBAL"
    return _FailingProvider(mass, manifest, config)


async def test_failing_post_load_still_reports_the_provider_as_ready() -> None:
    """A failed post load step must not leave the waiters of a provider hanging."""
    mass = _mass()
    provider = _provider(mass)

    await mass._register_loaded_provider(provider, provider.config)
    # the post load steps run as a task of their own, so let it reach its failure
    await asyncio.sleep(0)

    assert provider.available is True
    assert provider.initialized.is_set()
    assert mass.get_provider_ready_event("test").is_set()


async def test_successful_post_load_runs_the_remaining_steps() -> None:
    """The steps behind the ready signal still run when the post load step succeeds."""
    mass = _mass()
    provider = _provider(mass)
    loaded_in_mass = AsyncMock()
    provider.loaded_in_mass = loaded_in_mass  # type: ignore[method-assign]

    await mass._register_loaded_provider(provider, provider.config)
    await asyncio.sleep(0)

    loaded_in_mass.assert_awaited_once()
    assert mass.get_provider_ready_event("test").is_set()
    cast("AsyncMock", mass.run_provider_discovery).assert_awaited_once_with("test--1")
