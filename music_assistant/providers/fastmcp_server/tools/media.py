"""Media: favorites, library add/remove, announcements."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..tags import Tag
from ._common import TIMEOUT_MUTATION, TIMEOUT_QUERY, confirm_or_raise, resolve_uri

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def _resolve_to_library_item(mass: MusicAssistant, uri: str) -> Any:
    """
    Resolve a URI to its library counterpart, raising ToolError when not in library.

    MA's :meth:`MusicController.remove_item_from_favorites` and
    :meth:`remove_item_from_library` expect a library item id. When the
    caller passes a provider-native URI (e.g. ``yandex_music://track/abc``),
    :func:`resolve_uri` returns a MediaItem with the provider's id —
    feeding that into the controller silently targets the wrong row (or
    fails on ``int(...)``). This helper looks up the library counterpart
    via :meth:`get_library_item_by_prov_id` and raises if the item isn't
    in the library.
    """
    item = await resolve_uri(mass, uri)
    if getattr(item, "provider", None) == "library":
        return item
    lib_item = await mass.music.get_library_item_by_prov_id(
        item.media_type, item.item_id, item.provider
    )
    if lib_item is None:
        msg = f"URI {uri!r} is not in the library"
        raise ToolError(msg)
    return lib_item


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
        """
        Mark a media item as a favorite.

        Does not add the item to the library — use ``add_to_library`` for
        that. Safe to call on an already-favorited item. Returns nothing.

        :param uri: Music Assistant URI of the artist, album, track,
            playlist or radio station (e.g. as found on ``TrackBrief.uri``).
        """
        item = await resolve_uri(mass, uri)
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
        """
        Remove a media item from favorites.

        The item must already be in the library. Does not remove the item
        from the library itself — use ``remove_from_library`` for that.
        When ``Confirm destructive operations`` is enabled the client is
        asked to confirm first. Returns nothing.

        :param uri: Music Assistant URI of the item to unfavorite.
        """
        await confirm_or_raise(
            ctx,
            f"Remove {uri!r} from favorites?",
            enabled=require_confirmation,
        )
        item = await _resolve_to_library_item(mass, uri)
        await mass.music.remove_item_from_favorites(item.media_type, item.item_id)

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
        """
        Import a media item into the local library.

        Does not mark the item as a favorite — use ``add_to_favorites`` for
        that. Safe to call if the item is already in the library. Returns
        nothing.

        :param uri: Music Assistant URI of the artist, album, track,
            playlist or radio station to import.
        """
        item = await resolve_uri(mass, uri)
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
        """
        Permanently remove a media item from the library. Cannot be undone.

        Implicitly removes any favorite status. When ``Confirm destructive
        operations`` is enabled the client is asked to confirm first.
        Returns nothing.

        :param uri: Music Assistant URI of the item to remove.
        """
        await confirm_or_raise(
            ctx,
            f"Remove {uri!r} from the library? This cannot be undone.",
            enabled=require_confirmation,
        )
        item = await _resolve_to_library_item(mass, uri)
        await mass.music.remove_item_from_library(item.media_type, item.item_id)

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
        """
        Update play history for a media item.

        Increments the item's play count and sets the last-played timestamp.
        Does not play any audio. Returns nothing.

        :param uri: Music Assistant URI of the item to mark as played.
        """
        item = await resolve_uri(mass, uri)
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
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def play_announcement(player_id: str, url: str, volume_level: int | None = None) -> None:
        """
        Interrupt playback to play a one-shot audio clip, then resume.

        Returns nothing.

        :param player_id: Player identifier from ``PlayerBrief.player_id``.
        :param url: HTTP(S) URL of the audio clip — not a Music Assistant
            URI. Non-http(s) schemes (``file://``, ``data:``, …) are
            rejected with a ``ToolError`` before reaching MA.
        :param volume_level: Override volume ``0``-``100`` for the
            announcement only; out-of-range values are clamped. Omit to
            keep the player's current volume.
        """
        scheme = urlsplit(url).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ToolError(f"play_announcement only accepts http(s) URLs, got scheme={scheme!r}")
        if volume_level is not None:
            volume_level = max(0, min(100, int(volume_level)))
        await mass.players.play_announcement(player_id, url, volume_level=volume_level)

    return sub
