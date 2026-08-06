"""Group Player implementation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from time import time
from typing import TYPE_CHECKING, cast

from aiohttp import HttpVersion11, web
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.constants import PLAYER_CONTROL_FAKE, PLAYER_CONTROL_NONE
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
    CONF_ENTRY_HTTP_PROFILE_DEFAULT_1,
    CONF_GROUP_MEMBERS,
    CONF_HTTP_PROFILE,
    CONF_POWER_CONTROL,
    DEFAULT_STREAM_HEADERS,
    DLNA_CONTENT_FEATURES_REALTIME,
)
from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.helpers.audio import get_mime_type
from music_assistant.helpers.util import TaskManager
from music_assistant.models.player import DeviceInfo, Player, PlayerMedia

from .constants import (
    CONF_ENTRY_UGP_OUTPUT_FORMAT,
    CONF_UGP_OUTPUT_FORMAT,
    CONFIG_ENTRY_UGP_NOTE,
    EXTRA_FEATURES_FROM_MEMBERS,
    IDLE_GRACE_SECONDS,
    UGP_OUTPUT_MP3,
    resolve_ugp_output_format,
)
from .ugp_stream import UGPStream

if TYPE_CHECKING:
    from .provider import UniversalGroupProvider

# The features the group carries on its own. Everything else is resolved per read in
# the supported_features property: POWER when the user assigns 'Fake power control',
# SET_MEMBERS for dynamic groups, and EXTRA_FEATURES_FROM_MEMBERS from the members.
# PlayerFeature.POWER is intentionally not a base feature: the lifecycle (form on
# play, dissolve on stop, debounced idle deform) governs whether the group captures
# its members.
BASE_FEATURES = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.MULTI_DEVICE_DSP,
}


class UniversalGroupPlayer(Player):
    """Universal Group Player implementation."""

    _attr_type: PlayerType = PlayerType.GROUP

    def __init__(
        self,
        provider: UniversalGroupProvider,
        player_id: str,
    ) -> None:
        """Initialize UniversalGroupPlayer instance."""
        super().__init__(provider, player_id)
        self.stream: UGPStream | None = None
        self._attr_name = self.config.name or f"Universal Group {player_id}"
        self._attr_available = True
        # See SyncGroupPlayer: groups have no opinion on power by default; the
        # session lifecycle is what governs activity. Fake power control is the
        # opt-in mechanism for explicit on/off semantics.
        self._attr_powered = None
        self._attr_device_info = DeviceInfo(model="Universal Group", manufacturer=provider.name)
        self._attr_needs_poll = True
        self._attr_poll_interval = 30
        # task that releases members after the idle grace window expires
        self._idle_grace_task: asyncio.Task[None] | None = None
        # register dynamic routes for the ugp stream (FLAC + MP3 cover the configured
        # output formats; the actual codec served is decided by the UGP's own config,
        # not by the request URL)
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
        self._set_attributes()

    @property
    def supported_features(self) -> set[PlayerFeature]:
        """Return the supported features of the player."""
        features = {*BASE_FEATURES}
        # The raw config value is read here to avoid recursion via the power_control
        # property (which itself may inspect supported features).
        raw_power_conf = self.mass.config.get_raw_player_config_value(
            self.player_id, CONF_POWER_CONTROL
        )
        if raw_power_conf == PLAYER_CONTROL_FAKE:
            features.add(PlayerFeature.POWER)
        if self.is_dynamic:
            features.add(PlayerFeature.SET_MEMBERS)
        # derive the fanned-out features from all (configured) members, so volume and
        # mute are advertised whether or not the group currently has a live session.
        for member_id in self._attr_group_members:
            member_player = self.mass.players.get_player(member_id)
            if member_player and member_player.state.available:
                for feature in EXTRA_FEATURES_FROM_MEMBERS:
                    if feature in member_player.state.supported_features:
                        features.add(feature)
        return features

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    @property
    def synced_to(self) -> str | None:
        """Return the id of the player this player is synced to (sync leader)."""
        # groups can't be synced
        return None

    @property
    def is_active_session(self) -> bool:
        """
        Return whether this group currently has captured members.

        The session is considered active while the multicast stream is live or
        while the idle grace timer is still pending. ``__final_active_group``
        reads this to decide whether the configured members should be marked
        as ``active_group`` for this group.
        """
        if self.stream is not None and not self.stream.done:
            return True
        return self._idle_grace_task is not None

    @property
    def can_group_with(self) -> set[str]:
        """Return the id's of players this player can group with."""
        if not self.is_dynamic:
            # in case of static members,
            # we can only group with the players defined in the config, so we return those directly
            return set(self._attr_static_group_members)
        # allow grouping with all providers, except the ugp provider itself
        return {
            x.instance_id
            for x in self.mass.players.providers
            if x.instance_id != self.provider.instance_id
        }

    @cached_property
    def supported_sample_rates(self) -> list[tuple[int, int]] | None:
        """Return the (sample_rate, bit_depth) pair the UGP serves to its members."""
        # UGP delivers the same encoded stream to every member, so its only natively
        # supported rate is whatever the configured output format produces. Returning a
        # single-rate list keeps the upstream MA flow stream pinned and prevents
        # smart/bit-perfect modes from triggering needless restarts.
        output_format, _ = resolve_ugp_output_format(
            cast("str", self.config.get_value(CONF_UGP_OUTPUT_FORMAT, UGP_OUTPUT_MP3))
        )
        return [(output_format.sample_rate, output_format.bit_depth)]

    async def on_config_updated(self) -> None:
        """Handle logic when the PlayerConfig is first loaded or updated."""
        static_members = cast("list[str]", self.config.get_value(CONF_GROUP_MEMBERS, []))
        self._attr_static_group_members = static_members.copy()
        if not self.is_active_session:
            # only realign members to the configured static set when the group
            # is dormant — otherwise we would lose any dynamic adds mid-session.
            self._attr_group_members = static_members.copy()

    @cached_property
    def is_dynamic(self) -> bool:
        """Return if the player is a dynamic group player."""
        return bool(self.config.get_value(CONF_DYNAMIC_GROUP_MEMBERS, False))

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return [
            # add universal group specific entries
            CONFIG_ENTRY_UGP_NOTE,
            ConfigEntry(
                key=CONF_GROUP_MEMBERS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                default_value=[],
                required=False,  # needed for dynamic members (which allows empty members list)
                options=[
                    ConfigValueOption(x.player_id, title=x.display_name)
                    for x in self.mass.players.all_players(True, False)
                    if x.type != PlayerType.GROUP
                ],
            ),
            ConfigEntry(
                key=CONF_DYNAMIC_GROUP_MEMBERS,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
            ),
            CONF_ENTRY_UGP_OUTPUT_FORMAT,
            CONF_ENTRY_HTTP_PROFILE_DEFAULT_1,
        ]

    async def stop(self) -> None:
        """
        Handle STOP command.

        An explicit stop releases the captured members immediately so they
        return to individual control. The idle grace timer is only used for
        natural end-of-queue transitions (see :meth:`_set_attributes`). Users
        who want the group to stay 'active' across stops can assign Fake
        power control and use that to pin the group.
        """
        # an explicit stop overrides any pending idle-grace release
        self._cancel_idle_grace_timer()
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(self, active_only=True):
                # Use internal handler to get protocol selection and avoid redirect
                tg.create_task(self.mass.players._handle_cmd_stop(member.player_id))
        # abort the stream session — this drops is_active_session to False so
        # the (former) members will see active_group=None on their next state
        # update and accept direct playback commands again.
        if self.stream and not self.stream.done:
            await self.stream.stop()
            self.stream = None
        # snap group_members back to the configured static set so we don't
        # keep stale dynamic adds around once the session has ended.
        if self._attr_powered is not True:
            self._attr_group_members = self._attr_static_group_members.copy()
        self._set_attributes()

    async def power(self, powered: bool) -> None:
        """
        Handle POWER command to group player.

        Only called when the user has assigned a power control (native or fake)
        to the group. Powering ON prepares the members so the group is
        considered 'active' immediately (matching the legacy behaviour for
        users who opt in). Powering OFF stops any playback and releases the
        captured members.

        :param powered: True to power on (capture members), False to power off (release).
        """
        # any pending idle-grace release is moot — we're on an explicit transition
        self._cancel_idle_grace_timer()

        # always stop at power off
        if not powered and self._attr_playback_state in (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        ):
            await self.stop()

        prev_power = self._attr_powered
        self._attr_powered = powered

        if powered:
            await self._capture_members()
        elif prev_power:
            # handle TURN_OFF of the group player by turning off all members
            for member in self.mass.players.iter_group_members(
                self, only_powered=True, active_only=True
            ):
                if member.powered and member.power_control != PLAYER_CONTROL_NONE:
                    await self.mass.players.cmd_power(member.player_id, False)

        if not powered:
            # reset the original group members when powered off
            self._attr_group_members = self._attr_static_group_members.copy()
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        # form on play: cancel any pending idle-grace release, then capture the
        # configured members and free them of any conflicting prior allegiance.
        self._cancel_idle_grace_timer()
        await self._capture_members()

        if self.stream and not self.stream.done:
            # stop any existing stream first
            await self.stream.stop()

        # resolve the static output format the UGP serves to all members
        output_format, fmt_str = resolve_ugp_output_format(
            cast("str", self.config.get_value(CONF_UGP_OUTPUT_FORMAT, UGP_OUTPUT_MP3))
        )
        # internal PCM pivot for the multiplexer: F32 at the configured output rate
        # so the per-member encoder doesn't have to resample
        pivot_format = AudioFormat(
            content_type=ContentType.PCM_F32LE,
            sample_rate=output_format.sample_rate,
            bit_depth=32,
            channels=2,
        )
        audio_source = self.mass.streams.get_stream(media, pivot_format, self.player_id)
        self.stream = UGPStream(
            audio_source=audio_source,
            audio_format=pivot_format,
            base_pcm_format=pivot_format,
            queue_id=media.source_id,
            session_id=get_media_session_id(media),
        )
        base_url = f"{self.mass.streams.base_url}/ugp/{self.player_id}.{fmt_str}"

        # set the state optimistically
        self._attr_current_media = deepcopy(media)
        self._attr_elapsed_time = 0
        self._attr_elapsed_time_last_updated = time() - 1
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

        # forward to downstream play_media commands
        async with TaskManager(self.mass) as tg:
            for member in self.mass.players.iter_group_members(self, only_powered=True):
                # Use internal handler to get protocol selection and avoid redirect
                tg.create_task(
                    self.mass.players._handle_play_media(
                        member.player_id,
                        PlayerMedia(
                            uri=f"{base_url}?player_id={member.player_id}",
                            media_type=MediaType.FLOW_STREAM,
                            title=self.display_name,
                            source_id=self.player_id,
                            custom_data={
                                "ugp_player_id": self.player_id,
                                "session_id": self.stream.session_id,
                            },
                        ),
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
                f"Group {self.display_name} does not allow dynamically adding/removing members!",
                translation_key="group_not_dynamic",
                translation_owner=self.translation_owner,
                translation_args=[self.display_name],
            )
        # handle additions
        for player_id in player_ids_to_add or []:
            if player_id in self._attr_group_members:
                continue
            if player_id == self.player_id:
                raise UnsupportedFeaturedException(
                    f"Cannot add {self.display_name} to itself as a member!",
                    translation_key="cannot_add_group_to_itself",
                    translation_owner=self.translation_owner,
                    translation_args=[self.display_name],
                )
            child_player = self.mass.players.get_player(player_id, True)
            assert child_player  # for type checking
            if child_player.synced_to:
                # This is player is part of a syncgroup - ungroup it first
                await child_player.ungroup()
            self._attr_group_members.append(player_id)
            # let the newly added member join the stream if it's still live —
            # the `self.powered` gate that used to guard this is gone with the
            # session-lifecycle refactor (groups now have `_attr_powered=None`
            # unless the user assigned Fake control).
            if self.stream and not self.stream.done:
                _, fmt_str = resolve_ugp_output_format(
                    cast("str", self.config.get_value(CONF_UGP_OUTPUT_FORMAT, UGP_OUTPUT_MP3))
                )
                base_url = f"{self.mass.streams.base_url}/ugp/{self.player_id}.{fmt_str}"
                # Use internal handler to get protocol selection and avoid redirect
                await self.mass.players._handle_play_media(
                    player_id,
                    PlayerMedia(
                        uri=f"{base_url}?player_id={player_id}",
                        media_type=MediaType.FLOW_STREAM,
                        title=self.display_name,
                        source_id=self.player_id,
                        custom_data={
                            "ugp_player_id": self.player_id,
                            "session_id": self.stream.session_id,
                        },
                    ),
                )
        # handle removals
        for player_id in player_ids_to_remove or []:
            if player_id not in self._attr_group_members:
                continue
            if player_id == self.player_id:
                raise UnsupportedFeaturedException(
                    f"Cannot remove {self.display_name} from itself as a member!",
                    translation_key=(
                        "provider.universal_group.errors.cannot_remove_group_from_itself"
                    ),
                    translation_args=[self.display_name],
                )
            self._attr_group_members.remove(player_id)
            child_player = self.mass.players.get_player(player_id, True)
            assert child_player is not None  # for type checking
            if child_player.playback_state in (
                PlaybackState.PLAYING,
                PlaybackState.PAUSED,
            ):
                # if the child player is playing the group stream, stop it
                # Use internal handler to get protocol selection and avoid redirect
                await self.mass.players._handle_cmd_stop(player_id)
        self.update_state()

    async def poll(self) -> None:
        """Poll player for state updates."""
        self._set_attributes()

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        self._cancel_idle_grace_timer()
        await super().on_unload()
        if self.is_active_session or self._attr_powered is True:
            # tear down any in-flight session before unloading
            await self.stop()
            self._attr_powered = False

    async def _capture_members(self) -> None:
        """
        Resolve collisions and prepare the configured members for grouping.

        Rebuilds the effective member list from the configured static set,
        powers on each member that has a power control, releases members
        that are currently captured by another group / sync session, and
        leaves the group ready for playback. Idempotent: safe to call on an
        already-prepared group.
        """
        # rebuild the effective member list from the configured static set
        self._attr_group_members = []
        for static_group_member in self._attr_static_group_members:
            if (
                (member_player := self.mass.players.get_player(static_group_member))
                and member_player.available
                and member_player.enabled
            ):
                self._attr_group_members.append(static_group_member)
        # ensure each member is free of any prior group/sync allegiance and ready to play
        for member in self.mass.players.iter_group_members(
            self, only_powered=False, active_only=False
        ):
            if (
                member.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
                and member.active_source != self.active_source
            ):
                # Use internal handler to get protocol selection and avoid redirect
                await self.mass.players._handle_cmd_stop(member.player_id)
            if (
                member.state.active_group is not None
                and member.state.active_group != self.player_id
            ):
                # collision: child is currently captured by a different group
                if other_group := self.mass.players.get_player(member.state.active_group):
                    if (
                        other_group.supports_feature(PlayerFeature.SET_MEMBERS)
                        and member.player_id not in other_group.static_group_members
                    ):
                        async with self.mass.players.wait_for_player_update(
                            member.player_id, timeout=5
                        ):
                            await other_group.set_members(player_ids_to_remove=[member.player_id])
                    # the other group can't release this member dynamically — stop
                    # it entirely so the member is freed. Route power-off through
                    # the controller so a FAKE-power group also gets its extra_data
                    # updated; calling other_group.power() directly would only set
                    # _attr_powered and leave the cached fake state out of sync.
                    elif other_group.state.power_control != PLAYER_CONTROL_NONE:
                        async with self.mass.players.wait_for_player_update(
                            member.player_id, timeout=5
                        ):
                            await self.mass.players._handle_cmd_power(other_group.player_id, False)
                    else:
                        async with self.mass.players.wait_for_player_update(
                            member.player_id, timeout=5
                        ):
                            await other_group.stop()
            if member.synced_to:
                # member is part of a syncgroup — release it first
                await member.ungroup()
            if not member.powered and member.power_control != PLAYER_CONTROL_NONE:
                await self.mass.players.cmd_power(member.player_id, True)

    def _set_attributes(self) -> None:
        """Set attributes of the group player."""
        prev_state = self._attr_playback_state
        # grab current media and state from one of the active players
        # use state properties (not raw attributes) to account for protocol player propagation
        for child_player in self.mass.players.iter_group_members(self, active_only=True):
            self._attr_playback_state = child_player.state.playback_state
            if child_player.state.elapsed_time:
                self._attr_elapsed_time = child_player.state.elapsed_time
                self._attr_elapsed_time_last_updated = child_player.state.elapsed_time_last_updated
            break
        else:
            self._attr_playback_state = PlaybackState.IDLE
        # idle grace handling: schedule a debounced release when playback
        # naturally transitions to IDLE (e.g. queue ended). Skipped if the
        # user has pinned the group with Fake power control.
        if (
            self._attr_playback_state == PlaybackState.IDLE
            and prev_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
            and self._attr_powered is not True
            and self.stream is not None
            and not self.stream.done
        ):
            self._schedule_idle_grace_timer()
        elif self._attr_playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            self._cancel_idle_grace_timer()
        self.update_state()

    def _schedule_idle_grace_timer(self) -> None:
        """Schedule a debounced session release after the stream becomes idle."""
        self._cancel_idle_grace_timer()
        self.logger.debug(
            "Scheduling idle-grace release for universal group %s in %ss",
            self.display_name,
            IDLE_GRACE_SECONDS,
        )
        self._idle_grace_task = self.mass.create_task(self._idle_grace_runner())

    def _cancel_idle_grace_timer(self) -> None:
        """Cancel any pending idle-grace release task."""
        if self._idle_grace_task is not None:
            if not self._idle_grace_task.done():
                self._idle_grace_task.cancel()
            self._idle_grace_task = None

    async def _idle_grace_runner(self) -> None:
        """Wait the grace window, then release members if still idle."""
        try:
            await asyncio.sleep(IDLE_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        # re-check state at fire time — a new play may have arrived, the user
        # may have powered the group on, or another path may have torn down
        # the session already.
        self._idle_grace_task = None
        if self._attr_powered is True:
            return
        if self._attr_playback_state != PlaybackState.IDLE:
            return
        self.logger.info(
            "Idle-grace expired for universal group %s, releasing members",
            self.display_name,
        )
        if self.stream and not self.stream.done:
            await self.stream.stop()
            self.stream = None
        # snap group_members back to the configured static set; this drops
        # is_active_session to False so children see active_group=None.
        self._attr_group_members = self._attr_static_group_members.copy()
        self.update_state()

    async def _serve_ugp_stream(self, request: web.Request) -> web.StreamResponse:
        """Serve the UGP (multi-client) flow stream audio to a player."""
        ugp_player_id = request.path.rsplit(".")[0].rsplit("/")[-1]
        # child_player_id is optional and only used for per-member DSP — never to
        # decide the output codec/rate. The output format is dictated by the UGP
        # player's own CONF_UGP_OUTPUT_FORMAT so every member receives an identical
        # encoded stream.
        child_player_id = request.query.get("player_id")

        if not (ugp_player := self.mass.players.get_player(ugp_player_id)):
            raise web.HTTPNotFound(reason=f"Unknown UGP player: {ugp_player_id}")
        if not self.stream or self.stream.done:
            raise web.HTTPNotFound(body=f"There is no active UGP stream for {ugp_player_id}!")

        output_format, output_format_str = resolve_ugp_output_format(
            cast("str", self.config.get_value(CONF_UGP_OUTPUT_FORMAT, UGP_OUTPUT_MP3))
        )
        headers = {
            **DEFAULT_STREAM_HEADERS,
            "contentFeatures.dlna.org": DLNA_CONTENT_FEATURES_REALTIME,
            "Content-Type": get_mime_type(output_format_str),
        }
        resp = web.StreamResponse(status=200, reason="OK", headers=headers)
        http_profile = self.get_config_value(CONF_HTTP_PROFILE, "chunked")
        # prefer the configuration of the player that actually renders the audio
        # (the member's active protocol player when it outputs via a protocol);
        # child player_id may be stale/invalid, then fall back to the group profile
        if child_player_id and (child_player := self.mass.players.get_player(child_player_id)):
            http_profile = child_player.get_output_config_value(CONF_HTTP_PROFILE, http_profile)
        if http_profile == "chunked" and request.version < HttpVersion11:
            # chunked encoding is not allowed on HTTP/1.0; fall back to
            # connection-close streaming to avoid raising in resp.prepare()
            self.logger.debug(
                "Disabling chunked encoding for UGP stream to HTTP/1.0 client %s",
                child_player_id or request.remote,
            )
            http_profile = "no_content_length"
        if http_profile == "forced_content_length":
            # some clients (notably older Chromecast firmware) refuse to play unless
            # they see a Content-Length header up front
            resp.content_length = 4294967296
        elif http_profile == "chunked":
            resp.enable_chunked_encoding()
        await resp.prepare(request)

        # return early if this is not a GET request
        if request.method != "GET":
            return resp

        self.logger.debug(
            "Start serving UGP flow audio stream for UGP-player %s to %s",
            ugp_player.display_name,
            child_player_id or request.remote,
        )

        # Generate filter params for the player specific DSP settings
        output_plan = None
        if child_player_id:
            output_plan = self.mass.streams.audio.get_player_output_plan(
                child_player_id,
                self.stream.input_format,
                output_format,
                queue_id=self.stream.queue_id,
                session_id=self.stream.session_id,
            )

        async for chunk in self.stream.get_stream(
            output_format,
            filter_params=output_plan.filter_params if output_plan else None,
        ):
            try:
                await resp.write(chunk)
            except ConnectionError, ConnectionResetError:
                break

        return resp
