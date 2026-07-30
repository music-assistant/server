"""Playback: play, pause, seek, skip, play media."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.radio_playlist import radio_playlist_uri

from ..tags import Tag
from ._common import TIMEOUT_MUTATION, TIMEOUT_QUERY

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _control_annotations(*, title: str, idempotent: bool = False) -> ToolAnnotations:
    """Build default annotations for transport-control tools (mutate but non-destructive)."""
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
        annotations=_control_annotations(title="Pause playback"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def pause(queue_id: str) -> None:
        """
        Pause playback on the given queue.

        Always pauses — unlike ``playback_play_pause``, this does not toggle. Prefer this
        when the user asks to pause. Returns nothing.

        :param queue_id: Queue identifier — same as ``player_id`` from
            ``players_list_players``.
        """
        await mass.player_queues.pause(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Resume playback"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def resume(queue_id: str) -> None:
        """
        Resume paused playback on the given queue.

        Always resumes — unlike ``playback_play_pause``, this does not toggle. Prefer this
        when the user asks to resume or unpause. Returns nothing.

        :param queue_id: Queue identifier — same as ``player_id`` from
            ``players_list_players``.
        """
        await mass.player_queues.resume(queue_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Toggle play / pause"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def play_pause(queue_id: str) -> None:
        """
        Toggle play/pause on the given queue.

        Playing → pauses, paused → resumes. Prefer ``playback_pause`` or
        ``playback_resume`` when the user gives an explicit instruction —
        toggling can flip the wrong way if state is unknown. Use
        ``playback_stop`` to halt and reset position. Returns
        nothing.

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

        Use ``playback_play_pause`` to resume without losing position. Returns nothing.

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
        mode. Use ``playback_play_index`` to jump to a specific position. Returns
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
        radio: bool = False,
    ) -> None:
        """
        Load and start playing an artist, album, track, playlist, or radio station on a queue.

        Replaces whatever the queue was playing. Use ``playback_play_index`` to start an
        item that is already in the queue. Returns nothing.

        :param queue_id: Queue identifier — for a player queue this is the same
            ``player_id`` returned by ``players_list_players``.
        :param uri: Music Assistant URI of the artist, album, track, playlist
            or radio station to play, of the form
            ``<provider>://<media_type>/<id>`` (e.g. as found on
            ``TrackBrief.uri`` / ``AlbumBrief.uri`` / ...).
        :param radio: When ``True``, play an endless "radio" seeded from ``uri``
            instead of just the item itself. Music Assistant builds a dynamic
            playlist from the seed (artist, album, track, playlist or genre),
            mixing in its own tracks and continuously refilling the queue with
            similar tracks.
        """
        if radio:
            try:
                seed = await mass.music.get_item_by_uri(uri)
            except MusicAssistantError as err:
                msg = f"Could not resolve URI for radio: {uri!r} ({err})"
                raise ToolError(msg) from err
            if isinstance(seed, BrowseFolder):
                msg = f"Cannot start a radio from a browse folder: {uri!r}"
                raise ToolError(msg)
            uri = radio_playlist_uri(seed)
        await mass.player_queues.play_media(queue_id, uri)

    @sub.tool(
        tags={Tag.CONTROL_PLAYBACK},
        annotations=_control_annotations(title="Play queue item at index"),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def play_index(queue_id: str, index: int) -> None:
        """
        Start playing the item at the given position in the existing queue.

        Does not load new media — use ``playback_play_media`` for that. Returns nothing.

        :param queue_id: Queue identifier from ``QueueBrief.queue_id``.
        :param index: Zero-based position in the queue (``>= 0``).
        """
        await mass.player_queues.play_index(queue_id, index)

    return sub
