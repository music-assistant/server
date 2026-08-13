"""Various helpers and utils for the Open Home Player Provider."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiohttp.web import Request, Response
from async_upnp_client.const import HttpRequest
from async_upnp_client.event_handler import UpnpEventHandler, UpnpNotifyServer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpRequester

    from music_assistant import MusicAssistant

def generate_string(track_details):
    title = track_details.get("title", "") or ""
    uri = track_details.get("uri", "") or ""
    albumArtwork = track_details.get("albumArtwork", "") or ""

    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="" parentID="" restricted="True">'
        f"<dc:title>{title}</dc:title>"
        f'<res protocolInfo="*:*:*:*">{uri}</res>'
        f"<upnp:albumArtURI>{albumArtwork}</upnp:albumArtURI>"
        "<upnp:class>object.item.audioItem</upnp:class>"
        "</item>"
        "</DIDL-Lite>"
    )

class OpenHomeNotifyServer(UpnpNotifyServer):  # type: ignore[misc,unused-ignore]
    """Notify server for async_upnp_client which uses the MA webserver."""

    def __init__(
        self,
        requester: UpnpRequester,
        mass: MusicAssistant,
    ) -> None:
        """Initialize."""
        self.mass = mass
        self.event_handler = UpnpEventHandler(self, requester)
        # self.mass.streams.register_dynamic_route("/notify", self._handle_request, method="NOTIFY")

    async def _handle_request(self, request: Request) -> Response:
        """Handle incoming requests."""
        if request.method != "NOTIFY":
            return Response(status=405)

        # transform aiohttp request to async_upnp_client request
        http_request = HttpRequest(
            method=request.method,
            url=str(request.url),
            headers=request.headers,
            body=await request.text(),
        )

        status = await self.event_handler.handle_notify(http_request)

        return Response(status=status)

    @property
    def callback_url(self) -> str:
        """Return callback URL on which we are callable."""
        return f"{self.mass.streams.base_url}/notify"



# FIXME: text must be URL encoded - no & allowed
# NOTE: does didl-lite do this?
def create_linn_metadata(media, item):

    streamdetails = item.streamdetails
    audioformat = streamdetails.audio_format
    mediaitem = item.media_item
    metadata = mediaitem.metadata

    # provider = streamdetails.provider
    item_id = streamdetails.item_id

    # for qobuz - trackId must be after version
    uri_escaped = f"qobuz://track?version=2&amp;trackId={item_id}"
    title = html.escape(media.title)
    album = html.escape(media.album)
    artist = html.escape(media.artist)

    upnp_class = "object.item.audioItem.musicTrack"
    album_artist = artist
    composer = artist
    date = ""
    res_freq = audioformat.sample_rate
    res_bits = audioformat.bit_depth
    res_duration = item.duration
    res_uri = uri_escaped
    pins_uri = uri_escaped
    pins_mode = item.media_item.provider
    pins_type = "track"
    artist_id = mediaitem.artists[0].item_id
    album_id = mediaitem.album.item_id

    # TODO go with large, small and thumbnail
    albumart_small_uri = albumart_qb_uri(album_id, 230)
    albumart_large_uri = albumart_qb_uri(album_id, 600)
    albumart_thumb_uri = albumart_qb_uri(album_id, 50)

    # TODO handle this more flexibly using ElementTree XML
    metadata = f"""
<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"
    xmlns:linn="https://linn.co.uk">
<item>
<dc:title>{title}</dc:title>
<upnp:class>{upnp_class}</upnp:class>
<upnp:albumArtURI>{albumart_small_uri}</upnp:albumArtURI>
<upnp:albumArtURI>{albumart_large_uri}</upnp:albumArtURI>
<upnp:album>{album}</upnp:album>
<upnp:artist>{artist}</upnp:artist>
<upnp:artist role="AlbumArtist">{album_artist}</upnp:artist>
<upnp:artist role="Composer">{composer}</upnp:artist>
<dc:date>{date}</dc:date>
<res sampleFrequency="{res_freq}" bitsPerSample="{res_bits}" duration="{res_duration}">{res_uri}</res>
<linn:desc id="pinsUri">{pins_uri}</linn:desc>
<linn:desc id="pinsMode">{pins_mode}</linn:desc>
<linn:desc id="pinsType">{pins_type}</linn:desc>
<linn:desc id="artistId">{artist_id}</linn:desc>
<linn:desc id="albumId">{album_id}</linn:desc>
</item></DIDL-Lite>
"""
    return metadata

def albumart_qb_uri(album_id, dim):
    return f"https://static.qobuz.com/images/covers/{album_id[-2:]}/{album_id[-4:-2]}/{album_id}_{dim}.jpg"
