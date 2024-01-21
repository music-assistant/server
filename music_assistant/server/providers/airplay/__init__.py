"""Airplay Player provider.

This is more like a "virtual" player provider, running on top of slimproto.
It uses the amazing work of Philippe44 who created a bridge from airplay to slimproto.
https://github.com/philippe44/LMS-Raop
"""
from __future__ import annotations

import asyncio
import os
import platform
import xml.etree.ElementTree as ET  # noqa: N817
from contextlib import suppress
from typing import TYPE_CHECKING

import aiofiles

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ConfigEntryType, ProviderFeature
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.constants import CONF_LOG_LEVEL, CONF_PLAYERS
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant
    from music_assistant.server.controllers.streams import MultiClientStreamJob
    from music_assistant.server.models import ProviderInstanceType
    from music_assistant.server.providers.slimproto import SlimprotoProvider


PLAYER_CONFIG_ENTRIES = (
    ConfigEntry(
        key="airplay_header",
        type=ConfigEntryType.DIVIDER,
        label="Airplay specific settings",
        description="Configure Airplay specific settings. "
        "Note that changing any airplay specific setting, will reconnect all players.",
        advanced=True,
    ),
    ConfigEntry(
        key="read_ahead",
        type=ConfigEntryType.INTEGER,
        range=(200, 3000),
        default_value=1000,
        label="Read ahead buffer",
        description="Sets the number of milliseconds of audio buffer in the player. "
        "This is important to absorb network throughput jitter. "
        "Note that the resume after pause will be skipping that amount of time "
        "and volume changes will be delayed by the same amount, when using digital volume.",
        advanced=True,
    ),
    ConfigEntry(
        key="encryption",
        type=ConfigEntryType.BOOLEAN,
        default_value=False,
        label="Enable encryption",
        description="Enable encrypted communication with the player, "
        "some (3rd party) players require this.",
        advanced=True,
    ),
    ConfigEntry(
        key="alac_encode",
        type=ConfigEntryType.BOOLEAN,
        default_value=True,
        label="Enable compression",
        description="Save some network bandwidth by sending the audio as "
        "(lossless) ALAC at the cost of a bit CPU.",
        advanced=True,
    ),
    ConfigEntry(
        key="remove_timeout",
        type=ConfigEntryType.INTEGER,
        default_value=0,
        range=(-1, 3600),
        label="Remove timeout",
        description="Player discovery is managed using mDNS protocol, "
        "which means that a player sends regular keep-alive messages and a bye when "
        "disconnecting. Some faulty mDNS stack implementations (e.g. Riva) do not always"
        "send keep-alive messages, so the Airplay bridge is disconnecting them regularly. \n\n"
        "As a workaround, a timer can be set so that the bridge does not immediately remove "
        "the player from LMS when missing a keep-alive, waiting for it to reconnect. \n\n\n"
        "A value of -1 will disable this feature and never remove the player. \n\n"
        "A value of 0 (the default) disabled the player when keep-alive is missed or "
        "when a bye message is received. \n\n"
        "Any other value means to disable the player after missing keep-alive for "
        "this number of seconds.",
        advanced=True,
    ),
)

NEED_BRIDGE_RESTART = {"values/read_ahead", "values/encryption", "values/alac_encode", "enabled"}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = AirplayProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


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
    # ruff: noqa: ARG001
    return tuple()  # we do not have any config entries (yet)


class AirplayProvider(PlayerProvider):
    """Player provider for Airplay based players, using the slimproto bridge."""

    _bridge_bin: str | None = None
    _bridge_proc: asyncio.subprocess.Process | None = None
    _timer_handle: asyncio.TimerHandle | None = None
    _closing: bool = False
    _config_file: str | None = None
    _log_reader_task: asyncio.Task | None = None
    _removed_players: set[str] | None = None

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        # for now do not allow creation of airplay groups
        # in preparation of new airplay provider coming up soon
        # return (ProviderFeature.SYNC_PLAYERS,)
        return tuple()

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self._removed_players = set()
        self._config_file = os.path.join(self.mass.storage_path, "airplay_bridge.xml")
        # locate the raopbridge binary (will raise if that fails)
        self._bridge_bin = await self._get_bridge_binary()
        # make sure that slimproto provider is loaded
        slimproto_prov: SlimprotoProvider = self.mass.get_provider("slimproto")
        assert slimproto_prov, "This provider depends on the SlimProto provider."
        # register as virtual provider on slimproto provider
        slimproto_prov.register_virtual_provider(
            "RaopBridge",
            self._handle_player_register_callback,
            self._handle_player_update_callback,
        )
        await self._check_config_xml()
        # start running the bridge
        asyncio.create_task(self._bridge_process_runner(slimproto_prov))

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        self._closing = True
        if slimproto_prov := self.mass.get_provider("slimproto"):
            slimproto_prov.unregister_virtual_provider("RaopBridge")
        await self._stop_bridge()

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry, ...]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        slimproto_prov = self.mass.get_provider("slimproto")
        base_entries = await slimproto_prov.get_player_config_entries(player_id)
        return base_entries + PLAYER_CONFIG_ENTRIES

    def on_player_config_changed(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        super().on_player_config_changed(config, changed_keys)
        # forward to slimproto too
        slimproto_prov = self.mass.get_provider("slimproto")
        slimproto_prov.on_player_config_changed(config, changed_keys)

        async def update_config():
            # stop bridge (it will be auto restarted)
            if changed_keys.intersection(NEED_BRIDGE_RESTART):
                self.restart_bridge()

        asyncio.create_task(update_config())

    def on_player_config_removed(self, player_id: str) -> None:
        """Call (by config manager) when the configuration of a player is removed."""
        super().on_player_config_removed(player_id)
        self._removed_players.add(player_id)
        self.restart_bridge()

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.cmd_stop(player_id)

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.cmd_play(player_id)

    async def play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int,
        fade_in: bool,
    ) -> None:
        """Handle PLAY MEDIA on given player.

        This is called by the Queue controller to start playing a queue item on the given player.
        The provider's own implementation should work out how to handle this request.

            - player_id: player_id of the player to handle the command.
            - queue_item: The QueueItem that needs to be played on the player.
            - seek_position: Optional seek to this position.
            - fade_in: Optionally fade in the item at playback start.
        """
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.play_media(
            player_id,
            queue_item=queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
        )

    async def play_stream(self, player_id: str, stream_job: MultiClientStreamJob) -> None:
        """Handle PLAY STREAM on given player.

        This is a special feature from the Universal Group provider.
        """
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.play_stream(player_id, stream_job)

    async def enqueue_next_queue_item(self, player_id: str, queue_item: QueueItem):
        """Handle enqueuing of the next queue item on the player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.enqueue_next_queue_item(player_id, queue_item)

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.cmd_pause(player_id)

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.cmd_power(player_id, powered)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.cmd_volume_set(player_id, volume_level)

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.cmd_volume_mute(player_id, muted)

    async def cmd_sync(self, player_id: str, target_player: str) -> None:
        """Handle SYNC command for given player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.cmd_sync(player_id, target_player)

    async def cmd_unsync(self, player_id: str) -> None:
        """Handle UNSYNC command for given player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.cmd_unsync(player_id)

    async def _handle_player_register_callback(self, player: Player) -> None:
        """Handle player register callback from slimproto source player."""
        # TODO: Can we get better device info from mDNS ?
        player.provider = self.domain
        player.device_info = DeviceInfo(
            model="Airplay device",
            address=player.device_info.address,
            manufacturer="Generic",
        )
        player.supports_24bit = False
        # disable sonos by default
        if "sonos" in player.name.lower() or "rincon" in player.name.lower():
            player.enabled_by_default = False

        # extend info from the discovery xml
        async with aiofiles.open(self._config_file, "r") as _file:
            xml_data = await _file.read()
            with suppress(ET.ParseError):
                xml_root = ET.XML(xml_data)
                for device_elem in xml_root.findall("device"):
                    player_id = device_elem.find("mac").text
                    if player_id != player.player_id:
                        continue
                    # prefer name from UDN because default name is often wrong
                    udn = device_elem.find("udn").text
                    udn_name = udn.split("@")[1].split("._")[0]
                    player.name = udn_name
                    break

    def _handle_player_update_callback(self, player: Player) -> None:
        """Handle player update callback from slimproto source player."""

    async def _get_bridge_binary(self):
        """Find the correct bridge binary belonging to the platform."""
        # ruff: noqa: SIM102

        async def check_bridge_binary(bridge_binary_path: str) -> str | None:
            try:
                bridge_binary = await asyncio.create_subprocess_exec(
                    *[bridge_binary_path, "-t", "-x", self._config_file],
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout, _ = await bridge_binary.communicate()
                if (
                    bridge_binary.returncode == 1
                    and b"This program is free software: you can redistribute it and/or modify"
                    in stdout
                ):
                    self._bridge_bin = bridge_binary_path
                    return bridge_binary_path
            except OSError as err:
                self.logger.exception(err)
                return None

        base_path = os.path.join(os.path.dirname(__file__), "bin")

        system = platform.system().lower()
        architecture = platform.machine().lower()

        if bridge_binary := await check_bridge_binary(
            os.path.join(base_path, f"squeeze2raop-{system}-{architecture}-static")
        ):
            return bridge_binary

        raise RuntimeError(f"Unable to locate RaopBridge for {system}/{architecture}")

    async def _bridge_process_runner(self, slimproto_prov: SlimprotoProvider) -> None:
        """Run the bridge binary in the background."""
        self.logger.debug(
            "Starting Airplay bridge using config file %s",
            self._config_file,
        )
        conf_log_level = self.config.get_value(CONF_LOG_LEVEL)
        enable_debug_log = conf_log_level == "DEBUG"
        args = [
            self._bridge_bin,
            "-s",
            f"localhost:{slimproto_prov.port}",
            "-x",
            self._config_file,
            "-I",
            "-Z",
            "-d",
            f'all={"debug" if enable_debug_log else "warn"}',
            # filter out apple tv's for now until we fix auth
            "-m",
            "apple-tv,appletv",
        ]
        start_success = False
        while True:
            try:
                self._bridge_proc = await asyncio.create_subprocess_shell(
                    " ".join(args),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                self._log_reader_task = asyncio.create_task(self._log_reader())
                await self._bridge_proc.wait()
            except Exception as err:
                if not start_success:
                    raise err
                self.logger.exception("Error in Airplay bridge", exc_info=err)
            if self._closing:
                break
            await asyncio.sleep(10)

    async def _stop_bridge(self) -> None:
        """Stop the bridge process."""
        if self._bridge_proc:
            try:
                self.logger.info("Stopping bridge process...")
                self._bridge_proc.terminate()
                await self._bridge_proc.wait()
                self.logger.info("Bridge process stopped.")
                await asyncio.sleep(5)
            except ProcessLookupError:
                pass
        if self._log_reader_task and not self._log_reader_task.done():
            self._log_reader_task.cancel()

    async def _check_config_xml(self, recreate: bool = False) -> None:
        """Check the bridge config XML file."""
        # ruff: noqa: PLR0915
        if recreate or not os.path.isfile(self._config_file):
            if os.path.isfile(self._config_file):
                os.remove(self._config_file)
            # discover players and create default config file
            args = [
                self._bridge_bin,
                "-i",
                self._config_file,
            ]
            proc = await asyncio.create_subprocess_shell(
                " ".join(args),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

        # read xml file's data
        async with aiofiles.open(self._config_file, "r") as _file:
            xml_data = await _file.read()

        try:
            xml_root = ET.XML(xml_data)
        except ET.ParseError as err:
            if recreate:
                raise err
            await self._check_config_xml(True)
            return

        # set common/global values
        common_elem = xml_root.find("common")
        for key, value in {
            "codecs": "flc,pcm",
            "sample_rate": "44100",
            "resample": "0",
        }.items():
            xml_elem = common_elem.find(key)
            xml_elem.text = value

        # default values for players
        for conf_entry in PLAYER_CONFIG_ENTRIES:
            if conf_entry.type == ConfigEntryType.LABEL:
                continue
            conf_val = conf_entry.default_value
            xml_elem = common_elem.find(conf_entry.key)
            if xml_elem is None:
                xml_elem = ET.SubElement(common_elem, conf_entry.key)
            if conf_entry.type == ConfigEntryType.BOOLEAN:
                xml_elem.text = "1" if conf_val else "0"
            else:
                xml_elem.text = str(conf_val)

        # get/set all device configs
        for device_elem in xml_root.findall("device"):
            player_id = device_elem.find("mac").text
            if player_id in self._removed_players:
                xml_root.remove(device_elem)
                self._removed_players.remove(player_id)
                continue
            # use raw config values because players are not
            # yet available at startup/init (race condition)
            raw_player_conf = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
            if not raw_player_conf:
                continue
            device_elem.find("enabled").text = "1" if raw_player_conf["enabled"] else "0"

            # set some values that are not (yet) configurable
            for key, value in {
                "player_volume": "-1",
                "prevent_playback": "off",
            }.items():
                xml_elem = device_elem.find(key)
                if xml_elem is None:
                    xml_elem = ET.SubElement(device_elem, key)
                xml_elem.text = value

            # set values based on config entries
            for conf_entry in PLAYER_CONFIG_ENTRIES:
                if conf_entry.type == ConfigEntryType.LABEL:
                    continue
                conf_val = raw_player_conf["values"].get(conf_entry.key, conf_entry.default_value)
                xml_elem = device_elem.find(conf_entry.key)
                if xml_elem is None:
                    xml_elem = ET.SubElement(device_elem, conf_entry.key)
                if conf_entry.type == ConfigEntryType.BOOLEAN:
                    xml_elem.text = "1" if conf_val else "0"
                else:
                    xml_elem.text = str(conf_val)

        # save config file
        async with aiofiles.open(self._config_file, "w") as _file:
            xml_str = ET.tostring(xml_root)
            await _file.write(xml_str.decode())

    async def _log_reader(self) -> None:
        """Read log output from bridge process."""
        bridge_logger = self.logger.getChild("squeeze2raop")
        while self._bridge_proc.returncode is None:
            async for line in self._bridge_proc.stdout:
                bridge_logger.debug(line.decode().strip())

    def restart_bridge(self) -> None:
        """Schedule restart of bridge process."""
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

        async def restart_bridge():
            self.logger.info("Restarting Airplay bridge (due to config changes)")
            await self._stop_bridge()
            await self._check_config_xml()

        # schedule the action for later
        self._timer_handle = self.mass.loop.call_later(10, self.mass.create_task, restart_bridge)
