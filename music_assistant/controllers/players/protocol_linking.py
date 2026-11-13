"""
Protocol Linking Mixin for the Player Controller.

Handles all logic for the protocols of a player: which player a protocol that
attaches belongs to, which of its protocols a player plays and is controlled
through, and how a group of players is formed over them.

This module provides the ProtocolLinkingMixin class which is inherited by
PlayerController to add these capabilities.
"""

from __future__ import annotations

import re
import secrets
import time
from contextlib import suppress
from copy import deepcopy
from typing import TYPE_CHECKING

from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.errors import (
    InvalidCommand,
    InvalidDataError,
    MusicAssistantError,
)

from music_assistant.constants import (
    CONF_CACHED_ARP_MAC,
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_GROUP_MEMBERS,
    CONF_MUTE_CONTROL,
    CONF_PLAYER_DSP,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYER_QUEUES,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_IDENTIFIERS,
    CONF_REPORTED_MAC,
    CONF_SESSION_ONLY,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_UNLINKED_FROM,
    CONF_VOLUME_CONTROL,
    NATIVE_PRIORITY,
)
from music_assistant.controllers.config.constants import AUTOMATIC_VALUE_KEYS, GROUP_MEMBER_KEYS
from music_assistant.controllers.players.matching import (
    DeviceIdentity,
    has_hardware_mac,
    matches_device,
    vetoes_device,
)
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import OWN_PROTOCOL_ROLES, Player, get_device_name

from .base import _PlayerControllerBase

if TYPE_CHECKING:
    from music_assistant.models.player_protocol import PlayerProtocol

# How long an audio protocol that matches no player waits for the identifiers that tell
# which device it belongs to (an address that is not resolved yet), before it becomes a
# player of its own.
IDENTITY_COLLECT_WINDOW = 15.0

# The prefix of the id of a player that is created for a new device.
PLAYER_ID_PREFIX = "pl"

# The id of a player that was created for a new device, as opposed to one from before the
# player split.
MINTED_PLAYER_ID = re.compile(rf"{PLAYER_ID_PREFIX}[0-9a-f]{{12}}")


class ProtocolLinkingMixin(_PlayerControllerBase):
    """
    Mixin class providing the handling of protocols for PlayerController.

    Handles the complex logic of:
    - Finding the player a protocol belongs to via its device identifiers
    - Creating a player for a device that is new
    - Selecting the best output protocol for playback
    - Translating a group of players to the protocols that carry it
    """

    def _resolve_player_id(self, protocol: PlayerProtocol) -> str | None:
        """
        Return the id of the player a protocol belongs to, None when it matches none.

        That is the player it was attached to before, else the player whose device
        identifiers it shares.

        :param protocol: The protocol that attaches.
        """
        if protocol.underlying_protocol_id:
            # A derived protocol (e.g. a Sendspin bridge riding on another protocol)
            # belongs to the player of the protocol it rides on - no identifier matching.
            return self._resolve_derived_player_id(protocol)
        protocol_id = protocol.protocol_id
        identity = self._identity_of(protocol)
        stored = self.mass.config.get_protocol_record(protocol_id)
        if stored and (player_id := stored.get("player_id")):
            if self._can_attach_to(protocol, identity, player_id, attached_only=True):
                return str(player_id)
            # what only exists while it is connected stands for no device: a link it can
            # not follow stays for when the protocol connects as the device again
            if not protocol.session_only:
                # the player it was attached to turned out to be another device
                self.logger.info(
                    "Protocol %s no longer belongs to player %s, looking for its device again",
                    protocol_id,
                    player_id,
                )
                self.mass.config.set_protocol_value(protocol_id, "player_id", None)
                if player := self._players.get(player_id):
                    player.remove_protocol(protocol_id, forget=True)
        if protocol.role == ProtocolRole.VIRTUAL:
            # a group is no device that another protocol could stand for
            return None
        if protocol.session_only:
            # what only exists while it is connected stands for no device
            return None
        candidates = sorted(
            self._get_candidate_player_ids(),
            # a player holding a protocol of its provider that is away is where the device
            # came back to under another id
            key=lambda player_id: self._get_replaced_protocol_id(protocol, player_id) is None,
        )
        for player_id in candidates:
            if not self._can_attach_to(protocol, identity, player_id):
                continue
            if not matches_device(identity, self._identity_parts(player_id)):
                continue
            if replaced_id := self._get_replaced_protocol_id(protocol, player_id):
                self._take_over_protocol_record(replaced_id, protocol_id, player_id)
            return player_id
        return None

    def _resolve_derived_player_id(self, protocol: PlayerProtocol) -> str | None:
        """
        Return the id of the player of the protocol a derived protocol rides on.

        None while that protocol is not attached to a player, when its player already
        has a protocol of the domain of the derived one (attached, or away and not
        switched off by the user), or when it is being removed.

        :param protocol: The derived protocol.
        """
        underlying_id = protocol.underlying_protocol_id or ""
        underlying = self._protocols.get(underlying_id)
        if underlying is not None and underlying.player is not None:
            player_id: str | None = underlying.player.player_id
        elif stored := self.mass.config.get_protocol_record(underlying_id):
            player_id = stored.get("player_id")
        else:
            player_id = None
        if (
            not player_id
            or player_id in self._removing
            # a protocol of the domain that is away returns to its place
            or self._has_other_protocol_of_domain(protocol, player_id, include_away=True)
        ):
            return None
        return player_id

    def _get_candidate_player_ids(self) -> list[str]:
        """Return the ids of the players a protocol can be matched to: live ones first."""
        stored_ids = [
            player_id
            for player_id, stored in self.mass.config.get(CONF_PLAYERS, {}).items()
            if isinstance(stored, dict) and player_id not in self._players
        ]
        return [*self._players, *stored_ids]

    def _can_attach_to(
        self,
        protocol: PlayerProtocol,
        identity: DeviceIdentity,
        player_id: str,
        attached_only: bool = False,
    ) -> bool:
        """
        Return whether the attach rules allow a protocol on a player.

        A player has at most one protocol per domain, and two protocols that stand for the
        device itself only when one ranks above the other (see _attach_role); no identifier
        of its device may rule the protocol out. A player that only exists while its device
        is connected takes only such a protocol, and such a protocol only such a player.
        Nothing attaches to a player that is being removed.

        :param protocol: The protocol that attaches.
        :param identity: What the protocol reports about its device.
        :param player_id: Id of the player.
        :param attached_only: Only the protocols that are attached to the player count,
            for a protocol that was attached to the player before.
        """
        if player_id in self._removing:
            return False
        player = self._players.get(player_id)
        stored = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if player is None and not isinstance(stored, dict):
            return False
        if self._is_session_only_player(player_id) != protocol.session_only:
            return False
        player_type = player.type if player else (stored or {}).get("player_type")
        is_own = protocol.role in OWN_PROTOCOL_ROLES
        if player_type == PlayerType.GROUP and not (is_own and attached_only):
            # only its own protocol belongs to a group player
            return False
        protocol_id = protocol.protocol_id
        if self._has_other_protocol_of_domain(protocol, player_id):
            return False
        for other_id in self._get_protocol_ids(player_id):
            if other_id == protocol_id:
                continue
            other = self._protocols.get(other_id)
            other_record = self.mass.config.get_protocol_record(other_id) or {}
            other_provider = other.provider_id if other else str(other_record.get("provider"))
            other_role = player.get_role(other_id) if player and other else other_record.get("role")
            if other is None and attached_only:
                continue
            # a protocol that is away makes room for the one its provider now offers
            # for the same device (see _get_replaced_protocol_id)
            replaceable = other is None and other_provider == protocol.provider_id
            if (
                is_own
                and other_role in OWN_PROTOCOL_ROLES
                and not replaceable
                and _native_rank(other_provider) == _native_rank(protocol.provider_id)
            ):
                return False
        return not vetoes_device(
            identity, self._identity_parts(player_id, protocol_id, attached_only=attached_only)
        )

    def _attach_role(self, protocol: PlayerProtocol, player_id: str) -> ProtocolRole:
        """
        Return the role a protocol is attached to a player in.

        That is the role it reports, unless it stands for the device itself while another
        protocol of the player that does so ranks above it (NATIVE_PRIORITY), attached or
        away, and the user has that one switched on: then it is a control protocol of the
        player.

        :param protocol: The protocol.
        :param player_id: Id of the player it is attached to.
        """
        if protocol.role not in OWN_PROTOCOL_ROLES:
            return protocol.role
        rank = _native_rank(protocol.provider_id)
        for other_id in self._get_protocol_ids(player_id) - {protocol.protocol_id}:
            if other := self._protocols.get(other_id):
                stands_for_device = other.role in OWN_PROTOCOL_ROLES
                other_provider = other.provider_id
            else:
                record = self.mass.config.get_protocol_record(other_id) or {}
                stands_for_device = record.get("role") in OWN_PROTOCOL_ROLES
                other_provider = str(record.get("provider"))
            if (
                stands_for_device
                and _native_rank(other_provider) < rank
                and self.mass.config.is_protocol_switched_on(other_id)
            ):
                return ProtocolRole.CONTROL
        return protocol.role

    async def _attach_in_changed_roles(self, player: Player) -> None:
        """
        Attach the protocols of a player again whose role changed with its other protocols.

        A protocol that stands for the device itself becomes a control protocol when a
        protocol that ranks above it arrives, and stands for it again once that one is
        removed. The caller holds the attach lock.

        :param player: The player.
        """
        for protocol in list(player.protocols.values()):
            if self._attach_role(protocol, player.player_id) != player.get_role(
                protocol.protocol_id
            ):
                self.logger.info(
                    "Protocol %s of player %s attaches again in another role",
                    protocol.protocol_id,
                    player.display_name,
                )
                await self._run_attach(protocol)

    def _get_replaced_protocol_id(self, protocol: PlayerProtocol, player_id: str) -> str | None:
        """
        Return the id of the protocol of a player that the given protocol takes the place of.

        That is a protocol of the same provider that is not attached: the device came
        back under another id.

        :param protocol: The protocol that attaches to the player.
        :param player_id: Id of the player.
        """
        for other_id in self._get_protocol_ids(player_id):
            if other_id == protocol.protocol_id or other_id in self._protocols:
                continue
            other_record = self.mass.config.get_protocol_record(other_id) or {}
            if other_record.get("provider") == protocol.provider_id:
                return other_id
        return None

    def _link_applies(self, protocol: PlayerProtocol | None, player_id: str | None) -> bool:
        """
        Return whether the stored link of a protocol to a player governs the protocol.

        Not for a protocol that attaches as one that only exists while it is connected,
        while that player is no such player: what the link and its record say is the
        business of that player then. The link stays, for when the protocol connects as an
        output of that player again.

        :param protocol: The protocol object, None when it is not known.
        :param player_id: Id of the player the record of the protocol names.
        """
        if protocol is None or not player_id or not protocol.session_only:
            return True
        return self._is_session_only_player(player_id)

    def _is_session_only_player(self, player_id: str) -> bool:
        """
        Return whether a player only exists while its device is connected.

        :param player_id: Id of the player, live or stored.
        """
        if player := self._players.get(player_id):
            return player.session_only
        stored = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        values = stored.get("values") if isinstance(stored, dict) else None
        return isinstance(values, dict) and bool(values.get(CONF_SESSION_ONLY))

    def _has_other_protocol_of_domain(
        self, protocol: PlayerProtocol, player_id: str, include_away: bool = False
    ) -> bool:
        """
        Return whether a player has an attached protocol of the domain of the given one.

        This prevents a second protocol of the same domain (e.g., a second AirPlay
        instance on the same host) from ending up on the same player.

        :param protocol: The protocol that attaches.
        :param player_id: Id of the player.
        :param include_away: Also count the protocols of the player that are not attached,
            unless the user switched them off.
        """
        domain = protocol.provider.domain
        if include_away:
            for other_id in self._get_protocol_ids(player_id) - {protocol.protocol_id}:
                known = self._protocols.get(other_id) or self._unattached.get(other_id)
                if not self._link_applies(known, player_id):
                    continue
                if other := self._protocols.get(other_id):
                    other_domain = other.provider.domain
                else:
                    record = self.mass.config.get_protocol_record(other_id) or {}
                    if not record.get("enabled", True):
                        continue
                    other_domain = str(record.get("provider") or "").split("--", maxsplit=1)[0]
                if other_domain == domain:
                    return True
            return False
        if not (player := self._players.get(player_id)):
            return False
        # An attached protocol of this domain blocks, even if unavailable.
        # Being offline doesn't make it a different device — it's still occupying
        # this domain slot. The provider should remove stale protocols explicitly.
        return any(
            other.provider.domain == domain and other is not protocol
            for other in player.protocols.values()
        )

    def _get_protocol_ids(self, player_id: str) -> set[str]:
        """
        Return the ids of the protocols that belong to a player, attached or not.

        :param player_id: Id of the player.
        """
        if player := self._players.get(player_id):
            return player.protocol_ids
        return set(self.mass.config.get_protocol_ids_of_player(player_id))

    def _identity_of(self, protocol: PlayerProtocol) -> DeviceIdentity:
        """
        Return what a protocol reports about its device, for identifier matching.

        :param protocol: The protocol to describe.
        """
        return DeviceIdentity(
            identifiers=protocol.identifiers,
            domains=frozenset({protocol.provider.domain}),
            native=protocol.role in (ProtocolRole.NATIVE, ProtocolRole.CONTROL),
        )

    def _identity_parts(
        self, player_id: str, exclude_protocol_id: str | None = None, attached_only: bool = False
    ) -> list[DeviceIdentity]:
        """
        Return what is known about the device of a player, per protocol that reported it.

        An attached protocol reports its identifiers itself; for one that is away the
        identifiers it last reported were stored, without its address: that is handed out
        again to another device while this one is away.

        :param player_id: Id of the player.
        :param exclude_protocol_id: Id of a protocol of the player to leave out.
        :param attached_only: Leave out what the protocols that are away reported.
        """
        parts: list[DeviceIdentity] = []
        domains: set[str] = set()
        for protocol_id in self._get_protocol_ids(player_id):
            if protocol_id == exclude_protocol_id:
                continue
            if protocol := self._protocols.get(protocol_id):
                parts.append(self._identity_of(protocol))
                domains.add(protocol.provider.domain)
                continue
            stored = self.mass.config.get_protocol_record(protocol_id) or {}
            domain = str(stored.get("provider") or "").split("--", maxsplit=1)[0]
            domains.add(domain)
            if attached_only:
                continue
            values = stored.get("values") or {}
            identifiers = {
                (IdentifierType(identifier_type), str(value))
                for identifier_type, value in values.get(CONF_PROTOCOL_IDENTIFIERS) or ()
                if identifier_type != IdentifierType.IP_ADDRESS
            }
            for mac_address in (values.get(CONF_CACHED_ARP_MAC), values.get(CONF_REPORTED_MAC)):
                if mac_address:
                    identifiers.add((IdentifierType.MAC_ADDRESS, str(mac_address)))
            parts.append(
                DeviceIdentity(
                    identifiers=frozenset(identifiers),
                    domains=frozenset({domain}),
                    native=stored.get("role") in (ProtocolRole.NATIVE, ProtocolRole.CONTROL),
                )
            )
        if not attached_only:
            # what a player from before the protocols kept their own identifiers stored
            # for its device: one value per type, of any of its protocols
            player_record = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}") or {}
            player_values = player_record.get("values")
            if not isinstance(player_values, dict):
                player_values = {}
            stored_identifiers = player_values.get(CONF_DEVICE_IDENTIFIERS)
            if isinstance(stored_identifiers, dict) and stored_identifiers:
                parts.append(
                    DeviceIdentity(
                        identifiers=frozenset(
                            (IdentifierType(identifier_type), str(value))
                            for identifier_type, value in stored_identifiers.items()
                            if identifier_type != IdentifierType.IP_ADDRESS
                        ),
                        domains=frozenset(domains),
                        native=False,
                    )
                )
        return parts

    def _merged_identity(self, player_id: str) -> DeviceIdentity:
        """
        Return what the protocols of a player report about its device, as one identity.

        :param player_id: Id of the player.
        """
        parts = self._identity_parts(player_id)
        return DeviceIdentity(
            identifiers=frozenset().union(*(part.identifiers for part in parts)),
            domains=frozenset().union(*(part.domains for part in parts)),
            native=any(part.native for part in parts),
        )

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
        players the one with an id from before the player split is kept.

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

    def _awaits_identity(self, protocol: PlayerProtocol) -> bool:
        """
        Return whether a protocol that matches no player has to wait before it gets its own.

        Only an audio protocol of a device that is not fully identified yet waits: the
        other protocols of its device may still attach, or its own address may still
        resolve to the hardware address that ties it to them. A derived protocol waits
        for the protocol it rides on.

        :param protocol: The protocol that matches no player.
        """
        if protocol.underlying_protocol_id:
            return True
        if protocol.role != ProtocolRole.AUDIO or protocol.session_only:
            return False
        return not has_hardware_mac(self._identity_of(protocol))

    def _hold_protocol(self, protocol: PlayerProtocol) -> None:
        """
        Hold a protocol until it is known which player it belongs to.

        :param protocol: The protocol that waits, see _awaits_identity.
        """
        protocol_id = protocol.protocol_id
        self._protocols[protocol_id] = protocol
        if handle := self._pending_protocol_evaluations.pop(protocol_id, None):
            handle.cancel()
        if protocol.underlying_protocol_id:
            # attached as soon as the protocol it rides on is, however long that takes
            self.logger.debug(
                "Protocol %s waits for the protocol it rides on (%s)",
                protocol_id,
                protocol.underlying_protocol_id,
            )
            return
        self.logger.debug("Protocol %s waits for the identifiers of its device", protocol_id)
        self._pending_protocol_evaluations[protocol_id] = self.mass.loop.call_later(
            IDENTITY_COLLECT_WINDOW,
            lambda: self.mass.create_task(self._evaluate_pending_protocol(protocol_id, final=True)),
        )

    async def _evaluate_pending_protocol(self, protocol_id: str, final: bool) -> None:
        """
        Attach a protocol that is held, once it is known which player it belongs to.

        :param protocol_id: Id of the protocol that is held.
        :param final: The wait is over: a protocol that still matches no player becomes
            a player of its own.
        """
        if final:
            self._pending_protocol_evaluations.pop(protocol_id, None)
        async with self._register_lock:
            protocol = self._protocols.get(protocol_id)
            if protocol is None or protocol.player is not None:
                return
            if self.mass.closing or protocol.provider.unloading:
                return
            await self._run_attach(protocol, final)

    def _schedule_held_protocols(self) -> None:
        """
        Look at the held protocols again, now that a protocol was attached to a player or left one.

        An attached protocol can be the one a held protocol rides on, or the one that
        created the player of its device; one that left can free the place it waits for.
        """
        for protocol in list(self._protocols.values()):
            if protocol.player is not None:
                continue
            self.mass.create_task(
                self._evaluate_pending_protocol(protocol.protocol_id, final=False),
                task_id=f"evaluate_protocol_{protocol.protocol_id}",
            )

    def _mint_player(self, protocol: PlayerProtocol) -> str:
        """
        Create the config of a new player for the device of a protocol and return its id.

        A protocol that only exists while its device is connected gets a player with
        its own id, so that player has the same id in every session, and nothing is
        stored for it. A config its provider stored for the protocol beforehand is kept.

        :param protocol: The protocol of a device that is no player yet.
        """
        player_id = protocol.protocol_id if protocol.session_only else ""
        if (
            not player_id
            or player_id in self.mass.config.get(CONF_PLAYERS, {})
            or player_id in self._players
        ):
            player_id = self._new_player_id()
        if protocol.session_only:
            return player_id
        own = protocol if protocol.role in OWN_PROTOCOL_ROLES else None
        self.mass.config.create_player_record(
            player_id,
            provider=protocol.provider_id,
            player_type=protocol.type,
            default_name=get_device_name(own, (protocol,)),
            enabled=protocol.enabled_by_default,
            values={CONF_CREATED_AT: time.time_ns()},
        )
        if self.mass.config.get_protocol_record(protocol.protocol_id) is None:
            self.mass.config.create_protocol_record(
                protocol.protocol_id,
                provider=protocol.provider_id,
                player_id=player_id,
                role=protocol.role,
                default_name=protocol.name,
            )
        return player_id

    def _new_player_id(self) -> str:
        """Return a new player id, which no stored or live player has."""
        stored_players = self.mass.config.get(CONF_PLAYERS, {})
        player_id = ""
        while not player_id or player_id in stored_players or player_id in self._players:
            player_id = f"{PLAYER_ID_PREFIX}{secrets.token_hex(6)}"
        return player_id

    def _store_protocol(self, protocol: PlayerProtocol, player_id: str) -> None:
        """
        Store what has to be known about a protocol while it is away.

        That is the player it belongs to, its role and the identifiers of its device.
        Nothing is stored for a player without a config, which only exists while its
        device is connected: a config of the protocol that names another player stays.

        :param protocol: The protocol.
        :param player_id: Id of the player it belongs to.
        """
        config = self.mass.config
        protocol_id = protocol.protocol_id
        if not isinstance(config.get(f"{CONF_PLAYERS}/{player_id}"), dict):
            return
        stored = config.get_protocol_record(protocol_id)
        if stored is None:
            config.create_protocol_record(
                protocol_id,
                provider=protocol.provider_id,
                player_id=player_id,
                role=protocol.role,
                default_name=protocol.name,
                # a new output of a player that exists follows what its device asks for
                enabled=protocol.enabled_by_default,
            )
            stored = config.get_protocol_record(protocol_id) or {}
        for key, value in (
            ("player_id", player_id),
            ("provider", protocol.provider_id),
            ("role", self._attach_role(protocol, player_id).value),
            ("default_name", protocol.name or stored.get("default_name")),
        ):
            if stored.get(key) != value:
                config.set_protocol_value(protocol_id, key, value)
        if player := self._players.get(player_id):
            player.protocol_ids.add(protocol_id)
        values = stored.get("values") or {}
        identifiers = sorted(
            [identifier_type.value, value]
            for identifier_type, value in (protocol.device_info.identifiers.items())
        )
        mac_address = protocol.device_info.identifiers.get(IdentifierType.MAC_ADDRESS)
        if not (mac_address and is_valid_mac_address(mac_address)):
            mac_address = values.get(CONF_CACHED_ARP_MAC)
        reported_mac = next(
            (
                value
                for identifier_type, value in protocol.identifiers
                if identifier_type == IdentifierType.MAC_ADDRESS and value != mac_address
            ),
            None,
        )
        for key, value in (
            (CONF_PROTOCOL_IDENTIFIERS, identifiers),
            # the resolved MAC, for fast matching on subsequent restarts
            (CONF_CACHED_ARP_MAC, mac_address),
            # the MAC the provider reported where ARP resolved another one, which enables
            # multi-MAC matching for devices with multiple interfaces (e.g., WiFi + Ethernet)
            (CONF_REPORTED_MAC, reported_mac),
            # the derived-transport edge, so a derived output keeps its base reference
            # also while it is not attached
            (CONF_UNDERLYING_PLAYER_ID, protocol.underlying_protocol_id),
        ):
            if values.get(key) != value:
                config.set(f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}/values/{key}", value)

    def _take_over_protocol_record(
        self, replaced_id: str, protocol_id: str, player_id: str
    ) -> None:
        """
        Hand the config of a protocol that is away to the protocol that takes its place.

        The device came back under another protocol id: the new protocol inherits the
        settings and setup data it has none of itself, and what the player stored about
        the old one follows.

        :param replaced_id: Id of the protocol that is replaced.
        :param protocol_id: Id of the protocol that takes its place.
        :param player_id: Id of the player both belong to.
        """
        config = self.mass.config
        self.logger.info(
            "Protocol %s takes the place of %s on player %s", protocol_id, replaced_id, player_id
        )
        replaced = config.get_protocol_record(replaced_id) or {}
        current = config.get_protocol_record(protocol_id) or {}
        record = {**replaced, **current, "protocol_id": protocol_id}
        for key in ("values", "setup_data"):
            record[key] = {**(replaced.get(key) or {}), **(current.get(key) or {})}
        # what identifies the old protocol is not carried over
        for key in (CONF_PROTOCOL_IDENTIFIERS, CONF_CACHED_ARP_MAC, CONF_REPORTED_MAC):
            if key not in (current.get("values") or {}):
                record["values"].pop(key, None)
        config.set(f"{CONF_PLAYER_PROTOCOLS}/{protocol_id}", deepcopy(record))
        config.remove_protocol_record(replaced_id)
        self._replace_protocol_references(player_id, replaced_id, protocol_id)
        if player := self._players.get(player_id):
            player.protocol_ids.discard(replaced_id)
            player.protocol_ids.add(protocol_id)

    def _replace_protocol_references(
        self, player_id: str, protocol_id: str, replacement_id: str | None
    ) -> None:
        """
        Let the output and control choices of a player that name a protocol name another one.

        :param player_id: Id of the player.
        :param protocol_id: Id of the protocol the choices may name.
        :param replacement_id: Id of the protocol that takes its place, None to let the
            choices fall back to their default.
        """
        config = self.mass.config
        for key in (CONF_PREFERRED_OUTPUT_PROTOCOL, CONF_VOLUME_CONTROL, CONF_MUTE_CONTROL):
            if config.get_player_value(player_id, key) != protocol_id:
                continue
            if replacement_id is None:
                config.remove(f"{CONF_PLAYERS}/{player_id}/values/{key}")
            else:
                config.set_player_value(player_id, key, replacement_id)

    def _update_group_memberships(self, player_id: str, replacement_id: str | None = None) -> None:
        """
        Drop a removed player from the groups that list it, or let them name its replacement.

        A removed player is gone for good, so it must not linger in a group and pull a
        device that returns under the same id back in. Its allow-list entry is left alone,
        since an allow-list that runs empty stops restricting at all. A player that is
        merged into another one is replaced by it in both lists.
        Updates the persisted config and keeps any attached group whose membership
        changed in sync.

        :param player_id: Player id that is being removed.
        :param replacement_id: Id of the player it is merged into, None for a removal.
        """
        all_protocol_configs = self.mass.config.get(CONF_PLAYER_PROTOCOLS, {})
        if not isinstance(all_protocol_configs, dict):
            return
        for other_id, other_cfg in all_protocol_configs.items():
            if not isinstance(other_cfg, dict):
                continue
            other_values = other_cfg.get("values")
            if not isinstance(other_values, dict):
                continue
            changed = False
            for key in GROUP_MEMBER_KEYS:
                members = other_values.get(key)
                if not isinstance(members, list) or player_id not in members:
                    continue
                if replacement_id is None and key != CONF_GROUP_MEMBERS:
                    # its entry in an allow-list of a group stays: a list that runs empty
                    # reads as "everyone may join", and a removed player can never join
                    # again anyway
                    continue
                new_members = list(
                    dict.fromkeys(
                        [
                            replacement_id if member_id == player_id else member_id
                            for member_id in members
                        ]
                        if replacement_id
                        else [member_id for member_id in members if member_id != player_id]
                    )
                )
                self.mass.config.set_protocol_value(other_id, key, new_members)
                changed = True
            if changed and (group_protocol := self._protocols.get(other_id)):
                self.mass.create_task(self._reload_group_members(group_protocol))

    async def _reload_group_members(self, protocol: PlayerProtocol) -> None:
        """
        Let a group re-read its member config so its live member list follows along.

        :param protocol: The protocol of the group whose stored member list changed.
        """
        await protocol.on_config_updated()
        protocol.update_state(force_update=True)


def _native_rank(provider_id: str) -> int:
    """
    Return how a provider ranks among the providers whose protocols stand for one device.

    :param provider_id: Instance id of the provider.
    """
    return NATIVE_PRIORITY.get(provider_id.split("--", maxsplit=1)[0], 0)
