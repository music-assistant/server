"""Lifecycle tests for the debug sub-server inside MCPServerRuntime."""

from __future__ import annotations

from unittest.mock import MagicMock


async def test_setup_subscribes_when_debug_events_enabled(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """When DEBUG_EVENTS=True, EventBuffer is started during runtime.start()."""
    mock_config.get_value.side_effect = lambda key, default=None: {
        "debug_events": True,
        "debug_event_buffer_capacity": 100,
    }.get(key, default if default is not None else False)

    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logger=MagicMock())
    await runtime.start()
    try:
        assert mock_mass.subscribe.called
        assert runtime._event_buffer is not None
    finally:
        await runtime.stop()


async def test_unload_does_not_raise_when_events_disabled(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """With DEBUG_EVENTS=False, no subscription is created and stop() doesn't raise."""
    mock_config.get_value.return_value = False
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logger=MagicMock())
    await runtime.start()
    await runtime.stop()  # must not raise
    assert mock_mass.subscribe.called is False


async def test_unload_stops_event_buffer_before_unmount(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """EventBuffer.stop() runs BEFORE FastMCP transport teardown (lifespan-race protection)."""
    mock_config.get_value.side_effect = lambda key, default=None: {
        "debug_events": True,
        "debug_event_buffer_capacity": 100,
    }.get(key, default if default is not None else False)

    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logger=MagicMock())
    await runtime.start()

    call_order: list[str] = []
    buf = runtime._event_buffer
    assert buf is not None
    original_stop = buf.stop

    def _stop_record() -> None:
        call_order.append("buffer_stop")
        original_stop()

    setattr(buf, "stop", _stop_record)  # noqa: B010 -- patch bound method for call-order assertion

    original_unmount = runtime._unmount

    async def _unmount_record() -> None:
        call_order.append("unmount")
        if original_unmount is not None:
            await original_unmount()

    setattr(runtime, "_unmount", _unmount_record)  # noqa: B010 -- patch teardown hook for call-order assertion

    await runtime.stop()
    assert call_order, "no recorded teardown calls"
    assert call_order[0] == "buffer_stop", call_order


async def test_event_buffer_stop_idempotent_during_unload(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """Double-stop must not raise."""
    mock_config.get_value.side_effect = lambda key, default=None: {
        "debug_events": True,
        "debug_event_buffer_capacity": 100,
    }.get(key, default if default is not None else False)

    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logger=MagicMock())
    await runtime.start()
    await runtime.stop()
    await runtime.stop()  # must not raise — second call is a no-op
