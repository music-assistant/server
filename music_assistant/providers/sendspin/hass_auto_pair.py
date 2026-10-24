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
        # bumped by unsubscribe(), so a subscription answered after a teardown is dropped
        self._generation = 0

    async def subscribe(self) -> None:
        """Start following the client ids Home Assistant announces, if it is available."""
        self.unsubscribe()
        if (hass := self._hass) is None:
            return
        generation = self._generation
        unsubscribe = await hass.subscribe_sendspin_clients(partial(self._on_clients, generation))
        if unsubscribe is None:
            return
        if generation != self._generation:
            # torn down while the subscription was being set up
            unsubscribe()
            return
        self._unsubscribe = unsubscribe

    def unsubscribe(self) -> None:
        """Stop following Home Assistant, forget its announcements and stop pending attempts."""
        self._generation += 1
        self._announced.clear()
        if (unsubscribe := self._unsubscribe) is not None:
            self._unsubscribe = None
            unsubscribe()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    def on_client_connected(self, client_id: str) -> None:
        """Pair a client that just connected, if Home Assistant announced it."""
        if client_id in self._announced:
            self._start_pairing(client_id)

    @property
    def _hass(self) -> HomeAssistantProvider | None:
        hass = cast("HomeAssistantProvider | None", self.provider.mass.get_provider("hass"))
        return hass if hass is not None and hass.available else None

    def _on_clients(self, generation: int, client_ids: list[str]) -> None:
        """Handle the client ids Home Assistant can fetch a pairing token for."""
        if generation != self._generation:
            # a snapshot of a subscription that was torn down meanwhile
            return
        announced = set(client_ids)
        new_client_ids = announced - self._announced
        self._announced = announced
        for client_id in new_client_ids:
            self._start_pairing(client_id)

    def _start_pairing(self, client_id: str) -> None:
        """Run a pairing attempt in the background, at most one per client at a time."""
        if (task := self._tasks.get(client_id)) is not None and not task.done():
            return
        task = self.provider.mass.create_task(self._pair(client_id))
        self._tasks[client_id] = task
        task.add_done_callback(partial(self._forget_task, client_id))

    def _forget_task(self, client_id: str, task: asyncio.Task[None]) -> None:
        """Drop a finished attempt, unless a newer one for the client replaced it already."""
        if self._tasks.get(client_id) is task:
            del self._tasks[client_id]

    async def _pair(self, client_id: str) -> None:
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
        if sendspin_client is None or not sendspin_client.is_connected:
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
