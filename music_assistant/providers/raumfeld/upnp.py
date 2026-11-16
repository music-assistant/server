"""UPnP calls to the devices of a Teufel Raumfeld system."""

from __future__ import annotations

from typing import TYPE_CHECKING

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory

from .constants import UPNP_TIMEOUT

if TYPE_CHECKING:
    import aiohttp
    from async_upnp_client.client import UpnpAction

SERVICE_AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
SERVICE_RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
SERVICE_CONTENT_DIRECTORY = "urn:schemas-upnp-org:service:ContentDirectory:1"
SERVICE_SETUP = "urn:schemas-raumfeld-com:service:SetupService:1"

# The host lists the children of these library objects only to the Raumfeld app.
_RAUMFELD_APP_OBJECTS = ("0/RadioTime", "0/Tidal")
_RAUMFELD_APP_HEADERS = {"User-Agent": "RaumfeldControl/3.10 RaumfeldProtocol"}


async def get_action(
    session: aiohttp.ClientSession,
    location: str,
    service: str,
    action_name: str,
    http_headers: dict[str, str] | None = None,
) -> UpnpAction:
    """
    Return an action of a device's service, read from the device's descriptions.

    :param session: The HTTP session to use.
    :param location: The device description URL.
    :param service: The service type that has the action.
    :param action_name: The name of the action.
    :param http_headers: Extra headers to send with the requests.
    :raises KeyError: When the device has no such service or action.
    """
    requester = AiohttpSessionRequester(session, timeout=UPNP_TIMEOUT, http_headers=http_headers)
    device = await UpnpFactory(requester).async_create_device(location)
    return device.service(service).action(action_name)


async def browse(
    session: aiohttp.ClientSession, location: str, object_id: str, children: bool
) -> str | None:
    """
    Browse a media server, returning its DIDL-Lite, or ``None`` if it gave none.

    :param session: The HTTP session to use.
    :param location: The device description URL of the media server.
    :param object_id: The library object to browse.
    :param children: Whether to list its children rather than describe it.
    """
    headers = _RAUMFELD_APP_HEADERS if children and object_id in _RAUMFELD_APP_OBJECTS else None
    action = await get_action(session, location, SERVICE_CONTENT_DIRECTORY, "Browse", headers)
    response = await action.async_call(
        ObjectID=object_id,
        BrowseFlag="BrowseDirectChildren" if children else "BrowseMetadata",
        Filter="*",
        StartingIndex=0,
        RequestedCount=0,
        SortCriteria="",
    )
    if "Result" not in response or (result := action.argument("Result")) is None:
        return None
    # the DIDL-Lite as the server sent it
    return result.raw_upnp_value


async def get_software_version(session: aiohttp.ClientSession, location: str) -> str | None:
    """
    Return the software version of a speaker, or ``None`` if it did not say.

    :param session: The HTTP session to use.
    :param location: The description URL of the speaker's Raumfeld device.
    """
    action = await get_action(session, location, SERVICE_SETUP, "GetInfo")
    response = await action.async_call()
    version = response.get("SoftwareVersion")
    return str(version) if version else None
