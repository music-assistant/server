"""
Yandex Quasar — cloud API for device discovery and fallback commands.

Adapted from AlexxIT/YandexStation (MIT license).
Stripped of Home Assistant dependencies; uses pure aiohttp.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .constants import QUASAR_DEVICES_URL

if TYPE_CHECKING:
    from .session import YandexSession

_LOGGER = logging.getLogger(__name__)


class YandexQuasar:
    """Yandex Quasar cloud API client for device discovery."""

    def __init__(self, session: YandexSession) -> None:
        """Initialize with an authenticated session."""
        self.session = session
        # Cached bulk device list, populated on first ``get_devices`` call
        # and read by external callers (e.g. ``provider.py`` for cloud-side
        # metadata enrichment).  Per-instance so multiple ``YandexQuasar``
        # objects (across reload cycles or parallel tests) don't leak
        # stale state via a shared class attribute.
        self.devices: list[dict[str, Any]] | None = None

    async def get_devices(self) -> list[dict[str, Any]]:
        """Fetch all devices from Quasar IoT API."""
        _LOGGER.debug("Fetching device list from Quasar")
        # ``async with`` releases the connector slot back to aiohttp's
        # pool when the body is fully read.  Without it, repeated
        # discovery retries (or other long-lived providers calling here)
        # could exhaust the connection pool over time.
        async with await self.session.get(QUASAR_DEVICES_URL, timeout=15) as r:
            resp = await r.json()
        if resp.get("status") != "ok":
            msg = f"Failed to get devices: {resp}"
            raise RuntimeError(msg)

        devices: list[dict[str, Any]] = []
        for house in resp.get("households", []):
            devices.extend(
                {**device, "house_name": house.get("name", "")} for device in house.get("all", [])
            )

        self.devices = devices
        return devices

    async def get_speakers(self) -> list[dict[str, Any]]:
        """Get only speaker devices (those with quasar_info)."""
        devices = await self.get_devices()
        return [d for d in devices if "quasar_info" in d]

    async def load_device_config(self, device: dict[str, Any]) -> None:
        """Load device_id and platform from device config (not in main list)."""
        device_cloud_id = device.get("id")
        if not device_cloud_id:
            return
        async with await self.session.get(
            f"https://iot.quasar.yandex.ru/m/user/devices/{device_cloud_id}/configuration"
        ) as r:
            resp = await r.json()
        if resp.get("status") == "ok" and "quasar_info" in resp:
            device["quasar_info"] = resp["quasar_info"]

    async def get_local_speakers(self) -> list[dict[str, Any]]:
        """
        Get device list via Glagol device_list API (includes IP/port).

        Returns devices with keys: device_id, name, platform, host, port, glagol.
        Only returns devices that have networkInfo with IP addresses.
        """
        try:
            async with await self.session.get("https://quasar.yandex.net/glagol/device_list") as r:
                resp = await r.json()
            result: list[dict[str, Any]] = []
            for d in resp.get("devices", []):
                ni = d.get("networkInfo") or {}
                ips = ni.get("ip_addresses") or []
                if not ips:
                    continue
                result.append(
                    {
                        "device_id": d["id"],
                        "name": d.get("name", ""),
                        "platform": d.get("platform", ""),
                        "host": ips[0],
                        "port": ni.get("external_port", 1961),
                        "glagol": d.get("glagol", {}),
                    }
                )
            return result
        except Exception:
            _LOGGER.exception("Failed to load local speakers")
            return []
