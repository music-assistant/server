"""
State Updates Mixin for the Player Controller.

Publishes the state of a player and acts on what changed in it, lets other parts of
the server follow those changes and polls the protocols that need it.

This module provides the StateUpdatesMixin class which is inherited by
PlayerController to add the state updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import EventType, PlaybackState, PlayerType
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.constants import (
    ATTR_ACTIVE_SOURCE,
    ATTR_AVAILABLE,
    ATTR_ENABLED,
    ATTR_GROUP_MEMBERS,
    ATTR_MUTE_CONTROL,
    ATTR_POWER_CONTROL,
    ATTR_POWERED,
    ATTR_REACHABLE,
    ATTR_SUPPORTED_FEATURES,
    ATTR_VOLUME_CONTROL,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.colors import get_palette_for_url
from music_assistant.models.player import Player, PlayerMedia

from .base import _PlayerControllerBase

if TYPE_CHECKING:
    from collections.abc import Callable


# state keys that carry the current_media playback-position anchor; these only
# change on discrete position events (play/pause/seek/track change/buffer correction)
POSITION_ANCHOR_KEYS = frozenset(
    {
        "current_media.elapsed_time",
        "current_media.elapsed_time_last_updated",
    }
)


# How long a triggered player update waits, so the triggers of one change (a related player,
# a config change) lead to a single state calculation.
PLAYER_UPDATE_DEBOUNCE = 0.25


# How long the players wait to recalculate what they can group with after a protocol attached
# or left, or a grouping changed: these come in bursts (a provider that finds its devices, a
# group that forms member by member), which are then recalculated once.
GROUPING_UPDATE_DELAY = 2.0


# How long the queue of a player waits to follow the player's state, so it follows a burst of
# updates (and the poll ticks while the player plays) once.
QUEUE_UPDATE_DEBOUNCE = 0.5


# How long a new active source of a player that plays over a grouped output protocol has to
# stay before it counts as an external source taking the player over: a device can report a
# passing source while a stream starts or ends.
EXTERNAL_SOURCE_TAKEOVER_DELAY = 5


# Sentinel used to detect omitted optional arguments where ``None`` is a valid value.
_SENTINEL: Any = object()


class StateUpdatesMixin(_PlayerControllerBase):
    """Mixin class providing the state updates for PlayerController."""

    def trigger_player_update(
        self,
        player_id: str,
        force_update: bool = False,
        debounce_delay: float = PLAYER_UPDATE_DEBOUNCE,
    ) -> None:
        """Trigger a (debounced) update for the given player."""
        if self.mass.closing:
            return
        if not (player := self.get_player(player_id)):
            return
        # mark dirty right away (not at execution): a trigger means state the player
        # derives from changed, and a direct update_state call may come in before
        # the debounced one runs
        player.mark_state_dirty()
        task_id = f"player_update_state_{player_id}"
        self.mass.call_later(
            debounce_delay,
            player.update_state,
            force_update=force_update,
            task_id=task_id,
        )

    def on_player_position_jumped(self, player: Player) -> None:
        """
        Handle a discrete jump of a player's corrected playback position.

        Called by a Player when its corrected position moved significantly
        outside regular playback progression (seek or buffer correction). This
        is not an event by itself: it re-bases the active queue's timing on the
        fresh position and nudges related players so derived positions stay in
        sync; current_media then re-anchors from the corrected queue time on
        the follow-up update, which emits the actual update event.
        """
        if self.mass.closing:
            return
        self.mass.player_queues.on_player_elapsed_time_corrected(player)
        self.trigger_player_update(player.player_id)
        self._forward_state_update(player, {})

    def signal_player_state_update(
        self,
        player: Player,
        changed_values: dict[str, tuple[Any, Any]],
        force_update: bool = False,
        skip_forward: bool = False,
        media_position_jumped: bool = False,
    ) -> None:
        """
        Signal a player state update.

        Called by a Player when its state has changed.
        This will update the player state in the controller and signal the event bus.
        """
        player_id = player.player_id
        if self.mass.closing:
            return

        # a player that was just created is published once its config is loaded
        if not player.initialized.is_set():
            return

        # a debounced update can still run on a player that was removed meanwhile
        if self._players.get(player_id) is not player:
            return

        # The current_media position anchor only changes on discrete events
        # (play/pause/seek/track change/buffer correction), so a change set holding
        # only anchor keys represents a position correction rather than a regular
        # state change.
        non_anchor_keys = changed_values.keys() - POSITION_ANCHOR_KEYS
        if len(non_anchor_keys) == 0 and not force_update:
            if not media_position_jumped:
                # anchor adoption without a significant corrected-position change
                return
            # current_media's corrected position jumped (seek or buffer correction
            # reached the current media): emit the full player update below so
            # consumers see the fresh position

        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Player state updated for %s: changed fields: %s",
                player.display_name,
                ", ".join(changed_values.keys()),
            )

        # signal update to the playerqueue
        self.mass.call_later(
            QUEUE_UPDATE_DEBOUNCE,
            self.mass.player_queues.on_player_update,
            player,
            changed_values,
            task_id=f"queue_on_player_update_{player.player_id}",
        )

        # Kick async palette extraction on cold cache. On transition prefetch
        # the next queue item too. Skip players that mirror another player's media
        # (grouped/synced members): their current_media - palette
        # included - is taken wholesale from the owner, so resolving it per member is
        # wasted work that also produces duplicate state updates across the group.
        if (
            not self._mirrors_parent_media(player)
            and (current_media := player.state.current_media)
            and current_media.image_url
        ):
            if current_media.palette is None:
                self._schedule_palette_fetch(player_id, current_media.image_url)
            if "current_media.image_url" in changed_values or "current_media" in changed_values:
                self._schedule_next_queue_item_palette_prefetch(player_id, current_media)

        # handle DSP reload of the leader when grouping/ungrouping
        if ATTR_GROUP_MEMBERS in changed_values:
            prev_group_members, new_group_members = changed_values[ATTR_GROUP_MEMBERS]
            self._handle_group_dsp_change(player, prev_group_members or [], new_group_members)
            # Removed group members also need to be updated since they are no longer part
            # of this group and are available for playback again
            removed_members = set(prev_group_members or []) - set(new_group_members or [])
            for _removed_player_id in removed_members:
                if removed_player := self.get_player(_removed_player_id):
                    removed_player.refresh_state()

        # detect when active_source changes to
        # something external while we have a grouped protocol active
        if ATTR_ACTIVE_SOURCE in changed_values:
            task_id = f"external_source_takeover_{player_id}"
            self.mass.call_later(
                EXTERNAL_SOURCE_TAKEOVER_DELAY,
                self._check_external_source_takeover,
                player,
                task_id=task_id,
            )
        self._restart_idle_count(player, changed_values)
        # only steer into the (relatively expensive) membership cleanup when a field
        # that can require an unsync actually changed - this runs on every state tick
        if changed_values.keys() & {ATTR_AVAILABLE, ATTR_ENABLED, ATTR_POWERED, ATTR_REACHABLE}:
            self._handle_membership_cleanup_on_state_change(player, changed_values)
            self._handle_external_power_off(player, changed_values)

        # enforce volume limits when volume changes externally
        if "volume_level" in changed_values:
            corrected = self._enforce_volume_limits(player)
            # a level set on the device itself makes the reference a group volume change
            # interpolates from obsolete. a member on its way to a level we did send
            # reports levels too, and a group only ever reports what its members are at,
            # so neither of those counts. a correction always is the device's own doing:
            # the levels we command never fall outside the configured range
            if player.state.type != PlayerType.GROUP and (
                corrected or self._unexpired_volume_target(player) is None
            ):
                self._invalidate_group_volume_snapshot(player_id)
        # dispatch to internal state update subscribers (with changed_values)
        self._dispatch_state_update_subscribers(player, changed_values)

        # signal player update on the eventbus
        self.mass.signal_event(EventType.PLAYER_UPDATED, object_id=player_id, data=player)

        # signal a separate PlayerOptionsUpdated event
        if options := changed_values.get("options"):
            self.mass.signal_event(
                EventType.PLAYER_OPTIONS_UPDATED, object_id=player_id, data=options
            )
        # signal player config update event if playerfeatures changed
        # this is temporary needed for the Home Assistant integration which only
        # re-evalues the entity's supported features on a PLAYER_CONFIG_UPDATED event.
        # TODO: Remove this temporary workaround once the HA integration is updated to
        # also re-evaluate supported features on PLAYER_UPDATED events.
        if changed_values.keys() & {
            ATTR_SUPPORTED_FEATURES,
            ATTR_MUTE_CONTROL,
            ATTR_VOLUME_CONTROL,
            ATTR_POWER_CONTROL,
        }:
            self.mass.signal_event(
                EventType.PLAYER_CONFIG_UPDATED, object_id=player_id, data=player.config
            )

        if not skip_forward or force_update:
            self._forward_state_update(player, changed_values)

        # trigger update of all players in a provider if group related fields changed
        # this ensures that calculated fields like can_group_with are updated on all players
        if any(
            key in changed_values
            for key in ("group_members", "synced_to", ATTR_AVAILABLE, ATTR_REACHABLE)
        ):
            self._trigger_provider_players_update(player)

    def subscribe_player_state_update(
        self,
        callback: Callable[[Player, dict[str, tuple[Any, Any]]], None],
    ) -> Callable[[], None]:
        """
        Subscribe to player state update notifications.

        The callback receives the Player and a dict of changed values
        (mapping attribute name to a (previous, new) tuple).

        :param callback: Function to invoke for each player state update.
        :return: An unsubscribe function.
        """
        self._state_update_subscribers.append(callback)

        def _unsub() -> None:
            with suppress(ValueError):
                self._state_update_subscribers.remove(callback)

        return _unsub

    @contextlib.asynccontextmanager
    async def wait_for_player_update(
        self,
        player_id: str,
        attribute_name: str | None = None,
        attribute_value: Any = _SENTINEL,
        timeout: float = 5.0,
    ) -> AsyncIterator[None]:
        """
        Async context manager that waits for a player state update.

        Subscribes to player state updates on entry, runs the body (typically
        the action that triggers the expected update), then waits for a
        matching update on exit. If ``attribute_name`` and ``attribute_value``
        are both provided and the current value already matches at entry, the
        wait is skipped.

        Example::

            async with mass.players.wait_for_player_update(
                player_id, attribute_name="playback_state",
                attribute_value=PlaybackState.IDLE, timeout=5,
            ):
                await mass.players._handle_cmd_stop(player_id)

        :param player_id: The player ID to wait for.
        :param attribute_name: Optional state attribute to watch for changes
            (e.g. ``"playback_state"``). If omitted, any state change satisfies
            the wait.
        :param attribute_value: Optional value the watched attribute must reach.
            Only meaningful in combination with ``attribute_name``.
        :param timeout: Maximum time to wait in seconds.
        """
        update_event = asyncio.Event()

        def _on_state_update(player: Player, changed_values: dict[str, tuple[Any, Any]]) -> None:
            if player.player_id != player_id:
                return
            if attribute_name is None:
                update_event.set()
                return
            if attribute_name not in changed_values:
                return
            if attribute_value is _SENTINEL:
                update_event.set()
                return
            _prev, new_val = changed_values[attribute_name]
            if new_val == attribute_value:
                update_event.set()

        # short-circuit when the desired value is already the current state
        already_satisfied = (
            attribute_name is not None
            and attribute_value is not _SENTINEL
            and (player := self.get_player(player_id)) is not None
            and getattr(player.state, attribute_name, _SENTINEL) == attribute_value
        )

        unsub = self.subscribe_player_state_update(_on_state_update)
        try:
            yield
            if already_satisfied:
                return
            try:
                async with asyncio.timeout(timeout):
                    await update_event.wait()
            except TimeoutError:
                self.logger.debug(
                    "Timed out waiting for player update on %s (attr=%s value=%s)",
                    player_id,
                    attribute_name,
                    attribute_value,
                )
        finally:
            unsub()

    def _mirrors_parent_media(self, player: Player) -> bool:
        """
        Return True if the player's current_media is taken from another player.

        Grouped/synced members mirror their parent's
        current_media (palette included), so they must not resolve it themselves.

        :param player: The player to check.
        """
        state = player.state
        # a self-referential active_group/synced_to is not a real parent (mirror the
        # != self guard of the state derivation), so it must not skip resolution
        parent_id = state.active_group or state.synced_to
        return bool(parent_id and parent_id != player.player_id)

    def _schedule_palette_fetch(
        self, player_id: str, image_url: str | None, *, trigger_update: bool = True
    ) -> None:
        """
        Kick off an async palette extraction for an image URL.

        :param player_id: Player the palette is scoped to (used for task dedup).
        :param image_url: Image URL to extract from. No-op when empty or already cached.
        :param trigger_update: When True, re-emit player state once palette is ready
                               (current track). When False, only warm the cache (prefetch).
        """
        if not image_url:
            return
        # Key the task on the image (not just the player) so a track change always
        # schedules a fetch for the new image instead of being dropped by an in-flight
        # fetch for the previous one; repeated schedules for the same image still dedupe.
        slot = "current" if trigger_update else "next"
        self.mass.create_task(
            self._fetch_palette(player_id, image_url, trigger_update=trigger_update),
            task_id=f"palette_fetch_{player_id}_{slot}_{image_url}",
            abort_existing=False,
        )

    async def _fetch_palette(self, player_id: str, image_url: str, *, trigger_update: bool) -> None:
        palette = await get_palette_for_url(self.mass, image_url)
        if palette is None or not trigger_update:
            return  # prefetch only warms the cache controller; nothing to attach
        player = self.get_player(player_id)
        if player is None:
            return
        current = player.state.current_media
        if current is None or current.image_url != image_url:
            return  # media changed while fetching
        # Carry the palette on player state so the (sync) serialization reads it back.
        player.set_resolved_palette(image_url, palette)
        # Avoid trigger_player_update so a concurrent state-change debounce
        # doesn't cancel our timer via the shared player_update_state task_id.
        self.mass.call_later(
            0,
            player.update_state,
            force_update=True,
            task_id=f"palette_player_update_{player_id}",
        )

    def _schedule_next_queue_item_palette_prefetch(
        self, player_id: str, current_media: PlayerMedia
    ) -> None:
        """Warm the palette cache for the next queue item so it's hot at transition."""
        queue_id, item_id = current_media.source_id, current_media.queue_item_id
        if not queue_id or not item_id:
            return
        next_item = self.mass.player_queues.get_next_item(queue_id, item_id)
        if next_item is None or not next_item.image:
            return
        next_url = self.mass.metadata.get_image_url(
            next_item.image, size=512, prefer_stream_server=True
        )
        self._schedule_palette_fetch(player_id, next_url, trigger_update=False)

    def _forward_state_update(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Forward a player state update to related players (groups, sync parent)."""
        # TODO: make this fan-out change-aware (skip relatives that derive nothing from
        # the changed fields) once reverse indexes for synced_to/active_group exist.
        # Propagate group or sync-leader updates to child players.
        if player.state.group_members:
            for child_player in self.iter_group_members(player, exclude_self=True):
                for protocol in list(child_player.attached_protocols.values()):
                    if player.type == PlayerType.GROUP:
                        protocol.on_group_updated(player, changed_values)
                    else:
                        protocol.on_sync_parent_updated(player, changed_values)
        # update/signal group player(s) when a member updates. A sync leader is a member of the
        # group player that formed the sync group and gaining members of its own does not change
        # that: a group player mirrors its leader, so it depends on exactly these updates.
        for group_player in self._get_player_groups(player):
            if group_protocol := group_player.own_protocol:
                group_protocol.on_group_member_updated(player, changed_values)
            else:
                self.trigger_player_update(group_player.player_id)

        # update/signal manually sync-parent player when child updates
        if (_sync_parent_id := player.state.synced_to) and (
            _sync_parent := self.get_player(_sync_parent_id)
        ):
            self.trigger_player_update(_sync_parent.player_id)

    def _trigger_provider_players_update(self, player: Player) -> None:
        """
        Trigger an update of the players that share a provider with the given player.

        :param player: The player whose grouping or availability changed.
        """
        for protocol in player.attached_protocols.values():
            for related in self.iter_protocols(protocol.provider.instance_id):
                if related.player is not None and related.player is not player:
                    self.trigger_player_update(
                        related.player.player_id, debounce_delay=GROUPING_UPDATE_DELAY
                    )

    def _dispatch_state_update_subscribers(
        self, player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Notify all internal subscribers of a player state update."""
        for subscriber in list(self._state_update_subscribers):
            try:
                subscriber(player, changed_values)
            except Exception:
                self.logger.exception(
                    "Error in player state update subscriber for %s", player.player_id
                )

    async def _wait_for_playback_state(
        self,
        player: Player,
        wanted_state: PlaybackState,
        timeout: float,
        minimal_time: float = 0,
    ) -> None:
        """Wait for a player to reach a playback state, with optional minimum wait time."""
        start_timestamp = time.time()
        async with self.wait_for_player_update(
            player.player_id,
            attribute_name="playback_state",
            attribute_value=wanted_state,
            timeout=timeout,
        ):
            pass
        elapsed = time.time() - start_timestamp
        if elapsed < minimal_time:
            await asyncio.sleep(minimal_time - elapsed)

    async def _poll_players(self) -> None:
        """Background task that polls the protocols of the players for updates."""
        while True:
            for player in list(self._players.values()):
                self._check_idle_power_off(player)
                # if the player is playing, update elapsed time every tick
                # to ensure the queue has accurate details
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.mass.call_later(
                        QUEUE_UPDATE_DEBOUNCE,
                        self.mass.player_queues.on_player_update,
                        player,
                        {"corrected_elapsed_time": (None, player.state.corrected_elapsed_time)},
                        task_id=f"queue_on_player_update_{player.player_id}",
                    )
            for protocol in list(self._protocols.values()):
                # Poll protocol;
                if not protocol.needs_poll:
                    continue
                last_poll = self._last_polls.get(protocol.protocol_id, 0.0)
                if (self.mass.loop.time() - last_poll) < protocol.poll_interval:
                    continue
                if self._protocols.get(protocol.protocol_id) is not protocol:
                    # detached while the protocols before it were polled
                    continue
                self._last_polls[protocol.protocol_id] = self.mass.loop.time()
                try:
                    await protocol.poll()
                except PlayerUnavailableError:
                    self.logger.debug("Player %s is unavailable", protocol.display_name)
                except Exception as err:
                    self.logger.warning(
                        "Error while requesting latest state from player %s: %s",
                        protocol.display_name,
                        str(err),
                        exc_info=err if self.logger.isEnabledFor(10) else None,
                    )
                # Yield to event loop to prevent blocking
                await asyncio.sleep(0)
            await asyncio.sleep(1)

    def _schedule_update_all_players(self) -> None:
        """
        Schedule a debounced update of all players' state.

        Used when protocols are attached or detached, or players come or go, to ensure all
        existing players update their dynamic properties (like can_group_with) that may have
        changed.
        """
        if self.mass.closing:
            return

        for player in self.iter_players(return_unavailable=True, return_disabled=False):
            self.trigger_player_update(player.player_id, debounce_delay=GROUPING_UPDATE_DELAY)
