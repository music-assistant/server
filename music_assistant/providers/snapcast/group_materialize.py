"""Helpers for proactively materializing MA sync groups into live Snapcast state."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.sync_group.constants import SGP_PREFIX

if TYPE_CHECKING:
    from .ma_stream import SnapcastMAStream
    from .provider import SnapCastProvider
    from .snap_cntrl_proto import SnapgroupProto


@dataclass(slots=True)
class SnapcastGroupMaterializeTarget:
    """Desired native Snapcast state for one MA sync group."""

    sync_group_player_id: str
    sync_group_name: str
    sync_leader_id: str
    member_player_ids: list[str]


class SnapcastGroupMaterializer:
    """Materialize MA sync-group runtime state into native Snapcast groups/streams."""

    def __init__(self, provider: SnapCastProvider) -> None:
        """Initialize the materializer."""
        self._provider = provider
        self._logger = provider.logger.getChild("group_materialize")

    async def materialize(self, sync_group_player_id: str) -> None:
        """Materialize one MA sync-group into native Snapcast state."""
        if self._provider.mass.closing:
            return

        sync_group_player = self._provider.mass.players.get_player(
            sync_group_player_id, raise_unavailable=False
        )
        if sync_group_player is None or not sync_group_player_id.startswith(SGP_PREFIX):
            return

        if getattr(sync_group_player, "playback_state", None) == PlaybackState.PLAYING:
            self._logger.debug(
                "Skipping pre-materialization for sync group '%s' while it is actively playing",
                getattr(getattr(sync_group_player, "config", None), "name", sync_group_player_id),
            )
            return

        target = self._collect_target(sync_group_player)
        if target is None:
            await self._cleanup_target(sync_group_player)
            return

        idle_stream = await self._provider.ensure_sync_group_idle_stream(sync_group_player)
        if idle_stream is None or idle_stream.stream_id is None:
            return

        await self._materialize_target(sync_group_player, target, idle_stream)

    async def _cleanup_target(self, sync_group_player: Any) -> None:
        """Remove previously materialized idle Snapcast state for an empty sync group."""
        idle_stream = self._provider.get_snap_ma_stream(sync_group_player.player_id)
        if idle_stream is None or idle_stream.queue_id is not None:
            return

        if idle_stream.stream_id is not None:
            for snap_group in self._provider._snapserver.groups:
                if getattr(snap_group, "stream", None) != idle_stream.stream_id:
                    continue
                for snap_client_id in getattr(snap_group, "clients", []):
                    with suppress(AssertionError, KeyError, ValueError):
                        player_id = self._provider._get_ma_id(snap_client_id)
                        await self._provider.move_player_to_fallback_group(player_id)

        await self._provider.delete_ma_stream(idle_stream.stream_name)
        self._logger.info(
            "Removed idle Snapcast stream '%s' for empty MA sync group '%s'",
            idle_stream.stream_display_name,
            getattr(
                getattr(sync_group_player, "config", None),
                "name",
                sync_group_player.player_id,
            ),
        )
        self._provider._update_group_callbacks(poke=True)

    def _collect_target(self, sync_group_player: Any) -> SnapcastGroupMaterializeTarget | None:
        """Collect the desired native Snapcast target state for a sync group."""
        member_player_ids = list(
            dict.fromkeys(
                member_id
                for member_id in getattr(sync_group_player, "group_members", [])
                if member_id != getattr(sync_group_player, "player_id", "")
                and self._provider.get_snap_client(player_id=member_id) is not None
            )
        )
        if not member_player_ids:
            return None

        leader_player_id = self._resolve_sync_leader_id(sync_group_player, member_player_ids)
        if leader_player_id is None:
            return None

        ordered_member_ids = [
            leader_player_id,
            *[member_id for member_id in member_player_ids if member_id != leader_player_id],
        ]
        return SnapcastGroupMaterializeTarget(
            sync_group_player_id=sync_group_player.player_id,
            sync_group_name=getattr(getattr(sync_group_player, "config", None), "name", ""),
            sync_leader_id=leader_player_id,
            member_player_ids=ordered_member_ids,
        )

    def _resolve_sync_leader_id(
        self, sync_group_player: Any, member_player_ids: list[str]
    ) -> str | None:
        """Resolve a deterministic Snapcast leader for the sync group."""
        current_leader = cast(
            "str | None",
            getattr(getattr(sync_group_player, "sync_leader", None), "player_id", None),
        )
        if current_leader in member_player_ids:
            return current_leader
        return member_player_ids[0] if member_player_ids else None

    async def _materialize_target(
        self,
        sync_group_player: Any,
        target: SnapcastGroupMaterializeTarget,
        idle_stream: SnapcastMAStream,
    ) -> None:
        """Apply the desired native Snapcast target state."""
        stream_ref = self._provider._get_stable_stream_reference(idle_stream.stream_id)
        leader_group = await self._provider.ensure_player_owned_group(
            target.sync_leader_id, set_stream_id=stream_ref
        )
        if leader_group is None:
            return

        leader_group.set_callback(None)
        await self._move_stale_stream_members_to_fallback(
            target,
            idle_stream.stream_id,
            exclude_group_id=getattr(leader_group, "identifier", None),
        )
        current_member_ids = self._resolve_group_member_player_ids(leader_group)
        changes_applied = False

        for member_player_id in current_member_ids:
            if member_player_id == target.sync_leader_id:
                continue
            if member_player_id in target.member_player_ids:
                continue
            moved_to_fallback = await self._provider.move_player_to_fallback_group(member_player_id)
            if not moved_to_fallback:
                await self._provider.isolate_player_to_dedicated_group(
                    member_player_id, target_stream_id=stream_ref
                )
            changes_applied = True

        for member_player_id in target.member_player_ids:
            if member_player_id == target.sync_leader_id:
                continue
            if member_player_id in current_member_ids:
                continue
            with suppress(AssertionError, KeyError, ValueError):
                await leader_group.add_client(self._provider._get_snapclient_id(member_player_id))
                changes_applied = True

        if getattr(leader_group, "stream", None) != idle_stream.stream_id:
            await leader_group.set_stream(stream_ref)
            changes_applied = True

        leader_changed = False
        if leader_player := self._provider.mass.players.get_player(
            target.sync_leader_id, raise_unavailable=False
        ):
            if getattr(getattr(sync_group_player, "sync_leader", None), "player_id", None) != (
                leader_player.player_id
            ):
                sync_group_player_any = cast("Any", sync_group_player)
                sync_group_player_any.sync_leader = leader_player
                leader_changed = True

        if not changes_applied and not leader_changed:
            return

        self._logger.info(
            "Materialized MA sync group '%s' to native Snapcast leader=%s members=%s stream=%s",
            target.sync_group_name or target.sync_group_player_id,
            target.sync_leader_id,
            target.member_player_ids,
            idle_stream.stream_display_name,
        )
        self._provider.mass.players.trigger_player_update(
            target.sync_group_player_id, force_update=True, debounce_delay=0
        )
        self._provider._update_group_callbacks(poke=True)

    async def _move_stale_stream_members_to_fallback(
        self,
        target: SnapcastGroupMaterializeTarget,
        stream_id: str | None,
        exclude_group_id: str | None,
    ) -> None:
        """Move stale clients on the same sync-group stream back to the fallback group."""
        if stream_id is None:
            return

        snapserver = getattr(self._provider, "_snapserver", None)
        if snapserver is None:
            return

        for snap_group in snapserver.groups:
            if getattr(snap_group, "identifier", None) == exclude_group_id:
                continue
            if getattr(snap_group, "stream", None) != stream_id:
                continue
            for snap_client_id in getattr(snap_group, "clients", []):
                player_id: str | None = None
                with suppress(AssertionError, KeyError, ValueError):
                    player_id = self._provider._get_ma_id(snap_client_id)
                if not player_id:
                    continue
                if player_id in target.member_player_ids:
                    continue
                await self._provider.move_player_to_fallback_group(player_id)

    def _resolve_group_member_player_ids(self, snap_group: SnapgroupProto) -> list[str]:
        """Translate a live Snapcast group's client ids to MA player ids."""
        member_player_ids: list[str] = []
        for snap_client_id in getattr(snap_group, "clients", []):
            with suppress(AssertionError, KeyError, ValueError):
                member_player_ids.append(self._provider._get_ma_id(snap_client_id))
        return member_player_ids
