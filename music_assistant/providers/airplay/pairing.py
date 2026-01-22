"""Pairing helper for Apple devices using pyatv."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pyatv
from music_assistant_models.errors import PlayerCommandFailed
from pyatv.const import PairingRequirement
from pyatv.const import Protocol as PyAtvProtocol

from .constants import StreamingProtocol

if TYPE_CHECKING:
    from pyatv.interface import BaseConfig as ATVConfig
    from pyatv.interface import PairingHandler


def streaming_protocol_to_pyatv(protocol: StreamingProtocol) -> PyAtvProtocol:
    """Convert Music Assistant StreamingProtocol to pyatv Protocol.

    :param protocol: Music Assistant streaming protocol.
    :return: Corresponding pyatv Protocol.
    """
    if protocol == StreamingProtocol.RAOP:
        return PyAtvProtocol.RAOP
    # StreamingProtocol.AIRPLAY2 maps to pyatv's AirPlay protocol
    return PyAtvProtocol.AirPlay


class AirPlayPairing:
    """Handle pairing for Apple devices using pyatv.

    This class manages the pairing process for Apple devices (Apple TV, Mac, HomePod)
    using the pyatv library. It handles device discovery, pairing initiation,
    PIN exchange, and credential retrieval.
    """

    def __init__(
        self,
        address: str,
        name: str,
        protocol: StreamingProtocol,
        logger: logging.Logger,
    ) -> None:
        """Initialize AirPlayPairing.

        :param address: IP address of the device to pair with.
        :param name: Display name of the device.
        :param protocol: The streaming protocol to pair for (RAOP or AIRPLAY2).
        :param logger: Logger instance for logging pairing events.
        """
        self.address = address
        self.name = name
        self.protocol = protocol
        self.pyatv_protocol = streaming_protocol_to_pyatv(protocol)
        self.logger = logger
        self._pairing_handler: PairingHandler | None = None
        self._config: ATVConfig | None = None
        self._device_provides_pin: bool = True

    @property
    def is_pairing(self) -> bool:
        """Return True if a pairing session is in progress."""
        return self._pairing_handler is not None

    @property
    def device_provides_pin(self) -> bool:
        """Return True if the device displays the PIN (user must enter it in MA)."""
        return self._device_provides_pin

    @property
    def protocol_name(self) -> str:
        """Return human-readable protocol name."""
        if self.protocol == StreamingProtocol.RAOP:
            return "RAOP (AirPlay 1)"
        return "AirPlay"

    async def scan_device(self) -> ATVConfig | None:
        """Scan for device and return pyatv config.

        :return: Device configuration if found, None otherwise.
        """
        try:
            atvs = await pyatv.scan(
                asyncio.get_event_loop(),
                hosts=[self.address],
                timeout=5,
            )
            return atvs[0] if atvs else None
        except Exception as err:
            self.logger.warning("Failed to scan device %s: %s", self.address, err)
            return None

    async def requires_pairing(self) -> bool:
        """Check if device requires pairing for the configured protocol.

        :return: True if pairing is required, False otherwise.
        """
        config = await self.scan_device()
        if not config:
            return False

        # Check pairing requirement for the configured protocol
        for service in config.services:
            if service.protocol == self.pyatv_protocol:
                return service.pairing not in (
                    PairingRequirement.NotNeeded,
                    PairingRequirement.Unsupported,
                )
        return False

    async def start_pairing(self) -> bool:
        """Start pairing for the configured protocol.

        :return: True if device provides PIN (user enters in MA), False if MA provides PIN.
        :raises PlayerCommandFailed: If device not found or pairing fails to start.
        """
        self._config = await self.scan_device()
        if not self._config:
            raise PlayerCommandFailed(f"Device {self.name} not found for pairing")

        self.logger.info(
            "Starting %s pairing with %s",
            self.protocol_name,
            self.name,
        )

        try:
            self._pairing_handler = await pyatv.pair(
                self._config,
                self.pyatv_protocol,
                asyncio.get_event_loop(),
                name="Music Assistant",
            )
            await self._pairing_handler.begin()
            self._device_provides_pin = self._pairing_handler.device_provides_pin

            if self._device_provides_pin:
                self.logger.info(
                    "Device %s is displaying PIN - user should enter it in Music Assistant",
                    self.name,
                )
            else:
                self.logger.info(
                    "Music Assistant will provide PIN to enter on device %s",
                    self.name,
                )

            return self._device_provides_pin

        except Exception as err:
            self.logger.exception("Failed to start pairing with %s", self.name)
            await self.close()
            raise PlayerCommandFailed(f"Failed to start pairing: {err}") from err

    async def finish_pairing(self, pin: str) -> str:
        """Complete pairing with PIN and return credentials.

        :param pin: The PIN code (4 digits) entered by user or displayed to user.
        :return: Credentials string to store for future connections.
        :raises PlayerCommandFailed: If pairing not started, PIN invalid, or pairing fails.
        """
        if not self._pairing_handler:
            raise PlayerCommandFailed("Pairing not started")

        self.logger.info("Completing %s pairing with %s using PIN", self.protocol_name, self.name)

        try:
            self._pairing_handler.pin(int(pin))
            await self._pairing_handler.finish()

            if not self._pairing_handler.has_paired:
                raise PlayerCommandFailed("Pairing failed - device did not confirm")

            credentials: str | None = self._pairing_handler.service.credentials
            if not credentials:
                raise PlayerCommandFailed("Pairing completed but no credentials returned")

            self.logger.info("Successfully paired %s with %s", self.protocol_name, self.name)

            return credentials

        except PlayerCommandFailed:
            raise
        except Exception as err:
            self.logger.exception("Pairing failed with %s", self.name)
            raise PlayerCommandFailed(f"Pairing failed: {err}") from err
        finally:
            await self.close()

    async def close(self) -> None:
        """Clean up pairing handler."""
        if self._pairing_handler:
            try:
                await self._pairing_handler.close()
            except Exception as err:
                self.logger.debug("Error closing pairing handler: %s", err)
            self._pairing_handler = None
        self._config = None
