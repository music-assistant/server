"""
Universal Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create player groups from all players known in the system.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from time import time
from typing import TYPE_CHECKING, Final, cast

import shortuuid
from aiohttp import web

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_CROSSFADE,
    CONF_ENTRY_CROSSFADE_DURATION,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    PlayerConfig,
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
from music_assistant.constants import CONF_GROUP_MEMBERS, CONF_HTTP_PROFILE, SYNCGROUP_PREFIX
from music_assistant.server.controllers.streams import DEFAULT_STREAM_HEADERS
from music_assistant.server.helpers.audio import get_ffmpeg_stream
from music_assistant.server.helpers.util import TaskManager
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


# ruff: noqa: ARG002

UGP_FORMAT = AudioFormat(
    content_type=ContentType.from_bit_depth(16), sample_rate=44100, bit_depth=16
)
UGP_PREFIX = "ugp_"

CONF_ACTION_CREATE_PLAYER = "create_player"
CONF_ACTION_CREATE_PLAYER_SAVE = "create_player_save"
CONF_ENTRY_SAMPLE_RATES_UGP = create_sample_rates_config_entry(44100, 16, 44100, 16, True)
CONF_GROUP_PLAYERS: Final[str] = "group_players"
CONF_NEW_GROUP_NAME: Final[str] = "name"
CONF_NEW_GROUP_MEMBERS: Final[list[str]] = "members"

CONFIG_ENTRY_UGP_NOTE = ConfigEntry(
    key="ugp_note",
    type=ConfigEntryType.LABEL,
    label="Please note that although the universal group "
    "allows you to group any player, it will not enable audio sync "
    "between players of different ecosystems. It is advised to always use native "
    "player groups or sync groups when available for your player type(s) and use "
    "the Universal Group only to group players of different ecosystems.",
    required=False,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return UniversalGroupProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    if not (ugp_provider := mass.get_provider(instance_id)):
        # UGP provider is not (yet) loaded
        return ()
    if TYPE_CHECKING:
        ugp_provider = cast(UniversalGroupProvider, ugp_provider)
    if action == CONF_ACTION_CREATE_PLAYER:
        # create new group player
        name = values.pop(CONF_NEW_GROUP_NAME)
        members: list[str] = values.pop(CONF_GROUP_MEMBERS)
        members = ugp_provider._filter_members(members)
        await ugp_provider.create_group(name, members)
        return (
            ConfigEntry(
                key="ugp_note",
                type=ConfigEntryType.LABEL,
                label=f"Your new Universal Group Player {name} has been created and "
                "is available in the players list.",
                required=False,
            ),
        )
    return (
        ConfigEntry(
            key="ugp_new",
            type=ConfigEntryType.LABEL,
            label="Fill in the details below to create a new Universal Group "
            "Player and click the 'Create new universal group' button.",
            required=False,
        ),
        ConfigEntry(
            key=CONF_NEW_GROUP_NAME,
            type=ConfigEntryType.STRING,
            label="Name",
            required=False,
        ),
        ConfigEntry(
            key=CONF_GROUP_MEMBERS,
            type=ConfigEntryType.STRING,
            label=CONF_NEW_GROUP_MEMBERS,
            default_value=[],
            options=tuple(
                ConfigValueOption(x.display_name, x.player_id)
                for x in mass.players.all(True, False)
            ),
            multi_value=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_ACTION_CREATE_PLAYER,
            type=ConfigEntryType.ACTION,
            label="Create new Universal Player Group",
            required=False,
        ),
        CONFIG_ENTRY_UGP_NOTE,
    )


class UniversalGroupProvider(PlayerProvider):
    """Base/builtin provider for universally grouping players."""

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return ()

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self._registered_routes: set[str] = set()
        self.streams: dict[str, UGPStream] = {}

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
                    if x.player_id != player_id and not x.player_id.startswith(UGP_PREFIX)
                ),
                description="Select all players you want to be part of this universal group",
                multi_value=True,
                required=True,
            ),
            CONFIG_ENTRY_UGP_NOTE,
            CONF_ENTRY_CROSSFADE,
            CONF_ENTRY_CROSSFADE_DURATION,
            CONF_ENTRY_SAMPLE_RATES_UGP,
        )

    def on_player_config_changed(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        if f"values/{CONF_GROUP_MEMBERS}" in changed_keys:
            player = self.mass.players.get(config.player_id)
            members = config.get_value(CONF_GROUP_MEMBERS)
            # ensure we filter invalid members
            members = self._filter_members(members)
            player.group_childs = members
            self.mass.config.set_raw_player_config_value(
                config.player_id, CONF_GROUP_PLAYERS, members
            )
            self.mass.players.update(config.player_id)

    def on_player_config_removed(self, player_id: str) -> None:
        """Call (by config manager) when the configuration of a player is removed."""
        # ensure that any group players get removed
        group_players = self.mass.config.get_raw_provider_config_value(
            self.instance_id, CONF_GROUP_PLAYERS, {}
        )
        if player_id in group_players:
            del group_players[player_id]
            self.mass.config.set_raw_provider_config_value(
                self.instance_id, CONF_GROUP_PLAYERS, group_players
            )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        group_player = self.mass.players.get(player_id)
        group_player.state = PlayerState.IDLE
        self.mass.players.update(player_id)
        # forward command to player and any connected sync child's
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                if member.state == PlayerState.IDLE:
                    continue
                tg.create_task(self.mass.players.cmd_stop(member.player_id))
        if (stream := self.streams.pop(player_id, None)) and not stream.done:
            await stream.stop()

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given UGP group player."""
        group_player = self.mass.players.get(player_id, True)

        if group_player.powered == powered:
            return  # nothing to do

        # make sure to update the group power state
        group_player.powered = powered

        any_member_powered = False
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
                any_member_powered = True
                if powered:
                    if member.state in (PlayerState.PLAYING, PlayerState.PAUSED):
                        # stop playing existing content on member if we start the group player
                        tg.create_task(self.cmd_stop(member.player_id))
                    # set active source to group player if the group (is going to be) powered
                    member.active_group = group_player.active_group
                    member.active_source = group_player.active_source
                    self.mass.players.update(member.player_id, skip_forward=True)
                else:
                    # turn off child player when group turns off
                    tg.create_task(self.cmd_power(member.player_id, False))
                    # reset active source on player
                    member.active_source = None
                    member.active_group = None
                    self.mass.players.update(member.player_id, skip_forward=True)
            # edge case: group turned on but no members are powered, power them all!
            # TODO: Do we want to make this configurable ?
            if not any_member_powered and powered:
                for member in self.mass.players.iter_group_members(
                    group_player, only_powered=False
                ):
                    tg.create_task(self.cmd_power(member.player_id, True))
                    member.active_group = group_player.player_id
                    member.active_source = group_player.active_source

        self.mass.players.update(player_id)

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
            await existing.stop()

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
        self.streams[player_id] = UGPStream(audio_source=audio_source, audio_format=UGP_FORMAT)
        base_url = f"{self.mass.streams.base_url}/ugp/{player_id}.aac"

        # forward to downstream play_media commands
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(group_player, only_powered=True):
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
        new_group_id = f"{UGP_PREFIX}{shortuuid.random(8).lower()}"
        # cleanup list, filter groups (should be handled by frontend, but just in case)
        members = self._filter_members(members)
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
            type=PlayerType.GROUP,
            name=name,
            available=True,
            powered=False,
            device_info=DeviceInfo(model="Group", manufacturer=self.name),
            supported_features=(PlayerFeature.VOLUME_SET, PlayerFeature.POWER),
            group_childs=set(members),
        )
        self.mass.players.register_or_update(player)
        # register dynamic route for the ugp stream
        route_path = f"/ugp/{group_player_id}.aac"
        self.mass.streams.register_dynamic_route(route_path, self._serve_ugp_stream)
        self._registered_routes.add(route_path)

        return player

    def on_group_child_power(
        self, group_player: Player, child_player: Player, new_power: bool, group_state: PlayerState
    ) -> None:
        """
        Call when a power command was executed on one of the child player of a PlayerGroup.

        The group state is sent with the state BEFORE the power command was executed.
        """
        if not group_player.powered:
            # guard, this should be caught in the player controller but just in case...
            return None

        powered_childs = [
            x
            for x in self.mass.players.iter_group_members(group_player, True)
            if not (not new_power and x.player_id == child_player.player_id)
        ]
        if new_power and child_player not in powered_childs:
            powered_childs.append(child_player)

        # if the last player of a group turned off, turn off the group
        if len(powered_childs) == 0:
            self.logger.debug(
                "Group %s has no more powered members, turning off group player",
                group_player.display_name,
            )
            self.mass.create_task(self.cmd_power(group_player.player_id, False))
            return False

        # if a child player turned ON while the group player is already playing
        # we just direct it to the existing stream (we dont care about the audio being in sync)
        if new_power and group_state == PlayerState.PLAYING:
            base_url = f"{self.mass.streams.base_url}/ugp/{group_player.player_id}.aac"
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
        child_player_id = request.query.get("player_id")  # optional!

        if not (ugp_player := self.mass.players.get(ugp_player_id)):
            raise web.HTTPNotFound(reason=f"Unknown UGP player: {ugp_player_id}")

        if not (stream := self.streams.get(ugp_player_id, None)) or stream.done:
            raise web.HTTPNotFound(body=f"There is no active UGP stream for {ugp_player_id}!")

        http_profile: str = await self.mass.config.get_player_config_value(
            child_player_id, CONF_HTTP_PROFILE
        )
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": "audio/aac",
            "Accept-Ranges": "none",
            "Cache-Control": "no-cache",
            "Connection": "close",
        }

        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        if http_profile == "forced_content_length":
            resp.content_length = 4294967296
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()

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
        async for chunk in stream.subscribe():
            try:
                await resp.write(chunk)
            except (ConnectionError, ConnectionResetError):
                break

        return resp

    def _filter_members(self, members: list[str]) -> list[str]:
        """Filter out members that are not valid players."""
        # cleanup members - filter out impossible choices
        syncgroup_childs: list[str] = []
        for member in members:
            if not member.startswith(SYNCGROUP_PREFIX):
                continue
            if syncgroup := self.mass.players.get(member):
                syncgroup_childs.extend(syncgroup.group_childs)
        # we filter out other UGP players and syncgroup childs
        # if their parent is already in the list
        return [
            x
            for x in members
            if self.mass.players.get(x)
            and x not in syncgroup_childs
            and not x.startswith(UGP_PREFIX)
        ]


class UGPStream:
    """
    Implementation of a Stream for the Universal Group Player.

    Basiclaly this is like a fake radio radio stream (AAC) format with multiple subscribers.
    The AAC format is chosen because it is widely supported and has a good balance between
    quality and bandwidth and also allows for mid-stream joining of (extra) players.
    """

    def __init__(
        self,
        audio_source: AsyncGenerator[bytes, None],
        audio_format: AudioFormat,
    ) -> None:
        """Initialize UGP Stream."""
        self.audio_source = audio_source
        self.input_format = audio_format
        self.output_format = AudioFormat(content_type=ContentType.AAC)
        self.subscribers: list[Callable[[bytes], Awaitable]] = []
        self._task: asyncio.Task | None = None
        self._done: asyncio.Event = asyncio.Event()

    @property
    def done(self) -> bool:
        """Return if this stream is already done."""
        return self._done.is_set() and self._task and self._task.done()

    async def stop(self) -> None:
        """Stop/cancel the stream."""
        if self._done.is_set():
            return
        if self._task and not self._task.done():
            self._task.cancel()
        self._done.set()

    async def subscribe(self) -> AsyncGenerator[bytes, None]:
        """Subscribe to the raw/unaltered audio stream."""
        # start the runner as soon as the (first) client connects
        if not self._task:
            self._task = asyncio.create_task(self._runner())
        queue = asyncio.Queue(1)
        try:
            self.subscribers.append(queue.put)
            while True:
                chunk = await queue.get()
                if not chunk:
                    break
                yield chunk
        finally:
            self.subscribers.remove(queue.put)

    async def _runner(self) -> None:
        """Run the stream for the given audio source."""
        await asyncio.sleep(0.25)  # small delay to allow subscribers to connect
        async for chunk in get_ffmpeg_stream(
            audio_input=self.audio_source,
            input_format=self.input_format,
            output_format=self.output_format,
            # TODO: enable readrate limiting + initial burst once we have a newer ffmpeg version
            # extra_input_args=["-readrate", "1.15"],
        ):
            await asyncio.gather(*[sub(chunk) for sub in self.subscribers], return_exceptions=True)
        # empty chunk when done
        await asyncio.gather(*[sub(b"") for sub in self.subscribers], return_exceptions=True)
        self._done.set()
