"""Tests for ``MCPServerRuntime.apply_permission_change`` hot-swap vs restart routing.

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
    """A ``res_*`` toggle must restart the runtime (resources are bound at start time).

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
async def test_empty_changed_keys_does_not_restart(
    mock_mass: MagicMock, mock_config: MagicMock
) -> None:
    """A no-op call (``changed_keys=set()``) must not force a restart.

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
    """A permission-key-only change updates ``_allowed_tags`` in place — no restart."""
    from music_assistant.providers.fastmcp_server.server import MCPServerRuntime  # noqa: PLC0415

    runtime = MCPServerRuntime(mock_mass, mock_config, logging.getLogger("t"))
    # Pretend the runtime has started so _allowed_tags exists and hot-swap is viable.
    runtime._allowed_tags = {"query:library"}
    runtime.stop = AsyncMock()
    runtime.start = AsyncMock()

    await runtime.apply_permission_change(
        mock_config, changed_keys={"control_volume", "query_library"}
    )

    runtime.stop.assert_not_awaited()
    runtime.start.assert_not_awaited()
    # _allowed_tags rebuilt from new_config (default: 4 query tags enabled).
    assert "query:library" in runtime._allowed_tags
