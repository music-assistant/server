"""Helpers to make an UPnP request."""

import logging
from contextlib import suppress

import aiohttp

from music_assistant.helpers.upnp import (
    get_xml_soap_media_info,
    get_xml_soap_next,
    get_xml_soap_play,
    get_xml_soap_previous,
    get_xml_soap_set_next_url,
    get_xml_soap_set_url,
    get_xml_soap_stop,
)
from music_assistant.helpers.util import format_ip_for_url
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.musiccast.constants import (
    MC_DEVICE_UPNP_CTRL_ENDPOINT,
    MC_DEVICE_UPNP_PORT,
)
from music_assistant.providers.musiccast.musiccast import MusicCastPhysicalDevice

LOGGER = logging.getLogger(__name__)


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
    return f"http://{format_ip_for_url(physical_device.device.device.ip)}:{MC_DEVICE_UPNP_PORT}/{MC_DEVICE_UPNP_CTRL_ENDPOINT}"


async def _post_soap(
    client: aiohttp.ClientSession,
    ctrl_url: str,
    xml: str,
    soap_action: str,
    op_name: str,
    read_body: bool = False,
) -> str | None:
    """
    POST a SOAP request and log a warning on 4xx/5xx error responses.

    :param client: Session to send the request with.
    :param ctrl_url: AVTransport control url of the device.
    :param xml: SOAP request body.
    :param soap_action: SOAPACTION header value.
    :param op_name: Operation name for the log.
    :param read_body: Return the response body; None then means an error response.
    """
    headers = get_headers(xml, soap_action)
    async with client.post(ctrl_url, headers=headers, data=xml) as response:
        if response.status < 400:
            return (await response.read()).decode(errors="replace") if read_body else None
        body_excerpt = ""
        with suppress(aiohttp.ClientError):
            body_excerpt = (await response.read())[:300].decode(errors="replace")
    LOGGER.warning(
        "AVT %s failed: status=%s url=%s body=%s",
        op_name,
        response.status,
        ctrl_url,
        body_excerpt,
    )
    return None


async def avt_play(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Play."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_play()
    await _post_soap(client, ctrl_url, xml, soap_action, "Play")


async def avt_stop(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Stop."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_stop()
    await _post_soap(client, ctrl_url, xml, soap_action, "Stop")


async def avt_next(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Next."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_next()
    await _post_soap(client, ctrl_url, xml, soap_action, "Next")


async def avt_previous(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Previous."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_previous()
    await _post_soap(client, ctrl_url, xml, soap_action, "Previous")


async def avt_get_media_info(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> str:
    """Get Media Info."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_media_info()
    body = await _post_soap(client, ctrl_url, xml, soap_action, "GetMediaInfo", read_body=True)
    if body is None:
        raise aiohttp.ClientError("GetMediaInfo failed")
    return body


async def avt_set_url(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
    player_media: PlayerMedia,
    enqueue: bool = False,
) -> None:
    """
    Set Url.

    If device is playing, this will just continue with new media.
    """
    ctrl_url = get_upnp_ctrl_url(physical_device)
    if enqueue:
        xml, soap_action = get_xml_soap_set_next_url(player_media)
        op_name = "SetNextAVTransportURI"
    else:
        xml, soap_action = get_xml_soap_set_url(player_media)
        op_name = "SetAVTransportURI"
    LOGGER.debug("AVT %s uri=%s", op_name, player_media.uri)
    await _post_soap(client, ctrl_url, xml, soap_action, op_name)


def search_xml(xml: str, tag: str) -> str | None:
    """Search single line xml for these tags."""
    start_str = f"<{tag}>"
    end_str = f"</{tag}>"
    start_int = xml.find(start_str)
    end_int = xml.find(end_str)
    if start_int == -1 or end_int == -1:
        return None
    return xml[start_int + len(start_str) : end_int]
