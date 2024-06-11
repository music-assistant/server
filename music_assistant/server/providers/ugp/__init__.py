"""
Universal Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create player groups from all players known in the system.
"""

from __future__ import annotations

import asyncio
from time import time
from typing import TYPE_CHECKING

import shortuuid
from aiohttp import web

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    create_sample_rates_config_entry,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant.common.models.media_items import AudioFormat
from music_assistant.common.models.player import DeviceInfo, Player, PlayerMedia
from music_assistant.constants import CONF_GROUP_MEMBERS, SYNCGROUP_PREFIX
from music_assistant.server.controllers.streams import DEFAULT_STREAM_HEADERS
from music_assistant.server.helpers.audio import get_ffmpeg_stream, get_player_filter_params
from music_assistant.server.helpers.multi_client_stream import MultiClientStream
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

CONF_ENTRY_SAMPLE_RATES_UGP = create_sample_rates_config_entry(48000, 24, 48000, 24, True)


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
        self._registered_routes: set[str] = set()
        self.streams: dict[str, MultiClientStream] = {}

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._register_all_players()

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        for route_path in list(self._registered_routes):
            self._registered_routes.remove(route_path)
            self.mass.streams.unregister_dynamic_route(route_path)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
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
                required=False,
            ),
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_CROSSFADE_DURATION,
            CONF_ENTRY_SAMPLE_RATES_UGP,
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
        if (stream := self.streams.pop(player_id, None)) and not stream.done:
            await stream.stop()

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
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        # power ON
        await self.cmd_power(player_id, True)
        group_player = self.mass.players.get(player_id)
        # stop any existing stream first
        if (existing := self.streams.pop(player_id, None)) and not existing.done:
            existing.task.cancel()

        # select audio source
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            audio_source = self.mass.streams.get_announcement_stream(
                media.custom_data["url"],
                output_format=UGP_FORMAT,
                use_pre_announce=media.custom_data["use_pre_announce"],
            )
        elif media.queue_id and media.queue_item_id:
            # regular queue stream request
            audio_source = self.mass.streams.get_flow_stream(
                queue=self.mass.player_queues.get(media.queue_id),
                start_queue_item=self.mass.player_queues.get_item(
                    media.queue_id, media.queue_item_id
                ),
                pcm_format=UGP_FORMAT,
            )
        else:
            # assume url or some other direct path
            # NOTE: this will fail if its an uri not playable by ffmpeg
            audio_source = get_ffmpeg_stream(
                audio_input=media.uri,
                input_format=AudioFormat(ContentType.try_parse(media.uri)),
                output_format=UGP_FORMAT,
            )

        # start the stream task
        self.streams[player_id] = MultiClientStream(
            audio_source=audio_source, audio_format=UGP_FORMAT
        )
        base_url = f"{self.mass.streams.base_url}/ugp/{player_id}.flac"

        # forward to downstream play_media commands
        async with asyncio.TaskGroup() as tg:
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                if member.player_id.startswith(SYNCGROUP_PREFIX):
                    member = self.mass.players.get_sync_leader(member)  # noqa: PLW2901
                    if member is None:
                        continue
                tg.create_task(
                    self.mass.players.play_media(
                        member.player_id,
                        media=PlayerMedia(
                            uri=f"{base_url}?player_id={member.player_id}",
                            media_type=MediaType.FLOW_STREAM,
                            title=group_player.display_name,
                            queue_id=group_player.player_id,
                        ),
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
        # register dynamic routes for the ugp stream (both flac and mp3)
        for fmt in ("mp3", "flac"):
            route_path = f"/ugp/{group_player_id}.{fmt}"
            self.mass.streams.register_dynamic_route(route_path, self._serve_ugp_stream)
            self._registered_routes.add(route_path)

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
        # we just direct it to the existing stream (we dont care about the audio being in sync)
        if new_power and group_player.state == PlayerState.PLAYING:
            base_url = f"{self.mass.streams.base_url}/ugp/{player_id}.flac"
            self.mass.create_task(
                self.mass.players.play_media(
                    child_player.player_id,
                    media=PlayerMedia(
                        uri=f"{base_url}?player_id={child_player.player_id}",
                        media_type=MediaType.FLOW_STREAM,
                        title=group_player.display_name,
                        queue_id=group_player.player_id,
                    ),
                )
            )

        return None

    async def _serve_ugp_stream(self, request: web.Request) -> web.Response:
        """Serve the UGP (multi-client) flow stream audio to a player."""
        ugp_player_id = request.path.rsplit(".")[0].rsplit("/")[-1]
        fmt = request.path.rsplit(".")[-1]
        child_player_id = request.query.get("player_id")  # optional!

        if not (ugp_player := self.mass.players.get(ugp_player_id)):
            raise web.HTTPNotFound(reason=f"Unknown UGP player: {ugp_player_id}")

        if not (stream := self.streams.get(ugp_player_id, None)) or stream.done:
            raise web.HTTPNotFound(body=f"There is no active UGP stream for {ugp_player_id}!")

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                **DEFAULT_STREAM_HEADERS,
                "Content-Type": f"audio/{fmt}",
            },
        )
        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        # all checks passed, start streaming!
        self.logger.debug(
            "Start serving UGP flow audio stream for UGP-player %s to %s",
            ugp_player.display_name,
            child_player_id or request.remote,
        )

        async for chunk in stream.get_stream(
            output_format=AudioFormat(content_type=ContentType.try_parse(fmt)),
            filter_params=get_player_filter_params(self.mass, child_player_id)
            if child_player_id
            else None,
        ):
            try:
                await resp.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # race condition
                break

        return resp
