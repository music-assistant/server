"""Pair Sendspin players on the Home Assistant host with the pairing token they serve locally."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from aiohttp import ClientError, ClientTimeout
from aiosendspin.models.types import PairMethod
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.pairing import PairingError
from aiosendspin.noise.trust_store import PskCategory

from music_assistant.providers.sendspin.constants import CONF_LOCAL_PAIRING
from music_assistant.providers.sendspin.helpers import SecurityActionError, effective_pair_methods

if TYPE_CHECKING:
    from music_assistant.providers.sendspin.provider import SendspinProvider

# Provisional application-specific role a player lists when it serves its token locally.
LOCAL_PAIRING_ROLE: Final = "_ma_local_pairing@v1"
LOCAL_PAIRING_SUPPORT_KEY: Final = f"{LOCAL_PAIRING_ROLE}_support"
# Host side of the hassio network. Fixed here, never taken from the client, so a client
# can only steer which port on this one address is asked.
LOCAL_PAIRING_HOST: Final = "172.30.32.1"
LOCAL_PAIRING_PORT_RANGE: Final = range(1024, 65536)
LOCAL_PAIRING_TIMEOUT: Final = ClientTimeout(total=5)
# A pairing token is well under this; anything longer is not one.
LOCAL_PAIRING_MAX_BODY: Final = 512


class LocalAutoPairer:
    """
    Pair unpaired encrypted clients without user interaction, on the Home Assistant host.

    A player running next to Music Assistant lists the local pairing role in its hello,
    with the port it serves its pairing token on at the host side of the hassio network.
    That token is fetched and used to pair the client as soon as it connects.
    """

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

    def on_client_connected(self, client_id: str) -> None:
        """Pair a client that just connected, if it serves its token locally."""
        if self._enabled:
            self._request_attempt(client_id)

    @property
    def _enabled(self) -> bool:
        # the hassio network, and so the token endpoint, only exists on the add-on
        return (
            not self._closed
            and self.provider.mass.running_as_hass_addon
            and bool(self.provider.config.get_value(CONF_LOCAL_PAIRING, True))
        )

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
            self._request_attempt(client_id)

    async def _attempt(self, client_id: str) -> None:
        """Pair the client with the token it serves on the Home Assistant host."""
        if (port := self._token_port(client_id)) is None or not await self._needs_pairing(
            client_id
        ):
            return
        token = await self._fetch_token(client_id, port)
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
            self.logger.warning("Pairing %s with its local token failed: %s", client_id, err)
        else:
            self.logger.info("Paired %s with its local token", client_id)

    def _token_port(self, client_id: str) -> int | None:
        """Return the port the client serves its token on, if its hello announced a valid one."""
        sendspin_client = self.provider.server_api.get_client(client_id)
        if sendspin_client is None or (info := sendspin_client.info_or_none) is None:
            return None
        support = info.application_objects.get(LOCAL_PAIRING_SUPPORT_KEY)
        if not isinstance(support, dict):
            return None
        port = support.get("port")
        # bool is an int subclass, and never a port
        if type(port) is not int or port not in LOCAL_PAIRING_PORT_RANGE:
            self.logger.debug("Ignoring local pairing of %s: invalid port %r", client_id, port)
            return None
        return port

    async def _fetch_token(self, client_id: str, port: int) -> str | None:
        """Fetch the pairing token from the host side of the hassio network."""
        url = f"http://{LOCAL_PAIRING_HOST}:{port}/"
        try:
            async with self.provider.mass.http_session.get(
                url, timeout=LOCAL_PAIRING_TIMEOUT, allow_redirects=False
            ) as response:
                if response.status != 200:
                    self.logger.warning(
                        "Fetching the local token of %s failed: HTTP %s", client_id, response.status
                    )
                    return None
                body = b""
                while len(body) <= LOCAL_PAIRING_MAX_BODY and (
                    chunk := await response.content.read(LOCAL_PAIRING_MAX_BODY + 1 - len(body))
                ):
                    body += chunk
        except (ClientError, TimeoutError) as err:
            self.logger.warning("Fetching the local token of %s failed: %s", client_id, err)
            return None
        if len(body) > LOCAL_PAIRING_MAX_BODY:
            self.logger.warning("Fetching the local token of %s failed: too long", client_id)
            return None
        try:
            return body.decode("ascii").strip()
        except UnicodeDecodeError:
            self.logger.warning("Fetching the local token of %s failed: not text", client_id)
            return None

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
