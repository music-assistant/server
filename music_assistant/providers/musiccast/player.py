"""MusicCastPlayer."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from aiohttp.client_exceptions import ClientError
from aiomusiccast.exceptions import MusicCastGroupException
from aiomusiccast.pyamaha import MusicCastConnectionException
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, PlaybackState, PlayerFeature
from music_assistant_models.player import DeviceInfo, PlayerMedia, PlayerSource
from propcache import under_cached_property as cached_property

from music_assistant.models.player import Player
from music_assistant.providers.musiccast.avt_helpers import (
    avt_get_media_info,
    avt_next,
    avt_play,
    avt_previous,
    avt_set_url,
    avt_stop,
    search_xml,
)
from music_assistant.providers.musiccast.constants import (
    CONF_PLAYER_HANDLE_SOURCE_DISABLED,
    CONF_PLAYER_SWITCH_SOURCE_NON_NET,
    CONF_PLAYER_TURN_OFF_ON_LEAVE,
    MC_CONTROL_SOURCE_IDS,
    MC_NETUSB_SOURCE_IDS,
    MC_PASSIVE_SOURCE_IDS,
    MC_POLL_INTERVAL,
    MC_SOURCE_MAIN_SYNC,
    MC_SOURCE_MC_LINK,
    PLAYER_CONFIG_ENTRIES,
    PLAYER_ZONE_SPLITTER,
)
from music_assistant.providers.musiccast.musiccast import (
    MusicCastPhysicalDevice,
    MusicCastPlayerState,
    MusicCastZoneDevice,
)

if TYPE_CHECKING:
    from .provider import MusicCastProvider


@dataclass(kw_only=True)
class UpnpUpdateHelper:
    """UpnpUpdateHelper.

    See _update_player_attributes.
    """

    last_poll: float  # time.time
    controlled_by_mass: bool
    current_uri: str | None


class MusicCastPlayer(Player):
    """MusicCastPlayer in Music Assistant."""

    def __init__(
        self,
        provider: "MusicCastProvider",
        player_id: str,
        physical_device: MusicCastPhysicalDevice,
        zone_device: MusicCastZoneDevice,
    ) -> None:
        """Init MC Player.

        Keep reference to physical and zone device.
        """
        super().__init__(provider, player_id)
        self.physical_device = physical_device
        self.zone_device = zone_device

        # make this a property and update during normal state updates?
        # refers to being controlled by upnp.
        self.update_lock = asyncio.Lock()
        self.upnp_update_helper: UpnpUpdateHelper | None = None

    async def setup(self) -> None:
        """Set up player in Music Assistant."""
        self.set_static_attributes()

    def set_static_attributes(self) -> None:
        """Set static properties."""
        self._attr_supported_features = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,  # for non MA control, see pause method
            PlayerFeature.POWER,
            PlayerFeature.SELECT_SOURCE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
        }

        self._attr_device_info = DeviceInfo(
            manufacturer="Yamaha Corporation",
            model=self.physical_device.device.data.model_name or "unknown model",
            software_version=(self.physical_device.device.data.system_version or "unknown version"),
        )

        # polling
        self._attr_needs_poll = True
        self._attr_poll_interval = MC_POLL_INTERVAL

        # default MC name
        if self.zone_device.zone_data is not None:
            self._attr_name = self.zone_device.zone_data.name

        # group
        self._attr_can_group_with = {self.provider.instance_id}

        self._attr_available = True

        # SOURCES
        for source_id, source_name in self.zone_device.source_mapping.items():
            control = source_id in MC_CONTROL_SOURCE_IDS
            passive = source_id in MC_PASSIVE_SOURCE_IDS
            self._attr_source_list.append(
                PlayerSource(
                    id=source_id,
                    name=source_name,
                    passive=passive,
                    can_play_pause=control,
                    can_seek=False,
                    can_next_previous=control,
                )
            )

    async def set_dynamic_attributes(self) -> None:
        """Update Player attributes."""
        # ruff: noqa: PLR0915
        self._attr_available = True

        zone_data = self.zone_device.zone_data
        if zone_data is None:
            return

        self._attr_powered = zone_data.power == "on"

        # NOTE: aiomusiccast does not type hint the volume variables, and they may
        # be none, and not only integers
        _current_volume = cast("int | None", zone_data.current_volume)
        _max_volume = cast("int | None", zone_data.max_volume)
        _min_volume = cast("int | None", zone_data.min_volume)
        if _current_volume is None:
            self._attr_volume_level = None
        else:
            _min_volume = 0 if _min_volume is None else _min_volume
            _max_volume = 100 if _max_volume is None else _max_volume
            if _min_volume == _max_volume:
                _max_volume += 1
            self._attr_volume_level = int(_current_volume / (_max_volume - _min_volume) * 100)
        self._attr_volume_muted = zone_data.mute

        # STATE

        match self.zone_device.state:
            case MusicCastPlayerState.PAUSED:
                self._attr_playback_state = PlaybackState.PAUSED
            case MusicCastPlayerState.PLAYING:
                self._attr_playback_state = PlaybackState.PLAYING
            case MusicCastPlayerState.IDLE | MusicCastPlayerState.OFF:
                self._attr_playback_state = PlaybackState.IDLE
        self._attr_elapsed_time = self.zone_device.media_position
        if self.zone_device.media_position_updated_at is not None:
            self._attr_elapsed_time_last_updated = (
                self.zone_device.media_position_updated_at.timestamp()
            )
        else:
            self._attr_elapsed_time_last_updated = None

        # UPDATE UPNP HELPER
        now = time.time()
        if self.upnp_update_helper is None or now - self.upnp_update_helper.last_poll > 5:
            # Let's not do this too often
            # Note: The devices always return the last UPnP xmls, even if
            # currently another source/ playback method is used
            try:
                _xml_media_info = await avt_get_media_info(
                    self.mass.http_session, self.physical_device
                )
            except ClientError:
                # this is regularly called, we can ignore a failing update
                self.logger.debug("Acquiring media info failed, trying again in 5s.")
                if self.upnp_update_helper is not None:
                    self.upnp_update_helper.last_poll = now
                return
            _player_current_url = search_xml(_xml_media_info, "CurrentURI")

            # controlled by mass is only True, if we are directly controlled
            # i.e. we are not a group member.
            # the device's source id is server, if controlled by upnp, but also, if the internal
            # dlna function of the device are used. As a fallback, we then
            # use the item's title. This can only fail, if our current and next item
            # has the same name as the external.
            controlled_by_mass = False
            if _player_current_url is not None:
                controlled_by_mass = (
                    self.player_id in _player_current_url
                    and self.mass.streams.base_url in _player_current_url
                    and self.zone_device.source_id == "server"
                )

            self.upnp_update_helper = UpnpUpdateHelper(
                last_poll=now,
                controlled_by_mass=controlled_by_mass,
                current_uri=_player_current_url,
            )

        # UPDATE PLAYBACK INFORMATION
        # Note to self:
        # player._current_media tells queue controller what is playing
        # and player.set_current_media is the helper function
        # do not access the queue controller to gain playback information here
        if (
            self.upnp_update_helper.current_uri is not None
            and self.upnp_update_helper.controlled_by_mass
        ):
            self.set_current_media(uri=self.upnp_update_helper.current_uri, clear_all=True)
        elif self.zone_device.is_client:
            _server = self.zone_device.group_server
            _server_id = self._get_player_id_from_zone_device(_server)
            _server_player = cast("MusicCastPlayer | None", self.mass.players.get(_server_id))
            _server_update_helper: None | UpnpUpdateHelper = None
            if _server_player is not None:
                _server_update_helper = _server_player.upnp_update_helper
            if (
                _server_update_helper is not None
                and _server_update_helper.current_uri is not None
                and _server_update_helper.controlled_by_mass
            ):
                self.set_current_media(uri=_server_update_helper.current_uri, clear_all=True)
            else:
                self.set_current_media(
                    uri=f"{_server_id}_{_server.source_id}",
                    title=_server.media_title,
                    artist=_server.media_artist,
                    album=_server.media_album_name,
                    image_url=_server.media_image_url,
                )
        else:
            self.set_current_media(
                uri=f"{self.player_id}_{self.zone_device.source_id}",
                title=self.zone_device.media_title,
                artist=self.zone_device.media_artist,
                album=self.zone_device.media_album_name,
                image_url=self.zone_device.media_image_url,
            )

        # SOURCE
        self._attr_active_source = self.player_id
        if not self.zone_device.is_client and not self.upnp_update_helper.controlled_by_mass:
            self._attr_active_source = self.zone_device.source_id
        elif self.zone_device.is_client:
            _server = self.zone_device.group_server
            _server_id = self._get_player_id_from_zone_device(_server)
            _server_player = cast("MusicCastPlayer | None", self.mass.players.get(_server_id))
            if _server_player is not None and _server_player.upnp_update_helper is not None:
                self._attr_active_source = (
                    self.zone_device.source_id
                    if not _server_player.upnp_update_helper.controlled_by_mass
                    else None
                )

        # GROUPING
        # A zone cannot be synced to another zone or main of the same device.
        # Additionally, a zone can only be synced, if main is currently not using any netusb
        # function.
        # For a Zone which will be synced to main, grouping emits a "main_sync" instead
        # of a mc link. The other way round, we log a warning.
        if len(self.zone_device.musiccast_group) == 1:
            if self.zone_device.musiccast_group[0] == self.zone_device:
                # we are in a group with ourselves.
                self._attr_group_members.clear()

        elif not self.zone_device.is_client and not self.zone_device.is_server:
            self._attr_group_members.clear()

        elif self.zone_device.is_client:
            _synced_to_id = self._get_player_id_from_zone_device(self.zone_device.group_server)
            self._attr_group_members.clear()

        elif self.zone_device.is_server:
            self._attr_group_members = [
                self._get_player_id_from_zone_device(x) for x in self.zone_device.musiccast_group
            ]

        self.update_state()

    @cached_property
    def synced_to(self) -> str | None:
        """
        Return the id of the player this player is synced to (sync leader).

        If this player is not synced to another player (or is the sync leader itself),
        this should return None.
        If it is part of a (permanent) group, this should also return None.
        """
        if self.zone_device.is_network_client:
            server_id = self._get_player_id_from_zone_device(self.zone_device.group_server)
            return server_id if server_id != self.player_id else None
        return None

    async def _cmd_run(self, fun: Callable[..., Coroutine[Any, Any, None]], *args: Any) -> None:
        """Help function for all player cmds."""
        try:
            await fun(*args)
        except MusicCastConnectionException:
            # should go to provider here.
            await self._set_player_unavailable()
        except MusicCastGroupException:
            # can happen, user shall try again.
            ...

    async def _handle_zone_grouping(self, zone_player: MusicCastZoneDevice) -> None:
        """Handle zone grouping.

        If a device has multiple zones, only a single zone can be net controlled.
        If another zone wants to join the group, the current net zone has to switch
        its input to a non-net one and optionally turn off.

        This methods targets another zone of this players physical device!
        """
        # this is not this player's id
        player_id = self._get_player_id_from_zone_device(zone_player)
        assert player_id is not None  # for TYPE_CHECKING

        # skip zone handling if disabled.
        if bool(
            await self.mass.config.get_player_config_value(
                player_id, CONF_PLAYER_HANDLE_SOURCE_DISABLED
            )
        ):
            return

        _source = str(
            await self.mass.config.get_player_config_value(
                player_id, CONF_PLAYER_SWITCH_SOURCE_NON_NET
            )
        )
        # verify that this source actually exists and is non net
        _allowed_sources = self._get_allowed_sources_zone_switch(zone_player)
        mass_player = self.mass.players.get(player_id)
        if mass_player is None:
            # Do not assert here, should the player not yet exist
            return
        if _source not in _allowed_sources:
            msg = (
                "The switch source you specified for "
                f"{mass_player.display_name or mass_player.name}"
                " is not allowed. "
                f"The source must be any of: {', '.join(sorted(_allowed_sources))} "
                "Will use the first available source."
            )
            self.logger.error(msg)
            _source = _allowed_sources.pop()

        await mass_player.select_source(_source)
        _turn_off = bool(
            await self.mass.config.get_player_config_value(player_id, CONF_PLAYER_TURN_OFF_ON_LEAVE)
        )
        if _turn_off:
            await asyncio.sleep(2)
            await mass_player.power(powered=False)

    def _get_player_id_from_zone_device(self, zone_player: MusicCastZoneDevice) -> str:
        device_id = zone_player.physical_device.device.data.device_id
        assert device_id is not None
        return f"{device_id}{PLAYER_ZONE_SPLITTER}{zone_player.zone_name}"

    def _get_allowed_sources_zone_switch(self, zone_player: MusicCastZoneDevice) -> set[str]:
        """Return non net sources for a zone player."""
        assert zone_player.zone_data is not None, "zone data missing"
        _input_sources: set[str] = set(zone_player.zone_data.input_list)
        _net_sources = set(MC_NETUSB_SOURCE_IDS)
        _net_sources.add(MC_SOURCE_MC_LINK)  # mc grouping source
        _net_sources.add(MC_SOURCE_MAIN_SYNC)  # main zone sync
        return _input_sources.difference(_net_sources)

    async def _set_player_unavailable(self) -> None:
        """Set this player and associated zone players unavailable.

        Only called from a main zone player.
        """
        assert self.zone_device.zone_name == "main", "Call only from main player!"
        self.logger.debug("Player %s became unavailable.", self.display_name)

        if TYPE_CHECKING:
            assert isinstance(self.provider, MusicCastProvider)

        # disable polling
        self.physical_device.remove()

        async with self.update_lock:
            self._attr_available = False
            self.update_state()

        # set other zone unavailable
        for zone_device in self.zone_device.other_zones:
            if zone_device_player := self.mass.players.get(
                self._get_player_id_from_zone_device(zone_device)
            ):
                assert isinstance(zone_device_player, MusicCastPlayer)  # for type checking
                async with zone_device_player.update_lock:
                    zone_device_player._attr_available = False
                    zone_device_player.update_state()

    async def poll(self) -> None:
        """Poll player."""
        if self.update_lock.locked():
            # udp updates come in roughly every second when playing, so discard
            return
        if self.zone_device.zone_name != "main":
            # we only poll main, which polls the whole device
            return
        async with self.update_lock:
            # explicit polling on main
            try:
                await self.physical_device.fetch()
            except (MusicCastConnectionException, MusicCastGroupException):
                await self._set_player_unavailable()
                return
            except ClientError:
                return
            await self.set_dynamic_attributes()

    def _non_async_udp_callback(self, physical_device: MusicCastPhysicalDevice) -> None:
        """Call on UDP updates."""
        self.mass.loop.create_task(self._async_udp_callback())

    async def _async_udp_callback(self) -> None:
        async with self.update_lock:
            await self.set_dynamic_attributes()

    async def power(self, powered: bool) -> None:
        """Power command."""
        if powered:
            await self._cmd_run(self.zone_device.turn_on)
        else:
            await self._cmd_run(self.zone_device.turn_off)

    async def volume_set(self, volume_level: int) -> None:
        """Volume set command."""
        await self._cmd_run(self.zone_device.volume_set, volume_level)

    async def volume_mute(self, muted: bool) -> None:
        """Volume mute command."""
        await self._cmd_run(self.zone_device.volume_mute, muted)

    async def play(self) -> None:
        """Play command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            await avt_play(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.play)

    async def stop(self) -> None:
        """Stop command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            await avt_stop(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.stop)

    async def pause(self) -> None:
        """Pause command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            # if we are controlled by MA, i.e. upnp, send a stop, since
            # pause appears to be unreliable/ not working
            await avt_stop(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.pause)

    async def next_track(self) -> None:
        """Next command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            await avt_next(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.next_track)

    async def previous_track(self) -> None:
        """Previous command."""
        if self.upnp_update_helper is not None and self.upnp_update_helper.controlled_by_mass:
            await avt_previous(self.mass.http_session, self.physical_device)
        else:
            await self._cmd_run(self.zone_device.previous_track)

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        if len(self.physical_device.zone_devices) > 1:
            # zone handling
            # only a single zone may have netusb capability
            for zone_name, dev in self.physical_device.zone_devices.items():
                if zone_name == self.zone_device.zone_name:
                    continue
                if dev.is_netusb:
                    await self._handle_zone_grouping(dev)
        async with self.update_lock:
            # just in case
            if self.zone_device.source_id != "server":
                await self.select_source("server")
            await avt_set_url(self.mass.http_session, self.physical_device, player_media=media)
            await avt_play(self.mass.http_session, self.physical_device)

            self.upnp_update_helper = UpnpUpdateHelper(
                last_poll=time.time(),
                controlled_by_mass=True,
                current_uri=media.uri,
            )

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Enqueue next command."""
        await avt_set_url(
            self.mass.http_session,
            self.physical_device,
            player_media=media,
            enqueue=True,
        )

    async def select_source(self, source: str) -> None:
        """Select source command."""
        await self._cmd_run(self.zone_device.select_source, source)

    async def ungroup(self) -> None:
        """Ungroup command."""
        if self.zone_device.zone_name.startswith("zone"):
            # We are are zone.
            # We do not leave an MC group, but just change our source.
            await self._handle_zone_grouping(self.zone_device)
            return
        await self._cmd_run(self.zone_device.unjoin_player)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Set multiple members.

        This function is called on the server.
        """
        # Removing players
        if player_ids_to_remove:
            for player_id in player_ids_to_remove:
                if player := self.mass.players.get(player_id):
                    assert isinstance(player, MusicCastPlayer)  # for type checking
                    await player.ungroup()

        # Adding players
        if not player_ids_to_add:
            return
        children: set[str] = set()  # set[ma_player_id]
        children_zones: list[str] = []  # list[ma_player_id]
        player_ids_to_add = [] if player_ids_to_add is None else player_ids_to_add
        for child_id in player_ids_to_add:
            if child_player := self.mass.players.get(child_id):
                assert isinstance(child_player, MusicCastPlayer)  # for type checking
                _other_zone_mc: MusicCastZoneDevice | None = None
                for x in child_player.zone_device.other_zones:
                    if x.is_netusb:
                        _other_zone_mc = x
                if _other_zone_mc and _other_zone_mc != child_player.zone_device:
                    # of the same device, we use main_sync as input
                    if _other_zone_mc.zone_name == "main":
                        children_zones.append(child_id)
                    else:
                        self.logger.warning(
                            "It is impossible to join as a normal zone to another zone of the same "
                            "device. Only joining to main is possible. Please refer to the docs."
                        )
                else:
                    children.add(child_id)

        for child_id in children_zones:
            child_player = self.mass.players.get(child_id)
            if TYPE_CHECKING:
                child_player = cast("MusicCastPlayer", child_player)
            if child_player.zone_device.state == MusicCastPlayerState.OFF:
                await child_player.power(powered=True)
            await child_player.select_source(MC_SOURCE_MAIN_SYNC)
        if not children:
            return

        child_player_zone_devices: list[MusicCastZoneDevice] = []
        for child_id in children:
            child_player = self.mass.players.get(child_id)
            if TYPE_CHECKING:
                child_player = cast("MusicCastPlayer", child_player)
            child_player_zone_devices.append(child_player.zone_device)

        await self._cmd_run(self.zone_device.join_players, child_player_zone_devices)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Get player config entries."""
        base_entries = await super().get_config_entries(action=action, values=values)

        zone_entries: list[ConfigEntry] = []
        if len(self.physical_device.zone_devices) > 1:
            source_options: list[ConfigValueOption] = []
            allowed_sources = self._get_allowed_sources_zone_switch(self.zone_device)
            for (
                source_id,
                source_name,
            ) in self.zone_device.source_mapping.items():
                if source_id in allowed_sources:
                    source_options.append(ConfigValueOption(title=source_name, value=source_id))
            if len(source_options) == 0:
                # this should never happen
                self.logger.error(
                    "The player %s has multiple zones, but lacks a non-net source to switch to."
                    " Please report this on github or discord.",
                    self.display_name or self.name,
                )
                zone_entries = []
            else:
                zone_entries = [
                    ConfigEntry(
                        key=CONF_PLAYER_HANDLE_SOURCE_DISABLED,
                        type=ConfigEntryType.BOOLEAN,
                        label="Disable zone handling completely.",
                        default_value=False,
                        description="This disables zone handling completely. Other options "
                        "will be ignored. Enable should you encounter playback issues while "
                        "e.g. playing to main. You can also hide the player from the UI "
                        "by taking advantage of 'Hide the player in the user interface' "
                        "dropdown.",
                    ),
                    ConfigEntry(
                        key=CONF_PLAYER_SWITCH_SOURCE_NON_NET,
                        label="Switch to this non-net source when leaving a group.",
                        type=ConfigEntryType.STRING,
                        options=source_options,
                        default_value=source_options[0].value,
                        description="The zone will switch to this source when leaving a  group."
                        " It must be an input which doesn't require network connectivity.",
                    ),
                    ConfigEntry(
                        key=CONF_PLAYER_TURN_OFF_ON_LEAVE,
                        type=ConfigEntryType.BOOLEAN,
                        label="Turn off the zone when it leaves a group.",
                        default_value=False,
                        description="Turn off the zone when it leaves a group.",
                    ),
                ]

        return base_entries + zone_entries + PLAYER_CONFIG_ENTRIES
