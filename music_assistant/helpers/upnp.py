"""Helper(s) to create DIDL Lite metadata and AVTransport requests for Sonos/DLNA players."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape as xmlescape

import aiohttp
from music_assistant_models.enums import MediaType

from music_assistant.constants import MASS_LOGO_ONLINE
from music_assistant.helpers.audio import get_mime_type

if TYPE_CHECKING:
    from music_assistant.models.player import PlayerMedia

LOGGER = logging.getLogger(__name__)


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
def create_didl_metadata(media: PlayerMedia, url: str | None = None) -> str:
    """Create DIDL metadata string from url and PlayerMedia."""
    uri = url or media.uri

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

    ext = uri.split(".")[-1].split("?")[0]
    mime_type = get_mime_type(ext)
    image_url = media.image_url or MASS_LOGO_ONLINE
    if media.media_type in (MediaType.FLOW_STREAM, MediaType.RADIO) or not media.duration:
        # flow stream, radio or other duration-less stream
        # Use streaming-optimized DLNA flags to prevent buffering
        title = media.title or uri
        return (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            f'<item id="flowmode" parentID="0" restricted="1">'
            f"<dc:title>{escape_metadata(title)}</dc:title>"
            f"<upnp:albumArtURI>{escape_metadata(image_url)}</upnp:albumArtURI>"
            f"<dc:queueItemId>{escape_metadata(uri)}</dc:queueItemId>"
            f"<dc:description>Music Assistant</dc:description>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f'<res protocolInfo="http-get:*:{mime_type}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000">{escape_metadata(uri)}</res>'
            "</item>"
            "</DIDL-Lite>"
        )

    assert media.queue_item_id is not None  # for type checking

    # For regular tracks with duration, use flags optimized for on-demand content
    # DLNA.ORG_FLAGS=01500000000000000000000000000000 indicates:
    # - Streaming transfer mode (bit 24)
    # - Background transfer mode supported (bit 22)
    # - DLNA v1.5 (bit 20)
    # the res duration describes the audio we hand over, which is shorter than
    # the media item when playback starts at a seek position
    stream_duration = int(media.stream_duration or media.duration or 0)
    duration_str = str(stream_duration // 3600).zfill(2) + ":"
    duration_str += str((stream_duration % 3600) // 60).zfill(2) + ":"
    duration_str += str(stream_duration % 60).zfill(2)

    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        f'<item id="{media.queue_item_id or xmlescape(uri)}" restricted="true" parentID="{media.source_id or ""}">'
        f"<dc:title>{escape_metadata(media.title or uri)}</dc:title>"
        f"<dc:creator>{escape_metadata(media.artist or '')}</dc:creator>"
        f"<upnp:album>{escape_metadata(media.album or '')}</upnp:album>"
        f"<upnp:artist>{escape_metadata(media.artist or '')}</upnp:artist>"
        f"<dc:queueItemId>{escape_metadata(media.queue_item_id)}</dc:queueItemId>"
        f"<dc:description>Music Assistant</dc:description>"
        f"<upnp:albumArtURI>{escape_metadata(image_url)}</upnp:albumArtURI>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res duration="{duration_str}" protocolInfo="http-get:*:{mime_type}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01500000000000000000000000000000">{escape_metadata(uri)}</res>'
        '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">RINCON_AssociatedZPUDN</desc>'
        "</item>"
        "</DIDL-Lite>"
    )


def create_didl_metadata_str(media: PlayerMedia) -> str:
    """Create (xml-escaped) DIDL metadata string from url and PlayerMedia."""
    return xmlescape(create_didl_metadata(media))


def search_xml(xml: str, tag: str) -> str | None:
    """Search single line xml for these tags."""
    start_str = f"<{tag}>"
    end_str = f"</{tag}>"
    start_int = xml.find(start_str)
    end_int = xml.find(end_str)
    if start_int == -1 or end_int == -1:
        return None
    return xml[start_int + len(start_str) : end_int]


@dataclass(frozen=True)
class AVTransport:
    """UPnP AVTransport service of a renderer, controlled with plain SOAP requests."""

    client: aiohttp.ClientSession
    ctrl_url: str
    user_agent: str
    encoding: str = "utf-8"

    async def play(self) -> None:
        """Play."""
        await self._post(*get_xml_soap_play(), "Play")

    async def stop(self) -> None:
        """Stop."""
        await self._post(*get_xml_soap_stop(), "Stop")

    async def next(self) -> None:
        """Next."""
        await self._post(*get_xml_soap_next(), "Next")

    async def previous(self) -> None:
        """Previous."""
        await self._post(*get_xml_soap_previous(), "Previous")

    async def set_url(self, player_media: PlayerMedia, enqueue: bool = False) -> None:
        """
        Set the url to play, or with enqueue the url to play next.

        If the renderer is playing, it continues with the new media.

        :param player_media: Media to play.
        :param enqueue: Set the next url instead of the current one.
        """
        if enqueue:
            xml, soap_action = get_xml_soap_set_next_url(player_media)
            op_name = "SetNextAVTransportURI"
        else:
            xml, soap_action = get_xml_soap_set_url(player_media)
            op_name = "SetAVTransportURI"
        LOGGER.debug("AVT %s uri=%s", op_name, player_media.uri)
        await self._post(xml, soap_action, op_name)

    async def get_media_info(self) -> str:
        """Get the GetMediaInfo response xml."""
        body = await self._post(*get_xml_soap_media_info(), "GetMediaInfo", read_body=True)
        if body is None:
            raise aiohttp.ClientError("GetMediaInfo failed")
        return body

    async def _post(
        self, xml: str, soap_action: str, op_name: str, read_body: bool = False
    ) -> str | None:
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{soap_action}"',
            "Accept": "*/*",
            "User-Agent": self.user_agent,
        }
        # commands skip the body, a renderer can hold it back until it fetched the stream
        async with self.client.post(
            self.ctrl_url, headers=headers, data=xml.encode(self.encoding)
        ) as response:
            if response.status < 400:
                if not read_body:
                    return None
                return (await response.read()).decode(self.encoding, errors="replace")
            body_excerpt = ""
            with suppress(aiohttp.ClientError):
                body_excerpt = (await response.read())[:300].decode(self.encoding, errors="replace")
        LOGGER.warning(
            "AVT %s failed: status=%s url=%s body=%s",
            op_name,
            response.status,
            self.ctrl_url,
            body_excerpt,
        )
        return None
