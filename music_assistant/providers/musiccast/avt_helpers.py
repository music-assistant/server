"""Helpers to make an UPnP request."""

import aiohttp

from music_assistant.helpers.upnp import (
    get_xml_soap_media_info,
    get_xml_soap_next,
    get_xml_soap_pause,
    get_xml_soap_play,
    get_xml_soap_previous,
    get_xml_soap_set_next_url,
    get_xml_soap_set_url,
    get_xml_soap_stop,
    get_xml_soap_transport_info,
)
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.musiccast.constants import (
    MC_DEVICE_UPNP_CTRL_ENDPOINT,
    MC_DEVICE_UPNP_PORT,
)
from music_assistant.providers.musiccast.musiccast import MusicCastPhysicalDevice


def get_headers(xml: str, soap_action: str) -> dict[str, str]:
    """Get headers for MusicCast."""
    return {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{soap_action}"',
        "Accept": "*/*",
        "User-Agent": "MusicCast/6.00 (Android)",
        "Content-Length": str(len(xml)),
    }


def get_upnp_ctrl_url(physical_device: MusicCastPhysicalDevice) -> str:
    """Get UPNP control URL."""
    return f"http://{physical_device.device.device.ip}:{MC_DEVICE_UPNP_PORT}/{MC_DEVICE_UPNP_CTRL_ENDPOINT}"


async def avt_play(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Play."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_play()
    headers = get_headers(xml, soap_action)
    await client.post(ctrl_url, headers=headers, data=xml)


async def avt_stop(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Play."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_stop()
    headers = get_headers(xml, soap_action)
    await client.post(ctrl_url, headers=headers, data=xml)


async def avt_pause(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Play."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_pause()
    headers = get_headers(xml, soap_action)
    await client.post(ctrl_url, headers=headers, data=xml)


async def avt_next(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Play."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_next()
    headers = get_headers(xml, soap_action)
    await client.post(ctrl_url, headers=headers, data=xml)


async def avt_previous(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Play."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_previous()
    headers = get_headers(xml, soap_action)
    await client.post(ctrl_url, headers=headers, data=xml)


async def avt_get_media_info(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> str:
    """Get Media Info."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_media_info()
    headers = get_headers(xml, soap_action)
    response = await client.post(ctrl_url, headers=headers, data=xml)
    response_text = await response.read()
    return response_text.decode()


async def avt_get_transport_info(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> str:
    """Get Media Info."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_transport_info()
    headers = get_headers(xml, soap_action)
    response = await client.post(ctrl_url, headers=headers, data=xml)
    response_text = await response.read()
    return response_text.decode()


async def avt_set_url(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
    player_media: PlayerMedia,
    enqueue: bool = False,
) -> None:
    """Set Url.

    If device is playing, this will just continue with new media.
    """
    ctrl_url = get_upnp_ctrl_url(physical_device)
    if enqueue:
        xml, soap_action = get_xml_soap_set_next_url(player_media)
    else:
        xml, soap_action = get_xml_soap_set_url(player_media)
    headers = get_headers(xml, soap_action)
    await client.post(ctrl_url, headers=headers, data=xml)


def search_xml(xml: str, tag: str) -> str | None:
    """Search single line xml for these tags."""
    start_str = f"<{tag}>"
    end_str = f"</{tag}>"
    start_int = xml.find(start_str)
    end_int = xml.find(end_str)
    if start_int == -1 or end_int == -1:
        return None
    return xml[start_int + len(start_str) : end_int]
