"""Players: list, inspect, power, group."""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..models import PlayerBrief
from ..tags import Tag
from ._common import TIMEOUT_FAST, TIMEOUT_MUTATION, safe_active_queue, to_brief_player

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
    async def list_players(
        include_unavailable: bool = False,
        include_disabled: bool = False,
    ) -> list[PlayerBrief]:
        """
        List players known to Music Assistant.

        Returns ``PlayerBrief`` items with ``player_id``, ``name``, ``state``,
        ``powered``, ``volume_level``, ``available``, ``enabled``,
        ``needs_setup``, ``active_group``, ``synced_to``, ``external_source``
        and the currently playing item (if any). ``state`` summarises
        usability — values are ``unavailable`` (offline), ``disabled``
        (admin-disabled), ``needs_setup`` (first-run config pending),
        ``synced`` (member of an active sync group; its queue belongs to the
        group leader), or the normal playback states
        (``idle`` / ``playing`` / ``paused`` / ...). When a player is being
        driven by an external "Connect"-style source (e.g. Spotify Connect,
        AirPlay, Yandex Ynison), ``state`` reflects the active queue
        (``playing`` / ``paused``) rather than ``idle``, ``external_source``
        holds the controlling provider instance id, and ``current_item``
        shows the real track title rather than the source wrapper name.
        Offline and admin-disabled players are hidden by default — flip the
        corresponding ``include_*`` flag to get them back. Does not include
        queue contents — use the ``queue`` tools for that.

        :param include_unavailable: When ``True``, include players whose
            ``available`` flag is ``False`` (offline / unreachable
            devices). Defaults to ``False``.
        :param include_disabled: When ``True``, include players that the
            admin has disabled in Music Assistant settings. Defaults to
            ``False`` (matching MA's own ``return_disabled`` default).
        """
        # Delegate filtering to MA's native ``return_unavailable`` /
        # ``return_disabled`` knobs rather than re-implementing them in
        # Python — MA short-circuits the build at the controller level and
        # applies the same user-role visibility filters as every other
        # consumer.
        players = mass.players.all_players(
            return_unavailable=include_unavailable,
            return_disabled=include_disabled,
        )
        return [to_brief_player(p, safe_active_queue(mass, p.player_id)) for p in players]

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
        if player is None:
            return None
        return to_brief_player(player, safe_active_queue(mass, player_id))

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

        Does not affect sync-group membership — use ``group_player`` to add a
        player to a sync group, or ``ungroup_player`` to remove one. Setting
        the current power state again is a no-op. Returns nothing.

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
        sub-server for that. Use ``ungroup_player`` to remove a player from
        a sync group. Returns nothing.

        :param player_id: Player to add to the group.
        :param target_player_id: Player whose sync group ``player_id`` joins
            (typically the group leader).
        """
        await mass.players.cmd_group(player_id, target_player_id)

    @sub.tool(
        tags={Tag.CONTROL_PLAYERS},
        annotations=ToolAnnotations(
            title="Ungroup player from sync group",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        timeout=TIMEOUT_MUTATION,
    )  # type: ignore[untyped-decorator, unused-ignore]
    async def ungroup_player(player_id: str) -> None:
        """
        Remove a player from its sync group so it plays independently again.

        If the player is not currently grouped, this is a no-op. Use
        ``group_player`` to add a player to another player's sync group.
        Returns nothing.

        :param player_id: Player to remove from any active sync group.
        """
        await mass.players.cmd_ungroup(player_id)

    return sub
