"""
Sync Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create 'presets' of players to sync together (of the same type).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from time import time
from typing import TYPE_CHECKING, Final, cast

import shortuuid
from aiohttp import web
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    PlayerConfig,
)
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import (
    InvalidDataError,
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import AudioFormat, UniqueList
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia

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
from music_assistant.models.player_provider import PlayerProvider

from .ugp_stream import UGPStream

if TYPE_CHECKING:
    from collections.abc import Iterable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


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


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PlayerGroupProvider(mass, manifest, config)


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
    # nothing to configure (for now)
    return ()


class PlayerGroupProvider(PlayerProvider):
    """Base/builtin provider for creating (permanent) player groups."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self.ugp_streams: dict[str, UGPStream] = {}
        self._on_unload: list[Callable[[], None]] = [
            self.mass.register_api_command("player_group/create", self.create_group),
        ]

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.REMOVE_PLAYER}

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # temp: migrate old config entries
        # remove this after MA 2.4 release
        for player_config in await self.mass.config.get_player_configs(include_values=True):
            # migrate provider set to domain to instance_id
            if player_config.provider == self.manifest.domain:
                self.mass.config.set_raw_player_config_value(
                    player_config.player_id, "provider", self.instance_id
                )
                player_config.provider = self.instance_id
            # migrate old syncgroup/UGP players to this provider
            if player_config.values.get(CONF_GROUP_TYPE) is not None:
                # already migrated
                continue
            if player_config.player_id.startswith(SYNCGROUP_PREFIX):
                self.mass.config.set_raw_player_config_value(
                    player_config.player_id, CONF_GROUP_TYPE, player_config.provider
                )
                player_config.provider = self.instance_id
                self.mass.config.set_raw_player_config_value(
                    player_config.player_id, "provider", self.instance_id
                )
            elif player_config.player_id.startswith(UNIVERSAL_PREFIX):
                self.mass.config.set_raw_player_config_value(
                    player_config.player_id, CONF_GROUP_TYPE, "universal"
                )
                player_config.provider = self.instance_id
                self.mass.config.set_raw_player_config_value(
                    player_config.player_id, "provider", self.instance_id
                )

        await self._register_all_players()
        # listen for player added events so we can catch late joiners
        # (because a group depends on its childs to be available)
        self._on_unload.append(
            self.mass.subscribe(self._on_mass_player_added_event, EventType.PLAYER_ADDED)
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        # power off all group players at unload
        for group_player in self.players:
            if group_player.powered:
                await self.cmd_power(group_player.player_id, False)
        for unload_cb in self._on_unload:
            unload_cb()

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        # default entries for player groups
        base_entries = (
            *await super().get_player_config_entries(player_id),
            CONF_ENTRY_GROUP_TYPE,
            CONF_ENTRY_GROUP_MEMBERS,
            CONFIG_ENTRY_DYNAMIC_MEMBERS,
        )
        # group type is static and can not be changed. we just grab the existing, stored value
        group_type: str = self.mass.config.get_raw_player_config_value(
            player_id, CONF_GROUP_TYPE, GROUP_TYPE_UNIVERSAL
        )
        # handle config entries for universal group players
        if group_type == GROUP_TYPE_UNIVERSAL:
            group_members = CONF_ENTRY_GROUP_MEMBERS
            group_members.options = tuple(
                ConfigValueOption(x.display_name, x.player_id)
                for x in self.mass.players.all(True, False)
                if not x.player_id.startswith(UNIVERSAL_PREFIX)
            )
            return (
                *base_entries,
                group_members,
                CONFIG_ENTRY_UGP_NOTE,
                CONF_ENTRY_SAMPLE_RATES_UGP,
                CONF_ENTRY_FLOW_MODE_ENFORCED,
            )
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
        assert player_provider.instance_id != self.instance_id
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
        child_config_entries = await player_provider.get_player_config_entries(
            child_player.player_id
        )
        return (
            *base_entries,
            group_members,
            *(entry for entry in child_config_entries if entry.key in allowed_conf_entries),
        )

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        members = config.get_value(CONF_GROUP_MEMBERS)
        if f"values/{CONF_GROUP_MEMBERS}" in changed_keys:
            # ensure we filter invalid members
            members = self._filter_members(config.get_value(CONF_GROUP_TYPE), members)
            if group_player := self.mass.players.get(config.player_id):
                group_player.group_childs.set(members)
                if group_player.powered:
                    # power on group player (which will also resync) if needed
                    await self.cmd_power(group_player.player_id, True)
        if f"values/{CONFIG_ENTRY_DYNAMIC_MEMBERS.key}" in changed_keys:
            # dynamic members feature changed
            if group_player := self.mass.players.get(config.player_id):
                if PlayerFeature.SET_MEMBERS in group_player.supported_features:
                    group_player.supported_features.remove(PlayerFeature.SET_MEMBERS)
                else:
                    group_player.supported_features.add(PlayerFeature.SET_MEMBERS)
        if not members and not config.get_value(CONFIG_ENTRY_DYNAMIC_MEMBERS.key):
            raise InvalidDataError("Group player must have at least one member")
        await super().on_player_config_change(config, changed_keys)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        group_player = self.mass.players.get(player_id)
        # syncgroup: forward command to sync leader
        if player_id.startswith(SYNCGROUP_PREFIX):
            if sync_leader := self._get_sync_leader(group_player):
                if player_provider := self.mass.get_provider(sync_leader.provider):
                    await player_provider.cmd_stop(sync_leader.player_id)
            return
        # ugp: forward command to all members
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(group_player, active_only=True):
                if player_provider := self.mass.get_provider(member.provider):
                    tg.create_task(player_provider.cmd_stop(member.player_id))
        # abort the stream session
        if (stream := self.ugp_streams.pop(player_id, None)) and not stream.done:
            await stream.stop()

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        group_player = self.mass.players.get(player_id)
        if not player_id.startswith(SYNCGROUP_PREFIX):
            # this shouldn't happen, but just in case
            raise UnsupportedFeaturedException
        # forward command to sync leader
        if sync_leader := self._get_sync_leader(group_player):
            if player_provider := self.mass.get_provider(sync_leader.provider):
                await player_provider.cmd_play(sync_leader.player_id)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        group_player = self.mass.players.get(player_id)
        if not player_id.startswith(SYNCGROUP_PREFIX):
            # this shouldn't happen, but just in case
            raise UnsupportedFeaturedException
        # forward command to sync leader
        if sync_leader := self._get_sync_leader(group_player):
            if player_provider := self.mass.get_provider(sync_leader.provider):
                await player_provider.cmd_pause(sync_leader.player_id)

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Handle POWER command to group player."""
        group_player = self.mass.players.get(player_id, raise_unavailable=True)
        if TYPE_CHECKING:
            group_player = cast("Player", group_player)

        # always stop at power off
        if not powered and group_player.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            await self.cmd_stop(group_player.player_id)

        if powered and player_id.startswith(SYNCGROUP_PREFIX):
            await self._form_syncgroup(group_player)

        if powered:
            # handle TURN_ON of the group player by turning on all members
            for member in self.mass.players.iter_group_members(
                group_player, only_powered=False, active_only=False
            ):
                player_provider = self.mass.get_provider(member.provider)
                assert player_provider  # for typing
                if (
                    member.state in (PlayerState.PLAYING, PlayerState.PAUSED)
                    and member.active_source != group_player.active_source
                ):
                    # stop playing existing content on member if we start the group player
                    await player_provider.cmd_stop(member.player_id)
                if member.active_group not in (
                    None,
                    group_player.player_id,
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
                member.active_group = group_player.player_id
                member.active_source = group_player.active_source
        else:
            # handle TURN_OFF of the group player by turning off all members
            # optimistically set the group state to prevent race conditions
            group_player.powered = False
            for member in self.mass.players.iter_group_members(
                group_player, only_powered=True, active_only=True
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
        group_player.powered = powered
        self.mass.players.update(group_player.player_id)
        if not powered:
            # reset the original group members when powered off
            group_player.group_childs.set(
                self.mass.config.get_raw_player_config_value(player_id, CONF_GROUP_MEMBERS, [])
            )

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # group volume is already handled in the player manager

    async def play_media(
        self,
        player_id: str,
        media: PlayerMedia,
    ) -> None:
        """Handle PLAY MEDIA on given player."""
        group_player = self.mass.players.get(player_id)
        # power on (which will also resync) if needed
        await self.cmd_power(player_id, True)

        # handle play_media for sync group
        if player_id.startswith(SYNCGROUP_PREFIX):
            # simply forward the command to the sync leader
            sync_leader = self._get_sync_leader(group_player)
            player_provider = self.mass.get_provider(sync_leader.provider)
            assert player_provider  # for typing
            await player_provider.play_media(
                sync_leader.player_id,
                media=media,
            )
            return

        # handle play_media for UGP group
        if (existing := self.ugp_streams.pop(player_id, None)) and not existing.done:
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
        self.ugp_streams[player_id] = UGPStream(
            audio_source=audio_source, audio_format=UGP_FORMAT, base_pcm_format=UGP_FORMAT
        )
        base_url = f"{self.mass.streams.base_url}/ugp/{player_id}.flac"

        # set the state optimistically
        group_player.current_media = media
        group_player.elapsed_time = 0
        group_player.elapsed_time_last_updated = time() - 1
        group_player.state = PlayerState.PLAYING
        self.mass.players.update(player_id)

        # forward to downstream play_media commands
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(
                group_player, only_powered=True, active_only=True
            ):
                player_provider = self.mass.get_provider(member.provider)
                assert player_provider  # for typing
                tg.create_task(
                    player_provider.play_media(
                        member.player_id,
                        media=PlayerMedia(
                            uri=f"{base_url}?player_id={member.player_id}",
                            media_type=MediaType.FLOW_STREAM,
                            title=group_player.display_name,
                            queue_id=group_player.player_id,
                        ),
                    )
                )

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of a next media item on the player."""
        group_player = self.mass.players.get(player_id, True)
        if not player_id.startswith(SYNCGROUP_PREFIX):
            # this shouldn't happen, but just in case
            raise UnsupportedFeaturedException("Command is not supported for UGP players")
        if sync_leader := self._get_sync_leader(group_player):
            await self.mass.players.enqueue_next_media(
                sync_leader.player_id,
                media=media,
            )

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates.

        This is called by the Player Manager;
        if 'needs_poll' is set to True in the player object.
        """
        if group_player := self.mass.players.get(player_id):
            self._update_attributes(group_player)
            if group_player.powered:
                await self._ungroup_subgroups_if_found(group_player)

    async def create_group(
        self, group_type: str, name: str, members: list[str], dynamic: bool = False
    ) -> Player:
        """Create new Group Player."""
        # perform basic checks
        if group_type == GROUP_TYPE_UNIVERSAL:
            prefix = UNIVERSAL_PREFIX
        else:
            prefix = SYNCGROUP_PREFIX
            if (player_prov := self.mass.get_provider(group_type)) is None:
                msg = f"Provider {group_type} is not available!"
                raise ProviderUnavailableError(msg)
            if ProviderFeature.SYNC_PLAYERS not in player_prov.supported_features:
                msg = f"Provider {player_prov.name} does not support creating groups"
                raise UnsupportedFeaturedException(msg)
            group_type = player_prov.instance_id  # just in case only domain was sent

        new_group_id = f"{prefix}{shortuuid.random(8).lower()}"
        # cleanup list, just in case the frontend sends some garbage
        members = self._filter_members(group_type, members)
        # create default config with the user chosen name
        self.mass.config.create_default_player_config(
            new_group_id,
            self.instance_id,
            name=name,
            enabled=True,
            values={
                CONF_GROUP_MEMBERS: members,
                CONF_GROUP_TYPE: group_type,
                CONFIG_ENTRY_DYNAMIC_MEMBERS.key: dynamic,
            },
        )
        return await self._register_group_player(
            group_player_id=new_group_id, group_type=group_type, name=name, members=members
        )

    async def remove_player(self, player_id: str) -> None:
        """Remove a group player."""
        if not (group_player := self.mass.players.get(player_id)):
            return
        if group_player.powered:
            # edge case: the group player is powered and being removed
            # make sure to turn it off first (which will also ungroup a syncgroup)
            await self.cmd_power(player_id, False)

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player.

        Join/add the given player(id) to the given (master) player/sync group.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the sync leader.
        """
        group_player = self.mass.players.get(target_player, raise_unavailable=True)
        if TYPE_CHECKING:
            group_player = cast("Player", group_player)
        dynamic_members_enabled = self.mass.config.get_raw_player_config_value(
            group_player.player_id,
            CONFIG_ENTRY_DYNAMIC_MEMBERS.key,
            CONFIG_ENTRY_DYNAMIC_MEMBERS.default_value,
        )
        group_type = self.mass.config.get_raw_player_config_value(
            group_player.player_id, CONF_ENTRY_GROUP_TYPE.key, CONF_ENTRY_GROUP_TYPE.default_value
        )
        if not dynamic_members_enabled:
            raise UnsupportedFeaturedException(
                f"Adjusting group members is not allowed for group {group_player.display_name}"
            )
        child_player = self.mass.players.get(player_id, raise_unavailable=True)
        if TYPE_CHECKING:
            group_player = cast("Player", group_player)
        if child_player.active_group and child_player.active_group != group_player.player_id:
            raise InvalidDataError(
                f"Player {child_player.display_name} already has another group active"
            )
        group_player.group_childs.append(player_id)

        # Ensure that all player are just in this group and not in any other group
        await self._ungroup_subgroups_if_found(group_player)

        # handle resync/resume if group player was already playing
        if group_player.state == PlayerState.PLAYING and group_type == GROUP_TYPE_UNIVERSAL:
            child_player_provider = self.mass.players.get_player_provider(player_id)
            base_url = f"{self.mass.streams.base_url}/ugp/{group_player.player_id}.flac"
            await child_player_provider.play_media(
                player_id,
                media=PlayerMedia(
                    uri=f"{base_url}?player_id={player_id}",
                    media_type=MediaType.FLOW_STREAM,
                    title=group_player.display_name,
                    queue_id=group_player.player_id,
                ),
            )
        elif group_player.powered and group_type != GROUP_TYPE_UNIVERSAL:
            # power on group player (which will also resync) if needed
            await self.cmd_power(target_player, True)

    async def cmd_ungroup_member(self, player_id: str, target_player: str) -> None:
        """Handle UNGROUP command for given player.

        Remove the given player(id) from the given (master) player/sync group.

            - player_id: player_id of the (child) player to ungroup from the group.
            - target_player: player_id of the group player.
        """
        group_player = self.mass.players.get(target_player, raise_unavailable=True)
        child_player = self.mass.players.get(player_id, raise_unavailable=True)
        if TYPE_CHECKING:
            group_player = cast("Player", group_player)
            child_player = cast("Player", child_player)
        dynamic_members_enabled = self.mass.config.get_raw_player_config_value(
            group_player.player_id,
            CONFIG_ENTRY_DYNAMIC_MEMBERS.key,
            CONFIG_ENTRY_DYNAMIC_MEMBERS.default_value,
        )
        if not dynamic_members_enabled:
            raise UnsupportedFeaturedException(
                f"Adjusting group members is not allowed for group {group_player.display_name}"
            )
        group_type = self.mass.config.get_raw_player_config_value(
            group_player.player_id, CONF_ENTRY_GROUP_TYPE.key, CONF_ENTRY_GROUP_TYPE.default_value
        )
        was_playing = child_player.state == PlayerState.PLAYING
        is_sync_leader = len(child_player.group_childs) > 0
        group_player.group_childs.remove(player_id)
        child_player.active_group = None
        child_player.active_source = None
        player_provider = self.mass.players.get_player_provider(child_player.player_id)
        if group_type == GROUP_TYPE_UNIVERSAL:
            if was_playing:
                # stop playing the child player that was unjoined from the UGP
                await player_provider.cmd_stop(child_player.player_id)
            self._update_attributes(group_player)
            return
        # handle sync group
        if child_player.group_childs:
            # this is the sync leader, unsync all its childs!
            # NOTE that some players/providers might support this in a less intrusive way
            # but for now we just ungroup all childs to keep things universal
            self.logger.info("Detected ungroup of sync leader, ungrouping all childs")
            async with TaskManager(self.mass) as tg:
                for sync_child_id in child_player.group_childs:
                    if sync_child_id == child_player.player_id:
                        continue
                    tg.create_task(player_provider.cmd_ungroup(sync_child_id))
            await player_provider.cmd_stop(child_player.player_id)
        else:
            # this is a regular member, just ungroup itself
            await player_provider.cmd_ungroup(child_player.player_id)

        if is_sync_leader and was_playing and group_player.powered:
            # ungrouping the sync leader stops the group so we need to resume
            self.logger.info("Resuming group after ungrouping of sync leader")
            task_id = f"resync_group_{group_player.player_id}"
            self.mass.call_later(
                2, self.mass.players.cmd_play(group_player.player_id), task_id=task_id
            )

    async def _register_all_players(self) -> None:
        """Register all (virtual/fake) group players in the Player controller."""
        player_configs = await self.mass.config.get_player_configs(
            self.instance_id, include_values=True
        )
        for player_config in player_configs:
            if self.mass.players.get(player_config.player_id):
                continue  # already registered
            members = player_config.get_value(CONF_GROUP_MEMBERS)
            group_type = player_config.get_value(CONF_GROUP_TYPE)
            with suppress(PlayerUnavailableError):
                await self._register_group_player(
                    player_config.player_id,
                    group_type,
                    player_config.name or player_config.default_name,
                    members,
                )

    async def _register_group_player(
        self, group_player_id: str, group_type: str, name: str, members: Iterable[str]
    ) -> Player:
        """Register a syncgroup player."""
        player_features = {
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
        }

        if not (self.mass.players.get(x) for x in members):
            raise PlayerUnavailableError("One or more members are not available!")

        if group_type == GROUP_TYPE_UNIVERSAL:
            model_name = "Universal Group"
            manufacturer = self.name
            # register dynamic route for the ugp stream
            self._on_unload.append(
                self.mass.streams.register_dynamic_route(
                    f"/ugp/{group_player_id}.flac", self._serve_ugp_stream
                )
            )
            self._on_unload.append(
                self.mass.streams.register_dynamic_route(
                    f"/ugp/{group_player_id}.mp3", self._serve_ugp_stream
                )
            )
            can_group_with = {
                # allow grouping with all providers, except the playergroup provider itself
                x.instance_id
                for x in self.mass.players.providers
                if x.instance_id != self.instance_id
            }
            player_features.add(PlayerFeature.MULTI_DEVICE_DSP)
        elif player_provider := self.mass.get_provider(group_type):
            # grab additional details from one of the provider's players
            if TYPE_CHECKING:
                player_provider = cast("PlayerProvider", player_provider)
            model_name = "Sync Group"
            manufacturer = self.mass.get_provider(group_type).name
            can_group_with = {player_provider.instance_id}
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
            raise PlayerUnavailableError(f"Provider for syncgroup {group_type} is not available!")

        if self.mass.config.get_raw_player_config_value(
            group_player_id,
            CONFIG_ENTRY_DYNAMIC_MEMBERS.key,
            CONFIG_ENTRY_DYNAMIC_MEMBERS.default_value,
        ):
            player_features.add(PlayerFeature.SET_MEMBERS)

        player = Player(
            player_id=group_player_id,
            provider=self.instance_id,
            type=PlayerType.GROUP,
            name=name,
            available=True,
            # group players are always powered off by default at init/startup
            powered=False,
            device_info=DeviceInfo(model=model_name, manufacturer=manufacturer),
            supported_features=player_features,
            active_source=group_player_id,
            needs_poll=True,
            poll_interval=30,
            can_group_with=can_group_with,
            group_childs=UniqueList(members),
        )

        await self.mass.players.register_or_update(player)
        self._update_attributes(player)
        return player

    def _get_sync_leader(self, group_player: Player) -> Player:
        """Get the active sync leader player for the syncgroup."""
        for child_player in self.mass.players.iter_group_members(
            group_player, only_powered=False, only_playing=False, active_only=False
        ):
            # the syncleader is always the first player in the group
            return child_player
        raise RuntimeError("No players available in syncgroup")

    async def _form_syncgroup(self, group_player: Player) -> None:
        """Form syncgroup by sync all (possible) members."""
        sync_leader = await self._select_sync_leader(group_player)
        # ensure the sync leader is first in the list
        group_player.group_childs.set(
            [
                sync_leader.player_id,
                *[x for x in group_player.group_childs if x != sync_leader.player_id],
            ]
        )
        members_to_sync: list[str] = []
        for member in self.mass.players.iter_group_members(group_player, active_only=False):
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

    async def _select_sync_leader(self, group_player: Player) -> Player:
        """Select the active sync leader player for a syncgroup."""
        # prefer the first player that already has sync childs
        for prefer_sync_leader in (True, False):
            for child_player in self.mass.players.iter_group_members(group_player):
                if prefer_sync_leader and child_player.synced_to:
                    continue
                if child_player.active_group not in (
                    None,
                    group_player.player_id,
                    child_player.player_id,
                ):
                    # this should not happen (because its already handled in the power on logic),
                    # but guard it just in case bad things happen
                    continue
                return child_player
        raise RuntimeError("No players available to form syncgroup")

    async def _on_mass_player_added_event(self, event: MassEvent) -> None:
        """Handle player added event from player controller."""
        await self._register_all_players()

    def _update_attributes(self, player: Player) -> None:
        """Update attributes of a player."""
        group_type = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_ENTRY_GROUP_TYPE.key, CONF_ENTRY_GROUP_TYPE.default_value
        )
        # grab current media and state from one of the active players
        for child_player in self.mass.players.iter_group_members(
            player, active_only=True, only_playing=True
        ):
            if child_player.synced_to:
                # ignore child players
                continue
            if child_player.active_source not in (None, player.active_source):
                # this should not happen but guard just in case
                continue
            player.state = child_player.state
            if child_player.current_media:
                player.current_media = child_player.current_media
            player.elapsed_time = child_player.elapsed_time
            player.elapsed_time_last_updated = child_player.elapsed_time_last_updated
            break
        else:
            player.state = PlayerState.IDLE
        if group_type == GROUP_TYPE_UNIVERSAL:
            can_group_with = {
                # allow grouping with all providers, except the playergroup provider itself
                x.instance_id
                for x in self.mass.players.providers
                if x.instance_id != self.instance_id
            }
        elif sync_player_provider := self.mass.get_provider(group_type):
            can_group_with = {sync_player_provider.instance_id}
        else:
            can_group_with = {}
        player.can_group_with = can_group_with
        self.mass.players.update(player.player_id)

    async def _ungroup_subgroups_if_found(self, player: Player) -> None:
        """Verify that no player is part of a separate group."""
        group_type = self.mass.config.get_raw_player_config_value(
            player.player_id, CONF_ENTRY_GROUP_TYPE.key, CONF_ENTRY_GROUP_TYPE.default_value
        )
        if group_type != GROUP_TYPE_UNIVERSAL:
            return

        changed = False
        # Verify that no player is part of a separate group
        for child_player_id in player.group_childs:
            child_player = self.mass.players.get(child_player_id)
            if child_player is None:
                continue
            if PlayerFeature.SET_MEMBERS not in child_player.supported_features:
                continue
            if child_player.group_childs:
                # This is a leader in another group
                player_provider = self.mass.players.get_player_provider(child_player_id)
                for sync_child_id in child_player.group_childs:
                    if sync_child_id == child_player_id:
                        continue
                    await player_provider.cmd_ungroup(sync_child_id)
                    changed = True
            if child_player.synced_to:
                # This is a member of another group
                await self.cmd_ungroup_member(child_player.player_id, child_player.synced_to)
                changed = True
        if changed and player.state == PlayerState.PLAYING:
            # Restart playback to ensure all members play the same content
            await self.mass.player_queues.resume(player.player_id, False)

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

        if not (stream := self.ugp_streams.get(ugp_player_id, None)) or stream.done:
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

    def _filter_members(self, group_type: str, members: list[str]) -> list[str]:
        """Filter out members that are not valid players."""
        if group_type != GROUP_TYPE_UNIVERSAL:
            player_provider = self.mass.get_provider(group_type)
            return [
                x
                for x in members
                if (player := self.mass.players.get(x))
                and player.provider == player_provider.instance_id
            ]
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
            and not x.startswith(UNIVERSAL_PREFIX)
        ]
