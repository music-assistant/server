"""Helpers for restoring live Snapcast groups to MA sync-group runtime state."""

from __future__ import annotations

import urllib.parse
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import PlayerFeature

from music_assistant.providers.sync_group.constants import SGP_PREFIX

if TYPE_CHECKING:
    from .provider import SnapCastProvider
    from .snap_cntrl_proto import SnapgroupProto, SnapstreamProto


@dataclass(slots=True)
class SnapcastGroupRestoreTarget:
    """Resolved restore target for one live Snapcast group."""

    sync_group_player_id: str
    sync_group_name: str
    stream_display_name: str
    sync_leader_id: str
    member_player_ids: list[str]


class SnapcastGroupRestorer:
    """Restore live Snapcast grouping into MA sync-group runtime state."""

    def __init__(self, provider: SnapCastProvider) -> None:
        """Initialize the restorer."""
        self._provider = provider
        self._logger = provider.logger.getChild("group_restore")

    async def restore(self) -> None:
        """Restore all matching live Snapcast groups into MA sync-group runtime state."""
        if self._provider.mass.closing:
            return

        for target in self._collect_restore_targets():
            await self._restore_target(target)

    def _collect_restore_targets(self) -> list[SnapcastGroupRestoreTarget]:
        """Collect restore targets from live Snapcast groups."""
        targets: list[SnapcastGroupRestoreTarget] = []
        seen_sync_groups: set[str] = set()
        fallback_group_name = getattr(self._provider, "dedicated_fallback_group_name", None)
        for snap_group in self._provider._snapserver.groups:
            if fallback_group_name and getattr(snap_group, "name", None) == fallback_group_name:
                self._logger.debug(
                    "Skipping dedicated fallback group '%s' during restore",
                    fallback_group_name,
                )
                continue

            member_player_ids = self._resolve_group_member_player_ids(snap_group)
            if len(member_player_ids) < 2:
                continue

            stream_display_name = self._resolve_group_stream_display_name(snap_group.stream)
            if not stream_display_name or stream_display_name == "default":
                continue

            sync_group_player = self._provider.resolve_sync_group_player(stream_display_name)
            if sync_group_player is None:
                self._logger.debug(
                    "No matching MA sync group found for live Snapcast stream '%s'",
                    stream_display_name,
                )
                continue

            if sync_group_player.player_id in seen_sync_groups:
                self._logger.debug(
                    "Skipping duplicate restore target for sync group %s",
                    sync_group_player.player_id,
                )
                continue

            if not self._supports_dynamic_restore(sync_group_player):
                self._logger.debug(
                    "Skipping sync group %s because it does not support dynamic member restore",
                    sync_group_player.player_id,
                )
                continue

            sync_leader_id = self._resolve_sync_leader_id(snap_group, member_player_ids)
            if not sync_leader_id:
                continue

            ordered_member_ids = [
                sync_leader_id,
                *[member_id for member_id in member_player_ids if member_id != sync_leader_id],
            ]
            targets.append(
                SnapcastGroupRestoreTarget(
                    sync_group_player_id=sync_group_player.player_id,
                    sync_group_name=getattr(sync_group_player.config, "name", stream_display_name),
                    stream_display_name=stream_display_name,
                    sync_leader_id=sync_leader_id,
                    member_player_ids=ordered_member_ids,
                )
            )
            seen_sync_groups.add(sync_group_player.player_id)
        return targets

    async def _restore_target(self, target: SnapcastGroupRestoreTarget) -> None:
        """Restore one live Snapcast group into the matching MA sync-group runtime."""
        sync_group_player = self._provider.mass.players.get_player(target.sync_group_player_id)
        if sync_group_player is None:
            return

        current_members = [
            member_id
            for member_id in getattr(sync_group_player, "group_members", [])
            if member_id != sync_group_player.player_id
        ]
        target_members = list(dict.fromkeys(target.member_player_ids))
        static_members = set(getattr(sync_group_player, "static_group_members", []))
        player_ids_to_add = [
            member_id for member_id in target_members if member_id not in current_members
        ]
        player_ids_to_remove = [
            member_id
            for member_id in current_members
            if member_id not in target_members and member_id not in static_members
        ]

        if player_ids_to_add or player_ids_to_remove:
            self._logger.info(
                "Restoring MA sync group '%s' from live Snapcast group members=%s",
                target.sync_group_name,
                target_members,
            )
            await self._provider.mass.players.cmd_set_members(
                target.sync_group_player_id,
                player_ids_to_add=player_ids_to_add or None,
                player_ids_to_remove=player_ids_to_remove or None,
            )

        if sync_leader := self._provider.mass.players.get_player(target.sync_leader_id):
            sync_group_player_any = cast("Any", sync_group_player)
            sync_group_player_any.sync_leader = sync_leader

        self._provider.mass.players.trigger_player_update(
            target.sync_group_player_id, force_update=True, debounce_delay=0
        )

    def _resolve_group_member_player_ids(self, snap_group: SnapgroupProto) -> list[str]:
        """Translate a live Snapcast group's client ids to MA player ids."""
        member_player_ids: list[str] = []
        for snap_client_id in getattr(snap_group, "clients", []):
            with suppress(AssertionError, KeyError, ValueError):
                member_player_ids.append(self._provider._get_ma_id(snap_client_id))
        return member_player_ids

    def _resolve_group_stream_display_name(self, stream_ref: str | None) -> str | None:
        """Resolve the visible stream name for a live Snapcast group."""
        if not stream_ref:
            return None

        if ma_stream := self._provider._get_stream_registry().resolve(stream_ref):
            return ma_stream.stream_display_name or ma_stream.stream_id or ma_stream.stream_name

        snap_stream = self._find_snapstream(stream_ref)
        if snap_stream is None:
            return None if stream_ref == "default" else stream_ref

        return (
            self._get_snapstream_visible_name(snap_stream)
            or getattr(snap_stream, "friendly_name", None)
            or getattr(snap_stream, "identifier", None)
        )

    def _find_snapstream(self, stream_ref: str) -> SnapstreamProto | None:
        """Find a live Snapserver stream by id or visible name reference."""
        with suppress(KeyError, AttributeError):
            snap_stream = self._provider._snapserver.stream(stream_ref)
            if snap_stream is not None:
                return snap_stream

        for snap_stream in getattr(self._provider._snapserver, "streams", []):
            if getattr(snap_stream, "identifier", None) == stream_ref:
                return cast("SnapstreamProto", snap_stream)
            if getattr(snap_stream, "friendly_name", None) == stream_ref:
                return cast("SnapstreamProto", snap_stream)
            if self._get_snapstream_visible_name(snap_stream) == stream_ref:
                return cast("SnapstreamProto", snap_stream)
        return None

    def _get_snapstream_visible_name(self, snap_stream: SnapstreamProto) -> str | None:
        """Extract the visible stream name from a live Snapserver stream."""
        stream_meta = cast("dict[str, Any]", getattr(snap_stream, "_stream", {}))
        uri_meta = cast("dict[str, Any]", stream_meta.get("uri", {}))
        raw_uri = cast("str | None", uri_meta.get("raw"))
        if not raw_uri:
            return None
        parsed = urllib.parse.urlparse(raw_uri)
        name = urllib.parse.parse_qs(parsed.query).get("name")
        if not name:
            return None
        return urllib.parse.unquote_plus(name[0])

    def _resolve_sync_leader_id(
        self, snap_group: SnapgroupProto, member_player_ids: list[str]
    ) -> str | None:
        """Resolve the MA sync leader player id for a live Snapcast group."""
        candidate_refs = [
            getattr(snap_group, "name", None),
            getattr(snap_group, "identifier", None),
        ]
        for candidate_ref in candidate_refs:
            if not candidate_ref:
                continue
            candidate_ref = cast("str", candidate_ref)
            if candidate_ref in member_player_ids:
                return candidate_ref
            with suppress(AssertionError, KeyError, ValueError):
                candidate_player_id = self._provider._get_ma_id(candidate_ref)
                if candidate_player_id in member_player_ids:
                    return candidate_player_id
        return member_player_ids[0] if member_player_ids else None

    def _supports_dynamic_restore(self, sync_group_player: Any) -> bool:
        """Return True if the sync-group player supports runtime member restore."""
        if not getattr(sync_group_player, "player_id", "").startswith(SGP_PREFIX):
            return False
        state = getattr(sync_group_player, "state", None)
        supported_features = cast(
            "set[PlayerFeature] | None", getattr(state, "supported_features", None)
        )
        if supported_features is None:
            supported_features = cast(
                "set[PlayerFeature]", getattr(sync_group_player, "supported_features", set())
            )
        return PlayerFeature.SET_MEMBERS in supported_features
