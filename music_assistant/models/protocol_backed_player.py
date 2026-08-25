"""
Base class for players that delegate playback to linked protocol players.

A protocol-backed player owns no native audio path: playback, transport state,
current media and volume are provided by one or more linked protocol players
(AirPlay, DLNA, Chromecast, Sendspin, ...). Subclasses only supply the set of
backing protocol player ids and may add their own native capabilities on top
(for example native multiroom grouping).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.constants import EXTERNAL_SOURCES
from music_assistant.models.player import Player

if TYPE_CHECKING:
    from music_assistant_models.enums import RepeatMode
    from music_assistant_models.player import PlayerMedia, PlayerSource

# Protocol domains where an external source (e.g. Spotify Connect) can play
# independently of Music Assistant, so we surface that protocol player's state.
EXTERNAL_SOURCE_PROTOCOLS = {"chromecast", "dlna"}

# Features forwarded from a protocol player that is playing an external source.
# Volume and mute are excluded: the base Player resolves those to the protocol player.
FORWARDED_FEATURES = {
    PlayerFeature.PAUSE,
    PlayerFeature.SEEK,
    PlayerFeature.NEXT_PREVIOUS,
}


class ProtocolBackedPlayer(Player):
    """
    Base for players whose playback is delegated to linked protocol players.

    The player has no PLAY_MEDIA capability of its own; the player controller routes
    playback to one of the linked protocol players. This base surfaces the delegated
    playback/state/media and any external source active on a linked protocol player.
    """

    @property
    def available(self) -> bool:
        """Return if the player is currently available."""
        return any(
            (p := self.mass.players.get_player(pid)) and p.available_for_playback
            for pid in self._backing_protocol_player_ids()
        )

    @property
    def needs_setup(self) -> bool:
        """Return if the player needs setup (a protocol is connected but not set up)."""
        if self.available:
            return False
        return self._get_protocol_player_needing_setup() is not None

    @property
    def setup_reason(self) -> str | None:
        """Return why the player needs setup, or None when it is ready to use."""
        if self.available:
            return None
        if protocol_player := self._get_protocol_player_needing_setup():
            return protocol_player.setup_reason
        return None

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            # Keep this player's own native capabilities (a subclass may add some, e.g.
            # grouping) and add the forwardable transport controls of the external source.
            return self._attr_supported_features | (
                ext_player.supported_features & FORWARDED_FEATURES
            )
        return self._attr_supported_features

    @property
    def active_source(self) -> str | None:
        """Return the active source of the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            return ext_player.active_source
        return None

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            return ext_player.playback_state
        return self._attr_playback_state

    @property
    def elapsed_time(self) -> float | None:
        """Return the elapsed time in (fractional) seconds of the current track."""
        if ext_player := self._get_protocol_player_with_external_source():
            return ext_player.elapsed_time
        return None

    @property
    def elapsed_time_last_updated(self) -> float | None:
        """Return when the elapsed time was last updated."""
        if ext_player := self._get_protocol_player_with_external_source():
            return ext_player.elapsed_time_last_updated
        return None

    @property
    def current_media(self) -> PlayerMedia | None:
        """Return the current media being played by the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            return ext_player.current_media
        if protocol_player := self._get_active_output_protocol_player():
            # while playing through an output protocol player, surface its raw current_media
            # so consumers of this player's raw value (e.g. a sync group mirroring its leader)
            # can resolve the active queue item. Reading the protocol player's .state here would
            # route back through this player's __final_current_media and lose the queue item id.
            return protocol_player.current_media
        return None

    @property
    def source_list(self) -> list[PlayerSource]:
        """Return list of available sources for this player."""
        if ext_player := self._get_protocol_player_with_external_source():
            # if an external source is active, show sources from that protocol player
            return ext_player.source_list
        return super().source_list

    async def stop(self) -> None:
        """Handle STOP command on the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            await ext_player.stop()

    async def play(self) -> None:
        """Handle PLAY command on the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            await ext_player.play()

    async def pause(self) -> None:
        """Handle PAUSE command on the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            await ext_player.pause()

    async def next_track(self) -> None:
        """Handle NEXT_TRACK command on the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            await ext_player.next_track()

    async def previous_track(self) -> None:
        """Handle PREVIOUS_TRACK command on the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            await ext_player.previous_track()

    async def seek(self, position: int) -> None:
        """Handle SEEK command on the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            await ext_player.seek(position)
            self.mass.players.trigger_player_update(ext_player.player_id, debounce_delay=2)

    async def set_shuffle(self, shuffle_enabled: bool) -> None:
        """Handle SET SHUFFLE command on the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            await ext_player.set_shuffle(shuffle_enabled)

    async def set_repeat(self, repeat_mode: RepeatMode) -> None:
        """Handle SET REPEAT command on the player."""
        if ext_player := self._get_protocol_player_with_external_source():
            await ext_player.set_repeat(repeat_mode)

    def _backing_protocol_player_ids(self) -> list[str]:
        """Return the ids of the protocol players backing this player."""
        raise NotImplementedError

    def _get_protocol_player_needing_setup(self) -> Player | None:
        """Return the first connected protocol player that still needs setup, if any."""
        for pid in self._backing_protocol_player_ids():
            protocol_player = self.mass.players.get_player(pid)
            if protocol_player and protocol_player.available and protocol_player.needs_setup:
                return protocol_player
        return None

    def _get_active_output_protocol_player(self) -> Player | None:
        """Return the protocol player currently selected as this player's output, if any."""
        if self.active_output_protocol and self.active_output_protocol != "native":
            return self.mass.players.get_player(self.active_output_protocol)
        return None

    def _get_protocol_player_with_external_source(self) -> Player | None:
        """
        Return a chromecast or dlna protocol player that has an external source active.

        Prefers chromecast over dlna because dlna metadata tends to be less reliable.
        """
        if self.active_output_protocol:
            # if an output protocol is active, don't consider external sources
            return None
        result: Player | None = None
        for pid in self._backing_protocol_player_ids():
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
