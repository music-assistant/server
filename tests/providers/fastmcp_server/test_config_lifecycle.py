"""Lifecycle test: config sub-server is mounted with secret-writes wiring."""

from __future__ import annotations

from unittest.mock import MagicMock


async def test_config_server_mounted_and_visible(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """Config sub-server is mounted and tools are visible through the root MCP."""
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
        assert "config_get_provider" in names
        assert "config_set_provider_value" in names
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
        assert "config_set_provider_value" in names
    finally:
        await runtime.stop()
