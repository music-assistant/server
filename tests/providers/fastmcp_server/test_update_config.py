"""
Tests for ``MCPServerProvider.update_config`` routing.

The provider strips the ``values/`` prefix MA's ConfigController prepends to
each changed key, then dispatches to either a hot-swap (when every changed
key is a resource or v2 policy key) or a full restart. Neither branch was
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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.commands import ProviderCommandSet
from music_assistant.providers.fastmcp_server.constants import (
    CONF_DEFAULT_POLICY,
    CONF_POLICY_TOKEN_SUFFIXES,
)
from music_assistant.providers.fastmcp_server.policy import PolicyProfile, policy_snapshot
from music_assistant.providers.fastmcp_server.policy_config import (
    policy_mode_key,
    policy_token_suffix,
    token_policy_key,
)


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


@pytest.mark.asyncio
async def test_get_config_entries_surfaces_current_user_and_manual_tokens(
    mock_mass: MagicMock,
) -> None:
    """Provider options compose MA current-user discovery with manual IDs."""
    provider = MCPServerProvider.__new__(MCPServerProvider)
    provider.mass = mock_mass
    provider.get_config_value = MagicMock(
        side_effect=lambda key, default=None: {
            "mount_path": "/mcp/v1",
            "policy_manual_token_ids": ["manual-id"],
        }.get(key, default)
    )
    mock_mass.webserver.auth.get_current_user_info = AsyncMock(
        return_value=types.SimpleNamespace(user_id="u1")
    )
    mock_mass.webserver.auth.get_user_tokens = AsyncMock(
        return_value=[types.SimpleNamespace(token_id="discovered-id", user_id="u1", name="MCP — A")]
    )

    entries = await provider.get_config_entries()
    keys = {entry.key for entry in entries}

    assert token_policy_key("discovered-id") in keys
    assert token_policy_key("manual-id") in keys


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
    provider._runtime.apply_config_change = AsyncMock()
    provider._runtime.stop = AsyncMock()
    provider._runtime.start = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_token_policy_hot_update_persists_non_secret_suffix_index(
    mock_mass: MagicMock,
) -> None:
    """A rendered auto-token edit makes its exact policy durable for cold starts."""
    token_id = "auto-token-secret-identifier"
    suffix = policy_token_suffix(token_id)
    config = MagicMock()
    config.instance_id = "mcp_server--1"
    config.get_value.side_effect = lambda key, default=None: {
        CONF_POLICY_TOKEN_SUFFIXES: [],
    }.get(key, default)
    config.values = {
        CONF_POLICY_TOKEN_SUFFIXES: SimpleNamespace(value=[]),
    }
    provider = _provider_with_mock_runtime(mock_mass, config)

    await provider.update_config(
        config,
        changed_keys={f"values/{token_policy_key(token_id)}"},
    )

    mock_mass.config.set_raw_provider_config_value.assert_called_once_with(
        "mcp_server--1",
        CONF_POLICY_TOKEN_SUFFIXES,
        [suffix],
        immediate=True,
    )
    assert config.values[CONF_POLICY_TOKEN_SUFFIXES].value == [suffix]
    assert token_id not in repr(mock_mass.config.set_raw_provider_config_value.call_args)


@pytest.mark.asyncio
async def test_hot_swappable_change_takes_hot_swap_path(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """All-permission changed_keys → ``apply_config_change`` path."""
    provider = _provider_with_mock_runtime(mock_mass, mock_config)
    new_config = MagicMock()

    await provider.update_config(new_config, changed_keys={"values/policy_default"})

    provider._runtime.apply_config_change.assert_awaited_once()
    args, _ = provider._runtime.apply_config_change.call_args
    assert args[0] is new_config
    # The provider strips the ``values/`` prefix before forwarding.
    assert args[1] == {"policy_default"}
    provider._runtime.stop.assert_not_awaited()
    provider._runtime.start.assert_not_awaited()
    # Hot-swap path swaps ``self.config`` in place — no rebuild of runtime.
    assert provider.config is new_config


@pytest.mark.asyncio
async def test_non_hot_swappable_change_triggers_full_restart(
    mock_mass: MagicMock, mock_config: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change outside resource/v2 policy keys rebuilds the runtime from scratch."""
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
    original_runtime.apply_config_change.assert_not_awaited()
    factory.assert_called_once_with(
        mock_mass,
        new_config,
        provider.logger,
        policy_change_callback=provider._apply_policy_token_ids,
    )
    rebuilt.start.assert_awaited_once()
    assert provider._runtime is rebuilt
    assert provider.config is new_config


@pytest.mark.asyncio
async def test_runtime_replacement_preserves_raw_event_buffer_override(
    mock_mass: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new empty registry still honors the context-free hashed config override."""
    token_id = "discovered-token-id"
    values = {
        CONF_DEFAULT_POLICY: "Safe queries",
        CONF_POLICY_TOKEN_SUFFIXES: [policy_token_suffix(token_id)],
        token_policy_key(token_id): "Custom",
        policy_mode_key(Capability.DEBUG_EVENTS, token_id): "allow",
        "debug_event_buffer_capacity": 100,
    }

    def config() -> MagicMock:
        result = MagicMock()
        result.get_value.side_effect = lambda key, default=None: values.get(key, default)
        result.values = {key: SimpleNamespace(value=value) for key, value in values.items()}
        return result

    old_config = config()
    new_config = config()
    unsubscribe = MagicMock()
    mock_mass.subscribe = MagicMock(return_value=unsubscribe)
    mock_mass.register_api_command = MagicMock(return_value=MagicMock())
    commands = ProviderCommandSet(
        mock_mass,
        old_config,
        policy_provider=lambda _bearer: policy_snapshot(PolicyProfile.TRUSTED),
    )
    commands.start()
    commands.update_config(old_config, active_token_ids={token_id})
    assert mock_mass.subscribe.call_count == 1

    provider = _provider_with_mock_runtime(mock_mass, old_config)
    provider._commands = commands
    rebuilt = MagicMock()
    rebuilt.start = AsyncMock()
    monkeypatch.setattr(
        "music_assistant.providers.fastmcp_server.server.MCPServerRuntime",
        MagicMock(return_value=rebuilt),
    )

    await provider.update_config(new_config, changed_keys={"values/mount_path"})

    unsubscribe.assert_not_called()


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
    await provider.update_config(MagicMock(), changed_keys={"values/policy_default"})


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
        changed_keys={
            "values/policy_default",
            "values/policy_mode_edit_queue",
            "policy_token_deadbeef",
        },
    )

    provider._runtime.apply_config_change.assert_awaited_once()
    forwarded_keys = provider._runtime.apply_config_change.call_args.args[1]
    assert forwarded_keys == {
        "policy_default",
        "policy_mode_edit_queue",
        "policy_token_deadbeef",
    }
