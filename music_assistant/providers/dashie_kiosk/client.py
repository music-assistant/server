"""Async client for the Dashie Kiosk REST API."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)


class DashieKioskError(Exception):
    """Raised when a Dashie Kiosk API request fails."""

    def __init__(self, status_code: int, message: str) -> None:
        """Initialize the error."""
        self.status_code = status_code
        self.message = message
        super().__init__(f"DashieKioskError({status_code}): {message}")


class DashieKioskClient:
    """Client for communicating with the Dashie Kiosk REST API."""

    def __init__(
        self,
        session: ClientSession,
        host: str,
        port: str,
        password: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._host = host
        self._port = port
        self._password = password
        self._device_info: dict[str, Any] = {}

    @property
    def device_info(self) -> dict[str, Any]:
        """Return the cached device info."""
        return self._device_info

    async def _send_command(self, cmd: str, **kwargs: Any) -> dict[str, Any]:
        """Send a command to the Dashie Kiosk API."""
        url = f"http://{self._host}:{self._port}"
        params: list[tuple[str, str]] = [
            ("cmd", cmd),
            ("password", self._password),
            ("type", "json"),
        ]
        for key, value in kwargs.items():
            if value is not None:
                params.append((key, str(value)))

        _LOGGER.debug("Sending command to %s: %s %s", url, cmd, kwargs)
        async with self._session.get(
            url, params=params, headers={"Accept": "application/json"}, ssl=False
        ) as response:
            if response.status != 200:
                raise DashieKioskError(response.status, await response.text())
            content_type = response.headers.get("Content-Type", "")
            data = await response.json(content_type=content_type)
            if isinstance(data, dict) and data.get("status") == "Error":
                raise DashieKioskError(401, data.get("statustext", "Unknown error"))
            return dict(data)

    async def get_device_info(self) -> dict[str, Any]:
        """Get device information and update the cache."""
        self._device_info = await self._send_command("deviceInfo")
        return self._device_info

    async def play_sound(self, url: str, stream: int = 4) -> None:
        """Play an audio URL on the device."""
        await self._send_command("playSound", url=url, stream=stream)

    async def stop_sound(self) -> None:
        """Stop audio playback."""
        await self._send_command("stopSound")

    async def pause_sound(self) -> None:
        """Pause audio playback."""
        await self._send_command("pauseSound")

    async def resume_sound(self) -> None:
        """Resume audio playback."""
        await self._send_command("resumeSound")

    async def seek_sound(self, position_ms: int) -> None:
        """Seek to a position in the current audio (milliseconds)."""
        await self._send_command("seekSound", position=position_ms)

    async def set_audio_volume(self, level: int, stream: int = 4) -> None:
        """Set the audio volume (0-100)."""
        await self._send_command("setAudioVolume", level=level, stream=stream)
