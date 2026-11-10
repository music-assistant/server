"""
UPnP of the Sonos speakers: the settings the websocket api cannot change, and their events.

The websocket api reads most settings but sets only the home theater options, and sends no
event when a setting changes. The UPnP services of a speaker (port 1400) do both. Events are
received on Music Assistant's stream server, which the speakers reach anyway to fetch the audio.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
import defusedxml.ElementTree as DefusedET
from aiohttp.web import Request, Response
from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.const import HttpRequest
from async_upnp_client.event_handler import UpnpEventHandler, UpnpNotifyServer
from async_upnp_client.exceptions import UpnpError

from .const import (
    EVENT_SUBSCRIPTION_TIMEOUT,
    EVENTS_PATH,
    RENDERING_CONTROL_SERVICE,
    UPNP_PORT,
    UPNP_TIMEOUT,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from async_upnp_client.client import UpnpDevice, UpnpService, UpnpStateVariable

    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)
# a timeout is a TimeoutError (UpnpConnectionTimeoutError included)
UPNP_ERRORS = (UpnpError, aiohttp.ClientError, OSError, TimeoutError, KeyError)

ChangeCallback = Callable[[dict[str, str]], None]


class SonosUpnp(UpnpNotifyServer):  # type: ignore[misc,unused-ignore]
    """The UPnP of all Sonos speakers, with their events received on the stream server."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Start receiving events; a speaker is reached with ``speaker``.

        :param mass: The Music Assistant instance.
        """
        self._mass = mass
        requester = AiohttpSessionRequester(mass.http_session_no_ssl, timeout=UPNP_TIMEOUT)
        # a speaker's service descriptions are not always strict UPnP
        self.factory = UpnpFactory(requester, non_strict=True)
        self.event_handler = UpnpEventHandler(self, requester)
        self._unregister = mass.streams.register_dynamic_route(
            EVENTS_PATH, self._handle_request, method="NOTIFY"
        )

    @property
    def callback_url(self) -> str:
        """Return the URL the speakers send their events to."""
        return f"{self._mass.streams.base_url}{EVENTS_PATH}"

    def speaker(self, player_ip: str) -> SonosSpeakerUpnp:
        """
        Return the UPnP of a speaker.

        :param player_ip: The speaker's IP address.
        """
        return SonosSpeakerUpnp(self, player_ip)

    def close(self) -> None:
        """Stop receiving events; the speakers end their own subscriptions on unload."""
        self._unregister()

    async def _handle_request(self, request: Request) -> Response:
        # a speaker can send a name in another encoding than it says; decode leniently
        body = (await request.read()).decode("utf-8", errors="replace")
        http_request = HttpRequest(
            method=request.method, url=str(request.url), headers=request.headers, body=body
        )
        try:
            status = await self.event_handler.handle_notify(http_request)
        except (DefusedET.ParseError, ValueError) as err:
            LOGGER.debug("Ignoring an unreadable event from %s: %r", request.remote, err)
            return Response(status=400)
        return Response(status=status)


class SonosSpeakerUpnp:
    """
    The UPnP services of one speaker: action calls and the RenderingControl events.

    The speaker counts as followed only once its first event came in. Until then, and after
    the subscription is lost, its settings have to be polled.
    """

    def __init__(self, upnp: SonosUpnp, player_ip: str) -> None:
        """
        Initialize; the speaker's description is read on first use.

        :param upnp: The UPnP of all speakers.
        :param player_ip: The speaker's IP address.
        """
        self._upnp = upnp
        self.location = f"http://{player_ip}:{UPNP_PORT}/xml/device_description.xml"
        self._device: UpnpDevice | None = None
        self._device_lock = asyncio.Lock()
        # kept here because the event handler holds the service only weakly
        self._events_service: UpnpService | None = None
        self._renew_at = 0.0
        self._events_live = False
        # one subscribe/renew at a time
        self._events_lock = asyncio.Lock()

    @property
    def follows(self) -> bool:
        """Return whether the speaker's RenderingControl events come in."""
        return self._events_service is not None and self._events_live

    async def call(self, service_type: str, action: str, **arguments: Any) -> Mapping[str, Any]:
        """
        Call a UPnP action of the speaker and return its output arguments.

        Raises one of UPNP_ERRORS when the speaker lacks it or does not answer.

        :param service_type: The service, e.g. urn:schemas-upnp-org:service:RenderingControl:1.
        :param action: The action, e.g. GetBass.
        :param arguments: The input arguments as Python values.
        """
        service = await self._service(service_type)
        return await service.action(action).async_call(**arguments)

    async def rendering_control(self, action: str, **arguments: Any) -> Mapping[str, Any]:
        """
        Call a RenderingControl action (of instance 0) of the speaker.

        :param action: The action, e.g. SetBass.
        :param arguments: The input arguments other than InstanceID.
        """
        return await self.call(RENDERING_CONTROL_SERVICE, action, InstanceID=0, **arguments)

    async def follow(self, on_change: ChangeCallback) -> None:
        """
        Subscribe to the RenderingControl events, or renew the subscription when due.

        A subscription that fails is tried again on the next call.

        :param on_change: Called with the state variables an event changed.
        """
        async with self._events_lock:
            if self._events_service is None:
                await self._subscribe(on_change)
            elif self._renew_at <= time.time():
                await self._renew()

    async def unfollow(self) -> None:
        """End the subscription; a speaker that is gone keeps it until it runs out."""
        async with self._events_lock:
            if (service := self._events_service) is None:
                return
            self._events_service = None
            self._events_live = False
            try:
                await self._upnp.event_handler.async_unsubscribe(service)
            except UPNP_ERRORS as err:
                LOGGER.debug("Ending the events of %s failed: %r", self.location, err)

    async def _service(self, service_type: str) -> UpnpService:
        async with self._device_lock:
            if self._device is None:
                self._device = await self._upnp.factory.async_create_device(self.location)
        # e.g. RenderingControl sits in the MediaRenderer device embedded in the ZonePlayer
        if (service := self._device.find_service(service_type)) is None:
            msg = f"{self.location} has no {service_type}"
            raise UpnpError(msg)
        return service

    async def _subscribe(self, on_change: ChangeCallback) -> None:
        try:
            service = await self._service(RENDERING_CONTROL_SERVICE)
            service.on_event = lambda _service, variables: self._handle_event(variables, on_change)
            # set before subscribing: the first event can come in before the answer
            self._events_service = service
            self._renew_at = float("inf")
            _, timeout = await self._upnp.event_handler.async_subscribe(
                service, timeout=timedelta(seconds=EVENT_SUBSCRIPTION_TIMEOUT)
            )
        except UPNP_ERRORS as err:
            self._events_service = None
            LOGGER.debug("Subscribing to the events of %s failed: %r", self.location, err)
            return
        self._renew_at = time.time() + timeout.total_seconds() / 2
        LOGGER.debug("Subscribed to the events of %s", self.location)

    async def _renew(self) -> None:
        assert self._events_service is not None  # checked by follow
        try:
            _, timeout = await self._upnp.event_handler.async_resubscribe(
                self._events_service, timeout=timedelta(seconds=EVENT_SUBSCRIPTION_TIMEOUT)
            )
        except UPNP_ERRORS as err:
            # lost: made again on the next follow
            self._events_service = None
            self._events_live = False
            LOGGER.debug("Renewing the events of %s failed: %r", self.location, err)
            return
        self._renew_at = time.time() + timeout.total_seconds() / 2

    def _handle_event(
        self, variables: Sequence[UpnpStateVariable[Any]], on_change: ChangeCallback
    ) -> None:
        if self._events_service is None:
            return
        if not self._events_live:
            LOGGER.debug("Events of %s come in", self.location)
            self._events_live = True
        changes: dict[str, str] = {}
        for variable in variables:
            if variable.name == "LastChange":
                changes.update(parse_last_change(variable.upnp_value))
        if changes:
            on_change(changes)


def parse_last_change(last_change_xml: str | None) -> dict[str, str]:
    """
    Parse a RenderingControl LastChange event into {state variable: value}.

    A variable of a single channel other than Master is named with it, e.g. Volume_LF.
    Malformed or empty XML yields an empty dict.

    :param last_change_xml: The LastChange event XML.
    """
    if not last_change_xml or not last_change_xml.strip():
        return {}
    try:
        root = DefusedET.fromstring(last_change_xml)
    except DefusedET.ParseError, ValueError:
        return {}
    changes: dict[str, str] = {}
    for element in root.iter():
        if (value := element.get("val")) is None:
            continue
        name = element.tag.rsplit("}", 1)[-1]
        if (channel := element.get("channel")) and channel != "Master":
            name = f"{name}_{channel}"
        changes[name] = value
    return changes
