"""
Tests for ``MCPServerProvider.update_config`` routing.

The provider strips the ``values/`` prefix MA's ConfigController prepends to
each changed key, then dispatches to either a hot-swap (when every changed
key is in ``HOT_SWAPPABLE_KEYS``) or a full restart. Neither branch was
covered before — a regression in the ``removeprefix`` (e.g. dropping the
slash) or in the subset check would silently break MA-driven config edits
in production.

Importing ``provider.provider`` pulls in the full ``music_assistant`` stack
(via :class:`PluginProvider`), which transitively imports ``hass_client`` —
a dep that is **not** installed in the bare provider venv used by CI's unit
suite. We inject a minimal ``hass_client`` stub into ``sys.modules`` before
the import so the test module is importable without the HA-add-on extras.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, union-attr, attr-defined"

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


# Install the stub BEFORE the ``provider.provider`` import below. ``setdefault``
# is intentional: if a future test environment has the real ``hass_client``
# installed we want to use it.
def _install_hass_client_stub() -> None:
    """
    Inject a minimal stub for ``hass_client`` + submodules touched by MA's auth chain.

    Each submodule supplies just enough attribute surface for ``import``
    statements to succeed. The provider's update_config tests never actually
    call any of these — they just need the import to land.
    """
    if "hass_client" in sys.modules:
        return
    pkg = types.ModuleType("hass_client")
    pkg.__path__ = []
    pkg.HomeAssistantClient = object

    exc = types.ModuleType("hass_client.exceptions")
    exc.BaseHassClientError = type("BaseHassClientError", (Exception,), {})

    utils = types.ModuleType("hass_client.utils")
    for name in ("base_url", "get_auth_url", "get_token", "get_websocket_url"):
        setattr(utils, name, lambda *_a, **_k: None)

    sys.modules.update(
        {
            "hass_client": pkg,
            "hass_client.exceptions": exc,
            "hass_client.utils": utils,
        }
    )


_install_hass_client_stub()

from music_assistant.providers.fastmcp_server.provider import MCPServerProvider  # noqa: E402


def _provider_with_mock_runtime(mock_mass: MagicMock, mock_config: MagicMock) -> MCPServerProvider:
    """
    Build a provider with an injected mock runtime (no real start required).

    ``MCPServerProvider`` is a plugin subclass with framework init we don't
    want to invoke here; we set the attributes the update_config branches
    actually read and leave the rest alone.
    """
    provider = MCPServerProvider.__new__(MCPServerProvider)
    provider.mass = mock_mass
    provider.config = mock_config
    provider.logger = logging.getLogger("t")
    provider._runtime = MagicMock()
    provider._runtime.apply_permission_change = AsyncMock()
    provider._runtime.stop = AsyncMock()
    provider._runtime.start = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_hot_swappable_change_takes_hot_swap_path(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """All-permission changed_keys → ``apply_permission_change`` path."""
    provider = _provider_with_mock_runtime(mock_mass, mock_config)
    new_config = MagicMock()

    await provider.update_config(new_config, changed_keys={"values/query_library"})

    provider._runtime.apply_permission_change.assert_awaited_once()
    args, _ = provider._runtime.apply_permission_change.call_args
    assert args[0] is new_config
    # The provider strips the ``values/`` prefix before forwarding.
    assert args[1] == {"query_library"}
    provider._runtime.stop.assert_not_awaited()
    provider._runtime.start.assert_not_awaited()
    # Hot-swap path swaps ``self.config`` in place — no rebuild of runtime.
    assert provider.config is new_config


@pytest.mark.asyncio
async def test_non_hot_swappable_change_triggers_full_restart(
    mock_mass: MagicMock, mock_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change outside HOT_SWAPPABLE_KEYS rebuilds the runtime from scratch."""
    provider = _provider_with_mock_runtime(mock_mass, mock_config)
    original_runtime = provider._runtime  # captured before the rebind
    new_config = MagicMock()

    # Intercept the inline `from .server import MCPServerRuntime` so we can
    # assert a fresh runtime gets constructed without actually starting one.
    rebuilt = MagicMock()
    rebuilt.start = AsyncMock()
    factory = MagicMock(return_value=rebuilt)
    monkeypatch.setattr("music_assistant.providers.fastmcp_server.server.MCPServerRuntime", factory)

    await provider.update_config(new_config, changed_keys={"values/mount_path"})

    # Old runtime stopped, new one built + started, hot-swap NOT called.
    original_runtime.stop.assert_awaited_once()
    original_runtime.apply_permission_change.assert_not_awaited()
    factory.assert_called_once_with(mock_mass, new_config, provider.logger)
    rebuilt.start.assert_awaited_once()
    assert provider._runtime is rebuilt
    assert provider.config is new_config


@pytest.mark.asyncio
async def test_update_config_noop_when_runtime_is_none(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """If the runtime never started (e.g. setup failed), update_config is a clean no-op."""
    provider = MCPServerProvider.__new__(MCPServerProvider)
    provider.mass = mock_mass
    provider.config = mock_config
    provider.logger = logging.getLogger("t")
    provider._runtime = None

    # Must not raise.
    await provider.update_config(MagicMock(), changed_keys={"values/query_library"})


@pytest.mark.asyncio
async def test_values_prefix_stripped_off_every_key(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """
    Mixed prefixed/non-prefixed keys all normalise correctly.

    MA's ConfigController prepends ``values/`` to each changed config key,
    but the prefix is an MA implementation detail — the provider must not
    leak it into the hot-swap subset check. If ``removeprefix`` is replaced
    with e.g. ``split('/', 1)[-1]`` and an unprefixed key sneaks through,
    this test catches it.
    """
    provider = _provider_with_mock_runtime(mock_mass, mock_config)
    new_config = MagicMock()

    await provider.update_config(
        new_config,
        changed_keys={"values/query_library", "values/edit_queue", "query_metadata"},
    )

    provider._runtime.apply_permission_change.assert_awaited_once()
    forwarded_keys = provider._runtime.apply_permission_change.call_args.args[1]
    assert forwarded_keys == {"query_library", "edit_queue", "query_metadata"}
