"""PyATV Manager for handling Apple TV connections and pairing."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pyatv
from pyatv import exceptions as pyatv_exceptions
from pyatv.const import Protocol

if TYPE_CHECKING:
    from pyatv.interface import AppleTV, PairingHandler

    from .player import ApplePlayer
    from .provider import AirPlayProvider


class PyAtvManager:
    """Manager for pyatv connections to Apple TV devices."""

    def __init__(self, provider: AirPlayProvider) -> None:
        """Initialize PyAtvManager."""
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger
        self._connections: dict[str, AppleTV] = {}
        self._pairing_handlers: dict[str, PairingHandler] = {}
        self._state_listeners: dict[str, asyncio.Task[None]] = {}

    async def close(self) -> None:
        """Close all connections and cleanup."""
        # Cancel all state listeners
        for task in self._state_listeners.values():
            if not task.done():
                task.cancel()

        # Close all pairing handlers
        for handler in self._pairing_handlers.values():
            await handler.close()
        self._pairing_handlers.clear()

        # Close all connections
        for atv in self._connections.values():
            atv.close()
        self._connections.clear()

    async def connect(self, player: ApplePlayer, credentials: str | None = None) -> AppleTV:
        """
        Connect to an Apple TV device.

        Args:
            player: The ApplePlayer instance
            credentials: Optional pyatv credentials string

        Returns:
            AppleTV: The connected pyatv interface

        """
        player_id = player.player_id

        # Close existing connection if any
        if player_id in self._connections:
            await self.disconnect(player_id)

        # Scan for the specific device
        address = player.address
        self.logger.debug("Scanning for Apple TV at %s", address)

        atvs = await pyatv.scan(
            self.mass.loop,
            hosts=[address],
            timeout=5,
        )

        if not atvs:
            msg = f"Could not find Apple TV at {address}"
            raise ConnectionError(msg)

        config = atvs[0]

        # Apply credentials if provided
        if credentials:
            # Credentials format from pyatv is "device_id:private_key"
            # We need to apply this to the AirPlay protocol service
            for service in config.services:
                if service.protocol == Protocol.AirPlay:
                    config.set_credentials(Protocol.AirPlay, credentials)
                    self.logger.debug("Applied stored credentials for %s", player.display_name)
                    break

        # Connect to device
        self.logger.debug("Connecting to Apple TV %s", player.display_name)
        try:
            atv = await pyatv.connect(config, self.mass.loop)
            self._connections[player_id] = atv

            # Start state listener
            self._start_state_listener(player_id, atv)

            self.logger.info("Successfully connected to Apple TV %s", player.display_name)
            return atv

        except pyatv_exceptions.AuthenticationError as err:
            self.logger.warning("Authentication failed for %s: %s", player.display_name, err)
            raise
        except Exception as err:
            self.logger.error(
                "Failed to connect to Apple TV %s: %s",
                player.display_name,
                err,
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            raise

    async def disconnect(self, player_id: str) -> None:
        """Disconnect from an Apple TV device."""
        # Cancel state listener
        if player_id in self._state_listeners:
            task = self._state_listeners.pop(player_id)
            if not task.done():
                task.cancel()

        # Close connection
        if player_id in self._connections:
            atv = self._connections.pop(player_id)
            atv.close()
            self.logger.debug("Disconnected from Apple TV %s", player_id)

    async def start_pairing(
        self, player: ApplePlayer, protocol: Protocol = Protocol.AirPlay
    ) -> PairingHandler:
        """
        Start pairing process for a device.

        Args:
            player: The ApplePlayer instance
            protocol: The protocol to pair with (default: AirPlay)

        Returns:
            PairingHandler: The pairing handler to complete pairing

        """
        player_id = player.player_id
        address = player.address

        # Scan for the specific device
        self.logger.debug("Scanning for Apple TV at %s for pairing", address)
        atvs = await pyatv.scan(
            self.mass.loop,
            hosts=[address],
            timeout=5,
        )

        if not atvs:
            msg = f"Could not find Apple TV at {address}"
            raise ConnectionError(msg)

        config = atvs[0]

        # Start pairing
        self.logger.info("Starting pairing for %s (protocol: %s)", player.display_name, protocol)
        pairing = await pyatv.pair(config, protocol, self.mass.loop)
        await pairing.begin()

        # Store pairing handler for later completion
        self._pairing_handlers[player_id] = pairing

        return pairing

    async def finish_pairing(self, player_id: str, pin: int) -> str:
        """
        Finish pairing process with PIN code.

        Args:
            player_id: The player ID
            pin: The PIN code from the device or user

        Returns:
            str: The credentials string to store

        """
        if player_id not in self._pairing_handlers:
            msg = f"No active pairing session for {player_id}"
            raise ValueError(msg)

        pairing = self._pairing_handlers[player_id]

        # Provide PIN
        pairing.pin(pin)

        # Complete pairing
        await pairing.finish()

        if not pairing.has_paired:
            msg = "Pairing failed"
            raise RuntimeError(msg)

        # Get credentials
        credentials = pairing.service.credentials
        if not credentials:
            msg = "Pairing succeeded but no credentials were returned"
            raise RuntimeError(msg)

        self.logger.info("Pairing completed for %s", player_id)

        # Cleanup pairing handler
        await pairing.close()
        del self._pairing_handlers[player_id]

        return credentials

    async def cancel_pairing(self, player_id: str) -> None:
        """Cancel an active pairing session."""
        if player_id in self._pairing_handlers:
            pairing = self._pairing_handlers.pop(player_id)
            await pairing.close()
            self.logger.debug("Cancelled pairing for %s", player_id)

    def get_connection(self, player_id: str) -> AppleTV | None:
        """Get active connection for a player."""
        conn: AppleTV | None = self._connections.get(player_id)
        return conn

    def is_connected(self, player_id: str) -> bool:
        """Check if a player has an active connection."""
        return player_id in self._connections

    def is_pairing(self, player_id: str) -> bool:
        """Check if a player has an active pairing session."""
        return player_id in self._pairing_handlers

    def _start_state_listener(self, player_id: str, atv: AppleTV) -> None:
        """Start listening for state updates from the device."""
        # Cancel existing listener if any
        if player_id in self._state_listeners:
            task = self._state_listeners[player_id]
            if not task.done():
                task.cancel()

        # Create new listener task
        task = self.mass.create_task(self._state_listener_task(player_id, atv))
        self._state_listeners[player_id] = task

    async def _state_listener_task(self, player_id: str, atv: AppleTV) -> None:
        """Task to listen for state updates from the device."""
        player = self.provider.get_player(player_id)
        if not player:
            return

        self.logger.debug("Starting state listener for %s", player.display_name)

        try:
            # Get push updates interface
            push_updater = atv.push_updater

            # Register state listener
            def _on_state_update() -> None:
                """Handle state update from device."""
                # This will be called when device state changes
                # We'll process it in the main event loop
                self.mass.create_task(self._handle_state_update(player_id, atv))

            push_updater.listener = _on_state_update
            push_updater.start()

            # Keep the task alive while connected
            while player_id in self._connections:
                await asyncio.sleep(1)

        except Exception as err:
            self.logger.error(
                "State listener error for %s: %s",
                player.display_name,
                err,
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
        finally:
            self.logger.debug("State listener stopped for %s", player.display_name)

    async def _handle_state_update(self, player_id: str, atv: AppleTV) -> None:
        """Handle state update from device."""
        player = self.provider.get_player(player_id)
        if not player:
            return

        # This will be implemented later to update player state
        # For now, just log the update
        try:
            # Get the playing state from metadata
            playing = await atv.metadata.playing()
            self.logger.debug(
                "State update for %s: device_state=%s, title=%s",
                player.display_name,
                playing.device_state if playing else "unknown",
                playing.title if playing else "unknown",
            )
        except Exception as err:
            self.logger.debug("Could not get state from device: %s", err)
