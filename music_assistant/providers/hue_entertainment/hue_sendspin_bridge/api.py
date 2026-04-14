"""
Hue Bridge REST API wrapper for Entertainment operations.

Handles pairing, entertainment area discovery, and entertainment mode
start/stop using the Hue V2 CLIP API directly via aiohttp.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .models import EntertainmentArea, LightChannel

LOGGER = logging.getLogger(__name__)

PAIR_RETRY_INTERVAL = 2.0
PAIR_TIMEOUT = 30.0


class HueEntertainmentAPI:
    """Wrapper around the Hue V2 CLIP API for entertainment operations."""

    def __init__(self, host: str, app_key: str | None = None) -> None:
        """Initialize the API client."""
        self._host = host
        self._app_key = app_key
        self._session: aiohttp.ClientSession | None = None

    @property
    def host(self) -> str:
        """Return the bridge host."""
        return self._host

    @host.setter
    def host(self, value: str) -> None:
        """Update the bridge host (e.g. when IP changes via mDNS)."""
        self._host = value

    @property
    def base_url(self) -> str:
        """Return the base URL for the Hue V2 CLIP API."""
        return f"https://{self._host}"

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def pair(self) -> dict[str, str]:
        """
        Pair with the Hue bridge (user must press the bridge button).

        Retries for up to PAIR_TIMEOUT seconds. Returns a dict with
        'username' and 'clientkey' keys.
        """
        session = await self._get_session()
        url = f"{self.base_url}/api"
        body = {
            "devicetype": "music_assistant#hue_entertainment",
            "generateclientkey": True,
        }
        deadline = asyncio.get_event_loop().time() + PAIR_TIMEOUT
        last_error = "Timeout waiting for bridge button press"

        while asyncio.get_event_loop().time() < deadline:
            try:
                async with session.post(url, json=body, ssl=False) as resp:
                    result = await resp.json()
            except (aiohttp.ClientError, TimeoutError) as err:
                last_error = str(err)
                await asyncio.sleep(PAIR_RETRY_INTERVAL)
                continue

            if isinstance(result, list) and result:
                entry = result[0]
                if "success" in entry:
                    success = entry["success"]
                    return {
                        "username": success["username"],
                        "clientkey": success["clientkey"],
                    }
                if "error" in entry:
                    error = entry["error"]
                    if error.get("type") == 101:
                        # Link button not pressed yet
                        await asyncio.sleep(PAIR_RETRY_INTERVAL)
                        continue
                    last_error = error.get("description", str(error))
                    break

            last_error = f"Unexpected pairing response: {result}"
            break

        msg = f"Hue bridge pairing failed: {last_error}"
        raise TimeoutError(msg)

    async def get_entertainment_areas(self) -> list[EntertainmentArea]:
        """Fetch all entertainment configurations from the bridge."""
        result = await self._request("GET", "/clip/v2/resource/entertainment_configuration")
        areas: list[EntertainmentArea] = []

        data = result.get("data", []) if isinstance(result, dict) else result
        for config in data:
            area_id = config.get("id", "")
            name = config.get("metadata", {}).get("name", "Unknown Area")
            channels: list[LightChannel] = []

            for channel in config.get("channels", []):
                ch_id = channel.get("channel_id", 0)
                members = channel.get("members", [])
                service_id = ""
                if members:
                    service = members[0].get("service", {})
                    service_id = service.get("rid", "")
                position_data = channel.get("position", {})
                position = (
                    position_data.get("x", 0.0),
                    position_data.get("y", 0.0),
                    position_data.get("z", 0.0),
                )
                channels.append(
                    LightChannel(
                        channel_id=ch_id,
                        service_id=service_id,
                        name=f"Channel {ch_id}",
                        position=position,
                    )
                )

            areas.append(EntertainmentArea(id=area_id, name=name, channels=channels))

        return areas

    async def start_entertainment(self, area_id: str) -> None:
        """Start entertainment mode for the given area."""
        await self._request(
            "PUT",
            f"/clip/v2/resource/entertainment_configuration/{area_id}",
            json_data={"action": "start"},
        )
        LOGGER.info("Entertainment mode started for area %s", area_id)

    async def stop_entertainment(self, area_id: str) -> None:
        """Stop entertainment mode for the given area."""
        try:
            await self._request(
                "PUT",
                f"/clip/v2/resource/entertainment_configuration/{area_id}",
                json_data={"action": "stop"},
            )
            LOGGER.info("Entertainment mode stopped for area %s", area_id)
        except (aiohttp.ClientError, Exception) as err:
            LOGGER.debug("Error stopping entertainment mode: %s", err)

    async def get_bridge_id(self) -> str | None:
        """Fetch the bridge ID from the config endpoint."""
        try:
            result = await self._request("GET", "/clip/v2/resource/bridge")
            data = result.get("data", []) if isinstance(result, dict) else result
            if data and isinstance(data, list):
                bridge_id = data[0].get("id")
                return str(bridge_id) if bridge_id else None
        except (aiohttp.ClientError, Exception) as err:
            LOGGER.debug("Failed to get bridge ID: %s", err)
        return None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None or self._session.closed:
            # Hue bridge uses a self-signed certificate
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to the Hue API."""
        session = await self._get_session()
        headers: dict[str, str] = {}
        if self._app_key:
            headers["hue-application-key"] = self._app_key
        url = f"{self.base_url}{path}"
        async with session.request(method, url, headers=headers, json=json_data, ssl=False) as resp:
            resp.raise_for_status()
            return await resp.json()
