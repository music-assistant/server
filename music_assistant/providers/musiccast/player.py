"""MusicCastPlayer."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from aiohttp.client_exceptions import ClientError
from aiomusiccast.capabilities import BinarySensor as MCBinarySensor
from aiomusiccast.capabilities import BinarySetter as MCBinarySetter
from aiomusiccast.capabilities import NumberSensor as MCNumberSensor
from aiomusiccast.capabilities import NumberSetter as MCNumberSetter
from aiomusiccast.capabilities import OptionSetter as MCOptionSetter
from aiomusiccast.capabilities import TextSensor as MCTextSensor
from aiomusiccast.exceptions import MusicCastGroupException
from aiomusiccast.pyamaha import MusicCastConnectionException
from aiomusiccast.pyamaha import System as MCSystem
from mashumaro import DataClassDictMixin
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    IdentifierType,
    PlaybackState,
    PlayerFeature,
)
from music_assistant_models.player import (
    DeviceInfo,
    PlayerMedia,
    PlayerOption,
    PlayerOptionEntry,
    PlayerOptionType,
    PlayerOptionValueType,
    PlayerSoundMode,
    PlayerSource,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.helpers.util import is_valid_mac_address
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
    CONF_PLAYER_AUTO_ADVANCE,
    CONF_PLAYER_HANDLE_SOURCE_DISABLED,
    CONF_PLAYER_SWITCH_SOURCE_NON_NET,
    CONF_PLAYER_TURN_OFF_ON_LEAVE,
    MC_CAPABILITIES,
    MC_CONTROL_SOURCE_IDS,
    MC_NETUSB_SOURCE_IDS,
    MC_PASSIVE_SOURCE_IDS,
    MC_POLL_INTERVAL,
    MC_SOUND_MODE_FRIENDLY_NAMES,
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


def get_player_option_translation_key(mc_key: str) -> str:
    """
    Get translation key for player option.

    MC key has format like 'zone_ENHANCER' or 'zone_TONE_CONTROL_bass'
    """
    mc_key = mc_key.lower().replace("zone_", "")
    if mc_key == "tone_control_bass":
        return "bass"
    if mc_key == "tone_control_treble":
        return "treble"
    if mc_key == "surr_decoder_type":
        return "surround_decoder_type"
    return mc_key


@dataclass
class MusicCastMacAddresses(DataClassDictMixin):
    """
    MusicCastMacAddresses.

    The MAC addresses lack the colons.
    """

    wired_lan: str | None = None
    wireless_lan: str | None = None
    wireless_direct: str | None = None


@dataclass
class MusicCastNetworkStatus(DataClassDictMixin):
    """Helper class to parse the relevant information from aiomusiccast."""

    connection: str | None = None
    ip_address: str | None = None
    mac_address: MusicCastMacAddresses | None = None


@dataclass(kw_only=True)
class UpnpUpdateHelper:
    """
    UpnpUpdateHelper.

    See _update_player_attributes.
    """

    last_poll: float  # time.time
    controlled_by_mass: bool
    current_uri: str | None


class MusicCastPlayer(Player):
    """MusicCastPlayer in Music Assistant."""

    def __init__(
        self,
        provider: MusicCastProvider,
        player_id: str,
        physical_device: MusicCastPhysicalDevice,
        zone_device: MusicCastZoneDevice,
    ) -> None:
        """
        Init MC Player.

        Keep reference to physical and zone device.
        """
        super().__init__(provider, player_id)
        self.physical_device = physical_device
        self.zone_device = zone_device

        # make this a property and update during normal state updates?
        # refers to being controlled by upnp.
        self.update_lock = asyncio.Lock()
        self.upnp_update_helper: UpnpUpdateHelper | None = None
        # last netusb_track value, used to detect device-driven gapless transitions
        self._last_netusb_track: str | None = None
        # used to detect when the device dropped to idle mid-queue without
        # honouring the queued NextURI (Yamaha gapless can fail this way)
        self._last_playback_state: PlaybackState | None = None
        self._last_playing_elapsed_time: float = 0.0

    async def setup(self) -> None:
        """Set up player in Music Assistant."""
        await self.set_static_attributes()
        await self.set_dynamic_attributes(update_state=False)

    async def set_static_attributes(self) -> None:
        """Set static properties."""
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.POWER,
            PlayerFeature.SELECT_SOURCE,
            PlayerFeature.NEXT_PREVIOUS,
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
            PlayerFeature.SELECT_SOUND_MODE,
            PlayerFeature.OPTIONS,
        }

        self._attr_device_info = DeviceInfo(
            manufacturer="Yamaha Corporation",
            model=self.physical_device.device.data.model_name or "unknown model",
            software_version=(self.physical_device.device.data.system_version or "unknown version"),
        )

        if "zone" not in self.player_id:
            # we do not add mac/ ip information to zone players to prevent false player merging
            network_status = await self.physical_device.device.device.request_json(
                MCSystem.get_network_status()
            )
            network_info = MusicCastNetworkStatus.from_dict(network_status)
            mac_address: str | None = None

            if network_info.connection is not None and network_info.mac_address is not None:
                mac_address = getattr(network_info.mac_address, network_info.connection, None)

            if device_ip := self.physical_device.device.device.ip:
                self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, device_ip)

            if mac_address is not None:
                mac = ":".join(mac_address[i : i + 2].upper() for i in range(0, 12, 2))
                self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac)

            if device_id := self.physical_device.device.data.device_id:
                self._attr_device_info.add_identifier(IdentifierType.UUID, device_id)
                # device_id is the MAC address (12 hex chars), format as XX:XX:XX:XX:XX:XX
                if len(device_id) == 12 and mac_address is None:
                    # fallback to device id for mac
                    mac = ":".join(device_id[i : i + 2].upper() for i in range(0, 12, 2))
                    # Only add MAC address if it's valid (not 00:00:00:00:00:00)
                    if is_valid_mac_address(mac):
                        self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac)

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

        # SOUND MODES
        for source_id in self.zone_device.sound_mode_list:
            friendly_name = MC_SOUND_MODE_FRIENDLY_NAMES.get(source_id) or " ".join(
                [x.capitalize() for x in source_id.split("_")]
            )
            self._attr_sound_mode_list.append(
                PlayerSoundMode(id=source_id, name=friendly_name, passive=False)
            )

    async def set_dynamic_attributes(self, update_state: bool = True) -> None:
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

        self._attr_elapsed_time = None
        match self.zone_device.state:
            case MusicCastPlayerState.PAUSED:
                self._attr_playback_state = PlaybackState.PAUSED
            case MusicCastPlayerState.PLAYING:
                self._attr_playback_state = PlaybackState.PLAYING
                if self.zone_device.media_position_updated_at is not None:
                    self._attr_elapsed_time = self.zone_device.media_position
                    self._attr_elapsed_time_last_updated = (
                        self.zone_device.media_position_updated_at.timestamp()
                    )
            case MusicCastPlayerState.IDLE | MusicCastPlayerState.OFF:
                self._attr_playback_state = PlaybackState.IDLE

        # UPDATE UPNP HELPER
        now = time.time()
        _current_netusb_track = (
            self.physical_device.device.data.netusb_track if self.zone_device.is_netusb else None
        )
        _netusb_track_changed = (
            self._last_netusb_track is not None
            and _current_netusb_track is not None
            and _current_netusb_track != self._last_netusb_track
        )
        self._last_netusb_track = _current_netusb_track
        _upnp_cache_age = (
            None if self.upnp_update_helper is None else now - self.upnp_update_helper.last_poll
        )
        # invalidate the cache on a netusb_track change so a gapless transition
        # reflects in current_uri without waiting for the regular 5s refresh
        _upnp_cache_hit = (
            _upnp_cache_age is not None and _upnp_cache_age <= 5 and not _netusb_track_changed
        )
        _prev_current_uri = (
            self.upnp_update_helper.current_uri if self.upnp_update_helper is not None else None
        )
        if not _upnp_cache_hit:
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

        # either freshly assigned above or a cache hit (which implies it was set before)
        assert self.upnp_update_helper is not None

        # UPDATE PLAYBACK INFORMATION
        # Note to self:
        # player._current_media tells queue controller what is playing
        # and player.set_current_media is the helper function
        # do not access the queue controller to gain playback information here
        self._attr_supported_features.add(PlayerFeature.PAUSE)  # we support pause...
        if (
            self.upnp_update_helper.current_uri is not None
            and self.upnp_update_helper.controlled_by_mass
        ):
            self._attr_supported_features.discard(
                PlayerFeature.PAUSE
            )  # ...unless we are controlled by MA
            self.set_current_media(uri=self.upnp_update_helper.current_uri, clear_all=True)
        elif self.zone_device.is_client:
            _server = self.zone_device.group_server
            _server_id = self._get_player_id_from_zone_device(_server)
            _server_player = cast(
                "MusicCastPlayer | None", self.mass.players.get_player(_server_id)
            )
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
            _server_player = cast(
                "MusicCastPlayer | None", self.mass.players.get_player(_server_id)
            )
            if _server_player is not None and _server_player.upnp_update_helper is not None:
                self._attr_active_source = (
                    self.zone_device.source_id
                    if not _server_player.upnp_update_helper.controlled_by_mass
                    else None
                )

        # SOUND MODE
        self._attr_active_sound_mode = self.zone_device.sound_mode_id

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

        # disallow set members (i.e. a zone to become a group leader) if it is currently grouped to the main zone
        if self.zone_device.source_id == MC_SOURCE_MAIN_SYNC:
            self._attr_supported_features.discard(PlayerFeature.SET_MEMBERS)
        else:
            self._attr_supported_features.add(PlayerFeature.SET_MEMBERS)

        # PLAYER OPTIONS
        # see https://github.com/vigonotion/aiomusiccast/blob/main/aiomusiccast/capabilities.py
        # capability can be any instance of OptionSetter, BinarySetter, NumberSetter, NumberSensor,
        # BinarySensor, TextSensor
        # the type hint of the lib's zone_data.capabilities is wrong (_not_ list[str])
        self._attr_options = []
        for capability in cast(
            "list[MC_CAPABILITIES]",
            zone_data.capabilities,
        ):
            if isinstance(capability, MCBinarySensor):
                self._attr_options.append(
                    PlayerOption(
                        key=capability.id,
                        translation_key=get_player_option_translation_key(capability.id),
                        name=capability.name,
                        type=PlayerOptionType.BOOLEAN,
                        read_only=True,
                        value=capability.current,
                    )
                )
            elif isinstance(capability, MCBinarySetter):
                self._attr_options.append(
                    PlayerOption(
                        key=capability.id,
                        translation_key=get_player_option_translation_key(capability.id),
                        name=capability.name,
                        type=PlayerOptionType.BOOLEAN,
                        value=capability.current,
                        read_only=False,
                    )
                )
            elif isinstance(capability, MCNumberSensor):
                self._attr_options.append(
                    PlayerOption(
                        key=capability.id,
                        translation_key=get_player_option_translation_key(capability.id),
                        name=capability.name,
                        type=PlayerOptionType.INTEGER,
                        value=capability.current,
                        read_only=True,
                    )
                )
            elif isinstance(capability, MCNumberSetter):
                self._attr_options.append(
                    PlayerOption(
                        key=capability.id,
                        translation_key=get_player_option_translation_key(capability.id),
                        name=capability.name,
                        type=PlayerOptionType.INTEGER,
                        value=capability.current,
                        read_only=False,
                        min_value=capability.value_range.minimum,
                        max_value=capability.value_range.maximum,
                        step=capability.value_range.step,
                    )
                )
            elif isinstance(capability, MCTextSensor):
                self._attr_options.append(
                    PlayerOption(
                        key=capability.id,
                        translation_key=get_player_option_translation_key(capability.id),
                        name=capability.name,
                        type=PlayerOptionType.STRING,
                        value=capability.current,
                        read_only=True,
                    )
                )
            elif isinstance(capability, MCOptionSetter):
                options = []
                for option_key, option_name in capability.options.items():
                    options.append(
                        PlayerOptionEntry(
                            key=str(option_key),  # aiomusiccast allows str and int.
                            name=option_name,
                            value=str(option_key),
                            type=PlayerOptionType.STRING,
                        )
                    )
                self._attr_options.append(
                    PlayerOption(
                        key=capability.id,
                        translation_key=get_player_option_translation_key(capability.id),
                        name=capability.name,
                        type=PlayerOptionType.STRING,
                        value=str(capability.current),
                        read_only=False,
                        options=UniqueList(options),
                    )
                )

        if update_state:
            self.update_state()

        # state.current_media is queue-derived, so a current_uri change alone does not
        # produce a state diff. Nudge the queue directly so it re-parses the new URI.
        if (
            update_state
            and self.upnp_update_helper.controlled_by_mass
            and self.upnp_update_helper.current_uri != _prev_current_uri
        ):
            self.mass.player_queues.on_player_update(self, {})

        self._maybe_advance_on_track_end()

    def _maybe_advance_on_track_end(self) -> None:
        """Schedule a queue advance if the device went idle at end of track."""
        # The device sometimes drops the queued NextURI and stops instead of
        # transitioning. Recover by calling next() on the queue. Gated by a
        # per-player config so users who don't want the safety net can opt out.
        _prev_state = self._last_playback_state
        self._last_playback_state = self._attr_playback_state
        if self._attr_playback_state == PlaybackState.PLAYING:
            if self._attr_elapsed_time is not None:
                self._last_playing_elapsed_time = self._attr_elapsed_time
            return
        if (
            _prev_state != PlaybackState.PLAYING
            or self._attr_playback_state != PlaybackState.IDLE
            or self.upnp_update_helper is None
            or not self.upnp_update_helper.controlled_by_mass
        ):
            return
        if not bool(
            self.mass.config.get_raw_player_config_value(
                self.player_id, CONF_PLAYER_AUTO_ADVANCE, default=True
            )
        ):
            return
        queue = self.mass.player_queues.get(self.player_id)
        if queue is None or queue.current_item is None or queue.next_item is None:
            return
        _duration = queue.current_item.duration or 0
        # only act within 4 s of track duration to minimise hijacking a user stop
        if not _duration or self._last_playing_elapsed_time < _duration - 4:
            return
        self.mass.call_later(
            3,
            self._advance_queue_after_idle,
            queue.current_item.queue_item_id,
            task_id=f"musiccast_advance_after_idle_{self.player_id}",
        )

    async def _advance_queue_after_idle(self, expected_current_item_id: str) -> None:
        """Advance the queue if the player is still idle on the same item."""
        if self._attr_playback_state != PlaybackState.IDLE:
            return
        queue = self.mass.player_queues.get(self.player_id)
        if queue is None or not queue.active:
            return
        if (
            queue.current_item is None
            or queue.current_item.queue_item_id != expected_current_item_id
        ):
            return
        if queue.next_item is None:
            return
        self.logger.debug("Advancing queue to next item after end-of-track idle")
        await self.mass.player_queues.next(self.player_id)

    @property
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
        """
        Handle zone grouping.

        If a device has multiple zones, only a single zone can be net controlled.
        If another zone wants to join the group, the current net zone has to switch
        its input to a non-net one and optionally turn off.

        This methods targets another zone of this players physical device!
        """
        # this is not this player's id
        player_id = self._get_player_id_from_zone_device(zone_player)
        assert player_id is not None  # for TYPE_CHECKING

        mass_player = self.mass.players.get_player(player_id)
        if mass_player is None:
            # Do not assert here, should the player not yet exist
            return

        # skip zone handling if player is disabled globally
        if not mass_player.enabled:
            self.logger.debug("Ignoring zone handling for disabled player %s.", player_id)
            return

        # skip zone handling if disabled via setting
        if mass_player.get_config_value(CONF_PLAYER_HANDLE_SOURCE_DISABLED):
            self.logger.debug("Ignoring zone handling for player %s.", player_id)
            return

        self.logger.debug("Handling zone for player %s.", player_id)

        _source = mass_player.get_config_value(CONF_PLAYER_SWITCH_SOURCE_NON_NET, return_type=str)
        # verify that this source actually exists and is non net
        _allowed_sources = self._get_allowed_sources_zone_switch(zone_player)
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
        _turn_off = mass_player.get_config_value(CONF_PLAYER_TURN_OFF_ON_LEAVE, return_type=bool)
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
        """Set this player and associated zone players unavailable."""
        self.logger.debug("Player %s became unavailable.", self.display_name)

        if TYPE_CHECKING:
            assert isinstance(self.provider, MusicCastProvider)

        # UDP polling is stopped but the physical device stays registered so
        # the next poll can recover it.
        self.physical_device.disable_polling()

        # no update_lock: _cmd_run can call this while play_media already holds it
        self._attr_available = False
        self.update_state()

        for zone_device in self.zone_device.other_zones:
            if zone_device_player := self.mass.players.get_player(
                self._get_player_id_from_zone_device(zone_device)
            ):
                assert isinstance(zone_device_player, MusicCastPlayer)  # for type checking
                zone_device_player._attr_available = False
                zone_device_player.update_state()

    async def _set_player_available(self) -> None:
        """Re-enable UDP polling and refresh zone players after recovery."""
        assert self.zone_device.zone_name == "main", "Call only from main player!"
        self.logger.debug("Player %s became available again.", self.display_name)
        await self.physical_device.enable_polling()
        for zone_device in self.zone_device.other_zones:
            if zone_device_player := self.mass.players.get_player(
                self._get_player_id_from_zone_device(zone_device)
            ):
                assert isinstance(zone_device_player, MusicCastPlayer)  # for type checking
                async with zone_device_player.update_lock:
                    await zone_device_player.set_dynamic_attributes()

    async def poll(self) -> None:
        """Poll player."""
        if self.update_lock.locked():
            # udp updates come in roughly every second when playing, so discard
            return
        if self.zone_device.zone_name != "main":
            # we only poll main, which polls the whole device
            return
        async with self.update_lock:
            _was_unavailable = not self._attr_available
            try:
                await self.physical_device.fetch()
            except MusicCastConnectionException, MusicCastGroupException:
                await self._set_player_unavailable()
                return
            except ClientError:
                return
            if _was_unavailable:
                await self._set_player_available()
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
        _zone_handling_attempted = False
        if len(self.physical_device.zone_devices) > 1:
            # zone handling
            # only a single zone may have netusb capability
            for zone_name, dev in self.physical_device.zone_devices.items():
                if zone_name == self.zone_device.zone_name:
                    continue
                # skip powered-off zones: their remembered source can match netusb_input
                # without actually consuming the resource, and switching can affect main
                if dev.is_netusb and dev.zone_data is not None and dev.zone_data.power == "on":
                    await self._handle_zone_grouping(dev)
                    _zone_handling_attempted = True
        async with self.update_lock:
            # re-assert "server" when zone handling ran or the cached source is stale;
            # autoplay_disabled stops the device resuming the input's last queue
            if _zone_handling_attempted or self.zone_device.source_id != "server":
                await self._cmd_run(self.zone_device.select_source, "server", "autoplay_disabled")
            media.uri = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
            # clear any pending AVT state to avoid wedging on rapid play_media
            await avt_stop(self.mass.http_session, self.physical_device)
            await avt_set_url(self.mass.http_session, self.physical_device, player_media=media)
            await avt_play(self.mass.http_session, self.physical_device)

            self.upnp_update_helper = UpnpUpdateHelper(
                last_poll=time.time(),
                controlled_by_mass=True,
                current_uri=media.uri,
            )

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Enqueue next command."""
        media.uri = await self.provider.mass.streams.resolve_stream_url(self.player_id, media)
        await avt_set_url(
            self.mass.http_session,
            self.physical_device,
            player_media=media,
            enqueue=True,
        )

    async def select_source(self, source: str) -> None:
        """Select source command."""
        await self._cmd_run(self.zone_device.select_source, source)

    async def select_sound_mode(self, sound_mode: str) -> None:
        """Select sound Mode Command."""
        await self._cmd_run(self.zone_device.select_sound_mode, sound_mode)

    async def set_option(self, option_key: str, option_value: PlayerOptionValueType) -> None:
        """Set player option."""
        if self.zone_device.zone_data is None:
            return
        for capability in cast(
            "list[MC_CAPABILITIES]",
            self.zone_device.zone_data.capabilities,
        ):
            if str(capability.id) != option_key:
                continue
            if not isinstance(capability, MCBinarySetter | MCNumberSetter | MCOptionSetter):
                self.logger.error(f"Option {capability.name} is read only!")
                return
            if isinstance(capability, MCBinarySetter):
                await capability.set(bool(option_value))
            elif isinstance(capability, MCNumberSetter):
                min_value = capability.value_range.minimum
                max_value = capability.value_range.maximum
                if not min_value <= int(option_value) <= max_value:
                    self.logger.error(
                        f"Option {capability.name} has numeric range of"
                        f"{min_value} <= value <= {max_value}"
                    )
                    return
                await capability.set(int(option_value))
            elif isinstance(capability, MCOptionSetter):
                assert isinstance(option_value, str | int)  # for type checking
                _option_value = option_value  # we may have an int in aiomusiccast as key
                with suppress(ValueError):
                    _option_value = int(_option_value)
                if _option_value not in capability.options:
                    self.logger.error(f"Option {_option_value} is not allowed for {option_key}")
                    return
                await capability.set(_option_value)
            break

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
        """
        Set multiple members.

        This function is called on the server.
        """
        # Removing players
        if player_ids_to_remove:
            for player_id in player_ids_to_remove:
                if player := self.mass.players.get_player(player_id):
                    assert isinstance(player, MusicCastPlayer)  # for type checking
                    await player.ungroup()

        # Adding players
        if not player_ids_to_add:
            return
        children: set[str] = set()  # set[ma_player_id]
        children_zones: list[str] = []  # list[ma_player_id]
        player_ids_to_add = [] if player_ids_to_add is None else player_ids_to_add
        for child_id in player_ids_to_add:
            child_player = self.mass.players.get_player(child_id)
            if child_player is None:
                continue
            assert isinstance(child_player, MusicCastPlayer)  # for type checking

            # find a sibling zone on the child's device currently using netusb;
            # skip disabled zones (user opted out of MA managing them)
            _other_zone_mc: MusicCastZoneDevice | None = None
            for x in child_player.zone_device.other_zones:
                if not x.is_netusb:
                    continue
                _other_player_id = self._get_player_id_from_zone_device(x)
                _other_player = self.mass.players.get_player(_other_player_id)
                if _other_player is None or not _other_player.enabled:
                    continue
                _other_zone_mc = x
                # only one zone can hold netusb at a time
                break

            # no conflicting sibling -> standard client join
            if _other_zone_mc is None:
                children.add(child_id)
                continue

            # child is a non-main zone of a device whose main is the netusb consumer;
            # join the group via main_sync so the child follows main locally
            if child_player.zone_device.zone_name != "main" and _other_zone_mc.zone_name == "main":
                children_zones.append(child_id)
                continue

            # child is main but a sibling holds netusb; free the sibling so main
            # can become the netusb client, then join normally
            if child_player.zone_device.zone_name == "main":
                await child_player._handle_zone_grouping(_other_zone_mc)
                children.add(child_id)
                continue

            # non-main child while another non-main sibling holds netusb is unsupported
            self.logger.warning(
                "It is impossible to join as a normal zone to another zone of the same "
                "device. Only joining to main is possible. Please refer to the docs."
            )

        for child_id in children_zones:
            child_player = self.mass.players.get_player(child_id)
            if TYPE_CHECKING:
                child_player = cast("MusicCastPlayer", child_player)
            if child_player.zone_device.state == MusicCastPlayerState.OFF:
                await child_player.power(powered=True)
            await child_player.select_source(MC_SOURCE_MAIN_SYNC)
        if not children:
            return

        child_player_zone_devices: list[MusicCastZoneDevice] = []
        for child_id in children:
            child_player = self.mass.players.get_player(child_id)
            if TYPE_CHECKING:
                child_player = cast("MusicCastPlayer", child_player)
            child_player_zone_devices.append(child_player.zone_device)

        await self._cmd_run(self.zone_device.join_players, child_player_zone_devices)

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Get player config entries."""
        base_entries = await super().get_config_entries()

        zone_entries: list[ConfigEntry] = []
        if len(self.physical_device.zone_devices) > 1:
            source_options: list[ConfigValueOption] = []
            allowed_sources = self._get_allowed_sources_zone_switch(self.zone_device)
            for (
                source_id,
                source_name,
            ) in self.zone_device.source_mapping.items():
                if source_id in allowed_sources:
                    source_options.append(ConfigValueOption(source_id, title=source_name))
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
                        default_value=False,
                    ),
                    ConfigEntry(
                        key=CONF_PLAYER_SWITCH_SOURCE_NON_NET,
                        type=ConfigEntryType.STRING,
                        options=source_options,
                        default_value=source_options[0].value,
                    ),
                    ConfigEntry(
                        key=CONF_PLAYER_TURN_OFF_ON_LEAVE,
                        type=ConfigEntryType.BOOLEAN,
                        default_value=False,
                    ),
                ]

        auto_advance_entry = ConfigEntry(
            key=CONF_PLAYER_AUTO_ADVANCE,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
        )

        return base_entries + zone_entries + [auto_advance_entry] + PLAYER_CONFIG_ENTRIES
