"""Client."""

import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast
from xml.etree.ElementTree import Element

from aiohttp import ClientResponseError
from defusedxml import ElementTree

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

    async def get_now_playing(self, endpoint: str = "nowPlaying") -> NowPlaying:
        """Get now playing."""
        try:
            element = await self._get(endpoint)
        except NotFoundError:
            # both cases seem to be supported by the API
            element = await self._get(endpoint="now_playing")
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

    async def get_track_info(self) -> NowPlaying:
        """Get track info."""
        return await self.get_now_playing("trackInfo")

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
        mac_addresses: set[str] = set()
        ip_addresses: set[str] = set()
        for network_info in response.findall("networkInfo"):
            if mac := network_info.findtext("macAddress"):
                mac_addresses.add(mac)
            if ip := network_info.findtext("ipAddress"):
                ip_addresses.add(ip)
        software_version: str | None = None
        for component in response.iter("component"):
            if version := component.findtext("softwareVersion"):
                software_version = version.split(" ", 1)[0]
                break

        # our connection ip should already be present, but just in case
        ip_addresses.add(self.session_config.ip)

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
        """GET request to abs api."""

        async def _request() -> ClientResponse:
            return await self.session_config.session.get(
                f"http://{self.session_config.ip}:{self.session_config.http_port}/{endpoint}",
                params=params,
                timeout=self.session_config.timeout,
            )

        response = await _request()

        status = response.status
        if status == 404:
            raise NotFoundError
        if response.content_type == "text/xml" and status == 200:
            _response = await response.read()
            return cast("Element[str]", ElementTree.fromstring(_response))
        raise ApiError(f"API GET call to {endpoint} failed.")

    async def _post(
        self,
        endpoint: str,
        data: str | None = None,
    ) -> bytes:
        """POST request to api."""

        async def _request() -> ClientResponse:
            return await self.session_config.session.post(
                f"http://{self.session_config.ip}:{self.session_config.http_port}/{endpoint}",
                data=data,
                raise_for_status=True,
                timeout=self.session_config.timeout,
            )

        try:
            response = await _request()
        except ClientResponseError as exc:
            if exc.code == 404:
                raise NotFoundError from exc
            raise ApiError(f"API POST call to {endpoint} failed.") from exc

        return await response.read()
