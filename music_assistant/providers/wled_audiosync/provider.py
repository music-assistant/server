"""WLED Audio Sync Plugin Provider implementation."""

from __future__ import annotations

import re
from contextlib import suppress
from ipaddress import AddressValueError, IPv4Address
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from zeroconf import ServiceStateChange

from music_assistant.helpers.util import get_primary_ip_address_from_zeroconf
from music_assistant.models.plugin import PluginProvider

from .bridge import WledAudioSyncBridge
from .constants import (
    CONF_DUPLICATE_TRANSMIT,
    CONF_MANUAL_PLAYERS,
    CONF_MULTICAST_TTL,
    CONF_REQUIRE_AUDIOREACTIVE,
    DEFAULT_DUPLICATE_TRANSMIT,
    DEFAULT_REQUIRE_AUDIOREACTIVE,
    JSON_INFO_PROBE_TIMEOUT_S,
    WLED_AUDIOSYNC_DEFAULT_PORT,
)
from .wled_audiosync_bridge import DEFAULT_MULTICAST_TTL

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


_MANUAL_BRIDGE_ID_PREFIX = "wled_manual_"
_MDNS_BRIDGE_ID_PREFIX = "wled_"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_AUDIOREACTIVE_USERMOD_KEY = "AudioReactive"


def _slugify(value: str) -> str:
    """Lowercase a string and reduce non-alphanumerics to single underscores."""
    return _SLUG_RE.sub("_", value.lower()).strip("_")


def _is_multicast(address: str) -> bool:
    """Return True if the address is an IPv4 multicast group."""
    try:
        return IPv4Address(address).is_multicast
    except (AddressValueError, ValueError):
        return False


def info_has_audioreactive(info: dict[str, Any]) -> bool:
    """
    Return True if a /json/info response advertises the AudioReactive usermod.

    :param info: The parsed JSON object returned by GET /json/info.
    """
    # The `u` (usermods) dict gains an "AudioReactive" key whenever the
    # usermod is compiled into the WLED build, while `brand` stays "WLED"
    # on both upstream and MoonModules forks — so usermod presence is the
    # cleanest detection signal.
    usermods = info.get("u") or {}
    if not isinstance(usermods, dict):
        return False
    return _AUDIOREACTIVE_USERMOD_KEY in usermods


async def probe_audioreactive(
    session: aiohttp.ClientSession,
    address: str,
    timeout: float = JSON_INFO_PROBE_TIMEOUT_S,
) -> bool:
    """
    Probe a WLED device's /json/info endpoint to detect AudioReactive support.

    Returns False on any error (timeout, connection refused, non-200 status,
    malformed JSON, missing keys). Callers should treat False as "skip" rather
    than "definitely incompatible" — a flapping LAN device may produce false
    negatives and can be re-tested on the next mDNS update event.

    :param session: A shared aiohttp.ClientSession.
    :param address: The IPv4 address of the WLED device.
    :param timeout: Request timeout in seconds.
    """
    url = f"http://{address}/json/info"
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with session.get(url, timeout=client_timeout) as resp:
            if resp.status != 200:
                return False
            try:
                data = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                return False
    except (TimeoutError, aiohttp.ClientError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    return info_has_audioreactive(data)


class WledAudioSyncProvider(PluginProvider):
    """
    Plugin provider that bridges WLED Audio Sync receivers to MA via Sendspin.

    Discovers WLED devices over mDNS, filters to those that report the
    AudioReactive usermod via their /json/info endpoint, and spawns one
    :class:`WledAudioSyncBridge` per device. Each bridge connects to MA's
    local Sendspin server as a VISUALIZER client and emits 44-byte V2 packets
    to the device at audible-playback time.

    Manually-configured destinations (broadcast / multicast endpoints that
    aren't visible to mDNS) get their own bridges alongside.
    """

    _bridges: dict[str, WledAudioSyncBridge]

    async def handle_async_init(self) -> None:
        """Initialise the bridge registry and register manually-configured destinations."""
        self._bridges = {}
        manual_entries = cast("list[str]", self.config.get_value(CONF_MANUAL_PLAYERS) or [])
        for entry in manual_entries:
            await self._register_manual_bridge(entry)

    async def unload(self, is_removed: bool = False) -> None:
        """Tear down every running bridge when the provider unloads."""
        for bridge in list(self._bridges.values()):
            with suppress(Exception):
                await bridge.stop()
        self._bridges.clear()

    @property
    def bridges(self) -> dict[str, WledAudioSyncBridge]:
        """Read-only view of currently-managed bridges keyed by client id."""
        return self._bridges

    async def _register_manual_bridge(self, entry: str) -> None:
        """Parse a 'name=address' config entry and start a bridge for it."""
        if "=" not in entry:
            self.logger.warning(
                "Ignoring manual WLED entry %r: expected 'name=address' format",
                entry,
            )
            return
        name, _, address = entry.partition("=")
        name = name.strip()
        address = address.strip()
        if not name or not address:
            self.logger.warning("Ignoring manual WLED entry %r: empty name or address", entry)
            return
        client_id = f"{_MANUAL_BRIDGE_ID_PREFIX}{_slugify(name)}"
        if client_id in self._bridges:
            self._bridges[client_id].set_destination(address, WLED_AUDIOSYNC_DEFAULT_PORT)
            return
        self.logger.info(
            "Registering manual WLED bridge %s -> %s:%d%s",
            name,
            address,
            WLED_AUDIOSYNC_DEFAULT_PORT,
            " (multicast)" if _is_multicast(address) else "",
        )
        bridge = self._build_bridge(
            client_id=client_id,
            name=name,
            address=address,
            port=WLED_AUDIOSYNC_DEFAULT_PORT,
        )
        await self._start_bridge(bridge)

    async def on_mdns_service_state_change(
        self,
        name: str,
        state_change: ServiceStateChange,
        info: AsyncServiceInfo | None,
    ) -> None:
        """Handle a WLED device appearing/updating/disappearing on the LAN."""
        # The service name for WLED looks like "wled-bedroom._wled._tcp.local."
        hostname = name.split(".", maxsplit=1)[0]
        if not hostname:
            return
        client_id = f"{_MDNS_BRIDGE_ID_PREFIX}{_slugify(hostname)}"

        if state_change == ServiceStateChange.Removed:
            bridge = self._bridges.pop(client_id, None)
            if bridge is not None:
                self.logger.debug("WLED bridge offline: %s", client_id)
                with suppress(Exception):
                    await bridge.stop()
            return

        if not info:
            return  # nothing to act on without the discovery info
        address = get_primary_ip_address_from_zeroconf(info)
        if not address:
            self.logger.debug("Skipping WLED discovery for %s: no usable IP address", hostname)
            return
        # `info.port` is the HTTP port WLED advertises for its UI/JSON API
        # (typically 80) — not the V2 audio-sync RX port (11988 by default).
        port = WLED_AUDIOSYNC_DEFAULT_PORT

        existing = self._bridges.get(client_id)
        if existing is not None:
            existing.set_destination(address, port)
            return

        if not await self._check_audioreactive(hostname, address):
            return

        display_name = hostname.replace("-", " ").replace("_", " ").title()
        self.logger.info("Discovered WLED %s at %s:%d", display_name, address, port)
        bridge = self._build_bridge(
            client_id=client_id,
            name=display_name,
            address=address,
            port=port,
        )
        await self._start_bridge(bridge)

    async def _check_audioreactive(self, hostname: str, address: str) -> bool:
        """
        Return True if this WLED should be bridged.

        When require_audioreactive is enabled (default), probes /json/info and
        returns False unless the AudioReactive usermod is present.
        When disabled, every discovered WLED is accepted.
        """
        require = bool(
            self.config.get_value(CONF_REQUIRE_AUDIOREACTIVE, DEFAULT_REQUIRE_AUDIOREACTIVE)
        )
        if not require:
            return True
        if await probe_audioreactive(self.mass.http_session, address):
            return True
        self.logger.debug(
            "Skipping %s (%s): /json/info did not report the AudioReactive usermod",
            hostname,
            address,
        )
        return False

    def _build_bridge(
        self, *, client_id: str, name: str, address: str, port: int
    ) -> WledAudioSyncBridge:
        """Construct (but do not start) a WledAudioSyncBridge with current provider config."""
        duplicate = bool(self.config.get_value(CONF_DUPLICATE_TRANSMIT, DEFAULT_DUPLICATE_TRANSMIT))
        multicast_ttl = int(
            cast("int", self.config.get_value(CONF_MULTICAST_TTL, DEFAULT_MULTICAST_TTL))
        )
        return WledAudioSyncBridge(
            provider=self,
            client_id=client_id,
            name=name,
            address=address,
            port=port,
            duplicate_transmit=duplicate,
            multicast_ttl=multicast_ttl,
        )

    async def _start_bridge(self, bridge: WledAudioSyncBridge) -> None:
        """Start a bridge and register it under its client id."""
        try:
            await bridge.start()
        except Exception:
            self.logger.exception(
                "Failed to start WLED bridge %s -> %s:%d",
                bridge.client_id,
                bridge.destination_address,
                bridge.destination_port,
            )
            with suppress(Exception):
                await bridge.stop()
            return
        self._bridges[bridge.client_id] = bridge
