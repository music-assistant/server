"""
UPnP event subscriptions to the RenderingControl service of Sonos speakers.

The websocket api sends no event when a setting (EQ, night mode, ...) changes, the UPnP
RenderingControl service does. The events are received on Music Assistant's stream server,
which the speakers reach anyway to fetch the audio.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
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

from .const import EVENT_SUBSCRIPTION_TIMEOUT, EVENTS_PATH, RENDERING_CONTROL_SERVICE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from async_upnp_client.client import UpnpService, UpnpStateVariable

    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)
UPNP_ERRORS = (aiohttp.ClientError, OSError, TimeoutError, KeyError, UpnpError)

ChangeCallback = Callable[[dict[str, str]], None]


@dataclass
class _Subscription:
    """A subscription to one speaker's RenderingControl service."""

    # kept here because the event handler holds the service only weakly
    service: UpnpService
    on_change: ChangeCallback
    renew_at: float
    # whether an event came in, which proves events reach Music Assistant at all
    live: bool = False


class SonosEvents(UpnpNotifyServer):  # type: ignore[misc,unused-ignore]
    """
    RenderingControl events of the Sonos speakers, received on Music Assistant's stream server.

    A speaker counts as followed only once its first event came in. Until then, and after a
    subscription is lost, its settings have to be polled as before.
    """

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Start receiving events; a speaker is subscribed to with ``follow``.

        :param mass: The Music Assistant instance.
        """
        self._mass = mass
        requester = AiohttpSessionRequester(mass.http_session_no_ssl, timeout=10)
        self._factory = UpnpFactory(requester)
        self._handler = UpnpEventHandler(self, requester)
        # speaker description URL -> its subscription
        self._subscriptions: dict[str, _Subscription] = {}
        # one subscribe/renew at a time, so a speaker is not subscribed to twice
        self._lock = asyncio.Lock()
        self._unregister = mass.streams.register_dynamic_route(
            EVENTS_PATH, self._handle_request, method="NOTIFY"
        )

    @property
    def callback_url(self) -> str:
        """Return the URL the speakers send their events to."""
        return f"{self._mass.streams.base_url}{EVENTS_PATH}"

    def follows(self, location: str) -> bool:
        """
        Return whether a speaker's events come in.

        :param location: The speaker's UPnP description URL.
        """
        return (subscription := self._subscriptions.get(location)) is not None and subscription.live

    async def follow(self, location: str, on_change: ChangeCallback) -> None:
        """
        Subscribe to a speaker, or renew its subscription when due.

        A subscription that fails is tried again on the next call.

        :param location: The speaker's UPnP description URL.
        :param on_change: Called with the state variables an event changed.
        """
        async with self._lock:
            if (subscription := self._subscriptions.get(location)) is None:
                await self._subscribe(location, on_change)
            elif subscription.renew_at <= time.time():
                await self._renew(location, subscription)

    async def unfollow(self, location: str) -> None:
        """
        End the subscription to a speaker; a speaker that is gone keeps it until it runs out.

        :param location: The speaker's UPnP description URL.
        """
        async with self._lock:
            if (subscription := self._subscriptions.pop(location, None)) is None:
                return
            try:
                await self._handler.async_unsubscribe(subscription.service)
            except UPNP_ERRORS as err:
                LOGGER.debug("Ending the events of %s failed: %r", location, err)

    async def close(self) -> None:
        """Stop receiving events and end every subscription."""
        # first: a speaker that does not answer its unsubscribe can outlast the unload,
        # and a route left behind keeps the provider from loading again
        self._unregister()
        for location in list(self._subscriptions):
            await self.unfollow(location)

    async def _subscribe(self, location: str, on_change: ChangeCallback) -> None:
        try:
            device = await self._factory.async_create_device(location)
            # RenderingControl sits in the MediaRenderer device embedded in the ZonePlayer
            service = device.find_service(RENDERING_CONTROL_SERVICE)
            if service is None:
                LOGGER.debug("%s has no RenderingControl service", location)
                return
            service.on_event = lambda _service, variables: self._handle_event(location, variables)
            # registered before subscribing: the first event can come in before the answer
            subscription = _Subscription(service, on_change, renew_at=float("inf"))
            self._subscriptions[location] = subscription
            _, timeout = await self._handler.async_subscribe(
                service, timeout=timedelta(seconds=EVENT_SUBSCRIPTION_TIMEOUT)
            )
        except UPNP_ERRORS as err:
            self._subscriptions.pop(location, None)
            LOGGER.debug("Subscribing to the events of %s failed: %r", location, err)
            return
        subscription.renew_at = time.time() + timeout.total_seconds() / 2
        LOGGER.debug("Subscribed to the events of %s", location)

    async def _renew(self, location: str, subscription: _Subscription) -> None:
        try:
            _, timeout = await self._handler.async_resubscribe(
                subscription.service, timeout=timedelta(seconds=EVENT_SUBSCRIPTION_TIMEOUT)
            )
        except UPNP_ERRORS as err:
            # lost: made again on the next follow
            self._subscriptions.pop(location, None)
            LOGGER.debug("Renewing the events of %s failed: %r", location, err)
            return
        subscription.renew_at = time.time() + timeout.total_seconds() / 2

    def _handle_event(self, location: str, variables: Sequence[UpnpStateVariable[Any]]) -> None:
        if (subscription := self._subscriptions.get(location)) is None:
            return
        if not subscription.live:
            LOGGER.debug("Events of %s come in", location)
            subscription.live = True
        changes: dict[str, str] = {}
        for variable in variables:
            if variable.name == "LastChange":
                changes.update(parse_last_change(variable.upnp_value))
        if changes:
            subscription.on_change(changes)

    async def _handle_request(self, request: Request) -> Response:
        # a speaker can send a name in another encoding than it says; decode leniently
        body = (await request.read()).decode("utf-8", errors="replace")
        http_request = HttpRequest(
            method=request.method, url=str(request.url), headers=request.headers, body=body
        )
        try:
            status = await self._handler.handle_notify(http_request)
        except (DefusedET.ParseError, ValueError) as err:
            LOGGER.debug("Ignoring an unreadable event from %s: %r", request.remote, err)
            return Response(status=400)
        return Response(status=status)


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
