"""Snapcast Player."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, TypedDict, cast

from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature
from music_assistant_models.player import DeviceInfo, PlayerMedia
from propcache import under_cached_property as cached_property

from music_assistant.constants import (
    ATTR_ANNOUNCEMENT_IN_PROGRESS,
    CONF_ENTRY_HTTP_PROFILE_HIDDEN,
    SYNCGROUP_PREFIX,
)
from music_assistant.models.player import Player
from music_assistant.providers.snapcast.constants import CONF_ENTRY_SAMPLE_RATES_SNAPCAST
from music_assistant.providers.snapcast.ma_stream import SnapcastMAStream

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

    from music_assistant.providers.snapcast.provider import SnapCastProvider
    from music_assistant.providers.snapcast.snap_cntrl_proto import (
        SnapclientProto,
        SnapstreamProto,
    )


class TrackedPlayerState(TypedDict, total=False):
    """Tracked state for the Snapcast MA player.

    It is used for change detection and state synchronization, and may be
    partially populated depending on which information is
    currently available.

    Keys prefixed with ``_attr_`` are exposed as player attributes, while the
    remaining keys represent internal Snapcast grouping and connection state.
    """

    # Player attribute fields
    _attr_name: str
    _attr_volume_level: float
    _attr_volume_muted: bool
    _attr_available: bool

    # snapclient fields
    connected: bool
    stream_id: str
    stream_status: str | None
    grp_name: str
    grp_member_ids: list[str]
    grp_member_avail: list[bool]


class SnapCastPlayer(Player):
    """SnapCastPlayer."""

    def __init__(
        self,
        provider: SnapCastProvider,
        player_id: str,
        snap_client: SnapclientProto,
    ) -> None:
        """Init."""
        self.snap_client = snap_client
        super().__init__(provider, player_id)

        self._snap_ma_stream: SnapcastMAStream | None = None

        self._update_worker: asyncio.Task[None] | None = None
        self._poke_evt = asyncio.Event()
        self._state_update_lock = asyncio.Lock()
        self._last_tracked_state: TrackedPlayerState | None = None

    @property
    def snap_provider(self) -> SnapCastProvider:
        """Return the Snapcast provider instance."""
        return cast("SnapCastProvider", self.provider)

    @property
    def requires_flow_mode(self) -> bool:
        """Return if the player requires flow mode."""
        return True

    @cached_property
    def synced_to(self) -> str | None:
        """Return the id of the player this player is synced to (sync leader)."""
        grp_name = self.snap_group_name
        if grp_name == self.player_id:
            # is group leader
            return None

        grp_player_ids = self._get_player_ids_of_curr_group()
        if len(grp_player_ids) < 2 or grp_name not in grp_player_ids:
            return None

        if leader_player := self.mass.players.get(grp_name):
            return grp_name if leader_player.available else None

        return None

    @cached_property
    def group_members(self) -> list[str]:
        """Return the group members of the player."""
        if not self._attr_available:
            return []

        grp_name = self.snap_group_name
        if grp_name != self.player_id:
            # only group leaders can have members
            return []

        player_ids = self._get_player_ids_of_curr_group()
        if self.player_id not in player_ids:
            # should not happen, unless the current
            # state repr is invalid
            return []

        player_ids.remove(self.player_id)
        connected = [
            player_id
            for player_id in player_ids
            if (client := self.snap_provider.get_snap_client(player_id=player_id))
            and client.connected
        ]
        if connected:
            return [self.player_id, *connected]

        return []

    @property
    def playback_state(self) -> PlaybackState:
        """Return the current playback state of the player."""
        snap_stream = self._get_active_snapstream()
        if snap_stream is None:
            return PlaybackState.IDLE

        if snap_stream.identifier == "default" or snap_stream.status == "idle":
            return PlaybackState.IDLE

        return PlaybackState.PLAYING

    def setup(self) -> None:
        """Set up player."""
        self._attr_name = self.snap_client.friendly_name
        self._attr_available = self.snap_client.connected

        host_dict = self.snap_client._client.get("host", {})
        os, arch, ip = (host_dict.get(key, "") for key in ["os", "arch", "ip"])
        self._attr_device_info = DeviceInfo(
            model=os,
            manufacturer=arch,
        )
        self._attr_device_info.ip_address = ip
        self._attr_supported_features = {
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PLAY_ANNOUNCEMENT,
        }
        self._attr_can_group_with = {self.snap_provider.instance_id}
        if not self._update_worker:
            self._update_worker = self.mass.create_task(self._player_update_worker)

    async def volume_set(self, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # Use optimistic server state for now
        # not guaranteed that the client respects it
        await self.snap_client.set_volume(volume_level)

    async def stop(self) -> None:
        """Send STOP command to given player."""
        if ma_stream := self.active_snap_ma_stream:
            ma_stream.request_stop_stream()
            return

        self.poke_player_update()

    async def volume_mute(self, muted: bool) -> None:
        """Send MUTE command to given player."""
        # Use optimistic server state for now
        # not guaranteed that the client respects it
        # TODO: move this to the snapcast python library
        vol = self.snap_client._client["config"]["volume"]
        vol["muted"] = muted
        res = await self.snap_provider._snapserver.client_volume(self.snap_client.identifier, vol)
        if res and "muted" in res:
            self.snap_client._client["config"]["volume"] = res
            self.snap_client.callback()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        # get the group owned by this player (identified by the group name)
        player_group = await self.snap_provider.ensure_player_owned_group(self.player_id)

        if player_group is None:
            return

        player_group.set_callback(None)

        curr_ma_player_ids = [
            ma_id
            for cli_id in player_group.clients
            if (ma_id := self.snap_provider._get_ma_id(cli_id))
        ]

        curr_stream_id = player_group.stream
        sync_group_player = None
        if curr_ma_stream := self.snap_provider.get_snap_ma_stream(curr_stream_id):
            media = curr_ma_stream.media
            if media.media_type == MediaType.PLUGIN_SOURCE:
                custom_data = media.custom_data or {}
                assigned_player = custom_data.get("player_id", "")
                if assigned_player.startswith(SYNCGROUP_PREFIX):
                    sync_group_player = self.mass.players.get(assigned_player)
            else:
                media_src_id = media.source_id or ""
                if media_src_id.startswith(SYNCGROUP_PREFIX):
                    sync_group_player = self.mass.players.get(media_src_id)

        if sync_group_player and self.player_id in (player_ids_to_remove or []):
            # players in sync_group_player.group_members will be rejoined
            # remove others first
            for id_to_remove in player_ids_to_remove or []:
                if id_to_remove == self.player_id:
                    continue
                if (
                    id_to_remove in curr_ma_player_ids
                    and id_to_remove not in sync_group_player.group_members
                ):
                    await self.snap_provider.isolate_player_to_dedicated_group(
                        id_to_remove, target_stream_id="default"
                    )

            # split remaining group into individual groups,
            # keeps the current stream, set this group to default stream
            await self.snap_provider.isolate_player_to_dedicated_group(
                target_player_id=self.player_id,
                target_stream_id="default",
                others_stream_id=curr_stream_id,
            )
        else:
            for player_id in player_ids_to_remove or []:
                if player_id not in curr_ma_player_ids:
                    continue
                await self.snap_provider.isolate_player_to_dedicated_group(
                    player_id, target_stream_id="default"
                )
                curr_ma_player_ids.remove(player_id)

        for ma_id in player_ids_to_add or []:
            if (
                snap_id := self.snap_provider._get_snapclient_id(ma_id)
            ) and ma_id not in curr_ma_player_ids:
                await player_group.add_client(snap_id)

        # some caller require instant state updates before returning
        async with self._state_update_lock:
            if await self._process_snapcast_client_state():
                self.update_state()

        self.snap_provider._update_group_callbacks(poke=True)

    async def play_media(self, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player."""
        if self.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)

        ma_stream = await self.snap_provider.get_snapcast_media_stream(
            media, filter_settings_owner=self.player_id
        )

        if ma_stream is None or ma_stream.stream_id is None:
            return

        self._snap_ma_stream = ma_stream

        # e.g. DSP settings require a restart
        await self._snap_ma_stream.start_stream(allow_restart=True)

        # if no announcement is playing we activate the stream now, otherwise it
        # will be activated by play_announcement when the announcement is over.
        if not self.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
            player_group = await self.snap_provider.ensure_player_owned_group(self.player_id)
            assert player_group is not None  # for type checking
            await player_group.set_stream(ma_stream.stream_id)

        self.poke_player_update()

    async def play_announcement(
        self, announcement: PlayerMedia, volume_level: int | None = None
    ) -> None:
        """Handle (provider native) playback of an announcement on given player."""
        was_synced_to: str | None = self.synced_to
        orig_volume_level: int | None = self.volume_level

        prev_stream = self.active_snap_ma_stream

        ma_stream = await self.snap_provider.get_snapcast_media_stream(
            announcement, filter_settings_owner=self.player_id
        )
        player_group = await self.snap_provider.ensure_player_owned_group(self.player_id)

        if ma_stream is None or ma_stream.stream_id is None or player_group is None:
            return

        await player_group.set_stream(ma_stream.stream_id)

        if self.snap_provider._use_builtin_server:
            await asyncio.sleep(self.snap_provider._snapcast_server_buffer_size / 1000.0)

        if volume_level is not None:
            await self.volume_set(volume_level)

        await ma_stream.start_stream()
        await ma_stream.wait_for_stopped()

        if self.volume_level == volume_level and orig_volume_level is not None:
            await self.volume_set(orig_volume_level)

        if was_synced_to:
            if (
                leader_group := await self.snap_provider.ensure_player_owned_group(was_synced_to)
            ) is None:
                return
            await leader_group.add_client(self.snap_client.identifier)
        else:
            await player_group.set_stream(
                prev_stream.stream_id
                if prev_stream and prev_stream.stream_id is not None
                else "default"
            )

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Player config."""
        return [
            CONF_ENTRY_SAMPLE_RATES_SNAPCAST,
            # we don't use the http server for streaming
            CONF_ENTRY_HTTP_PROFILE_HIDDEN,
        ]

    def _handle_player_update(self, snap_client: SnapclientProto) -> None:
        """Forward snap_client updates."""
        self.poke_player_update()

    def poke_player_update(self) -> None:
        """Signal that a player state update should be processed."""
        self._poke_evt.set()

    async def _player_update_worker(self) -> None:
        """Aggregate and process player state update requests."""
        while True:
            await self._poke_evt.wait()
            self._poke_evt.clear()
            while True:
                call_update: bool = False
                async with self._state_update_lock:
                    call_update = await self._process_snapcast_client_state()
                if call_update:
                    self.update_state()
                if self._poke_evt.is_set():
                    self._poke_evt.clear()
                    continue
                break

    async def _process_snapcast_client_state(self) -> bool:
        """Process the latest Snapcast client state and apply changes to this player.

        Returns:
        True if changes were applied and a state update should be emitted via
        ``update_state()``; False if no update is necessary (or if required data
        is temporarily unavailable and the update should be retried later).
        """
        snap_group = self.snap_client.group
        if snap_group is None:
            # some data syncing error, a client is always a group member
            # retry again later, don't call update now
            return False

        stream_id = snap_group.stream
        snap_stream: SnapstreamProto | None = None
        with suppress(KeyError):
            snap_stream = self.snap_provider._snapserver.stream(stream_id)

        members = list(snap_group.clients)  # snapshot

        curr_state: TrackedPlayerState = {
            "_attr_name": self.snap_client.friendly_name,
            "_attr_volume_level": self.snap_client.volume,
            "_attr_volume_muted": self.snap_client.muted,
            "_attr_available": self.snap_client.connected,
            "connected": self.snap_client.connected,
            "stream_id": snap_group.stream,
            "stream_status": snap_stream.status if snap_stream is not None else None,
            "grp_name": snap_group.name,
            "grp_member_ids": members,
            "grp_member_avail": [
                pl.available
                for cl_id in members
                if (pl_id := self.snap_provider._get_ma_id(cl_id))
                and (pl := self.mass.players.get(pl_id))
            ],
        }

        prev_state: TrackedPlayerState = (
            self._last_tracked_state if self._last_tracked_state is not None else {}
        )
        self._last_tracked_state = curr_state

        # change detection for simple attrs
        changed_attrs = {
            k: v for k, v in curr_state.items() if k.startswith("_attr_") and prev_state.get(k) != v
        }

        prev_connected = prev_state.get("connected", False)
        now_connected = curr_state.get("connected", False)
        connection_changed = prev_connected != now_connected

        prev_stream_id = prev_state.get("stream_id")
        curr_stream_id = curr_state["stream_id"]
        prev_stream_status = prev_state.get("stream_status")
        curr_stream_status = curr_state.get("stream_status")

        stream_changed = (
            prev_stream_id != curr_stream_id or prev_stream_status != curr_stream_status
        )

        grouping_changed = any(
            prev_state.get(k) != curr_state.get(k)
            for k in ("grp_name", "grp_member_ids", "grp_member_avail")
        )

        needs_processing = bool(
            changed_attrs or grouping_changed or stream_changed or connection_changed
        )
        if not needs_processing:
            return False

        if connection_changed or grouping_changed:
            self.snap_provider.poke_group_members(snap_group)

        # help cleaning up unused streams
        if curr_stream_id == "default" or (
            (my_stream := self._snap_ma_stream)
            and my_stream.stream_id in {prev_stream_id, curr_stream_id}
        ):
            self.snap_provider.update_stream_usage()

        # apply changed attrs
        for key, value in changed_attrs.items():
            setattr(self, key, value)

        # finally notify state update once
        return True

    @property
    def active_snap_ma_stream(self) -> SnapcastMAStream | None:
        """Return the MA stream source of the active group."""
        grp = self.snap_client.group
        if grp is None or grp.stream is None:
            return None

        if grp.stream == "default":
            return None

        return self.snap_provider.get_snap_ma_stream(grp.stream)

    @property
    def snap_group_name(self) -> str:
        """Return the name of the active group."""
        snap_group = self.snap_client.group
        if snap_group is None:
            return ""
        return snap_group.name

    @cached_property
    def _current_media(self) -> PlayerMedia | None:
        """
        Return the current media being played by the player.

        Note that this is NOT the final current media of the player,
        as it may be overridden by a active group/sync membership.
        Hence it's marked as a private property.
        The final current media can be retrieved by using the 'current_media' property.
        """
        if snap_ma_stream := self.active_snap_ma_stream:
            return snap_ma_stream.media
        return None

    @property
    def _active_source(self) -> str | None:
        """
        Return the (id of) the active source of the player.

        Only required if the player supports PlayerFeature.SELECT_SOURCE.

        Set to None if the player is not currently playing a source or
        the player_id if the player is currently playing a MA queue.

        Note that this is NOT the final active source of the player,
        as it may be overridden by a active group/sync membership.
        Hence it's marked as a private property.
        The final active source can be retrieved by using the 'active_source' property.
        """
        grp = self.snap_client.group
        if grp is None or grp.stream is None:
            return None

        if grp.stream == "default":
            return None

        if ma_stream := self.snap_provider.get_snap_ma_stream(grp.stream):
            return ma_stream.source_id

        # external snapcast stream
        return grp.stream or None

    def _get_active_snapstream(self) -> SnapstreamProto | None:
        """Get active stream for given player_id."""
        if group := self.snap_client.group:
            with suppress(KeyError):
                return self.snap_provider._snapserver.stream(group.stream)
        return None

    def _get_player_ids_of_curr_group(self) -> list[str]:
        snap_group = self.snap_client.group
        if snap_group is None:
            return []
        return [
            ma_id
            for client_id in snap_group.clients
            if (ma_id := self.snap_provider._get_ma_id(client_id))
        ]

    def _get_players_of_curr_group(self) -> list[Player]:
        return [
            ma_player
            for ma_id in self._get_player_ids_of_curr_group()
            if (ma_player := self.mass.players.get(ma_id))
        ]
