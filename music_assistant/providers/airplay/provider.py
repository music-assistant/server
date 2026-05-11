"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import plistlib
import socket
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from ipaddress import ip_address
from typing import cast

from music_assistant_models.enums import PlaybackState
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import CONF_ENABLED, CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.util import (
    format_ip_for_url,
    get_ip_pton,
    get_primary_ip_address_from_zeroconf,
    select_free_port,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    AIRPLAY_DISCOVERY_TYPE,
    AIRPLAY_VOLUME_MUTE,
    CONF_IGNORE_VOLUME,
    CONF_STORED_VOLUME,
    DACP_DISCOVERY_TYPE,
    FALLBACK_VOLUME,
    RAOP_DISCOVERY_TYPE,
)
from .helpers import convert_airplay_volume, get_model_info
from .player import AirPlayPlayer
from .sendspin_bridge import SendspinBridgeManager

# TODO: AirPlay provider
# Implement Companion protocol for communicating with original Apple (TV) devices
# This allows for getting state/metadata changes from the device,
# even if we are not actively streaming to it.


DEFAULT_AIRPLAY_PORT = 7000
DEFAULT_RAOP_PORT = 5000
MANUAL_DISCOVERY_TIMEOUT = 5.0


@dataclass(frozen=True)
class ManualAirPlayDiscovery:
    """Discovery result for a manually configured AirPlay target."""

    display_name: str
    device_id: str
    service_infos: tuple[AsyncServiceInfo, ...]


def _normalize_manual_airplay_host(address: str) -> str:
    """Normalize a manual AirPlay IP address or hostname."""
    host = address.strip()
    if not host:
        raise ValueError("Address is empty")
    if any(char in host for char in ("/", "?", "#")):
        raise ValueError("Only IP addresses or hostnames are supported")

    ip_candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        parsed_ip = ip_address(ip_candidate)
    except ValueError:
        pass
    else:
        if parsed_ip.is_unspecified:
            raise ValueError("Address is unspecified")
        return str(parsed_ip)

    if ":" in host:
        raise ValueError("Custom ports are not supported")
    if any(char.isspace() for char in host):
        raise ValueError("Address contains whitespace")
    return host


def _stringify_airplay_info_value(value: object) -> str | None:
    """Convert an AirPlay /info plist value to a TXT-record-compatible string."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"0x{value:x}"
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def _normalize_airplay_device_id(value: object) -> str | None:
    """Normalize a device ID/MAC address into 12 uppercase hex chars."""
    if not isinstance(value, str):
        return None
    normalized = value.replace(":", "").replace("-", "").upper()
    if len(normalized) != 12:
        return None
    try:
        int(normalized, 16)
    except ValueError:
        return None
    if normalized in ("000000000000", "FFFFFFFFFFFF"):
        return None
    return normalized


def _device_id_from_airplay_info(info: Mapping[str, object]) -> str | None:
    """Return the stable device ID from AirPlay /info metadata."""
    for key in ("deviceID", "deviceid", "macAddress", "mac_address"):
        if device_id := _normalize_airplay_device_id(info.get(key)):
            return device_id
    return None


def _airplay_info_to_txt_properties(info: Mapping[str, object]) -> dict[str, str]:
    """Map an AirPlay /info plist response to mDNS TXT-like properties."""
    properties: dict[str, str] = {"txtvers": "1"}
    for key, value in info.items():
        if txt_value := _stringify_airplay_info_value(value):
            properties[key] = txt_value

    if device_id := _device_id_from_airplay_info(info):
        properties["deviceid"] = ":".join(device_id[i : i + 2] for i in range(0, 12, 2))

    if source_version := info.get("sourceVersion"):
        if txt_value := _stringify_airplay_info_value(source_version):
            properties["srcvers"] = txt_value

    if status_flags := info.get("statusFlags"):
        if txt_value := _stringify_airplay_info_value(status_flags):
            properties["sf"] = txt_value
            properties["flags"] = txt_value
    properties.setdefault("sf", properties.get("flags", "0x0"))
    properties.setdefault("flags", properties["sf"])

    if model := properties.get("model"):
        properties.setdefault("am", model)

    return properties


def _display_name_from_airplay_info(info: Mapping[str, object], fallback: str) -> str:
    """Return display name from AirPlay /info metadata."""
    for key in ("name", "deviceName", "displayName", "sourceDisplayName"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _parse_airplay_info_response(response: bytes) -> dict[str, object] | None:
    """Parse a HTTP/RTSP /info response body into a plist dictionary."""
    if b"\r\n\r\n" not in response:
        return None
    header_bytes, body = response.split(b"\r\n\r\n", 1)
    header_lines = header_bytes.decode("utf-8", "ignore").split("\r\n")
    status_line = header_lines[0] if header_lines else ""
    if " 200 " not in f" {status_line} ":
        return None
    headers: dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    if content_length := headers.get("content-length"):
        with suppress(ValueError):
            body = body[: int(content_length)]
    try:
        parsed = plistlib.loads(body)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return cast("dict[str, object]", parsed)


class AirPlayProvider(PlayerProvider):
    """Player provider for AirPlay based players."""

    _dacp_server: asyncio.Server
    _dacp_info: AsyncServiceInfo
    _bridge_manager: SendspinBridgeManager
    _manual_ip_config: tuple[str, ...] = ()

    @property
    def bridge_manager(self) -> SendspinBridgeManager:
        """Return the Sendspin bridge manager."""
        return self._bridge_manager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        manual_ip_config = cast(
            "list[str]",
            self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key, []),
        )
        self._manual_ip_config = tuple(
            address.strip() for address in manual_ip_config if address.strip()
        )

        # Initialize Sendspin bridge manager for protocol linking
        self._bridge_manager = SendspinBridgeManager(self)

        # register DACP zeroconf service
        dacp_port = await select_free_port(39831, 49831)
        # Use first 16 hex chars of server_id as a persistent DACP ID
        # This ensures the DACP ID remains the same across restarts, which is required
        # for AirPlay 2 (HAP) pair-verify to work with previously paired devices
        self.dacp_id = dacp_id = self.mass.server_id[:16].upper()
        self.logger.debug("Starting DACP ActiveRemote %s on port %s", dacp_id, dacp_port)
        self._dacp_server = await asyncio.start_server(self._handle_dacp_request, port=dacp_port)
        server_id = f"iTunes_Ctrl_{dacp_id}.{DACP_DISCOVERY_TYPE}"
        self._dacp_info = AsyncServiceInfo(
            DACP_DISCOVERY_TYPE,
            name=server_id,
            addresses=[await get_ip_pton(str(self.mass.streams.publish_ip))],
            port=dacp_port,
            properties={
                "txtvers": "1",
                "Ver": "63B5E5C0C201542E",
                "DbId": "63B5E5C0C201542E",
                "OSsi": "0x1F5",
            },
            server=f"{socket.gethostname()}.local",
        )
        await self.mass.discovery.aiozc.async_register_service(self._dacp_info)

    async def discover_players(self) -> None:
        """Discover manually configured AirPlay players."""
        await self._setup_manual_players()

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if not info:
            if state_change == ServiceStateChange.Removed and "@" in name:
                # Service name is enough to mark the player as unavailable on 'Removed' notification
                raw_id, display_name = name.split(".", maxsplit=1)[0].split("@", 1)
            else:
                # If we are not in a 'Removed' state, we need info to be filled to update the player
                return
        elif "@" in info.name:
            raw_id, display_name = info.name.split(".")[0].split("@", 1)
        elif deviceid := info.decoded_properties.get("deviceid"):
            raw_id = deviceid.replace(":", "")
            display_name = info.name.split(".")[0]
        else:
            return
        player_id = f"ap{raw_id.lower()}"
        # handle removed player
        if state_change == ServiceStateChange.Removed:
            if _player := self.mass.players.get_player(player_id):
                # the player has become unavailable
                self.logger.debug("Player offline: %s", _player.display_name)
                # Remove the Sendspin bridge first
                await self._bridge_manager.remove_bridge(player_id)
                await self.mass.players.unregister(player_id)
            return
        # handle update for existing device
        assert info is not None  # type guard
        player: AirPlayPlayer | None
        if player := cast("AirPlayPlayer | None", self.mass.players.get_player(player_id)):
            # update the latest discovery info for existing player
            player.set_discovery_info(info, display_name)
            return
        await self._setup_player(player_id, display_name, info)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Stop all Sendspin bridges
        bridge_manager = getattr(self, "_bridge_manager", None)
        if bridge_manager:
            await bridge_manager.stop_all()
        # shutdown DACP server
        if self._dacp_server:
            self._dacp_server.close()
        # shutdown DACP zeroconf service
        if self._dacp_info:
            await self.mass.discovery.aiozc.async_unregister_service(self._dacp_info)

    async def _setup_player(
        self,
        player_id: str,
        display_name: str,
        discovery_info: AsyncServiceInfo,
        discovery_infos: tuple[AsyncServiceInfo, ...] = (),
    ) -> None:
        """Handle setup of a new player that is discovered using mdns."""
        # return early if player is disabled in config
        if not self.mass.config.get_raw_player_config_value(player_id, CONF_ENABLED, True):
            self.logger.debug("Ignoring %s in discovery as it is disabled.", display_name)
            return

        if discovery_infos:
            raop_discovery_info = next(
                (info for info in discovery_infos if info.type == RAOP_DISCOVERY_TYPE),
                None,
            )
            airplay_discovery_info = next(
                (info for info in discovery_infos if info.type == AIRPLAY_DISCOVERY_TYPE),
                None,
            )
            if raop_discovery_info:
                self.logger.debug("Discovered RAOP service for %s", display_name)
            if airplay_discovery_info:
                self.logger.debug("Discovered AirPlay service for %s", display_name)
        elif discovery_info.type == RAOP_DISCOVERY_TYPE:
            # RAOP service discovered - try to also find the AirPlay service
            raop_discovery_info = discovery_info
            self.logger.debug("Discovered RAOP service for %s", display_name)
            airplay_discovery_info = await self.mass.discovery.async_find_mdns_service(
                AIRPLAY_DISCOVERY_TYPE, display_name, timeout=10.0
            )
        else:
            # AirPlay service discovered - try to also find the RAOP service
            self.logger.debug("Discovered AirPlay service for %s", display_name)
            airplay_discovery_info = discovery_info
            raop_discovery_info = await self.mass.discovery.async_find_mdns_service(
                RAOP_DISCOVERY_TYPE, display_name, timeout=10.0
            )

        if airplay_discovery_info:
            manufacturer, model = get_model_info(airplay_discovery_info)
        elif raop_discovery_info:
            manufacturer, model = get_model_info(raop_discovery_info)
        else:
            return  # should not happen, but guard just in case

        prefer_ipv6 = ":" in str(self.mass.streams.publish_ip)
        primary_discovery_info = airplay_discovery_info or raop_discovery_info or discovery_info
        address = get_primary_ip_address_from_zeroconf(
            primary_discovery_info, prefer_ipv6=prefer_ipv6
        )
        if not address:
            return  # should not happen, but guard just in case

        # Filter out shairport-sync instances running on THIS Music Assistant server
        # These are managed by the AirPlay Receiver provider, not the AirPlay provider
        # We check both model name AND that it's a local address to avoid filtering
        # shairport-sync instances running on other machines
        if model == "ShairportSync":
            # Check if this is a local address (loopback or matches our server's IP)
            if ip_address(address).is_loopback or address == self.mass.streams.publish_ip:
                # Only filter if the port matches one of MA's own AirPlay Receiver instances.
                # This allows user-configured shairport-sync instances on the same machine
                # to be used as AirPlay players (e.g., multiple audio outputs via shairport-sync).
                receiver_ports = {
                    port
                    for prov in self.mass.get_provider_instances("airplay_receiver")
                    if (port := getattr(prov, "airplay_port", None)) is not None
                }
                discovered_ports = {
                    info.port
                    for info in (raop_discovery_info, airplay_discovery_info)
                    if info is not None
                }
                if discovered_ports.intersection(receiver_ports):
                    return

        # if we reach this point, all preflights are ok and we can create the player
        self.logger.debug("Discovered AirPlay device %s on %s", display_name, address)

        # Get stored volume from playerconfig
        volume = int(
            self.mass.config.get_raw_player_config_value(
                player_id, CONF_STORED_VOLUME, FALLBACK_VOLUME
            )
        )

        # Final check before registration to handle race conditions
        # (multiple MDNS events processed in parallel for same device)
        if self.mass.players.get_player(player_id):
            self.logger.debug(
                "Player %s already registered during setup, skipping registration", player_id
            )
            return

        self.logger.debug(
            "Setting up player %s: manufacturer=%s, model=%s",
            display_name,
            manufacturer,
            model,
        )

        # Create single AirPlayPlayer for all devices
        # Pairing config entries will be shown conditionally based on device type
        player = AirPlayPlayer(
            provider=self,
            player_id=player_id,
            raop_discovery_info=raop_discovery_info,
            airplay_discovery_info=airplay_discovery_info,
            address=address,
            display_name=display_name,
            manufacturer=manufacturer,
            model=model,
            initial_volume=volume,
        )
        await self.mass.players.register(player)

        # Set up Sendspin bridge for protocol linking (if Sendspin provider is available)
        await self._bridge_manager.setup_bridge(player)

    async def _setup_manual_players(self) -> None:
        """Set up manually configured AirPlay players."""
        for address in self._manual_ip_config:
            try:
                discovery = await self._probe_manual_airplay_device(address)
            except Exception as err:
                self.logger.warning(
                    "Unexpected error probing manual AirPlay device %s: %s",
                    address,
                    err,
                    exc_info=err,
                )
                continue
            if discovery is None:
                self.logger.debug(
                    "Ignoring manual AirPlay device %s: no AirPlay/RAOP info found",
                    address,
                )
                continue
            player_id = f"ap{discovery.device_id.lower()}"
            if player := cast("AirPlayPlayer | None", self.mass.players.get_player(player_id)):
                for service_info in discovery.service_infos:
                    player.set_discovery_info(service_info, discovery.display_name)
                continue
            await self._setup_player(
                player_id,
                discovery.display_name,
                discovery.service_infos[0],
                discovery_infos=discovery.service_infos,
            )

    async def _probe_manual_airplay_device(self, address: str) -> ManualAirPlayDiscovery | None:
        """Probe a manually configured host for AirPlay/RAOP /info metadata."""
        try:
            host = _normalize_manual_airplay_host(address)
        except ValueError as err:
            self.logger.warning("Ignoring invalid manual AirPlay address %s: %s", address, err)
            return None

        resolved_addresses = await self._resolve_manual_airplay_addresses(host)
        if not resolved_addresses:
            return None

        for resolved_address in resolved_addresses:
            service_infos: list[AsyncServiceInfo] = []
            display_name: str | None = None
            device_id: str | None = None

            for port in (DEFAULT_AIRPLAY_PORT, DEFAULT_RAOP_PORT):
                parsed_info = await self._request_airplay_info(resolved_address, port)
                if parsed_info is None:
                    continue
                parsed_device_id = _device_id_from_airplay_info(parsed_info)
                if parsed_device_id is None:
                    self.logger.debug(
                        "Manual AirPlay device %s:%s did not report a stable device ID",
                        resolved_address,
                        port,
                    )
                    continue
                if device_id and parsed_device_id != device_id:
                    self.logger.debug(
                        "Ignoring AirPlay info from %s:%s with mismatched device ID %s",
                        resolved_address,
                        port,
                        parsed_device_id,
                    )
                    continue
                device_id = parsed_device_id
                display_name = _display_name_from_airplay_info(parsed_info, address.strip())
                discovery_info = await self._create_manual_service_info(
                    resolved_address,
                    port,
                    parsed_info,
                    display_name,
                    device_id,
                    RAOP_DISCOVERY_TYPE if port == DEFAULT_RAOP_PORT else AIRPLAY_DISCOVERY_TYPE,
                )
                service_infos.append(discovery_info)

            if device_id and service_infos:
                return ManualAirPlayDiscovery(
                    display_name=display_name or address.strip(),
                    device_id=device_id,
                    service_infos=tuple(service_infos),
                )
        return None

    async def _resolve_manual_airplay_addresses(self, host: str) -> list[str]:
        """Resolve a manual AirPlay target to concrete IP addresses."""
        try:
            parsed_target_ip = ip_address(host)
        except ValueError:
            pass
        else:
            if parsed_target_ip.is_unspecified:
                return []
            return [str(parsed_target_ip)]

        try:
            addr_infos = await self.mass.loop.getaddrinfo(
                host,
                DEFAULT_AIRPLAY_PORT,
                type=socket.SOCK_STREAM,
            )
        except OSError as err:
            self.logger.debug("Failed to resolve manual AirPlay host %s: %s", host, err)
            return []

        addresses: list[str] = []
        for _family, _type, _proto, _canonname, sockaddr in addr_infos:
            resolved_address = str(sockaddr[0])
            if "%" in resolved_address:
                resolved_address = resolved_address.split("%", 1)[0]
            with suppress(ValueError):
                parsed_ip = ip_address(resolved_address)
                if parsed_ip.is_unspecified:
                    continue
            if resolved_address not in addresses:
                addresses.append(resolved_address)
        return addresses

    async def _request_airplay_info(self, address: str, port: int) -> dict[str, object] | None:
        """Request and parse AirPlay /info metadata from a host/port."""
        for protocol in ("RTSP/1.0", "HTTP/1.1"):
            parsed = await self._request_airplay_info_once(address, port, protocol)
            if parsed is not None:
                return parsed
        return None

    async def _request_airplay_info_once(
        self, address: str, port: int, protocol: str
    ) -> dict[str, object] | None:
        """Request AirPlay /info once using a specific HTTP/RTSP protocol string."""
        response: bytes = b""
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port),
                timeout=MANUAL_DISCOVERY_TIMEOUT,
            )
            host_header = f"{format_ip_for_url(address)}:{port}"
            dacp_id = getattr(self, "dacp_id", self.mass.server_id[:16].upper())
            request = (
                f"GET /info {protocol}\r\n"
                f"Host: {host_header}\r\n"
                "CSeq: 1\r\n"
                f"DACP-ID: {dacp_id}\r\n"
                f"Active-Remote: {dacp_id}\r\n"
                "User-Agent: Music Assistant\r\n"
                "Connection: close\r\n\r\n"
            )
            if writer is None:
                return None
            writer.write(request.encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=MANUAL_DISCOVERY_TIMEOUT)
        except (OSError, TimeoutError):
            return None
        finally:
            if writer is not None:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
        return _parse_airplay_info_response(response)

    async def _create_manual_service_info(
        self,
        address: str,
        port: int,
        info: Mapping[str, object],
        display_name: str,
        device_id: str,
        service_type: str,
    ) -> AsyncServiceInfo:
        """Create synthetic Zeroconf service info from manual AirPlay metadata."""
        properties = _airplay_info_to_txt_properties(info)
        formatted_device_id = ":".join(device_id[i : i + 2] for i in range(0, 12, 2))
        properties["deviceid"] = formatted_device_id
        if service_type == RAOP_DISCOVERY_TYPE:
            service_name = f"{device_id}@{display_name}.{service_type}"
        else:
            service_name = f"{display_name}.{service_type}"

        return AsyncServiceInfo(
            service_type,
            name=service_name,
            addresses=[await get_ip_pton(address)],
            port=port,
            properties=properties,
            server=f"{display_name.replace(' ', '-')}.local.",
        )

    async def _handle_dacp_request(  # noqa: PLR0915
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle new connection on the socket."""
        try:
            raw_request = b""
            while recv := await reader.read(1024):
                raw_request += recv
                if len(recv) < 1024:
                    break
            if not raw_request:
                # Some device (Phorus PS10) seems to send empty request
                # Maybe as a ack message? we have nothing to do here with empty request
                # so we return early.
                return

            request = raw_request.decode("UTF-8")
            if "\r\n\r\n" in request:
                headers_raw, body = request.split("\r\n\r\n", 1)
            else:
                headers_raw = request
                body = ""
            headers_split = headers_raw.split("\r\n")
            headers = {}
            for line in headers_split[1:]:
                if ":" not in line:
                    continue
                x, y = line.split(":", 1)
                headers[x.strip()] = y.strip()
            active_remote = headers.get("Active-Remote")
            _, path, _ = headers_split[0].split(" ")
            # lookup airplay player by active remote id
            player: AirPlayPlayer | None = next(
                (
                    x
                    for x in self.get_players()
                    if x.stream and x.stream.active_remote_id == active_remote
                ),
                None,
            )
            self.logger.debug(
                "DACP request for %s (%s): %s -- %s",
                player.name if player else "UNKNOWN PLAYER",
                active_remote,
                path,
                body,
            )
            if not player:
                return
            if player.protocol_parent_id and (
                parent := self.mass.players.get_player(player.protocol_parent_id)
            ):
                parent_player = parent
            else:
                parent_player = player

            player_id = player.player_id
            ignore_volume_report = (
                self.mass.config.get_raw_player_config_value(player_id, CONF_IGNORE_VOLUME, False)
                or player.device_info.manufacturer.lower() == "apple"
            )
            if path == "/ctrl-int/1/nextitem":
                self.mass.create_task(self.mass.players.cmd_next_track(player_id))
            elif path == "/ctrl-int/1/previtem":
                self.mass.create_task(self.mass.players.cmd_previous_track(player_id))
            elif path == "/ctrl-int/1/play":
                # sometimes this request is sent by a device as confirmation of a play command
                # we ignore this if the player is already playing
                if player.playback_state != PlaybackState.PLAYING:
                    self.mass.create_task(self.mass.players.cmd_play(player_id))
            elif path == "/ctrl-int/1/playpause":
                self.mass.create_task(self.mass.players.cmd_play_pause(player_id))
            elif path == "/ctrl-int/1/stop":
                self.mass.create_task(self.mass.players.cmd_stop(player_id))
            elif path == "/ctrl-int/1/volumeup":
                self.mass.create_task(self.mass.players.cmd_volume_up(player_id))
            elif path == "/ctrl-int/1/volumedown":
                self.mass.create_task(self.mass.players.cmd_volume_down(player_id))
            elif path == "/ctrl-int/1/shuffle_songs":
                active_queue = self.mass.players.get_active_queue(player)
                if not active_queue:
                    return
                await self.mass.player_queues.set_shuffle(
                    active_queue.queue_id, not active_queue.shuffle_enabled
                )
            elif path == "/ctrl-int/1/pause":
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.mass.create_task(self.mass.players.cmd_pause(player_id))
            elif path == "/ctrl-int/1/discrete-pause":
                # Some devices send discrete-pause right before device-prevent-playback=1
                # when switching to another source. We debounce the pause to avoid
                # unnecessary pause commands that would interfere with source switching
                # so we only process the pause command if we don't receive a
                # prevent-playback=1 within a short time window.
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.mass.call_later(
                        1.0,
                        self.mass.players.cmd_pause,
                        player_id,
                        task_id=f"debounced_pause_{player_id}",
                    )
            elif "dmcp.device-volume=" in path and not ignore_volume_report:
                # This is a bit annoying as this can be either the device confirming a new volume
                # we've sent or the device requesting a new volume itself.
                # In case of a small rounding difference, we ignore this,
                # to prevent an endless pingpong of volume changes
                airplay_volume = float(path.split("dmcp.device-volume=", 1)[-1])
                if airplay_volume <= AIRPLAY_VOLUME_MUTE:
                    player._attr_volume_muted = True
                    if player.stream and player.stream.running:
                        self.mass.create_task(player.stream.send_cli_command("VOLUME=0"))
                    player.update_state()
                else:
                    if player.volume_muted:
                        player._attr_volume_muted = False
                        if player.stream and player.stream.running:
                            self.mass.create_task(
                                player.stream.send_cli_command(f"VOLUME={player.volume_level or 0}")
                            )
                    volume = convert_airplay_volume(airplay_volume)
                    player.update_volume_from_device(volume)
            elif "dmcp.volume=" in path:
                # volume change request from device (e.g. volume buttons)
                volume = int(path.split("dmcp.volume=", 1)[-1])
                player.update_volume_from_device(volume)
            elif "device-prevent-playback=1" in path:
                # device switched to another source (or is powered off)
                # Cancel any pending debounced pause since prevent-playback takes precedence
                self.mass.cancel_timer(f"debounced_pause_{player_id}")
                # Ignore during stream transition (stale message from old CLI process)
                if player._transitioning or not player.stream:
                    self.logger.debug("Ignoring prevent-playback during stream transition")
                elif player.stream.prevent_playback:
                    # Already handling a prevent-playback for this stream
                    # (duplicate message while ungroup/stop is still in progress)
                    self.logger.debug("Ignoring duplicate prevent-playback for %s", player.name)
                else:
                    player.stream.prevent_playback = True
                    if player.stream.session:
                        # Some devices (e.g. Denon AVR) emit a transient
                        # prevent-playback=1/=0 pair during RAOP connection setup;
                        # debounce so a quick =0 cancels the action.
                        scheduled_stream = player.stream

                        def _act_on_prevent_playback() -> None:
                            # bail out if the stream was swapped out during the debounce
                            # window or its prevent_playback flag was already cleared
                            if (
                                player.stream is not scheduled_stream
                                or not scheduled_stream.prevent_playback
                            ):
                                return
                            self.logger.debug(
                                "Prevent playback command detected for player %s",
                                player.name,
                            )
                            if player.synced_to or parent_player.state.active_group:
                                self.mass.create_task(
                                    self.mass.players.cmd_ungroup(parent_player.player_id)
                                )
                            else:
                                self.mass.create_task(scheduled_stream.stop())

                        self.mass.call_later(
                            1.0,
                            _act_on_prevent_playback,
                            task_id=f"prevent_playback_{player_id}",
                        )
            elif "device-prevent-playback=0" in path:
                # device reports that its ready for playback again
                # use a debounced reset to avoid race conditions where a quick
                # prevent-playback=0 between duplicate prevent-playback=1 messages
                # would reset the flag and allow the second message to act
                # Cancel any pending prevent-playback action (transient =1/=0 pair).
                self.mass.cancel_timer(f"prevent_playback_{player_id}")
                if (stream := player.stream) and stream.prevent_playback:
                    self.mass.call_later(
                        5,
                        setattr,
                        stream,
                        "prevent_playback",
                        False,
                        task_id=f"reset_prevent_playback_{player_id}",
                    )

            # send response
            date_str = utc().strftime("%a, %-d %b %Y %H:%M:%S")
            response = (
                f"HTTP/1.0 204 No Content\r\nDate: {date_str} "
                "GMT\r\nDAAP-Server: iTunes/7.6.2 (Windows; N;)\r\nContent-Type: "
                "application/x-dmap-tagged\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(response.encode())
            await writer.drain()
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    def get_players(self) -> list[AirPlayPlayer]:
        """Return all airplay players belonging to this instance."""
        return cast("list[AirPlayPlayer]", self.players)

    def get_player(self, player_id: str) -> AirPlayPlayer | None:
        """Return AirplayPlayer by id."""
        return cast("AirPlayPlayer | None", self.mass.players.get_player(player_id))
