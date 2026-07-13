"""Metadata: lyrics, recommendations, similar tracks, refresh."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from music_assistant_models.enums import MediaType

from ..models import RecommendationFolderBrief, TrackBrief
from ..tags import Tag
from ._common import TIMEOUT_QUERY, page_args, resolve_typed_uri, to_brief_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _readonly(title: str) -> ToolAnnotations:
    """Read-only metadata tool annotations with the supplied UI title."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def build_metadata_server(mass: MusicAssistant) -> FastMCP:
    """Construct the ``metadata/*`` sub-server."""
    sub: FastMCP = FastMCP(name="metadata")

    @sub.tool(
        tags={Tag.QUERY_METADATA},
        annotations=ToolAnnotations(
            title="Recommendations",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def recommendations(
        ctx: Context | None = None,
    ) -> list[RecommendationFolderBrief]:
        """
        Return Music Assistant's curated recommendation folders.

        Each ``RecommendationFolderBrief`` has a ``name`` and a list of
        ``item_uris`` that can be passed to ``play_media`` or to
        ``get_track_by_uri`` to inspect further.
        """
        if ctx is not None:
            await ctx.info("Fetching MA curated recommendations…")
        folders = await mass.music.recommendations.get_recommendations()
        result: list[RecommendationFolderBrief] = []
        for folder in folders:
            folder_items = getattr(folder, "items", None) or []
            result.append(
                RecommendationFolderBrief(
                    name=str(getattr(folder, "name", "")),
                    item_uris=[str(getattr(it, "uri", "")) for it in folder_items],
                )
            )
        return result

    @sub.tool(
        tags={Tag.QUERY_METADATA},
        annotations=ToolAnnotations(
            title="Recently played tracks",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def recently_played(limit: int = 10) -> list[TrackBrief]:
        """
        Return the user's most recently played tracks, newest first.

        Returns ``TrackBrief`` items. Items without a resolved name are
        filtered out.

        :param limit: Max results to return (clamped to ``[1, 200]``).
        """
        _, limit = page_args(0, limit)
        items = await mass.music.recently_played(limit=limit)
        return [to_brief_track(it) for it in items if getattr(it, "name", None)]

    @sub.tool(
        tags={Tag.QUERY_METADATA},
        annotations=_readonly("Get lyrics"),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_lyrics(track_uri: str) -> str | None:
        """
        Return lyrics for a track on a best-effort basis.

        Returns ``None`` if lyrics are not available for the track. Raises
        ``ToolError`` if the URI resolves to a non-track — without that
        guard, querying ``metadata.lyrics`` on an album / playlist would
        silently return ``None`` and the type confusion would never
        surface to the caller.

        :param track_uri: Music Assistant track URI (e.g. as found on
            ``TrackBrief.uri``).
        """
        item = await resolve_typed_uri(
            mass,
            track_uri,
            MediaType.TRACK,
            type_label="track",
            hint="lyrics only apply to tracks.",
        )
        metadata = getattr(item, "metadata", None)
        lyrics = getattr(metadata, "lyrics", None) if metadata else None
        return str(lyrics) if lyrics else None

    return sub
