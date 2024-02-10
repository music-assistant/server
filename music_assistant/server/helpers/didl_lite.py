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
    mass: MusicAssistant, url: str, queue_item: QueueItem | None = None
) -> str:
    """Create DIDL metadata string from url and (optional) QueueItem."""
    ext = url.split(".")[-1].split("?")[0]
    if queue_item is None:
        return (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            f'<item id="flowmode" parentID="0" restricted="1">'
            "<dc:title>Music Assistant</dc:title>"
            f"<upnp:albumArtURI>{escape_string(MASS_LOGO_ONLINE)}</upnp:albumArtURI>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
            f'<res duration="23:59:59.000" protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_PN={ext.upper()};DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{escape_string(url)}</res>'
            "</item>"
            "</DIDL-Lite>"
        )
    is_radio = queue_item.media_type != MediaType.TRACK or not queue_item.duration
    image_url = mass.metadata.get_image_url(queue_item.image) if queue_item.image else ""
    if is_radio:
        # radio or other non-track item
        return (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
            '<item id="1" parentID="0" restricted="1">'
            f"<dc:title>{escape_string(queue_item.name)}</dc:title>"
            f"<upnp:albumArtURI>{escape_string(image_url)}</upnp:albumArtURI>"
            f"<dc:queueItemId>{queue_item.queue_item_id}</dc:queueItemId>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
            f'<res duration="23:59:59.000" protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_PN={ext.upper()};DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{escape_string(url)}</res>'
            "</item>"
            "</DIDL-Lite>"
        )
    title = escape_string(queue_item.media_item.name)
    if queue_item.media_item.artists and queue_item.media_item.artists[0].name:
        artist = escape_string(queue_item.media_item.artists[0].name)
    else:
        artist = ""
    if queue_item.media_item.album and queue_item.media_item.album.name:
        album = escape_string(queue_item.media_item.album.name)
    else:
        album = ""
    duration_str = str(datetime.timedelta(seconds=queue_item.duration)) + ".000"
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        '<item id="1" parentID="0" restricted="1">'
        f"<dc:title>{title}</dc:title>"
        f"<dc:creator>{artist}</dc:creator>"
        f"<upnp:album>{album}</upnp:album>"
        f"<upnp:artist>{artist}</upnp:artist>"
        f"<upnp:duration>{int(queue_item.duration)}</upnp:duration>"
        f"<dc:queueItemId>{queue_item.queue_item_id}</dc:queueItemId>"
        f"<upnp:albumArtURI>{escape_string(image_url)}</upnp:albumArtURI>"
        "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
        f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
        f'<res duration="{duration_str}" protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_PN={ext.upper()};DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{escape_string(url)}</res>'
        "</item>"
        "</DIDL-Lite>"
    )


def escape_string(data: str) -> str:
    """Create DIDL-safe string."""
    data = data.replace("&", "&amp;")
    # data = data.replace("?", "&#63;")
    data = data.replace(">", "&gt;")
    return data.replace("<", "&lt;")
