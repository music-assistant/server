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

from music_assistant.models.protocol_backed_player import ProtocolBackedPlayer

if TYPE_CHECKING:
    from music_assistant.models.player import DeviceInfo

    from .provider import UniversalPlayerProvider


class UniversalPlayer(ProtocolBackedPlayer):
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
        self._protocol_player_ids = protocol_player_ids
        super().__init__(provider, player_id)
        # Set player attributes
        self._attr_name = name
        self._attr_device_info = device_info
        # a universal player does not have any features on its own,
        # it delegates to protocol players
        self._attr_supported_features = set()

    def add_protocol_player(self, protocol_player_id: str) -> None:
        """Add a protocol player to this universal player."""
        if protocol_player_id not in self._protocol_player_ids:
            self._protocol_player_ids.append(protocol_player_id)

    def remove_protocol_player(self, protocol_player_id: str) -> None:
        """Remove a protocol player from this universal player."""
        if protocol_player_id in self._protocol_player_ids:
            self._protocol_player_ids.remove(protocol_player_id)

    def _backing_protocol_player_ids(self) -> list[str]:
        """Return the ids of the protocol players backing this player."""
        return self._protocol_player_ids
