"""Demo/test providers."""

import functools
from typing import List

import vlc
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import DeviceInfo, Player, PlayerFeature, PlayerState
from music_assistant.models.player_queue import QueueItem
from music_assistant.models.playerprovider import PlayerProvider

PROV_ID = "demo_player"
PROV_NAME = "Demo/Test players"


class DemoPlayerProvider(PlayerProvider):
    """Demo PlayerProvider which provides fake players."""

    def __init__(self, *args, **kwargs):
        """Initialize."""
        self._players = {}
        super().__init__(*args, **kwargs)

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return PROV_NAME

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""
        return []

    async def async_on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        # create some fake players
        for count in range(5)[1:]:
            player_id = f"demo_{count}"
            player = Player(
                player_id=player_id,
                provider_id=PROV_ID,
                name=f"Demo player {count}",
                should_poll=False,
                available=True,
            )
            model_name = "Base"
            if count == 1:
                # player 1 has no support for special features
                model_name = "Basic"
            if count == 2:
                # player 2 has QUEUE support feature but no crossfade
                player.features = [PlayerFeature.QUEUE]
                model_name = "QUEUE support"
            if count == 3:
                # player 3 has support for all features
                player.features = [
                    PlayerFeature.QUEUE,
                    PlayerFeature.GAPLESS,
                    PlayerFeature.CROSSFADE,
                ]
            if count == 4:
                # player 4 is a group player
                player.is_group_player = True
                player.group_childs = ["demo_1", "demo_2", "demo_8"]
                player.blaat = True
            player.device_info = DeviceInfo(
                model=model_name, address=f"http://demo:{count}", manufacturer=PROV_ID
            )
            player.vlc_instance = vlc.Instance()
            player.vlc_player = player.vlc_instance.media_player_new()
            events = player.vlc_player.event_manager()
            player_event_cb = functools.partial(self.player_event, player_id)
            events.event_attach(vlc.EventType.MediaPlayerEndReached, player_event_cb)
            events.event_attach(vlc.EventType.MediaPlayerMediaChanged, player_event_cb)
            events.event_attach(vlc.EventType.MediaPlayerPlaying, player_event_cb)
            events.event_attach(vlc.EventType.MediaPlayerPaused, player_event_cb)
            events.event_attach(vlc.EventType.MediaPlayerStopped, player_event_cb)
            events.event_attach(vlc.EventType.MediaPlayerTimeChanged, player_event_cb)
            events.event_attach(vlc.EventType.MediaPlayerMuted, player_event_cb)
            events.event_attach(vlc.EventType.MediaPlayerUnmuted, player_event_cb)
            events.event_attach(vlc.EventType.MediaPlayerAudioVolume, player_event_cb)
            self._players[player_id] = player
            self.mass.add_job(self.mass.player_manager.async_add_player(player))

        return True

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        for player_id, player in self._players.items():
            player.vlc_player.release()
            player.vlc_instance.release()
            del player
            await self.mass.player_manager.async_remove_player(player_id)
        self._players = {}

    def player_event(self, player_id, event):
        """Call on vlc player events."""
        # pylint: disable = unused-argument
        vlc_player: vlc.MediaPlayer = self._players[player_id].vlc_player
        self._players[player_id].muted = vlc_player.audio_get_mute()
        self._players[player_id].volume_level = vlc_player.audio_get_volume()
        if vlc_player.is_playing():
            self._players[player_id].state = PlayerState.Playing
            self._players[player_id].powered = True
        elif vlc_player.get_media():
            self._players[player_id].state = PlayerState.Paused
        else:
            self._players[player_id].state = PlayerState.Stopped
        self._players[player_id].elapsed_time = int(vlc_player.get_time() / 1000)
        self.mass.add_job(
            self.mass.player_manager.async_update_player(self._players[player_id])
        )

    # SERVICE CALLS / PLAYER COMMANDS

    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the given player.

            :param player_id: player_id of the player to handle the command.
        """
        # self._players[player_id].current_uri = uri
        media = self._players[player_id].vlc_instance.media_new_location(uri)
        self.mass.add_job(self._players[player_id].vlc_player.set_media, media)
        self.mass.add_job(self._players[player_id].vlc_player.play)

    async def async_cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].vlc_player.stop)

    async def async_cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        if self._players[player_id].vlc_player.get_media():
            self.mass.add_job(self._players[player_id].vlc_player.play)

    async def async_cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].vlc_player.pause)

    async def async_cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].vlc_player.next_chapter)

    async def async_cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].vlc_player.previous_chapter)

    async def async_cmd_power_on(self, player_id: str) -> None:
        """
        Send POWER ON command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self._players[player_id].powered = True
        self.mass.add_job(
            self.mass.player_manager.async_update_player(self._players[player_id])
        )

    async def async_cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].vlc_player.stop)
        self._players[player_id].powered = False
        self.mass.add_job(
            self.mass.player_manager.async_update_player(self._players[player_id])
        )

    async def async_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        self.mass.add_job(
            self._players[player_id].vlc_player.audio_set_volume, volume_level
        )

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
        Send volume MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with new mute state.
        """
        self.mass.add_job(self._players[player_id].vlc_player.audio_set_mute, is_muted)

    # OPTIONAL: QUEUE SERVICE CALLS/COMMANDS - OVERRIDE ONLY IF SUPPORTED BY PROVIDER
    # pylint: disable=abstract-method

    async def async_cmd_queue_play_index(self, player_id: str, index: int):
        """
        Play item at index X on player's queue.

            :param player_id: player_id of the player to handle the command.
            :param index: (int) index of the queue item that should start playing
        """
        raise NotImplementedError

    async def async_cmd_queue_load(self, player_id: str, queue_items: List[QueueItem]):
        """
        Load/overwrite given items in the player's queue implementation.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        raise NotImplementedError

    async def async_cmd_queue_insert(
        self, player_id: str, queue_items: List[QueueItem], insert_at_index: int
    ):
        """
        Insert new items at position X into existing queue.

        If insert_at_index 0 or None, will start playing newly added item(s)
            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
            :param insert_at_index: queue position to insert new items
        """
        raise NotImplementedError

    async def async_cmd_queue_append(
        self, player_id: str, queue_items: List[QueueItem]
    ):
        """
        Append new items at the end of the queue.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        raise NotImplementedError

    async def async_cmd_queue_update(
        self, player_id: str, queue_items: List[QueueItem]
    ):
        """
        Overwrite the existing items in the queue, used for reordering.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        raise NotImplementedError

    async def async_cmd_queue_clear(self, player_id: str):
        """
        Clear the player's queue.

            :param player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError
