"""Fakes for the Home Assistant side of the Hue Lights Sync ZHA backend."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

ZIGGURAT_URL = "ws://3ccd9485-ziggurat:9999"


def zha_device(
    ieee: str,
    name: str,
    *,
    nwk: int | str = "0x1a2b",
    area_id: str | None = None,
    user_given_name: str | None = None,
    manufacturer: str = "Signify Netherlands B.V.",
    endpoint: str = "11",
    clusters: tuple[str, ...] = ("0x0000", "0x0006", "0xfc01"),
    entities: tuple[str, ...] = ("light.hue_lamp",),
) -> dict[str, Any]:
    """Return a device as listed by ZHA's ``zha/devices`` command; a Hue colour lamp by default."""
    return {
        "ieee": ieee,
        "nwk": nwk,
        "name": name,
        "user_given_name": user_given_name,
        "manufacturer": manufacturer,
        "area_id": area_id,
        "signature": {"endpoints": {endpoint: {"input_clusters": list(clusters)}}},
        "entities": [{"entity_id": entity_id} for entity_id in entities],
    }


class FakeHass:
    """The Home Assistant plugin, answering the websocket commands the ZHA lookups send."""

    def __init__(
        self,
        devices: list[dict[str, Any]] | None = None,
        *,
        ziggurat_path: str = ZIGGURAT_URL,
        internal_url: str | None = "http://192.168.1.10:8123",
        url: str | None = "http://homeassistant.lan:8123",
        areas: dict[str, str] | None = None,
        connected: bool = True,
    ) -> None:
        """
        Initialize the fake.

        :param devices: The ZHA devices.
        :param ziggurat_path: ZHA's radio path.
        :param internal_url: Home Assistant's internal URL.
        :param url: The URL the plugin connects to.
        :param areas: Area name per area id.
        :param connected: Whether the plugin is connected.
        """
        self.url = url
        self.commands: dict[str, Any] = {
            "zha/devices": devices or [],
            "zha/network/settings": {
                "device": {"path": ziggurat_path},
                "network_key": {"key": "secret"},
            },
            "network/url": {"internal": internal_url, "external": None},
        }
        self.areas = {
            area_id: {"area_id": area_id, "name": name} for area_id, name in (areas or {}).items()
        }
        self.hass = SimpleNamespace(connected=connected, send_command=self.send_command)

    async def send_command(self, command: str) -> Any:
        """Answer a websocket command; an exception stored as the answer is raised."""
        result = self.commands[command]
        if isinstance(result, Exception):
            raise result
        return result

    async def get_area_registry(self) -> dict[str, Any]:
        """Return the area registry, keyed by area id."""
        return self.areas


class FakeMass:
    """The MusicAssistant surface the ZHA lookups use."""

    def __init__(
        self, hass: FakeHass | None = None, *, addon: bool = False, resolves: bool = True
    ) -> None:
        """
        Initialize the fake.

        :param hass: The loaded Home Assistant plugin, if any.
        :param addon: Whether Music Assistant runs as a Home Assistant add-on.
        :param resolves: Whether host names resolve.
        """
        self.hass = hass
        self.running_as_hass_addon = addon
        error = None if resolves else socket.gaierror(socket.EAI_NONAME, "not known")
        self.loop = SimpleNamespace(getaddrinfo=AsyncMock(return_value=[], side_effect=error))

    def get_provider(self, domain: str) -> FakeHass | None:
        """Return the Home Assistant plugin for its domain."""
        return self.hass if domain == "hass" else None
