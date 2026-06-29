"""
Regression tests for malformed provider config entries (issue #5728).

When a provider failed to load, its ``last_error`` was written back to the
provider config key. If the config had been removed in the meantime (e.g. the
user removed an unsupported provider while a load/retry was still in flight),
the underlying ``set`` helper auto-created the parent dict, leaving a stub entry
with no ``domain`` key. That stub then crashed ``get_provider_configs`` (and
therefore startup, via ``create_builtin_provider_config``) with
``KeyError: 'domain'``.
"""

from __future__ import annotations

import pytest
from music_assistant_models.config_entries import ProviderError
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.mass import MusicAssistant


async def test_get_provider_configs_skips_entry_without_domain(
    mass_minimal: MusicAssistant,
) -> None:
    """A stored config stub lacking a 'domain' key is skipped, not fatal."""
    error = ProviderError(error_code=SetupFailedError.error_code, message="boom")
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/sonic_analysis--orphan",
        {"last_error": error.to_dict()},
    )
    # Must not raise KeyError: 'domain'.
    configs = await mass_minimal.config.get_provider_configs()
    assert all(c.instance_id != "sonic_analysis--orphan" for c in configs)


async def test_get_provider_configs_with_domain_filter_skips_stub(
    mass_minimal: MusicAssistant,
) -> None:
    """The exact startup path: create_builtin_provider_config filters by domain."""
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/sonic_analysis--orphan",
        {"last_error": {"error_code": 999, "message": "x"}},
    )
    # create_builtin_provider_config calls get_provider_configs(provider_domain=...).
    configs = await mass_minimal.config.get_provider_configs(provider_domain="filesystem_local")
    assert configs == []


async def test_get_provider_config_rejects_stub_without_domain(
    mass_minimal: MusicAssistant,
) -> None:
    """Fetching a single malformed entry raises a clear KeyError, not 'domain'."""
    mass_minimal.config.set(
        f"{CONF_PROVIDERS}/sonic_analysis--orphan",
        {"last_error": {"error_code": 999, "message": "x"}},
    )
    with pytest.raises(KeyError, match="sonic_analysis--orphan"):
        await mass_minimal.config.get_provider_config("sonic_analysis--orphan")


async def test_update_provider_last_error_ignores_removed_entry(
    mass_minimal: MusicAssistant,
) -> None:
    """Writing last_error must not resurrect a removed config as a domain-less stub."""
    config = mass_minimal.config
    instance = "sonic_analysis--xyz"
    # No config entry exists (it was removed).
    error = ProviderError(error_code=SetupFailedError.error_code, message="boom")
    config.update_provider_last_error(instance, error)
    assert config.get(f"{CONF_PROVIDERS}/{instance}") is None


async def test_update_provider_last_error_writes_when_entry_exists(
    mass_minimal: MusicAssistant,
) -> None:
    """When the config entry still exists, last_error is persisted as usual."""
    config = mass_minimal.config
    instance = "filesystem_local--1"
    config.set(
        f"{CONF_PROVIDERS}/{instance}",
        {"domain": "filesystem_local", "type": "music", "instance_id": instance},
    )
    error = ProviderError(error_code=SetupFailedError.error_code, message="boom")
    config.update_provider_last_error(instance, error)
    stored = config.get(f"{CONF_PROVIDERS}/{instance}/last_error")
    assert stored is not None
    assert stored["message"] == "boom"
