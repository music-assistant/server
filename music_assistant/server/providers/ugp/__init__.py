"""
Universal Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create player groups from all players known in the system.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from time import time
from typing import TYPE_CHECKING

import shortuuid

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE_DURATION,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
    QueueOption,
    StreamType,
)
from music_assistant.common.models.media_items import (
    AudioFormat,
    MediaItemType,
    ProviderMapping,
    Radio,
)
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import CONF_CROSSFADE, CONF_GROUP_MEMBERS, SYNCGROUP_PREFIX
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


# ruff: noqa: ARG002

UGP_FORMAT = AudioFormat(
    content_type=ContentType.from_bit_depth(24), sample_rate=48000, bit_depth=24
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return UniversalGroupProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return ()


class UniversalGroupProvider(PlayerProvider):
    """Base/builtin provider for universally grouping players."""

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return (ProviderFeature.PLAYER_GROUP_CREATE,)

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self._ugp_streamers: dict[str, asyncio.Task] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._register_all_players()

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                label="Group members",
                default_value=[],
                options=tuple(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in self.mass.players.all(True, False)
                    if x.player_id != player_id
                ),
                description="Select all players you want to be part of this universal group",
                multi_value=True,
                required=True,
            ),
            ConfigEntry(
                key="ugp_note",
                type=ConfigEntryType.ALERT,
                label="Please note that although the universal group "
                "allows you to group any player, it will not enable audio sync "
                "between players of different ecosystems.",
            ),
            ConfigEntry(
                key=CONF_CROSSFADE,
                type=ConfigEntryType.BOOLEAN,
                label="Enable crossfade",
                default_value=False,
                description="Enable a crossfade transition between (queue) tracks. \n\n"
                "Note that DLNA does not natively support crossfading so you need to enable "
                "the 'flow mode' workaround to use crossfading with DLNA players.",
                category="audio",
            ),
            CONF_ENTRY_CROSSFADE_DURATION,
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.state = PlayerState.IDLE
        self.mass.players.update(player_id)
        # forward command to player and any connected sync child's
        async with asyncio.TaskGroup() as tg:
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                if member.state == PlayerState.IDLE:
                    continue
                tg.create_task(self.mass.players.cmd_stop(member.player_id))
        if (stream := self._ugp_streamers.pop(player_id, None)) and not stream.done():
            stream.cancel()

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        await self.mass.players.cmd_group_power(player_id, powered)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # group volume is already handled in the player manager

    async def play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        # power ON
        await self.cmd_power(player_id, True)
        group_player = self.mass.players.get(player_id)
        # stop any existing stream first
        if (existing := self._ugp_streamers.pop(player_id, None)) and not existing.done():
            existing.cancel()

        # special case: handle announcement sent to this UGP
        # we just forward this as-is downstream and let all child players handle this themselves
        if queue_item.media_type == MediaType.ANNOUNCEMENT:
            async with asyncio.TaskGroup() as tg:
                for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                    if member.player_id.startswith(SYNCGROUP_PREFIX):
                        member = self.mass.players.get_sync_leader(member)  # noqa: PLW2901
                        if member is None:
                            continue
                    player_prov = self.mass.players.get_player_provider(member.player_id)
                    tg.create_task(player_prov.play_media(member.player_id, queue_item))
            return

        # create a fake radio item to forward to downstream play_media commands
        ugp_item = await self.get_item(MediaType.RADIO, player_id)

        self._subscribers[player_id] = []
        self._ugp_streamers[player_id] = asyncio.create_task(self._ugp_streamer(player_id))

        # insert the fake queue item into all underlying playerqueues
        async with asyncio.TaskGroup() as tg:
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                if member.player_id.startswith(SYNCGROUP_PREFIX):
                    member = self.mass.players.get_sync_leader(member)  # noqa: PLW2901
                    if member is None:
                        continue
                tg.create_task(
                    self.mass.player_queues.play_media(
                        member.player_id, ugp_item, option=QueueOption.PLAY
                    )
                )
        # set the state optimistically
        group_player.elapsed_time = 0
        group_player.elapsed_time_last_updated = time() - 1
        group_player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

    async def create_group(self, name: str, members: list[str]) -> Player:
        """Create new PlayerGroup on this provider.

        Create a new PlayerGroup with given name and members.

            - name: Name for the new group to create.
            - members: A list of player_id's that should be part of this group.
        """
        new_group_id = f"{self.domain}_{shortuuid.random(8).lower()}"
        # cleanup list, filter groups (should be handled by frontend, but just in case)
        members = [
            x.player_id
            for x in self.mass.players
            if x.player_id in members
            if x.provider != self.instance_id
        ]
        # create default config with the user chosen name
        self.mass.config.create_default_player_config(
            new_group_id,
            self.instance_id,
            name=name,
            enabled=True,
            values={CONF_GROUP_MEMBERS: members},
        )
        return self._register_group_player(new_group_id, name=name, members=members)

    async def _register_all_players(self) -> None:
        """Register all (virtual/fake) group players in the Player controller."""
        player_configs = await self.mass.config.get_player_configs(
            self.instance_id, include_values=True
        )
        for player_config in player_configs:
            members = player_config.get_value(CONF_GROUP_MEMBERS)
            self._register_group_player(
                player_config.player_id,
                player_config.name or player_config.default_name,
                members,
            )

    def _register_group_player(
        self, group_player_id: str, name: str, members: Iterable[str]
    ) -> Player:
        """Register a UGP group player in the Player controller."""
        player = Player(
            player_id=group_player_id,
            provider=self.instance_id,
            type=PlayerType.SYNC_GROUP,
            name=name,
            available=True,
            powered=False,
            device_info=DeviceInfo(model="Group", manufacturer=self.name),
            supported_features=(PlayerFeature.VOLUME_SET, PlayerFeature.POWER),
            group_childs=set(members),
        )
        self.mass.players.register_or_update(player)
        return player

    def on_child_power(self, player_id: str, child_player_id: str, new_power: bool) -> None:
        """
        Call when a power command was executed on one of the child player of a PlayerGroup.

        This is used to handle special actions such as (re)syncing.
        """
        group_player = self.mass.players.get(player_id)
        child_player = self.mass.players.get(child_player_id)

        if not group_player.powered:
            # guard, this should be caught in the player controller but just in case...
            return None

        powered_childs = [
            x
            for x in self.mass.players.iter_group_members(group_player, True)
            if not (not new_power and x.player_id == child_player_id)
        ]
        if new_power and child_player not in powered_childs:
            powered_childs.append(child_player)

        # if the last player of a group turned off, turn off the group
        if len(powered_childs) == 0:
            self.logger.debug(
                "Group %s has no more powered members, turning off group player",
                group_player.display_name,
            )
            self.mass.create_task(self.cmd_power(player_id, False))
            return False

        # if a child player turned ON while the group player is already playing
        # we just direct it to the existing stream
        if new_power and group_player.state == PlayerState.PLAYING:

            async def join_player() -> None:
                ugp_item = await self.get_item(MediaType.RADIO, player_id)
                await self.mass.player_queues.play_media(
                    child_player.player_id, ugp_item, option=QueueOption.PLAY
                )

            self.mass.create_task(join_player())
        return None

    async def get_item(self, media_type: MediaType, prov_item_id: str) -> MediaItemType:
        """Get single MediaItem from provider."""
        return Radio(
            item_id=prov_item_id,
            provider=self.instance_id,
            name="Music Assistant - UGP",
            provider_mappings={
                ProviderMapping(
                    item_id=prov_item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=UGP_FORMAT,
                )
            },
        )

    async def on_streamed(self, streamdetails: StreamDetails, seconds_streamed: int) -> None:
        """Handle callback when an item completed streaming."""

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=UGP_FORMAT,
            media_type=MediaType.UNKNOWN,
            stream_type=StreamType.CUSTOM,
            duration=None,
            can_seek=False,
        )

    async def get_audio_stream(  # type: ignore[return]
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the (custom) audio stream for the provider item."""
        player_id = streamdetails.item_id
        if not (stream := self._ugp_streamers.get(player_id, None)) or stream.done():
            # edge case: player itself requests our stream while we have no ugp session active
            # just end some silence so the player can move on doing other stuff
            for _ in range(30):
                yield b"\0" * int(UGP_FORMAT.sample_rate * (UGP_FORMAT.bit_depth / 8) * 2)
            return
        try:
            queue = asyncio.Queue(1)
            self._subscribers[player_id].append(queue)
            while True:
                chunk = await queue.get()
                if not chunk:
                    return
                yield chunk
        finally:
            with suppress(ValueError):
                self._subscribers[player_id].remove(queue)

    async def _ugp_streamer(self, player_id: str) -> None:
        """Run the UGP Flow stream."""
        queue = self.mass.player_queues.get(player_id)
        # wait for first subscriber
        count = 0
        while count < 50:
            await asyncio.sleep(0.5)
            count += 1
            if len(self._subscribers[player_id]) > 0:
                break
            if count == 50:
                return
        async for chunk in self.mass.streams.get_flow_stream(
            queue=queue,
            start_queue_item=queue.current_item,
            pcm_format=UGP_FORMAT,
        ):
            if len(self._subscribers[player_id]) == 0:
                return
            async with asyncio.TaskGroup() as tg:
                for sub in list(self._subscribers[player_id]):
                    tg.create_task(sub.put(chunk))
        for sub in list(self._subscribers[player_id]):
            self.mass.create_task(sub.put(b""))
