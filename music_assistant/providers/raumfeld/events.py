"""UPnP event subscriptions to the renderers of a Teufel Raumfeld system."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import defusedxml.ElementTree as DefusedET
from aiohttp.web import Request, Response
from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.const import HttpRequest
from async_upnp_client.event_handler import UpnpEventHandler, UpnpNotifyServer
from async_upnp_client.exceptions import UpnpError

from .constants import EVENT_SUBSCRIPTION_TIMEOUT, HOST_ERRORS, UPNP_TIMEOUT
from .helpers import parse_last_change

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from async_upnp_client.client import UpnpService, UpnpStateVariable

    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)

# the paths of Music Assistant's stream server the events are sent to: the volume and the
# transport of the rooms' own renderers, and the transport of the zone renderers
ROOM_EVENTS_PATH = "/raumfeld/notify/rooms"
ROOM_TRANSPORT_EVENTS_PATH = "/raumfeld/notify/room-transport"
ZONE_EVENTS_PATH = "/raumfeld/notify/zones"


@dataclass
class _Subscription:
    """A subscription to one renderer's service."""

    # kept here because the event handler holds the service only weakly
    service: UpnpService
    renew_at: float
    # whether an event came in, which proves events reach Music Assistant at all
    live: bool = False


class RendererEvents(UpnpNotifyServer):
    """
    Subscriptions to one service of a set of renderers, received on Music Assistant's stream server.

    A renderer counts as followed only once its first event came in. Until then, and after a
    subscription is lost, its state has to be read as before.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        path: str,
        service_type: str,
        on_change: Callable[[str, dict[str, str]], None],
    ) -> None:
        """
        Start receiving events; nothing is subscribed to until ``refresh``.

        :param mass: The Music Assistant instance.
        :param path: The path of Music Assistant's stream server the events are sent to.
        :param service_type: The service to subscribe to on each renderer.
        :param on_change: Called with a renderer's description URL and the state variables
            its event changed.
        """
        self._mass = mass
        self._path = path
        self._service_type = service_type
        self._on_change = on_change
        requester = AiohttpSessionRequester(mass.http_session, timeout=UPNP_TIMEOUT)
        self._factory = UpnpFactory(requester)
        self._handler = UpnpEventHandler(self, requester)
        # renderer description URL -> its subscription
        self._subscriptions: dict[str, _Subscription] = {}
        # one refresh at a time, so a renderer is not subscribed to twice
        self._refresh_lock = asyncio.Lock()
        self._unregister = mass.streams.register_dynamic_route(
            path, self._handle_request, method="NOTIFY"
        )

    @property
    def callback_url(self) -> str:
        """Return the URL renderers send their events to."""
        return f"{self._mass.streams.base_url}{self._path}"

    def follows(self, location: str) -> bool:
        """
        Return whether a renderer's events come in, so its state needs no reading.

        :param location: The renderer's description URL.
        """
        return (subscription := self._subscriptions.get(location)) is not None and subscription.live

    async def refresh(self, locations: Iterable[str]) -> None:
        """
        Subscribe to new renderers, renew what is due, and drop renderers no longer wanted.

        :param locations: The description URLs of the renderers to follow.
        """
        wanted = set(locations)
        async with self._refresh_lock:
            gone = [location for location in self._subscriptions if location not in wanted]
            await asyncio.gather(*(self._unsubscribe(location) for location in gone))
            now = time.time()
            await asyncio.gather(
                *(
                    self._renew(location)
                    if location in self._subscriptions
                    else self._subscribe(location)
                    for location in wanted
                    if location not in self._subscriptions
                    or self._subscriptions[location].renew_at <= now
                )
            )

    async def clear(self) -> None:
        """Drop every subscription."""
        async with self._refresh_lock:
            await asyncio.gather(
                *(self._unsubscribe(location) for location in list(self._subscriptions))
            )

    async def close(self) -> None:
        """Stop receiving events and drop every subscription."""
        # first: a renderer that does not answer its unsubscribe can outlast the unload,
        # and a route left behind keeps the provider from loading again
        self._unregister()
        await self.clear()

    async def _subscribe(self, location: str) -> None:
        """Subscribe to a renderer's service; a renderer that does not take it stays unfollowed."""
        try:
            device = await self._factory.async_create_device(location)
            service = device.service(self._service_type)
            service.on_event = lambda _service, variables: self._handle_event(location, variables)
            # registered before subscribing: the first event can come in before the answer
            subscription = _Subscription(service=service, renew_at=float("inf"))
            self._subscriptions[location] = subscription
            _, timeout = await self._handler.async_subscribe(
                service, timeout=timedelta(seconds=EVENT_SUBSCRIPTION_TIMEOUT)
            )
        except (*HOST_ERRORS, UpnpError) as err:
            self._subscriptions.pop(location, None)
            LOGGER.debug("Subscribing to events of %s failed: %r", location, err)
            return
        subscription.renew_at = time.time() + timeout.total_seconds() / 2
        LOGGER.debug("Subscribed to the events of %s", location)

    async def _renew(self, location: str) -> None:
        """Renew a subscription; a lost one is dropped, and made again on the next refresh."""
        subscription = self._subscriptions[location]
        try:
            _, timeout = await self._handler.async_resubscribe(
                subscription.service, timeout=timedelta(seconds=EVENT_SUBSCRIPTION_TIMEOUT)
            )
        except (*HOST_ERRORS, UpnpError) as err:
            self._subscriptions.pop(location, None)
            LOGGER.debug("Renewing the events of %s failed: %r", location, err)
            return
        subscription.renew_at = time.time() + timeout.total_seconds() / 2

    async def _unsubscribe(self, location: str) -> None:
        """End a subscription; a renderer that is gone keeps it until it runs out."""
        subscription = self._subscriptions.pop(location)
        try:
            await self._handler.async_unsubscribe(subscription.service)
        except (*HOST_ERRORS, UpnpError) as err:
            LOGGER.debug("Ending the events of %s failed: %r", location, err)

    def _handle_event(self, location: str, variables: Sequence[UpnpStateVariable[Any]]) -> None:
        """Pass on what a renderer's event changed."""
        if (subscription := self._subscriptions.get(location)) is None:
            return
        if not subscription.live:
            LOGGER.debug("Events of %s come in", location)
            subscription.live = True
        changes: dict[str, str] = {}
        for variable in variables:
            if variable.name == "LastChange":
                changes.update(parse_last_change(variable.upnp_value))
            else:
                changes[variable.name] = variable.upnp_value
        if changes:
            self._on_change(location, changes)

    async def _handle_request(self, request: Request) -> Response:
        """Hand an incoming event to the event handler."""
        # a renderer can send a name in another encoding than it says; decode leniently
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
