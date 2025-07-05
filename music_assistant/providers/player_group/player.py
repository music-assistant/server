"""Group Player implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from time import time
from typing import TYPE_CHECKING, Final, cast

import shortuuid
from aiohttp import web
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
)
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioFormat, UniqueList

from music_assistant.constants import (
    CONF_CROSSFADE,
    CONF_CROSSFADE_DURATION,
    CONF_ENABLE_ICY_METADATA,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_FLOW_MODE,
    CONF_GROUP_MEMBERS,
    CONF_HTTP_PROFILE,
    CONF_OUTPUT_CODEC,
    CONF_SAMPLE_RATES,
    DEFAULT_PCM_FORMAT,
    create_sample_rates_config_entry,
)
from music_assistant.controllers.streams import DEFAULT_STREAM_HEADERS
from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.util import TaskManager
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .ugp_stream import UGPStream

if TYPE_CHECKING:
    from music_assistant.models.player_provider import PlayerProvider

    from .provider import PlayerGroupProvider


UGP_FORMAT = AudioFormat(
    content_type=DEFAULT_PCM_FORMAT.content_type,
    sample_rate=DEFAULT_PCM_FORMAT.sample_rate,
    bit_depth=DEFAULT_PCM_FORMAT.bit_depth,
)

# ruff: noqa: ARG002

UNIVERSAL_PREFIX: Final[str] = "ugp_"
SYNCGROUP_PREFIX: Final[str] = "syncgroup_"
GROUP_TYPE_UNIVERSAL: Final[str] = "universal"
CONF_GROUP_TYPE: Final[str] = "group_type"
CONF_ENTRY_GROUP_TYPE = ConfigEntry(
    key=CONF_GROUP_TYPE,
    type=ConfigEntryType.STRING,
    label="Group type",
    default_value="universal",
    hidden=True,
    required=True,
)
CONF_ENTRY_GROUP_MEMBERS = ConfigEntry(
    key=CONF_GROUP_MEMBERS,
    type=ConfigEntryType.STRING,
    multi_value=True,
    label="Group members",
    default_value=[],
    description="Select all players you want to be part of this group",
    required=False,  # otherwise dynamic members won't work (which allows empty members list)
)
CONF_ENTRY_SAMPLE_RATES_UGP = create_sample_rates_config_entry(
    max_sample_rate=96000, max_bit_depth=24, hidden=True
)
CONFIG_ENTRY_UGP_NOTE = ConfigEntry(
    key="ugp_note",
    type=ConfigEntryType.LABEL,
    label="Please note that although the Universal Group "
    "allows you to group any player, it will not enable audio sync "
    "between players of different ecosystems. It is advised to always use native "
    "player groups or sync groups when available for your player type(s) and use "
    "the Universal Group only to group players of different ecosystems/protocols.",
    required=False,
)
CONFIG_ENTRY_DYNAMIC_MEMBERS = ConfigEntry(
    key="dynamic_members",
    type=ConfigEntryType.BOOLEAN,
    label="Enable dynamic members",
    description="Allow members to (temporary) join/leave the group dynamically, "
    "so the group more or less behaves the same like manually syncing players together, "
    "with the main difference being that the groupplayer will hold the queue.",
    default_value=False,
    required=False,
)


class GroupPlayer(Player):
    """Group Player implementation."""

    def __init__(
        self,
        provider: PlayerGroupProvider,
        player_id: str,
        group_type: str,
        name: str,
        members: Iterable[str],
    ) -> None:
        """Initialize GroupPlayer instance."""
        super().__init__(provider, player_id)
        self.group_type = group_type
        self._attr_name = name
        self._attr_type = PlayerType.GROUP
        self._attr_available = True
        self._attr_powered = False  # group players are always powered off by default
        self._attr_needs_poll = True
        self._attr_poll_interval = 30
        self._attr_active_source = player_id
        self._attr_group_childs = UniqueList(members)

        # Set up player features and device info based on group type
        self._setup_player_attributes()

    def _setup_player_attributes(self) -> None:
        """Set up player attributes based on group type."""
        player_features = {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
        }

        if self.group_type == GROUP_TYPE_UNIVERSAL:
            model_name = "Universal Group"
            manufacturer = self.provider.name
            self._attr_can_group_with = {
                # allow grouping with all providers, except the playergroup provider itself
                x.instance_id
                for x in self.mass.players.providers
                if x.instance_id != self.provider.instance_id
            }
            player_features.add(PlayerFeature.MULTI_DEVICE_DSP)
            # register dynamic route for the ugp stream
            self.provider._on_unload.append(
                self.mass.streams.register_dynamic_route(
                    f"/ugp/{self.player_id}.flac", self._serve_ugp_stream
                )
            )
            self.provider._on_unload.append(
                self.mass.streams.register_dynamic_route(
                    f"/ugp/{self.player_id}.mp3", self._serve_ugp_stream
                )
            )
        elif player_provider := self.mass.get_provider(self.group_type):
            model_name = "Sync Group"
            manufacturer = player_provider.name
            self._attr_can_group_with = {player_provider.instance_id}
            for feature in (
                PlayerFeature.PAUSE,
                PlayerFeature.VOLUME_MUTE,
                PlayerFeature.ENQUEUE,
                PlayerFeature.MULTI_DEVICE_DSP,
                PlayerFeature.GAPLESS_PLAYBACK,
                PlayerFeature.GAPLESS_DIFFERENT_SAMPLERATE,
            ):
                if all(feature in x.supported_features for x in player_provider.players):
                    player_features.add(feature)
        else:
            raise PlayerUnavailableError(
                f"Provider for syncgroup {self.group_type} is not available!"
            )

        if self.mass.config.get_raw_player_config_value(
            self.player_id,
            CONFIG_ENTRY_DYNAMIC_MEMBERS.key,
            CONFIG_ENTRY_DYNAMIC_MEMBERS.default_value,
        ):
            player_features.add(PlayerFeature.SET_MEMBERS)

        self._attr_supported_features = player_features
        self._attr_device_info = DeviceInfo(model=model_name, manufacturer=manufacturer)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        # default entries for player groups
        base_entries = [
            *await super().get_config_entries(),
            CONF_ENTRY_GROUP_TYPE,
            CONF_ENTRY_GROUP_MEMBERS,
            CONFIG_ENTRY_DYNAMIC_MEMBERS,
        ]
        # group type is static and can not be changed. we just grab the existing, stored value
        group_type: str = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_GROUP_TYPE, GROUP_TYPE_UNIVERSAL
        )
        # handle config entries for universal group players
        if group_type == GROUP_TYPE_UNIVERSAL:
            group_members = CONF_ENTRY_GROUP_MEMBERS
            group_members.options = tuple(
                ConfigValueOption(x.display_name, x.player_id)
                for x in self.mass.players.all(True, False)
                if not x.player_id.startswith(UNIVERSAL_PREFIX)
            )
            return [
                *base_entries,
                group_members,
                CONFIG_ENTRY_UGP_NOTE,
                CONF_ENTRY_SAMPLE_RATES_UGP,
                CONF_ENTRY_FLOW_MODE_ENFORCED,
            ]
        # handle config entries for syncgroup players
        group_members = CONF_ENTRY_GROUP_MEMBERS
        if player_prov := self.mass.get_provider(group_type):
            group_members.options = tuple(
                ConfigValueOption(x.display_name, x.player_id) for x in player_prov.players
            )

        # grab additional details from one of the provider's players
        if not (player_provider := self.mass.get_provider(group_type)):
            return base_entries  # guard
        if TYPE_CHECKING:
            player_provider = cast("PlayerProvider", player_provider)
        assert player_provider.instance_id != self.provider.instance_id
        if not (child_player := next((x for x in player_provider.players), None)):
            return base_entries  # guard

        # combine base group entries with (base) player entries for this player type
        allowed_conf_entries = (
            CONF_HTTP_PROFILE,
            CONF_ENABLE_ICY_METADATA,
            CONF_CROSSFADE,
            CONF_CROSSFADE_DURATION,
            CONF_OUTPUT_CODEC,
            CONF_FLOW_MODE,
            CONF_SAMPLE_RATES,
        )
        child_config_entries = await child_player.get_config_entries()
        return [
            *base_entries,
            group_members,
            *(entry for entry in child_config_entries if entry.key in allowed_conf_entries),
        ]

    async def stop(self) -> None:
        """Send STOP command to given player."""
        # syncgroup: forward command to sync leader
        if self.player_id.startswith(SYNCGROUP_PREFIX):
            if sync_leader := self._get_sync_leader():
                if self.mass.get_provider(sync_leader.provider):
                    await sync_leader.stop()
            return
        # ugp: forward command to all members
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(self, active_only=True):
                tg.create_task(member.stop())
        # abort the stream session
        if (stream := self.provider.ugp_streams.pop(self.player_id, None)) and not stream.done:
            await stream.stop()

    async def play(self) -> None:
        """Send PLAY command to given player."""
        if not self.player_id.startswith(SYNCGROUP_PREFIX):
            # this shouldn't happen, but just in case
            raise UnsupportedFeaturedException
        # forward command to sync leader
        if sync_leader := self._get_sync_leader():
            await sync_leader.play()

    async def pause(self) -> None:
        """Send PAUSE command to given player."""
        if not self.player_id.startswith(SYNCGROUP_PREFIX):
            # this shouldn't happen, but just in case
            raise UnsupportedFeaturedException
        # forward command to sync leader
        if sync_leader := self._get_sync_leader():
            await sync_leader.pause()

    async def power(self, powered: bool) -> None:
        """Handle POWER command to group player."""
        # always stop at power off
        if not powered and self.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.stop()

        if powered and self.player_id.startswith(SYNCGROUP_PREFIX):
            await self._form_syncgroup()

        if powered:
            # handle TURN_ON of the group player by turning on all members
            for member in self.mass.players.iter_group_members(
                self, only_powered=False, active_only=False
            ):
                if (
                    member.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
                    and member.active_source != self.active_source
                ):
                    # stop playing existing content on member if we start the group player
                    await member.stop()
                if member.active_group not in (
                    None,
                    self.player_id,
                    member.player_id,
                ):
                    # collision: child player is part of multiple groups
                    # and another group already active !
                    # solve this by powering off the other group
                    await self.mass.players.cmd_power(member.active_group, False)
                    await asyncio.sleep(1)
                if not member.powered and member.power_control != PLAYER_CONTROL_NONE:
                    member.active_group = None  # needed to prevent race conditions
                    await self.mass.players.cmd_power(member.player_id, True)
                # set active source to group player if the group (is going to be) powered
                member.active_group = self.player_id
                member.active_source = self.active_source
        else:
            # handle TURN_OFF of the group player by turning off all members
            # optimistically set the group state to prevent race conditions
            self._attr_powered = False
            for member in self.mass.players.iter_group_members(
                self, only_powered=True, active_only=True
            ):
                # reset active group on player when the group is turned off
                member.active_group = None
                member.active_source = None
                if member.synced_to:
                    # always ungroup first
                    await self.mass.players.cmd_ungroup(member.player_id)
                # handle TURN_OFF of the group player by turning off all members
                if member.powered and member.power_control != PLAYER_CONTROL_NONE:
                    await self.mass.players.cmd_power(member.player_id, False)

        # optimistically set the group state
        self._attr_powered = powered
        self.update_state()
        if not powered:
            # reset the original group members when powered off
            self._attr_group_childs.set(
                self.mass.config.get_raw_player_config_value(self.player_id, CONF_GROUP_MEMBERS, [])
            )

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # group volume is already handled in the player manager

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        # power on (which will also resync) if needed
        await self.power(True)

        # handle play_media for sync group
        if self.player_id.startswith(SYNCGROUP_PREFIX):
            # simply forward the command to the sync leader
            sync_leader = self._get_sync_leader()
            await sync_leader.play_media(media)
            return

        # handle play_media for UGP group
        if (existing := self.provider.ugp_streams.pop(self.player_id, None)) and not existing.done:
            # stop any existing stream first
            await existing.stop()

        # select audio source
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            audio_source = self.mass.streams.get_announcement_stream(
                media.custom_data["url"],
                output_format=UGP_FORMAT,
                use_pre_announce=media.custom_data["use_pre_announce"],
            )
        elif media.media_type == MediaType.PLUGIN_SOURCE:
            # special case: plugin source stream
            audio_source = self.mass.streams.get_plugin_source_stream(
                plugin_source_id=media.custom_data["source_id"],
                output_format=UGP_FORMAT,
                player_id=media.custom_data["player_id"],
            )
        elif media.queue_id and media.queue_item_id:
            # regular queue stream request
            audio_source = self.mass.streams.get_queue_flow_stream(
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
        self.provider.ugp_streams[self.player_id] = UGPStream(
            audio_source=audio_source, audio_format=UGP_FORMAT, base_pcm_format=UGP_FORMAT
        )
        base_url = f"{self.mass.streams.base_url}/ugp/{self.player_id}.flac"

        # set the state optimistically
        self._attr_current_media = media
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time() - 1
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

        # forward to downstream play_media commands
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(
                self, only_powered=True, active_only=True
            ):
                tg.create_task(
                    member.play_media(
                        PlayerMedia(
                            uri=f"{base_url}?player_id={member.player_id}",
                            media_type=MediaType.FLOW_STREAM,
                            title=self.display_name,
                            queue_id=self.player_id,
                        )
                    )
                )

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of a next media item on the player."""
        if not self.player_id.startswith(SYNCGROUP_PREFIX):
            # this shouldn't happen, but just in case
            raise UnsupportedFeaturedException("Command is not supported for UGP players")
        if sync_leader := self._get_sync_leader():
            await sync_leader.enqueue_next_media(media)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        # This would be implemented similar to cmd_group/cmd_ungroup_member logic
        # from the original implementation

    async def poll(self) -> None:
        """Poll player for state updates."""
        self._update_attributes()
        if self.powered:
            await self._ungroup_subgroups_if_found()

    async def remove(self) -> None:
        """Remove a group player."""
        if self.powered:
            # edge case: the group player is powered and being removed
            # make sure to turn it off first (which will also ungroup a syncgroup)
            await self.power(False)

    def _get_sync_leader(self) -> Player:
        """Get the active sync leader player for the syncgroup."""
        for child_player in self.mass.players.iter_group_members(
            self, only_powered=False, only_playing=False, active_only=False
        ):
            # the syncleader is always the first player in the group
            return child_player
        raise RuntimeError("No players available in syncgroup")

    async def _form_syncgroup(self) -> None:
        """Form syncgroup by sync all (possible) members."""
        sync_leader = await self._select_sync_leader()
        # ensure the sync leader is first in the list
        self._attr_group_childs.set(
            [
                sync_leader.player_id,
                *[x for x in self.group_childs if x != sync_leader.player_id],
            ]
        )
        members_to_sync: list[str] = []
        for member in self.mass.players.iter_group_members(self, active_only=False):
            if member.synced_to and member.synced_to != sync_leader.player_id:
                # ungroup first
                await self.mass.players.cmd_ungroup(member.player_id)
            if sync_leader.player_id == member.player_id:
                # skip sync leader
                continue
            if (
                member.synced_to == sync_leader.player_id
                and member.player_id in sync_leader.group_childs
            ):
                # already synced
                continue
            members_to_sync.append(member.player_id)
        if members_to_sync:
            await self.mass.players.cmd_group_many(sync_leader.player_id, members_to_sync)

    async def _select_sync_leader(self) -> Player:
        """Select the active sync leader player for a syncgroup."""
        # prefer the first player that already has sync childs
        for prefer_sync_leader in (True, False):
            for child_player in self.mass.players.iter_group_members(self):
                if prefer_sync_leader and child_player.synced_to:
                    continue
                if child_player.active_group not in (
                    None,
                    self.player_id,
                    child_player.player_id,
                ):
                    # this should not happen (because its already handled in the power on logic),
                    # but guard it just in case bad things happen
                    continue
                return child_player
        raise RuntimeError("No players available to form syncgroup")

    def _update_attributes(self) -> None:
        """Update attributes of a player."""
        group_type = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_ENTRY_GROUP_TYPE.key, CONF_ENTRY_GROUP_TYPE.default_value
        )
        # grab current media and state from one of the active players
        for child_player in self.mass.players.iter_group_members(
            self, active_only=True, only_playing=True
        ):
            if child_player.synced_to:
                # ignore child players
                continue
            if child_player.active_source not in (None, self.active_source):
                # this should not happen but guard just in case
                continue
            self._attr_playback_state = child_player.state
            if child_player.current_media:
                self._attr_current_media = child_player.current_media
            self._attr_elapsed_time = child_player.elapsed_time
            self._attr_elapsed_time_last_updated = child_player.elapsed_time_last_updated
            break
        else:
            self._attr_playback_state = PlaybackState.IDLE
        if group_type == GROUP_TYPE_UNIVERSAL:
            can_group_with = {
                # allow grouping with all providers, except the playergroup provider itself
                x.instance_id
                for x in self.mass.players.providers
                if x.instance_id != self.provider.instance_id
            }
        elif sync_player_provider := self.mass.get_provider(group_type):
            can_group_with = {sync_player_provider.instance_id}
        else:
            can_group_with = set()
        self._attr_can_group_with = can_group_with
        self.update_state()

    async def _ungroup_subgroups_if_found(self) -> None:
        """Verify that no player is part of a separate group."""
        group_type = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_ENTRY_GROUP_TYPE.key, CONF_ENTRY_GROUP_TYPE.default_value
        )
        if group_type != GROUP_TYPE_UNIVERSAL:
            return

        changed = False
        # Verify that no player is part of a separate group
        for child_player_id in self.group_members:
            child_player = self.mass.players.get(child_player_id)
            if child_player is None:
                continue
            if PlayerFeature.SET_MEMBERS not in child_player.supported_features:
                continue
            if child_player.group_childs:
                # This is a leader in another group
                for sync_child_id in child_player.group_childs:
                    if sync_child_id == child_player_id:
                        continue
                    await child_player.ungroup_member(sync_child_id)
                    changed = True
            if child_player.synced_to:
                # This is a member of another group
                synced_group = self.mass.players.get(child_player.synced_to)
                if synced_group:
                    await synced_group.ungroup_member(child_player.player_id)
                changed = True
        if changed and self.state == PlaybackState.PLAYING:
            # Restart playback to ensure all members play the same content
            await self.mass.player_queues.resume(self.player_id, False)

    async def _serve_ugp_stream(self, request: web.Request) -> web.Response:
        """Serve the UGP (multi-client) flow stream audio to a player."""
        ugp_player_id = request.path.rsplit(".")[0].rsplit("/")[-1]
        child_player_id = request.query.get("player_id")  # optional!
        output_format_str = request.path.rsplit(".")[-1]

        if child_player_id and (child_player := self.mass.players.get(child_player_id)):
            # Use the preferred output format of the child player
            output_format = await self.mass.streams.get_output_format(
                output_format_str=output_format_str,
                player=child_player,
                content_sample_rate=UGP_FORMAT.sample_rate,
                content_bit_depth=UGP_FORMAT.bit_depth,
            )
        elif output_format_str == "flac":
            output_format = AudioFormat(content_type=ContentType.FLAC)
        else:
            output_format = AudioFormat(content_type=ContentType.MP3)

        if not (ugp_player := self.mass.players.get(ugp_player_id)):
            raise web.HTTPNotFound(reason=f"Unknown UGP player: {ugp_player_id}")

        if not (stream := self.provider.ugp_streams.get(ugp_player_id, None)) or stream.done:
            raise web.HTTPNotFound(body=f"There is no active UGP stream for {ugp_player_id}!")

        http_profile: str = await self.mass.config.get_player_config_value(
            child_player_id, CONF_HTTP_PROFILE
        )
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "Content-Type": f"audio/{output_format_str}",
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

        # Generate filter params for the player specific DSP settings
        filter_params = None
        if child_player_id:
            filter_params = get_player_filter_params(
                self.mass, child_player_id, stream.input_format, output_format
            )

        async for chunk in stream.get_stream(
            output_format,
            filter_params=filter_params,
        ):
            try:
                await resp.write(chunk)
            except (ConnectionError, ConnectionResetError):
                break

        return resp

    @staticmethod
    async def create_group(
        provider: PlayerGroupProvider,
        group_type: str,
        name: str,
        members: list[str],
        dynamic: bool = False,
    ) -> Player:
        """Create new Group Player."""
        # perform basic checks
        if group_type == GROUP_TYPE_UNIVERSAL:
            prefix = UNIVERSAL_PREFIX
        else:
            prefix = SYNCGROUP_PREFIX
            if (player_prov := provider.mass.get_provider(group_type)) is None:
                msg = f"Provider {group_type} is not available!"
                raise ProviderUnavailableError(msg)
            if ProviderFeature.SYNC_PLAYERS not in player_prov.supported_features:
                msg = f"Provider {player_prov.name} does not support creating groups"
                raise UnsupportedFeaturedException(msg)
            group_type = player_prov.instance_id  # just in case only domain was sent

        new_group_id = f"{prefix}{shortuuid.random(8).lower()}"
        # cleanup list, just in case the frontend sends some garbage
        members = GroupPlayer._filter_members(provider, group_type, members)
        # create default config with the user chosen name
        provider.mass.config.create_default_player_config(
            new_group_id,
            provider.instance_id,
            name=name,
            enabled=True,
            values={
                CONF_GROUP_MEMBERS: members,
                CONF_GROUP_TYPE: group_type,
                CONFIG_ENTRY_DYNAMIC_MEMBERS.key: dynamic,
            },
        )
        return await GroupPlayer._register_group_player(
            provider=provider,
            group_player_id=new_group_id,
            group_type=group_type,
            name=name,
            members=members,
        )

    @staticmethod
    async def register_all_players(provider: PlayerGroupProvider) -> None:
        """Register all (virtual/fake) group players in the Player controller."""
        player_configs = await provider.mass.config.get_player_configs(
            provider.instance_id, include_values=True
        )
        for player_config in player_configs:
            if provider.mass.players.get(player_config.player_id):
                continue  # already registered
            members = player_config.get_value(CONF_GROUP_MEMBERS)
            group_type = player_config.get_value(CONF_GROUP_TYPE)
            with suppress(PlayerUnavailableError):
                await GroupPlayer._register_group_player(
                    provider=provider,
                    group_player_id=player_config.player_id,
                    group_type=group_type,
                    name=player_config.name or player_config.default_name,
                    members=members,
                )

    @staticmethod
    async def _register_group_player(
        provider: PlayerGroupProvider,
        group_player_id: str,
        group_type: str,
        name: str,
        members: Iterable[str],
    ) -> Player:
        """Register a group player."""
        if not (provider.mass.players.get(x) for x in members):
            raise PlayerUnavailableError("One or more members are not available!")

        # Create the GroupPlayer instance
        group_player = GroupPlayer(
            provider=provider,
            player_id=group_player_id,
            group_type=group_type,
            name=name,
            members=members,
        )

        await provider.mass.players.register_or_update(group_player)
        group_player._update_attributes()
        return group_player

    @staticmethod
    def _filter_members(
        provider: PlayerGroupProvider, group_type: str, members: list[str]
    ) -> list[str]:
        """Filter out members that are not valid players."""
        if group_type != GROUP_TYPE_UNIVERSAL:
            player_provider = provider.mass.get_provider(group_type)
            return [
                x
                for x in members
                if (player := provider.mass.players.get(x))
                and player.provider.instance_id == player_provider.instance_id
            ]
        # cleanup members - filter out impossible choices
        syncgroup_childs: list[str] = []
        for member in members:
            if not member.startswith(SYNCGROUP_PREFIX):
                continue
            if syncgroup := provider.mass.players.get(member):
                syncgroup_childs.extend(syncgroup.group_childs)
        # we filter out other UGP players and syncgroup childs
        # if their parent is already in the list
        return [
            x
            for x in members
            if provider.mass.players.get(x)
            and x not in syncgroup_childs
            and not x.startswith(UNIVERSAL_PREFIX)
        ]
