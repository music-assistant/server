"""Pair Sendspin clients with the pairing token Home Assistant reads from their device."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, cast

from aiosendspin.models.types import PairMethod
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.trust_store import PskCategory

from music_assistant.providers.sendspin.helpers import SecurityActionError, effective_pair_methods

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.providers.hass import HomeAssistantProvider
    from music_assistant.providers.sendspin.provider import SendspinProvider


class HassAutoPairer:
    """
    Pair unpaired encrypted clients without user interaction, through Home Assistant.

    Home Assistant announces the Sendspin client ids of the devices it can read a pairing
    token from. A client that is both announced and connected here unpaired is paired with
    that token, whichever of the two happens last.
    """

    def __init__(self, provider: SendspinProvider) -> None:
        """Initialize for a Sendspin provider, without following Home Assistant yet."""
        self.provider = provider
        self.logger = provider.logger
        self._announced: set[str] = set()
        self._unsubscribe: Callable[[], None] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # clients triggered again while their attempt was running: tried once more after it
        self._pending: set[str] = set()
        # bumped by unsubscribe(), so a subscription answered after a teardown is dropped
        self._generation = 0
        self._closed = False

    def subscribe(self) -> None:
        """Start following the client ids Home Assistant announces, if it is available."""
        self.unsubscribe()
        if self._closed or (hass := self._hass) is None:
            return
        # in the background: Home Assistant must never hold up the Sendspin provider
        self.provider.mass.create_task(self._subscribe(hass))

    def unsubscribe(self) -> None:
        """Stop following Home Assistant and forget its announcements."""
        self._generation += 1
        self._announced.clear()
        self._pending.clear()
        if (unsubscribe := self._unsubscribe) is not None:
            self._unsubscribe = None
            unsubscribe()

    def close(self) -> None:
        """Stop following Home Assistant for good and stop running attempts, as the provider unloads."""
        self._closed = True
        self.unsubscribe()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    def on_client_connected(self, client_id: str) -> None:
        """Pair a client that just connected, if Home Assistant announced it."""
        if client_id in self._announced:
            self._request_attempt(client_id)

    @property
    def _hass(self) -> HomeAssistantProvider | None:
        hass = cast("HomeAssistantProvider | None", self.provider.mass.get_provider("hass"))
        return hass if hass is not None and hass.available else None

    async def _subscribe(self, hass: HomeAssistantProvider) -> None:
        """Set up the subscription, unless it was torn down while Home Assistant answered."""
        generation = self._generation
        unsubscribe = await hass.subscribe_sendspin_clients(partial(self._on_clients, generation))
        if unsubscribe is None:
            return
        if generation != self._generation:
            unsubscribe()
            return
        self._unsubscribe = unsubscribe

    def _on_clients(self, generation: int, client_ids: list[str]) -> None:
        """Handle the client ids Home Assistant can fetch a pairing token for."""
        if generation != self._generation:
            # a snapshot of a subscription that was torn down meanwhile
            return
        announced = set(client_ids)
        new_client_ids = announced - self._announced
        self._announced = announced
        for client_id in new_client_ids:
            self._request_attempt(client_id)

    def _request_attempt(self, client_id: str) -> None:
        """Run a pairing attempt in the background, at most one per client at a time."""
        if client_id in self._tasks:
            # the running attempt may be about to fail on the state this trigger changed
            self._pending.add(client_id)
            return
        task = self.provider.mass.create_task(self._run_attempt(client_id))
        if not task.done():
            self._tasks[client_id] = task

    async def _run_attempt(self, client_id: str) -> None:
        """Run one attempt and, if the client was triggered again meanwhile, one more."""
        try:
            await self._attempt(client_id)
        finally:
            self._tasks.pop(client_id, None)
        if client_id in self._pending:
            self._pending.discard(client_id)
            if client_id in self._announced:
                self._request_attempt(client_id)

    async def _attempt(self, client_id: str) -> None:
        """Pair the client with the token Home Assistant reads from its device."""
        if not await self._needs_pairing(client_id) or (hass := self._hass) is None:
            return
        token = await hass.get_sendspin_pairing_token(client_id)
        if token is None or not await self._needs_pairing(client_id):
            return
        try:
            await self.provider.pair_with_token(client_id, token)
        except (
            SecurityActionError,
            PairingError,
            HandshakeAbortedError,
            TimeoutError,
            OSError,
        ) as err:
            self.logger.warning(
                "Pairing %s with the token from Home Assistant failed: %s", client_id, err
            )
        else:
            self.logger.info("Paired %s with the token from Home Assistant", client_id)

    async def _needs_pairing(self, client_id: str) -> bool:
        """Whether the client is connected, encrypted, unpaired and offers the token method."""
        server_api = self.provider.server_api
        sendspin_client = server_api.get_client(client_id)
        if sendspin_client is None or sendspin_client.connection is None:
            return False
        security = sendspin_client.connection_security
        if security is None:
            # legacy unencrypted connection: nothing to pair
            return False
        if (
            security.psk_category is PskCategory.LONG_TERM
            and await server_api.pairing_store.record_by_client_id(client_id) is not None
        ):
            return False
        session = self.provider.get_pin_session(client_id)
        if session is not None and session.attempt_running:
            # leave a pairing the user started by hand alone
            return False
        return PairMethod.PAIRING_PSK in effective_pair_methods(
            sendspin_client.info_or_none, self.provider.pairing_config_snapshot(client_id)
        )
