"""
Protocol Linking Mixin for the Player Controller.

Handles which player a protocol that attaches belongs to: the attach rules and the
roles of its protocols, the identity of its device, a protocol held until that is known,
the player created for a new device and the stored records of the protocols.

This module provides the ProtocolLinkingMixin class which is inherited by
PlayerController to add these capabilities.
"""

from __future__ import annotations

import re
import secrets
import time
from copy import deepcopy
from typing import TYPE_CHECKING

from music_assistant_models.enums import (
    IdentifierType,
    PlayerType,
    ProtocolRole,
)

from music_assistant.constants import (
    CONF_CACHED_ARP_MAC,
    CONF_CREATED_AT,
    CONF_DEVICE_IDENTIFIERS,
    CONF_GROUP_MEMBERS,
    CONF_MUTE_CONTROL,
    CONF_PLAYER_PROTOCOLS,
    CONF_PLAYERS,
    CONF_PREFERRED_OUTPUT_PROTOCOL,
    CONF_PROTOCOL_IDENTIFIERS,
    CONF_REPORTED_MAC,
    CONF_SESSION_ONLY,
    CONF_UNDERLYING_PLAYER_ID,
    CONF_VOLUME_CONTROL,
    NATIVE_PRIORITY,
)
from music_assistant.controllers.config.constants import GROUP_MEMBER_KEYS
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

# The format of a minted player id.
MINTED_PLAYER_ID = re.compile(rf"{PLAYER_ID_PREFIX}[0-9a-f]{{12}}")


class ProtocolLinkingMixin(_PlayerControllerBase):
    """
    Mixin class providing the handling of protocols for PlayerController.

    Handles the complex logic of:
    - Finding the player a protocol belongs to via its device identifiers
    - Creating a player for a device that is new
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
        for protocol in list(player.attached_protocols.values()):
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
            for other in player.attached_protocols.values()
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
