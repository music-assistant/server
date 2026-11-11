"""Client."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast
from xml.etree.ElementTree import Element, ParseError

from aiohttp import ClientError, ClientResponseError, WSMsgType
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from music_assistant.providers.bose_soundtouch.client.const import (
    NOTIFICATION_PORT,
    RECONNECT_DELAY,
    STRING_ENCODING,
    WS_HEARTBEAT,
    WS_SUBPROTOCOLS,
)
from music_assistant.providers.bose_soundtouch.client.exceptions import (
    ApiError,
    NotFoundError,
    SoundtouchError,
)
from music_assistant.providers.bose_soundtouch.client.schema.enums import Key, KeyState
from music_assistant.providers.bose_soundtouch.client.schema.models import (
    Bass,
    BassCapabilities,
    Info,
    NowPlaying,
    Presets,
    Sources,
    Volume,
    Zone,
)

from .session_configuration import SessionConfiguration

if TYPE_CHECKING:
    from aiohttp.client import ClientResponse


def xml_escape(value: str) -> str:
    """Escape xml."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_string(string: str) -> str | bool | int:
    """Parse a string value."""
    if string.lower() == "true":
        return True
    if string.lower() == "false":
        return False

    with suppress(ValueError):
        return int(string)

    return string


def create_zone_xml(zone: Zone, sender_ip: str | None = None) -> str:
    """Create zone xml."""
    assert zone.leader
    assert zone.leader.mac is not None

    if sender_ip:
        xml = f'<zone master="{zone.leader.mac}" senderIPAddress="{sender_ip}">'
    else:
        xml = f'<zone master="{zone.leader.mac}">'
    for member in zone.members:
        xml += f'<member ipaddress="{member.ip}">{member.mac}</member>'
    xml += "</zone>"
    return xml


def create_notification_xml(app_key: str, url: str, volume: int | None = None) -> str:
    """Create xml used for notifications."""
    volume_xml = f"<volume>{volume}</volume>" if volume is not None else ""
    return (
        "<play_info>"
        f"<app_key>{xml_escape(app_key)}</app_key>"
        f"<url>{xml_escape(url)}</url>"
        "<service>Music Assistant</service>"
        "<reason>Music Assistant</reason>"
        f"{volume_xml}"
        "</play_info>"
    )


class SoundtouchDevice:
    """SoundtouchDevice."""

    def __init__(self, session_configuration: SessionConfiguration) -> None:
        """Initialize."""
        self.session_config = session_configuration
        self._now_playing_endpoint: str | None = None

        if self.session_config.logger is None:
            self.logger = logging.getLogger(__name__)
            logging.basicConfig()
            self.logger.setLevel(logging.DEBUG)
        else:
            self.logger = self.session_config.logger

    async def press_key(self, key: Key) -> None:
        """Press a key."""
        body_press = f'<key state="{KeyState.PRESS}" sender="Gabbo">{key}</key>'
        body_release = f'<key state="{KeyState.RELEASE}" sender="Gabbo">{key}</key>'
        await self._post("key", body_press)
        await self._post("key", body_release)

    async def get_sources(self) -> Sources:
        """Get sources."""
        element = await self._get("sources")
        return Sources.from_dict(
            {"sources": [{**x.attrib, "source_name": x.text} for x in element]}
        )

    async def select_source(self, source: str, source_account: str | None = None) -> None:
        """Select a source on the speaker."""
        account = f' sourceAccount="{source_account}"' if source_account else ""
        await self._post("select", f'<ContentItem source="{source}"{account}></ContentItem>')

    async def get_bass_capabilities(self) -> BassCapabilities:
        """Get bass capabilities."""
        element = await self._get("bassCapabilities")
        return BassCapabilities.from_dict(
            {x.tag: x.text if x.text is None else parse_string(x.text) for x in element}
        )

    async def get_bass(self) -> Bass:
        """Get bass if supported (BassCapabilities to verify!)."""
        element = await self._get("bass")
        return Bass.from_dict(
            {x.tag: x.text if x.text is None else parse_string(x.text) for x in element}
        )

    async def set_bass(self, value: int) -> None:
        """Set bass if supported (BassCapabilities to verify!)."""
        await self._post("bass", f"<bass>{value}</bass>")

    async def get_zone(self) -> Zone:
        """Get zone."""
        element = await self._get("getZone")
        return Zone.from_dict(
            {
                "leader": {"mac": element.attrib.get("master")},
                "members": [{**x.attrib, "mac": x.text} for x in element],
            }
        )

    async def set_zone(self, zone: Zone, sender_ip: str | None = None) -> None:
        """Set zone."""
        if (
            zone.leader is None
            or zone.leader.mac is None
            or zone.leader.ip is None
            or not zone.members
            or any(member.mac is None for member in zone.members)
            or any(member.ip is None for member in zone.members)
        ):
            raise SoundtouchError("Zone information is incomplete.")

        # verify, that leader is part of member, and move to first position if necessary
        leader = next((member for member in zone.members if member.mac == zone.leader.mac), None)
        if leader is None:
            zone.members.insert(0, zone.leader)
        leader_index = zone.members.index(zone.leader)
        if leader_index != 0:
            zone.members.pop(leader_index)
            zone.members.insert(0, zone.leader)
        await self._post("setZone", create_zone_xml(zone, sender_ip))

    async def add_zone_members(self, zone: Zone) -> None:
        """Add zone.members to a zone."""
        await self._add_or_remove_zone_members(zone, add_members=True)

    async def remove_zone_members(self, zone: Zone) -> None:
        """Remove zone.members from a zone."""
        await self._add_or_remove_zone_members(zone, add_members=False)

    async def get_now_playing(self) -> NowPlaying:
        """Get now playing."""
        if self._now_playing_endpoint is None:
            # firmware differs in which spelling it serves, so probe once and remember it:
            # now playing is refreshed on every poll and on every push notification
            try:
                element = await self._get("nowPlaying")
                self._now_playing_endpoint = "nowPlaying"
            except NotFoundError:
                element = await self._get("now_playing")
                self._now_playing_endpoint = "now_playing"
        else:
            element = await self._get(self._now_playing_endpoint)
        d: dict[str, Any] = element.attrib
        for el in element:
            if el.tag == "ContentItem":
                item_name: str | None = None
                for sub_el in el.iter():
                    if sub_el.tag == "itemName":
                        item_name = sub_el.text
                d["content_item"] = {
                    **el.attrib,
                    "sourceAccount": el.attrib.get("sourceAccount")
                    or element.attrib.get("sourceAccount"),
                    "item_name": item_name,
                }
            elif el.tag == "time":
                d["time_information"] = {
                    "total": parse_string(el.get("total", "-1")),
                    "position": None if el.text is None else parse_string(el.text),
                }
            elif el.tag == "art":
                d["art"] = {"status": el.get("artImageStatus"), "url": el.text}
            else:
                d[el.tag] = el.text

        return NowPlaying.from_dict(d)

    async def get_volume(self) -> Volume:
        """Get volume."""
        element = await self._get("volume")
        return Volume.from_dict(
            {x.tag: x.text if x.text is None else parse_string(x.text) for x in element}
        )

    async def set_volume(self, volume: int, *, mute: bool) -> None:
        """Set volume."""
        mute_str = "true" if mute else "false"
        xml = f"<volume>{volume}<muteenabled>{mute_str}</muteenabled></volume>"
        await self._post("volume", xml)

    async def get_presets(self) -> Presets:
        """Get presets."""
        element = await self._get("presets")
        presets_list: list[dict[str, Any]] = []
        for el in element:
            preset_dict: dict[str, Any] = el.attrib
            for sub_el in el:
                if sub_el.tag == "ContentItem":
                    item_name: str | None = None
                    for sub_sub_el in sub_el.iter():
                        if sub_sub_el.tag == "itemName":
                            item_name = sub_sub_el.text
                    preset_dict["content_item"] = {**sub_el.attrib, "item_name": item_name}
            presets_list.append(preset_dict)
        return Presets.from_dict({"presets": presets_list})

    async def store_preset(self, preset_id: int, preset_url: str) -> None:
        """Store a preset."""
        xml = (
            f'<preset id="{preset_id}">'
            '<ContentItem source="LOCAL_INTERNET_RADIO" '
            f'type="stationurl" location="{preset_url}" sourceAccount="" isPresetable="true">'
            f"<itemName>Music Assistant Preset {preset_id}</itemName>"
            "</ContentItem>"
            "</preset>"
        )
        await self._post("storePreset", xml)

    async def get_info(self) -> Info:
        """Get info necessary for us."""
        response = await self._get("info")
        interfaces = [
            (network_info.findtext("macAddress"), network_info.findtext("ipAddress"))
            for network_info in response.findall("networkInfo")
        ]
        # a speaker reports one entry per interface (wired and wireless). Put the one we
        # actually talk to first: callers take the first as the device identifier, and an
        # identifier that changes per run makes protocol linking a coin flip.
        interfaces.sort(key=lambda interface: interface[1] != self.session_config.ip)
        mac_addresses = list(dict.fromkeys(mac for mac, _ in interfaces if mac))
        ip_addresses = list(dict.fromkeys(ip for _, ip in interfaces if ip))
        software_version: str | None = None
        for component in response.iter("component"):
            if version := component.findtext("softwareVersion"):
                software_version = version.split(" ", 1)[0]
                break

        # our connection ip should already be present, but just in case
        if self.session_config.ip not in ip_addresses:
            ip_addresses.insert(0, self.session_config.ip)

        return Info(
            device_id=response.attrib.get("deviceID", ""),
            name=response.findtext("name") or "Bose SoundTouch",
            model=response.findtext("type"),
            mac_addresses=mac_addresses,
            ip_addresses=ip_addresses,
            software_version=software_version,
        )

    async def set_name(self, name: str) -> None:
        """Set Name."""
        xml = f"<name>{name}</name>"
        await self._post("name", xml)

    async def play_notification(self, app_key: str, url: str, volume: int | None = None) -> None:
        """Plays notification as in previous client."""
        xml = create_notification_xml(app_key, url, volume)
        await self._post("speaker", xml)

    async def websocket_notification_loop(
        self, on_connect: Callable[[], None] | None = None
    ) -> AsyncGenerator[str]:
        """
        Yield the speaker's push notifications as raw xml, reconnecting as needed.

        Runs until the consumer stops iterating, so the caller owns the task and ends the
        stream by cancelling it or closing the iterator.

        :param on_connect: Called every time the channel is (re)established.
        """
        while True:
            # read the address on every attempt: it can change while we are connected
            uri = f"ws://{self.session_config.ip}:{NOTIFICATION_PORT}"
            try:
                async with self.session_config.session.ws_connect(
                    uri, protocols=WS_SUBPROTOCOLS, heartbeat=WS_HEARTBEAT
                ) as websocket:
                    self.logger.debug("Connected to SoundTouch websocket: %s", uri)
                    if on_connect is not None:
                        on_connect()
                    async for msg in websocket:
                        if msg.type == WSMsgType.TEXT:
                            yield msg.data
                        elif msg.type == WSMsgType.BINARY:
                            yield msg.data.decode(STRING_ENCODING)
                        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                            break
            except (ClientError, OSError, TimeoutError, UnicodeDecodeError) as err:
                self.logger.debug(
                    "SoundTouch websocket error for %s: %s. Reconnecting in %ss",
                    self.session_config.ip,
                    err,
                    RECONNECT_DELAY,
                )
            await asyncio.sleep(RECONNECT_DELAY)

    async def _add_or_remove_zone_members(self, zone: Zone, *, add_members: bool = True) -> None:
        """Add or remove members to a zone."""
        if (
            zone.leader is None
            or zone.leader.mac is None
            or any(member.mac is None for member in zone.members)
            or any(member.ip is None for member in zone.members)
        ):
            raise SoundtouchError("Zone information is incomplete.")
        if add_members:
            await self._post("addZoneSlave", create_zone_xml(zone))
            return
        await self._post("removeZoneSlave", create_zone_xml(zone))

    async def _get(self, endpoint: str, params: dict[str, str | int] | None = None) -> Element[str]:
        """GET request to api."""
        # the context manager releases the connection back to the pool on every path,
        # including the error ones - an unread response would be torn down instead
        async with self.session_config.session.get(
            f"http://{self.session_config.ip}:{self.session_config.http_port}/{endpoint}",
            params=params,
            timeout=self.session_config.timeout,
        ) as response:
            if response.status == 404:
                raise NotFoundError
            if response.content_type != "text/xml" or response.status != 200:
                raise ApiError(f"API GET call to {endpoint} failed.")
            body = (await response.read()).decode(STRING_ENCODING)

        try:
            return cast("Element[str]", ElementTree.fromstring(body))
        except (ParseError, DefusedXmlException) as exc:
            # the speakers emit truncated xml when they are under load; an ApiError keeps
            # that inside the aiohttp.ClientError hierarchy every caller already handles
            raise ApiError(f"API GET call to {endpoint} returned malformed xml.") from exc

    async def _post(
        self,
        endpoint: str,
        data: str | None = None,
    ) -> str:
        """POST request to api."""

        async def _request() -> ClientResponse:
            return await self.session_config.session.post(
                f"http://{self.session_config.ip}:{self.session_config.http_port}/{endpoint}",
                data=data.encode(STRING_ENCODING) if data else None,
                raise_for_status=True,
                timeout=self.session_config.timeout,
            )

        try:
            response = await _request()
        except ClientResponseError as exc:
            if exc.code == 404:
                raise NotFoundError from exc
            raise ApiError(f"API POST call to {endpoint} failed.") from exc

        return (await response.read()).decode(STRING_ENCODING)
