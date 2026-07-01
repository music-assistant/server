"""
Regression tests for malformed provider config entries (music-assistant/support#5728).

When a provider failed to load, its ``last_error`` was written back to the provider
config key. If the config had been removed in the meantime (e.g. the user removed an
unsupported provider while a load/retry was still in flight), the underlying ``set``
helper auto-created the parent dict, leaving a stub entry with no ``domain`` key. That
stub then crashed ``get_provider_configs`` (and therefore startup) with
``KeyError: 'domain'``.

Two complementary fixes are covered here:
- ``update_provider_last_error`` only writes when the config still exists, so a removed
  provider is never resurrected as a domain-less stub (root cause).
- the ``_migrate`` settings migration drops any pre-existing orphaned stubs left on disk
  by older versions, before they can reach the read path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.controllers.config import ConfigController


def _make_controller() -> ConfigController:
    controller = ConfigController(MagicMock())
    controller.initialized = True
    controller.save = MagicMock()  # type: ignore[method-assign]
    return controller


def test_migrate_drops_orphaned_provider_stub() -> None:
    """A stored provider stub lacking a 'domain' key is removed by migration."""
    controller = _make_controller()
    controller._data = {
        CONF_PROVIDERS: {
            "sonic_analysis--orphan": {"last_error": "boom"},
            "filesystem_local--1": {
                "domain": "filesystem_local",
                "type": "music",
                "instance_id": "filesystem_local--1",
            },
        }
    }
    assert controller._migrate_orphaned_provider_stubs() is True
    assert "sonic_analysis--orphan" not in controller._data[CONF_PROVIDERS]
    assert "filesystem_local--1" in controller._data[CONF_PROVIDERS]


def test_migrate_leaves_valid_provider_configs_alone() -> None:
    """Migration is a no-op for provider configs that all have a 'domain'."""
    controller = _make_controller()
    controller._data = {
        CONF_PROVIDERS: {
            "filesystem_local--1": {
                "domain": "filesystem_local",
                "type": "music",
                "instance_id": "filesystem_local--1",
            },
        }
    }
    assert controller._migrate_orphaned_provider_stubs() is False
    assert "filesystem_local--1" in controller._data[CONF_PROVIDERS]


def test_update_provider_last_error_ignores_removed_entry() -> None:
    """Writing last_error must not resurrect a removed config as a domain-less stub."""
    controller = _make_controller()
    controller._data = {CONF_PROVIDERS: {}}
    controller.update_provider_last_error("sonic_analysis--xyz", "boom")
    assert controller.get(f"{CONF_PROVIDERS}/sonic_analysis--xyz") is None


def test_update_provider_last_error_writes_when_entry_exists() -> None:
    """When the config entry still exists, last_error is persisted as usual."""
    controller = _make_controller()
    controller._data = {
        CONF_PROVIDERS: {
            "filesystem_local--1": {
                "domain": "filesystem_local",
                "type": "music",
                "instance_id": "filesystem_local--1",
            }
        }
    }
    controller.update_provider_last_error("filesystem_local--1", "boom")
    assert controller.get(f"{CONF_PROVIDERS}/filesystem_local--1/last_error") == "boom"
