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

from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.constants import EXTERNAL_SOURCES
from music_assistant.models.player import DeviceInfo, Player

from .constants import EXTERNAL_SOURCE_PROTOCOLS

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia, PlayerSource

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
        self._protocol_player_ids = protocol_player_ids
        super().__init__(provider, player_id)
        # Set player attributes
        self._attr_name = name
        self._attr_device_info = device_info
        # a universal player does not have any features on its own,
        # it delegates to protocol players
        self._attr_supported_features = set()

    @property
    def available(self) -> bool:
        """Return if the player is currently available."""
        return any(
            (p := self.mass.players.get_player(pid)) and p.available
            for pid in self._protocol_player_ids
        )

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        if ext_player := self._get_external_source_protocol_player():
            return ext_player.supported_features
        return self._attr_supported_features

    @property
    def active_source(self) -> str | None:
        """Return the active source of the player."""
        if ext_player := self._get_external_source_protocol_player():
            return ext_player.active_source
        return None

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        if ext_player := self._get_external_source_protocol_player():
            return ext_player.playback_state
        return self._attr_playback_state

    @property
    def elapsed_time(self) -> float | None:
        """Return the elapsed time in (fractional) seconds of the current track."""
        if ext_player := self._get_external_source_protocol_player():
            return ext_player.elapsed_time
        return None

    @property
    def elapsed_time_last_updated(self) -> float | None:
        """Return when the elapsed time was last updated."""
        if ext_player := self._get_external_source_protocol_player():
            return ext_player.elapsed_time_last_updated
        return None

    @property
    def current_media(self) -> PlayerMedia | None:
        """Return the current media being played by the player."""
        if ext_player := self._get_external_source_protocol_player():
            return ext_player.current_media
        return None

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return list of available sources for this player."""
        if ext_player := self._get_external_source_protocol_player():
            # if an external source is active, show sources from that protocol player
            return ext_player.source_list
        return super().source_list

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        if ext_player := self._get_external_source_protocol_player():
            # power off the protocol player to fully stop external apps (e.g. quit cast app)
            if PlayerFeature.POWER in ext_player.supported_features:
                await ext_player.power(False)
            else:
                await ext_player.stop()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        if ext_player := self._get_external_source_protocol_player():
            await ext_player.play()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        if ext_player := self._get_external_source_protocol_player():
            await ext_player.pause()

    async def next_track(self) -> None:
        """Handle NEXT_TRACK command on the player."""
        if ext_player := self._get_external_source_protocol_player():
            await ext_player.next_track()

    async def previous_track(self) -> None:
        """Handle PREVIOUS_TRACK command on the player."""
        if ext_player := self._get_external_source_protocol_player():
            await ext_player.previous_track()

    async def seek(self, position: int) -> None:
        """Handle SEEK command on the player."""
        if ext_player := self._get_external_source_protocol_player():
            await ext_player.seek(position)
            self.mass.players.trigger_player_update(ext_player.player_id, debounce_delay=2)

    def add_protocol_player(self, protocol_player_id: str) -> None:
        """Add a protocol player to this universal player."""
        if protocol_player_id not in self._protocol_player_ids:
            self._protocol_player_ids.append(protocol_player_id)

    def remove_protocol_player(self, protocol_player_id: str) -> None:
        """Remove a protocol player from this universal player."""
        if protocol_player_id in self._protocol_player_ids:
            self._protocol_player_ids.remove(protocol_player_id)

    def _get_external_source_protocol_player(self) -> Player | None:
        """
        Return a chromecast or dlna protocol player that has an external source active.

        Prefers chromecast over dlna because dlna metadata tends to be less reliable.
        """
        if self.active_output_protocol:
            # if an output protocol is active, don't consider external sources
            return None
        result: Player | None = None
        for pid in self._protocol_player_ids:
            protocol_player = self.mass.players.get_player(pid)
            if not protocol_player or not protocol_player.available:
                continue
            if protocol_player.provider.domain not in EXTERNAL_SOURCE_PROTOCOLS:
                continue
            if (
                protocol_player.active_source
                and protocol_player.active_source.lower() in EXTERNAL_SOURCES
                and protocol_player.playback_state != PlaybackState.IDLE
            ):
                # chromecast is preferred, return immediately
                if protocol_player.provider.domain == "chromecast":
                    return protocol_player
                result = result or protocol_player
        return result
