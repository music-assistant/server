"""Models and helpers for a player provider."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import List

from music_assistant.models.player_queue import QueueItem
from music_assistant.models.provider import Provider, ProviderType


@dataclass
class PlayerProvider(Provider):
    """
    Base class for a Playerprovider.

    Should be overridden/subclassed by provider specific implementation.
    """

    type: ProviderType = ProviderType.PLAYER_PROVIDER

    # SERVICE CALLS / PLAYER COMMANDS

    @abstractmethod
    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the given player.

            :param player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_cmd_power_on(self, player_id: str) -> None:
        """
        Send POWER ON command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        raise NotImplementedError

    @abstractmethod
    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
        Send volume MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with new mute state.
        """
        raise NotImplementedError

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
        # pylint: disable=abstract-method
        raise NotImplementedError

    async def async_cmd_queue_append(
        self, player_id: str, queue_items: List[QueueItem]
    ):
        """
        Append new items at the end of the queue.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        # pylint: disable=abstract-method
        raise NotImplementedError

    async def async_cmd_queue_update(
        self, player_id: str, queue_items: List[QueueItem]
    ):
        """
        Overwrite the existing items in the queue, used for reordering.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        # pylint: disable=abstract-method
        raise NotImplementedError

    async def async_cmd_queue_clear(self, player_id: str):
        """
        Clear the player's queue.

            :param player_id: player_id of the player to handle the command.
        """
        # pylint: disable=abstract-method
        raise NotImplementedError
