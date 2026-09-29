"""URI-addressable read-only player and queue resources."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..capabilities import Capability
from ..resource_helpers import (
    safe_active_queue,
    to_brief_player,
    to_brief_queue,
    to_resource_text,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def register_player_resources(mcp: Any, mass: MusicAssistant) -> None:
    """Register ``player://`` and ``queue://`` resources on the given FastMCP root."""

    @mcp.resource("player://{player_id}", tags={Capability.QUERY_PLAYERS})  # type: ignore[untyped-decorator, unused-ignore]
    async def player_resource(player_id: str) -> str | None:
        """Player snapshot by id."""
        player = mass.players.get_player(player_id)
        return to_resource_text(
            to_brief_player(player, safe_active_queue(mass, player_id))
            if player is not None
            else None
        )

    @mcp.resource("queue://{queue_id}", tags={Capability.QUERY_QUEUE})  # type: ignore[untyped-decorator, unused-ignore]
    async def queue_resource(queue_id: str) -> str | None:
        """Queue snapshot by id (up to 500 items — MA's default page size)."""
        queue = mass.player_queues.get(queue_id)
        if queue is None:
            return None
        items = mass.player_queues.items(queue_id, limit=500)
        return to_resource_text(to_brief_queue(queue, items=list(items)))
