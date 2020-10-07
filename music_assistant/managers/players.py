"""PlayerManager: Orchestrates all players from player providers."""

import logging
from typing import List, Optional

from music_assistant.constants import (
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    EVENT_PLAYER_ADDED,
    EVENT_PLAYER_CONTROL_REGISTERED,
    EVENT_PLAYER_CONTROL_UPDATED,
    EVENT_PLAYER_REMOVED,
    EVENT_REGISTER_PLAYER_CONTROL,
    EVENT_UNREGISTER_PLAYER_CONTROL,
)
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.helpers.util import (
    async_iter_items,
    callback,
    run_periodic,
    try_parse_int,
)
from music_assistant.models.media_types import MediaItem, MediaType, Track
from music_assistant.models.player import (
    PlaybackState,
    Player,
    PlayerControl,
    PlayerControlType,
)
from music_assistant.models.player_queue import PlayerQueue, QueueItem, QueueOption
from music_assistant.models.player_state import PlayerState
from music_assistant.models.provider import PlayerProvider, ProviderType

POLL_INTERVAL = 30

LOGGER = logging.getLogger("player_manager")


class PlayerManager:
    """Several helpers to handle playback through player providers."""

    def __init__(self, mass: MusicAssistantType):
        """Initialize class."""
        self.mass = mass
        self._player_states = {}
        self._providers = {}
        self._player_queues = {}
        self._poll_ticks = 0
        self._controls = {}
        self.mass.add_event_listener(
            self.__handle_websocket_player_control_event,
            [
                EVENT_REGISTER_PLAYER_CONTROL,
                EVENT_UNREGISTER_PLAYER_CONTROL,
                EVENT_PLAYER_CONTROL_UPDATED,
            ],
        )

    async def async_setup(self):
        """Async initialize of module."""
        self.mass.add_job(self.poll_task())

    async def async_close(self):
        """Handle stop/shutdown."""
        for player_queue in list(self._player_queues.values()):
            await player_queue.async_close()
        for player in self.players:
            await player.async_on_remove()

    @run_periodic(1)
    async def poll_task(self):
        """Check for updates on players that need to be polled."""
        for player in self.players:
            if player.should_poll and (
                self._poll_ticks >= POLL_INTERVAL
                or player.state == PlaybackState.Playing
            ):
                await player.async_on_update()
        if self._poll_ticks >= POLL_INTERVAL:
            self._poll_ticks = 0
        else:
            self._poll_ticks += 1

    @property
    def player_states(self) -> List[PlayerState]:
        """Return PlayerState of all registered players."""
        return list(self._player_states.values())

    @property
    def players(self) -> List[Player]:
        """Return all registered players."""
        return [player_state.player for player_state in self._player_states.values()]

    @property
    def player_queues(self) -> List[PlayerQueue]:
        """Return all player queues."""
        return list(self._player_queues.values())

    @property
    def providers(self) -> List[PlayerProvider]:
        """Return all loaded player providers."""
        return self.mass.get_providers(ProviderType.PLAYER_PROVIDER)

    @callback
    def get_player_state(self, player_id: str) -> PlayerState:
        """Return PlayerState by player_id or None if player does not exist."""
        return self._player_states.get(player_id)

    @callback
    def get_player(self, player_id: str) -> Player:
        """Return Player by player_id or None if player does not exist."""
        player_state = self._player_states.get(player_id)
        if player_state:
            return player_state.player
        return None

    @callback
    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return provider by player_id or None if player does not exist."""
        player = self.get_player(player_id)
        return self.mass.get_provider(player.provider_id) if player else None

    @callback
    def get_player_queue(self, player_id: str) -> PlayerQueue:
        """Return player's queue by player_id or None if player does not exist."""
        player_state = self.get_player_state(player_id)
        if not player_state:
            LOGGER.warning("Player(queue) %s is not available!", player_id)
            return None
        return self._player_queues.get(player_state.active_queue)

    @callback
    def get_player_control(self, control_id: str) -> PlayerControl:
        """Return PlayerControl by id."""
        if control_id not in self._controls:
            LOGGER.warning("PlayerControl %s is not available", control_id)
            return None
        return self._controls[control_id]

    @callback
    def get_player_controls(
        self, filter_type: Optional[PlayerControlType] = None
    ) -> List[PlayerControl]:
        """Return all PlayerControls, optionally filtered by type."""
        return [
            item
            for item in self._controls.values()
            if (filter_type is None or item.type == filter_type)
        ]

    # ADD/REMOVE/UPDATE HELPERS

    async def async_add_player(self, player: Player) -> None:
        """Register a new player or update an existing one."""
        if not player or not player.available or self.mass.exit:
            return
        if player.player_id in self._player_states:
            return await self.async_update_player(player)
        # set the mass object on the player
        player.mass = self.mass
        # create playerstate and queue object
        self._player_states[player.player_id] = PlayerState(self.mass, player)
        self._player_queues[player.player_id] = PlayerQueue(self.mass, player.player_id)
        # TODO: turn on player if it was previously turned on ?
        LOGGER.info(
            "New player added: %s/%s",
            player.provider_id,
            self._player_states[player.player_id].name,
        )
        self.mass.signal_event(
            EVENT_PLAYER_ADDED, self._player_states[player.player_id]
        )

    async def async_remove_player(self, player_id: str):
        """Remove a player from the registry."""
        player_state = self._player_states.pop(player_id, None)
        if player_state:
            await player_state.player.async_on_remove()
        self._player_queues.pop(player_id, None)
        LOGGER.info("Player removed: %s", player_id)
        self.mass.signal_event(EVENT_PLAYER_REMOVED, {"player_id": player_id})

    async def async_update_player(self, player: Player):
        """Update an existing player (or register as new if non existing)."""
        if self.mass.exit:
            return
        if player.player_id not in self._player_states:
            return await self.async_add_player(player)
        await self._player_states[player.player_id].async_update(player)

    async def async_trigger_player_update(self, player_id: str):
        """Trigger update of an existing player.."""
        player = self.get_player(player_id)
        if player:
            await self._player_states[player.player_id].async_update(player)

    async def async_register_player_control(self, control: PlayerControl):
        """Register a playercontrol with the player manager."""
        # control.mass = self.mass
        control.mass = self.mass
        control.type = PlayerControlType(control.type)
        self._controls[control.control_id] = control
        LOGGER.info(
            "New PlayerControl (%s) registered: %s\\%s",
            control.type,
            control.provider,
            control.name,
        )
        # update all players using this playercontrol
        for player_state in self.player_states:
            conf = self.mass.config.player_settings[player_state.player_id]
            if control.control_id in [
                conf.get(CONF_POWER_CONTROL),
                conf.get(CONF_VOLUME_CONTROL),
            ]:
                self.mass.add_job(
                    self.async_trigger_player_update(player_state.player_id)
                )

    async def async_update_player_control(self, control: PlayerControl):
        """Update a playercontrol's state on the player manager."""
        if control.control_id not in self._controls:
            return await self.async_register_player_control(control)
        new_state = control.state
        if self._controls[control.control_id].state == new_state:
            return
        self._controls[control.control_id].state = new_state
        LOGGER.debug(
            "PlayerControl %s\\%s updated - new state: %s",
            control.provider,
            control.name,
            new_state,
        )
        # update all players using this playercontrol
        for player_state in self.player_states:
            conf = self.mass.config.player_settings[player_state.player_id]
            if control.control_id in [
                conf.get(CONF_POWER_CONTROL),
                conf.get(CONF_VOLUME_CONTROL),
            ]:
                self.mass.add_job(
                    self.async_trigger_player_update(player_state.player_id)
                )

    # SERVICE CALLS / PLAYER COMMANDS

    async def async_play_media(
        self,
        player_id: str,
        media_items: List[MediaItem],
        queue_opt: QueueOption = QueueOption.Play,
    ):
        """
        Play media item(s) on the given player.

            :param player_id: player_id of the player to handle the command.
            :param media_item: media item(s) that should be played (single item or list of items)
            :param queue_opt:
                QueueOption.Play -> Insert new items in queue and start playing at inserted position
                QueueOption.Replace -> Replace queue contents with these items
                QueueOption.Next -> Play item(s) after current playing item
                QueueOption.Add -> Append new items at end of the queue
        """
        # a single item or list of items may be provided
        queue_items = []
        for media_item in media_items:
            # collect tracks to play
            if media_item.media_type == MediaType.Artist:
                tracks = self.mass.music.async_get_artist_toptracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.Album:
                tracks = self.mass.music.async_get_album_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.Playlist:
                tracks = self.mass.music.async_get_playlist_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            else:
                tracks = async_iter_items(media_item)  # single track
            async for track in tracks:
                queue_item = QueueItem(track)
                # generate uri for this queue item
                queue_item.uri = "%s/stream/queue/%s/%s" % (
                    self.mass.web.internal_url,
                    player_id,
                    queue_item.queue_item_id,
                )
                queue_items.append(queue_item)
        # turn on player
        await self.async_cmd_power_on(player_id)
        # load items into the queue
        player_queue = self.get_player_queue(player_id)
        if queue_opt == QueueOption.Replace or (
            len(queue_items) > 10 and queue_opt in [QueueOption.Play, QueueOption.Next]
        ):
            return await player_queue.async_load(queue_items)
        if queue_opt == QueueOption.Next:
            return await player_queue.async_insert(queue_items, 1)
        if queue_opt == QueueOption.Play:
            return await player_queue.async_insert(queue_items, 0)
        if queue_opt == QueueOption.Add:
            return await player_queue.async_append(queue_items)

    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the given player.

        Will create a fake track on the queue.

            :param player_id: player_id of the player to handle the command.
            :param uri: Url/Uri that can be played by a player.
            :param queue_opt:
                QueueOption.Play -> Insert new items in queue and start playing at inserted position
                QueueOption.Replace -> Replace queue contents with these items
                QueueOption.Next -> Play item(s) after current playing item
                QueueOption.Add -> Append new items at end of the queue
        """
        queue_item = QueueItem(
            Track(
                item_id=uri,
                provider="uri",
                name=uri,
            )
        )
        # generate uri for this queue item
        queue_item.uri = "%s/stream/%s/%s" % (
            self.mass.web.internal_url,
            player_id,
            queue_item.queue_item_id,
        )
        # turn on player
        await self.async_cmd_power_on(player_id)
        # load item into the queue
        player_queue = self.get_player_queue(player_id)
        return await player_queue.async_insert([queue_item], 0)

    async def async_cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        queue_player_id = player_state.active_queue
        queue_player = self.get_player(queue_player_id)
        return await queue_player.async_cmd_stop()

    async def async_cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        queue_player_id = player_state.active_queue
        queue_player = self.get_player(queue_player_id)
        # unpause if paused else resume queue
        if queue_player.state == PlaybackState.Paused:
            return await queue_player.async_cmd_play()
        # power on at play request
        await self.async_cmd_power_on(player_id)
        return await self._player_queues[queue_player_id].async_resume()

    async def async_cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        queue_player_id = player_state.active_queue
        queue_player = self.get_player(queue_player_id)
        return await queue_player.async_cmd_pause()

    async def async_cmd_play_pause(self, player_id: str):
        """
        Toggle play/pause on given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        if player_state.state == PlaybackState.Playing:
            return await self.async_cmd_pause(player_id)
        return await self.async_cmd_play(player_id)

    async def async_cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        queue_player_id = player_state.active_queue
        return await self.get_player_queue(queue_player_id).async_next()

    async def async_cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        queue_player_id = player_state.active_queue
        return await self.get_player_queue(queue_player_id).async_previous()

    async def async_cmd_power_on(self, player_id: str) -> None:
        """
        Send POWER ON command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        player_config = self.mass.config.player_settings[player_state.player_id]
        # turn on player
        await player_state.player.async_cmd_power_on()
        # player control support
        if player_config.get(CONF_POWER_CONTROL):
            control = self.get_player_control(player_config[CONF_POWER_CONTROL])
            if control:
                await control.async_set_state(True)

    async def async_cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        # send stop if player is playing
        if player_state.active_queue == player_id and player_state.state in [
            PlaybackState.Playing,
            PlaybackState.Paused,
        ]:
            await self.async_cmd_stop(player_id)
        player_config = self.mass.config.player_settings[player_state.player_id]
        # turn off player
        await player_state.player.async_cmd_power_off()
        # player control support
        if player_config.get(CONF_POWER_CONTROL):
            control = self.get_player_control(player_config[CONF_POWER_CONTROL])
            if control:
                await control.async_set_state(False)
        # handle group power
        if player_state.is_group_player:
            # player is group, turn off all childs
            for child_player_id in player_state.group_childs:
                child_player = self.get_player(child_player_id)
                if child_player and child_player.powered:
                    self.mass.add_job(self.async_cmd_power_off(child_player_id))
        else:
            # if this was the last powered player in the group, turn off group
            for parent_player_id in player_state.group_parents:
                parent_player = self.get_player(parent_player_id)
                if not parent_player or not parent_player.powered:
                    continue
                has_powered_players = False
                for child_player_id in parent_player.group_childs:
                    if child_player_id == player_id:
                        continue
                    child_player = self.get_player(child_player_id)
                    if child_player and child_player.powered:
                        has_powered_players = True
                if not has_powered_players:
                    self.mass.add_job(self.async_cmd_power_off(parent_player_id))

    async def async_cmd_power_toggle(self, player_id: str):
        """
        Send POWER TOGGLE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        if player_state.powered:
            return await self.async_cmd_power_off(player_id)
        return await self.async_cmd_power_on(player_id)

    async def async_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        player_state = self.get_player_state(player_id)
        if not player_state or not player_state.powered:
            return
        player_config = self.mass.config.player_settings[player_state.player_id]
        volume_level = try_parse_int(volume_level)
        if volume_level < 0:
            volume_level = 0
        elif volume_level > 100:
            volume_level = 100
        # player control support
        if player_config.get(CONF_VOLUME_CONTROL):
            control = self.get_player_control(player_config[CONF_VOLUME_CONTROL])
            if control:
                await control.async_set_state(volume_level)
                # just force full volume on actual player if volume is outsourced to volumecontrol
                await player_state.player.async_cmd_volume_set(player_id, 100)
        # handle group volume
        elif player_state.is_group_player:
            cur_volume = player_state.volume_level
            new_volume = volume_level
            volume_dif = new_volume - cur_volume
            if cur_volume == 0:
                volume_dif_percent = 1 + (new_volume / 100)
            else:
                volume_dif_percent = volume_dif / cur_volume
            for child_player_id in player_state.group_childs:
                child_player = self.get_player_state(child_player_id)
                if child_player and child_player.available and child_player.powered:
                    cur_child_volume = child_player.volume_level
                    new_child_volume = cur_child_volume + (
                        cur_child_volume * volume_dif_percent
                    )
                    await self.async_cmd_volume_set(child_player_id, new_child_volume)
        # regular volume command
        else:
            await player_state.player.async_cmd_volume_set(volume_level)

    async def async_cmd_volume_up(self, player_id: str):
        """
        Send volume UP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        new_level = player_state.volume_level + 1
        if new_level > 100:
            new_level = 100
        return await self.async_cmd_volume_set(player_id, new_level)

    async def async_cmd_volume_down(self, player_id: str):
        """
        Send volume DOWN command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        new_level = player_state.volume_level - 1
        if new_level < 0:
            new_level = 0
        return await self.async_cmd_volume_set(player_id, new_level)

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
        Send MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with the new mute state.
        """
        player_state = self.get_player_state(player_id)
        if not player_state:
            return
        # TODO: handle mute on volumecontrol?
        return await player_state.player.async_cmd_volume_mute(is_muted)

    # OTHER/HELPER FUNCTIONS

    async def async_get_gain_correct(
        self, player_id: str, item_id: str, provider_id: str
    ):
        """Get gain correction for given player / track combination."""
        player_conf = self.mass.config.get_player_config(player_id)
        if not player_conf["volume_normalisation"]:
            return 0
        target_gain = int(player_conf["target_volume"])
        fallback_gain = int(player_conf["fallback_gain_correct"])
        track_loudness = await self.mass.database.async_get_track_loudness(
            item_id, provider_id
        )
        if track_loudness is None:
            gain_correct = fallback_gain
        else:
            gain_correct = target_gain - track_loudness
        gain_correct = round(gain_correct, 2)
        return gain_correct

    async def __handle_websocket_player_control_event(self, msg, msg_details):
        """Handle player controls over the websockets api."""
        if msg in [EVENT_REGISTER_PLAYER_CONTROL, EVENT_PLAYER_CONTROL_UPDATED]:
            # create or update a playercontrol registered through the websockets api
            control = PlayerControl(**msg_details)
            await self.async_update_player_control(control)
            # send confirmation to the client that the register was successful
            if msg == EVENT_PLAYER_CONTROL_REGISTERED:
                self.mass.signal_event(EVENT_PLAYER_CONTROL_REGISTERED, control)
