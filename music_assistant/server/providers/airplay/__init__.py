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
from typing import TYPE_CHECKING

import aiofiles

from music_assistant.common.models.config_entries import (
    CONF_ENTRY_OUTPUT_CODEC,
    ConfigEntry,
    ConfigValueType,
)
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.common.models.player import DeviceInfo, Player
from music_assistant.common.models.queue_item import QueueItem
from music_assistant.constants import CONF_PLAYERS
from music_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import PlayerConfig, ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
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
        range=(0, 2000),
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
    ConfigEntry.from_dict(
        {**CONF_ENTRY_OUTPUT_CODEC.to_dict(), "default_value": "pcm", "hidden": True}
    ),
)

NEED_BRIDGE_RESTART = {"values/read_ahead", "values/encryption", "values/alac_encode"}


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

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
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
        return tuple(base_entries + PLAYER_CONFIG_ENTRIES)

    def on_player_config_changed(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        # forward to slimproto too
        slimproto_prov = self.mass.get_provider("slimproto")
        slimproto_prov.on_player_config_changed(config, changed_keys)

        async def update_config():
            # stop bridge (it will be auto restarted)
            if changed_keys.intersection(NEED_BRIDGE_RESTART):
                self.restart_bridge()

        asyncio.create_task(update_config())

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

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        flow_mode: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player."""
        # simply forward to underlying slimproto player
        slimproto_prov = self.mass.get_provider("slimproto")
        await slimproto_prov.cmd_play_media(
            player_id,
            queue_item=queue_item,
            seek_position=seek_position,
            fade_in=fade_in,
            flow_mode=flow_mode,
        )

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

    def _handle_player_register_callback(self, player: Player) -> None:
        """Handle player register callback from slimproto source player."""
        # TODO: Can we get better device info from mDNS ?
        player.provider = self.domain
        player.device_info = DeviceInfo(
            model="Airplay device",
            address=player.device_info.address,
            manufacturer="Generic",
        )
        player.supports_24bit = False

    def _handle_player_update_callback(self, player: Player) -> None:
        """Handle player update callback from slimproto source player."""
        # we could override anything on the player object here

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
            except OSError:
                return None

        base_path = os.path.join(os.path.dirname(__file__), "bin")
        if platform.system() == "Windows" and (
            bridge_binary := await check_bridge_binary(
                os.path.join(base_path, "squeeze2raop-static.exe")
            )
        ):
            return bridge_binary
        if platform.system() == "Darwin":
            # macos binary is autoselect x86_64/arm64
            if bridge_binary := await check_bridge_binary(
                os.path.join(base_path, "squeeze2raop-macos-static")
            ):
                return bridge_binary

        if platform.system() == "FreeBSD":
            # FreeBSD binary is x86_64 intel
            if bridge_binary := await check_bridge_binary(
                os.path.join(base_path, "squeeze2raop-freebsd-x86_64-static")
            ):
                return bridge_binary

        if platform.system() == "Linux":
            architecture = platform.machine()
            if architecture in ["AMD64", "x86_64"]:
                # generic linux x86_64 binary
                if bridge_binary := await check_bridge_binary(
                    os.path.join(
                        base_path,
                        "squeeze2raop-linux-x86_64-static",
                    )
                ):
                    return bridge_binary

            # other linux architecture... try all options one by one...
            for arch in ["aarch64", "arm", "armv6", "mips", "sparc64", "x86"]:
                if bridge_binary := await check_bridge_binary(
                    os.path.join(base_path, f"squeeze2raop-linux-{arch}-static")
                ):
                    return bridge_binary

        raise RuntimeError(
            f"Unable to locate RaopBridge for {platform.system()} ({platform.machine()})"
        )

    async def _bridge_process_runner(self, slimproto_prov: SlimprotoProvider) -> None:
        """Run the bridge binary in the background."""
        self.logger.debug(
            "Starting Airplay bridge using config file %s",
            self._config_file,
        )
        args = [
            self._bridge_bin,
            "-s",
            f"localhost:{slimproto_prov.port}",
            "-x",
            self._config_file,
            "-I",
            "-Z",
            "-d",
            "all=warn",
            # filter out apple tv's for now until we fix auth
            "-m",
            "apple-tv,appletv",
            # enable terminate on exit otherwise exists are soooo slooooowwww
            "-k",
        ]
        start_success = False
        while True:
            try:
                self._bridge_proc = await asyncio.create_subprocess_shell(
                    " ".join(args),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await self._bridge_proc.wait()
            except Exception as err:
                if not start_success:
                    raise err
                self.logger.exception("Error in Airplay bridge", exc_info=err)
            else:
                self.logger.debug("Airplay Bridge process stopped")
            if self._closing:
                break
            await asyncio.sleep(1)

    async def _stop_bridge(self) -> None:
        """Stop the bridge process."""
        if self._bridge_proc:
            try:
                self.logger.debug("Stopping bridge process...")
                self._bridge_proc.terminate()
                await self._bridge_proc.wait()
                self.logger.debug("Bridge process stopped.")
            except ProcessLookupError:
                pass

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

        # set codecs and sample rate to airplay default
        common_elem = xml_root.find("common")
        common_elem.find("codecs").text = "pcm"
        common_elem.find("sample_rate").text = "44100"
        common_elem.find("resample").text = "0"
        common_elem.find("player_volume").text = "20"

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
            # use raw config values because players are not
            # yet available at startup/init (race condition)
            raw_player_conf = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
            if not raw_player_conf:
                continue
            # prefer name from UDN because default name is often wrong
            udn = device_elem.find("udn").text
            udn_name = udn.split("@")[1].split("._")[0]
            device_elem.find("name").text = udn_name
            device_elem.find("enabled").text = "1" if raw_player_conf["enabled"] else "0"

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
            await _file.write(ET.tostring(xml_root).decode())

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
