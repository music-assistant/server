"""Commands to the web service of a Teufel Raumfeld host."""

from __future__ import annotations

import aiohttp

from .constants import HOST_COMMAND_TIMEOUT


async def connect_rooms_to_zone(
    session: aiohttp.ClientSession, host_url: str, room_udns: list[str]
) -> None:
    """
    Have the host put rooms in a zone of their own.

    :param session: The HTTP session to use.
    :param host_url: The base URL of the host's web service.
    :param room_udns: The UDNs of the rooms of the new zone.
    :raises ValueError: When no rooms are given; the host would then take every room.
    """
    if not room_udns:
        raise ValueError("A zone needs at least one room")
    await _command(session, host_url, "connectRoomsToZone", roomUDNs=",".join(room_udns))


async def connect_room_to_zone(
    session: aiohttp.ClientSession, host_url: str, room_udn: str, zone_udn: str
) -> None:
    """
    Have the host add a room to an existing zone.

    :param session: The HTTP session to use.
    :param host_url: The base URL of the host's web service.
    :param room_udn: The UDN of the room to add.
    :param zone_udn: The UDN of the zone to add it to.
    """
    await _command(session, host_url, "connectRoomToZone", roomUDN=room_udn, zoneUDN=zone_udn)


async def drop_room(session: aiohttp.ClientSession, host_url: str, room_udn: str) -> None:
    """
    Have the host take a room out of its zone.

    :param session: The HTTP session to use.
    :param host_url: The base URL of the host's web service.
    :param room_udn: The UDN of the room to take out.
    """
    await _command(session, host_url, "dropRoomJob", roomUDN=room_udn)


async def enter_manual_standby(
    session: aiohttp.ClientSession, host_url: str, room_udn: str
) -> None:
    """
    Have the host switch a room off.

    :param session: The HTTP session to use.
    :param host_url: The base URL of the host's web service.
    :param room_udn: The UDN of the room to switch off.
    """
    await _command(session, host_url, "enterManualStandby", roomUDN=room_udn)


async def leave_standby(session: aiohttp.ClientSession, host_url: str, room_udn: str) -> None:
    """
    Have the host wake a room from standby.

    :param session: The HTTP session to use.
    :param host_url: The base URL of the host's web service.
    :param room_udn: The UDN of the room to wake.
    """
    await _command(session, host_url, "leaveStandby", roomUDN=room_udn)


async def _command(
    session: aiohttp.ClientSession, host_url: str, command: str, **params: str
) -> None:
    """Send a command to the host's web service, raising when it does not take it."""
    # the host answers with a redirect to a session path first, which aiohttp follows
    async with session.get(
        f"{host_url}/{command}",
        params=params,
        timeout=aiohttp.ClientTimeout(total=HOST_COMMAND_TIMEOUT),
    ) as response:
        response.raise_for_status()
