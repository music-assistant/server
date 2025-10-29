"""Helper(s) to create DIDL Lite metadata for Sonos/DLNA players."""

from __future__ import annotations

from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as xmlescape

from music_assistant_models.enums import MediaType

from music_assistant.constants import MASS_LOGO_ONLINE

if TYPE_CHECKING:
    from music_assistant.models.player import PlayerMedia


# ruff: noqa: E501


# XML
def _get_soap_action(command: str) -> str:
    return f"urn:schemas-upnp-org:service:AVTransport:1#{command}"


def _get_body(command: str, arguments: str = "", service: str = "AVTransport") -> str:
    return (
        f'<u:{command} xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
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


def get_xml_soap_remove_all_tracks() -> tuple[str, str]:
    """Get UPnP xml and soap for RemoveAllTracksFromQueue."""
    command = "RemoveAllTracksFromQueue"
    return _get_xml(_get_body(command)), _get_soap_action(command)


def get_xml_soap_set_next_url(player_media: PlayerMedia) -> tuple[str, str]:
    """Get UPnP xml and soap for SetNextAVTransportURI."""
    metadata = create_didl_metadata_str(player_media)
    command = "SetNextAVTransportURI"
    arguments = (
        f"<NextURI>{player_media.uri}</NextURI><NextURIMetaData>{metadata}</NextURIMetaData>"
    )
    return _get_xml(_get_body(command, arguments)), _get_soap_action(command)


# RemoveTrackFromQueue
def get_xml_soap_remove_track(object_id: str) -> tuple[str, str]:
    """Get UPnP xml and soap for RemoveTrackFromQueue."""
    command = "RemoveTrackFromQueue"
    arguments = f"<ObjectID>{object_id}</ObjectID>"
    return _get_xml(_get_body(command, arguments)), _get_soap_action(command)


# AddURIToQueue
def get_xml_soap_add_uri_to_queue(player_media: PlayerMedia) -> tuple[str, str]:
    """Get UPnP xml and soap for AddURIToQueue."""
    metadata = create_didl_metadata_str(player_media)
    command = "AddURIToQueue"
    arguments = (
        f"<EnqueuedURI>{player_media.uri}</EnqueuedURI>"
        f"<EnqueuedURIMetaData>{metadata}</EnqueuedURIMetaData>"
        "<DesiredFirstTrackNumberEnqueued>1</DesiredFirstTrackNumberEnqueued>"
        "<EnqueueAsNext>0</EnqueueAsNext>"
    )
    return _get_xml(_get_body(command, arguments)), _get_soap_action(command)


# CreateSavedQueue
def get_xml_soap_create_saved_queue(queue_name: str, player_media: PlayerMedia) -> tuple[str, str]:
    """Get UPnP xml and soap for CreateSavedQueue."""
    command = "CreateSavedQueue"
    metadata = create_didl_metadata_str(player_media)
    arguments = (
        f"<Title>{xmlescape(queue_name)}</Title>"
        f"<EnqueuedURI>{player_media.uri}</EnqueuedURI>"
        f"<EnqueuedURIMetaData>{metadata}</EnqueuedURIMetaData>"
    )
    return _get_xml(_get_body(command, arguments)), _get_soap_action(command)


# CreateQueue
def get_xml_soap_create_queue() -> tuple[str, str]:
    """Get UPnP xml and soap for CreateQueue."""
    command = "CreateQueue"
    arguments = (
        "<QueueOwnerID>mass</QueueOwnerID>"
        "<QueueOwnerContext>mass</QueueOwnerContext>"
        "<QueuePolicy>0</QueuePolicy>"
    )
    return _get_xml(_get_body(command, arguments, "Queue")), _get_soap_action(command)


# DIDL-LITE
def create_didl_metadata(media: PlayerMedia) -> str:
    """Create DIDL metadata string from url and PlayerMedia."""

    def escape_metadata(data: str) -> str:
        """Escape didl metadata."""
        data = xmlescape(data)
        # Escape non-ascii to decimal code.
        result = ""
        for char in data:
            unicode_code = ord(char)
            if unicode_code < 128:
                # ascii
                result += char
            else:
                result += f"&#{unicode_code};"
        return result

    ext = media.uri.split(".")[-1].split("?")[0]
    image_url = media.image_url or MASS_LOGO_ONLINE
    if media.media_type in (MediaType.FLOW_STREAM, MediaType.RADIO) or not media.duration:
        # flow stream, radio or other duration-less stream
        # Use streaming-optimized DLNA flags to prevent buffering
        title = media.title or media.uri
        return (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            f'<item id="flowmode" parentID="0" restricted="1">'
            f"<dc:title>{escape_metadata(title)}</dc:title>"
            f"<upnp:albumArtURI>{escape_metadata(image_url)}</upnp:albumArtURI>"
            f"<dc:queueItemId>{escape_metadata(media.uri)}</dc:queueItemId>"
            f"<dc:description>Music Assistant</dc:description>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f'<res protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000">{escape_metadata(media.uri)}</res>'
            "</item>"
            "</DIDL-Lite>"
        )

    assert media.queue_item_id is not None  # for type checking

    # For regular tracks with duration, use flags optimized for on-demand content
    # DLNA.ORG_FLAGS=01500000000000000000000000000000 indicates:
    # - Streaming transfer mode (bit 24)
    # - Background transfer mode supported (bit 22)
    # - DLNA v1.5 (bit 20)
    duration_str = str(int(media.duration or 0) // 3600).zfill(2) + ":"
    duration_str += str((int(media.duration or 0) % 3600) // 60).zfill(2) + ":"
    duration_str += str(int(media.duration or 0) % 60).zfill(2)

    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        f'<item id="{media.queue_item_id or xmlescape(media.uri)}" restricted="true" parentID="{media.source_id or ""}">'
        f"<dc:title>{escape_metadata(media.title or media.uri)}</dc:title>"
        f"<dc:creator>{escape_metadata(media.artist or '')}</dc:creator>"
        f"<upnp:album>{escape_metadata(media.album or '')}</upnp:album>"
        f"<upnp:artist>{escape_metadata(media.artist or '')}</upnp:artist>"
        f"<dc:queueItemId>{escape_metadata(media.queue_item_id)}</dc:queueItemId>"
        f"<dc:description>Music Assistant</dc:description>"
        f"<upnp:albumArtURI>{escape_metadata(image_url)}</upnp:albumArtURI>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res duration="{duration_str}" protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01500000000000000000000000000000">{escape_metadata(media.uri)}</res>'
        '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">RINCON_AssociatedZPUDN</desc>'
        "</item>"
        "</DIDL-Lite>"
    )


def create_didl_metadata_str(media: PlayerMedia) -> str:
    """Create (xml-escaped) DIDL metadata string from url and PlayerMedia."""
    return xmlescape(create_didl_metadata(media))
