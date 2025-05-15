"""Helper(s) to create DIDL Lite metadata for Sonos/DLNA players."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as xmlescape

from music_assistant_models.enums import MediaType

from music_assistant.constants import MASS_LOGO_ONLINE

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia


# ruff: noqa: E501


# XML
def _get_soap_action(command: str) -> str:
    return f"urn:schemas-upnp-org:service:AVTransport:1#{command}"


def _get_body(command: str, arguments: str = "") -> str:
    return (
        f'<u:{command} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        r"<InstanceID>0</InstanceID>"
        f"{arguments}"
        f"</u:{command}>"
    )


def _get_xml(body: str) -> str:
    return (
        r'<?xml version="1.0"?>'
        r'<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        r"<s:Body>"
        f"{body}"
        r"</s:Body>"
        r"</s:Envelope>"
    )


def get_xml_soap_play() -> tuple[str, str]:
    """Get UPnP xml and soap for Play."""
    command = "Play"
    arguments = r"<Speed>1</Speed>"
    return _get_xml(_get_body(command, arguments)), _get_soap_action(command)


def get_xml_soap_stop() -> tuple[str, str]:
    """Get UPnP xml and soap for Stop."""
    command = "Stop"
    return _get_xml(_get_body(command)), _get_soap_action(command)


def get_xml_soap_pause() -> tuple[str, str]:
    """Get UPnP xml and soap for Pause."""
    command = "Pause"
    return _get_xml(_get_body(command)), _get_soap_action(command)


def get_xml_soap_next() -> tuple[str, str]:
    """Get UPnP xml and soap for Next."""
    command = "Next"
    return _get_xml(_get_body(command)), _get_soap_action(command)


def get_xml_soap_previous() -> tuple[str, str]:
    """Get UPnP xml and soap for Previous."""
    command = "Previous"
    return _get_xml(_get_body(command)), _get_soap_action(command)


def get_xml_soap_transport_info() -> tuple[str, str]:
    """Get UPnP xml and soap for GetTransportInfo."""
    command = "GetTransportInfo"
    return _get_xml(_get_body(command)), _get_soap_action(command)


def get_xml_soap_media_info() -> tuple[str, str]:
    """Get UPnP xml and soap for GetMediaInfo."""
    command = "GetMediaInfo"
    return _get_xml(_get_body(command)), _get_soap_action(command)


def get_xml_soap_set_url(player_media: PlayerMedia) -> tuple[str, str]:
    """Get UPnP xml and soap for SetAVTransportURI."""
    metadata = create_didl_metadata_str(player_media)
    command = "SetAVTransportURI"
    arguments = (
        f"<CurrentURI>{player_media.uri}</CurrentURI>"
        "<CurrentURIMetaData>"
        f"{metadata}"
        "</CurrentURIMetaData>"
    )
    return _get_xml(_get_body(command, arguments)), _get_soap_action(command)


def get_xml_soap_set_next_url(player_media: PlayerMedia) -> tuple[str, str]:
    """Get UPnP xml and soap for SetNextAVTransportURI."""
    metadata = create_didl_metadata_str(player_media)
    command = "SetNextAVTransportURI"
    arguments = (
        f"<NextURI>{player_media.uri}</NextURI><NextURIMetaData>{metadata}</NextURIMetaData>"
    )
    return _get_xml(_get_body(command, arguments)), _get_soap_action(command)


# DIDL-LITE
def create_didl_metadata(media: PlayerMedia) -> str:
    """Create DIDL metadata string from url and PlayerMedia."""
    ext = media.uri.split(".")[-1].split("?")[0]
    image_url = media.image_url or MASS_LOGO_ONLINE
    if media.media_type in (MediaType.FLOW_STREAM, MediaType.RADIO) or not media.duration:
        # flow stream, radio or other duration-less stream
        title = media.title or media.uri
        return (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            f'<item id="flowmode" parentID="0" restricted="1">'
            f"<dc:title>{xmlescape(title)}</dc:title>"
            f"<upnp:albumArtURI>{xmlescape(image_url)}</upnp:albumArtURI>"
            f"<dc:queueItemId>{media.uri}</dc:queueItemId>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
            f'<res protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_PN={ext.upper()};DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{xmlescape(media.uri)}</res>'
            "</item>"
            "</DIDL-Lite>"
        )
    duration_str = str(datetime.timedelta(seconds=media.duration or 0)) + ".000"

    assert media.queue_item_id is not None  # for type checking

    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        f'<item id="{media.queue_item_id or xmlescape(media.uri)}" restricted="true" parentID="{media.queue_id or ""}">'
        f"<dc:title>{xmlescape(media.title or media.uri)}</dc:title>"
        f"<dc:creator>{xmlescape(media.artist or '')}</dc:creator>"
        f"<upnp:album>{xmlescape(media.album or '')}</upnp:album>"
        f"<upnp:artist>{xmlescape(media.artist or '')}</upnp:artist>"
        f"<upnp:duration>{int(media.duration or 0)}</upnp:duration>"
        f"<dc:queueItemId>{xmlescape(media.queue_item_id)}</dc:queueItemId>"
        f"<dc:description>Music Assistant</dc:description>"
        f"<upnp:albumArtURI>{xmlescape(image_url)}</upnp:albumArtURI>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
        f'<res duration="{duration_str}" protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_PN={ext.upper()};DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{xmlescape(media.uri)}</res>'
        '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">RINCON_AssociatedZPUDN</desc>'
        "</item>"
        "</DIDL-Lite>"
    )


def create_didl_metadata_str(media: PlayerMedia) -> str:
    """Create (xml-escaped) DIDL metadata string from url and PlayerMedia."""
    return xmlescape(create_didl_metadata(media))
