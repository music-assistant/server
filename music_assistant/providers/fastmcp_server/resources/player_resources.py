"""URI-addressable read-only player and queue resources."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..tags import Tag
from ..tools._common import to_brief_player, to_brief_queue

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def register_player_resources(mcp: Any, mass: MusicAssistant) -> None:
    """Register ``player://`` and ``queue://`` resources on the given FastMCP root."""

    @mcp.resource("player://{player_id}", tags={Tag.QUERY_PLAYERS})  # type: ignore[untyped-decorator, unused-ignore]
    async def player_resource(player_id: str) -> Any:
        """Player snapshot by id."""
        player = mass.players.get_player(player_id)
        return to_brief_player(player) if player is not None else None

    @mcp.resource("queue://{queue_id}", tags={Tag.QUERY_QUEUE})  # type: ignore[untyped-decorator, unused-ignore]
    async def queue_resource(queue_id: str) -> Any:
        """Queue snapshot by id (up to 500 items — MA's default page size)."""
        queue = mass.player_queues.get(queue_id)
        if queue is None:
            return None
        items = mass.player_queues.items(queue_id, limit=500)
        return to_brief_queue(queue, items=list(items))
