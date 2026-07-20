"""Tests for the library list_* tools' calls into the music controllers."""
# mypy: disable-error-code="arg-type, no-untyped-def, type-arg, assignment, operator, misc, union-attr"

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP

from music_assistant.providers.fastmcp_server.tools.library import build_library_server


@pytest.fixture
def library_server(mock_mass: Any) -> FastMCP:
    """Mount only the library sub-server."""
    mcp: FastMCP = FastMCP(name="t")
    mcp.mount(build_library_server(mock_mass), namespace="library")
    return mcp


class TestListToolsRequestFullItems:
    """
    The list tools must pin ``summary=False`` on ``library_items``.

    Since upstream music-assistant/server#4693 the controllers default to
    slim summary items; the pin keeps full models flowing into the
    ``to_brief_*`` mappers even if future summary slimming drops fields
    the Brief payloads rely on.
    """

    @pytest.mark.parametrize(
        ("tool_name", "controller"),
        [
            ("library_list_library_tracks", "tracks"),
            ("library_list_library_albums", "albums"),
            ("library_list_library_artists", "artists"),
            ("library_list_library_playlists", "playlists"),
            ("library_list_library_radio", "radio"),
        ],
    )
    async def test_list_tool_pins_summary_false(
        self,
        library_server: FastMCP,
        mock_mass: Any,
        tool_name: str,
        controller: str,
    ) -> None:
        """Each list tool passes paging args and ``summary=False``."""
        async with Client(library_server) as client:
            await client.call_tool(tool_name, {"offset": 10, "limit": 25})
        library_items = getattr(mock_mass.music, controller).library_items
        library_items.assert_awaited_once_with(limit=25, offset=10, summary=False)
