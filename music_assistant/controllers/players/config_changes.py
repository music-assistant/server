"""
Config Changes Mixin for the Player Controller.

What a changed config of a player, of one of its protocols or of its DSP does to the
player and to what it plays.

This module provides the ConfigChangesMixin class which is inherited by
PlayerController to add the handling of config changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import (
    ATTR_ENABLED,
    ATTR_GROUP_VOLUME_SNAPSHOT,
    CONF_MAX_VOLUME,
    CONF_MIN_VOLUME,
)
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider

from .base import _PlayerControllerBase

if TYPE_CHECKING:
    from music_assistant_models.config_entries import PlayerConfig


# How long a queue that is restarted for a changed setting waits after its stop before it
# resumes, so the stop has reached the device before the new stream starts.
CONFIG_CHANGE_RESUME_DELAY = 1


class ConfigChangesMixin(_PlayerControllerBase):
    """Mixin class providing the handling of config changes for PlayerController."""

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        min_vol_changed = f"values/{CONF_MIN_VOLUME}" in changed_keys
        max_vol_changed = f"values/{CONF_MAX_VOLUME}" in changed_keys
        if min_vol_changed or max_vol_changed:
            raw_min = config.get_value(CONF_MIN_VOLUME)
            raw_max = config.get_value(CONF_MAX_VOLUME)
            min_vol = int(cast("int", raw_min)) if raw_min is not None else 0
            max_vol = int(cast("int", raw_max)) if raw_max is not None else 100
            if min_vol > max_vol:
                msg = "Minimum volume cannot exceed maximum volume"
                raise InvalidDataError(msg)
        player_id = config.player_id
        player = self._players.get(player_id)
        if ATTR_ENABLED in changed_keys and not config.enabled:
            if player is not None:
                await self._disable_player(player)
            return
        if ATTR_ENABLED in changed_keys and player is None:
            await self._enable_player(player_id)
            return
        if not player:
            return  # a player that is not there takes its config when it comes back

        # ensure player state gets updated with any updated config
        player.set_config(await self.mass.config.get_player_object_config(player))
        if (own := player.own_protocol) is not None:
            # the settings of its own protocol are part of the config of the player
            own.set_config(await self._get_protocol_config(own))
            await own.on_config_updated()
        player.update_state()
        await self._restart_playback_after_config_change(
            player,
            any(
                v.requires_reload
                for v in config.values.values()
                if f"values/{v.key}" in changed_keys
            ),
        )

    async def on_protocol_config_change(self, protocol_id: str, changed_keys: set[str]) -> None:
        """
        Call (by config manager) when the configuration of one protocol of a player changes.

        :param protocol_id: Id of the protocol.
        :param changed_keys: The keys that changed: ``enabled`` for whether the user
            wants the protocol used, ``values/<key>`` for a setting, ``setup_data/<key>``
            for what a setup flow collected.
        """
        protocol = self._protocols.get(protocol_id)
        if ATTR_ENABLED in changed_keys:
            player_id = self.get_player_id_of_protocol(protocol_id)
            if not self.wants_protocol(protocol_id):
                # the user switched this output of the player off
                await self.unregister(protocol_id)
                # a protocol that waits for the place of its domain may take it now
                self._schedule_held_protocols()
                if player_id:
                    # and one it ranked above stands for the device again
                    self._schedule_protocol_roles(player_id)
                return
            if player_id:
                # a protocol switched on again ranks above others again
                self._schedule_protocol_roles(player_id)
            await self._hold_derived_protocols_without_place(protocol_id)
            if protocol is None or protocol.player is None:
                # its provider registers it when it looks for its devices again
                stored = self.mass.config.get_protocol_record(protocol_id) or {}
                provider = self.mass.get_provider(str(stored.get("provider")))
                if isinstance(provider, PlayerProvider):
                    provider.schedule_discovery()
                return
        if protocol is None or (player := protocol.player) is None:
            return
        config = await self._get_protocol_config(protocol)
        protocol.set_config(config)
        await protocol.on_config_updated()
        player.update_state()
        await self._restart_playback_after_config_change(
            player,
            any(
                v.requires_reload
                for v in config.values.values()
                if f"values/{v.key}" in changed_keys
            ),
        )

    async def on_player_dsp_change(
        self, player_id: str, *, after_group_change: bool = False
    ) -> None:
        """
        Restart playback so a changed DSP setup is applied.

        :param player_id: The player whose DSP settings (or group shape) changed.
        :param after_group_change: Whether the call follows a group membership change
            instead of an edit of the DSP settings themselves.
        """
        # signal player provider that the config changed
        if not (player := self.get_player(player_id)):
            return
        if player.playback_state != PlaybackState.PLAYING:
            return
        if active_queue := self.get_active_queue(player):
            # a group change the provider reported can hand this player a queue it is not
            # rendering (a Sonos that becomes coordinator of a group playing another
            # player's queue falls back to its own, idle queue); resuming that would
            # replace the music with the wrong queue. Only checked after a group change;
            # an edit of the DSP settings needs no such evidence.
            if after_group_change and not self.mass.player_queues.is_playing_queue(
                active_queue.queue_id, player
            ):
                self.logger.debug(
                    "Not restarting %s after DSP change: it is not playing queue %s",
                    player.name,
                    active_queue.queue_id,
                )
                return
            self.logger.info("Restarting playback of Player %s after DSP change", player_id)
            # this will restart the queue stream/playback
            self.mass.call_later(0, self._resume_queue_after_dsp_change, active_queue.queue_id)
            return
        if after_group_change and not self._is_ma_managed_source(player, player.active_source):
            # an external source (a tv input, a connect session) has no stream of ours
            # to rebuild; stopping and starting it would only interrupt it
            self.logger.debug(
                "Not restarting %s after DSP change: it is playing external source %s",
                player.name,
                player.active_source,
            )
            return
        self.logger.info("Restarting playback of Player %s after DSP change", player_id)
        # if the player is not using a queue, we need to stop and start playback, which is
        # no command that starts a group
        with self.without_member_power_on():
            await self.cmd_stop(player_id)
            await self.cmd_play(player_id)

    async def _restart_playback_after_config_change(
        self, player: Player, requires_restart: bool
    ) -> None:
        """
        Restart the queue a player is playing, for a changed setting that needs it.

        :param player: The player whose config changed.
        :param requires_restart: Whether a setting changed that only applies to a new stream.
        """
        if not requires_restart or not player.active_source:
            return
        resume_queue = self.mass.player_queues.get(player.active_source)
        # if the PlayerQueue was playing, restart playback
        if resume_queue and resume_queue.state == PlaybackState.PLAYING:
            # always stop first to ensure the player uses the new config
            await self.mass.player_queues.stop(resume_queue.queue_id)
            # a timer keeps the context it is set in: the resume is no command that
            # starts a group
            with self.without_member_power_on():
                self.mass.call_later(
                    CONFIG_CHANGE_RESUME_DELAY,
                    self.mass.player_queues.resume,
                    resume_queue.queue_id,
                    False,
                )

    def _handle_group_dsp_change(
        self, player: Player, prev_group_members: list[str], new_group_members: list[str]
    ) -> None:
        """Handle DSP reload when group membership changes."""
        # reset cached group volume snapshot since membership changed
        player.extra_data.pop(ATTR_GROUP_VOLUME_SNAPSHOT, None)
        if player.synced_to and player.synced_to != player.player_id:
            # a sync child renders its leader's stream, so its own DSP setting cannot
            # require a restart (a leader that just became a child reports its members
            # as gone, which would otherwise read as the group shrinking)
            return
        prev_child_count = len(prev_group_members)
        new_child_count = len(new_group_members)
        is_player_group = player.type == PlayerType.GROUP

        # handle special case for PlayerGroups: since there are no leaders,
        # DSP still always work with a single player in the group.
        multi_device_dsp_threshold = 1 if is_player_group else 0

        prev_is_multiple_devices = prev_child_count > multi_device_dsp_threshold
        new_is_multiple_devices = new_child_count > multi_device_dsp_threshold

        if prev_is_multiple_devices == new_is_multiple_devices:
            return  # no change in multi-device status

        supports_multi_device_dsp = PlayerFeature.MULTI_DEVICE_DSP in player.supported_features

        dsp_enabled: bool
        if player.type == PlayerType.GROUP:
            # Since player groups do not have leaders, we will use the only child
            # that was in the group before and after the change
            if prev_is_multiple_devices:
                if childs := new_group_members:
                    # We shrank the group from multiple players to a single player
                    # So the now only child will control the DSP
                    dsp_enabled = self.mass.config.get_player_dsp_config(childs[0]).enabled
                else:
                    dsp_enabled = False
            elif childs := prev_group_members:
                # We grew the group from a single player to multiple players,
                # let's see if the previous single player had DSP enabled
                dsp_enabled = self.mass.config.get_player_dsp_config(childs[0]).enabled
            else:
                dsp_enabled = False
        else:
            dsp_enabled = self.mass.config.get_player_dsp_config(player.player_id).enabled

        if dsp_enabled and not supports_multi_device_dsp:
            # We now know that the group configuration has changed so:
            # - multi-device DSP is not supported
            # - we switched from a group with multiple players to a single player
            #   (or vice versa)
            # - the leader has DSP enabled
            self.mass.create_task(
                self.mass.players.on_player_dsp_change(player.player_id, after_group_change=True)
            )

    async def _resume_queue_after_dsp_change(self, queue_id: str) -> None:
        """Resume a queue to apply a DSP change, unless it was emptied while waiting for the lock."""
        # a running command (e.g. a leadership transfer) may move the queue away meanwhile
        async with self.get_group_and_player_lock(queue_id):
            queue = self.mass.player_queues.get(queue_id)
            if queue is None or not queue.items:
                self.logger.debug("Not restarting queue %s after DSP change: it is empty", queue_id)
                return
            await self.mass.player_queues.resume(queue_id, False)
