"""Lifecycle test: config sub-server is mounted with secret-writes wiring."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.config_entries import ConfigActionResult
from music_assistant_models.errors import ActionUnavailable

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.fastmcp_server import _init_helpers, server
from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.commands import ProviderCommandSet
from music_assistant.providers.fastmcp_server.constants import (
    CONF_DEFAULT_POLICY,
    CONF_POLICY_TOKEN_SUFFIXES,
)
from music_assistant.providers.fastmcp_server.policy_config import (
    policy_mode_key,
    policy_token_suffix,
    token_policy_key,
)
from music_assistant.providers.fastmcp_server.provider import MCPServerProvider
from music_assistant.providers.fastmcp_server.server import MCPServerRuntime


class _LifecycleMass:
    """Small MA boundary that records provider-owned registrations and subscriptions."""

    def __init__(self, call_order: list[str] | None = None) -> None:
        self.call_order = call_order if call_order is not None else []
        self.registered: dict[str, Callable[..., Any]] = {}
        self.register_api_command = MagicMock(side_effect=self._register)
        self.subscribe = MagicMock(side_effect=self._subscribe)
        self.webserver: Any = None

    def _register(
        self, command: str, handler: Callable[..., Any], **_kwargs: Any
    ) -> Callable[[], None]:
        if command in self.registered:
            raise RuntimeError(f"duplicate {command}")
        self.registered[command] = handler

        def unregister() -> None:
            self.call_order.append("commands.stop")
            self.registered.pop(command, None)

        return unregister

    def _subscribe(self, _callback: Callable[..., Any]) -> Callable[[], None]:
        def unsubscribe() -> None:
            self.call_order.append("buffer.stop")

        return unsubscribe


def _provider(mass: Any, config: MagicMock) -> MCPServerProvider:
    """Create a provider without invoking its MA framework constructor."""
    provider = MCPServerProvider.__new__(MCPServerProvider)
    provider.mass = mass
    provider.config = config
    provider.manifest = cast("Any", SimpleNamespace(domain="fastmcp_server"))
    provider.logger = logging.getLogger("task-7-lifecycle")
    provider._runtime = None
    return provider


def _config(*, debug_events: bool = False) -> MagicMock:
    """Return only the config surface provider command registration needs."""
    config = MagicMock()
    config.get_value.side_effect = lambda key, default=None: {
        CONF_DEFAULT_POLICY: "Custom",
        policy_mode_key(Capability.DEBUG_EVENTS): "allow" if debug_events else "deny",
        "debug_event_buffer_capacity": 100,
    }.get(key, default)
    return config


@pytest.mark.asyncio
async def test_open_connect_action_returns_a_one_shot_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frontend receives a one-shot URL without rebuilding the config form."""
    provider = _provider(MagicMock(), _config())
    get_entries = AsyncMock()
    monkeypatch.setattr(MCPServerProvider, "get_config_entries", get_entries)
    monkeypatch.setattr(MCPServerProvider, "get_config_value", MagicMock(return_value="/mcp/v1"))
    wizard_url = "https://ma.example/mcp/v1/connect?bootstrap=one-shot"
    monkeypatch.setattr(_init_helpers, "_dispatch_open_connect", AsyncMock(return_value=wizard_url))

    result = await provider.handle_config_action("open_connect")

    assert isinstance(result, ConfigActionResult)
    assert result.open_url == wizard_url
    assert result.message is None
    get_entries.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_connect_action_without_url_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing wizard URL is a translated action failure, not a silent redraw."""
    provider = _provider(MagicMock(), _config())
    monkeypatch.setattr(MCPServerProvider, "get_config_value", MagicMock(return_value="/mcp/v1"))
    monkeypatch.setattr(_init_helpers, "_dispatch_open_connect", AsyncMock(return_value=None))

    with pytest.raises(ActionUnavailable) as error:
        await provider.handle_config_action("open_connect")

    assert error.value.translation_key == "connect_wizard_unavailable"
    assert error.value.translation_owner == provider.translation_owner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_result", [None, ConfigActionResult(open_url="https://ma.example/result")]
)
async def test_unknown_config_action_preserves_native_result(
    monkeypatch: pytest.MonkeyPatch,
    base_result: ConfigActionResult | None,
) -> None:
    """Future MA action outcome types pass through without tuple coercion."""
    provider = _provider(MagicMock(), _config())
    base_handler = AsyncMock(return_value=base_result)
    monkeypatch.setattr(PluginProvider, "handle_config_action", base_handler)

    result = await provider.handle_config_action("future_ma_action")

    assert result is base_result
    base_handler.assert_awaited_once_with("future_ma_action")


@pytest.mark.asyncio
async def test_mcp_restart_does_not_reregister_provider_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime restart retains the one provider command registry surface."""
    mass = _LifecycleMass()
    provider = _provider(mass, _config())

    class Runtime:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        def dynamic_diagnostics(self) -> dict[str, bool]:
            return {"available": True}

    monkeypatch.setattr(server, "MCPServerRuntime", Runtime)

    await provider.handle_async_init()
    commands = provider._commands
    calls_after_start = mass.register_api_command.call_count

    await provider.update_config(_config(), {"mount_path"})

    assert provider._commands is commands
    assert mass.register_api_command.call_count == calls_after_start
    assert commands is not None
    assert commands._diagnostics_provider is not None
    assert commands._diagnostics_provider() == {"available": True}
    await provider.unload()


@pytest.mark.asyncio
async def test_runtime_start_base_exception_rolls_back_provider_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt during runtime start tears down every already-owned command."""
    call_order: list[str] = []
    mass = _LifecycleMass(call_order)
    provider = _provider(mass, _config())

    class Runtime:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def start(self) -> None:
            raise KeyboardInterrupt("mount interrupted")

        async def stop(self) -> None:
            call_order.append("runtime.stop")
            raise RuntimeError("teardown failed")

    monkeypatch.setattr(server, "MCPServerRuntime", Runtime)

    with pytest.raises(KeyboardInterrupt, match="mount interrupted"):
        await provider.handle_async_init()

    assert provider._runtime is None
    assert provider._commands is None
    assert mass.registered == {}
    assert call_order[0] == "runtime.stop"
    assert "commands.stop" in call_order


@pytest.mark.asyncio
async def test_unload_stops_mcp_before_unregistering_commands() -> None:
    """The endpoint is removed before its supporting MA commands disappear."""
    call_order: list[str] = []
    provider = _provider(MagicMock(), _config())
    provider._runtime = MagicMock(
        stop=AsyncMock(side_effect=lambda: call_order.append("runtime.stop"))
    )
    provider._commands = MagicMock(
        stop=MagicMock(side_effect=lambda: call_order.append("commands.stop"))
    )

    await provider.unload()

    assert call_order == ["runtime.stop", "commands.stop"]


@pytest.mark.asyncio
async def test_restart_does_not_duplicate_event_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP-only restarts preserve the command-owned event subscriber exactly once."""
    mass = _LifecycleMass()
    provider = _provider(mass, _config(debug_events=True))

    class Runtime:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(server, "MCPServerRuntime", Runtime)

    await provider.handle_async_init()
    await provider.update_config(_config(debug_events=True), {"mount_path"})

    assert mass.subscribe.call_count == 1
    await provider.unload()


@pytest.mark.asyncio
async def test_auto_discovered_debug_override_activates_buffer_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hashed configured overrides retain events without a settings-user lookup."""
    mass = _LifecycleMass()
    token_id = "auto-token-id"
    mass.webserver = MagicMock()
    mass.webserver.auth.get_current_user_info = AsyncMock(
        side_effect=AssertionError("startup must not depend on a current settings user")
    )
    mass.webserver.auth.get_user_tokens = AsyncMock(
        side_effect=AssertionError("startup must not enumerate user tokens")
    )
    values = {
        CONF_DEFAULT_POLICY: "Safe queries",
        CONF_POLICY_TOKEN_SUFFIXES: [policy_token_suffix(token_id)],
        token_policy_key(token_id): "Custom",
        policy_mode_key(Capability.DEBUG_EVENTS, token_id): "allow",
        "debug_event_buffer_capacity": 100,
    }
    config = MagicMock()
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    config.values = {key: SimpleNamespace(value=value) for key, value in values.items()}
    provider = _provider(mass, config)

    class Runtime:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(server, "MCPServerRuntime", Runtime)
    await provider.handle_async_init()

    assert mass.subscribe.call_count == 1
    mass.webserver.auth.get_current_user_info.assert_not_awaited()
    mass.webserver.auth.get_user_tokens.assert_not_awaited()
    await provider.unload()


@pytest.mark.asyncio
async def test_config_reaches_commands_before_runtime_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live MA commands observe the new config before the MCP runtime changes."""
    call_order: list[str] = []
    provider = _provider(MagicMock(), _config())
    provider._commands = MagicMock(
        spec=ProviderCommandSet,
        update_config=MagicMock(
            side_effect=lambda _config, **_kwargs: call_order.append("commands.update")
        ),
    )
    provider._runtime = MagicMock(
        stop=AsyncMock(side_effect=lambda: call_order.append("runtime.stop")),
    )

    class Runtime:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def start(self) -> None:
            call_order.append("runtime.start")

    monkeypatch.setattr("music_assistant.providers.fastmcp_server.server.MCPServerRuntime", Runtime)

    await provider.update_config(_config(), {"mount_path"})

    assert call_order[:2] == ["commands.update", "runtime.stop"]


@pytest.mark.asyncio
async def test_failed_runtime_replacement_clears_runtime_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed non-hot restart leaves diagnostics unavailable and can be retried."""
    mass = _LifecycleMass()
    provider = _provider(mass, _config())
    fail_replacement = False

    class Runtime:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.stopped = 0
            instances.append(self)

        async def start(self) -> None:
            if fail_replacement:
                raise KeyboardInterrupt("replacement interrupted")

        async def stop(self) -> None:
            self.stopped += 1

        def dynamic_diagnostics(self) -> dict[str, bool]:
            return {"available": True}

    instances: list[Runtime] = []

    monkeypatch.setattr(server, "MCPServerRuntime", Runtime)
    await provider.handle_async_init()
    initial = cast("Runtime | None", provider._runtime)
    assert initial is not None

    fail_replacement = True
    with pytest.raises(KeyboardInterrupt, match="replacement interrupted"):
        await provider.update_config(_config(), {"mount_path"})

    assert initial.stopped == 1
    assert instances[-1].stopped == 1
    assert provider._runtime is None
    assert provider._commands is not None
    assert provider._commands._diagnostics_provider is not None
    assert provider._commands._diagnostics_provider()["available"] is False

    fail_replacement = False
    await provider.update_config(_config(), {"mount_path"})

    assert cast("Runtime | None", provider._runtime) is instances[-1]
    assert provider._commands.event_buffer is not None
    await provider.unload()


def test_runtime_exposes_dynamic_diagnostics_without_adapter_leak(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """Provider closures can read a plain health snapshot without reaching into runtime internals."""
    runtime = MCPServerRuntime(mock_mass, mock_config, logger=MagicMock())
    runtime._dynamic_adapter = MagicMock(diagnostics=MagicMock(return_value={"available": True}))

    assert runtime.dynamic_diagnostics() == {
        "available": True,
        "policy_schema_version": 2,
        "token_resolution_failures": 0,
    }


async def test_config_server_mounted_and_visible(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """Config sub-server stays internal behind the permanent meta surface."""
    mock_config.get_value.side_effect = lambda key, default=None: {
        "config_read": True,
        "config_write_provider": True,
        "config_write_secret": True,
    }.get(key, default if default is not None else False)

    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logger=MagicMock())
    await runtime.start()
    try:
        from fastmcp import Client  # noqa: PLC0415

        async with Client(runtime._mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert names == {"search_tools", "get_tool_schema", "call_tool"}
    finally:
        await runtime.stop()


async def test_config_secret_flag_threaded(mock_mass: MagicMock, mock_config: MagicMock) -> None:
    """secret_writes_enabled is threaded from CONF_CONFIG_WRITE_SECRET config value."""
    # config_write_secret OFF → provider/read/write visible, but the
    # secret value-gate is closed. We assert the runtime starts cleanly
    # with the flag off (the gate behavior itself is unit-tested elsewhere).
    mock_config.get_value.side_effect = lambda key, default=None: {
        "config_read": True,
        "config_write_provider": True,
        "config_write_secret": False,
    }.get(key, default if default is not None else False)

    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logger=MagicMock())
    await runtime.start()
    try:
        from fastmcp import Client  # noqa: PLC0415

        async with Client(runtime._mcp) as client:
            names = {t.name for t in await client.list_tools()}
        assert names == {"search_tools", "get_tool_schema", "call_tool"}
    finally:
        await runtime.stop()
