"""
Home Assistant lookups for streaming to Hue lamps on ZHA via Ziggurat.

Uses the connection of the Home Assistant plugin, looked up on every call because a
reconnect reloads that plugin.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.hass import HomeAssistantProvider

# not imported from the plugin: that would pull in its requirements
HASS_DOMAIN: Final[str] = "hass"

_COMMAND_TIMEOUT_S: Final[float] = 10.0
_RESOLVE_TIMEOUT_S: Final[float] = 2.0
_HUE_MANUFACTURERS: Final[tuple[str, ...]] = ("signify", "philips")
# Hue's entertainment cluster on the light endpoint: only lamps with it can be streamed to
_ENTERTAINMENT_ENDPOINT: Final[str] = "11"
_ENTERTAINMENT_CLUSTER: Final[str] = "0xfc01"


class ZhaUnavailableError(Exception):
    """The Home Assistant plugin is not connected, or ZHA does not run on Ziggurat."""


@dataclass(frozen=True)
class ZhaLight:
    """A Hue lamp in ZHA that supports entertainment streaming."""

    ieee: str
    name: str
    area: str = ""


@dataclass(frozen=True)
class ZhaDevice:
    """The current network address and display name of a ZHA device."""

    nwk: int
    name: str


def get_hass(mass: MusicAssistant) -> HomeAssistantProvider | None:
    """
    Return the Home Assistant plugin when it is loaded and connected.

    :param mass: The MusicAssistant instance.
    """
    hass = cast("HomeAssistantProvider | None", mass.get_provider(HASS_DOMAIN))
    if hass is None or not hass.hass.connected:
        return None
    return hass


async def zha_available(mass: MusicAssistant) -> bool:
    """
    Return whether the Home Assistant plugin is connected and its ZHA runs on Ziggurat.

    :param mass: The MusicAssistant instance.
    """
    hass = get_hass(mass)
    if hass is None:
        return False
    try:
        return await _ziggurat_url(hass) is not None
    except Exception:
        return False


async def hue_lights(hass: HomeAssistantProvider) -> list[ZhaLight]:
    """
    Return the Hue lamps in ZHA that support entertainment streaming, sorted by area and name.

    :param hass: The connected Home Assistant plugin.
    :raises Exception: When Home Assistant does not answer or does not run ZHA.
    """
    devices = await _command(hass, "zha/devices")
    areas = await hass.get_area_registry()
    lights: list[ZhaLight] = []
    for device in devices:
        if not _is_entertainment_light(device):
            continue
        ieee = str(device["ieee"]).lower()
        area = areas.get(device.get("area_id") or "")
        lights.append(
            ZhaLight(ieee=ieee, name=_device_name(device, ieee), area=area["name"] if area else "")
        )
    return sorted(lights, key=lambda light: (light.area.casefold(), light.name.casefold()))


async def stream_info(mass: MusicAssistant) -> tuple[str, dict[str, ZhaDevice]]:
    """
    Return the Ziggurat websocket URL to connect to and the ZHA devices by IEEE.

    :param mass: The MusicAssistant instance.
    :raises ZhaUnavailableError: When the Home Assistant plugin is not connected or ZHA does
        not run on Ziggurat.
    """
    hass = get_hass(mass)
    if hass is None:
        msg = "the Home Assistant plugin is not connected"
        raise ZhaUnavailableError(msg)
    url = await _ziggurat_url(hass)
    if url is None:
        msg = "ZHA in Home Assistant does not run on Ziggurat"
        raise ZhaUnavailableError(msg)
    devices: dict[str, ZhaDevice] = {}
    for device in await _command(hass, "zha/devices"):
        nwk = _parse_nwk(device.get("nwk"))
        if nwk is not None:
            ieee = str(device.get("ieee", "")).lower()
            devices[ieee] = ZhaDevice(nwk, _device_name(device, ieee))
    return await _reachable_url(mass, hass, url), devices


async def _command(hass: HomeAssistantProvider, command: str) -> Any:
    """Run a websocket command without arguments on Home Assistant and return its result."""
    async with asyncio.timeout(_COMMAND_TIMEOUT_S):
        return await hass.hass.send_command(command)


async def _ziggurat_url(hass: HomeAssistantProvider) -> str | None:
    """Return the websocket URL of ZHA's Ziggurat server, or None for another radio."""
    # the settings also hold the network key: only the device path is ever read from them
    settings = await _command(hass, "zha/network/settings")
    device = settings.get("device") if isinstance(settings, dict) else None
    path = device.get("path") if isinstance(device, dict) else None
    if isinstance(path, str) and path.startswith(("ws://", "wss://")):
        return path
    return None


async def _reachable_url(mass: MusicAssistant, hass: HomeAssistantProvider, url: str) -> str:
    """Return the Ziggurat URL, moved to Home Assistant's host when its host does not resolve."""
    # ZHA names Ziggurat by its add-on host name, which only resolves for add-ons; the add-on
    # publishes the same port on the Home Assistant host
    parts = urlsplit(url)
    if mass.running_as_hass_addon or not parts.hostname:
        return url
    if await _resolves(mass, parts.hostname) or (host := await _ha_host(hass)) is None:
        return url
    if ":" in host:
        host = f"[{host}]"
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return parts._replace(netloc=netloc).geturl()


async def _resolves(mass: MusicAssistant, host: str) -> bool:
    """Return whether the host name resolves within a short timeout."""
    try:
        async with asyncio.timeout(_RESOLVE_TIMEOUT_S):
            await mass.loop.getaddrinfo(host, None)
    except OSError, TimeoutError:
        return False
    return True


async def _ha_host(hass: HomeAssistantProvider) -> str | None:
    """Return the host of Home Assistant's internal URL, else that of the plugin's URL."""
    try:
        urls = await _command(hass, "network/url")
    except Exception:
        urls = None
    internal = urls.get("internal") if isinstance(urls, dict) else None
    for url in (internal, hass.url):
        if isinstance(url, str) and (host := urlsplit(url).hostname):
            return host
    return None


def _is_entertainment_light(device: dict[str, Any]) -> bool:
    """Return whether a ZHA device is a Hue lamp with the entertainment cluster."""
    manufacturer = str(device.get("manufacturer") or "").lower()
    if not any(name in manufacturer for name in _HUE_MANUFACTURERS):
        return False
    if _parse_nwk(device.get("nwk")) is None or not device.get("ieee"):
        return False
    endpoints = (device.get("signature") or {}).get("endpoints") or {}
    clusters = (endpoints.get(_ENTERTAINMENT_ENDPOINT) or {}).get("input_clusters") or ()
    if _ENTERTAINMENT_CLUSTER not in {str(cluster).lower() for cluster in clusters}:
        return False
    return any(
        str(entity.get("entity_id", "")).startswith("light.")
        for entity in device.get("entities") or ()
    )


def _device_name(device: dict[str, Any], ieee: str) -> str:
    """Return the display name of a ZHA device: the user's name for it, else ZHA's."""
    return str(device.get("user_given_name") or device.get("name") or ieee)


def _parse_nwk(value: Any) -> int | None:
    """Return a ZHA network address given as a number or a hex string ("0x1A2B"), or None."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None
