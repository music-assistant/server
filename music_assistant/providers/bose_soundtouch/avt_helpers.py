"""Helpers to make an UPnP request."""

import logging
from contextlib import suppress
from typing import TYPE_CHECKING

import aiohttp

from music_assistant.helpers.upnp import (
    get_xml_soap_play,
    get_xml_soap_set_next_url,
    get_xml_soap_set_url,
    get_xml_soap_stop,
)
from music_assistant.helpers.util import format_ip_for_url
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.bose_soundtouch.const import UPNP_CONTROL_ENDPOINT, UPNP_PORT

if TYPE_CHECKING:
    from music_assistant.providers.bose_soundtouch.player import BoseSoundTouchPlayer

LOGGER = logging.getLogger(__name__)


def get_headers(soap_action: str) -> dict[str, str]:
    """Get headers for Bose Soundtouch."""
    return {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{soap_action}"',
        "Accept": "*/*",
        "User-Agent": "MusicAssistant",
    }


def get_upnp_ctrl_url(device: BoseSoundTouchPlayer) -> str:
    """Get UPNP control URL."""
    return f"http://{format_ip_for_url(device._client.session_config.ip)}:{UPNP_PORT}/{UPNP_CONTROL_ENDPOINT}"


async def avt_play(
    client: aiohttp.ClientSession,
    physical_device: BoseSoundTouchPlayer,
) -> None:
    """Play."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_play()
    await _post_soap(client, ctrl_url, xml, soap_action, "Play")


async def avt_stop(
    client: aiohttp.ClientSession,
    physical_device: BoseSoundTouchPlayer,
) -> None:
    """Stop."""
    ctrl_url = get_upnp_ctrl_url(physical_device)
    xml, soap_action = get_xml_soap_stop()
    await _post_soap(client, ctrl_url, xml, soap_action, "Stop")


async def avt_set_url(
    client: aiohttp.ClientSession,
    physical_device: BoseSoundTouchPlayer,
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


async def _post_soap(
    client: aiohttp.ClientSession,
    ctrl_url: str,
    xml: str,
    soap_action: str,
    op_name: str,
) -> aiohttp.ClientResponse:
    """POST a SOAP request and log a warning on 4xx/5xx error responses."""
    headers = get_headers(soap_action)
    response = await client.post(ctrl_url, headers=headers, data=xml)
    if response.status >= 400:
        body_excerpt = ""
        with suppress(aiohttp.ClientError, UnicodeError):
            body_excerpt = (await response.read())[:300].decode(errors="replace")
        LOGGER.warning(
            "AVT %s failed: status=%s url=%s body=%s",
            op_name,
            response.status,
            ctrl_url,
            body_excerpt,
        )
    return response
