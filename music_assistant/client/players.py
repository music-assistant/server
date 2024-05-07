"""Handle player related endpoints for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.common.models.enums import EventType
from music_assistant.common.models.player import Player

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.common.models.event import MassEvent

    from .client import MusicAssistantClient


class Players:
    """Player related endpoints/data for Music Assistant."""

    def __init__(self, client: MusicAssistantClient) -> None:
        """Handle Initialization."""
        self.client = client
        # subscribe to player events
        client.subscribe(
            self._handle_event,
            (
                EventType.PLAYER_ADDED,
                EventType.PLAYER_REMOVED,
                EventType.PLAYER_UPDATED,
            ),
        )
        # the initial items are retrieved after connect
        self._players: dict[str, Player] = {}

    @property
    def players(self) -> list[Player]:
        """Return all players."""
        return list(self._players.values())

    def __iter__(self) -> Iterator[Player]:
        """Iterate over (available) players."""
        return iter(self._players.values())

    def get(self, player_id: str) -> Player | None:
        """Return Player by ID (or None if not found)."""
        return self._players.get(player_id)

    #  Player related endpoints/commands

    async def player_command_stop(self, player_id: str) -> None:
        """Send STOP command to given player (directly)."""
        await self.client.send_command("players/cmd/stop", player_id=player_id)

    async def player_command_play(self, player_id: str) -> None:
        """Send PLAY command to given player (directly)."""
        await self.client.send_command("players/cmd/play", player_id=player_id)

    async def player_command_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player (directly)."""
        await self.client.send_command("players/cmd/pause", player_id=player_id)

    async def player_command_play_pause(self, player_id: str) -> None:
        """Send PLAY_PAUSE (toggle) command to given player (directly)."""
        await self.client.send_command("players/cmd/pause", player_id=player_id)

    async def player_command_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        await self.client.send_command("players/cmd/power", player_id=player_id, powered=powered)

    async def player_command_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME SET command to given player."""
        await self.client.send_command(
            "players/cmd/volume_set", player_id=player_id, volume_level=volume_level
        )

    async def player_command_volume_up(self, player_id: str) -> None:
        """Send VOLUME UP command to given player."""
        await self.client.send_command("players/cmd/volume_up", player_id=player_id)

    async def player_command_volume_down(self, player_id: str) -> None:
        """Send VOLUME DOWN command to given player."""
        await self.client.send_command("players/cmd/volume_down", player_id=player_id)

    async def player_command_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        await self.client.send_command("players/cmd/volume_mute", player_id=player_id, muted=muted)

    async def player_command_seek(self, player_id: str, position: int) -> None:
        """Handle SEEK command for given player (directly).

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        await self.client.send_command("players/cmd/seek", player_id=player_id, position=position)

    async def player_command_sync(self, player_id: str, target_player: str) -> None:
        """
        Handle SYNC command for given player.

        Join/add the given player(id) to the given (master) player/sync group.
        If the player is already synced to another player, it will be unsynced there first.
        If the target player itself is already synced to another player, this will fail.
        If the player can not be synced with the given target player, this will fail.

          - player_id: player_id of the player to handle the command.
          - target_player: player_id of the syncgroup master or group player.
        """
        await self.client.send_command(
            "players/cmd/sync", player_id=player_id, target_player=target_player
        )

    async def player_command_unsync(self, player_id: str) -> None:
        """
        Handle UNSYNC command for given player.

        Remove the given player from any syncgroups it currently is synced to.
        If the player is not currently synced to any other player,
        this will silently be ignored.

          - player_id: player_id of the player to handle the command.
        """
        await self.client.send_command("players/cmd/unsync", player_id=player_id)

    async def cmd_sync_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """Create temporary sync group by joining given players to target player."""
        await self.client.send_command(
            "players/cmd/sync_many", target_player=target_player, child_player_ids=child_player_ids
        )

    async def cmd_unsync_many(self, player_ids: list[str]) -> None:
        """Create temporary sync group by joining given players to target player."""
        await self.client.send_command("players/cmd/unsync_many", player_ids)

    async def play_announcement(
        self,
        player_id: str,
        url: str,
        use_pre_announce: bool | None = None,
        volume_level: int | None = None,
    ) -> None:
        """Handle playback of an announcement (url) on given player."""
        await self.client.send_command(
            "players/cmd/play_announcement",
            player_id=player_id,
            url=url,
            use_pre_announce=use_pre_announce,
            volume_level=volume_level,
        )

    #  PlayerGroup related endpoints/commands

    async def create_group(self, provider: str, name: str, members: list[str]) -> Player:
        """Create new (permanent) Player/Sync Group on given PlayerProvider with name and members.

        - provider: provider domain or instance id to create the new group on.
        - name: Name for the new group to create.
        - members: A list of player_id's that should be part of this group.

        Returns the newly created player on success.
        NOTE: Fails if the given provider does not support creating new groups
        or members are given that can not be handled by the provider.
        """
        return Player.from_dict(
            await self.client.send_command(
                "players/create_group", provider=provider, name=name, members=members
            )
        )

    async def set_player_group_volume(self, player_id: str, volume_level: int) -> None:
        """
        Send VOLUME_SET command to given playergroup.

        Will send the new (average) volume level to group child's.
        - player_id: player_id of the playergroup to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        await self.client.send_command(
            "players/cmd/group_volume", player_id=player_id, volume_level=volume_level
        )

    async def set_player_group_power(self, player_id: str, power: bool) -> None:
        """Handle power command for a (Sync)Group."""
        await self.client.send_command("players/cmd/group_volume", player_id=player_id, power=power)

    async def set_player_group_members(self, player_id: str, members: list[str]) -> None:
        """
        Update the memberlist of the given PlayerGroup.

          - player_id: player_id of the groupplayer to handle the command.
          - members: list of player ids to set as members.
        """
        await self.client.send_command(
            "players/cmd/set_members", player_id=player_id, members=members
        )

    # Other endpoints/commands

    async def _get_players(self) -> list[Player]:
        """Fetch all Players from the server."""
        return [Player.from_dict(item) for item in await self.client.send_command("players/all")]

    async def fetch_state(self) -> None:
        """Fetch initial state once the server is connected."""
        for player in await self._get_players():
            self._players[player.player_id] = player

    def _handle_event(self, event: MassEvent) -> None:
        """Handle incoming player event."""
        if event.event in (EventType.PLAYER_ADDED, EventType.PLAYER_UPDATED):
            self._players[event.object_id] = Player.from_dict(event.data)
            return
        if event.event == EventType.PLAYER_REMOVED:
            self._players.pop(event.object_id, None)
