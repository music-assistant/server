"""
Apple groups: play an Apple TV home theater group or a stereo pair as one player.

The Home app groups AirPlay devices and every device advertises that grouping in its
AirPlay TXT record (see the constants). Apple's own senders read it from discovery
alone: no setup, and unpairing in the Home app simply shows up on the next discovery.
This does the same. Each Apple group becomes a static sync group, owned by this
provider and keyed on the group id the members share, with its members hidden.

The group is managed by the Home app, not by MA, and exists only while discovery
shows it, as in the AirPlay menu of an iPhone: nothing about it can be changed in MA
(Player.managed_externally) and nothing about it is kept. It goes when none of its
members is discoverable any more, when a member advertises a different group (fresh
discovery wins), and when this provider unloads; a provider (re)start removes any
group a previous run left behind. A member going offline, or coming back with the
same group id, leaves the group alone. Re-pairing in the Home app yields a new group
id and so a new group (and queue).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from music_assistant.constants import (
    AIRPLAY_GROUP_HOME_THEATER,
    AIRPLAY_GROUP_STEREO_PAIR,
    CONF_AIRPLAY_GROUP_ID,
    CONF_AIRPLAY_GROUP_LEADER,
    CONF_AIRPLAY_GROUP_TYPE,
    CONF_GROUP_MEMBERS,
    CONF_PLAYERS,
)
from music_assistant.providers.sync_group.constants import SGP_PREFIX

from .constants import APPLE_GROUP_RECONCILE_DELAY, CONF_APPLE_HOME_GROUPS
from .helpers import get_decoded_property

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.providers.sync_group.provider import SyncGroupProvider

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider

# a group's leader (the Apple TV) sorts after its speakers: the speakers carry the audio
_LEADER_POSITION = 1000


@dataclass(frozen=True)
class AppleGroupMembership:
    """The Apple group a device advertises it belongs to."""

    group_id: str
    group_type: str
    name: str | None
    # where the member goes in the group's member list (leader preference order)
    position: int


@dataclass
class _FoundGroup:
    """An Apple group as discovery currently shows it."""

    group_type: str
    name: str | None = None
    members: list[tuple[int, str]] = field(default_factory=list)
    # the member advertising itself as the group's leader (igl): the Apple TV
    leader_id: str | None = None

    def ordered_member_ids(self) -> list[str]:
        """Return the member player ids in leader preference order."""
        return [player_id for _, player_id in sorted(self.members)]


def get_home_theater_membership(player: AirPlayPlayer) -> AppleGroupMembership | None:
    """
    Return the Apple TV home theater group a device advertises, if any.

    :param player: The AirPlay player to read the discovery info of.
    """
    if (info := player.airplay_discovery_info) is None:
        return None
    if not (psgid := get_decoded_property(info, "psgid")):
        return None
    if get_decoded_property(info, "psgtp") is None:
        return None
    pgid = get_decoded_property(info, "pgid")
    if pgid is None and get_decoded_property(info, "igl") == "1":
        # the group's leader (the Apple TV) advertises the group without pgid
        position = _LEADER_POSITION
    elif pgid == psgid:
        try:
            position = int(get_decoded_property(info, "pgmid") or 0)
        except ValueError:
            position = 0
    else:
        return None
    return AppleGroupMembership(
        group_id=psgid.upper(),
        group_type=AIRPLAY_GROUP_HOME_THEATER,
        name=get_decoded_property(info, "gpn"),
        position=position,
    )


def get_stereo_pair_membership(player: AirPlayPlayer) -> AppleGroupMembership | None:
    """
    Return the stereo pair a device advertises, if any.

    :param player: The AirPlay player to read the discovery info of.
    """
    if (info := player.airplay_discovery_info) is None:
        return None
    if not (tsid := get_decoded_property(info, "tsid")):
        return None
    # gid is "<tsid>+<role>+<device id>" within a pair: order the pair by role
    position = 0
    gid_parts = (get_decoded_property(info, "gid") or "").split("+")
    if len(gid_parts) == 3 and gid_parts[1].isdigit():
        position = int(gid_parts[1])
    return AppleGroupMembership(
        group_id=tsid.upper(),
        group_type=AIRPLAY_GROUP_STEREO_PAIR,
        name=get_decoded_property(info, "gpn"),
        position=position,
    )


def get_membership(player: AirPlayPlayer, group_type: str) -> AppleGroupMembership | None:
    """
    Return the Apple group of the given type a device advertises, if any.

    :param player: The AirPlay player to read the discovery info of.
    :param group_type: AIRPLAY_GROUP_HOME_THEATER or AIRPLAY_GROUP_STEREO_PAIR.
    """
    if group_type == AIRPLAY_GROUP_HOME_THEATER:
        return get_home_theater_membership(player)
    return get_stereo_pair_membership(player)


def group_player_id(group_id: str) -> str:
    """
    Return the (stable) sync group player id for an Apple group id.

    :param group_id: The group id the members share (psgid or tsid).
    """
    return f"{SGP_PREFIX}ap{group_id.replace('-', '').lower()}"


class AppleGroupManager:
    """Keep the sync groups for Apple groups in line with what discovery shows."""

    def __init__(self, provider: AirPlayProvider) -> None:
        """
        Initialize the manager.

        :param provider: The AirPlay provider whose players are grouped.
        """
        self.prov = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("apple_groups")
        self._lock = asyncio.Lock()
        self._reconcile_task_id = f"airplay_apple_groups_{provider.instance_id}"

    def schedule_reconcile(self) -> None:
        """Regroup once the current burst of discovery updates has settled."""
        self.mass.call_later(
            APPLE_GROUP_RECONCILE_DELAY, self.reconcile, task_id=self._reconcile_task_id
        )

    async def setup(self) -> None:
        """Start without groups: discovery forms them (again)."""
        await self._remove_groups()

    async def unload(self) -> None:
        """Stop regrouping and remove the groups this manager formed."""
        self.mass.cancel_timer(self._reconcile_task_id)
        if self.mass.closing:
            # leave the players alone while the server shuts down: the next start
            # removes what is left (see setup)
            return
        await self._remove_groups()

    async def reconcile(self) -> None:
        """Form, extend or dissolve the Apple group sync groups to match discovery."""
        async with self._lock:
            sync_prov = cast("SyncGroupProvider | None", self.mass.get_provider("sync_group"))
            if sync_prov is None:
                # retried on the next discovery update
                return
            enabled = bool(self.prov.config.get_value(CONF_APPLE_HOME_GROUPS, False))
            present = {
                player.player_id: player
                for player in self.prov.get_players()
                if player.airplay_discovery_info is not None
            }
            found = self._find_groups(present) if enabled else {}
            in_found_home_theater = {
                player_id
                for group in found.values()
                if group.group_type == AIRPLAY_GROUP_HOME_THEATER and len(group.members) > 1
                for _, player_id in group.members
            }
            # member -> name of the Apple group player that represents it
            represented_by: dict[str, str] = {}
            for player_conf in await self.mass.config.get_player_configs(sync_prov.instance_id):
                player_id = player_conf.player_id
                group_id = self.mass.config.get_raw_player_config_value(
                    player_id, CONF_AIRPLAY_GROUP_ID
                )
                if group_id is None:
                    # not ours: a sync group the user made
                    continue
                group_name = player_conf.name or player_conf.default_name or player_id
                group_type = self.mass.config.get_raw_player_config_value(
                    player_id, CONF_AIRPLAY_GROUP_TYPE
                )
                members = cast(
                    "list[str]",
                    self.mass.config.get_raw_player_config_value(player_id, CONF_GROUP_MEMBERS, []),
                )
                if reason := self._dissolve_reason(
                    str(group_id),
                    None if group_type is None else str(group_type),
                    members,
                    present,
                    enabled,
                    in_found_home_theater,
                ):
                    self.logger.info("Dissolving %s: %s", group_name, reason)
                    await sync_prov.remove_group_player(player_id)
                    continue
                if (group := found.pop(str(group_id), None)) is not None:
                    # a member discovery shows that the group lacks joins it (in its
                    # place in the group); members it does not show stay, as offline
                    new_members = group.ordered_member_ids()
                    final_members = new_members + [x for x in members if x not in new_members]
                    # the leader is learnt once it is seen (it may join after the group formed)
                    leader_update = (
                        {CONF_AIRPLAY_GROUP_LEADER: group.leader_id}
                        if group.leader_id
                        and group.leader_id
                        != self.mass.config.get_raw_player_config_value(
                            player_id, CONF_AIRPLAY_GROUP_LEADER
                        )
                        else {}
                    )
                    if set(final_members) != set(members) or leader_update:
                        if added := [x for x in final_members if x not in members]:
                            self.logger.info("Adding %s to %s", added, group_name)
                        await sync_prov.ensure_owned_group_player(
                            player_id, group_name, final_members, leader_update
                        )
                    members = final_members
                represented_by.update(dict.fromkeys(members, group_name))
            for group_id, group in found.items():
                if len(group.members) < 2:
                    # a group of one is no group (yet): its other members may follow
                    continue
                members = group.ordered_member_ids()
                name = group.name or present[members[-1]].display_name
                self.logger.info("Forming %s from %s", name, members)
                values: dict[str, ConfigValueType] = {
                    CONF_AIRPLAY_GROUP_ID: group_id,
                    CONF_AIRPLAY_GROUP_TYPE: group.group_type,
                }
                if group.leader_id:
                    values[CONF_AIRPLAY_GROUP_LEADER] = group.leader_id
                await sync_prov.ensure_owned_group_player(
                    group_player_id(group_id), name, members, values
                )
                represented_by.update(dict.fromkeys(members, name))
            for player_id, player in present.items():
                player.set_represented_by(represented_by.get(player_id))

    def _find_groups(self, present: dict[str, AirPlayPlayer]) -> dict[str, _FoundGroup]:
        """
        Return the Apple groups discovery shows now, keyed on group id.

        A stereo pair inside an Apple TV group plays as part of that group, as an
        iPhone shows it, so it only forms a group of its own when it is not in one.

        :param present: The AirPlay players discovery currently shows.
        """
        found: dict[str, _FoundGroup] = {}
        in_home_theater: set[str] = set()
        for group_type in (AIRPLAY_GROUP_HOME_THEATER, AIRPLAY_GROUP_STEREO_PAIR):
            for player_id, player in present.items():
                if player_id in in_home_theater:
                    continue
                if (membership := get_membership(player, group_type)) is None:
                    continue
                group = found.setdefault(membership.group_id, _FoundGroup(group_type))
                group.name = group.name or membership.name
                group.members.append((membership.position, player_id))
                if membership.position == _LEADER_POSITION:
                    group.leader_id = player_id
            if group_type == AIRPLAY_GROUP_HOME_THEATER:
                in_home_theater = {
                    player_id
                    for group in found.values()
                    if len(group.members) > 1
                    for _, player_id in group.members
                }
        return found

    def _dissolve_reason(
        self,
        group_id: str,
        group_type: str | None,
        members: list[str],
        present: dict[str, AirPlayPlayer],
        enabled: bool,
        in_found_home_theater: set[str],
    ) -> str | None:
        """
        Return why an Apple group must go, or None when it still holds.

        :param group_id: The group id the group is keyed on.
        :param group_type: The group's type.
        :param members: The group's members.
        :param present: The AirPlay players discovery currently shows.
        :param enabled: Whether Apple Home groupings are followed.
        :param in_found_home_theater: Players discovery shows in an Apple TV group.
        """
        if not enabled:
            return "Apple Home groupings turned off in the AirPlay settings"
        if group_type not in (AIRPLAY_GROUP_HOME_THEATER, AIRPLAY_GROUP_STEREO_PAIR):
            return f"its saved group type ({group_type}) is not a known Apple group type"
        if not any(member_id in present for member_id in members):
            return "none of its members is discoverable"
        for member_id in members:
            if (player := present.get(member_id)) is None:
                # offline is no conflict: it rejoins when it is back
                continue
            membership = get_membership(player, group_type)
            if membership is None or membership.group_id != group_id:
                return f"{player.display_name} now advertises a different group"
            if group_type == AIRPLAY_GROUP_STEREO_PAIR and member_id in in_found_home_theater:
                return f"{player.display_name} now plays in an Apple TV group"
        return None

    async def _remove_groups(self) -> None:
        """Remove every group player formed from an Apple group, including its config."""
        sync_prov = cast("SyncGroupProvider | None", self.mass.get_provider("sync_group"))
        async with self._lock:
            raw_player_confs = cast(
                "dict[str, dict[str, Any]]", self.mass.config.get(CONF_PLAYERS, {})
            )
            for player_id, raw_conf in list(raw_player_confs.items()):
                if CONF_AIRPLAY_GROUP_ID not in raw_conf.get("values", {}):
                    continue
                self.logger.debug("Removing %s", raw_conf.get("default_name") or player_id)
                if sync_prov is not None and self.mass.players.get_player(player_id):
                    await sync_prov.remove_group_player(player_id)
                else:
                    # not registered (the sync group provider is not loaded (yet))
                    self.mass.players.delete_player_config(player_id)
