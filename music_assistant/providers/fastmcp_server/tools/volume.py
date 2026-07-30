"""Volume control: set, up/down, mute, group volume."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..tags import Tag
from ._common import TIMEOUT_FAST

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def _vol_annotations(*, title: str, idempotent: bool) -> ToolAnnotations:
    """Build default volume-tool annotations: never destructive, never open-world."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def build_volume_server(mass: MusicAssistant) -> FastMCP:
    """Construct the ``volume/*`` sub-server."""
    sub: FastMCP = FastMCP(name="volume")

    @sub.tool(
        tags={Tag.CONTROL_VOLUME},
        annotations=_vol_annotations(title="Set volume", idempotent=True),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def volume_set(player_id: str, level: int) -> None:
        """
        Set absolute volume on a single player.

        Use ``volume_up`` / ``volume_down`` for relative steps and
        ``group_volume_set`` for a sync group. Returns nothing.

        :param player_id: Player identifier from ``PlayerBrief.player_id``.
        :param level: Target volume ``0`` (silent) to ``100`` (max);
            out-of-range values are clamped.
        """
        await mass.players.cmd_volume_set(player_id, max(0, min(100, int(level))))

    @sub.tool(
        tags={Tag.CONTROL_VOLUME},
        annotations=_vol_annotations(title="Volume up", idempotent=False),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def volume_up(player_id: str) -> None:
        """
        Increase volume by one device-defined step (typically ``5``).

        Use ``volume_set`` instead when a precise target level is needed.
        Returns nothing.

        :param player_id: Player identifier from ``PlayerBrief.player_id``.
        """
        await mass.players.cmd_volume_up(player_id)

    @sub.tool(
        tags={Tag.CONTROL_VOLUME},
        annotations=_vol_annotations(title="Volume down", idempotent=False),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def volume_down(player_id: str) -> None:
        """
        Decrease volume by one device-defined step (typically ``5``).

        Use ``volume_set`` instead when a precise target level is needed.
        Returns nothing.

        :param player_id: Player identifier from ``PlayerBrief.player_id``.
        """
        await mass.players.cmd_volume_down(player_id)

    @sub.tool(
        tags={Tag.CONTROL_VOLUME},
        annotations=_vol_annotations(title="Mute / unmute", idempotent=True),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def volume_mute(player_id: str, muted: bool) -> None:
        """
        Mute or unmute a player.

        Mute is a latch — it does not change the underlying volume level, so
        unmuting restores the original level. Use ``volume_set(level=0)``
        instead if you want the volume itself to be ``0`` across a
        mute/unmute cycle. Returns nothing.

        :param player_id: Player identifier from ``PlayerBrief.player_id``.
        :param muted: ``True`` to mute, ``False`` to unmute.
        """
        await mass.players.cmd_volume_mute(player_id, muted)

    @sub.tool(
        tags={Tag.CONTROL_VOLUME},
        annotations=_vol_annotations(title="Set group volume", idempotent=True),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def group_volume_set(player_id: str, level: int) -> None:
        """
        Set the overall volume of a sync group.

        Use ``volume_set`` for an individual (non-group) player. Returns
        nothing.

        :param player_id: Player identifier of the sync-group leader (the
            ``PlayerBrief`` entry whose group membership identifies it as
            the leader of the group to control).
        :param level: Target volume ``0`` (silent) to ``100`` (max);
            out-of-range values are clamped.
        """
        await mass.players.cmd_group_volume(player_id, max(0, min(100, int(level))))

    return sub
