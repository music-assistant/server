"""
plex.tv device registration for the Plex Connect plugin.

Mobile Plex clients (Plexamp on iOS/Android) do not use GDM discovery: they build
their cast list exclusively from the players registered in the plex.tv device
registry and then control them over their published local connection (the HTTP
companion endpoints this plugin already implements). This module registers a
plugin player instance on plex.tv via the official PIN link flow and publishes
its local connection URI, using the exact same identity (client identifier,
name, product, version) as the GDM advertisement and the companion server.
"""

from __future__ import annotations

import platform
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from aiohttp import ClientTimeout
from defusedxml import ElementTree as DefusedET
from yarl import URL

if TYPE_CHECKING:
    from aiohttp import ClientSession

PLEXTV_PINS_URL = "https://plex.tv/api/v2/pins"
PLEXTV_DEVICES_URL = "https://plex.tv/devices.xml"
PLEXTV_DEVICE_BASE_URL = "https://plex.tv/devices"
PLEXTV_LINK_URL = "https://plex.tv/link"

REQUEST_TIMEOUT = 15


def compute_client_id(plex_provider_id: str, ma_player_id: str) -> str:
    """
    Return the stable Plex client identifier for a plugin player instance.

    This is the identifier used as GDM Resource-Identifier, companion server
    machineIdentifier and plex.tv X-Plex-Client-Identifier - they must all match
    for Plex clients to treat them as one and the same device.

    :param plex_provider_id: Instance id of the linked Plex music provider.
    :param ma_player_id: The Music Assistant player id exposed by this instance.
    """
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"music-assistant-plex-{plex_provider_id}-{ma_player_id}",
        )
    )


def build_version(mass_version: str) -> str:
    """
    Return the version string to advertise to Plex.

    :param mass_version: The Music Assistant server version ("0.0.0" for dev builds).
    """
    return mass_version if mass_version != "0.0.0" else "1.0.0"


@dataclass(frozen=True)
class PlexTvIdentity:
    """Player identity presented to plex.tv (must match the GDM/companion identity)."""

    client_id: str
    name: str
    version: str
    product: str = "Music Assistant"

    @property
    def headers(self) -> dict[str, str]:
        """Return the X-Plex identity headers to send with every plex.tv request."""
        return {
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Product": self.product,
            "X-Plex-Version": self.version,
            "X-Plex-Platform": platform.system(),
            "X-Plex-Device": "Music Assistant",
            "X-Plex-Device-Name": self.name,
            "X-Plex-Model": "standalone",
            "X-Plex-Provides": "client,player,pubsub-player",
        }


@dataclass(frozen=True)
class PlexPin:
    """A plex.tv link PIN as returned by the pins API."""

    id: int
    code: str


class PlexTvError(Exception):
    """Raised on unexpected plex.tv API errors."""


class PlexTvAuthError(PlexTvError):
    """Raised when plex.tv rejects the device token (re-link needed)."""


class PlexTvPinExpiredError(PlexTvError):
    """Raised when a link PIN is no longer valid (PINs expire after ~15 minutes)."""


class PlexTvClient:
    """Async client for the plex.tv device registration endpoints."""

    def __init__(self, session: ClientSession, identity: PlexTvIdentity) -> None:
        """
        Initialize the plex.tv client.

        :param session: The (shared) aiohttp client session to use.
        :param identity: The player identity to present on all requests.
        """
        self._session = session
        self.identity = identity

    async def create_pin(self) -> PlexPin:
        """Request a new (4-character) link PIN from plex.tv."""
        status, data = await self._request(
            "POST", PLEXTV_PINS_URL, accept="application/json", data=b"strong=false"
        )
        if status not in (200, 201) or not isinstance(data, dict):
            raise PlexTvError(f"PIN creation failed (HTTP {status})")
        return PlexPin(id=int(str(data["id"])), code=str(data["code"]))

    async def check_pin(self, pin_id: int) -> str | None:
        """
        Check a link PIN and return the device token, or None while still pending.

        :param pin_id: The id of the PIN as returned by :meth:`create_pin`.
        """
        status, data = await self._request(
            "GET", f"{PLEXTV_PINS_URL}/{pin_id}", accept="application/json"
        )
        if status == 404:
            raise PlexTvPinExpiredError("The link code has expired")
        if status != 200 or not isinstance(data, dict):
            raise PlexTvError(f"PIN check failed (HTTP {status})")
        return str(data["authToken"]) if data.get("authToken") else None

    async def get_device_id(self, token: str) -> str | None:
        """
        Return the plex.tv device id for this identity, or None if not registered.

        :param token: The device token obtained through the PIN link flow.
        """
        status, text = await self._request("GET", PLEXTV_DEVICES_URL, token=token)
        if status != 200 or not isinstance(text, str):
            raise PlexTvError(f"Device listing failed (HTTP {status})")
        root = DefusedET.fromstring(text)
        for device in root.iter("Device"):
            if device.get("clientIdentifier") == self.identity.client_id:
                device_id: str | None = device.get("id")
                return device_id
        return None

    async def publish_connection(self, token: str, device_id: str, uri: str) -> None:
        """
        Publish the local connection URI for the registered device.

        :param token: The device token obtained through the PIN link flow.
        :param device_id: The plex.tv device id as returned by :meth:`get_device_id`.
        :param uri: The local connection URI, e.g. ``http://192.168.1.10:32500``.
        """
        # Build the query fully pre-encoded: plex.tv expects the literal
        # Connection[][uri] parameter and aiohttp/yarl would otherwise re-encode it.
        url = URL(
            f"{PLEXTV_DEVICE_BASE_URL}/{device_id}?Connection%5B%5D%5Buri%5D={quote(uri, safe='')}",
            encoded=True,
        )
        status, _ = await self._request("PUT", url, token=token)
        if status != 200:
            raise PlexTvError(f"Publishing connection URI failed (HTTP {status})")

    async def delete_device(self, token: str, device_id: str) -> None:
        """
        Remove the registered device from the plex.tv account.

        :param token: The device token obtained through the PIN link flow.
        :param device_id: The plex.tv device id as returned by :meth:`get_device_id`.
        """
        status, _ = await self._request(
            "DELETE", f"{PLEXTV_DEVICE_BASE_URL}/{device_id}.xml", token=token
        )
        if status not in (200, 204):
            raise PlexTvError(f"Device removal failed (HTTP {status})")

    async def _request(
        self,
        method: str,
        url: str | URL,
        token: str | None = None,
        accept: str = "application/xml",
        data: bytes | None = None,
    ) -> tuple[int, str | dict[str, object] | None]:
        """
        Perform a plex.tv request with the identity headers applied.

        :param method: HTTP method.
        :param url: Request URL (pass a pre-encoded yarl URL to prevent re-encoding).
        :param token: Optional X-Plex-Token to authenticate with.
        :param accept: Accept header; JSON responses are parsed, others returned as text.
        :param data: Optional request body.
        """
        headers = {**self.identity.headers, "Accept": accept}
        if token:
            headers["X-Plex-Token"] = token
        if data is not None:
            # plex.tv rejects the aiohttp default (application/octet-stream) with HTTP 415
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        async with self._session.request(
            method,
            url,
            headers=headers,
            data=data,
            timeout=ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            if response.status == 401:
                raise PlexTvAuthError("plex.tv rejected the device token (HTTP 401)")
            if response.status == 204:
                return response.status, None
            if accept == "application/json":
                try:
                    return response.status, await response.json()
                except ValueError:
                    return response.status, None
            return response.status, await response.text()
