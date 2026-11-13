"""
Player Merging Mixin for the Player Controller.

Merges two players that turn out to be one device, and takes a protocol off the
player it ended up on wrongly.

This module provides the PlayerMergingMixin class which is inherited by
PlayerController to add merging and unlinking.
"""

from __future__ import annotations

import time
from contextlib import suppress
from copy import deepcopy

from music_assistant_models.auth import Scope
from music_assistant_models.enums import PlaybackState, PlayerType, ProtocolRole
from music_assistant_models.errors import InvalidCommand, InvalidDataError, MusicAssistantError

from music_assistant.constants import (
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_LEGACY_IDS,
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_SESSION_ONLY,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_UNLINKED_FROM,
)
from music_assistant.controllers.config.constants import AUTOMATIC_VALUE_KEYS, GROUP_MEMBER_KEYS
from music_assistant.controllers.players.matching import matches_device, vetoes_device
from music_assistant.helpers.api import api_command
from music_assistant.models.player import OWN_PROTOCOL_ROLES, Player
from music_assistant.models.player_provider import PlayerProvider

from .base import _PlayerControllerBase
from .lifecycle import _Attach
from .protocol_linking import MINTED_PLAYER_ID, _native_rank


class PlayerMergingMixin(_PlayerControllerBase):
    """Mixin class providing merging and unlinking of players for PlayerController."""

    @api_command("players/merge", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def merge(self, player_id: str, target_player: str) -> None:
        """
        Merge a player into another one: the two turn out to be one device.

        The player goes and its outputs move to the target player, which answers to its
        id from then on. A name or setting only the player that goes has is kept, its
        queue is dropped.

        :param player_id: ID of the player that goes.
        :param target_player: ID of the player it is merged into, which stays.
        """
        await self.merge_players(target_player, player_id)

    async def merge_players(self, keep_player_id: str, absorb_player_id: str) -> None:
        """
        Merge a player into another one: the two turn out to be one device.

        The absorbed player goes and its protocols move to the kept player. Its id, and
        the ids it answered to, become legacy ids of the kept player, so what still names
        it reaches the kept player. A name or setting only the absorbed player has is
        carried over; where both have one, that of the kept player stays. The queue of
        the absorbed player is dropped, and stopped first when it plays.

        :param keep_player_id: Id of the player that stays, or one of its legacy ids.
        :param absorb_player_id: Id of the player that goes, or one of its legacy ids.
        :raises InvalidDataError: When a player does not exist, or both ids name one player.
        :raises InvalidCommand: When the two can not be one player: both have a protocol
            that stands for the device, or a protocol of the same domain, their identifiers
            tell them apart, or one of them is a group or only exists while connected.
        """
        keep_id = self.resolve_player_id(keep_player_id)
        absorb_id = self.resolve_player_id(absorb_player_id)
        self._check_merge(keep_id, absorb_id)
        if absorbed := self._players.get(absorb_id):
            await self._stop_player_that_is_merged(absorbed)
        async with self._register_lock:
            # either player may have changed while the playback stopped
            self._check_merge(keep_id, absorb_id)
            await self._merge_players(keep_id, absorb_id)

    @api_command("players/unlink_protocol", required_scope=Scope.CONFIG_PLAYERS_WRITE)
    async def unlink_protocol(self, protocol_id: str) -> str:
        """
        Take a protocol off its player and give it a player of its own.

        For a protocol that ended up on the player of another device. It stays on its new
        player, also after a restart, and is not merged back by itself; the protocols that
        ride on it move with it. Returns the id of the new player.

        :param protocol_id: ID of the protocol.
        :raises InvalidDataError: When no protocol has the id.
        :raises InvalidCommand: When the protocol can not stand on its own: it rides on another
            protocol, it is the protocol of a group, it only exists while connected, or it is
            the only protocol of its player.
        """
        async with self._register_lock:
            player_id = self._check_unlink(protocol_id)
            return await self._unlink_protocol(protocol_id, player_id)

    async def _stop_player_that_is_merged(self, player: Player) -> None:
        """
        Stop the queue a player plays before it is merged into another one, best effort.

        Its queue goes with it. A member of a group or of a sync leader is not stopped: its
        membership moves with its protocols.

        :param player: The player that is merged into another one.
        """
        player_id = player.player_id
        if (
            self._resolve_playback_owner(player) is not player
            or player.state.active_source != player_id
            or player.state.playback_state == PlaybackState.IDLE
        ):
            return
        try:
            await self.mass.player_queues.stop(player_id)
        except Exception:
            self.logger.exception("Error stopping %s before it is merged", player.display_name)

    async def _merge_players(self, keep_id: str, absorb_id: str) -> None:
        """
        Merge a player into another one, right after the attach rules allowed it.

        Called under the attach lock. What is stored changes before the first await and is
        saved before anything else of the absorbed player is deleted; the user filters follow
        once its removal went out, so a user limited to it still learns that it went. A
        protocol that fails to attach again, or that its provider unregistered meanwhile,
        attaches to the kept player when its provider finds it again.

        :param keep_id: Id of the player that stays.
        :param absorb_id: Id of the player that goes.
        """
        protocol_ids = set(self.mass.config.get_protocol_ids_of_player(absorb_id))
        if absorbed := self._players.get(absorb_id):
            protocol_ids.update(absorbed.protocol_ids)
        # a disabled player is no live player, only a stored one
        disabled = (
            absorbed is None and self.mass.config.get(f"{CONF_PLAYERS}/{absorb_id}") is not None
        )
        self.logger.info(
            "Merging player %s into %s",
            self._stored_player_name(absorb_id),
            self._stored_player_name(keep_id),
        )
        self._merge_player_config(keep_id, absorb_id, protocol_ids)
        await self.mass.config.async_save()
        absorbed = self._players.get(absorb_id)
        moving = list(absorbed.protocols.values()) if absorbed is not None else []
        # the output that plays for the absorbed player, e.g. in a group, stays the active output
        # of the kept one, so the group keeps its member
        playing_through = (
            absorbed.resolve_output_protocol()
            if absorbed is not None and absorbed.state.playback_state != PlaybackState.IDLE
            else None
        )
        # the providers of the protocols that are not attached look for their devices again
        provider_ids = {
            str(record.get("provider"))
            for protocol_id in protocol_ids - {protocol.protocol_id for protocol in moving}
            if (record := self.mass.config.get_protocol_record(protocol_id))
        }
        # a moving protocol is attaching: its provider unregistering it meanwhile takes this away
        moves = {protocol.protocol_id: _Attach() for protocol in moving}
        self._attaching.update(moves)
        for protocol in moving:
            if protocol.player is absorbed:
                await self._unbind(protocol)
        if absorbed is not None or disabled:
            # a live player that is gone meanwhile was removed, and announced, by the user
            await self._drop_player(absorb_id, absorbed)
        try:
            await self.mass.webserver.auth.replace_player_in_user_filters(
                absorb_id, keep_id, removed_player_ids=[absorb_id]
            )
        except Exception:
            self.logger.exception("Could not move the user filters of %s", absorb_id)
        for protocol in moving:
            if self._attaching.get(protocol.protocol_id) is not moves[protocol.protocol_id]:
                # its provider unregistered it while it moved, and keeps the object
                if not protocol.provider.unloading:
                    self._unattached[protocol.protocol_id] = protocol
                continue
            try:
                await self._run_attach(protocol)
            except Exception:
                self.logger.exception(
                    "Protocol %s did not attach to %s after the merge",
                    protocol.protocol_id,
                    self._stored_player_name(keep_id),
                )
        if keeper := self._players.get(keep_id):
            # a protocol that moved here while away may rank above one that is attached
            await self._attach_in_changed_roles(keeper)
        if (keeper := self._players.get(keep_id)) and keeper.initialized.is_set():
            # the player caches its legacy ids with its config
            keeper.set_config(await self.mass.config.get_player_object_config(keeper))
            keeper.update_state()
            if (
                playing_through is not None
                and keeper.protocols.get(playing_through.protocol_id) is playing_through
                and keeper.active_output_protocol is None
            ):
                keeper.set_active_output_protocol(playing_through.protocol_id)
        for provider_id in provider_ids:
            if isinstance(provider := self.mass.get_provider(provider_id), PlayerProvider):
                provider.schedule_discovery()
        self._schedule_held_protocols()
        self._schedule_update_all_players()

    def _merge_player_config(self, keep_id: str, absorb_id: str, protocol_ids: set[str]) -> None:
        """
        Move what is stored about a player to the player it is merged into.

        Its protocols, its group memberships and its ids follow; its name, its values and
        its DSP and queue settings where the kept player has none. Its own config is
        deleted last.

        :param keep_id: Id of the player that stays.
        :param absorb_id: Id of the player that goes.
        :param protocol_ids: Ids of the protocols of the player that goes.
        """
        config = self.mass.config
        keep_key = f"{CONF_PLAYERS}/{keep_id}"
        kept = config.get(keep_key) or {}
        absorbed = config.get(f"{CONF_PLAYERS}/{absorb_id}") or {}
        kept_values = kept.get("values") or {}
        absorbed_values = absorbed.get("values") or {}
        if absorbed.get("name") and not kept.get("name"):
            config.set(f"{keep_key}/name", absorbed["name"])
        for key, value in absorbed_values.items():
            if key not in kept_values and key not in AUTOMATIC_VALUE_KEYS:
                config.set(f"{keep_key}/values/{key}", deepcopy(value))
        stored_identifiers = absorbed_values.get(CONF_DEVICE_IDENTIFIERS)
        if isinstance(stored_identifiers, dict) and stored_identifiers:
            config.set(
                f"{keep_key}/values/{CONF_DEVICE_IDENTIFIERS}",
                {**stored_identifiers, **(kept_values.get(CONF_DEVICE_IDENTIFIERS) or {})},
            )
        legacy_ids = [
            *(kept_values.get(CONF_LEGACY_IDS) or []),
            absorb_id,
            *(absorbed_values.get(CONF_LEGACY_IDS) or []),
        ]
        config.set(
            f"{keep_key}/values/{CONF_LEGACY_IDS}",
            list(
                dict.fromkeys(
                    legacy_id
                    for legacy_id in legacy_ids
                    if isinstance(legacy_id, str) and legacy_id != keep_id
                )
            ),
        )
        merged = {keep_id, absorb_id}
        for protocol_id in protocol_ids | set(config.get_protocol_ids_of_player(keep_id)):
            if (record := config.get_protocol_record(protocol_id)) is None:
                continue
            if record.get("player_id") != keep_id:
                config.set_protocol_value(protocol_id, "player_id", keep_id)
            unlinked_from = (record.get("values") or {}).get(CONF_UNLINKED_FROM)
            if isinstance(unlinked_from, str) and self.resolve_player_id(unlinked_from) in merged:
                # the user puts the protocol back together with the player it was taken off
                config.remove(f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}/values/{CONF_UNLINKED_FROM}")
        if keeper := self._players.get(keep_id):
            keeper.protocol_ids.update(protocol_ids)
        absorbed_dsp = config.get(f"{CONF_PLAYER_DSP}/{absorb_id}")
        if absorbed_dsp and not config.get(f"{CONF_PLAYER_DSP}/{keep_id}"):
            config.set(f"{CONF_PLAYER_DSP}/{keep_id}", deepcopy(absorbed_dsp))
        absorbed_queue = config.get(f"{CONF_PLAYER_QUEUES}/{absorb_id}")
        if isinstance(absorbed_queue, dict) and absorbed_queue.get("values"):
            kept_queue = deepcopy(config.get(f"{CONF_PLAYER_QUEUES}/{keep_id}") or {})
            kept_queue["queue_id"] = keep_id
            kept_queue["values"] = {
                **deepcopy(absorbed_queue["values"]),
                **(kept_queue.get("values") or {}),
            }
            config.set(f"{CONF_PLAYER_QUEUES}/{keep_id}", kept_queue)
        self._update_group_memberships(absorb_id, keep_id)
        for key in (
            f"{CONF_PLAYERS}/{absorb_id}",
            f"{CONF_PLAYER_DSP}/{absorb_id}",
            f"{CONF_PLAYER_QUEUES}/{absorb_id}",
        ):
            config.remove(key)
        self._build_legacy_id_index()

    async def _unlink_protocol(self, protocol_id: str, player_id: str) -> str:
        """
        Move a protocol, with the protocols riding on it, to a new player of its own.

        :param protocol_id: Id of the protocol, which the unlink rules allow to move.
        :param player_id: Id of the player it is taken off.
        """
        config = self.mass.config
        protocol = self._protocols.get(protocol_id)
        record = config.get_protocol_record(protocol_id) or {}
        moved = [protocol_id, *config.get_protocol_ids_derived_from(protocol_id)]
        new_id = self._new_player_id()
        config.create_player_record(
            new_id,
            provider=str(record.get("provider")),
            player_type=protocol.type if protocol else PlayerType.PLAYER,
            default_name=(protocol.name if protocol else None) or record.get("default_name"),
            values={CONF_CREATED_AT: time.time_ns()},
        )
        for moved_id in moved:
            config.set_protocol_value(moved_id, "player_id", new_id)
            self._replace_protocol_references(player_id, moved_id, None)
        config.set_protocol_value(protocol_id, CONF_UNLINKED_FROM, player_id)
        if player := self._players.get(player_id):
            player.protocol_ids.difference_update(moved)
        self.logger.info(
            "Protocol %s taken off player %s", protocol_id, self._stored_player_name(player_id)
        )
        attached = [
            attached_protocol
            for moved_id in moved
            if (attached_protocol := self._protocols.get(moved_id))
            and attached_protocol.player is not None
        ]
        for attached_protocol in attached:
            self._attaching.pop(attached_protocol.protocol_id, None)
            await self._unbind(attached_protocol)
        for attached_protocol in attached:
            try:
                await self._run_attach(attached_protocol)
            except Exception:
                self.logger.exception(
                    "Protocol %s did not attach to its new player", attached_protocol.protocol_id
                )
        if new_id not in self._players:
            # its device is away, or was not attached as its player was disabled: the
            # player waits for it like a stored one at startup, and its provider looks again
            await self._publish_player(self._create_player(new_id))
            if isinstance(
                provider := self.mass.get_provider(str(record.get("provider"))), PlayerProvider
            ):
                provider.schedule_discovery()
        if player is not None:
            # a protocol the moved one ranked above stands for the device again
            await self._attach_in_changed_roles(player)
            await self._settle_player(player)
        self._schedule_update_all_players()
        return new_id

    def _schedule_duplicate_merge(self, player_id: str) -> None:
        """
        Look for unused duplicates of a player, now that one of its protocols attached.

        :param player_id: Id of the player.
        """
        self.mass.create_task(
            self._merge_duplicates(player_id), task_id=f"merge_duplicates_{player_id}"
        )

    async def _merge_duplicates(self, player_id: str) -> None:
        """
        Merge the players that turn out to stand for the device of a player, while unused.

        Of two such players the younger one goes into the older one, and only while nothing
        about it is the user's own yet (see _is_unused); otherwise both stay, for the user to
        merge by hand.

        :param player_id: Id of the player.
        """
        async with self._register_lock:
            while not self.mass.closing and player_id in self._players:
                for other_id in self._duplicates_of(player_id):
                    keep_id, absorb_id = sorted((player_id, other_id), key=self._merge_rank)
                    if (absorbed := self._players.get(absorb_id)) and self._is_unused(absorbed):
                        break
                else:
                    return
                self.logger.info(
                    "Player %s turns out to be the device of %s",
                    self._stored_player_name(absorb_id),
                    self._stored_player_name(keep_id),
                )
                await self._merge_players(keep_id, absorb_id)
                player_id = keep_id

    def _check_merge(self, keep_id: str, absorb_id: str) -> None:
        """
        Check that one player can take over the protocols of another by the attach rules.

        :param keep_id: Id of the player that stays.
        :param absorb_id: Id of the player that goes.
        :raises InvalidDataError: When a player does not exist, or both ids are the same.
        :raises InvalidCommand: When the two can not be one player.
        """
        if error := self._merge_refusal(keep_id, absorb_id):
            raise error

    def _merge_refusal(self, keep_id: str, absorb_id: str) -> MusicAssistantError | None:
        """
        Return why the attach rules do not let one player take over the protocols of another.

        :param keep_id: Id of the player that stays.
        :param absorb_id: Id of the player that goes.
        """
        if keep_id == absorb_id:
            return InvalidDataError(f"Player {keep_id} can not be merged into itself")
        names = [self._stored_player_name(player_id) for player_id in (keep_id, absorb_id)]
        for player_id, name in zip((keep_id, absorb_id), names, strict=True):
            player = self._players.get(player_id)
            stored = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
            values = stored.get("values") if isinstance(stored, dict) else None
            if (player and player.session_only) or (
                isinstance(values, dict) and values.get(CONF_SESSION_ONLY)
            ):
                msg = f"{name} only exists while it is connected and can not be merged"
                return self._refused(msg, "merge_session_only", name)
            if not isinstance(stored, dict):
                return InvalidDataError(f"No player with id {player_id}")
            if player_id in self._removing:
                return InvalidCommand(f"{name} is being removed")
            player_type = player.type if player else stored.get("player_type")
            if player_type == PlayerType.GROUP or ProtocolRole.VIRTUAL in self._roles_of(player_id):
                return self._refused(
                    f"{name} is a group and can not be merged", "merge_group", name
                )
        if self._own_ranks(keep_id) & self._own_ranks(absorb_id):
            # of two protocols that stand for the device, one has to rank above the other
            msg = f"{names[0]} and {names[1]} each stand for a device of their own"
            return self._refused(msg, "merge_two_own_protocols", *names)
        if shared := self._domains_of(keep_id) & self._domains_of(absorb_id):
            domain = min(shared)
            with suppress(KeyError):
                domain = self.mass.get_provider_manifest(domain).name
            msg = f"{names[0]} and {names[1]} both have a {domain} output"
            return self._refused(msg, "merge_shared_domain", *names, domain)
        if vetoes_device(self._merged_identity(absorb_id), self._identity_parts(keep_id)):
            msg = f"{names[0]} and {names[1]} report different devices"
            return self._refused(msg, "merge_identifiers_differ", *names)
        return None

    def _refused(self, msg: str, translation_key: str, *args: str) -> InvalidCommand:
        """
        Return the error of a merge or an unlink the attach rules do not allow.

        :param msg: What stands in the way.
        :param translation_key: Key of the translated message.
        :param args: The arguments of the translated message.
        """
        return InvalidCommand(
            msg,
            translation_key=translation_key,
            translation_owner=self.translation_owner,
            translation_args=list(args),
        )

    def _roles_of(self, player_id: str) -> set[ProtocolRole]:
        """
        Return the roles of the protocols of a player, attached or not.

        :param player_id: Id of the player.
        """
        player = self._players.get(player_id)
        roles: set[ProtocolRole] = set()
        for protocol_id in self._get_protocol_ids(player_id):
            if player and (role := player.get_role(protocol_id)):
                roles.add(role)
            elif (record := self.mass.config.get_protocol_record(protocol_id)) and (
                stored_role := record.get("role")
            ):
                roles.add(ProtocolRole(stored_role))
        return roles

    def _own_ranks(self, player_id: str) -> set[int]:
        """
        Return the ranks of the protocols of a player that stand for the device, attached or not.

        :param player_id: Id of the player.
        """
        ranks: set[int] = set()
        for protocol_id in self._get_protocol_ids(player_id):
            if protocol := self._protocols.get(protocol_id):
                stands_for_device = protocol.role in OWN_PROTOCOL_ROLES
                provider_id = protocol.provider_id
            else:
                record = self.mass.config.get_protocol_record(protocol_id) or {}
                stands_for_device = record.get("role") in OWN_PROTOCOL_ROLES
                provider_id = str(record.get("provider"))
            if stands_for_device:
                ranks.add(_native_rank(provider_id))
        return ranks

    def _domains_of(self, player_id: str) -> set[str]:
        """
        Return the domains of the protocols of a player, attached or not.

        :param player_id: Id of the player.
        """
        domains: set[str] = set()
        for protocol_id in self._get_protocol_ids(player_id):
            if protocol := self._protocols.get(protocol_id):
                domains.add(protocol.provider.domain)
            elif record := self.mass.config.get_protocol_record(protocol_id):
                domains.add(str(record.get("provider") or "").split("--", maxsplit=1)[0])
        return domains

    def _stored_player_name(self, player_id: str) -> str:
        """
        Return the name of a player, also of one that is not live.

        :param player_id: Id of the player.
        """
        if player := self._players.get(player_id):
            return player.display_name
        stored = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if isinstance(stored, dict):
            return str(stored.get("name") or stored.get("default_name") or player_id)
        return player_id

    def _check_unlink(self, protocol_id: str) -> str:
        """
        Check that a protocol can be taken off its player, and return the id of that player.

        :param protocol_id: Id of the protocol.
        :raises InvalidDataError: When no protocol has the id.
        :raises InvalidCommand: When the protocol can not stand on its own.
        """
        protocol = self._protocols.get(protocol_id)
        record = self.mass.config.get_protocol_record(protocol_id)
        if protocol is None and record is None:
            raise InvalidDataError(f"No protocol with id {protocol_id}")
        record = record or {}
        name = str(
            (protocol.name if protocol else None) or record.get("default_name") or protocol_id
        )
        if (protocol and protocol.session_only) or not record:
            msg = f"{name} only exists while it is connected and can not stand on its own"
            raise self._refused(msg, "unlink_session_only", name)
        if not (player_id := self.get_player_id_of_protocol(protocol_id)):
            raise InvalidDataError(f"Protocol {protocol_id} belongs to no player")
        if (protocol and protocol.underlying_protocol_id) or (record.get("values") or {}).get(
            CONF_UNDERLYING_PLAYER_ID
        ):
            msg = f"{name} rides on another protocol and follows it"
            raise self._refused(msg, "unlink_derived", name)
        if (protocol.role if protocol else record.get("role")) == ProtocolRole.VIRTUAL:
            msg = f"{name} is the protocol of a group"
            raise self._refused(msg, "unlink_group", name)
        if player_id in self._removing:
            raise InvalidCommand(f"{self._stored_player_name(player_id)} is being removed")
        moved = {protocol_id, *self.mass.config.get_protocol_ids_derived_from(protocol_id)}
        if not self._get_protocol_ids(player_id) - moved:
            msg = f"{name} is the only protocol of its player"
            raise self._refused(msg, "unlink_only_protocol", name)
        return player_id

    def _duplicates_of(self, player_id: str) -> list[str]:
        """
        Return the ids of the other live players that turn out to stand for a player's device.

        That is every player the attach rules let take over the protocols of the player (or
        the other way round) and whose identifiers it matches, unless the user took a
        protocol of one of the two off the other.

        :param player_id: Id of the player.
        """
        identity = self._merged_identity(player_id)
        return [
            other_id
            for other_id in self._players
            if other_id != player_id
            and matches_device(identity, self._identity_parts(other_id))
            and self._merge_refusal(other_id, player_id) is None
            and not self._taken_apart(player_id, other_id)
        ]

    def _taken_apart(self, player_id: str, other_id: str) -> bool:
        """
        Return whether the user took a protocol of one of two players off the other one.

        :param player_id: Id of one of the players.
        :param other_id: Id of the other player.
        """
        for holder_id, from_id in ((player_id, other_id), (other_id, player_id)):
            for protocol_id in self._get_protocol_ids(holder_id):
                record = self.mass.config.get_protocol_record(protocol_id) or {}
                unlinked_from = (record.get("values") or {}).get(CONF_UNLINKED_FROM)
                if (
                    isinstance(unlinked_from, str)
                    and self.resolve_player_id(unlinked_from) == from_id
                ):
                    return True
        return False

    def _merge_rank(self, player_id: str) -> tuple[int, bool, str]:
        """
        Return the sort key of a player in a merge, lowest is kept.

        The oldest player is kept, as the one clients have been bound to the longest. A
        record without a creation moment is older than any with one, and of two equally old
        players the one whose id is not minted is kept.

        :param player_id: Id of the player.
        """
        created_at = self.mass.config.get_player_value(player_id, CONF_CREATED_AT)
        minted = MINTED_PLAYER_ID.fullmatch(player_id) is not None
        return (created_at if isinstance(created_at, int) else 0, minted, player_id)

    def _is_unused(self, player: Player) -> bool:
        """
        Return whether nothing about a player is the user's own yet, so it may merge by itself.

        A player is used once it has any of:
        - a custom name, or a value the user set on it or on one of its protocols;
        - DSP that is switched on, or settings of its queue;
        - items in its queue (restored from the saved queue, if any), or playback right now,
          which a plugin source starts without queue items;
        - a place in the member list of a group, a sync leader or members synced to it, or
          a group that holds it.

        :param player: The player.
        """
        player_id = player.player_id
        config = self.mass.config
        record = config.get(f"{CONF_PLAYERS}/{player_id}") or {}
        records = [
            record,
            *filter(None, map(config.get_protocol_record, self._get_protocol_ids(player_id))),
        ]
        dsp = config.get(f"{CONF_PLAYER_DSP}/{player_id}")
        state = player.state
        return not (
            record.get("name")
            or any(
                key not in AUTOMATIC_VALUE_KEYS
                for stored in records
                for key in stored.get("values") or {}
            )
            or (isinstance(dsp, dict) and dsp.get("enabled"))
            or config.get(f"{CONF_PLAYER_QUEUES}/{player_id}") is not None
            or self.mass.player_queues.items(player_id, limit=1)
            or state.playback_state != PlaybackState.IDLE
            or self._is_group_member(player_id)
            or state.synced_to
            or state.group_members
            or state.active_group
        )

    def _is_group_member(self, player_id: str) -> bool:
        """
        Return whether a group lists a player as a member, or as one it allows.

        :param player_id: Id of the player.
        """
        for record in self.mass.config.get(CONF_PLAYER_PROTOCOLS, {}).values():
            values = record.get("values") if isinstance(record, dict) else None
            if isinstance(values, dict) and any(
                isinstance(members := values.get(key), list) and player_id in members
                for key in GROUP_MEMBER_KEYS
            ):
                return True
        return False
