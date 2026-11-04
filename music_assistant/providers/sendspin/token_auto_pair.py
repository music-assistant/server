"""Base for pairing Sendspin clients without user interaction, with a token fetched for them."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from aiosendspin.models.types import PairMethod
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.trust_store import PskCategory

from music_assistant.providers.sendspin.helpers import SecurityActionError, effective_pair_methods

if TYPE_CHECKING:
    from music_assistant.providers.sendspin.provider import SendspinProvider


class TokenAutoPairer(ABC):
    """
    Pair unpaired encrypted clients with a pairing token, at most one attempt per client at a time.

    Subclasses decide when a client is worth an attempt and where its token comes from.
    """

    # how the token is named in log messages, e.g. "its local token"
    source_label: ClassVar[str]

    def __init__(self, provider: SendspinProvider) -> None:
        """Initialize for a Sendspin provider."""
        self.provider = provider
        self.logger = provider.logger
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # clients triggered again while their attempt was running: tried once more after it
        self._pending: set[str] = set()
        self._closed = False

    def close(self) -> None:
        """Stop running attempts for good, as the provider unloads."""
        self._closed = True
        self._pending.clear()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    @abstractmethod
    async def _get_token(self, client_id: str) -> str | None:
        """
        Return the pairing token for a client, or None when there is none to use.

        :param client_id: The Sendspin client id to fetch the token for.
        """

    def _wants_retry(self, client_id: str) -> bool:
        """
        Whether a client triggered again during its attempt should get another one.

        :param client_id: The Sendspin client id that was triggered again.
        """
        return True

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
            if self._wants_retry(client_id):
                self._request_attempt(client_id)

    async def _attempt(self, client_id: str) -> None:
        """Pair the client with its token, if it still needs pairing once the token is in."""
        if not await self._needs_pairing(client_id):
            return
        token = await self._get_token(client_id)
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
            self.logger.warning("Pairing %s with %s failed: %s", client_id, self.source_label, err)
        else:
            self.logger.info("Paired %s with %s", client_id, self.source_label)

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
