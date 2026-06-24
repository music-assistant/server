"""Playback: play, pause, seek, skip, play media."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..tags import Tag
from ._common import TIMEOUT_MUTATION, TIMEOUT_QUERY

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
        """
        Toggle play/pause on the given queue.

        Playing → pauses, paused → resumes. Use ``stop`` to halt playback and
        reset the current position. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        """
        await mass.player_queues.play_pause(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Stop playback", idempotent=True),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def stop(queue_id: str) -> None:
        """
        Stop playback and reset the playback position. The queue is preserved.

        Use ``play_pause`` to resume without losing position. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        """
        await mass.player_queues.stop(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Next track"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def next_track(queue_id: str) -> None:
        """
        Skip to the next item in the queue.

        At the end of the queue the behaviour depends on the current repeat
        mode. Use ``play_index`` to jump to a specific position. Returns
        nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        """
        await mass.player_queues.next(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Previous track"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def previous_track(queue_id: str) -> None:
        """
        Go back in the queue.

        If the current track has been playing past Music Assistant's
        rewind threshold the call restarts the current track instead of
        moving to the previous one — invoke a second time to actually
        step back. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        """
        await mass.player_queues.previous(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Skip by seconds"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def skip(queue_id: str, seconds: int = 10) -> None:
        """
        Skip relative to the current playback position.

        Use ``seek`` for an absolute position. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param seconds: Seconds to skip; negative values skip backward.
            Defaults to ``10``.
        """
        await mass.player_queues.skip(queue_id, seconds)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Seek to position"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def seek(queue_id: str, position: int) -> None:
        """
        Seek to an absolute position within the current track.

        Use ``skip`` for relative offsets. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param position: Seconds from the start of the current track (``>= 0``).
        """
        await mass.player_queues.seek(queue_id, position)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=ToolAnnotations(
            title="Play media on a queue",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_QUERY,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def play_media(
        queue_id: str,
        uri: str,
        radio_mode: bool = False,
    ) -> None:
        """
        Load and start playing media on the given queue.

        Replaces whatever the queue was playing. Use ``play_index`` to start an
        item that is already in the queue. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param uri: Music Assistant URI of the artist, album, track, playlist
            or radio station to play, of the form
            ``<provider>://<media_type>/<id>`` (e.g. as found on
            ``TrackBrief.uri`` / ``AlbumBrief.uri`` / ...).
        :param radio_mode: When ``True``, Music Assistant fills the queue with
            similar items after the requested URI plays.
        """
        await mass.player_queues.play_media(queue_id, uri, radio_mode=radio_mode)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Play queue item at index"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def play_index(queue_id: str, index: int) -> None:
        """
        Start playing the item at the given position in the existing queue.

        Does not load new media — use ``play_media`` for that. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param index: Zero-based position in the queue (``>= 0``).
        """
        await mass.player_queues.play_index(queue_id, index)

    return sub
