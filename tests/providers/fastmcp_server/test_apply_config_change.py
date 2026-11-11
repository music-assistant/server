"""
Tests for ``MCPServerRuntime.apply_config_change`` hot-swap vs restart routing.

The provider's :meth:`update_config` strips ``values/`` prefixes from MA's
``changed_keys`` set and passes the normalised set to
:meth:`MCPServerRuntime.apply_config_change`. The runtime must decide
hot-swap vs full restart from that explicit set — not from a re-diff of
``self._config`` vs the new config, because Music Assistant mutates
:class:`ProviderConfig` in place, so the old and new references point to
the same object and a diff is empty.
"""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc"

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_resource_toggle_triggers_full_restart(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """
    A ``res_*`` toggle must restart the runtime (resources are bound at start time).

    The runtime can hot-swap only capability policy; resource registration
    happens once during :meth:`start`. If a resource toggle is mis-routed
    to the hot-swap path, the user's change silently has no effect.
    """
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    runtime.stop = AsyncMock()
    runtime.start = AsyncMock()

    await runtime.apply_config_change(mock_config, changed_keys={"res_library"})

    runtime.stop.assert_awaited_once()
    runtime.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_changed_keys_does_not_restart(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """
    A no-op call (``changed_keys=set()``) must not force a restart.

    MA's ``ConfigController`` short-circuits when there are no diffs, but the
    guard belongs here too: an empty set is by definition a subset of the
    permission keys, so classify as permission-only and let the hot-swap
    path noop-rebuild the policy snapshot.
    """
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    runtime.stop = AsyncMock()
    runtime.start = AsyncMock()

    await runtime.apply_config_change(mock_config, changed_keys=set())

    runtime.stop.assert_not_awaited()
    runtime.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_change_hot_swaps_immutable_snapshot(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """A v2 policy change replaces the resolver snapshot without remounting."""
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    before = runtime.policy_resolver
    runtime.stop = AsyncMock()
    runtime.start = AsyncMock()
    mock_config._values["policy_default"] = "Trusted"

    await runtime.apply_config_change(mock_config, changed_keys={"policy_default"})

    runtime.stop.assert_not_awaited()
    runtime.start.assert_not_awaited()
    assert runtime.policy_resolver is not before
    assert runtime.policy_resolver.resolve(None).profile.value == "Trusted"


@pytest.mark.asyncio
async def test_v1_dynamic_api_key_is_not_hot_swapped(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """A removed v1 dynamic risk key follows the ordinary restart path."""
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    runtime.stop = AsyncMock()
    runtime.start = AsyncMock()

    await runtime.apply_config_change(mock_config, changed_keys={"dynamic_api_control"})

    runtime.stop.assert_awaited_once()
    runtime.start.assert_awaited_once()
    assert runtime._config is mock_config


def test_authenticated_identity_notifies_event_buffer_policy(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """Binding a discovered token publishes its ID to the provider command owner."""
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    notified: list[frozenset[str]] = []
    runtime = MCPServerRuntime(
        mock_mass,
        mock_config,
        logging.getLogger("t"),
        policy_change_callback=notified.append,
    )

    runtime._token_identities.bind("bearer", user_id="u1", token_id="token-id")

    assert notified == [frozenset({"token-id"})]


@pytest.mark.asyncio
async def test_start_rolls_back_on_partial_mount_failure(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """
    If ``start()`` raises mid-mount, the in-progress state is torn down.

    Previously the well-known route could be registered while the main
    MCP mount failed, leaving the provider half-mounted: no MCP endpoint
    but a stale well-known route still answering 200. The new wrapper in
    :meth:`MCPServerRuntime.start` calls :meth:`stop` on any exception
    before re-raising, so a retry starts from a clean slate.
    """
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    runtime._start_impl = AsyncMock(side_effect=RuntimeError("mount blew up"))
    runtime.stop = AsyncMock()

    with pytest.raises(RuntimeError, match="mount blew up"):
        await runtime.start()

    runtime.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_rollback_swallows_stop_failure(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """
    A rollback that itself errors must not hide the original exception.

    The wrapper uses ``contextlib.suppress(Exception)`` around the rollback
    call so a failing teardown can't mask the actual start-failure cause —
    the original ``RuntimeError`` still propagates.
    """
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    runtime._start_impl = AsyncMock(side_effect=RuntimeError("primary failure"))
    runtime.stop = AsyncMock(side_effect=RuntimeError("rollback also failed"))

    with pytest.raises(RuntimeError, match="primary failure"):
        await runtime.start()

    runtime.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_rollback_preserves_primary_error_when_stop_raises_base_exception(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """Interrupting teardown cannot replace the original mount failure."""
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    runtime._start_impl = AsyncMock(side_effect=RuntimeError("primary failure"))
    runtime.stop = AsyncMock(side_effect=KeyboardInterrupt("rollback interrupted"))

    with pytest.raises(RuntimeError, match="primary failure"):
        await runtime.start()

    runtime.stop.assert_awaited_once()
