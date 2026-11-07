"""Pair Sendspin clients with the pairing token Home Assistant reads from their device."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast

from music_assistant.providers.sendspin.token_auto_pair import TokenAutoPairer

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.providers.hass import HomeAssistantProvider
    from music_assistant.providers.sendspin.provider import SendspinProvider


class HassAutoPairer(TokenAutoPairer):
    """
    Pair unpaired encrypted clients without user interaction, through Home Assistant.

    Home Assistant announces the Sendspin client ids of the devices it can read a pairing
    token from. A client that is both announced and connected here unpaired is paired with
    that token, whichever of the two happens last.
    """

    source_label = "the token from Home Assistant"

    def __init__(self, provider: SendspinProvider) -> None:
        """Initialize for a Sendspin provider, without following Home Assistant yet."""
        super().__init__(provider)
        self._announced: set[str] = set()
        self._unsubscribe: Callable[[], None] | None = None
        # bumped by unsubscribe(), so a subscription answered after a teardown is dropped
        self._generation = 0

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
        self.unsubscribe()
        super().close()

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

    async def _get_token(self, client_id: str) -> str | None:
        """Return the token Home Assistant reads from the client's device."""
        if (hass := self._hass) is None:
            return None
        return await hass.get_sendspin_pairing_token(client_id)

    def _wants_retry(self, client_id: str) -> bool:
        """Retry only clients Home Assistant still announces."""
        return client_id in self._announced
