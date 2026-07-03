"""
Tests for ``MCPServerRuntime.apply_permission_change`` hot-swap vs restart routing.

The provider's :meth:`update_config` strips ``values/`` prefixes from MA's
``changed_keys`` set and passes the normalised set to
:meth:`MCPServerRuntime.apply_permission_change`. The runtime must decide
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

    The runtime can hot-swap only permission tags; resource registration
    happens once during :meth:`start`. If a resource toggle is mis-routed
    to the hot-swap path, the user's change silently has no effect.
    """
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    runtime.stop = AsyncMock()
    runtime.start = AsyncMock()

    await runtime.apply_permission_change(mock_config, changed_keys={"res_library"})

    runtime.stop.assert_awaited_once()
    runtime.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_lean_admin_schema_toggle_triggers_full_restart(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """
    Toggling ``lean_admin_schema`` must restart — it is read at build time.

    The flag decides whether config/debug tools are registered with output
    schemas, which happens once during :meth:`start`. If a future change
    mis-files it under ``PERMISSION_KEYS`` it would route to the hot-swap path
    and the toggle would silently have no effect until the next restart.
    """
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    runtime.stop = AsyncMock()
    runtime.start = AsyncMock()

    await runtime.apply_permission_change(mock_config, changed_keys={"lean_admin_schema"})

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
    path noop-rebuild the tag snapshot.
    """
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    runtime._allowed_tags = {"query:library"}
    runtime.stop = AsyncMock()
    runtime.start = AsyncMock()

    await runtime.apply_permission_change(mock_config, changed_keys=set())

    runtime.stop.assert_not_awaited()
    runtime.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_only_change_hot_swaps(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """
    A permission-key-only change rebuilds ``_allowed_tags`` from the new config.

    The preset tag (``edit:queue``) is intentionally NOT enabled in
    ``mock_config``'s defaults, while ``query:library`` IS — so a passing
    test proves the rebuild actually happened. Asserting on a tag that
    matches the defaults would hold whether the rebuild ran or not (the
    original tautology we are fixing here).
    """
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    # Preset a tag that's NOT in mock_config defaults — must be REMOVED after the rebuild.
    runtime._allowed_tags = {"edit:queue"}
    runtime.stop = AsyncMock()
    runtime.start = AsyncMock()

    await runtime.apply_permission_change(
        mock_config, changed_keys={"control_volume", "query_library"}
    )

    runtime.stop.assert_not_awaited()
    runtime.start.assert_not_awaited()
    # The preset tag must be gone (default has edit_queue=False) and the
    # default-enabled query:library tag must be present (default has
    # query_library=True). Both checks are necessary to prove the rebuild
    # actually rebuilt from new_config rather than no-op'd or appended.
    assert "edit:queue" not in runtime._allowed_tags, (
        "preset edit:queue tag was not removed — hot-swap did not rebuild from new_config"
    )
    assert "query:library" in runtime._allowed_tags, (
        "default-enabled query:library tag missing — hot-swap rebuild lost defaults"
    )


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
