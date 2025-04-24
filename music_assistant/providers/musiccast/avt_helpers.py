"""Helpers to make an UPnP request.

didl_lite helpers of MA didn't help, unfortunately...
"""

from enum import Enum, auto

import aiohttp
from music_assistant_models.player import PlayerMedia

from music_assistant.helpers.didl_lite import create_didl_metadata_str
from music_assistant.providers.musiccast.constants import (
    MC_DEVICE_UPNP_CTRL_ENDPOINT,
    MC_DEVICE_UPNP_PORT,
)
from music_assistant.providers.musiccast.musiccast import MusicCastPhysicalDevice


class AVTRequest(Enum):
    """Request Type."""

    PLAY = auto()
    STOP = auto()
    SET_URL = auto()
    SET_NEXT_URL = auto()
    GET_TRANSPORT_INFO = auto()
    GET_MEDIA_INFO = auto()
    NEXT = auto()
    PREVIOUS = auto()


def get_request_data(
    request: AVTRequest,
    player_media: PlayerMedia | None = None,
) -> tuple[str, str, dict[str, str]]:
    """Give xml, soap_action, headers, in this order.

    request_metadata must be present for set_url
    """
    body: str | None = None
    soap_action: str | None = None

    match request:
        case AVTRequest.STOP:
            body = (
                r'<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                r"<InstanceID>0</InstanceID>"
                r"</u:Stop>"
            )
            soap_action = "urn:schemas-upnp-org:service:AVTransport:1#Stop"
        case AVTRequest.PLAY:
            body = (
                r'<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                r"<InstanceID>0</InstanceID>"
                r"<Speed>1</Speed>"
                r"</u:Play>"
            )
            soap_action = "urn:schemas-upnp-org:service:AVTransport:1#Play"
        case AVTRequest.NEXT:
            body = (
                r'<u:Next xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                r"<InstanceID>0</InstanceID>"
                r"</u:Next>"
            )
            soap_action = "urn:schemas-upnp-org:service:AVTransport:1#Next"
        case AVTRequest.PREVIOUS:
            body = (
                r'<u:Previous xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                r"<InstanceID>0</InstanceID>"
                r"</u:Previous>"
            )
            soap_action = "urn:schemas-upnp-org:service:AVTransport:1#Previous"
        case AVTRequest.SET_URL | AVTRequest.SET_NEXT_URL:
            assert player_media is not None
            metadata = create_didl_metadata_str(player_media)
            if request == AVTRequest.SET_URL:
                body = (
                    r'<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                    r"<InstanceID>0</InstanceID>"
                    f"<CurrentURI>{player_media.uri}</CurrentURI>"
                    "<CurrentURIMetaData>"
                    f"{metadata}"
                    "</CurrentURIMetaData>"
                    r"</u:SetAVTransportURI>"
                )
                soap_action = "urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"
            else:
                body = (
                    r'<u:SetNextAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                    # ruff: noqa: E501
                    r"<InstanceID>0</InstanceID>"
                    f"<NextURI>{player_media.uri}</NextURI>"
                    "<NextURIMetaData>"
                    f"{metadata}"
                    "</NextURIMetaData>"
                    r"</u:SetNextAVTransportURI>"
                )
                soap_action = "urn:schemas-upnp-org:service:AVTransport:1#SetNextAVTransportURI"
        case AVTRequest.GET_TRANSPORT_INFO:
            body = (
                r'<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                "<InstanceID>0</InstanceID>"
                "</u:GetTransportInfo>"
            )
            soap_action = "urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"
        case AVTRequest.GET_MEDIA_INFO:
            body = (
                r'<u:GetMediaInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                "<InstanceID>0</InstanceID>"
                "</u:GetMediaInfo>"
            )
            soap_action = "urn:schemas-upnp-org:service:AVTransport:1#GetMediaInfo"

    assert body is not None
    assert soap_action is not None

    xml = (
        r'<?xml version="1.0"?>'
        r'<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        r"<s:Body>"
        f"{body}"
        r"</s:Body>"
        r"</s:Envelope>"
    )
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{soap_action}"',
        "Accept": "*/*",
        "User-Agent": "MusicCast/6.00 (Android)",
        "Content-Length": str(len(xml)),
    }
    return xml, soap_action, headers


def get_upnp_ctrl_url(physical_device: MusicCastPhysicalDevice) -> str:
    """Get UPNP control URL."""
    return f"http://{physical_device.device.device.ip}:{MC_DEVICE_UPNP_PORT}/{MC_DEVICE_UPNP_CTRL_ENDPOINT}"


async def avt_play(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Play."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action, headers = get_request_data(AVTRequest.PLAY)
    await client.post(ctrl_url, headers=headers, data=xml)


async def avt_stop(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> None:
    """Play."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action, headers = get_request_data(AVTRequest.STOP)
    await client.post(ctrl_url, headers=headers, data=xml)


async def avt_get_media_info(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> str:
    """Get Media Info."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action, headers = get_request_data(AVTRequest.GET_MEDIA_INFO)
    response = await client.post(ctrl_url, headers=headers, data=xml)
    response_text = await response.read()
    return response_text.decode()


async def avt_get_transport_info(
    client: aiohttp.ClientSession,
    physical_device: MusicCastPhysicalDevice,
) -> str:
    """Get Media Info."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action, headers = get_request_data(AVTRequest.GET_TRANSPORT_INFO)
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
        xml, soap_action, headers = get_request_data(
            AVTRequest.SET_NEXT_URL, player_media=player_media
        )
    else:
        xml, soap_action, headers = get_request_data(AVTRequest.SET_URL, player_media=player_media)
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


def search_didl_queueitemid(xml: str) -> str | None:
    """Search single line xml for these tags."""
    start_str = r"&lt;dc:queueItemId&gt;"
    end_str = r"&lt;/dc:queueItemId&gt;"
    start_int = xml.find(start_str)
    end_int = xml.find(end_str)
    if start_int == -1 or end_int == -1:
        return None
    return xml[start_int + len(start_str) : end_int]
