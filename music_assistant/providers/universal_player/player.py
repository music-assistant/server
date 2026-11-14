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
    from music_assistant.models.player import DeviceInfo, Player

    from .provider import UniversalPlayerProvider


def get_clean_player_name(protocol_players: list[Player]) -> str | None:
    """
    Get the best display name from protocol players.

    Prefers names from protocols that typically provide user-friendly names
    (Chromecast, DLNA, AirPlay) over those that may use technical identifiers
    (Squeezelite, SendSpin). Filters out names that look like MAC addresses,
    UUIDs, or player IDs.

    :param protocol_players: The protocol players of one device.
    :return: The name, or None when none of the protocol players has a usable one.
    """
    # Protocol priority for name selection (higher priority = better names typically)
    # Chromecast and DLNA usually have good user-configured names
    # AirPlay also provides sensible names
    # Squeezelite and SendSpin may use MAC addresses or technical IDs
    name_priority = {
        "chromecast": 1,
        "airplay": 2,
        "dlna": 3,
        "squeezelite": 4,
        "sendspin": 5,
    }

    def is_valid_name(name: str) -> bool:
        """Check if a name looks like a real user-friendly name, not a technical ID."""
        if not name or len(name) < 2:
            return False
        name_lower = name.lower().replace(":", "").replace("-", "").replace("_", "")
        # Filter out names that look like MAC addresses (12 hex chars)
        if len(name_lower) == 12 and all(c in "0123456789abcdef" for c in name_lower):
            return False
        # Filter out names that look like UUIDs
        if len(name_lower) >= 32 and all(c in "0123456789abcdef" for c in name_lower[:32]):
            return False
        # Filter out names that start with common player ID prefixes
        return not name.lower().startswith(
            ("ap_", "cc_", "dlna_", "sq_", "sendspin_", "universal_")
        )

    # Sort players by protocol priority, then find the first valid name
    sorted_players = sorted(
        protocol_players,
        key=lambda p: name_priority.get(p.provider.domain, 10),
    )

    for player in sorted_players:
        player_name = player.state.name
        if is_valid_name(player_name):
            return player_name

    return None


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

    @property
    def name(self) -> str | None:
        """Return the name of the player."""
        protocol_players = [
            player
            for player_id in self._protocol_player_ids
            if (player := self.mass.players.get_player(player_id))
        ]
        if name := get_clean_player_name(protocol_players):
            # kept, as the name to show while none of them is registered or has a usable
            # name (e.g. a Sendspin player is unregistered when its client disconnects)
            self._attr_name = name
        return self._attr_name

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
