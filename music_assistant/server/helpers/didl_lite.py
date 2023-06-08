"""Helper(s) to create DIDL Lite metadata for Sonos/DLNA players."""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from music_assistant.common.models.enums import MediaType
from music_assistant.constants import MASS_LOGO_ONLINE

if TYPE_CHECKING:
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant

# ruff: noqa: E501


def create_didl_metadata(
    mass: MusicAssistant, url: str, queue_item: QueueItem, flow_mode: bool = False
) -> str:
    """Create DIDL metadata string from url and QueueItem."""
    ext = url.split(".")[-1]
    is_radio = queue_item.media_type != MediaType.TRACK or not queue_item.duration
    image_url = mass.metadata.get_image_url(queue_item.image) if queue_item.image else ""

    if flow_mode:
        return (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            f'<item id="{queue_item.queue_item_id}" parentID="0" restricted="1">'
            f"<dc:title>Music Assistant</dc:title>"
            f"<upnp:albumArtURI>{MASS_LOGO_ONLINE}</upnp:albumArtURI>"
            f"<dc:queueItemId>{queue_item.queue_item_id}</dc:queueItemId>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
            f'<res protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{url}</res>'
            "</item>"
            "</DIDL-Lite>"
        )

    if is_radio:
        # radio or other non-track item
        return (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            f'<item id="{queue_item.queue_item_id}" parentID="0" restricted="1">'
            f"<dc:title>{_escape_str(queue_item.name)}</dc:title>"
            f"<upnp:albumArtURI>{_escape_str(image_url)}</upnp:albumArtURI>"
            f"<dc:queueItemId>{queue_item.queue_item_id}</dc:queueItemId>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
            f'<res protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{url}</res>'
            "</item>"
            "</DIDL-Lite>"
        )
    title = _escape_str(queue_item.media_item.name)
    if queue_item.media_item.artists and queue_item.media_item.artists[0].name:
        artist = _escape_str(queue_item.media_item.artists[0].name)
    else:
        artist = ""
    if queue_item.media_item.album and queue_item.media_item.album.name:
        album = _escape_str(queue_item.media_item.album.name)
    else:
        album = ""
    item_class = "object.item.audioItem.musicTrack"
    duration_str = str(datetime.timedelta(seconds=queue_item.duration))
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        f'<item id="{queue_item.queue_item_id}" parentID="0" restricted="1">'
        f"<dc:title>{title}</dc:title>"
        f"<dc:creator>{artist}</dc:creator>"
        f"<upnp:album>{album}</upnp:album>"
        f"<upnp:artist>{artist}</upnp:artist>"
        f"<upnp:duration>{queue_item.duration}</upnp:duration>"
        "<upnp:playlistTitle>Music Assistant</upnp:playlistTitle>"
        f"<dc:queueItemId>{queue_item.queue_item_id}</dc:queueItemId>"
        f"<upnp:albumArtURI>{_escape_str(image_url)}</upnp:albumArtURI>"
        f"<upnp:class>{item_class}</upnp:class>"
        f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
        f'<res duration="{duration_str}" protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{url}</res>'
        "</item>"
        "</DIDL-Lite>"
    )


def _escape_str(data: str) -> str:
    """Create DIDL-safe string."""
    data = data.replace("&", "&amp;")
    data = data.replace(">", "&gt;")
    data = data.replace("<", "&lt;")
    return data
