"""PlayerManager: Orchestrates all players from player providers."""

import logging
from typing import Dict, List, Optional, Set, Tuple, Union

from music_assistant.constants import (
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    EVENT_PLAYER_ADDED,
    EVENT_PLAYER_REMOVED,
)
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import callback, run_periodic, try_parse_int
from music_assistant.helpers.web import api_route
from music_assistant.models.media_types import MediaItem, MediaType
from music_assistant.models.player import (
    PlaybackState,
    Player,
    PlayerControl,
    PlayerControlType,
)
from music_assistant.models.player_queue import PlayerQueue, QueueItem, QueueOption
from music_assistant.models.provider import PlayerProvider, ProviderType

POLL_INTERVAL = 30

LOGGER = logging.getLogger("player_manager")


class PlayerManager:
    """Several helpers to handle playback through player providers."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self._players = {}
        self._providers = {}
        self._player_queues = {}
        self._controls = {}

    async def setup(self) -> None:
        """Async initialize of module."""
        self.mass.add_job(self.poll_task())

    async def close(self) -> None:
        """Handle stop/shutdown."""
        for player_queue in self._player_queues.values():
            await player_queue.close()
        for player in self:
            await player.on_remove()

    @run_periodic(30)
    async def poll_task(self):
        """Check for updates on players that need to be polled."""
        for player in self:
            if not player.player_state.available:
                continue
            if not player.should_poll:
                continue
            await player.on_poll()

    @property
    def players(self) -> Dict[str, Player]:
        """Return dict of all registered players."""
        return self._players

    @property
    def player_queues(self) -> Dict[str, PlayerQueue]:
        """Return dict of all player queues."""
        return self._player_queues

    @property
    def providers(self) -> Tuple[PlayerProvider]:
        """Return tuple with all loaded player providers."""
        return self.mass.get_providers(ProviderType.PLAYER_PROVIDER)

    def __iter__(self):
        """Iterate over players."""
        return iter(self._players.values())

    @callback
    @api_route("players")
    def get_players(self) -> Tuple[Player]:
        """Return all players in a tuple."""
        return tuple(self._players.values())

    @callback
    @api_route("players/queues")
    def get_player_queues(self) -> Tuple[PlayerQueue]:
        """Return all player queues in a tuple."""
        return tuple(self._player_queues.values())

    @callback
    def get_player(self, player_id: str) -> Player:
        """Return Player by player_id or None if player does not exist."""
        return self._players.get(player_id)

    @callback
    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return provider by player_id or None if player does not exist."""
        player = self.get_player(player_id)
        return self.mass.get_provider(player.provider_id) if player else None

    @callback
    @api_route("players/:player_id/queue")
    def get_player_queue(self, player_id: str) -> PlayerQueue:
        """Return player's queue by player_id or None if player does not exist."""
        player = self.get_player(player_id)
        if not player:
            LOGGER.warning("Player(queue) %s is not available!", player_id)
            return None
        return self._player_queues.get(player.active_queue)

    @callback
    @api_route("players/:queue_id/queue/items")
    def get_player_queue_items(self, queue_id: str) -> Set[QueueItem]:
        """Return player's queueitems by player_id."""
        player_queue = self.get_player_queue(queue_id)
        return player_queue.items if player_queue else {}

    @callback
    @api_route("players/controls/:control_id")
    def get_player_control(self, control_id: str) -> PlayerControl:
        """Return PlayerControl by id."""
        if control_id not in self._controls:
            LOGGER.warning("PlayerControl %s is not available", control_id)
            return None
        return self._controls[control_id]

    @callback
    @api_route("players/controls")
    def get_player_controls(
        self, filter_type: Optional[PlayerControlType] = None
    ) -> Set[PlayerControl]:
        """Return all PlayerControls, optionally filtered by type."""
        return {
            item
            for item in self._controls.values()
            if (filter_type is None or item.type == filter_type)
        }

    # ADD/REMOVE/UPDATE HELPERS

    async def add_player(self, player: Player) -> None:
        """Register a new player or update an existing one."""
        # guard for invalid data or exit in progress
        if not player or self.mass.exit:
            return
        # redirect to update if player is already added
        if player.added_to_mass:
            return await self.trigger_player_update(player.player_id)
        # make sure that the mass instance is set on the player
        player.mass = self.mass
        self._players[player.player_id] = player
        # make sure that the player state is created/updated
        player.player_state.update(player.create_state())
        # Fully initialize only if player is enabled
        if player.enabled:
            await player.on_add()
            player.added_to_mass = True
            # create playerqueue instance
            self._player_queues[player.player_id] = PlayerQueue(
                self.mass, player.player_id
            )
            LOGGER.info(
                "Player added: %s/%s",
                player.provider_id,
                player.name,
            )
            self.mass.signal_event(EVENT_PLAYER_ADDED, player)
        else:
            LOGGER.debug(
                "Ignoring player: %s/%s because it's disabled",
                player.provider_id,
                player.name,
            )

    async def remove_player(self, player_id: str):
        """Remove a player from the registry."""
        self._player_queues.pop(player_id, None)
        player = self._players.pop(player_id, None)
        if player:
            await player.on_remove()
        player_name = player.name if player else player_id
        LOGGER.info("Player removed: %s", player_name)
        self.mass.signal_event(EVENT_PLAYER_REMOVED, {"player_id": player_id})

    async def trigger_player_update(self, player_id: str):
        """Trigger update of an existing player.."""
        player = self.get_player(player_id)
        if player:
            await player.on_poll()

    @api_route("players/controls/:control_id/register")
    async def register_player_control(self, control_id: str, control: PlayerControl):
        """Register a playercontrol with the player manager."""
        control.mass = self.mass
        self._controls[control_id] = control
        LOGGER.info(
            "New PlayerControl (%s) registered: %s\\%s",
            control.type,
            control.provider,
            control.name,
        )
        # update all players using this playercontrol
        for player in self:
            conf = self.mass.config.player_settings[player.player_id]
            if control_id in [
                conf.get(CONF_POWER_CONTROL),
                conf.get(CONF_VOLUME_CONTROL),
            ]:
                self.mass.add_job(self.trigger_player_update(player.player_id))

    @api_route("players/controls/:control_id/update")
    async def update_player_control(self, control_id: str, control: PlayerControl):
        """Update a playercontrol's state on the player manager."""
        if control_id not in self._controls:
            return await self.register_player_control(control_id, control)
        new_state = control.state
        if self._controls[control_id].state == new_state:
            return
        self._controls[control_id].state = new_state
        LOGGER.debug(
            "PlayerControl %s\\%s updated - new state: %s",
            control.provider,
            control.name,
            new_state,
        )
        # update all players using this playercontrol
        for player in self:
            conf = self.mass.config.player_settings[player.player_id]
            if control_id in [
                conf.get(CONF_POWER_CONTROL),
                conf.get(CONF_VOLUME_CONTROL),
            ]:
                self.mass.add_job(self.trigger_player_update(player.player_id))

    # SERVICE CALLS / PLAYER COMMANDS

    @api_route("players/:player_id/play_media")
    async def play_media(
        self,
        player_id: str,
        items: Union[MediaItem, List[MediaItem]],
        queue_opt: QueueOption = QueueOption.Play,
    ):
        """
        Play media item(s) on the given player.

            :param player_id: player_id of the player to handle the command.
            :param items: media item(s) that should be played (single item or list of items)
            :param queue_opt:
                QueueOption.Play -> Insert new items in queue and start playing at inserted position
                QueueOption.Replace -> Replace queue contents with these items
                QueueOption.Next -> Play item(s) after current playing item
                QueueOption.Add -> Append new items at end of the queue
        """
        # a single item or list of items may be provided
        if not isinstance(items, list):
            items = [items]
        queue_items = []
        for media_item in items:
            # collect tracks to play
            if media_item.media_type == MediaType.Artist:
                tracks = await self.mass.music.get_artist_toptracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.Album:
                tracks = await self.mass.music.get_album_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.Playlist:
                tracks = await self.mass.music.get_playlist_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.Radio:
                # single radio
                tracks = [
                    await self.mass.music.get_radio(
                        media_item.item_id, provider_id=media_item.provider
                    )
                ]
            else:
                # single track
                tracks = [
                    await self.mass.music.get_track(
                        media_item.item_id, provider_id=media_item.provider
                    )
                ]
            for track in tracks:
                if not track.available:
                    continue
                queue_item = QueueItem.from_track(track)
                # generate uri for this queue item
                queue_item.uri = "%s/queue/%s/%s" % (
                    self.mass.web.stream_url,
                    player_id,
                    queue_item.queue_item_id,
                )
                queue_items.append(queue_item)
        # turn on player
        await self.cmd_power_on(player_id)
        # load items into the queue
        player_queue = self.get_player_queue(player_id)
        if queue_opt == QueueOption.Replace:
            return await player_queue.load(queue_items)
        if queue_opt in [QueueOption.Play, QueueOption.Next] and len(queue_items) > 100:
            return await player_queue.load(queue_items)
        if queue_opt == QueueOption.Next:
            return await player_queue.insert(queue_items, 1)
        if queue_opt == QueueOption.Play:
            return await player_queue.insert(queue_items, 0)
        if queue_opt == QueueOption.Add:
            return await player_queue.append(queue_items)

    @api_route("players/:player_id/play_uri")
    async def cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the given player.

        Will create a fake track on the queue.

            :param player_id: player_id of the player to handle the command.
            :param uri: Url/Uri that can be played by a player.
        """
        queue_item = QueueItem(item_id=uri, provider="uri", name=uri)
        # generate uri for this queue item
        queue_item.uri = "%s/%s/%s" % (
            self.mass.web.stream_url,
            player_id,
            queue_item.queue_item_id,
        )
        # turn on player
        await self.cmd_power_on(player_id)
        # load item into the queue
        player_queue = self.get_player_queue(player_id)
        return await player_queue.insert([queue_item], 0)

    @api_route("players/:player_id/cmd/stop")
    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        queue_id = player.active_queue
        queue_player = self.get_player(queue_id)
        return await queue_player.cmd_stop()

    @api_route("players/:player_id/cmd/play")
    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        queue_id = player.active_queue
        queue_player = self.get_player(queue_id)
        # unpause if paused else resume queue
        if queue_player.state == PlaybackState.Paused:
            return await queue_player.cmd_play()
        # power on at play request
        await self.cmd_power_on(player_id)
        return await self._player_queues[queue_id].resume()

    @api_route("players/:player_id/cmd/pause")
    async def cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        queue_id = player.active_queue
        queue_player = self.get_player(queue_id)
        return await queue_player.cmd_pause()

    @api_route("players/:player_id/cmd/play_pause")
    async def cmd_play_pause(self, player_id: str):
        """
        Toggle play/pause on given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        if player.state == PlaybackState.Playing:
            return await self.cmd_pause(player_id)
        return await self.cmd_play(player_id)

    @api_route("players/:player_id/cmd/next")
    async def cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        queue_id = player.active_queue
        return await self.get_player_queue(queue_id).next()

    @api_route("players/:player_id/cmd/previous")
    async def cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        queue_id = player.active_queue
        return await self.get_player_queue(queue_id).previous()

    @api_route("players/:player_id/cmd/power_on")
    async def cmd_power_on(self, player_id: str) -> None:
        """
        Send POWER ON command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        player_config = self.mass.config.player_settings[player.player_id]
        # turn on player
        await player.cmd_power_on()
        # player control support
        if player_config.get(CONF_POWER_CONTROL):
            control = self.get_player_control(player_config[CONF_POWER_CONTROL])
            if control:
                await control.set_state(True)

    @api_route("players/:player_id/cmd/power_off")
    async def cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        # send stop if player is playing
        if player.active_queue == player_id and player.state in [
            PlaybackState.Playing,
            PlaybackState.Paused,
        ]:
            await self.cmd_stop(player_id)
        player_config = self.mass.config.player_settings[player.player_id]
        # turn off player
        await player.cmd_power_off()
        # player control support
        if player_config.get(CONF_POWER_CONTROL):
            control = self.get_player_control(player_config[CONF_POWER_CONTROL])
            if control:
                await control.set_state(False)
        # handle group power
        if player.is_group_player:
            # player is group, turn off all childs
            for child_player_id in player.group_childs:
                child_player = self.get_player(child_player_id)
                if child_player and child_player.player_state.powered:
                    self.mass.add_job(self.cmd_power_off(child_player_id))
        else:
            # if this was the last powered player in the group, turn off group
            for parent_player_id in player.group_parents:
                parent_player = self.get_player(parent_player_id)
                if not parent_player or not parent_player.player_state.powered:
                    continue
                has_powered_players = False
                for child_player_id in parent_player.group_childs:
                    if child_player_id == player_id:
                        continue
                    child_player = self.get_player(child_player_id)
                    if child_player and child_player.player_state.powered:
                        has_powered_players = True
                if not has_powered_players:
                    self.mass.add_job(self.cmd_power_off(parent_player_id))

    @api_route("players/:player_id/cmd/power_toggle")
    async def cmd_power_toggle(self, player_id: str):
        """
        Send POWER TOGGLE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        if player.player_state.powered:
            return await self.cmd_power_off(player_id)
        return await self.cmd_power_on(player_id)

    @api_route("players/:player_id/cmd/volume_set/:volume_level?")
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        player = self.get_player(player_id)
        if not player:
            return
        player_config = self.mass.config.player_settings[player.player_id]
        volume_level = try_parse_int(volume_level)
        if volume_level < 0:
            volume_level = 0
        elif volume_level > 100:
            volume_level = 100
        # player control support
        if player_config.get(CONF_VOLUME_CONTROL):
            control = self.get_player_control(player_config[CONF_VOLUME_CONTROL])
            if control:
                await control.set_state(volume_level)
                # just force full volume on actual player if volume is outsourced to volumecontrol
                await player.cmd_volume_set(100)
        # handle group volume
        elif player.is_group_player:
            cur_volume = player.volume_level
            new_volume = volume_level
            volume_dif = new_volume - cur_volume
            if cur_volume == 0:
                volume_dif_percent = 1 + (new_volume / 100)
            else:
                volume_dif_percent = volume_dif / cur_volume
            for child_player_id in player.group_childs:
                if child_player_id == player_id:
                    continue
                child_player = self.get_player(child_player_id)
                if (
                    child_player
                    and child_player.available
                    and child_player.player_state.powered
                ):
                    cur_child_volume = child_player.volume_level
                    new_child_volume = cur_child_volume + (
                        cur_child_volume * volume_dif_percent
                    )
                    await self.cmd_volume_set(child_player_id, new_child_volume)
        # regular volume command
        else:
            await player.cmd_volume_set(volume_level)

    @api_route("players/:player_id/cmd/volume_up")
    async def cmd_volume_up(self, player_id: str):
        """
        Send volume UP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        if player.volume_level <= 10 or player.volume_level >= 90:
            step_size = 2
        else:
            step_size = 5
        new_level = player.volume_level + step_size
        if new_level > 100:
            new_level = 100
        return await self.cmd_volume_set(player_id, new_level)

    @api_route("players/:player_id/cmd/volume_down")
    async def cmd_volume_down(self, player_id: str):
        """
        Send volume DOWN command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id)
        if not player:
            return
        if player.volume_level <= 10 or player.volume_level >= 90:
            step_size = 2
        else:
            step_size = 5
        new_level = player.volume_level - step_size
        if new_level < 0:
            new_level = 0
        return await self.cmd_volume_set(player_id, new_level)

    @api_route("players/:player_id/cmd/volume_mute/:is_muted")
    async def cmd_volume_mute(self, player_id: str, is_muted: bool = False):
        """
        Send MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with the new mute state.
        """
        player = self.get_player(player_id)
        if not player:
            return
        # TODO: handle mute on volumecontrol?
        return await player.cmd_volume_mute(is_muted)

    @api_route("players/:queue_id/queue/cmd/shuffle_enabled/:enable_shuffle?")
    async def player_queue_cmd_set_shuffle(
        self, queue_id: str, enable_shuffle: bool = False
    ):
        """
        Send enable/disable shuffle command to given playerqueue.

            :param queue_id: player_id of the playerqueue to handle the command.
            :param enable_shuffle: bool with the new ahuffle state.
        """
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.set_shuffle_enabled(enable_shuffle)

    @api_route("players/:queue_id/queue/cmd/repeat_enabled/:enable_repeat?")
    async def player_queue_cmd_set_repeat(
        self, queue_id: str, enable_repeat: bool = False
    ):
        """
        Send enable/disable repeat command to given playerqueue.

            :param queue_id: player_id of the playerqueue to handle the command.
            :param enable_repeat: bool with the new ahuffle state.
        """
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.set_repeat_enabled(enable_repeat)

    @api_route("players/:queue_id/queue/cmd/next")
    async def player_queue_cmd_next(self, queue_id: str):
        """
        Send next track command to given playerqueue.

            :param queue_id: player_id of the playerqueue to handle the command.
        """
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.next()

    @api_route("players/:queue_id/queue/cmd/previous")
    async def player_queue_cmd_previous(self, queue_id: str):
        """
        Send previous track command to given playerqueue.

            :param queue_id: player_id of the playerqueue to handle the command.
        """
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.previous()

    @api_route("players/:queue_id/queue/cmd/move/:queue_item_id?/:pos_shift?")
    async def player_queue_cmd_move_item(
        self, queue_id: str, queue_item_id: str, pos_shift: int = 1
    ):
        """
        Move queue item x up/down the queue.

        param pos_shift: move item x positions down if positive value
                         move item x positions up if negative value
                         move item to top of queue as next item if 0
        """
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.move_item(queue_item_id, pos_shift)

    @api_route("players/:queue_id/queue/cmd/play_index/:index?")
    async def play_index(self, queue_id: str, index: Union[int, str]) -> None:
        """Play item at index (or item_id) X in queue."""
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.play_index(index)

    @api_route("players/:queue_id/queue/cmd/clear")
    async def player_queue_cmd_clear(self, queue_id: str, enable_repeat: bool = False):
        """
        Clear all items in player's queue.

            :param queue_id: player_id of the playerqueue to handle the command.
        """
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.clear()

    # OTHER/HELPER FUNCTIONS

    async def get_gain_correct(self, player_id: str, item_id: str, provider_id: str):
        """Get gain correction for given player / track combination."""
        player_conf = self.mass.config.get_player_config(player_id)
        if not player_conf["volume_normalisation"]:
            return 0
        target_gain = int(player_conf["target_volume"])
        track_loudness = await self.mass.database.get_track_loudness(
            item_id, provider_id
        )
        if track_loudness is None:
            # fallback to provider average
            track_loudness = await self.mass.database.get_provider_loudness(provider_id)
            if track_loudness is None:
                # fallback to some (hopefully sane) average value for now
                track_loudness = -8.5
        gain_correct = target_gain - track_loudness
        gain_correct = round(gain_correct, 2)
        return gain_correct
