"""
Regression tests for malformed provider config entries (issue #5728).

When a provider failed to load, its ``last_error`` was written back to the
provider config key. If the config had been removed in the meantime (e.g. the
user removed an unsupported provider while a load/retry was still in flight),
the underlying ``set`` helper auto-created the parent dict, leaving a stub entry
with no ``domain`` key. That stub then crashed ``get_provider_configs`` (and
therefore startup, via ``create_builtin_provider_config``) with
``KeyError: 'domain'``.

Two complementary fixes are covered here:
- ``update_provider_last_error`` only writes when the config still exists, so a
  removed provider is never resurrected as a domain-less stub (root cause).
- the ``migrate`` settings migration drops any pre-existing orphaned stubs left
  on disk by older versions, before they can reach the read path.
"""

from __future__ import annotations

from typing import Any

from music_assistant_models.config_entries import ProviderError
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.config.migrations import migrate
from music_assistant.mass import MusicAssistant


async def test_migrate_drops_orphaned_provider_stub() -> None:
    """A stored provider stub lacking a 'domain' key is removed by migration."""
    data: dict[str, Any] = {
        CONF_PROVIDERS: {
            "sonic_analysis--orphan": {"last_error": {"error_code": 999, "message": "x"}},
            "filesystem_local--1": {
                "domain": "filesystem_local",
                "type": "music",
                "instance_id": "filesystem_local--1",
            },
        }
    }
    assert await migrate(data) is True
    # the domain-less stub is gone, the valid entry is untouched
    assert "sonic_analysis--orphan" not in data[CONF_PROVIDERS]
    assert "filesystem_local--1" in data[CONF_PROVIDERS]


async def test_migrate_leaves_valid_provider_configs_alone() -> None:
    """Migration is a no-op for provider configs that all have a 'domain'."""
    data: dict[str, Any] = {
        CONF_PROVIDERS: {
            "filesystem_local--1": {
                "domain": "filesystem_local",
                "type": "music",
                "instance_id": "filesystem_local--1",
            },
        }
    }
    assert await migrate(data) is False
    assert "filesystem_local--1" in data[CONF_PROVIDERS]


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
