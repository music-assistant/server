"""Playback: play, pause, seek, skip, play media."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..tags import Tag
from ._common import TIMEOUT_MUTATION

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _control_annotations(*, title: str, idempotent: bool = False) -> ToolAnnotations:
    """Default annotations for transport-control tools (mutate but non-destructive)."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def build_playback_server(mass: MusicAssistant) -> FastMCP:
    """Construct the ``playback/*`` sub-server."""
    sub: FastMCP = FastMCP(name="playback")

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Toggle play / pause"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def play_pause(queue_id: str) -> None:
        """Toggle play/pause on the given queue."""
        await mass.player_queues.play_pause(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Stop playback", idempotent=True),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def stop(queue_id: str) -> None:
        """Stop playback on the given queue."""
        await mass.player_queues.stop(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Next track"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def next_track(queue_id: str) -> None:
        """Advance to the next track."""
        await mass.player_queues.next(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Previous track"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def previous_track(queue_id: str) -> None:
        """Return to the previous track."""
        await mass.player_queues.previous(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Skip by seconds"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def skip(queue_id: str, seconds: int = 10) -> None:
        """Skip forward by ``seconds`` (or backward when negative)."""
        await mass.player_queues.skip(queue_id, seconds)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Seek to position"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def seek(queue_id: str, position: int) -> None:
        """Seek to absolute position (seconds) in the current track."""
        await mass.player_queues.seek(queue_id, position)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Play media on a queue"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def play_media(
        queue_id: str,
        uri: str,
        radio_mode: bool = False,
    ) -> None:
        """Play media on the given queue by MA URI.

        :param queue_id: queue to play on (typically the player_id).
        :param uri: MA URI of the media to play (artist, album, track, playlist, radio).
        :param radio_mode: when ``True``, MA fills the queue with similar items.
        """
        await mass.player_queues.play_media(queue_id, uri, radio_mode=radio_mode)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Play queue item at index"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def play_index(queue_id: str, index: int) -> None:
        """Play the queue item at the given zero-based index."""
        await mass.player_queues.play_index(queue_id, index)

    return sub
