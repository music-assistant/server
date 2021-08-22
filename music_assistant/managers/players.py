"""PlayerManager: Orchestrates all players from player providers."""

import asyncio
import logging
import pathlib
from typing import Dict, List, Optional, Set, Tuple, Union

from music_assistant.constants import (
    CONF_CROSSFADE_DURATION,
    CONF_POWER_CONTROL,
    CONF_VOLUME_CONTROL,
    EVENT_PLAYER_ADDED,
    EVENT_PLAYER_REMOVED,
)
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import callback, create_task, try_parse_int
from music_assistant.helpers.web import api_route
from music_assistant.models.media_types import MediaItem, MediaType
from music_assistant.models.player import (
    Player,
    PlayerControl,
    PlayerControlType,
    PlayerState,
)
from music_assistant.models.player_queue import PlayerQueue, QueueItem, QueueOption
from music_assistant.models.provider import PlayerProvider, ProviderType

POLL_INTERVAL = 30

LOGGER = logging.getLogger("player_manager")
RESOURCES_DIR = (
    pathlib.Path(__file__).parent.resolve().parent.resolve().joinpath("resources")
)
ALERT_ANNOUNCE_FILE = str(RESOURCES_DIR.joinpath("announce.flac"))
ALERT_FINISH_FILE = str(RESOURCES_DIR.joinpath("silence.flac"))


class PlayerManager:
    """Several helpers to handle playback through player providers."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self._players = {}
        self._providers = {}
        self._player_queues = {}
        self._controls = {}
        self._alerts_in_progress = set()

    async def setup(self) -> None:
        """Async initialize of module."""
        asyncio.create_task(self.poll_task())

    async def close(self) -> None:
        """Handle stop/shutdown."""
        for player_queue in self._player_queues.values():
            await player_queue.close()
        for player in self:
            await player.on_remove()

    async def poll_task(self):
        """Check for updates on players that need to be polled."""
        count = 0
        while True:
            for player in self:
                if not player.calculated_state.available:
                    continue
                if not player.should_poll:
                    continue
                if player.state == PlayerState.PLAYING or count == POLL_INTERVAL:
                    await player.on_poll()
            if count == POLL_INTERVAL:
                count = 0
            else:
                count += 1
            await asyncio.sleep(1)

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
    @api_route("queues")
    def get_player_queues(self) -> Tuple[PlayerQueue]:
        """Return all player queues in a tuple."""
        return tuple(self._player_queues.values())

    @callback
    @api_route("players/{player_id}")
    def get_player(self, player_id: str, raise_not_found: bool = False) -> Player:
        """Return Player by player_id."""
        player = self._players.get(player_id)
        if not player and raise_not_found:
            raise FileNotFoundError("Player not found %s" % player_id)
        return player

    @callback
    def get_player_by_name(
        self, name: str, provider_id: Optional[str] = None
    ) -> Optional[Player]:
        """Return Player by name or None if no match is found."""
        for player in self:
            if provider_id is not None and player.provider_id != provider_id:
                continue
            if name in (player.name, player.calculated_state.name):
                return player
        return None

    @callback
    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return provider by player_id or None if player does not exist."""
        player = self.get_player(player_id)
        return self.mass.get_provider(player.provider_id) if player else None

    @callback
    @api_route("queues/{queue_id}")
    def get_player_queue(self, queue_id: str) -> PlayerQueue:
        """Return player Queue by queue id or None if queue does not exist."""
        queue = self._player_queues.get(queue_id)
        if not queue:
            LOGGER.warning("Player(queue) %s is not available!", queue_id)
            return None
        return queue

    @callback
    @api_route("players/{player_id}/queue")
    def get_active_player_queue(
        self, player_id: str, raise_not_found: bool = True
    ) -> PlayerQueue:
        """Return the active queue for given player id."""
        player = self.get_player(player_id, raise_not_found)
        if player:
            return self.get_player_queue(player.calculated_state.active_queue)
        return None

    @callback
    @api_route("queues/{queue_id}/items")
    def get_player_queue_items(self, queue_id: str) -> Set[QueueItem]:
        """Return player's queueitems by player_id."""
        player_queue = self.get_player_queue(queue_id)
        return player_queue.items if player_queue else {}

    @callback
    @api_route("players/controls/{control_id}")
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
        player_id = player.player_id

        # guard for invalid data or exit in progress
        if not player or self.mass.exit:
            return

        # redirect to update if player is already added
        if player_id in self._players:
            player = self._players[player_id]
            if player.added_to_mass:
                await self.trigger_player_update(player_id)
                return
        else:
            self._players[player.player_id] = player
            # make sure that the mass instance is set on the player
            player.mass = self.mass

        # make sure that the player state is created/updated
        player.calculated_state.update(player.create_calculated_state())

        # Fully initialize only if player is enabled
        if not player.enabled:
            LOGGER.debug(
                "Ignoring player: %s/%s because it's disabled",
                player.provider_id,
                player.name,
            )
            return

        # new player
        player.added_to_mass = True
        await player.on_add()
        # create playerqueue instance
        self._player_queues[player.player_id] = PlayerQueue(self.mass, player.player_id)
        LOGGER.info(
            "Player added: %s/%s",
            player.provider_id,
            player.name,
        )
        self.mass.eventbus.signal(EVENT_PLAYER_ADDED, player.calculated_state)

    async def remove_player(self, player_id: str):
        """Remove a player from the registry."""
        self._player_queues.pop(player_id, None)
        player = self._players.pop(player_id, None)
        if player:
            await player.on_remove()
        player_name = player.name if player else player_id
        LOGGER.info("Player removed: %s", player_name)
        self.mass.eventbus.signal(EVENT_PLAYER_REMOVED, {"player_id": player_id})

    async def trigger_player_update(self, player_id: str):
        """Trigger update of an existing player.."""
        player = self.get_player(player_id, False)
        if player:
            await player.on_poll()

    @api_route("players/controls/{control_id}", method="POST")
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
                create_task(self.trigger_player_update(player.player_id))

    @api_route("players/controls/{control_id}", method="PUT")
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
                create_task(self.trigger_player_update(player.player_id))

    # SERVICE CALLS / PLAYER COMMANDS

    @api_route("players/{player_id}/play_media", method="PUT")
    async def play_media(
        self,
        player_id: str,
        items: Union[MediaItem, List[MediaItem]],
        queue_opt: QueueOption = QueueOption.PLAY,
    ):
        """
        Play media item(s) on the given player.

            :param player_id: player_id of the player to handle the command.
            :param items: media item(s) that should be played (single item or list of items)
            :param queue_opt:
                QueueOption.PLAY -> Insert new items in queue and start playing at inserted position
                QueueOption.REPLACE -> Replace queue contents with these items
                QueueOption.NEXT -> Play item(s) after current playing item
                QueueOption.ADD -> Append new items at end of the queue
        """
        player = self.get_player(player_id, True)
        player_queue = self.get_active_player_queue(player_id, True)
        # power on player if needed
        if not player.calculated_state.powered:
            await self.cmd_power_on(player_id)
        # a single item or list of items may be provided
        if not isinstance(items, list):
            items = [items]
        queue_items = []
        for media_item in items:
            # collect tracks to play
            if media_item.media_type == MediaType.ARTIST:
                tracks = await self.mass.music.get_artist_toptracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.ALBUM:
                tracks = await self.mass.music.get_album_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.PLAYLIST:
                tracks = await self.mass.music.get_playlist_tracks(
                    media_item.item_id, provider_id=media_item.provider
                )
            elif media_item.media_type == MediaType.RADIO:
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
                queue_item = player_queue.create_queue_item(track)
                queue_items.append(queue_item)

        # load items into the queue
        if queue_opt == QueueOption.REPLACE:
            return await player_queue.load(queue_items)
        if queue_opt in [QueueOption.PLAY, QueueOption.NEXT] and len(queue_items) > 100:
            return await player_queue.load(queue_items)
        if queue_opt == QueueOption.NEXT:
            return await player_queue.insert(queue_items, 1)
        if queue_opt == QueueOption.PLAY:
            return await player_queue.insert(queue_items, 0)
        if queue_opt == QueueOption.ADD:
            return await player_queue.append(queue_items)

    @api_route("players/{player_id}/play_uri", method="PUT")
    async def play_uri(
        self, player_id: str, uri: str, queue_opt: QueueOption = QueueOption.PLAY
    ):
        """
        Play the specified uri/url on the given player.

        Will create a fake track on the queue.

            :param player_id: player_id of the player to handle the command.
            :param uri: Url/Uri that can be played by a player.
        """
        # try media uri first
        if not uri.startswith("http"):
            item = await self.mass.music.get_item_by_uri(uri)
            if item:
                return await self.play_media(player_id, item, queue_opt)
            raise FileNotFoundError("Invalid uri: %s" % uri)
        player = self.get_player(player_id, True)
        player_queue = self.get_active_player_queue(player_id, True)
        # power on player if needed
        if not player.calculated_state.powered:
            await self.cmd_power_on(player_id)
        # load item into the queue
        queue_item = player_queue.create_queue_item(
            item_id=uri, provider="url", name=uri, uri=uri
        )
        if queue_opt == QueueOption.REPLACE:
            return await player_queue.load([queue_item])
        if queue_opt == QueueOption.NEXT:
            return await player_queue.insert([queue_item], 1)
        if queue_opt == QueueOption.PLAY:
            return await player_queue.insert([queue_item], 0)
        if queue_opt == QueueOption.ADD:
            return await player_queue.append([queue_item])

    @api_route("players/{player_id}/play_alert", method="PUT")
    async def play_alert(
        self,
        player_id: str,
        url: str,
        volume: Optional[int] = None,
        force: bool = True,
        announce: bool = False,
    ):
        """
        Play alert (e.g. tts message) on selected player.

        Will pause the current playing queue and resume after the alert is played.

            :param player_id: player_id of the player to handle the command.
            :param url: Url to the sound effect/tts message that should be played.
            :param volume: Force volume of player to this level during the alert.
            :param force: Play alert even if player is currently powered off.
            :param announce: Announce the alert by prepending an alert sound.
        """
        player = self.get_player(player_id, True)
        player_queue = self.get_active_player_queue(player_id)
        if player_queue.queue_id in self._alerts_in_progress:
            LOGGER.debug(
                "Ignoring Play Alert for queue %s - Another alert is already in progress.",
                player_queue.queue_id,
            )
            return
        self._alerts_in_progress.add(player_queue.queue_id)
        prev_state = player_queue.state
        prev_power = player.calculated_state.powered
        prev_volume = player.calculated_state.volume_level
        prev_repeat = player_queue.repeat_enabled
        if not player.calculated_state.powered:
            if not force:
                LOGGER.debug(
                    "Ignore alert playback: Player %s is powered off.",
                    player.calculated_state.name,
                )
                return
        # power on player if needed
        if not player.calculated_state.powered:
            await self.cmd_power_on(player_id)
        # snapshot the (active) queue
        prev_queue_items = player_queue.items
        prev_queue_index = player_queue.cur_index
        prev_queue_crossfade = self.mass.config.get_player_config(
            player_queue.queue_id
        )[CONF_CROSSFADE_DURATION]

        # pause playback
        if prev_state == PlayerState.PLAYING:
            await self.cmd_pause(player_queue.queue_id)
        # disable crossfade and repeat if needed
        if prev_queue_crossfade:
            self.mass.config.player_settings[player_queue.queue_id][
                CONF_CROSSFADE_DURATION
            ] = 0
        if prev_repeat:
            await player_queue.set_repeat_enabled(False)
        # set alert volume
        if volume:
            await self.cmd_volume_set(player_id, volume)
        # load alert items in player queue
        queue_items = []
        if announce:
            queue_items.append(
                player_queue.create_queue_item(
                    item_id="alert_announce",
                    provider="url",
                    name="alert_announce",
                    uri=ALERT_ANNOUNCE_FILE,
                )
            )
        queue_items.append(
            player_queue.create_queue_item(
                item_id="alert", provider="url", name="alert", uri=url
            )
        )
        queue_items.append(
            # add a special (silent) file so we can detect finishing of the alert
            player_queue.create_queue_item(
                item_id="alert_finish",
                provider="url",
                name="alert_finish",
                uri=ALERT_FINISH_FILE,
            )
        )
        # load queue items
        await player_queue.load(queue_items)

        # add listener when playback of alert finishes
        async def restore_queue():
            count = 0
            while count < 30:
                if (
                    player_queue.cur_item == queue_items[-1]
                    and player_queue.cur_item_time > 2
                ):
                    break
                count += 1
                await asyncio.sleep(1)
            # restore queue
            if volume:
                await self.cmd_volume_set(player_id, prev_volume)
            if prev_queue_crossfade:
                self.mass.config.player_settings[player_queue.queue_id][
                    CONF_CROSSFADE_DURATION
                ] = prev_queue_crossfade
            await player_queue.set_repeat_enabled(prev_repeat)
            # pylint: disable=protected-access
            player_queue._items = prev_queue_items
            player_queue._cur_index = prev_queue_index
            if prev_state == PlayerState.PLAYING:
                await player_queue.resume()
            if not prev_power:
                await self.cmd_power_off(player_id)
            self._alerts_in_progress.remove(player_queue.queue_id)
            player_queue.signal_update()

        create_task(restore_queue)

    @api_route("players/{player_id}/cmd/stop", method="PUT")
    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_queue = self.get_active_player_queue(player_id)
        await player_queue.stop()

    @api_route("players/{player_id}/cmd/play", method="PUT")
    async def cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, True)
        player_queue = self.get_active_player_queue(player_id)
        # power on player if needed
        if not player.calculated_state.powered:
            await self.cmd_power_on(player_id)
        # unpause if paused else resume queue
        if player_queue.state == PlayerState.PAUSED:
            await player_queue.play()
        else:
            await player_queue.resume()

    @api_route("players/{player_id}/cmd/pause", method="PUT")
    async def cmd_pause(self, player_id: str) -> None:
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_queue = self.get_active_player_queue(player_id, True)
        await player_queue.pause()

    @api_route("players/{player_id}/cmd/play_pause", method="PUT")
    async def cmd_play_pause(self, player_id: str) -> None:
        """
        Toggle play/pause on given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_queue = self.get_active_player_queue(player_id, True)
        if player_queue.state == PlayerState.PLAYING:
            await self.cmd_pause(player_queue.queue_id)
        else:
            await self.cmd_play(player_queue.queue_id)

    @api_route("players/{player_id}/cmd/next", method="PUT")
    async def cmd_next(self, player_id: str) -> None:
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_queue = self.get_active_player_queue(player_id, True)
        if player_queue.state == PlayerState.PLAYING:
            await player_queue.next()
        else:
            await self.cmd_play(player_queue.queue_id)

    @api_route("players/{player_id}/cmd/previous", method="PUT")
    async def cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player_queue = self.get_active_player_queue(player_id, True)
        if player_queue.state == PlayerState.PLAYING:
            await player_queue.previous()
        else:
            await self.cmd_play(player_queue.queue_id)

    @api_route("players/{player_id}/cmd/power_on", method="PUT")
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

    @api_route("players/{player_id}/cmd/power_off", method="PUT")
    async def cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, True)
        # send stop if player is active queue
        if player.active_queue == player_id and player.state not in [
            PlayerState.OFF,
            PlayerState.IDLE,
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
                if child_player and child_player.calculated_state.powered:
                    create_task(self.cmd_power_off(child_player_id))
        else:
            # if this was the last powered player in the group, turn off group
            for parent_player_id in player.group_parents:
                parent_player = self.get_player(parent_player_id)
                if not parent_player or not parent_player.calculated_state.powered:
                    continue
                has_powered_players = False
                for child_player_id in parent_player.group_childs:
                    if child_player_id == player_id:
                        continue
                    child_player = self.get_player(child_player_id)
                    if child_player and child_player.calculated_state.powered:
                        has_powered_players = True
                if not has_powered_players:
                    create_task(self.cmd_power_off(parent_player_id))

    @api_route("players/{player_id}/cmd/power_toggle", method="PUT")
    async def cmd_power_toggle(self, player_id: str):
        """
        Send POWER TOGGLE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, True)
        if player.calculated_state.powered:
            return await self.cmd_power_off(player_id)
        return await self.cmd_power_on(player_id)

    @api_route("players/{player_id}/cmd/volume_set", method="PUT")
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        player = self.get_player(player_id, True)
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
                    and child_player.calculated_state.powered
                ):
                    cur_child_volume = child_player.volume_level
                    new_child_volume = cur_child_volume + (
                        cur_child_volume * volume_dif_percent
                    )
                    await self.cmd_volume_set(child_player_id, new_child_volume)
        # regular volume command
        else:
            await player.cmd_volume_set(volume_level)

    @api_route("players/{player_id}/cmd/volume_up", method="PUT")
    async def cmd_volume_up(self, player_id: str):
        """
        Send volume UP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, True)
        if player.volume_level <= 10 or player.volume_level >= 90:
            step_size = 2
        else:
            step_size = 5
        new_level = player.volume_level + step_size
        if new_level > 100:
            new_level = 100
        return await self.cmd_volume_set(player_id, new_level)

    @api_route("players/{player_id}/cmd/volume_down", method="PUT")
    async def cmd_volume_down(self, player_id: str):
        """
        Send volume DOWN command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self.get_player(player_id, True)
        if player.volume_level <= 10 or player.volume_level >= 90:
            step_size = 2
        else:
            step_size = 5
        new_level = player.volume_level - step_size
        if new_level < 0:
            new_level = 0
        return await self.cmd_volume_set(player_id, new_level)

    @api_route("players/{player_id}/cmd/volume_mute", method="PUT")
    async def cmd_volume_mute(self, player_id: str, is_muted: bool = False):
        """
        Send MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with the new mute state.
        """
        player = self.get_player(player_id, True)
        # TODO: handle mute on volumecontrol?
        return await player.cmd_volume_mute(is_muted)

    @api_route("queues/{queue_id}", method="PUT")
    async def player_queue_update(
        self,
        queue_id: str,
        enable_shuffle: Optional[bool] = None,
        enable_repeat: Optional[bool] = None,
    ) -> None:
        """Set options to given playerqueue."""
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            raise FileNotFoundError("Unknown Queue: %s" % queue_id)
        if enable_shuffle is not None:
            await player_queue.set_shuffle_enabled(enable_shuffle)
        if enable_repeat is not None:
            await player_queue.set_repeat_enabled(enable_repeat)

    @api_route("queues/{queue_id}/cmd/next", method="PUT")
    async def player_queue_cmd_next(self, queue_id: str):
        """
        Send next track command to given playerqueue.

            :param queue_id: player_id of the playerqueue to handle the command.
        """
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.next()

    @api_route("queues/{queue_id}/cmd/previous", method="PUT")
    async def player_queue_cmd_previous(self, queue_id: str):
        """
        Send previous track command to given playerqueue.

            :param queue_id: player_id of the playerqueue to handle the command.
        """
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.previous()

    @api_route("queues/{queue_id}/cmd/move", method="PUT")
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

    @api_route("queues/{queue_id}/cmd/play_index", method="PUT")
    async def play_index(self, queue_id: str, index: Union[int, str]) -> None:
        """Play item at index (or item_id) X in queue."""
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.play_index(index)

    @api_route("queues/{queue_id}/items", method="DELETE")
    async def player_queue_cmd_clear(self, queue_id: str):
        """
        Clear all items in player's queue.

            :param queue_id: player_id of the playerqueue to handle the command.
        """
        player_queue = self.get_player_queue(queue_id)
        if not player_queue:
            return
        return await player_queue.clear()
