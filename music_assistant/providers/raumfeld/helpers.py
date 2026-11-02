"""Helpers for the Teufel Raumfeld player provider."""

from __future__ import annotations

from urllib.parse import parse_qs, quote, unquote, urlsplit

import aiohttp
import defusedxml.ElementTree as DefusedET
from music_assistant_models.enums import RepeatMode

from music_assistant.helpers.util import try_parse_duration

from .constants import HOST_CHECK_TIMEOUT, PLAYER_ID_PREFIX, RENDERER_QUERY_TIMEOUT

# DIDL-Lite / metadata XML namespaces used in UPnP TrackMetaData.
_DC = "{http://purl.org/dc/elements/1.1/}"
_UPNP = "{urn:schemas-upnp-org:metadata-1-0/upnp/}"
_DIDL = "{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}"
_RAUMFELD = "{urn:schemas-raumfeld-com:meta-data/raumfeld}"

# The section the host files a playing item under, mapped to the source id and name Music
# Assistant knows that service by. A section not listed here is used as it is.
_SECTION_SOURCES = {
    "spotify": ("spotify", "Spotify"),
    "radiotime": ("tunein", "TuneIn"),
    "tidal": ("tidal", "Tidal"),
}
# Source reported for playback the host does not file under any section.
_UNKNOWN_SOURCE = ("raumfeld", "Raumfeld")

_QUERY_LAST_CHANGE = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
    '<u:QueryStateVariable xmlns:u="urn:schemas-upnp-org:control-1-0">'
    "<u:varName>LastChange</u:varName>"
    "</u:QueryStateVariable></s:Body></s:Envelope>"
)
_QUERY_HEADERS = {
    "Content-Type": 'text/xml; charset="utf-8"',
    "SOAPACTION": '"urn:schemas-upnp-org:control-1-0#QueryStateVariable"',
}


def serial_to_player_id(serial: str) -> str:
    """
    Derive a stable Music Assistant player_id from a renderer's hardware serial.

    :param serial: The renderer's serialNumber (e.g. ``04:a3:16:f3:b6:c4``).
    """
    # The serial lives in the hardware, so it survives both a room rename and a rebuild of
    # the Raumfeld system. A room UDN only survives the former: re-adding a device (or
    # setting the system up again) hands every room a fresh UDN and orphans its config.
    cleaned = "".join(char for char in serial if char.isalnum()).lower()
    return f"{PLAYER_ID_PREFIX}_{cleaned}"


def room_udn_to_player_id(room_udn: str) -> str:
    """
    Derive a room-UDN based player_id, for a room whose serial another room already uses.

    :param room_udn: The room UDN (e.g. ``uuid:1234...``).
    """
    return f"{PLAYER_ID_PREFIX}_{room_udn.removeprefix('uuid:')}"


def parse_serial_number(description_xml: str | None) -> str | None:
    """
    Return the ``serialNumber`` from a UPnP device description, or ``None``.

    Malformed or empty XML, or a description without a serial, yields ``None``.

    :param description_xml: The renderer's device description XML.
    """
    return _parse_description_field(description_xml, "serialNumber")


def parse_model_name(description_xml: str | None) -> str | None:
    """
    Return the ``modelName`` from a UPnP device description, or ``None``.

    Malformed or empty XML, or a description without a model name, yields ``None``.

    :param description_xml: The renderer's device description XML.
    """
    return _parse_description_field(description_xml, "modelName")


def parse_didl_metadata(didl_xml: str | None) -> dict[str, str | None]:
    """
    Parse a DIDL-Lite ``TrackMetaData`` XML string into title/artist/album/image.

    Returns a dict with keys ``title``, ``artist``, ``album``, ``image_url``, ``section``
    (the service the host files the item under) and ``item_id``, each possibly ``None``.
    Malformed or empty XML yields all-``None`` (never raises).

    :param didl_xml: The DIDL-Lite metadata XML as returned by GetPositionInfo, or None.
    """
    result: dict[str, str | None] = {
        "title": None,
        "artist": None,
        "album": None,
        "image_url": None,
        "section": None,
        "item_id": None,
    }
    if not didl_xml or not didl_xml.strip() or didl_xml.strip().upper() == "NOT_IMPLEMENTED":
        return result
    try:
        root = DefusedET.fromstring(didl_xml)
    except DefusedET.ParseError, ValueError:
        return result
    item = root.find(".//{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}item")
    node = item if item is not None else root
    title = node.find(f"{_DC}title")
    artist = node.find(f"{_UPNP}artist")
    if artist is None:
        artist = node.find(f"{_DC}creator")
    album = node.find(f"{_UPNP}album")
    image = node.find(f"{_UPNP}albumArtURI")
    result["title"] = title.text if title is not None else None
    result["artist"] = artist.text if artist is not None else None
    result["album"] = album.text if album is not None else None
    result["image_url"] = image.text if image is not None else None
    section = node.find(f"{_RAUMFELD}section")
    result["section"] = section.text if section is not None else None
    result["item_id"] = item.get("id") if item is not None else None
    return result


def external_source(section: str | None) -> tuple[str, str]:
    """
    Return the ``(source id, name)`` for playback the host files under a section.

    :param section: The ``raumfeld:section`` of the playing item, or None.
    """
    if not section or not section.strip():
        return _UNKNOWN_SOURCE
    name = section.strip()
    return _SECTION_SOURCES.get(name.lower(), (name.lower().replace(" ", "_"), name))


def parse_last_change(last_change_xml: str | None) -> dict[str, str]:
    """
    Parse an AVTransport ``LastChange`` event into ``{state variable: value}``.

    Malformed or empty XML yields an empty dict (never raises).

    :param last_change_xml: The ``LastChange`` event XML.
    """
    if not last_change_xml or not last_change_xml.strip():
        return {}
    try:
        root = DefusedET.fromstring(last_change_xml)
    except DefusedET.ParseError, ValueError:
        return {}
    return {
        element.tag.rsplit("}", 1)[-1]: value
        for element in root.iter()
        if (value := element.get("val")) is not None
    }


def parse_control_url(description_xml: str | None, service: str) -> str | None:
    """
    Return the control URL of a service from a UPnP device description, or ``None``.

    :param description_xml: The device description XML.
    :param service: Part of the service type to look for (e.g. ``AVTransport``).
    """
    if not description_xml or not description_xml.strip():
        return None
    try:
        root = DefusedET.fromstring(description_xml)
    except DefusedET.ParseError, ValueError:
        return None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "service":
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in element}
        if service in fields.get("serviceType", ""):
            return fields.get("controlURL") or None
    return None


async def host_is_valid(session: aiohttp.ClientSession, host: str, port: int) -> bool:
    """
    Return whether a Raumfeld host answers ``getHostInfo`` at the given address.

    Replaces hassfeld's ``async_host_is_valid``, which opens a session of its own and
    gives the host 3 seconds.

    :param session: The HTTP session to use.
    :param host: Address of the Raumfeld host.
    :param port: Port of the host's webservice.
    """
    try:
        # the host answers with a redirect to a session path first, which aiohttp follows
        async with session.get(
            f"http://{host}:{port}/getHostInfo",
            timeout=aiohttp.ClientTimeout(total=HOST_CHECK_TIMEOUT),
        ) as response:
            if response.status != 200:
                return False
            body = await response.text()
        root = DefusedET.fromstring(body)
    except aiohttp.ClientError, OSError, TimeoutError, DefusedET.ParseError:
        return False
    return bool((root.findtext("hostName") or "").strip())


async def query_last_change(session: aiohttp.ClientSession, control_url: str) -> dict[str, str]:
    """
    Read the current ``LastChange`` state of a renderer's AVTransport service.

    :param session: The HTTP session to use.
    :param control_url: The AVTransport control URL of the renderer.
    """
    async with session.post(
        control_url,
        data=_QUERY_LAST_CHANGE,
        headers=_QUERY_HEADERS,
        timeout=aiohttp.ClientTimeout(total=RENDERER_QUERY_TIMEOUT),
    ) as response:
        if response.status != 200:
            return {}
        body = await response.text()
    return parse_last_change_response(body)


def parse_last_change_response(soap_xml: str | None) -> dict[str, str]:
    """
    Parse the SOAP response to a ``LastChange`` query into ``{state variable: value}``.

    Malformed or empty XML yields an empty dict (never raises).

    :param soap_xml: The QueryStateVariable response body.
    """
    if not soap_xml or not soap_xml.strip():
        return {}
    try:
        root = DefusedET.fromstring(soap_xml)
    except DefusedET.ParseError, ValueError:
        return {}
    # The UPnP specification puts the value in a <return> element; Raumfeld renderers name
    # it after the variable instead (measured), so accept either.
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] in ("return", "LastChange"):
            return parse_last_change(element.text)
    return {}


def parse_duration(value: str | None) -> int | None:
    """
    Parse a UPnP ``H:MM:SS`` (or ``H:MM:SS.mmm``) duration string into whole seconds.

    :param value: The duration string, or None.
    """
    if not value or value.strip().upper() in ("", "NOT_IMPLEMENTED"):
        return None
    # tolerant wrapper around the shared parser (which raises on non-numeric input)
    try:
        return int(try_parse_duration(value.strip()))
    except ValueError, TypeError:
        return None


def parse_line_in(didl_xml: str | None) -> dict[str, tuple[str, str]]:
    """
    Parse a Line-In DIDL-Lite listing into ``{renderer_uuid: (stream_url, title)}``.

    :param didl_xml: The DIDL-Lite XML returned by browsing the Line-In container.
    """
    result: dict[str, tuple[str, str]] = {}
    if not didl_xml or not didl_xml.strip():
        return result
    try:
        root = DefusedET.fromstring(didl_xml)
    except DefusedET.ParseError, ValueError:
        return result
    for item in root.findall(f".//{_DIDL}item"):
        res = item.find(f"{_DIDL}res")
        # the item id is like "0/Line In/uuid%3A<renderer-uuid>"; the renderer uuid is
        # the same one the player exposes as its UUID identifier
        item_id = unquote(item.get("id", ""))
        if "uuid:" not in item_id or res is None or not (res.text or "").strip():
            continue
        uuid = item_id.split("uuid:", 1)[1].strip().lower()
        title = item.find(f"{_DC}title")
        name = title.text if title is not None and title.text else "Line-in"
        result[uuid] = (res.text.strip(), name)
    return result


def _parse_description_field(description_xml: str | None, field: str) -> str | None:
    """
    Return the text of a root-device field in a UPnP device description, or ``None``.

    :param description_xml: The device description XML.
    :param field: The element name to look up, without namespace.
    """
    if not description_xml or not description_xml.strip():
        return None
    try:
        root = DefusedET.fromstring(description_xml)
    except DefusedET.ParseError, ValueError:
        return None
    # the description is namespaced (urn:schemas-upnp-org:device-1-0); the root device's
    # own fields come first, ahead of any embedded devices
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == field:
            return (element.text or "").strip() or None
    return None


def parse_library_items(didl_xml: str | None) -> list[tuple[str, str, bool]]:
    """
    Parse a DIDL-Lite listing of the host's library into ``(object id, title, container)``.

    Malformed or empty XML yields an empty list (never raises).

    :param didl_xml: The DIDL-Lite XML returned by browsing the host's media server.
    """
    if not didl_xml or not didl_xml.strip():
        return []
    try:
        root = DefusedET.fromstring(didl_xml)
    except DefusedET.ParseError, ValueError:
        return []
    result: list[tuple[str, str, bool]] = []
    for element in root:
        kind = element.tag.rsplit("}", 1)[-1]
        if kind not in ("item", "container") or not (object_id := element.get("id")):
            continue
        title = element.find(f"{_DC}title")
        name = title.text if title is not None and title.text else object_id
        result.append((object_id, name, kind == "container"))
    return result


def library_uri(media_server_udn: str, object_id: str, container: bool) -> str:
    """
    Return the transport URI that has the host play an item of its own library.

    :param media_server_udn: The UDN of the host's media server.
    :param object_id: The library object to play.
    :param container: Whether the object is a container (an album, a playlist).
    """
    server = quote(media_server_udn, safe="")
    service = quote("urn:upnp-org:serviceId:ContentDirectory", safe="")
    if container:
        return f"dlna-playcontainer://{server}?sid={service}&cid={quote(object_id, safe='')}&md=0"
    return f"dlna-playsingle://{server}?sid={service}&iid={quote(object_id, safe='')}"


def container_object_id(uri: str | None) -> str | None:
    """
    Return the library album or playlist a ``dlna-playcontainer://`` URI plays, or ``None``.

    :param uri: The transport URI the zone has loaded.
    """
    if not uri or not uri.startswith("dlna-playcontainer://"):
        return None
    container_ids = parse_qs(urlsplit(uri).query).get("cid")
    return container_ids[0] if container_ids else None


def parse_play_mode(play_mode: str | None) -> tuple[bool | None, RepeatMode]:
    """
    Return the shuffle state and repeat mode a Raumfeld play mode stands for.

    :param play_mode: The renderer's play mode, such as ``SHUFFLE`` or ``REPEAT_ALL``.
    """
    return _PLAY_MODES.get(play_mode or "", (None, RepeatMode.UNKNOWN))


def play_mode_for(shuffle: bool, repeat: RepeatMode) -> str:
    """
    Return the Raumfeld play mode for a shuffle state and repeat mode.

    The renderer has no mode that shuffles and repeats one track; repeating one wins.

    :param shuffle: Whether to play shuffled.
    :param repeat: The repeat mode.
    """
    if repeat == RepeatMode.ONE:
        return "REPEAT_ONE"
    if shuffle:
        return "RANDOM" if repeat == RepeatMode.ALL else "SHUFFLE"
    return "REPEAT_ALL" if repeat == RepeatMode.ALL else "NORMAL"


# the play modes as the Raumfeld app sets and the renderers report them (measured):
# RANDOM is shuffle and repeat all together
_PLAY_MODES: dict[str, tuple[bool | None, RepeatMode]] = {
    "NORMAL": (False, RepeatMode.OFF),
    "SHUFFLE": (True, RepeatMode.OFF),
    "REPEAT_ALL": (False, RepeatMode.ALL),
    "RANDOM": (True, RepeatMode.ALL),
    "REPEAT_ONE": (False, RepeatMode.ONE),
}
