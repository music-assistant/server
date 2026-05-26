"""Playlists: create, modify, delete."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from ..models import PlaylistBrief
from ..tags import Tag
from ._common import TIMEOUT_BULK, TIMEOUT_MUTATION, confirm_or_raise, to_brief_playlist

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def build_playlists_server(mass: MusicAssistant, *, require_confirmation: bool = True) -> FastMCP:
    """Construct the ``playlists/*`` sub-server."""
    sub: FastMCP = FastMCP(name="playlists")

    @sub.tool(
        tags={Tag.EDIT_PLAYLISTS},
        annotations=ToolAnnotations(
            title="Create a playlist",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def create_playlist(name: str, provider_instance_id: str | None = None) -> PlaylistBrief:
        """Create a new playlist on a music provider."""
        playlist = await mass.music.playlists.create_playlist(
            name, provider_instance_or_domain=provider_instance_id
        )
        return to_brief_playlist(playlist)

    @sub.tool(
        tags={Tag.EDIT_PLAYLISTS},
        annotations=ToolAnnotations(
            title="Add a single track to a playlist",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def add_track(playlist_id: str | int, track_uri: str) -> None:
        """Append one track to a playlist."""
        await mass.music.playlists.add_playlist_track(playlist_id, track_uri)

    @sub.tool(
        tags={Tag.EDIT_PLAYLISTS},
        annotations=ToolAnnotations(
            title="Add tracks to a playlist",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_BULK,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def add_tracks(
        playlist_id: str | int,
        track_uris: list[str],
        ctx: Context | None = None,
    ) -> None:
        """Append multiple tracks to a playlist.

        For batches up to 10 the call is bulk-dispatched (one round-trip);
        beyond that, items are added one-by-one with progress reporting so
        the LLM client can show a meaningful spinner / cancellation handle.

        .. warning::

            The per-item path is **not transactional**. If the client cancels
            (``notifications/cancelled``) or MA raises on the N-th track,
            tracks 0..N-1 stay added — there is no rollback. Callers that need
            atomic semantics should keep batches at ``<= 10`` so the bulk
            ``add_playlist_tracks`` round-trip is used.
        """
        total = len(track_uris)
        if total <= 10:
            await mass.music.playlists.add_playlist_tracks(playlist_id, track_uris)
            return
        added = 0
        try:
            for i, uri in enumerate(track_uris, start=1):
                await mass.music.playlists.add_playlist_track(playlist_id, uri)
                added = i
                if ctx is not None:
                    await ctx.report_progress(progress=i, total=total)
        except BaseException:
            # Surface partial-state to the client before re-raising. BaseException
            # also catches asyncio.CancelledError, which we want to flag.
            if ctx is not None and added < total:
                with contextlib.suppress(Exception):
                    await ctx.warning(
                        f"add_tracks: partial state — {added} of {total} tracks "
                        f"added to playlist {playlist_id!r} before failure / cancel"
                    )
            raise

    @sub.tool(
        tags={Tag.DELETE_PLAYLISTS},
        annotations=ToolAnnotations(
            title="Remove tracks from a playlist",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def remove_tracks(
        playlist_id: str | int,
        positions: list[int],
        ctx: Context | None = None,
    ) -> None:
        """Remove tracks at the given zero-based positions from a playlist."""
        await confirm_or_raise(
            ctx,
            f"Remove {len(positions)} track(s) from playlist {playlist_id!r}?",
            enabled=require_confirmation,
        )
        # MA's PlaylistController expects an immutable tuple, not a list, so
        # callers can't accidentally mutate it mid-removal.
        await mass.music.playlists.remove_playlist_tracks(playlist_id, tuple(positions))

    return sub
