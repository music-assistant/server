"""Players: list, inspect, power, group."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..models import PlayerBrief
from ..tags import Tag
from ._common import TIMEOUT_FAST, TIMEOUT_MUTATION, to_brief_player

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def build_players_server(mass: MusicAssistant) -> FastMCP:
    """Construct the ``players/*`` sub-server."""
    sub: FastMCP = FastMCP(name="players")

    @sub.tool(
        tags={Tag.QUERY_PLAYERS},
        annotations=ToolAnnotations(
            title="List players",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def list_players(include_unavailable: bool = False) -> list[PlayerBrief]:
        """
        List players known to Music Assistant.

        Returns ``PlayerBrief`` items with ``player_id``, ``name``, ``state``,
        ``powered``, ``volume_level``, ``available``, ``enabled`` and the
        currently playing item (if any). Players that MA has lost contact
        with are hidden by default — pass ``include_unavailable=True`` to
        get them back, with ``state="unavailable"`` so they are easy to
        distinguish. Does not include queue contents — use the ``queue``
        tools for that.

        :param include_unavailable: When ``True``, include players whose
            ``available`` flag is ``False`` (offline / unreachable
            devices). Defaults to ``False``.
        """
        # Delegate filtering to MA's native ``return_unavailable`` knob rather
        # than re-implementing it in Python — MA short-circuits the build at
        # the controller level and applies the same user-role visibility
        # filters as every other consumer.
        players = mass.players.all_players(return_unavailable=include_unavailable)
        return [to_brief_player(p) for p in players]

    @sub.tool(
        tags={Tag.QUERY_PLAYERS},
        annotations=ToolAnnotations(
            title="Get player by id",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_FAST,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def get_player(player_id: str) -> PlayerBrief | None:
        """
        Return a single player by id, or ``None`` if it doesn't exist.

        Same ``PlayerBrief`` shape as ``list_players``. Prefer this over
        ``list_players`` when the id is already known.

        :param player_id: Player identifier (from ``PlayerBrief.player_id``).
        """
        player = mass.players.get_player(player_id)
        return to_brief_player(player) if player is not None else None

    @sub.tool(
        tags={Tag.CONTROL_PLAYERS},
        annotations=ToolAnnotations(
            title="Power player on / off",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def set_power(player_id: str, powered: bool) -> None:
        """
        Power a player on or off.

        Does not affect sync-group membership — use ``group_player`` to
        change that. Setting the current power state again is a no-op.
        Returns nothing.

        :param player_id: Player identifier from ``PlayerBrief.player_id``.
        :param powered: ``True`` to power on, ``False`` to power off.
        """
        await mass.players.cmd_power(player_id, powered)

    @sub.tool(
        tags={Tag.CONTROL_PLAYERS},
        annotations=ToolAnnotations(
            title="Group player into sync group",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def group_player(player_id: str, target_player_id: str) -> None:
        """
        Add a player to another player's sync group so both play in lockstep.

        Does not change volume — use ``set_group_volume`` on the volume
        sub-server for that. Returns nothing.

        :param player_id: Player to add to the group.
        :param target_player_id: Player whose sync group ``player_id`` joins
            (typically the group leader).
        """
        await mass.players.cmd_group(player_id, target_player_id)

    return sub
