"""Group Player implementation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from time import time
from typing import TYPE_CHECKING, cast

from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import AudioFormat
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    CONF_DYNAMIC_GROUP_MEMBERS,
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_GROUP_MEMBERS,
    CONF_HTTP_PROFILE,
    DEFAULT_STREAM_HEADERS,
)
from music_assistant.helpers.audio import get_player_filter_params
from music_assistant.helpers.util import TaskManager
from music_assistant.models.player import DeviceInfo, GroupPlayer, PlayerMedia
from music_assistant.providers.universal_group.constants import UGP_FORMAT

from .constants import CONF_ENTRY_SAMPLE_RATES_UGP, CONFIG_ENTRY_UGP_NOTE
from .ugp_stream import UGPStream

if TYPE_CHECKING:
    from .provider import UniversalGroupProvider

BASE_FEATURES = {PlayerFeature.POWER, PlayerFeature.VOLUME_SET, PlayerFeature.MULTI_DEVICE_DSP}


class UniversalGroupPlayer(GroupPlayer):
    """Universal Group Player implementation."""

    def __init__(
        self,
        provider: UniversalGroupProvider,
        player_id: str,
    ) -> None:
        """Initialize GroupPlayer instance."""
        super().__init__(provider, player_id)
        self.stream: UGPStream | None = None
        self._attr_name = self.config.name or f"Universal Group {player_id}"
        self._attr_available = True
        self._attr_powered = False  # group players are always powered off by default
        self._attr_device_info = DeviceInfo(model="Universal Group", manufacturer=provider.name)
        self._attr_supported_features = {*BASE_FEATURES}
        self._attr_needs_poll = True
        self._attr_poll_interval = 30
        # register dynamic route for the ugp stream
        self._on_unload_callbacks.append(
            self.mass.streams.register_dynamic_route(
                f"/ugp/{self.player_id}.flac", self._serve_ugp_stream
            )
        )
        self._on_unload_callbacks.append(
            self.mass.streams.register_dynamic_route(
                f"/ugp/{self.player_id}.mp3", self._serve_ugp_stream
            )
        )
        # allow grouping with all providers, except the ugp provider itself
        self._attr_can_group_with = {
            x.instance_id
            for x in self.mass.players.providers
            if x.instance_id != self.provider.instance_id
        }
        self._set_attributes()

    async def on_config_updated(self) -> None:
        """Handle logic when the player is loaded or updated."""
        static_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        self._attr_static_group_members = static_members.copy()
        if not self.powered:
            self._attr_group_members = static_members.copy()
        if self.is_dynamic:
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        elif PlayerFeature.SET_MEMBERS in self._attr_supported_features:
            self._attr_supported_features.remove(PlayerFeature.SET_MEMBERS)

    @cached_property
    def is_dynamic(self) -> bool:
        """Return if the player is a dynamic group player."""
        return bool(self.config.get_value(CONF_DYNAMIC_GROUP_MEMBERS, False))

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return [
            # default entries for player groups
            *await super().get_config_entries(action=action, values=values),
            # add universal group specific entries
            CONFIG_ENTRY_UGP_NOTE,
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                label="Group members",
                default_value=[],
                description="Select all players you want to be part of this group",
                required=False,  # needed for dynamic members (which allows empty members list)
                options=[
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in self.mass.players.all(True, False)
                    if x.type != PlayerType.GROUP
                ],
            ),
            ConfigEntry(
                key=CONF_DYNAMIC_GROUP_MEMBERS,
                type=ConfigEntryType.BOOLEAN,
                label="Enable dynamic members",
                description="Allow members to (temporary) join/leave the group dynamically.",
                default_value=False,
                required=False,
            ),
            CONF_ENTRY_SAMPLE_RATES_UGP,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
        ]

    async def stop(self) -> None:
        """Handle STOP command."""
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(self, active_only=True):
                tg.create_task(member.stop())
        # abort the stream session
        if self.stream and not self.stream.done:
            await self.stream.stop()
            self.stream = None
        self._set_attributes()

    async def power(self, powered: bool) -> None:
        """Handle POWER command to group player."""
        # always stop at power off
        if not powered and self.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            await self.stop()

        # optimistically set the group state
        prev_power = self._attr_powered
        self._attr_powered = powered

        if powered:
            # reset the group members to the available static members when powering on
            self._attr_group_members = []
            for static_group_member in self._attr_static_group_members:
                if (
                    (member_player := self.mass.players.get(static_group_member))
                    and member_player.available
                    and member_player.enabled
                ):
                    self._attr_group_members.append(static_group_member)
            # handle TURN_ON of the group player by turning on all members
            for member in self.mass.players.iter_group_members(
                self, only_powered=False, active_only=False
            ):
                if (
                    member.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
                    and member.active_source != self.active_source
                ):
                    # stop playing existing content on member if we start the group player
                    await member.stop()
                if member.active_group is not None and member.active_group != self.player_id:
                    # collision: child player is part of multiple groups
                    # and another group already active !
                    # solve this by trying to leave the group first
                    if other_group := self.mass.players.get(member.active_group):
                        if (
                            other_group.supports_feature(PlayerFeature.SET_MEMBERS)
                            and member.player_id not in other_group.static_group_members
                        ):
                            await other_group.set_members(player_ids_to_remove=[member.player_id])
                        else:
                            # if the other group does not support SET_MEMBERS or it is a static
                            # member, we need to power it off to leave the group
                            await other_group.power(False)
                            await asyncio.sleep(1)
                    await asyncio.sleep(1)
                if member.synced_to:
                    # edge case: the member is part of a syncgroup - ungroup it first
                    await member.ungroup()
                if not member.powered and member.power_control != PLAYER_CONTROL_NONE:
                    await self.mass.players.cmd_power(member.player_id, True)
        elif prev_power:
            # handle TURN_OFF of the group player by turning off all members
            for member in self.mass.players.iter_group_members(
                self, only_powered=True, active_only=True
            ):
                # handle TURN_OFF of the group player by turning off all members
                if member.powered and member.power_control != PLAYER_CONTROL_NONE:
                    await self.mass.players.cmd_power(member.player_id, False)

        if not powered:
            # reset the original group members when powered off
            self._attr_group_members = self._attr_static_group_members.copy()
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # group volume is already handled in the player manager

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        await self.power(True)

        if self.stream and not self.stream.done:
            # stop any existing stream first
            await self.stream.stop()

        # select audio source
        audio_source = self.mass.streams.get_stream(media, UGP_FORMAT)

        # start the stream task
        self.stream = UGPStream(
            audio_source=audio_source, audio_format=UGP_FORMAT, base_pcm_format=UGP_FORMAT
        )
        base_url = f"{self.mass.streams.base_url}/ugp/{self.player_id}.flac"

        # set the state optimistically
        self._attr_current_media = deepcopy(media)
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
                            source_id=self.player_id,
                        )
                    )
                )

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        if not self.is_dynamic:
            raise UnsupportedFeaturedException(
                f"Group {self.display_name} does not allow dynamically adding/removing members!"
            )
        # handle additions
        for player_id in player_ids_to_add or []:
            if player_id in self._attr_group_members:
                continue
            if player_id == self.player_id:
                raise UnsupportedFeaturedException(
                    f"Cannot add {self.display_name} to itself as a member!"
                )
            child_player = self.mass.players.get(player_id, True)
            assert child_player  # for type checking
            if child_player.synced_to:
                # This is player is part of a syncgroup - ungroup it first
                await child_player.ungroup()
            self._attr_group_members.append(player_id)
            # let the newly add member join the stream if we're playing
            if self.stream and not self.stream.done and self.powered:
                base_url = f"{self.mass.streams.base_url}/ugp/{self.player_id}.flac"
                await child_player.play_media(
                    media=PlayerMedia(
                        uri=f"{base_url}?player_id={player_id}",
                        media_type=MediaType.FLOW_STREAM,
                        title=self.display_name,
                        source_id=child_player.player_id,
                    ),
                )
        # handle removals
        for player_id in player_ids_to_remove or []:
            if player_id not in self._attr_group_members:
                continue
            if player_id == self.player_id:
                raise UnsupportedFeaturedException(
                    f"Cannot remove {self.display_name} from itself as a member!"
                )
            self._attr_group_members.remove(player_id)
            child_player = self.mass.players.get(player_id, True)
            assert child_player is not None  # for type checking
            if child_player.playback_state in (
                PlaybackState.PLAYING,
                PlaybackState.PAUSED,
            ):
                # if the child player is playing the group stream, stop it
                await child_player.stop()
        self.update_state()

    async def poll(self) -> None:
        """Poll player for state updates."""
        self._set_attributes()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        await super().on_unload()
        if self.powered:
            # edge case: the group player is powered and being unloaded
            # make sure to turn it off first (which will also ungroup a syncgroup)
            await self.power(False)

    def _set_attributes(self) -> None:
        """Set attributes of the group player."""
        if self.is_dynamic and PlayerFeature.SET_MEMBERS not in self.supported_features:
            # dynamic group players should support SET_MEMBERS feature
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        elif not self.is_dynamic and PlayerFeature.SET_MEMBERS in self.supported_features:
            # static group players should not support SET_MEMBERS feature
            self._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        # grab current media and state from one of the active players
        for child_player in self.mass.players.iter_group_members(self, active_only=True):
            self._attr_playback_state = child_player.playback_state
            if child_player.elapsed_time:
                self._attr_elapsed_time = child_player.elapsed_time
                self._attr_elapsed_time_last_updated = child_player.elapsed_time_last_updated
            break
        else:
            self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def _serve_ugp_stream(self, request: web.Request) -> web.StreamResponse:
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
            http_profile = await self.mass.config.get_player_config_value(
                child_player_id, CONF_HTTP_PROFILE, return_type=str
            )
        elif output_format_str == "flac":
            output_format = AudioFormat(content_type=ContentType.FLAC)
        else:
            output_format = AudioFormat(content_type=ContentType.MP3)
            http_profile = "chunked"

        if not (ugp_player := self.mass.players.get(ugp_player_id)):
            raise web.HTTPNotFound(reason=f"Unknown UGP player: {ugp_player_id}")

        if not self.stream or self.stream.done:
            raise web.HTTPNotFound(body=f"There is no active UGP stream for {ugp_player_id}!")

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
                self.mass, child_player_id, self.stream.input_format, output_format
            )

        async for chunk in self.stream.get_stream(
            output_format,
            filter_params=filter_params,
        ):
            try:
                await resp.write(chunk)
            except (ConnectionError, ConnectionResetError):
                break

        return resp
