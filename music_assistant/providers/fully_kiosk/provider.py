"""FullyKiosk Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientSession, Fingerprint
from fullykiosk import FullyKiosk
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import (
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL_FINGERPRINT,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.models.player_provider import PlayerProvider

from .player import FullyKioskPlayer


@dataclass
class _FingerprintSessionWrapper:
    """Proxy ClientSession that enforces a TLS fingerprint."""

    session: ClientSession
    fingerprint: Fingerprint

    def get(self, *args: Any, **kwargs: Any) -> Any:
        """Call the wrapped session.get while injecting the fingerprint."""
        kwargs.setdefault("ssl", self.fingerprint)
        return self.session.get(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to the wrapped session."""
        return getattr(self.session, name)


def _build_fingerprint(value: str) -> Fingerprint:
    """Parse a fingerprint string (sha256 hex) into an aiohttp Fingerprint."""
    normalized = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if not normalized:
        msg = "Empty fingerprint provided."
        raise ValueError(msg)
    if len(normalized) % 2 != 0:
        msg = "Fingerprint must contain an even number of hex characters."
        raise ValueError(msg)
    digest = bytes.fromhex(normalized)
    return Fingerprint(digest)


class FullyKioskProvider(PlayerProvider):
    """Player provider for FullyKiosk based players."""

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set-up fullykiosk logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("fullykiosk").setLevel(logging.DEBUG)
        else:
            logging.getLogger("fullykiosk").setLevel(self.logger.level + 10)

        use_ssl = bool(self.config.get_value(CONF_USE_SSL))
        fingerprint_value = self.config.get_value(CONF_SSL_FINGERPRINT)
        fingerprint_raw = fingerprint_value.strip() if isinstance(fingerprint_value, str) else ""
        if fingerprint_raw and not use_ssl:
            msg = "Fingerprint validation requires HTTPS to be enabled."
            raise SetupFailedError(msg)

        verify_ssl = bool(self.config.get_value(CONF_VERIFY_SSL)) if use_ssl else False
        http_session: ClientSession | _FingerprintSessionWrapper
        if use_ssl:
            if fingerprint_raw:
                try:
                    fingerprint = _build_fingerprint(fingerprint_raw)
                except ValueError as err:
                    msg = f"Invalid TLS fingerprint configured: {err}"
                    raise SetupFailedError(msg) from err
                http_session = _FingerprintSessionWrapper(self.mass.http_session, fingerprint)
                verify_ssl = True
            else:
                http_session = (
                    self.mass.http_session if verify_ssl else self.mass.http_session_no_ssl
                )
        else:
            http_session = self.mass.http_session_no_ssl

        fully_kiosk = FullyKiosk(
            http_session,
            self.config.get_value(CONF_IP_ADDRESS),
            self.config.get_value(CONF_PORT),
            self.config.get_value(CONF_PASSWORD),
            use_ssl=use_ssl,
            verify_ssl=verify_ssl,
        )
        try:
            async with asyncio.timeout(15):
                await fully_kiosk.getDeviceInfo()
        except Exception as err:
            msg = f"Unable to start the FullyKiosk connection ({err!s}"
            raise SetupFailedError(msg) from err
        player_id = fully_kiosk.deviceInfo["deviceID"]
        scheme = "https" if use_ssl else "http"
        address = (
            f"{scheme}://{self.config.get_value(CONF_IP_ADDRESS)}:"
            f"{self.config.get_value(CONF_PORT)}"
        )
        player = FullyKioskPlayer(self, player_id, fully_kiosk, address)
        player.set_attributes()
        await self.mass.players.register(player)
