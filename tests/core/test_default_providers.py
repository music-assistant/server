"""Tests for the automatic setup of the default providers."""

from __future__ import annotations

import importlib
import inspect
from typing import cast
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import UnsupportedSystemError

from music_assistant.constants import CONF_PROVIDERS, DEFAULT_PROVIDERS
from music_assistant.mass import MusicAssistant


@pytest.mark.parametrize("domain", sorted(domain for domain, _ in DEFAULT_PROVIDERS))
def test_default_provider_setup_accepts_auto_setup(domain: str) -> None:
    """Every default provider's setup() accepts the auto_setup flag mass passes on first boot."""
    module = importlib.import_module(f"music_assistant.providers.{domain}")
    parameter = inspect.signature(module.setup).parameters.get("auto_setup")
    assert parameter is not None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False


def _mass_able_to_load(domain: str) -> MusicAssistant:
    """Return a minimal MusicAssistant that can run _load_provider for the given domain."""
    mass = object.__new__(MusicAssistant)
    mass._providers = {}
    manifest = MagicMock(domain=domain, multi_instance=False, depends_on=None, requirements=[])
    mass._provider_manifests = {domain: manifest}
    mass.config = MagicMock()
    mass.config.rehydrate_provider_config = AsyncMock()
    mass.unload_provider = AsyncMock()  # type: ignore[method-assign]
    mass._register_loaded_provider = AsyncMock()  # type: ignore[method-assign]
    return mass


async def test_load_provider_passes_auto_setup_only_when_set() -> None:
    """The auto_setup flag reaches setup() only when set, so other providers never see it."""
    mass = _mass_able_to_load("test")
    conf = MagicMock(enabled=True, domain="test", instance_id="test--1")
    conf.name = "Test"
    provider = MagicMock()
    provider.handle_async_init = AsyncMock()
    prov_mod = MagicMock()
    prov_mod.setup = AsyncMock(return_value=provider)

    with patch("music_assistant.mass.load_provider_module", AsyncMock(return_value=prov_mod)):
        await mass._load_provider(conf, auto_setup=True)
        assert prov_mod.setup.await_args.kwargs == {"auto_setup": True}

        await mass._load_provider(conf)
        assert prov_mod.setup.await_args.kwargs == {}


def _mass_refusing_setup(instance_id: str) -> MusicAssistant:
    """Return a minimal MusicAssistant whose provider refuses to run on this host."""
    mass = object.__new__(MusicAssistant)
    provider_config = MagicMock(enabled=True, instance_id=instance_id)
    provider_config.name = "Test"
    mass.config = MagicMock()
    mass.config.get_provider_config = AsyncMock(return_value=provider_config)
    mass.load_provider_config = AsyncMock(  # type: ignore[method-assign]
        side_effect=UnsupportedSystemError("refused")
    )
    mass.call_later = MagicMock()  # type: ignore[method-assign]
    mass._tracked_timers = {}
    return mass


async def test_auto_setup_refusal_drops_default_provider_config() -> None:
    """A refused automatic setup drops the config; a refused manual load records the error."""
    instance_id = "test--1"

    mass = _mass_refusing_setup(instance_id)
    await mass.load_provider(instance_id, auto_setup=True)
    cast("AsyncMock", mass.load_provider_config).assert_awaited_once_with(ANY, auto_setup=True)
    config = cast("MagicMock", mass.config)
    config.remove.assert_called_once_with(f"{CONF_PROVIDERS}/{instance_id}")
    config.update_provider_last_error.assert_not_called()

    mass = _mass_refusing_setup(instance_id)
    await mass.load_provider(instance_id, auto_setup=False)
    config = cast("MagicMock", mass.config)
    config.remove.assert_not_called()
    config.update_provider_last_error.assert_called_once()
