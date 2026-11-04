"""Pair Sendspin players on the Home Assistant host with the pairing token they serve locally."""

from __future__ import annotations

from typing import Final

from aiohttp import ClientError, ClientTimeout

from music_assistant.providers.sendspin.constants import CONF_LOCAL_PAIRING
from music_assistant.providers.sendspin.token_auto_pair import TokenAutoPairer

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


class LocalAutoPairer(TokenAutoPairer):
    """
    Pair unpaired encrypted clients without user interaction, on the Home Assistant host.

    A player running next to Music Assistant lists the local pairing role in its hello,
    with the port it serves its pairing token on at the host side of the hassio network.
    That token is fetched and used to pair the client as soon as it connects.
    """

    source_label = "its local token"

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

    async def _get_token(self, client_id: str) -> str | None:
        """Return the token the client serves on the Home Assistant host, if it announced one."""
        if (port := self._token_port(client_id)) is None:
            return None
        return await self._fetch_token(client_id, port)

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
