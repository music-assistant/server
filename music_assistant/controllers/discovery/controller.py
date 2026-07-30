"""Discovery core controller."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
from collections import defaultdict
from ipaddress import AddressValueError, IPv4Address
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from zeroconf import (
    NonUniqueNameException,
    ServiceStateChange,
    Zeroconf,
)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from music_assistant.constants import (
    CONF_ENTRY_ZEROCONF_INTERFACES,
    CONF_ZEROCONF_INTERFACES,
    INGRESS_SERVER_PORT,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.util import get_ip_pton, get_zeroconf_args
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from async_upnp_client.utils import CaseInsensitiveDict
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant.models import ProviderInstanceType

# RAOP cache keys prefix the device name with the device MAC, e.g. "aabbccddeeff@Kelder".
# Cache keys are lowercased, so the hex is matched in lowercase.
RAOP_MAC_PREFIX = re.compile(r"^[0-9a-f]{12}@")

CONF_UPNP_NETWORK_SCAN = "upnp_network_scan"
UPNP_DISCOVERY_INTERVAL = 300
SSDP_PORT = 1900
UPNP_DISCOVERY_BROADCAST_TARGET = (str(IPv4Address("255.255.255.255")), SSDP_PORT)
UPNP_DISCOVERY_TASK_ID = "discovery_upnp_cycle"
UPNP_DISCOVERY_TIMER_ID = "discovery_upnp_timer"

# Re-announce daily so the HA integration token rotates before expiry, also on long uptimes
HA_ANNOUNCE_INTERVAL = 86400
HA_ANNOUNCE_TIMER_ID = "discovery_ha_announce_timer"


async def async_upnp_search(*args: Any, **kwargs: Any) -> None:
    """Run async_upnp_client SSDP search with lazy import."""
    from async_upnp_client.search import async_search  # noqa: PLC0415

    await async_search(*args, **kwargs)


class DiscoveryController(CoreController):
    """Core controller that manages mDNS/Zeroconf and SSDP/UPnP discovery."""

    domain = "discovery"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize discovery controller."""
        super().__init__(*args, **kwargs)
        self.manifest.name = "Discovery"
        self.manifest.description = (
            "Handles mDNS/Zeroconf, SSDP/UPnP discovery and Music Assistant network broadcast."
        )
        self.manifest.icon = "radar"
        self._aiozc: AsyncZeroconf | None = None
        self._mdns_browser: AsyncServiceBrowser | None = None
        self._mass_service_info: AsyncServiceInfo | None = None
        self._mdns_locks: dict[str, asyncio.Lock] = {}
        self._upnp_locks: dict[str, asyncio.Lock] = {}
        self._upnp_run_lock = asyncio.Lock()
        self._mdns_waiters: list[asyncio.Event] = []

    @property
    def aiozc(self) -> AsyncZeroconf:
        """Return the shared AsyncZeroconf instance for discovery consumers."""
        assert self._aiozc is not None, "DiscoveryController is not initialized"
        return self._aiozc

    async def setup(self, config: CoreConfig) -> None:
        """Initialize discovery controller."""
        self.config = config
        if self._aiozc is None:
            self._aiozc = self._create_aiozc(config)
        self._configure_library_loggers()
        await self._setup_mdns_browser()
        await self._register_mass_service()
        if self.mass.running_as_hass_addon:
            # (re)announce to HA supervisor to make sure that HA picks it up
            await self._announce_to_homeassistant()
            self._schedule_periodic_ha_announce()
        self._schedule_periodic_upnp_discovery()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return config entries for the discovery controller."""
        return (
            CONF_ENTRY_ZEROCONF_INTERFACES,
            ConfigEntry(
                key=CONF_UPNP_NETWORK_SCAN,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                requires_reload=False,
            ),
        )

    async def close(self) -> None:
        """Handle logic on server stop."""
        self.mass.cancel_timer(UPNP_DISCOVERY_TIMER_ID)
        self.mass.cancel_task(UPNP_DISCOVERY_TASK_ID)
        self.mass.cancel_timer(HA_ANNOUNCE_TIMER_ID)

        await self._cancel_mdns_browser()

        aiozc = self._aiozc
        if self._mass_service_info:
            with contextlib.suppress(Exception):
                assert aiozc is not None
                await aiozc.async_unregister_service(self._mass_service_info)
            self._mass_service_info = None

        self._mdns_locks.clear()
        self._upnp_locks.clear()
        if aiozc is not None:
            with contextlib.suppress(Exception):
                await aiozc.async_close()
            self._aiozc = None

    async def run_provider_discovery(self, provider: ProviderInstanceType) -> None:
        """Run discovery for a specific provider."""
        if provider.manifest.mdns_discovery:
            await self._replay_mdns_discovery(provider)
        if provider.manifest.upnp_discovery:
            await self._run_upnp_discovery_cycle(set(provider.manifest.upnp_discovery))
        self._schedule_periodic_upnp_discovery()

    def on_provider_unload(self, instance_id: str) -> None:
        """Clean up provider-specific discovery state."""
        self._mdns_locks.pop(instance_id, None)
        self._upnp_locks.pop(instance_id, None)
        self._schedule_periodic_upnp_discovery()

    async def async_find_mdns_service(
        self, service_type: str, name_filter: str, timeout: float = 3.0
    ) -> AsyncServiceInfo | None:
        """
        Find an mDNS service by exact device name match, checking cache first then waiting.

        :param service_type: The mDNS service type (e.g., "_raop._tcp.local.").
        :param name_filter: Device name that must exactly match the service name portion.
        :param timeout: Maximum time to wait in seconds.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        # Cache keys are lowercased DNS names, so we must match case-insensitively
        name_filter_lower = name_filter.lower()
        service_type_lower = service_type.lower()
        event = asyncio.Event()
        self._mdns_waiters.append(event)
        try:
            while True:
                # Clear before scanning so events arriving during the scan are not lost
                event.clear()
                # Check cache for a matching entry
                for mdns_name in set(self.aiozc.zeroconf.cache.cache):
                    if service_type_lower not in mdns_name or mdns_name == service_type_lower:
                        continue
                    # Use exact matching on the device name portion to prevent a device named
                    # "Foo" from cross-matching another device named "ATV Foo".
                    # mDNS names are either "MAC@DeviceName.service.local." or "DeviceName.service.local."
                    # Strip the MAC prefix only when present, so device names that legitimately
                    # contain "@" are not truncated.
                    device_part = mdns_name.split(".")[0]
                    device_name = RAOP_MAC_PREFIX.sub("", device_part, count=1)
                    if device_name != name_filter_lower:
                        continue
                    info = AsyncServiceInfo(service_type, mdns_name)
                    if await info.async_request(self.aiozc.zeroconf, 3000):
                        return info
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                # Wait for the next mDNS state change event, then re-check the cache
                try:
                    await asyncio.wait_for(event.wait(), timeout=remaining)
                except TimeoutError:
                    return None
        finally:
            self._mdns_waiters.remove(event)

    def _configure_library_loggers(self) -> None:
        """Align third-party discovery logging with the discovery controller log level."""
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("async_upnp_client").setLevel(logging.DEBUG)
        else:
            logging.getLogger("async_upnp_client").setLevel(self.logger.level + 10)

    def _create_aiozc(self, config: CoreConfig) -> AsyncZeroconf:
        """Create the shared AsyncZeroconf instance for the discovery controller."""
        zeroconf_interfaces = str(config.get_value(CONF_ZEROCONF_INTERFACES, "default"))
        use_all_interfaces = zeroconf_interfaces == "all"
        zc_args = get_zeroconf_args(use_all_interfaces)
        self.logger.debug("Zeroconf configuration: %s", zc_args)
        return AsyncZeroconf(
            ip_version=zc_args["ip_version"],
            interfaces=zc_args["interfaces"],
        )

    async def _setup_mdns_browser(self) -> None:
        """Create the global mDNS browser for all subscribed provider types."""
        await self._cancel_mdns_browser()

        all_types: set[str] = set()
        for manifest in self.mass.get_provider_manifests():
            if manifest.mdns_discovery:
                all_types.update(manifest.mdns_discovery)

        if not all_types:
            return

        self._mdns_browser = AsyncServiceBrowser(
            self.aiozc.zeroconf,
            list(all_types),
            handlers=[self._on_mdns_service_state_change],
        )

    async def _cancel_mdns_browser(self) -> None:
        """Cancel the active mDNS browser if one is running."""
        if not self._mdns_browser:
            return

        async_cancel = getattr(self._mdns_browser, "async_cancel", None)
        cancel = getattr(self._mdns_browser, "cancel", None)
        if callable(async_cancel):
            result = async_cancel()
            if inspect.isawaitable(result):
                await result
            elif callable(cancel):
                cancel()
        elif callable(cancel):
            cancel()
        self._mdns_browser = None

    async def _register_mass_service(self) -> None:
        """Register the Music Assistant server on the network via Zeroconf."""
        zeroconf_type = "_mass._tcp.local."
        server_id = self.mass.server_id
        self.logger.debug("Starting Zeroconf broadcast...")
        info = AsyncServiceInfo(
            zeroconf_type,
            name=f"{server_id}.{zeroconf_type}",
            addresses=[
                await get_ip_pton(address) for address in self.mass.webserver.publish_addresses
            ],
            port=self.mass.webserver.publish_port,
            properties=self.mass.get_server_info().to_dict(),
            server="mass.local.",
        )
        try:
            if self._mass_service_info:
                await self.aiozc.async_update_service(info)
            else:
                await self.aiozc.async_register_service(info)
            self._mass_service_info = info
        except NonUniqueNameException:
            self.logger.error(
                "Music Assistant instance with identical name present in the local network!"
            )

    def _on_mdns_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Handle mDNS service state callbacks."""

        async def process_mdns_state_change(provider: ProviderInstanceType) -> None:
            lock = self._mdns_locks.setdefault(provider.instance_id, asyncio.Lock())
            if state_change == ServiceStateChange.Removed:
                info = None
            else:
                info = AsyncServiceInfo(service_type, name)
                await info.async_request(zeroconf, 3000)
            async with lock:
                await provider.on_mdns_service_state_change(name, state_change, info)

        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Service %s of type %s state changed: %s",
            name,
            service_type,
            state_change,
        )
        # Notify any waiters that a new mDNS event arrived
        for waiter in self._mdns_waiters:
            waiter.set()
        for provider in list(self.mass.providers):
            if not provider.available or not provider.manifest.mdns_discovery:
                continue
            if service_type in provider.manifest.mdns_discovery:
                self.mass.create_task(process_mdns_state_change(provider))

    async def _replay_mdns_discovery(self, provider: ProviderInstanceType) -> None:
        """Replay cached mDNS results for a provider after it loads."""
        lock = self._mdns_locks.setdefault(provider.instance_id, asyncio.Lock())
        async with lock:
            for mdns_type in provider.manifest.mdns_discovery or []:
                for mdns_name in set(self.aiozc.zeroconf.cache.cache):
                    if mdns_type not in mdns_name or mdns_type == mdns_name:
                        continue
                    info = AsyncServiceInfo(mdns_type, mdns_name)
                    if await info.async_request(self.aiozc.zeroconf, 3000):
                        await provider.on_mdns_service_state_change(
                            mdns_name, ServiceStateChange.Added, info
                        )

    def _schedule_periodic_upnp_discovery(self) -> None:
        """Ensure the periodic SSDP discovery cycle matches active subscriptions."""
        subscriptions, _ = self._get_upnp_subscriptions()
        if not subscriptions:
            self.mass.cancel_timer(UPNP_DISCOVERY_TIMER_ID)
            self.mass.cancel_task(UPNP_DISCOVERY_TASK_ID)
            return

        def run_discovery() -> None:
            self.mass.create_task(
                self._run_upnp_discovery_cycle(),
                task_id=UPNP_DISCOVERY_TASK_ID,
            )

        self.mass.call_later(
            UPNP_DISCOVERY_INTERVAL,
            run_discovery,
            task_id=UPNP_DISCOVERY_TIMER_ID,
        )

    def _get_upnp_subscriptions(
        self,
    ) -> tuple[dict[str, list[ProviderInstanceType]], set[str]]:
        """Return active SSDP subscriptions keyed by search target."""
        subscriptions: dict[str, list[ProviderInstanceType]] = defaultdict(list)
        broadcast_targets: set[str] = set()
        allow_network_scan = bool(self.config.get_value(CONF_UPNP_NETWORK_SCAN))
        for provider in list(self.mass.providers):
            if not provider.available or not provider.manifest.upnp_discovery:
                continue
            for search_target in provider.manifest.upnp_discovery:
                subscriptions[search_target].append(provider)
                if allow_network_scan:
                    broadcast_targets.add(search_target)
        return subscriptions, broadcast_targets

    async def _run_upnp_discovery_cycle(self, search_targets: set[str] | None = None) -> None:
        """Run one SSDP discovery cycle for all active subscriptions."""
        try:
            async with self._upnp_run_lock:
                subscriptions, broadcast_targets = self._get_upnp_subscriptions()
                if search_targets is not None:
                    subscriptions = {
                        search_target: providers
                        for search_target, providers in subscriptions.items()
                        if search_target in search_targets
                    }
                    broadcast_targets.intersection_update(search_targets)
                if not subscriptions:
                    return

                for search_target, providers in subscriptions.items():
                    seen_results: set[tuple[str | None, str | None, str | None]] = set()
                    await self._run_upnp_search(search_target, providers, seen_results)
                    if search_target in broadcast_targets:
                        await self._run_upnp_search(
                            search_target,
                            providers,
                            seen_results,
                            target=UPNP_DISCOVERY_BROADCAST_TARGET,
                        )
                    # Directed unicast searches for manually configured device addresses.
                    # Like multicast/broadcast above, responses dispatch to every provider
                    # subscribed to this search target, not just the address's owner.
                    valid_addresses: list[str] = []
                    for provider in providers:
                        for address in provider.upnp_manual_discovery_addresses:
                            try:
                                stripped = address.strip()
                                IPv4Address(stripped)
                            except AddressValueError, AttributeError:
                                self.logger.warning(
                                    "Ignoring invalid manual discovery address for %s: %s "
                                    "(only IPv4 addresses are supported)",
                                    provider.name,
                                    address,
                                )
                                continue
                            valid_addresses.append(stripped)
                    if valid_addresses:
                        # Deduplicate (order-preserving) so a repeated address is searched once,
                        # and run the searches concurrently instead of serially (each search
                        # blocks for the full SSDP MX timeout).
                        await asyncio.gather(
                            *(
                                self._run_upnp_search(
                                    search_target,
                                    providers,
                                    seen_results,
                                    target=(address, SSDP_PORT),
                                )
                                for address in dict.fromkeys(valid_addresses)
                            )
                        )
        finally:
            self._schedule_periodic_upnp_discovery()

    async def _run_upnp_search(
        self,
        search_target: str,
        providers: list[ProviderInstanceType],
        seen_results: set[tuple[str | None, str | None, str | None]],
        target: tuple[str, int] | None = None,
    ) -> None:
        """Run one SSDP search and dispatch matching responses to subscribed providers."""

        async def on_response(discovery_info: CaseInsensitiveDict) -> None:
            dedupe_key = (
                discovery_info.get("usn"),
                discovery_info.get("location"),
                discovery_info.get("_host"),
            )
            if dedupe_key in seen_results:
                return
            seen_results.add(dedupe_key)
            for provider in providers:
                if not provider.available:
                    continue
                await self._dispatch_upnp_discovery(provider, search_target, discovery_info)

        try:
            if target is None:
                await async_upnp_search(on_response, search_target=search_target)
            else:
                await async_upnp_search(on_response, search_target=search_target, target=target)
        except (OSError, ValueError) as err:
            target_label = f" via {target[0]}" if target else ""
            self.logger.warning(
                "UPnP discovery for %s failed%s: %s",
                search_target,
                target_label,
                err,
            )

    async def _dispatch_upnp_discovery(
        self,
        provider: ProviderInstanceType,
        search_target: str,
        discovery_info: CaseInsensitiveDict,
    ) -> None:
        """Dispatch one SSDP discovery result to a provider callback."""
        lock = self._upnp_locks.setdefault(provider.instance_id, asyncio.Lock())
        async with lock:
            try:
                await provider.on_upnp_service_discovered(search_target, discovery_info)
            except Exception as err:
                self.logger.warning(
                    "Error handling UPnP discovery for %s: %s",
                    provider.name,
                    err,
                    exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
                )

    async def _announce_to_homeassistant(self) -> None:
        """Announce Music Assistant Ingress server to Home Assistant via Supervisor API."""
        supervisor_token = os.environ["SUPERVISOR_TOKEN"]
        addon_hostname = os.environ["HOSTNAME"]
        ha_integration_token = await self.mass.webserver.auth.get_homeassistant_system_user_token()
        discovery_payload = {
            "service": "music_assistant",
            "config": {
                "host": addon_hostname,
                "port": INGRESS_SERVER_PORT,
                "auth_token": ha_integration_token,
            },
        }
        try:
            async with self.mass.http_session_no_ssl.post(
                "http://supervisor/discovery",
                headers={"Authorization": f"Bearer {supervisor_token}"},
                json=discovery_payload,
                timeout=ClientTimeout(total=10),
            ) as response:
                response.raise_for_status()
                result = await response.json()
                self.logger.debug(
                    "Successfully announced to Home Assistant. Discovery UUID: %s",
                    result.get("uuid"),
                )
        except Exception as err:
            self.logger.warning("Failed to announce to Home Assistant: %s", err)

    def _schedule_periodic_ha_announce(self) -> None:
        """Schedule the periodic (re)announce to Home Assistant."""

        def run_announce() -> None:
            self.mass.create_task(self._announce_to_homeassistant())
            self._schedule_periodic_ha_announce()

        self.mass.call_later(
            HA_ANNOUNCE_INTERVAL,
            run_announce,
            task_id=HA_ANNOUNCE_TIMER_ID,
        )
