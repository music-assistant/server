"""
Protocol Linking Mixin for the Player Controller.

Handles all logic for the protocols of a player: which player a protocol that
attaches belongs to, which of its protocols a player plays and is controlled
through, and how a group of players is formed over them.

This module provides the ProtocolLinkingMixin class which is inherited by
PlayerController to add these capabilities.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    IdentifierType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProtocolRole,
)
from music_assistant_models.errors import PlayerCommandFailed

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
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.players.matching import (
    DeviceIdentity,
    has_hardware_mac,
    matches_device,
    vetoes_device,
)
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import OWN_PROTOCOL_ROLES, Player, get_device_name

if TYPE_CHECKING:
    from music_assistant_models.player import OutputProtocol

    from music_assistant import MusicAssistant
    from music_assistant.models.player_protocol import PlayerProtocol

# How long an audio protocol that matches no player waits for the identifiers that tell
# which device it belongs to (an address that is not resolved yet), before it becomes a
# player of its own.
IDENTITY_COLLECT_WINDOW = 15.0

# The prefix of the id of a player that is created for a new device.
PLAYER_ID_PREFIX = "pl"


class ProtocolLinkingMixin:
    """
    Mixin class providing the handling of protocols for PlayerController.

    Handles the complex logic of:
    - Finding the player a protocol belongs to via its device identifiers
    - Creating a player for a device that is new
    - Selecting the best output protocol for playback
    - Translating a group of players to the protocols that carry it

    This mixin expects to be mixed with a class that provides:
    - mass: MusicAssistant instance
    - _players: dict of players
    - _protocols: dict of attached protocols
    - _pending_protocol_evaluations: dict of pending protocol evaluations
    - logger: logging.Logger instance
    """

    # Type hints for attributes provided by the class this mixin is used with
    if TYPE_CHECKING:
        mass: MusicAssistant
        _players: dict[str, Player]
        _protocols: dict[str, PlayerProtocol]
        _unattached: Any
        _removing: set[str]
        _pending_protocol_evaluations: dict[str, asyncio.TimerHandle]
        _register_lock: asyncio.Lock
        logger: logging.Logger
        translation_owner: str

        def get_player(self, player_id: str) -> Player | None: ...  # noqa: D102

        def get_protocol(self, protocol_id: str) -> PlayerProtocol | None: ...  # noqa: D102

        def get_player_by_protocol(  # noqa: D102
            self, protocol_or_player_id: str
        ) -> Player | None: ...

        def wants_protocol(self, protocol_id: str) -> bool: ...  # noqa: D102

        async def _run_attach(self, protocol: PlayerProtocol, final: bool = False) -> None: ...

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
        for player_id in self._get_candidate_player_ids():
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
        has a protocol of the domain of the derived one, attached or not, or when it is
        being removed.

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

        A player has at most one protocol per domain and at most one that stands for the
        device itself, and no identifier of its device may rule the protocol out. A player
        that only exists while its device is connected takes only such a protocol, and such
        a protocol only such a player. Nothing attaches to a player that is being removed.

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
        if player is not None:
            session_only = player.session_only
        else:
            values = (stored or {}).get("values")
            session_only = isinstance(values, dict) and bool(values.get(CONF_SESSION_ONLY))
        if session_only != protocol.session_only:
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
            if is_own and other_role in OWN_PROTOCOL_ROLES and not replaceable:
                return False
        return not vetoes_device(
            identity, self._identity_parts(player_id, protocol_id, attached_only=attached_only)
        )

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

    def _has_other_protocol_of_domain(
        self, protocol: PlayerProtocol, player_id: str, include_away: bool = False
    ) -> bool:
        """
        Return whether a player has an attached protocol of the domain of the given one.

        This prevents a second protocol of the same domain (e.g., a second AirPlay
        instance on the same host) from ending up on the same player.

        :param protocol: The protocol that attaches.
        :param player_id: Id of the player.
        :param include_away: Also count the protocols of the player that are not attached.
        """
        domain = protocol.provider.domain
        if include_away:
            for other_id in self._get_protocol_ids(player_id) - {protocol.protocol_id}:
                if other := self._protocols.get(other_id):
                    other_domain = other.provider.domain
                else:
                    record = self.mass.config.get_protocol_record(other_id) or {}
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
            stored_identifiers = self.mass.config.get(
                f"{CONF_PLAYERS}/{player_id}/values/{CONF_DEVICE_IDENTIFIERS}"
            )
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
        stored_players = self.mass.config.get(CONF_PLAYERS, {})
        player_id = protocol.protocol_id if protocol.session_only else ""
        while not player_id or player_id in stored_players or player_id in self._players:
            player_id = f"{PLAYER_ID_PREFIX}{secrets.token_hex(6)}"
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

    def _store_protocol(self, protocol: PlayerProtocol, player_id: str) -> None:
        """
        Store what has to be known about a protocol while it is away.

        That is the player it belongs to, its role and the identifiers of its device.
        Nothing is stored for a protocol that only exists while its device is connected,
        unless its player has a config.

        :param protocol: The protocol.
        :param player_id: Id of the player it belongs to.
        """
        config = self.mass.config
        protocol_id = protocol.protocol_id
        stored = config.get_protocol_record(protocol_id)
        if stored is None:
            if not isinstance(config.get(f"{CONF_PLAYERS}/{player_id}"), dict):
                return
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
            ("role", protocol.role.value),
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

    def _update_group_memberships(self, player_id: str) -> None:
        """
        Drop a removed player from the groups that list it as a member.

        The player is gone for good, so it must not linger in a group and pull a device
        that returns under the same id back in. Its allow-list entry is left alone, since
        an allow-list that runs empty stops restricting at all.
        Updates the persisted config and keeps any attached group whose membership
        changed in sync.

        :param player_id: Player id that is being removed.
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
            members = other_values.get(CONF_GROUP_MEMBERS)
            if not isinstance(members, list) or player_id not in members:
                continue
            # its entry in an allow-list of a group stays: a list that runs empty reads as
            # "everyone may join", and a removed player can never join again anyway
            new_members = [member_id for member_id in members if member_id != player_id]
            self.mass.config.set_protocol_value(other_id, CONF_GROUP_MEMBERS, new_members)
            if group_protocol := self._protocols.get(other_id):
                self.mass.create_task(self._reload_group_members(group_protocol))

    async def _reload_group_members(self, protocol: PlayerProtocol) -> None:
        """
        Let a group re-read its member config so its live member list follows along.

        :param protocol: The protocol of the group whose stored member list changed.
        """
        await protocol.on_config_updated()
        protocol.update_state(force_update=True)

    def _select_best_output_protocol(
        self, player: Player
    ) -> tuple[PlayerProtocol, OutputProtocol | None]:
        """
        Select the best available output protocol for a player.

        Selection priority:
        1. Output protocol that is currently grouped/synced with other players.
        2. User's preferred output protocol (from player settings).
        3. Native playback (if the native protocol of the player supports PLAY_MEDIA).
        4. The player's declared default output protocol domain, if available.
        5. Best available protocol by priority.

        Returns tuple of (target_protocol, output_protocol).
        output_protocol is None when using native playback.
        """
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Selecting output protocol for %s",
            player.state.name,
        )

        # 1. Check if any output protocol is currently grouped
        if protocol := self._get_grouped_output_protocol(player):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Selected protocol for %s: %s (grouped)",
                player.state.name,
                protocol.provider.name,
            )
            return protocol, player.get_linked_protocol(protocol.protocol_id)

        own = player.own_protocol
        native_playback = (
            own is not None
            and PlayerFeature.PLAY_MEDIA in own.supported_features
            and own.available_for_playback
        )

        # 2. Check for user's preferred output protocol.
        # The value is only stored while it differs from the entry's default: "native" when a
        # native output is available, otherwise "auto". A player without a native output (e.g. a
        # LinkPlay shell) therefore has no stored preference by default and gets its default
        # output domain applied in step 4.
        preferred = self.mass.config.get_player_value(
            player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if preferred and preferred != "auto":
            if preferred == "native":
                if own is not None and native_playback:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Selected protocol for %s: native (user preference)",
                        player.state.name,
                    )
                    return own, None
            else:
                for protocol in player.audio_protocols:
                    if protocol.protocol_id == preferred:
                        if protocol.available_for_playback:
                            self.logger.log(
                                VERBOSE_LOG_LEVEL,
                                "Selected protocol for %s: %s (user preference)",
                                player.state.name,
                                protocol.provider.name,
                            )
                            return protocol, player.get_linked_protocol(protocol.protocol_id)
                        break

        # 3. Use native playback if available
        if own is not None and native_playback:
            self.logger.log(
                VERBOSE_LOG_LEVEL, "Selected protocol for %s: native", player.state.name
            )
            return own, None

        # 4. Use the player's preferred default protocol domain, if it declares one and a
        # matching audio protocol is available (e.g. a LinkPlay shell prefers DLNA). This
        # never influences grouping; it only steers the default output for playback. "Auto"
        # is the entry default here, so it consistently resolves to this domain default.
        if own is not None and (default_domain := own.default_output_protocol_domain):
            for protocol in player.audio_protocols:
                if protocol.provider.domain != default_domain:
                    continue
                if protocol.available_for_playback:
                    self.logger.log(
                        VERBOSE_LOG_LEVEL,
                        "Selected protocol for %s: %s (default domain %s)",
                        player.state.name,
                        protocol.provider.name,
                        default_domain,
                    )
                    return protocol, player.get_linked_protocol(protocol.protocol_id)

        # 5. Fall back to best protocol by priority
        for protocol in player.audio_protocols:
            if protocol.available_for_playback:
                self.logger.log(
                    VERBOSE_LOG_LEVEL,
                    "Selected protocol for %s: %s (priority-based)",
                    player.state.name,
                    protocol.provider.name,
                )
                return protocol, player.get_linked_protocol(protocol.protocol_id)

        raise PlayerCommandFailed(f"Player {player.state.name} has no available output protocols")

    def _get_grouped_output_protocol(self, player: Player) -> PlayerProtocol | None:
        """
        Return the audio protocol that is currently grouped with other players, if any.

        :param player: The player to check the audio protocols of.
        """
        for protocol in player.audio_protocols:
            if protocol.available_for_playback and self._is_protocol_grouped(protocol):
                return protocol
        return None

    async def _regroup_off_unneeded_protocol(self, player: Player) -> None:
        """
        Regroup the group the player leads on an audio protocol once no member needs it anymore.

        A group moved onto a protocol for one member (e.g. a Sendspin speaker joining AirPlay
        speakers) stays on it after that member left. The remaining members are regrouped the
        way a fresh join would group them, which briefly ungroups them, so only call this
        right before a fresh playback start.

        :param player: The player that is about to start playback.
        """
        protocol = self._get_grouped_output_protocol(player)
        if not protocol or protocol.synced_to or len(protocol.group_members) < 2:
            return
        protocol_member_ids = [
            member_id for member_id in protocol.group_members if member_id != protocol.protocol_id
        ]
        member_ids: list[str] = []
        for protocol_member_id in protocol_member_ids:
            member = self.get_player_by_protocol(protocol_member_id)
            if not member or not member.available:
                return
            member_ids.append(member.player_id)
        protocol_members, native_members, _, protocol_domain = (
            self._translate_members_for_protocols(player, member_ids, None, None)
        )
        placed_members = len(protocol_members) + len(native_members)
        if protocol_domain == protocol.provider.domain or placed_members != len(member_ids):
            return
        # the player plays through its preferred protocol, so the group may only move onto that one
        preferred = self.mass.config.get_player_value(
            player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if (
            preferred
            and preferred not in ("auto", "native")
            and (preferred_protocol := player.get_linked_protocol(str(preferred)))
            and preferred_protocol.protocol_domain != protocol_domain
        ):
            return
        self.logger.info(
            "Regrouping %s off the %s protocol, which none of its members need anymore",
            player.state.name,
            protocol.provider.domain,
        )
        # Removed on the protocol itself: the player's active output protocol may
        # already be cleared after its last session, so a regular removal would not find them.
        await self._forward_protocol_set_members(player, protocol, [], protocol_member_ids)
        if player.active_output_protocol == protocol.protocol_id:
            player.set_active_output_protocol(None)
        await self.mass.players._handle_set_members_with_protocols(player, member_ids, [])

    def _get_control_target(
        self,
        player: Player,
        required_feature: PlayerFeature,
        require_active: bool = False,
    ) -> PlayerProtocol | None:
        """
        Get the best protocol to send audio-path commands to.

        Resolves commands that travel with the audio (enqueue, pause, and an
        announcement the device cannot handle itself), so the output that renders the
        audio outranks the native protocol. Volume and mute resolve through the
        controls of the player instead (:attr:`Player.volume_control`).

        :param player: The player the command was issued on.
        :param required_feature: The feature the resolved target has to support.
        :param require_active: Only accept the output that is already rendering,
            instead of falling back to an idle one.
        """
        # If we have an active protocol, use that
        if (
            player.active_output_protocol
            and player.active_output_protocol != "native"
            and (protocol := player.protocols.get(player.active_output_protocol))
            and required_feature in protocol.supported_features
        ):
            return protocol

        own = player.own_protocol
        own_supports = own is not None and required_feature in own.supported_features

        # if the native protocol renders and supports the required feature, use that
        if player.active_output_protocol == "native" and own_supports:
            return own

        # If require_active is set, and no active protocol found, return None
        if require_active:
            return None

        # if the native protocol supports the required feature, use that
        if own is not None and own_supports and own.available_for_playback:
            return own

        # An output the user explicitly picked owns the audio, so a command that has to
        # start playback on an idle player follows it rather than the priority below.
        # The stored value survives a protocol that moved to another player, so it only
        # counts while it still names one of this player's own outputs.
        preferred = self.mass.config.get_player_value(
            player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if preferred and preferred not in ("auto", "native"):
            for protocol in player.audio_protocols:
                if protocol.protocol_id != preferred:
                    continue
                if (
                    protocol.available_for_playback
                    and required_feature in protocol.supported_features
                ):
                    return protocol
                break

        # Otherwise, use the best available audio protocol, ordered by the same
        # priority that regular playback selection applies.
        for protocol in player.audio_protocols:
            if protocol.available_for_playback and required_feature in protocol.supported_features:
                return protocol

        return None

    def _is_protocol_grouped(self, protocol: PlayerProtocol) -> bool:
        """
        Check if a protocol is currently grouped/synced with other protocols.

        Used to prefer protocols that are actively participating in a group,
        ensuring consistent playback across grouped players.
        """
        is_grouped = bool(protocol.synced_to or len(protocol.group_members) > 1)
        if is_grouped:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Protocol %s is grouped",
                protocol.protocol_id,
            )
        return is_grouped

    def _output_protocol_id(self, player: Player, output: OutputProtocol) -> str:
        """
        Return the id of the protocol behind an output of a player.

        :param player: The player.
        :param output: One of its outputs.
        """
        if output.is_native and player.own_protocol is not None:
            return player.own_protocol.protocol_id
        return output.output_protocol_id

    def _native_member_id(self, parent_player: Player, member: Player) -> str:
        """
        Return the id the own protocol of a parent player knows a member by.

        A device's own protocol groups the protocols of its provider; a group player
        lists its members as players.

        :param parent_player: The player leading the group.
        :param member: The player that is (or becomes) a member.
        """
        parent_own = parent_player.own_protocol
        if (
            parent_own is not None
            and parent_own.role != ProtocolRole.VIRTUAL
            and member.own_protocol is not None
        ):
            return member.own_protocol.protocol_id
        return member.player_id

    def _translate_members_to_remove_for_protocols(
        self,
        parent_player: Player,
        player_ids: list[str],
        parent_protocol: PlayerProtocol | None,
        parent_protocol_domain: str | None,
    ) -> tuple[list[str], list[str]]:
        """
        Translate member IDs to remove into protocol and native lists.

        :param parent_player: The parent player to remove members from.
        :param player_ids: List of player IDs to remove.
        :param parent_protocol: The parent's protocol that holds the group, if available.
        :param parent_protocol_domain: The parent's protocol domain if available.
        """
        self.logger.debug(
            "Translating members to remove for %s: player_ids=%s, parent_protocol_domain=%s",
            parent_player.state.name,
            player_ids,
            parent_protocol_domain,
        )
        protocol_members: list[str] = []
        native_members: list[str] = []
        parent_own = parent_player.own_protocol
        native_group_members = parent_own.group_members if parent_own else []

        for child_player_id in player_ids:
            child_player = self.get_player(child_player_id)
            if not child_player:
                continue

            # Check if this member is in the parent's group via protocol
            if parent_protocol_domain and parent_protocol:
                child_output = child_player.get_output_protocol_by_domain(parent_protocol_domain)
                if child_output and child_output.available:
                    child_protocol_id = self._output_protocol_id(child_player, child_output)
                    if child_protocol_id in parent_protocol.group_members:
                        self.logger.debug(
                            "Translating removal: %s -> protocol %s",
                            child_player_id,
                            child_protocol_id,
                        )
                        protocol_members.append(child_protocol_id)
                        continue

            # Check if one of the child's audio protocols is in the parent's native
            # group_members. This handles a native protocol of an audio domain (e.g., a native
            # AirPlay player like Apple TV), which groups the audio protocols of other players
            translated = False
            for protocol in child_player.audio_protocols:
                if protocol.protocol_id in native_group_members:
                    self.logger.debug(
                        "Translating removal (native parent): %s -> protocol %s",
                        child_player_id,
                        protocol.protocol_id,
                    )
                    native_members.append(protocol.protocol_id)
                    translated = True
                    break

            if not translated:
                native_members.append(self._native_member_id(parent_player, child_player))

        return protocol_members, native_members

    def _filter_protocol_members(
        self, member_ids: list[str], protocol: PlayerProtocol
    ) -> list[str]:
        """Filter member IDs to only include protocols from the same protocol domain."""
        return [
            pid
            for pid in member_ids
            if (p := self.get_protocol(pid)) and p.provider.domain == protocol.provider.domain
        ]

    def _filter_native_members(self, member_ids: list[str], parent_player: Player) -> list[str]:
        """Filter member IDs to only include the ones the parent's own protocol can group."""
        parent_own = parent_player.own_protocol
        if parent_own is None:
            return []
        if parent_own.role == ProtocolRole.VIRTUAL:
            # a group player lists its members as players
            return [pid for pid in member_ids if self.get_player(pid)]
        return [
            pid
            for pid in member_ids
            if (p := self.get_protocol(pid))
            and (
                p.provider.instance_id == parent_own.provider.instance_id
                or pid in parent_own.can_group_with
                or p.provider.instance_id in parent_own.can_group_with
            )
        ]

    def _try_child_preferred_protocol(
        self,
        child_player: Player,
        parent_player: Player,
    ) -> tuple[str | None, str | None]:
        """
        Try to use child's preferred output protocol for grouping.

        Returns tuple of (child_protocol_id, protocol_domain) or (None, None).
        """
        child_preferred = self.mass.config.get_player_value(
            child_player.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if not child_preferred or child_preferred in {"auto", "native"}:
            return None, None

        # Find child's preferred protocol, with its current availability
        child_output = None
        for output_protocol in child_player.output_protocols:
            if output_protocol.output_protocol_id == child_preferred:
                child_output = output_protocol
                break

        if not child_output or not child_output.available:
            return None, None

        # Check if parent supports this protocol (including native protocol)
        parent_output = parent_player.get_output_protocol_by_domain(child_output.protocol_domain)
        if not parent_output or not parent_output.available:
            return None, None

        # Check if this protocol supports set_members
        parent_protocol = parent_player.get_output_protocol(parent_output.output_protocol_id)
        if not parent_protocol or PlayerFeature.SET_MEMBERS not in (
            parent_protocol.supported_features
        ):
            return None, None

        return child_output.output_protocol_id, child_output.protocol_domain

    def _can_use_native_grouping(
        self,
        child_player: Player,
        parent_player: Player,
        parent_supports_native: bool,
    ) -> bool:
        """Check if child can be grouped with parent using native grouping."""
        parent_own = parent_player.own_protocol
        child_own = child_player.own_protocol
        if not parent_supports_native or parent_own is None or child_own is None:
            return False
        return (
            parent_own.is_native_group_compatible(child_own)
            or child_own.protocol_id in parent_own.can_group_with
            or child_own.provider.instance_id in parent_own.can_group_with
        )

    def _try_find_common_protocol(
        self, child_player: Player, parent_player: Player
    ) -> tuple[OutputProtocol | None, OutputProtocol | None]:
        """
        Find common protocol that supports set_members.

        Returns tuple of (parent_output, child_output) or (None, None).
        """
        for parent_output in parent_player.output_protocols:
            if not parent_output.available:
                continue
            child_output = child_player.get_output_protocol_by_domain(parent_output.protocol_domain)
            if not child_output or not child_output.available:
                continue
            parent_protocol = parent_player.get_output_protocol(parent_output.output_protocol_id)
            if parent_protocol and PlayerFeature.SET_MEMBERS in parent_protocol.supported_features:
                return parent_output, child_output
        return None, None

    def _parent_has_live_native_session(self, parent_player: Player) -> bool:
        """
        Return True when the parent currently holds a live native playback session.

        The active output protocol lingers for a few seconds after stop, so a non-idle
        playback state is required to distinguish a real session from a just-stopped one.
        """
        return parent_player.active_output_protocol == "native" and (
            parent_player.state.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        )

    def _order_members_for_native_join(
        self,
        player_ids: list[str],
        parent_player: Player,
        parent_supports_native_grouping: bool,
    ) -> list[str]:
        """
        Order members so a live native session can be joined without splitting the group.

        When the parent already holds a live native session, children that cannot group
        natively are evaluated first: they may force a shared protocol for the whole group,
        and processing them before the native-capable children lets those join that same
        protocol instead of being stranded in a separate native sub-group. The order is left
        untouched when the parent is not playing natively, so fresh-group selection is unchanged.

        :param player_ids: The member IDs to be added, in their original order.
        :param parent_player: The parent player being joined.
        :param parent_supports_native_grouping: Whether the parent can group natively.
        """
        if not self._parent_has_live_native_session(parent_player):
            return player_ids
        return sorted(
            player_ids,
            key=lambda pid: bool(
                (child := self.get_player(pid))
                and self._can_use_native_grouping(
                    child, parent_player, parent_supports_native_grouping
                )
            ),
        )

    def _try_join_active_native_session(
        self,
        child_player: Player,
        parent_player: Player,
        parent_protocol_domain: str | None,
        parent_supports_native_grouping: bool,
        native_members: list[str],
    ) -> bool:
        """
        Add the child to native_members if it can join the parent's active native session.

        A child's preferred output protocol must only steer protocol selection when the child
        initiates its own playback; when it joins a parent that is already playing natively it
        should adopt native grouping if compatible, rather than forcing the whole group onto
        the child's preferred protocol. Skipped once a protocol has been selected for the group,
        so mixed batches stay cohesive on a single protocol.

        :param child_player: The player being added to the group.
        :param parent_player: The parent player being joined.
        :param parent_protocol_domain: The protocol domain already selected for the group, if any.
        :param parent_supports_native_grouping: Whether the parent can group natively.
        :param native_members: The native members list to append to when the child joins.
        """
        if not (
            self._parent_has_live_native_session(parent_player)
            and not parent_protocol_domain
            and self._can_use_native_grouping(
                child_player, parent_player, parent_supports_native_grouping
            )
        ):
            return False
        native_members.append(self._native_member_id(parent_player, child_player))
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Joining parent's active native session for %s",
            child_player.state.name,
        )
        return True

    def _translate_native_members_to_protocol(
        self, parent_player: Player, protocol_domain: str, member_ids: list[str]
    ) -> list[str]:
        """
        Translate natively grouped members onto the protocol domain the parent plays through.

        Members that do not have that protocol cannot follow the parent at all and are
        dropped with a warning instead.

        :param parent_player: The parent player the members are grouped with.
        :param protocol_domain: The protocol domain selected for the group.
        :param member_ids: The member IDs that were selected for native grouping.
        """
        translated: list[str] = []
        for member_id in member_ids:
            # a native group's members are listed by the id of their protocol
            member_player = self.get_player_by_protocol(member_id)
            if not member_player:
                continue
            member_output = member_player.get_output_protocol_by_domain(protocol_domain)
            if not member_output or not member_output.available:
                self.logger.warning(
                    "Cannot group %s with %s: the group plays through the %s protocol, "
                    "which %s does not support",
                    member_player.state.name,
                    parent_player.state.name,
                    protocol_domain,
                    member_player.state.name,
                )
                continue
            translated.append(self._output_protocol_id(member_player, member_output))
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Moving %s from native grouping to the %s protocol",
                member_player.state.name,
                protocol_domain,
            )
        return translated

    def _move_native_members_to_group_protocol(
        self,
        parent_player: Player,
        parent_protocol: PlayerProtocol | None,
        parent_protocol_domain: str | None,
        protocol_members: list[str],
        native_members: list[str],
    ) -> None:
        """
        Move the members selected for native grouping onto the protocol the group ended up on.

        Does nothing unless the group ends up on one of the parent's audio protocols while
        the parent's native grouping needs the parent's own stream: only then do those
        members have no session left to attach to.

        :param parent_player: The parent player being joined.
        :param parent_protocol: The protocol selected for the group, if any.
        :param parent_protocol_domain: The protocol domain selected for the group, if any.
        :param protocol_members: The protocol member list the translated IDs are added to.
        :param native_members: The native member IDs, emptied when they are moved over.
        """
        parent_own = parent_player.own_protocol
        if not (
            native_members
            and parent_protocol_domain
            and parent_protocol
            and parent_own is not None
            and parent_protocol is not parent_own
            and parent_own.native_grouping_requires_own_stream
        ):
            return
        protocol_members.extend(
            self._translate_native_members_to_protocol(
                parent_player, parent_protocol_domain, native_members
            )
        )
        native_members.clear()

    def _migrate_stranded_native_members(
        self,
        parent_player: Player,
        parent_protocol: PlayerProtocol,
        protocol_members: list[str],
    ) -> list[str]:
        """
        Move the native members that a switch to the given protocol strands onto that protocol.

        Returns the member IDs that are still attached to the parent's own stream, so the
        caller can release them from it. Empty unless the parent's native grouping attaches
        its members to exactly that stream: only then are they left without anything to play.
        An idle parent counts too, since its group outlives the session and would otherwise
        keep members that stay silent on the next play.

        :param parent_player: The parent player that is about to switch protocol.
        :param parent_protocol: The protocol the parent will render through.
        :param protocol_members: The protocol member list the translated IDs are added to.
        """
        parent_own = parent_player.own_protocol
        if parent_own is None or parent_protocol is parent_own:
            return []
        if not parent_own.native_grouping_requires_own_stream:
            return []
        if parent_player.active_output_protocol not in (None, "native"):
            return []
        stranded = [
            member_id
            for member_id in parent_own.group_members
            if member_id != parent_own.protocol_id
        ]
        for protocol_id in self._translate_native_members_to_protocol(
            parent_player, parent_protocol.provider.domain, stranded
        ):
            if protocol_id not in protocol_members:
                protocol_members.append(protocol_id)
        return stranded

    async def _release_native_members(
        self,
        parent_player: Player,
        parent_protocol: PlayerProtocol,
        stranded_native_members: list[str],
        *,
        stop_session: bool,
    ) -> None:
        """
        Release the given members from the parent's own stream, and stop it when it is live.

        :param parent_player: The parent player whose native grouping is handed over.
        :param parent_protocol: The protocol taking the output over.
        :param stranded_native_members: The members to release, already migrated.
        :param stop_session: Whether the parent still renders the stream they were attached to.
        """
        if (parent_own := parent_player.own_protocol) is None:
            return
        self.logger.debug(
            "Releasing the native members of %s before switching to %s: %s",
            parent_player.state.name,
            parent_protocol.provider.name,
            stranded_native_members,
        )
        # The members already joined the protocol group, so the native session only has to
        # release them and stop. Releasing them first also clears the native group, which
        # keeps a later native playback command from resurrecting it. Both calls take the
        # provider's own lock, so they must run one after the other.
        await parent_own.set_members(player_ids_to_remove=stranded_native_members)
        if stop_session:
            await parent_own.stop()

    def _translate_members_for_protocols(
        self,
        parent_player: Player,
        player_ids: list[str],
        parent_protocol: PlayerProtocol | None,
        parent_protocol_domain: str | None,
    ) -> tuple[list[str], list[str], PlayerProtocol | None, str | None]:
        """
        Translate member IDs to protocol or native IDs.

        The grouping method is picked per member, see _select_grouping_for_member.

        Returns tuple of (protocol_members, native_members, protocol, protocol_domain).
        """
        protocol_members: list[str] = []
        native_members: list[str] = []
        parent_own = parent_player.own_protocol
        parent_supports_native_grouping = (
            parent_own is not None and PlayerFeature.SET_MEMBERS in parent_own.supported_features
        )
        player_ids = self._order_members_for_native_join(
            player_ids, parent_player, parent_supports_native_grouping
        )

        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Translating members for %s: parent_supports_native=%s, parent_protocol=%s (%s)",
            parent_player.state.name,
            parent_supports_native_grouping,
            parent_protocol.protocol_id if parent_protocol else "none",
            parent_protocol_domain or "none",
        )

        for child_player_id in player_ids:
            child_player = self.get_player(child_player_id)
            if not child_player:
                continue

            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Processing child %s (type=%s, protocols=%s)",
                child_player.state.name,
                child_player.state.type,
                [p.protocol_domain for p in child_player.output_protocols],
            )

            parent_protocol, parent_protocol_domain = self._select_grouping_for_member(
                child_player,
                parent_player,
                parent_protocol,
                parent_protocol_domain,
                parent_supports_native_grouping,
                protocol_members,
                native_members,
            )

        # Post-pass: the protocol selected for the group is only known once every child has
        # been processed, so the members picked for native grouping are corrected here.
        self._move_native_members_to_group_protocol(
            parent_player,
            parent_protocol,
            parent_protocol_domain,
            protocol_members,
            native_members,
        )

        return protocol_members, native_members, parent_protocol, parent_protocol_domain

    def _select_grouping_for_member(
        self,
        child_player: Player,
        parent_player: Player,
        parent_protocol: PlayerProtocol | None,
        parent_protocol_domain: str | None,
        parent_supports_native_grouping: bool,
        protocol_members: list[str],
        native_members: list[str],
    ) -> tuple[PlayerProtocol | None, str | None]:
        """
        Pick the grouping method for a single member and add it to the matching member list.

        Selection priority when grouping:
        0. If the parent is already playing natively and the child can be grouped
           natively, join that native session (a child joining an existing group must
           not force the whole group onto its own preferred output protocol)
        1. Try child's preferred output protocol (from player settings)
        2. Try parent's active output protocol (if any and child supports it)
        3. Try native grouping (if parent and child are compatible)
        4. Search for common protocol that supports set_members
        5. Log warning if no option works

        Returns the protocol/domain the group is on, which the picked method may have
        changed.

        :param child_player: The player being added to the group.
        :param parent_player: The parent player being joined.
        :param parent_protocol: The protocol selected for the group so far, if any.
        :param parent_protocol_domain: The protocol domain selected for the group so far, if any.
        :param parent_supports_native_grouping: Whether the parent can group natively.
        :param protocol_members: The protocol member list to append to.
        :param native_members: The native member list to append to.
        """
        # Priority 0: The parent is already playing natively and the child can join
        # that native session directly - adopt it before considering the child's own
        # preferred output protocol.
        if self._try_join_active_native_session(
            child_player,
            parent_player,
            parent_protocol_domain,
            parent_supports_native_grouping,
            native_members,
        ):
            return parent_protocol, parent_protocol_domain

        # Priority 0.5: a player that runs its own multiroom (e.g. a LinkPlay control shell)
        # keeps grouping on its native path rather than routing it through an audio protocol
        # that is merely its preferred playback output. Native compatibility still decides
        # whether this is possible, so an incompatible/cross-backend pair falls through.
        child_own = child_player.own_protocol
        if (
            child_own is not None
            and child_own.prefer_native_grouping
            and self._can_use_native_grouping(
                child_player, parent_player, parent_supports_native_grouping
            )
        ):
            native_members.append(self._native_member_id(parent_player, child_player))
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Using native grouping (preferred) for %s",
                child_player.state.name,
            )
            return parent_protocol, parent_protocol_domain

        # Priority 1: the child's preferred output protocol
        grouped, parent_protocol, parent_protocol_domain = self._try_group_via_preferred_protocol(
            child_player,
            parent_player,
            parent_protocol,
            parent_protocol_domain,
            protocol_members,
        )
        if grouped:
            return parent_protocol, parent_protocol_domain

        # Priority 2: the protocol the group is already on
        grouped, parent_protocol, parent_protocol_domain = self._try_group_via_active_protocol(
            child_player,
            parent_protocol,
            parent_protocol_domain,
            protocol_members,
        )
        if grouped:
            return parent_protocol, parent_protocol_domain

        # Priority 3: native grouping
        if self._can_use_native_grouping(
            child_player, parent_player, parent_supports_native_grouping
        ):
            native_members.append(self._native_member_id(parent_player, child_player))
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Using native grouping for %s",
                child_player.state.name,
            )
            return parent_protocol, parent_protocol_domain

        # Priority 4: a protocol both players share that supports set_members
        grouped, parent_protocol, parent_protocol_domain = self._try_group_via_common_protocol(
            child_player,
            parent_player,
            parent_protocol,
            parent_protocol_domain,
            protocol_members,
        )
        if grouped:
            return parent_protocol, parent_protocol_domain

        # Priority 5: no option worked
        self.logger.warning(
            "Cannot group %s with %s: no compatible grouping method found "
            "(tried: child preferred protocol, parent active protocol, "
            "native grouping, common protocols)",
            child_player.state.name,
            parent_player.state.name,
        )
        return parent_protocol, parent_protocol_domain

    def _try_group_via_preferred_protocol(
        self,
        child_player: Player,
        parent_player: Player,
        parent_protocol: PlayerProtocol | None,
        parent_protocol_domain: str | None,
        protocol_members: list[str],
    ) -> tuple[bool, PlayerProtocol | None, str | None]:
        """
        Try to group the child through the output protocol it prefers in its player settings.

        Only used when the group is not on a protocol yet or is already on that same protocol.
        Returns whether the child was grouped, together with the protocol/domain the
        group is on: the child's preferred protocol may become the group's protocol.

        :param child_player: The player being added to the group.
        :param parent_player: The parent player being joined.
        :param parent_protocol: The protocol selected for the group so far, if any.
        :param parent_protocol_domain: The protocol domain selected for the group so far, if any.
        :param protocol_members: The protocol member list to append to.
        """
        child_protocol_id, protocol_domain = self._try_child_preferred_protocol(
            child_player, parent_player
        )
        if not (
            child_protocol_id
            and protocol_domain
            and (not parent_protocol_domain or protocol_domain == parent_protocol_domain)
        ):
            return False, parent_protocol, parent_protocol_domain

        if not parent_protocol or parent_protocol_domain != protocol_domain:
            parent_output = parent_player.get_output_protocol_by_domain(protocol_domain)
            if parent_output:
                parent_protocol = parent_player.get_output_protocol(
                    parent_output.output_protocol_id
                )
                parent_protocol_domain = protocol_domain
        protocol_members.append(child_protocol_id)
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Using child's preferred protocol %s for %s",
            protocol_domain,
            child_player.state.name,
        )
        return True, parent_protocol, parent_protocol_domain

    def _try_group_via_active_protocol(
        self,
        child_player: Player,
        parent_protocol: PlayerProtocol | None,
        parent_protocol_domain: str | None,
        protocol_members: list[str],
    ) -> tuple[bool, PlayerProtocol | None, str | None]:
        """
        Try to group the child through the protocol the group is already on.

        Returns whether the child was grouped, together with the protocol/domain the
        group is on: the selection is dropped when that protocol cannot group members itself,
        so a later grouping method can select another one.

        :param child_player: The player being added to the group.
        :param parent_protocol: The protocol selected for the group so far, if any.
        :param parent_protocol_domain: The protocol domain selected for the group so far, if any.
        :param protocol_members: The protocol member list to append to.
        """
        if not parent_protocol_domain or not parent_protocol:
            return False, parent_protocol, parent_protocol_domain

        if PlayerFeature.SET_MEMBERS not in parent_protocol.supported_features:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Parent's active protocol %s does not support SET_MEMBERS, "
                "will search for alternative",
                parent_protocol_domain,
            )
            # Drop the selection so a later grouping method can select a new protocol
            return False, None, None

        child_output = child_player.get_output_protocol_by_domain(parent_protocol_domain)
        if not child_output or not child_output.available:
            return False, parent_protocol, parent_protocol_domain

        # For a native protocol of this domain, use the id of that protocol
        # (e.g., a native sendspin web player IS the protocol)
        protocol_members.append(self._output_protocol_id(child_player, child_output))
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Using parent's active protocol %s for %s",
            parent_protocol_domain,
            child_player.state.name,
        )
        return True, parent_protocol, parent_protocol_domain

    def _try_group_via_common_protocol(
        self,
        child_player: Player,
        parent_player: Player,
        parent_protocol: PlayerProtocol | None,
        parent_protocol_domain: str | None,
        protocol_members: list[str],
    ) -> tuple[bool, PlayerProtocol | None, str | None]:
        """
        Try to group the child through a protocol both players share.

        Returns whether the child was grouped, together with the protocol/domain the
        group is on: the shared protocol may become the group's protocol.

        :param child_player: The player being added to the group.
        :param parent_player: The parent player being joined.
        :param parent_protocol: The protocol selected for the group so far, if any.
        :param parent_protocol_domain: The protocol domain selected for the group so far, if any.
        :param protocol_members: The protocol member list to append to.
        """
        parent_output, child_output = self._try_find_common_protocol(child_player, parent_player)
        if not parent_output or not child_output:
            return False, parent_protocol, parent_protocol_domain

        if not parent_protocol or parent_protocol_domain != parent_output.protocol_domain:
            parent_protocol = parent_player.get_output_protocol(parent_output.output_protocol_id)
            if parent_protocol:
                parent_protocol_domain = parent_protocol.provider.domain
        # For a native protocol of this domain, use the id of that protocol
        protocol_members.append(self._output_protocol_id(child_player, child_output))
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Selected common protocol %s for grouping %s with %s",
            parent_output.protocol_domain,
            child_player.state.name,
            parent_player.state.name,
        )
        return True, parent_protocol, parent_protocol_domain

    async def _forward_protocol_set_members(
        self,
        parent_player: Player,
        parent_protocol: PlayerProtocol,
        protocol_members_to_add: list[str],
        protocol_members_to_remove: list[str],
    ) -> None:
        """
        Forward protocol members to a protocol's set_members and manage active output protocol.

        :param parent_player: The parent player.
        :param parent_protocol: The protocol of the parent to forward commands to.
        :param protocol_members_to_add: Protocol IDs to add.
        :param protocol_members_to_remove: Protocol IDs to remove.
        """
        filtered_protocol_add = self._filter_protocol_members(
            protocol_members_to_add, parent_protocol
        )
        filtered_protocol_remove = self._filter_protocol_members(
            protocol_members_to_remove, parent_protocol
        )
        self.logger.debug(
            "Protocol grouping on %s: filtered_add=%s, filtered_remove=%s",
            parent_protocol.protocol_id,
            filtered_protocol_add,
            filtered_protocol_remove,
        )

        if not filtered_protocol_add and not filtered_protocol_remove:
            return

        # Safety check: verify protocol supports SET_MEMBERS
        if PlayerFeature.SET_MEMBERS not in parent_protocol.supported_features:
            self.logger.error(
                "Protocol %s does not support SET_MEMBERS, cannot perform grouping. "
                "This should have been caught earlier in the flow.",
                parent_protocol.protocol_id,
            )
            return

        # A join that switches protocols moves every member along, silencing those without it.
        if filtered_protocol_add and (
            unable_to_follow := self._members_unable_to_follow(parent_player, parent_protocol)
        ):
            added_names = ", ".join(
                player.display_name if (player := self.get_player_by_protocol(pid)) else pid
                for pid in filtered_protocol_add
            )
            stranded_names = ", ".join(unable_to_follow)
            protocol = parent_protocol.provider.manifest.name
            msg = (
                f"Cannot add {added_names} to {parent_player.state.name}: "
                f"{stranded_names} cannot play over {protocol}"
            )
            raise PlayerCommandFailed(
                msg,
                translation_key="member_cannot_follow_protocol",
                translation_owner=self.translation_owner,
                translation_args=[added_names, parent_player.state.name, stranded_names, protocol],
            )

        # Members that ride the parent's own stream are stranded by the protocol switch below,
        # so they join the protocol group in this very same call and are released from the
        # parent's native grouping afterwards.
        stranded_native_members = (
            self._migrate_stranded_native_members(
                parent_player, parent_protocol, filtered_protocol_add
            )
            if filtered_protocol_add
            else []
        )

        # This runs before set_members because a member's own stream starts inside that call
        # and the provider resolves the member's volume control as it starts: unless its player
        # already points at this protocol, that resolution picks another interface of the same
        # device (e.g. its cast side) over the one carrying the audio.
        self._activate_protocol_on_added_children(filtered_protocol_add)

        self.logger.debug(
            "Calling set_members on protocol %s with add=%s, remove=%s",
            parent_protocol.protocol_id,
            filtered_protocol_add,
            filtered_protocol_remove,
        )
        await parent_protocol.set_members(
            player_ids_to_add=filtered_protocol_add or None,
            player_ids_to_remove=filtered_protocol_remove or None,
        )

        self._release_protocol_on_removed_children(filtered_protocol_remove)

        if filtered_protocol_add:
            await self._activate_group_output_protocol(
                parent_player, parent_protocol, stranded_native_members
            )

        self.logger.debug(
            "After set_members, protocol %s state: group_members=%s, synced_to=%s",
            parent_protocol.protocol_id,
            parent_protocol.group_members,
            parent_protocol.synced_to,
        )

    def _activate_protocol_on_added_children(self, protocol_member_ids: list[str]) -> None:
        """
        Point the player of each given protocol member at the protocol carrying the group audio.

        :param protocol_member_ids: The protocol IDs joining the group.
        """
        for child_protocol_id in protocol_member_ids:
            if not (child_protocol := self.get_protocol(child_protocol_id)):
                continue
            child_player = child_protocol.player
            if child_player is None or child_protocol is child_player.own_protocol:
                continue
            if child_player.active_output_protocol == child_protocol_id:
                continue
            self.logger.debug(
                "Setting active output protocol on child %s to %s",
                child_player.state.name,
                child_protocol_id,
            )
            child_player.set_active_output_protocol(child_protocol_id)

    def _release_protocol_on_removed_children(self, protocol_member_ids: list[str]) -> None:
        """Clear the active output protocol a player still holds for a protocol member that left."""
        for child_protocol_id in protocol_member_ids:
            if not (child_protocol := self.get_protocol(child_protocol_id)):
                continue
            child_player = child_protocol.player
            if child_player is None or child_protocol is child_player.own_protocol:
                continue
            if child_player.active_output_protocol == child_protocol_id:
                child_player.set_active_output_protocol(None)

    async def _activate_group_output_protocol(
        self,
        parent_player: Player,
        parent_protocol: PlayerProtocol,
        stranded_native_members: list[str],
    ) -> None:
        """
        Mark the given protocol as the parent's output and hand the playback over to it.

        The handover only runs when the parent is actually switching protocol while it is
        rendering; playback is resumed only for a parent that was playing, so adding a member
        never starts playback on its own. Migrated native members are released regardless,
        because they moved to the protocol group whether or not the parent was rendering.

        :param parent_player: The parent player that just gained protocol members.
        :param parent_protocol: The protocol the members joined.
        :param stranded_native_members: The members left without a stream by the switch.
        """
        previous_protocol = parent_player.active_output_protocol
        was_playing = parent_player.state.playback_state == PlaybackState.PLAYING
        # A paused player still holds its output, so the handover has to run for it too.
        was_rendering = was_playing or parent_player.state.playback_state == PlaybackState.PAUSED

        # Native protocol: the group is on the parent's own protocol
        is_native_protocol = parent_protocol is parent_player.own_protocol
        already_using_native = previous_protocol in (None, "native")
        already_using_this_protocol = previous_protocol == parent_protocol.protocol_id
        switching_protocols = not (
            (is_native_protocol and already_using_native) or already_using_this_protocol
        )

        self.logger.debug(
            "Protocol grouping: is_native=%s, already_native=%s, already_this=%s, "
            "switching=%s, was_rendering=%s",
            is_native_protocol,
            already_using_native,
            already_using_this_protocol,
            switching_protocols,
            was_rendering,
        )

        if not (is_native_protocol and already_using_native):
            parent_player.set_active_output_protocol(parent_protocol.protocol_id)

        handing_over = was_rendering and switching_protocols
        if handing_over:
            self.logger.info(
                "Handing the output of %s over to the %s protocol%s",
                parent_player.state.name,
                parent_protocol.provider.domain,
                " and resuming playback" if was_playing else "",
            )
        if stranded_native_members:
            # The members joined the protocol group in the same call, so their place on the
            # parent's own stream is released either way; only a live session needs stopping.
            await self._release_native_members(
                parent_player,
                parent_protocol,
                stranded_native_members,
                stop_session=handing_over,
            )
        if not handing_over:
            return

        old_parent_members = await self._stop_previous_protocol(
            parent_player, parent_protocol, previous_protocol
        )
        if was_playing:
            await self.mass.players.cmd_resume(parent_player.player_id)
        if old_parent_members:
            self.logger.debug(
                "Re-adding migrated members %s to %s on new protocol",
                old_parent_members,
                parent_player.state.name,
            )
            # Use internal handler because we are already inside a
            # _handle_set_members call chain that holds the play lock.
            await self.mass.players._handle_set_members(
                parent_player,
                player_ids_to_add=old_parent_members,
            )

    async def _stop_previous_protocol(
        self,
        parent_player: Player,
        parent_protocol: PlayerProtocol,
        previous_protocol: str | None,
    ) -> list[str]:
        """
        Stop the protocol the parent was rendering through, ungroup and return its members.

        The returned IDs are player IDs, so the caller can re-add them to the group
        once the new protocol carries the audio. Empty if there is nothing to hand over.

        :param parent_player: The parent player that is switching protocol.
        :param parent_protocol: The protocol taking the output over.
        :param previous_protocol: The parent's previous active output protocol, if any.
        """
        if not (
            old_protocol := self._get_previous_protocol(
                parent_player, previous_protocol, parent_protocol
            )
        ):
            return []
        members = self._get_protocol_group_members(parent_player, old_protocol)
        old_protocol_members = [protocol_id for protocol_id, _ in members]
        old_parent_members = [player_id for _, player_id in members]
        self.logger.debug(
            "Stopping old protocol %s before switching to %s, migrating members: %s",
            old_protocol.protocol_id,
            parent_protocol.protocol_id,
            old_parent_members,
        )
        # Stop the specific protocol itself,
        # bypassing group/sync redirect and queue redirect logic.
        await old_protocol.stop()
        # A stopped protocol keeps its group, which would win the output selection
        # of the resume over the protocol that takes over.
        await self._forward_protocol_set_members(
            parent_player, old_protocol, [], old_protocol_members
        )
        return old_parent_members

    def _members_unable_to_follow(
        self, parent_player: Player, parent_protocol: PlayerProtocol
    ) -> list[str]:
        """
        Return the names of the members a handover to the given protocol would leave silent.

        :param parent_player: The parent player that would hand its output over.
        :param parent_protocol: The protocol that would take the output over.
        """
        if parent_player.state.playback_state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return []
        if not (
            old_protocol := self._get_previous_protocol(
                parent_player, parent_player.active_output_protocol, parent_protocol
            )
        ):
            return []
        protocol_domain = parent_protocol.provider.domain
        names: list[str] = []
        for _, member_id in self._get_protocol_group_members(parent_player, old_protocol):
            member = self.get_player(member_id)
            output = member.get_output_protocol_by_domain(protocol_domain) if member else None
            if not output or not output.available:
                names.append(member.display_name if member else member_id)
        return names

    def _get_previous_protocol(
        self, parent_player: Player, previous_protocol: str | None, parent_protocol: PlayerProtocol
    ) -> PlayerProtocol | None:
        """
        Return the audio protocol a parent renders through before switching to another one.

        :param parent_player: The parent player that switches protocol.
        :param previous_protocol: The parent's active output protocol before the switch.
        :param parent_protocol: The protocol taking the output over.
        """
        if previous_protocol is None or previous_protocol == "native":
            return None
        old_protocol = parent_player.protocols.get(previous_protocol)
        if not old_protocol or old_protocol is parent_protocol:
            return None
        return old_protocol

    def _get_protocol_group_members(
        self, parent_player: Player, protocol: PlayerProtocol
    ) -> list[tuple[str, str]]:
        """
        Return the members of a protocol group, other than the parent, as pairs of IDs.

        Each pair holds the member's protocol ID and the ID of its player.

        :param parent_player: The parent player leading the group.
        :param protocol: The protocol of the parent that holds the group.
        """
        members: list[tuple[str, str]] = []
        for member_id in protocol.group_members:
            if member_id == protocol.protocol_id:
                continue
            if not (member_player := self.get_player_by_protocol(member_id)):
                continue
            if member_player.player_id != parent_player.player_id:
                members.append((member_id, member_player.player_id))
        return members
