"""Helper(s) to create DIDL Lite metadata for Sonos/DLNA players."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from music_assistant.common.models.enums import MediaType
from music_assistant.constants import MASS_LOGO_ONLINE

if TYPE_CHECKING:
    from music_assistant.common.models.player import PlayerMedia

# ruff: noqa: E501


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
            f"<dc:title>{escape_string(title)}</dc:title>"
            f"<upnp:albumArtURI>{escape_string(image_url)}</upnp:albumArtURI>"
            f"<dc:queueItemId>{media.uri}</dc:queueItemId>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
            f'<res duration="23:59:59.000" protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_PN={ext.upper()};DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{escape_string(media.uri)}</res>'
            "</item>"
            "</DIDL-Lite>"
        )
    duration_str = str(datetime.timedelta(seconds=media.duration or 0)) + ".000"
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        '<item id="1" parentID="0" restricted="1">'
        f"<dc:title>{escape_string(media.title or media.uri)}</dc:title>"
        f"<dc:creator>{escape_string(media.artist or '')}</dc:creator>"
        f"<upnp:album>{escape_string(media.album or '')}</upnp:album>"
        f"<upnp:artist>{escape_string(media.artist or '')}</upnp:artist>"
        f"<upnp:duration>{int(media.duration or 0)}</upnp:duration>"
        f"<dc:queueItemId>{media.uri}</dc:queueItemId>"
        f"<upnp:albumArtURI>{escape_string(image_url)}</upnp:albumArtURI>"
        "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
        f"<upnp:mimeType>audio/{ext}</upnp:mimeType>"
        f'<res duration="{duration_str}" protocolInfo="http-get:*:audio/{ext}:DLNA.ORG_PN={ext.upper()};DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000">{escape_string(media.uri)}</res>'
        "</item>"
        "</DIDL-Lite>"
    )


def escape_string(data: str) -> str:
    """Create DIDL-safe string."""
    data = data.replace("&", "&amp;")
    # data = data.replace("?", "&#63;")
    data = data.replace(">", "&gt;")
    return data.replace("<", "&lt;")
