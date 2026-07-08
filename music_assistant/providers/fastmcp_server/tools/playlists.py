"""Playlists: create, modify, delete."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..models import PlaylistBrief
from ..tags import Tag
from ._common import TIMEOUT_BULK, TIMEOUT_MUTATION, confirm_or_raise, to_brief_playlist

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


_PLAYLIST_URI_RE = re.compile(r"^library://playlist/(\d+)$")


def _coerce_playlist_db_id(value: str | int) -> int:
    """
    Normalise a caller-supplied playlist identifier to MA's integer library id.

    Music Assistant's playlist controller calls ``int()`` on the id internally,
    so it accepts only the integer library id — not the provider-scoped URI
    that every other tool consumes. This helper bridges the gap so callers can
    pass either form.

    :param value: The integer library id, an all-digit string, or a
        ``library://playlist/<n>`` URI as returned by ``list_library_playlists`` /
        ``library_get_playlist_by_uri``.
    """
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    match = _PLAYLIST_URI_RE.match(s)
    if match:
        return int(match.group(1))
    raise ToolError(
        f"Invalid playlist_id {value!r}: pass the integer library id or a "
        f"library://playlist/<n> URI (provider-scoped URIs from other "
        f"providers are not supported here)."
    )


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
        """
        Create a new empty playlist on a music provider.

        Returns the new ``PlaylistBrief``. Pass ``PlaylistBrief.uri`` to
        ``add_track`` / ``add_tracks`` to populate it.

        :param name: Display name for the playlist.
        :param provider_instance_id: Music-provider instance to host the
            playlist (e.g. ``spotify--<account>``); omit to let Music
            Assistant pick the default writable provider.
        """
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
        """
        Append one track to a playlist.

        Use ``add_tracks`` for multiple tracks in a single call. Returns
        nothing.

        :param playlist_id: Either the integer library id or the
            ``library://playlist/<n>`` URI of the destination playlist.
        :param track_uri: Music Assistant URI of the track to append (e.g.
            ``TrackBrief.uri``).
        """
        await mass.music.playlists.add_playlist_track(
            _coerce_playlist_db_id(playlist_id), track_uri
        )

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
        """
        Append multiple tracks to a playlist in one call. Returns nothing.

        **Not atomic at any size.** Tracks are added one at a time with
        progress reporting; on failure or cancellation, tracks added so far
        remain in the playlist with no rollback.

        :param playlist_id: Either the integer library id or the
            ``library://playlist/<n>`` URI of the destination playlist.
        :param track_uris: List of Music Assistant track URIs to append, in
            order.
        """
        db_id = _coerce_playlist_db_id(playlist_id)
        total = len(track_uris)
        added = 0
        try:
            for i, uri in enumerate(track_uris, start=1):
                await mass.music.playlists.add_playlist_track(db_id, uri)
                added = i
                if ctx is not None:
                    await ctx.report_progress(progress=i, total=total)
        except BaseException:
            # Surface partial state to the client before re-raising. BaseException
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
        """
        Remove tracks from a playlist by zero-based position.

        All requested positions are removed in a single call — pass them
        together rather than one at a time, since removals shift the
        positions of all later items. When ``Confirm destructive
        operations`` is enabled the client is asked to confirm first.
        Returns nothing.

        :param playlist_id: Either the integer library id or the
            ``library://playlist/<n>`` URI of the playlist to modify.
        :param positions: Zero-based positions of tracks to remove.
        """
        await confirm_or_raise(
            ctx,
            f"Remove {len(positions)} track(s) from playlist {playlist_id!r}?",
            enabled=require_confirmation,
        )
        # MA's PlaylistController expects an immutable tuple, not a list, so
        # callers can't accidentally mutate it mid-removal.
        await mass.music.playlists.remove_playlist_tracks(
            _coerce_playlist_db_id(playlist_id), tuple(positions)
        )

    return sub
