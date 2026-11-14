"""
Outputs Mixin for the Player Controller.

Selects the protocol that renders the audio of a player, makes it the active output
and clears it again once the player stops or an external source takes it over.

This module provides the OutputsMixin class which is inherited by
PlayerController to add the output selection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.constants import CONF_PREFERRED_OUTPUT_PROTOCOL, VERBOSE_LOG_LEVEL
from music_assistant.controllers.players.grouping_plan import first_grouped_protocol
from music_assistant.models.player import Player

from .base import _PlayerControllerBase
from .grouping import apply_group_change
from .grouping_plan import ChangeKind, GroupChange

if TYPE_CHECKING:
    from music_assistant_models.player import OutputProtocol

    from music_assistant.models.player_protocol import PlayerProtocol


# How long the active output protocol of a stopped player waits for the player to report idle
# before it is cleared anyway: a device may keep reporting playing for a while after a stop.
ACTIVE_OUTPUT_CLEAR_TIMEOUT = 10


class OutputsMixin(_PlayerControllerBase):
    """Mixin class providing the output selection for PlayerController."""

    def schedule_active_output_protocol_clear(self, player: Player) -> None:
        """
        Clear the player's active output protocol once it stops playing.

        A device may keep reporting PLAYING for a short while after a stop
        command, so the clear is deferred until the player reports IDLE (with a
        timeout as fallback). Starting a new session cancels the pending clear
        (see Player.set_active_output_protocol).

        :param player: The player whose active output protocol must be cleared.
        """
        # Deduplicated per player via task_id: if a clear is already pending we
        # keep it, so the single tracked task stays cancellable by a new session.
        self.mass.create_task(
            self._clear_active_output_protocol_when_idle(player),
            task_id=f"clear_active_protocol_{player.player_id}",
        )

    def _get_transport_protocol(self, player: Player) -> PlayerProtocol:
        """
        Return the protocol that takes the transport commands for what the device plays itself.

        That is the audio protocol whose app is shown on the player, else its own protocol.

        :param player: The player the command was issued on.
        :raises PlayerUnavailableError: When the player has neither of them attached.
        """
        if protocol := player.foreign_protocol or player.own_protocol:
            return protocol
        msg = f"Player {player.display_name} has no protocol that can take this command"
        raise PlayerUnavailableError(msg)

    async def _clear_active_output_protocol_when_idle(self, player: Player) -> None:
        """Wait for the player to stop playing, then clear its active output protocol."""
        await self._wait_for_playback_state(
            player, PlaybackState.IDLE, timeout=ACTIVE_OUTPUT_CLEAR_TIMEOUT
        )
        player.set_active_output_protocol(None)

    def _check_external_source_takeover(self, player: Player) -> None:
        """
        Handle when an external source takes over playback on a player.

        When a player has an active grouped output protocol (e.g., AirPlay group) and
        an external source (e.g., Spotify Connect, TV input) takes over playback,
        we need to clear the active output protocol and ungroup the protocols.

        This prevents the situation where the player appears grouped via protocol
        but is actually playing from a different source.

        :param player: The player whose active_source changed.
        """
        # Not a takeover if the device itself is not actively playing
        own = player.own_protocol
        if own is None or own.playback_state != PlaybackState.PLAYING:
            return

        # Only relevant if we have an active output protocol (not native)
        if not player.active_output_protocol or player.active_output_protocol == "native":
            return

        new_source = player.active_source

        # Check if new source is external (not MA-managed)
        if self._is_ma_managed_source(player, new_source):
            return

        # Get the active protocol
        protocol_player = player.attached_protocols.get(player.active_output_protocol)
        if not protocol_player:
            return

        # If the source matches the active protocol's domain, it's expected - not a takeover
        # e.g., source "airplay" when using AirPlay protocol is normal
        if new_source and new_source.lower() == protocol_player.provider.domain.lower():
            return

        if (
            new_source
            and new_source.lower() in ("airplay", "cast", "chromecast", "network")
            and protocol_player.provider.domain.lower() == "sendspin"
        ):
            # Special case for Sendspin bridge: if the new source matches cast or airplay and the
            # active protocol is Sendspin, we consider this a normal behavior and not a takeover
            return

        # Confirmed external source takeover
        self.logger.info(
            "External source '%s' took over on %s while playing via protocol %s - "
            "clearing active output protocol and ungrouping",
            new_source,
            player.display_name,
            protocol_player.provider.domain,
        )

        # Set active output protocol to native
        player.set_active_output_protocol("native")

        # Ungroup the protocol (async task)
        self.mass.create_task(
            apply_group_change(
                self,
                GroupChange(kind=ChangeKind.LEAVE_PROTOCOL, target_id=protocol_player.protocol_id),
                protocol=protocol_player,
            )
        )

    async def _activate_output_protocol(self, player: Player, output_protocol_id: str) -> None:
        """
        Make an output the active output of a player, starting at the level of the player.

        :param player: The player whose output is (re)selected.
        :param output_protocol_id: Id of the output that takes the audio, "native" for
            the own protocol of the player.
        """
        volume_control = player.volume_control_for_output(output_protocol_id)
        mute_control = player.mute_control_for_output(output_protocol_id)
        # a control that takes over, or any while the player keeps the level or mute it had
        await self._hand_over_volume(
            player,
            volume_control
            if player.holds_volume(PlayerFeature.VOLUME_SET)
            or volume_control != player.volume_control
            else None,
            mute_control
            if player.holds_volume(PlayerFeature.VOLUME_MUTE) or mute_control != player.mute_control
            else None,
        )
        player.release_held_volume()
        player.set_active_output_protocol(output_protocol_id)

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
            player.name,
        )

        # 1. Check if any output protocol is currently grouped
        if protocol := self._get_grouped_output_protocol(player):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Selected protocol for %s: %s (grouped)",
                player.name,
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
                        player.name,
                    )
                    return own, None
            else:
                for protocol in player.audio_protocols:
                    if protocol.protocol_id == preferred:
                        if protocol.available_for_playback:
                            self.logger.log(
                                VERBOSE_LOG_LEVEL,
                                "Selected protocol for %s: %s (user preference)",
                                player.name,
                                protocol.provider.name,
                            )
                            return protocol, player.get_linked_protocol(protocol.protocol_id)
                        break

        # 3. Use native playback if available
        if own is not None and native_playback:
            self.logger.log(VERBOSE_LOG_LEVEL, "Selected protocol for %s: native", player.name)
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
                        player.name,
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
                    player.name,
                    protocol.provider.name,
                )
                return protocol, player.get_linked_protocol(protocol.protocol_id)

        raise PlayerCommandFailed(f"Player {player.name} has no available output protocols")

    def _get_grouped_output_protocol(self, player: Player) -> PlayerProtocol | None:
        """
        Return the audio protocol that is currently grouped with other players, if any.

        :param player: The player to check the audio protocols of.
        """
        return first_grouped_protocol(player.audio_protocols)

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
            and (protocol := player.attached_protocols.get(player.active_output_protocol))
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
