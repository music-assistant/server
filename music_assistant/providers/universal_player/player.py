"""Universal Player implementation.

A virtual player for devices that have no native (vendor-specific) provider in
Music Assistant but support one or more generic streaming protocols such as
AirPlay, Chromecast, or DLNA.

The Universal Player is automatically created when a protocol player with
PlayerType.PROTOCOL is registered, providing a unified interface while delegating
actual playback to the underlying protocol player(s).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.constants import CONF_PREFERRED_OUTPUT_PROTOCOL
from music_assistant.models.player import DeviceInfo, Player

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

    from .provider import UniversalPlayerProvider


class UniversalPlayer(Player):
    """Universal Player implementation.

    A virtual player for devices without native Music Assistant support that use
    generic streaming protocols. It does NOT have PLAY_MEDIA capability - playback
    is always delegated to one of the linked protocol players via the protocol
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
        """Initialize UniversalPlayer instance.

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

        # Universal players aggregate features from linked protocols
        # but do NOT have PLAY_MEDIA - that's handled via protocol linking
        self._attr_supported_features = self._aggregate_features()

    def _aggregate_features(self) -> set[PlayerFeature]:
        """Aggregate supported features from all linked protocol players."""
        features: set[PlayerFeature] = set()

        for protocol_player_id in self._protocol_player_ids:
            if protocol_player := self.mass.players.get(protocol_player_id):
                # Add features that make sense to aggregate
                for feature in protocol_player.supported_features:
                    if feature in (
                        PlayerFeature.VOLUME_SET,
                        PlayerFeature.VOLUME_MUTE,
                        PlayerFeature.POWER,
                        PlayerFeature.PAUSE,
                    ):
                        features.add(feature)

        return features

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the player."""
        # Get base config entries
        # Note: linked_protocol_ids is stored directly in config values, not as a visible entry
        base_entries = await super().get_config_entries(action=action, values=values)
        return list(base_entries)

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to the player.

        Delegates to the active protocol player or best available protocol.
        """
        target_player = self._get_control_target()
        if target_player and PlayerFeature.VOLUME_SET in target_player.supported_features:
            await target_player.volume_set(volume_level)
            self._attr_volume_level = volume_level
            self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Send VOLUME_MUTE command to the player."""
        target_player = self._get_control_target()
        if target_player and PlayerFeature.VOLUME_MUTE in target_player.supported_features:
            await target_player.volume_mute(muted)
            self._attr_volume_muted = muted
            self.update_state()

    async def power(self, powered: bool) -> None:
        """Send POWER command to the player."""
        target_player = self._get_control_target()
        if target_player and PlayerFeature.POWER in target_player.supported_features:
            await target_player.power(powered)
        self._attr_powered = powered
        self.update_state()

    async def play(self) -> None:
        """Handle PLAY command."""
        # Delegate to active protocol player
        if (
            self.active_output_protocol
            and self.active_output_protocol != "native"
            and (protocol_player := self.mass.players.get(self.active_output_protocol))
        ):
            await protocol_player.play()

    async def pause(self) -> None:
        """Handle PAUSE command."""
        if (
            self.active_output_protocol
            and self.active_output_protocol != "native"
            and (protocol_player := self.mass.players.get(self.active_output_protocol))
        ):
            await protocol_player.pause()

    async def stop(self) -> None:
        """Handle STOP command."""
        if (
            self.active_output_protocol
            and self.active_output_protocol != "native"
            and (protocol_player := self.mass.players.get(self.active_output_protocol))
        ):
            await protocol_player.stop()
        self._attr_current_media = None
        self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    def _get_control_target(self) -> Player | None:
        """Get the best player to send control commands to.

        Prefers the active output protocol, otherwise uses the first available
        protocol player that supports the needed feature.
        """
        # If we have an active protocol, use that
        if (
            self.active_output_protocol
            and self.active_output_protocol != "native"
            and (protocol_player := self.mass.players.get(self.active_output_protocol))
        ):
            return protocol_player

        # Otherwise, use the first available linked protocol
        for protocol_player_id in self._protocol_player_ids:
            if protocol_player := self.mass.players.get(protocol_player_id):
                if protocol_player.available:
                    return protocol_player

        return None

    def update_from_protocol_players(self) -> None:
        """Update state from linked protocol players.

        Called to sync state like volume, availability from protocol players.
        """
        # Aggregate availability - available if any protocol is available
        self._attr_available = any(
            (p := self.mass.players.get(pid)) and p.available for pid in self._protocol_player_ids
        )

        # Get volume from best control target
        if target := self._get_control_target():
            if target.volume_level is not None:
                self._attr_volume_level = target.volume_level
            if target.volume_muted is not None:
                self._attr_volume_muted = target.volume_muted

        self.update_state()

    def add_protocol_player(self, protocol_player_id: str) -> None:
        """Add a protocol player to this universal player."""
        if protocol_player_id not in self._protocol_player_ids:
            self._protocol_player_ids.append(protocol_player_id)
            self._attr_supported_features = self._aggregate_features()

    def remove_protocol_player(self, protocol_player_id: str) -> None:
        """Remove a protocol player from this universal player."""
        if protocol_player_id in self._protocol_player_ids:
            self._protocol_player_ids.remove(protocol_player_id)
            self._attr_supported_features = self._aggregate_features()

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
            and (protocol_player := self.mass.players.get(self.active_output_protocol))
            and protocol_player.available
        ):
            return protocol_player

        # 2. User's preferred output protocol (with fallback to highest priority)
        preferred = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_PREFERRED_OUTPUT_PROTOCOL
        )
        if preferred and (protocol_player := self.mass.players.get(str(preferred))):
            if protocol_player.available:
                return protocol_player

        # Fallback: if user's preferred protocol is not available,
        # use the highest priority available protocol
        for protocol in sorted(self.linked_output_protocols, key=lambda x: x.priority):
            if protocol_player := self.mass.players.get(protocol.output_protocol_id):
                if protocol_player.available:
                    return protocol_player

        return None

    @property
    def _supported_features(self) -> set[PlayerFeature]:
        """
        Return the supported features based on the active/preferred protocol.

        Universal players don't have native PLAY_MEDIA capability - playback is always
        delegated to protocol players. Other features are inherited from the
        active/preferred protocol player.
        """
        # Get the preferred protocol player
        protocol_player = self._get_preferred_protocol_player()
        if not protocol_player:
            # No protocol available, return minimal features
            return set()

        # Return features from the protocol player, excluding PLAY_MEDIA
        # (PLAY_MEDIA is handled via protocol linking)
        features = protocol_player._supported_features.copy()
        features.discard(PlayerFeature.PLAY_MEDIA)
        return features
