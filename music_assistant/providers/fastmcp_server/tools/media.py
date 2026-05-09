"""Media: favorites, library add/remove, announcements."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..tags import Tag
from ._common import TIMEOUT_MUTATION, confirm_or_raise

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def _resolve_uri(mass: MusicAssistant, uri: str) -> Any:
    """Look up a MediaItem by MA URI, raising ToolError when missing.

    MA's MusicController APIs that mutate library / favorites / play history
    expect a resolved (media_type, library_item_id) pair or a typed media
    object — not a raw URI string. This helper centralises the lookup.
    """
    # MA's ``get_item_by_uri`` is typed as returning a MediaItem (no Optional);
    # missing entries raise instead. Normalise to a ToolError for a consistent
    # tool-surface error path.
    try:
        return await mass.music.get_item_by_uri(uri)
    except Exception as exc:
        msg = f"Item not found for URI: {uri!r} ({exc})"
        raise ToolError(msg) from exc


def build_media_server(mass: MusicAssistant, *, require_confirmation: bool = True) -> FastMCP:
    """Construct the ``media/*`` sub-server."""
    sub: FastMCP = FastMCP(name="media")

    @sub.tool(
        tags={Tag.EDIT_FAVORITES},
        annotations=ToolAnnotations(
            title="Add to favorites",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def add_to_favorites(uri: str) -> None:
        """Add a media item (by URI) to favorites."""
        item = await _resolve_uri(mass, uri)
        await mass.music.add_item_to_favorites(item)

    @sub.tool(
        tags={Tag.DELETE_FAVORITES},
        annotations=ToolAnnotations(
            title="Remove from favorites",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def remove_from_favorites(uri: str, ctx: Context | None = None) -> None:
        """Remove a media item (by URI) from favorites."""
        await confirm_or_raise(
            ctx,
            f"Remove {uri!r} from favorites?",
            enabled=require_confirmation,
        )
        item = await _resolve_uri(mass, uri)
        await mass.music.remove_item_from_favorites(item.media_type, int(item.item_id))

    @sub.tool(
        tags={Tag.EDIT_LIBRARY},
        annotations=ToolAnnotations(
            title="Add to library",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def add_to_library(uri: str) -> None:
        """Add a media item (by URI) to the library."""
        item = await _resolve_uri(mass, uri)
        await mass.music.add_item_to_library(item)

    @sub.tool(
        tags={Tag.DELETE_LIBRARY},
        annotations=ToolAnnotations(
            title="Remove from library",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def remove_from_library(uri: str, ctx: Context | None = None) -> None:
        """Remove a media item (by URI) from the library."""
        await confirm_or_raise(
            ctx,
            f"Remove {uri!r} from the library? This cannot be undone.",
            enabled=require_confirmation,
        )
        item = await _resolve_uri(mass, uri)
        await mass.music.remove_item_from_library(item.media_type, int(item.item_id))

    @sub.tool(
        tags={Tag.CONTROL_MEDIA},
        annotations=ToolAnnotations(
            title="Mark item played",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def mark_played(uri: str) -> None:
        """Mark a media item as played (updates play history)."""
        item = await _resolve_uri(mass, uri)
        await mass.music.mark_item_played(item)

    @sub.tool(
        tags={Tag.CONTROL_MEDIA},
        annotations=ToolAnnotations(
            title="Play announcement",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def play_announcement(player_id: str, url: str, volume_level: int | None = None) -> None:
        """Play a one-shot announcement audio URL on a player."""
        await mass.players.play_announcement(player_id, url, volume_level=volume_level)

    return sub
