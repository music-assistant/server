"""Discovery SSDP listener."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from async_upnp_client.const import SsdpSource
from async_upnp_client.ssdp_listener import SsdpDevice, SsdpListener

from .consts import SSDP_ST
from .models import DiscoveryEventType, DiscoveryInfo, DiscoverySource


class DiscoverySsdpListener:
    """Encapsulates SSDP discovery logic."""

    def __init__(
        self, callback: Callable[[DiscoveryInfo], Awaitable[None]], logger: logging.Logger
    ) -> None:
        """Initialize the SSDP listener.

        :param callback: The async function to call when an event occurs.
        :param logger: The logger to use.
        """
        self._callback = callback
        self.logger = logger
        self._ssdp_listener: SsdpListener | None = None

    async def start(self) -> None:
        """Start the SSDP listener and trigger an initial search."""
        self._ssdp_listener = SsdpListener(
            async_callback=self._on_ssdp_response, search_target=SSDP_ST
        )
        await self._ssdp_listener.async_start()
        await self._ssdp_listener.async_search()

    async def stop(self) -> None:
        """Stop the SSDP passive listener and clean up resources."""
        if self._ssdp_listener:
            await self._ssdp_listener.async_stop()

    async def _on_ssdp_response(
        self, device: SsdpDevice, service_type: str, source: SsdpSource
    ) -> None:
        """Handle an incoming SSDP response.

        :param device: The discovered SSDP device.
        :param service_type: The service type advertised.
        :param source: The source of the event.
        """
        if service_type != SSDP_ST or not device.udn:
            return

        if source == SsdpSource.ADVERTISEMENT_BYEBYE:
            info = DiscoveryInfo(
                udn=device.udn,
                ip_address="",
                event_type=DiscoveryEventType.OFFLINE,
                discovery_source=DiscoverySource.SSDP,
            )
            await self._callback(info)
        elif device.location:
            # We must pass this to the handler to perform the IP probe,
            # so we map it as a presence event containing the location URI.
            ip_address = urlparse(device.location).hostname
            if ip_address:
                info = DiscoveryInfo(
                    udn=device.udn,
                    ip_address=ip_address,
                    event_type=DiscoveryEventType.PRESENCE,
                    discovery_source=DiscoverySource.SSDP,
                )
                await self._callback(info)
