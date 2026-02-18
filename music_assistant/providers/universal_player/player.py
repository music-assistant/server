"""
Universal Player implementation.

A virtual player for devices that have no native (vendor-specific) provider in
Music Assistant but support one or more generic streaming protocols such as
AirPlay, Sendspin, Chromecast, or DLNA.

The Universal Player is automatically created when a protocol player with
PlayerType.PROTOCOL is registered, providing a unified interface while delegating
actual playback to the underlying protocol player(s).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlayerFeature

from music_assistant.constants import CONF_PREFERRED_OUTPUT_PROTOCOL
from music_assistant.models.player import DeviceInfo, Player

if TYPE_CHECKING:
    from .provider import UniversalPlayerProvider


class UniversalPlayer(Player):
    """
    Universal Player implementation.

    A virtual player for devices without native Music Assistant support that use
    generic streaming protocols. It does NOT have PLAY_MEDIA capability on its own.
    Playback is always delegated to one of the linked protocol players via the protocol
    linking system.
    """

    def __init__(
        self,
        provider: UniversalPlayerProvider,
        player_id: str,
        name: str,
        device_info: DeviceInfo,
        protocol_player_ids: list[str],
    ) -> None:
        """
        Initialize UniversalPlayer instance.

        :param provider: The UniversalPlayerProvider instance.
        :param player_id: Unique player ID (typically based on MAC address).
        :param name: Display name for the player.
        :param device_info: Device information aggregated from protocol players.
        :param protocol_player_ids: List of protocol player IDs to link.
        """
        super().__init__(provider, player_id)
        self._protocol_player_ids = protocol_player_ids
        # Set player attributes
        self._attr_name = name
        self._attr_device_info = device_info
        # Start as unavailable - will be updated when protocol players are linked
        self._attr_available = False
        # a universal player does not have any features on its own,
        # it delegates to protocol players
        self._attr_supported_features = set()

    @property
    def hidden_by_default(self) -> bool:
        """Return if the player should be hidden in the UI by default."""
        if len(self.linked_output_protocols) == 0:
            # If we have no linked protocols, hide by default
            return True
        if self.device_info.model.lower() == "web browser":  # noqa: SIM103
            # hide web players by default
            return True
        return False

    @property
    def expose_to_ha_by_default(self) -> bool:
        """Return if the player should be exposed to Home Assistant by default."""
        if len(self.linked_output_protocols) == 0:
            # If we have no linked protocols, hide by default
            return False
        if self.device_info.model.lower() == "web browser":  # noqa: SIM103
            # hide web players by default
            return False
        return True

    def _get_control_target(
        self, required_feature: PlayerFeature, require_active: bool = False
    ) -> Player | None:
        """Get the best player to send control commands to.

        Prefers the active output protocol, otherwise uses the first available
        protocol player that supports the needed feature.
        """
        # If we have an active protocol, use that
        if (
            self.active_output_protocol
            and self.active_output_protocol != "native"
            and (protocol_player := self.mass.players.get_player(self.active_output_protocol))
            and required_feature in protocol_player.supported_features
        ):
            return protocol_player

        # If require_active is set, and no active protocol found, return None
        if require_active:
            return None

        # Otherwise, use the first available linked protocol
        for protocol_player_id in self._protocol_player_ids:
            if (
                (protocol_player := self.mass.players.get_player(protocol_player_id))
                and protocol_player.available
                and required_feature in protocol_player.supported_features
            ):
                return protocol_player

        return None

    def update_from_protocol_players(self) -> None:
        """
        Update state from linked protocol players.

        Called to sync state like volume, availability from protocol players.
        """
        # Aggregate availability - available if any protocol is available
        self._attr_available = any(
            (p := self.mass.players.get_player(pid)) and p.available
            for pid in self._protocol_player_ids
        )
        # Get volume from best control target
        if target := self._get_control_target(PlayerFeature.VOLUME_SET):
            if target.volume_level is not None:
                self._attr_volume_level = target.volume_level
        if target := self._get_control_target(PlayerFeature.VOLUME_MUTE):
            if target.volume_muted is not None:
                self._attr_volume_muted = target.volume_muted

        self.update_state()

    def add_protocol_player(self, protocol_player_id: str) -> None:
        """Add a protocol player to this universal player."""
        if protocol_player_id not in self._protocol_player_ids:
            self._protocol_player_ids.append(protocol_player_id)

    def remove_protocol_player(self, protocol_player_id: str) -> None:
        """Remove a protocol player from this universal player."""
        if protocol_player_id in self._protocol_player_ids:
            self._protocol_player_ids.remove(protocol_player_id)

    def _get_preferred_protocol_player(self) -> Player | None:
        """
        Get the preferred protocol player for this universal player.

        Selection priority:
        1. Active output protocol (if set and available)
        2. User's preferred output protocol (from settings), fallback to highest
           priority if preferred is not available
        """
        # 1. Active output protocol takes precedence
        if (
            self.active_output_protocol
            and self.active_output_protocol != "native"
            and (protocol_player := self.mass.players.get_player(self.active_output_protocol))
            and protocol_player.available
        ):
            return protocol_player

        # 2. User's preferred output protocol (with fallback to highest priority)
        preferred = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if preferred and (protocol_player := self.mass.players.get_player(str(preferred))):
            if protocol_player.available:
                return protocol_player

        # Fallback: if user's preferred protocol is not available,
        # use the highest priority available protocol
        for protocol in sorted(self.linked_output_protocols, key=lambda x: x.priority):
            if protocol_player := self.mass.players.get_player(protocol.output_protocol_id):
                if protocol_player.available:
                    return protocol_player

        return None
