"""
Lightweight async client for the Bose SoundTouch local API.

SoundTouch speakers expose a simple XML-over-HTTP API on port 8090 for control
and status, plus a websocket notification channel on port 8080 for push updates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from aiohttp import ClientTimeout
from defusedxml import ElementTree as DefusedET

from .const import API_PORT, BUFFERING_STATE, PAUSE_STATE, PLAY_STATE, REQUEST_TIMEOUT

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

    from aiohttp import ClientSession


@dataclass
class SoundTouchInfo:
    """Static device information from the /info endpoint."""

    device_id: str
    name: str
    model: str | None = None
    mac_address: str | None = None
    ip_address: str | None = None
    software_version: str | None = None


@dataclass
class SoundTouchNowPlaying:
    """Current playback state from the /now_playing endpoint."""

    source: str
    source_account: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    image_url: str | None = None
    duration: int | None = None
    position: int | None = None
    play_status: str | None = None


@dataclass
class SoundTouchVolume:
    """Volume state from the /volume endpoint."""

    level: int = 0
    muted: bool = False


@dataclass
class SoundTouchSource:
    """A selectable source from the /sources endpoint."""

    source: str
    source_account: str | None
    name: str
    ready: bool


@dataclass
class SoundTouchZone:
    """Zone (multiroom) state from the /getZone endpoint."""

    master_id: str | None = None
    member_ids: list[str] = field(default_factory=list)


class SoundTouchClient:
    """Async client for a single Bose SoundTouch speaker's local API."""

    def __init__(self, session: ClientSession, ip_address: str) -> None:
        """
        Initialize the client.

        :param session: The shared aiohttp client session to use for requests.
        :param ip_address: The IP address of the SoundTouch speaker.
        """
        self._session = session
        self.ip_address = ip_address

    async def get_info(self) -> SoundTouchInfo:
        """Return static device information."""
        return parse_info(await self._get("info"), self.ip_address)

    async def get_now_playing(self) -> SoundTouchNowPlaying:
        """Return the current playback state."""
        return parse_now_playing(await self._get("now_playing"))

    async def get_volume(self) -> SoundTouchVolume:
        """Return the current volume state."""
        return parse_volume(await self._get("volume"))

    async def set_volume(self, level: int) -> None:
        """Set the absolute volume level (0..100)."""
        level = max(0, min(100, level))
        await self._post("volume", f"<volume>{level}</volume>")

    async def press_key(self, key: str) -> None:
        """Send a key press (press followed by release) to the speaker."""
        body_press = f'<key state="press" sender="Gabbo">{key}</key>'
        body_release = f'<key state="release" sender="Gabbo">{key}</key>'
        await self._post("key", body_press)
        await self._post("key", body_release)

    async def get_sources(self) -> list[SoundTouchSource]:
        """Return the list of sources available on the speaker."""
        return parse_sources(await self._get("sources"))

    async def select_source(self, source: str, source_account: str | None = None) -> None:
        """Select a source on the speaker."""
        account = f' sourceAccount="{source_account}"' if source_account else ""
        await self._post("select", f'<ContentItem source="{source}"{account}></ContentItem>')

    async def play_notification(self, app_key: str, url: str, volume: int | None = None) -> None:
        """
        Play an audio notification (announcement) on the speaker.

        Uses the Bose audio notification API which overlays the given URL on top of
        the current playback (ducking it) and resumes afterwards. Requires a Bose
        developer app key.
        """
        await self._post("speaker", _build_notification_xml(app_key, url, volume))

    async def get_zone(self) -> SoundTouchZone:
        """Return the current multiroom zone state."""
        return parse_zone(await self._get("getZone"))

    async def set_zone(self, master_id: str, members: list[tuple[str, str]]) -> None:
        """
        Create a multiroom zone with this speaker as master.

        :param master_id: The device id of the zone master.
        :param members: List of (device_id, ip_address) tuples for all zone members.
        """
        await self._post("setZone", _build_zone_xml(master_id, members))

    async def add_zone_slaves(self, master_id: str, members: list[tuple[str, str]]) -> None:
        """Add the given members to this speaker's existing zone."""
        await self._post("addZoneSlave", _build_zone_xml(master_id, members))

    async def remove_zone_slaves(self, master_id: str, members: list[tuple[str, str]]) -> None:
        """Remove the given members from this speaker's zone."""
        await self._post("removeZoneSlave", _build_zone_xml(master_id, members))

    async def _get(self, path: str) -> Element:
        url = f"http://{self.ip_address}:{API_PORT}/{path}"
        async with self._session.get(url, timeout=_TIMEOUT) as resp:
            resp.raise_for_status()
            return _parse_xml(await resp.text())

    async def _post(self, path: str, body: str) -> None:
        url = f"http://{self.ip_address}:{API_PORT}/{path}"
        async with self._session.post(url, data=body, timeout=_TIMEOUT) as resp:
            resp.raise_for_status()


def parse_info(root: Element, fallback_ip: str | None = None) -> SoundTouchInfo:
    """Parse a /info response into a SoundTouchInfo."""
    mac_address: str | None = None
    ip_address: str | None = None
    for network_info in root.findall("networkInfo"):
        if (mac := network_info.findtext("macAddress")) and not mac_address:
            mac_address = mac
        if (ip := network_info.findtext("ipAddress")) and not ip_address:
            ip_address = ip
    software_version: str | None = None
    for component in root.iter("component"):
        if version := component.findtext("softwareVersion"):
            software_version = version.split(" ", 1)[0]
            break
    return SoundTouchInfo(
        device_id=root.attrib.get("deviceID", ""),
        name=root.findtext("name") or "Bose SoundTouch",
        model=root.findtext("type"),
        mac_address=mac_address,
        ip_address=ip_address or fallback_ip,
        software_version=software_version,
    )


def parse_now_playing(root: Element) -> SoundTouchNowPlaying:
    """Parse a /now_playing response into a SoundTouchNowPlaying."""
    art_url: str | None = None
    if (art := root.find("art")) is not None and art.text:
        if art.attrib.get("artImageStatus") == "IMAGE_PRESENT":
            art_url = art.text
    duration: int | None = None
    position: int | None = None
    if (time_elem := root.find("time")) is not None:
        duration = _int_or_none(time_elem.attrib.get("total"))
        position = _int_or_none(time_elem.text)
    return SoundTouchNowPlaying(
        source=root.attrib.get("source", ""),
        source_account=root.attrib.get("sourceAccount") or None,
        title=root.findtext("track") or root.findtext("stationName"),
        artist=root.findtext("artist"),
        album=root.findtext("album"),
        image_url=art_url,
        duration=duration,
        position=position,
        play_status=root.findtext("playStatus"),
    )


def parse_volume(root: Element) -> SoundTouchVolume:
    """Parse a /volume response into a SoundTouchVolume."""
    return SoundTouchVolume(
        level=_int_or_none(root.findtext("actualvolume")) or 0,
        muted=(root.findtext("muteenabled") or "").lower() == "true",
    )


def parse_sources(root: Element) -> list[SoundTouchSource]:
    """Parse a /sources response into a list of SoundTouchSource."""
    sources: list[SoundTouchSource] = []
    for item in root.findall("sourceItem"):
        source = item.attrib.get("source", "")
        if not source:
            continue
        sources.append(
            SoundTouchSource(
                source=source,
                source_account=item.attrib.get("sourceAccount") or None,
                name=(item.text or source).strip(),
                ready=item.attrib.get("status") == "READY",
            )
        )
    return sources


def parse_zone(root: Element) -> SoundTouchZone:
    """Parse a /getZone response into a SoundTouchZone."""
    master_id = root.attrib.get("master")
    member_ids = [member.text for member in root.findall("member") if member.text]
    return SoundTouchZone(master_id=master_id or None, member_ids=member_ids)


def play_status_is_playing(play_status: str | None) -> bool:
    """Return whether the given playStatus represents active playback."""
    return play_status in (PLAY_STATE, BUFFERING_STATE)


def play_status_is_paused(play_status: str | None) -> bool:
    """Return whether the given playStatus represents paused playback."""
    return play_status == PAUSE_STATE


def extract_preset_id(message: str) -> int | None:
    """
    Extract a Bose SoundTouch favorite/preset id from a websocket XML message.

    Detects a physical preset button press from the speaker's notification channel.
    """
    try:
        root = DefusedET.fromstring(message)
    except DefusedET.ParseError:
        return None

    preset_id = next(
        (
            element.attrib.get("id")
            for element in root.iter()
            if _local_name(element.tag) == "preset"
        ),
        None,
    )

    try:
        return int(preset_id) if preset_id else None
    except TypeError, ValueError:
        return None


def _parse_xml(payload: str) -> Element:
    return cast("Element", DefusedET.fromstring(payload))


def _local_name(tag: str) -> str:
    """Return an XML tag name without its namespace prefix."""
    return tag.rsplit("}", 1)[-1]


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError, TypeError:
        return None


def _build_zone_xml(master_id: str, members: list[tuple[str, str]]) -> str:
    member_xml = "".join(
        f'<member ipaddress="{ip_address}">{device_id}</member>'
        for device_id, ip_address in members
    )
    return f'<zone master="{master_id}">{member_xml}</zone>'


def _build_notification_xml(app_key: str, url: str, volume: int | None = None) -> str:
    volume_xml = f"<volume>{volume}</volume>" if volume is not None else ""
    return (
        "<play_info>"
        f"<app_key>{_xml_escape(app_key)}</app_key>"
        f"<url>{_xml_escape(url)}</url>"
        "<service>Music Assistant</service>"
        "<reason>Music Assistant</reason>"
        f"{volume_xml}"
        "</play_info>"
    )


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TIMEOUT = ClientTimeout(total=REQUEST_TIMEOUT)
