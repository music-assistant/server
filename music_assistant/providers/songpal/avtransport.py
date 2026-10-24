"""DLNA AVTransport client for Sony's Audio Control API playback samples.

Sony plays a URL with SOAP ``SetAVTransportURI`` then ``Play``, not with
``setPlayContent``. The control URL comes from the device description of an
``urn:schemas-upnp-org:service:AVTransport:1`` service.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urljoin
from xml.etree import ElementTree
from xml.sax.saxutils import escape

AVTRANSPORT_SERVICE = "urn:schemas-upnp-org:service:AVTransport:1"
_DEVICE_NS = "urn:schemas-upnp-org:device-1-0"
_SOAP_TIMEOUT = 10


class AvTransportError(Exception):
    """The renderer rejected an AVTransport SOAP action."""


def soap_action(action: str) -> str:
    """Return the quoted SOAPAction header value from Sony's cURL samples."""
    return f'"{AVTRANSPORT_SERVICE}#{action}"'


def didl_metadata(title: str) -> str:
    """Build the DIDL-Lite document from Sony's play-file sample."""
    safe_title = escape(title or "Music Assistant")
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/" '
        'xmlns:arib="urn:schemas-arib-or-jp:elements-1-0/" '
        'xmlns:av="urn:schemas-sony-com:av">'
        '<item id="1" restricted="1" parentID="0">'
        f"<dc:title>{safe_title}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        "</item></DIDL-Lite>"
    )


def _envelope(action: str, inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{AVTRANSPORT_SERVICE}">'
        "<InstanceID>0</InstanceID>"
        f"{inner}"
        f"</u:{action}>"
        "</s:Body></s:Envelope>"
    )


def set_av_transport_uri_body(uri: str, title: str) -> str:
    """SOAP body for SetAVTransportURI. The DIDL-Lite metadata is escaped."""
    metadata = escape(didl_metadata(title))
    inner = (
        f"<CurrentURI>{escape(uri)}</CurrentURI>"
        f"<CurrentURIMetaData>{metadata}</CurrentURIMetaData>"
    )
    return _envelope("SetAVTransportURI", inner)


def play_body() -> str:
    """SOAP body for Play at Speed 1, which also resumes a pause."""
    return _envelope("Play", "<Speed>1</Speed>")


def pause_body() -> str:
    """SOAP body for Pause."""
    return _envelope("Pause", "")


def stop_body() -> str:
    """SOAP body for Stop."""
    return _envelope("Stop", "")


def avtransport_control_url(description: str | bytes, description_url: str) -> str | None:
    """Resolve the AVTransport control URL from a UPnP device description."""
    payload = description.encode() if isinstance(description, str) else description
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return None
    ns = {"d": _DEVICE_NS}
    services = root.findall(".//d:service", ns)
    if not services:
        services = root.findall(".//service")
    for service in services:
        service_type = _text(service, "serviceType", ns) or ""
        if service_type.strip() != AVTRANSPORT_SERVICE:
            continue
        control = _text(service, "controlURL", ns)
        if not control:
            continue
        return urljoin(description_url, control.strip())
    return None


def _text(service: ElementTree.Element, tag: str, ns: dict[str, str]) -> str | None:
    found = service.find(f"d:{tag}", ns)
    if found is None:
        found = service.find(tag)
    if found is None or not found.text:
        return None
    return found.text


async def fetch_avtransport_control_url(session: object, location: str) -> str | None:
    """Download a device description and return its AVTransport control URL."""
    import aiohttp

    try:
        async with asyncio.timeout(_SOAP_TIMEOUT):
            async with session.get(location) as response:  # type: ignore[attr-defined]
                body = await response.read()
                if response.status >= 400:
                    return None
    except (TimeoutError, aiohttp.ClientError, AttributeError):
        return None
    return avtransport_control_url(body, location)


async def post_avtransport(session: object, control_url: str, action: str, body: str) -> None:
    """POST one AVTransport SOAP action to the renderer's control URL."""
    import aiohttp

    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": soap_action(action),
    }
    try:
        async with asyncio.timeout(_SOAP_TIMEOUT):
            async with session.post(  # type: ignore[attr-defined]
                control_url, data=body.encode("utf-8"), headers=headers
            ) as response:
                payload = await response.read()
                status = response.status
    except (TimeoutError, aiohttp.ClientError, AttributeError) as err:
        raise AvTransportError(str(err)) from err
    if status >= 400 or b":Fault" in payload:
        detail = payload.decode("utf-8", errors="replace")[:500]
        raise AvTransportError(f"{action} failed ({status}): {detail}")
