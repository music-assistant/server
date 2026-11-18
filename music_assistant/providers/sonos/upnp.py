"""
UPnP of the Sonos speakers: the settings the websocket api cannot change, and their events.

The websocket api reads most settings but sets only the home theater options, and sends no
event when a setting changes. The UPnP services of a speaker (port 1400) do both. Events are
received on Music Assistant's stream server, which the speakers reach anyway to fetch the audio.
"""

from __future__ import annotations

import asyncio
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
    UPNP_TIMEOUT_LIMIT,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Mapping, Sequence

    from async_upnp_client.client import UpnpDevice, UpnpService, UpnpStateVariable

    from music_assistant import MusicAssistant

# a timeout is a TimeoutError (UpnpConnectionTimeoutError included)
UPNP_ERRORS = (UpnpError, aiohttp.ClientError, OSError, TimeoutError)

ChangeCallback = Callable[[dict[str, str]], None]


class SonosUpnp(UpnpNotifyServer):  # type: ignore[misc,unused-ignore]
    """The UPnP of all Sonos speakers, with their events received on the stream server."""

    def __init__(self, mass: MusicAssistant, logger: logging.Logger) -> None:
        """
        Start receiving events; a speaker is reached with ``speaker``.

        :param mass: The Music Assistant instance.
        :param logger: The provider's logger.
        """
        self._mass = mass
        self._logger = logger
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
            self._logger.debug("Ignoring an unreadable event from %s: %r", request.remote, err)
            return Response(status=400)
        return Response(status=status)


class SonosSpeakerUpnp:
    """
    The UPnP services of one speaker: action calls and the RenderingControl events.

    After UPNP_TIMEOUT_LIMIT timeouts in a row nothing is sent until reset_timeouts.
    """

    def __init__(self, upnp: SonosUpnp, player_ip: str, logger: logging.Logger) -> None:
        """
        Initialize; the speaker's description is read on first use.

        :param upnp: The UPnP of all speakers.
        :param player_ip: The speaker's IP address.
        :param logger: The player's logger.
        """
        self._upnp = upnp
        self._logger = logger
        self.player_ip = player_ip
        self.location = f"http://{player_ip}:{UPNP_PORT}/xml/device_description.xml"
        self._device: UpnpDevice | None = None
        self._device_lock = asyncio.Lock()
        self._timeouts = 0
        # kept here because the event handler holds the service only weakly
        self._events_service: UpnpService | None = None
        self._renew_at = 0.0
        self._events_live = False
        # one subscribe/renew at a time
        self._events_lock = asyncio.Lock()

    @property
    def follows(self) -> bool:
        """Return whether the speaker's events come in: false until the first one arrived."""
        return self._events_service is not None and self._events_live

    @property
    def answers(self) -> bool:
        """Return whether requests are sent to the speaker."""
        return self._timeouts < UPNP_TIMEOUT_LIMIT

    def reset_timeouts(self) -> None:
        """Send requests again, e.g. after the speaker reconnected."""
        self._timeouts = 0

    async def call(self, service_type: str, action: str, **arguments: Any) -> Mapping[str, Any]:
        """
        Call a UPnP action of the speaker and return its output arguments.

        Raises one of UPNP_ERRORS when the speaker lacks it or does not answer.

        :param service_type: The service, e.g. urn:schemas-upnp-org:service:RenderingControl:1.
        :param action: The action, e.g. GetBass.
        :param arguments: The input arguments as Python values.
        """
        service = await self._service(service_type)
        if not service.has_action(action):
            msg = f"{self.location} has no {action}"
            raise UpnpError(msg)
        return await self._counting_timeouts(service.action(action).async_call(**arguments))

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
                await self._counting_timeouts(self._upnp.event_handler.async_unsubscribe(service))
            except UPNP_ERRORS as err:
                self._logger.debug("Ending the events of %s failed: %r", self.location, err)

    async def _counting_timeouts[T](self, request: Awaitable[T]) -> T:
        if not self.answers:
            if asyncio.iscoroutine(request):
                request.close()
            msg = f"{self.location} does not answer"
            raise UpnpError(msg)
        try:
            result = await request
        except TimeoutError:
            self._timeouts += 1
            if self._timeouts == UPNP_TIMEOUT_LIMIT:
                self._logger.warning(
                    "%s does not answer over UPnP; its settings are left out until it reconnects",
                    self.location,
                )
            raise
        self._timeouts = 0
        return result

    async def _service(self, service_type: str) -> UpnpService:
        async with self._device_lock:
            if self._device is None:
                self._device = await self._counting_timeouts(
                    self._upnp.factory.async_create_device(self.location)
                )
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
            _, timeout = await self._counting_timeouts(
                self._upnp.event_handler.async_subscribe(
                    service, timeout=timedelta(seconds=EVENT_SUBSCRIPTION_TIMEOUT)
                )
            )
        except UPNP_ERRORS as err:
            self._events_service = None
            self._logger.debug("Subscribing to the events of %s failed: %r", self.location, err)
            return
        self._renew_at = time.time() + timeout.total_seconds() / 2
        self._logger.debug("Subscribed to the events of %s", self.location)

    async def _renew(self) -> None:
        assert self._events_service is not None  # checked by follow
        try:
            _, timeout = await self._counting_timeouts(
                self._upnp.event_handler.async_resubscribe(
                    self._events_service, timeout=timedelta(seconds=EVENT_SUBSCRIPTION_TIMEOUT)
                )
            )
        except UPNP_ERRORS as err:
            # lost: made again on the next follow
            self._events_service = None
            self._events_live = False
            self._logger.debug("Renewing the events of %s failed: %r", self.location, err)
            return
        self._renew_at = time.time() + timeout.total_seconds() / 2

    def _handle_event(
        self, variables: Sequence[UpnpStateVariable[Any]], on_change: ChangeCallback
    ) -> None:
        if self._events_service is None:
            return
        if not self._events_live:
            self._logger.debug("Events of %s come in", self.location)
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
