"""Setup flow for the Auracast player provider."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from serial.tools import list_ports

from music_assistant.models.setup_flow import SetupFlowError

from .broadcaster import BROADCAST_ERRORS, CONTROLLER_STACK_SETUP_KEY, probe_controller
from .constants import (
    CONF_BAUDRATE,
    CONF_BROADCAST_NAME,
    CONF_MANUFACTURER_DATA,
    CONF_SERIAL_PORT,
    DEFAULT_BAUDRATE,
    DEFAULT_BROADCAST_NAME,
    MAX_BAUDRATE,
    MIN_BAUDRATE,
    SUPPORTED_USB_IDS,
)
from .validators import validate_baudrate, validate_broadcast_name, validate_manufacturer_data

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from music_assistant.models.player_provider import PlayerProvider
    from music_assistant.models.setup_flow import SetupSession

LOGGER = logging.getLogger(__name__)
PROBE_TIMEOUT = 15
SERIAL_BY_ID_DIR = Path("/dev/serial/by-id")


async def run_setup(session: SetupSession) -> None:
    """
    Select a controller serial port, verify it and configure its broadcast.

    Only the port chosen by the user is opened, so other serial devices on the host
    (e.g. Zigbee or Z-Wave sticks) are never touched.

    :param session: The setup session driving the flow.
    """
    released_players: list[Player] = []
    finished = False
    try:
        await _configure_controller(session, released_players)
        finished = True
    finally:
        if not finished and released_players:
            # Shielded so that a cancelled setup still brings the broadcast back
            await asyncio.shield(_restore_players(released_players))


async def _configure_controller(session: SetupSession, released_players: list[Player]) -> None:
    """
    Ask for the controller settings until they verify and the setup finishes.

    :param session: The setup session driving the flow.
    :param released_players: Collects the players that were powered off to free the controller.
    """
    context = session.context
    setup_data: dict[str, Any] = {**context.setup_data, **context.values}
    current_port = str(setup_data.get(CONF_SERIAL_PORT) or "")
    current_baudrate = int(setup_data.get(CONF_BAUDRATE) or DEFAULT_BAUDRATE)
    reconfigure_instance_id: str | None = None
    if context.kind == "reconfigure":
        reconfigure_instance_id = context.instance_id
        if reconfigure_instance_id is None:
            raise SetupFlowError("Cannot reconfigure Auracast without a provider instance")

    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        ports = await asyncio.to_thread(list_serial_ports, current_port)
        if not ports:
            raise SetupFlowError("No supported Auracast controller was found on this host")
        selected_port = str(setup_data.get(CONF_SERIAL_PORT) or "")
        if selected_port not in ports:
            selected_port = next(iter(ports))
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_SERIAL_PORT,
                    type=ConfigEntryType.STRING,
                    required=True,
                    value=selected_port,
                    options=[ConfigValueOption(port, title=label) for port, label in ports.items()],
                ),
                ConfigEntry(
                    key=CONF_BAUDRATE,
                    type=ConfigEntryType.INTEGER,
                    required=True,
                    default_value=DEFAULT_BAUDRATE,
                    value=setup_data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
                    range=(MIN_BAUDRATE, MAX_BAUDRATE),
                    validate=validate_baudrate,
                ),
                ConfigEntry(
                    key=CONF_BROADCAST_NAME,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=DEFAULT_BROADCAST_NAME,
                    value=setup_data.get(CONF_BROADCAST_NAME, DEFAULT_BROADCAST_NAME),
                    validate=validate_broadcast_name,
                ),
                ConfigEntry(
                    key=CONF_MANUFACTURER_DATA,
                    type=ConfigEntryType.STRING,
                    required=False,
                    default_value="",
                    value=setup_data.get(CONF_MANUFACTURER_DATA, ""),
                    validate=validate_manufacturer_data,
                ),
            ],
            step_id="controller",
            errors=errors,
            last_step=True,
        )
        setup_data.update(values)
        setup_data[CONF_BAUDRATE] = int(setup_data[CONF_BAUDRATE])
        try:
            await _verify_controller(
                session, setup_data, current_port, current_baudrate, released_players
            )
        except SetupFlowError as err:
            errors = {"base": err}
            continue

        previous_values = {key: context.values.get(key) for key in values}
        finished = False
        try:
            if reconfigure_instance_id:
                for key, value in values.items():
                    session.mass.config.set_raw_provider_config_value(
                        reconfigure_instance_id, key, value
                    )
            await session.finish(setup_data)
            finished = True
            return
        except SetupFlowError as err:
            errors = {"base": err}
        finally:
            if reconfigure_instance_id and not finished:
                for key, value in previous_values.items():
                    session.mass.config.set_raw_provider_config_value(
                        reconfigure_instance_id, key, value
                    )


def list_serial_ports(current_port: str = "") -> dict[str, str]:
    """
    Return the supported controllers' serial ports, keyed by stable device path.

    Only USB devices whose vendor and product ID are in SUPPORTED_USB_IDS are listed, so other
    serial devices (e.g. Zigbee or Z-Wave sticks) are never offered. The `/dev/serial/by-id`
    path is used where available, as it survives re-plugging and reboots.

    :param current_port: The port of an already configured controller, always included.
    """
    ports = {
        _stable_device_path(port.device): f"{port.description} ({port.device})"
        for port in sorted(list_ports.comports(), key=lambda port: port.device)
        if (port.vid, port.pid) in SUPPORTED_USB_IDS
    }
    if current_port and current_port not in ports:
        ports[current_port] = current_port
    return ports


def _stable_device_path(device: str) -> str:
    """Return the `/dev/serial/by-id` link of a serial device, or the device itself."""
    if SERIAL_BY_ID_DIR.is_dir():
        real_device = os.path.realpath(device)
        for link in sorted(SERIAL_BY_ID_DIR.iterdir()):
            if os.path.realpath(link) == real_device:
                return str(link)
    return device


async def _verify_controller(
    session: SetupSession,
    setup_data: dict[str, Any],
    current_port: str,
    current_baudrate: int,
    released_players: list[Player],
) -> None:
    """
    Check that the selected port holds a controller able to broadcast Auracast.

    Records the controller's stack description in the setup data.

    :param session: The setup session driving the flow.
    :param setup_data: The collected values, updated with the controller stack.
    :param current_port: The serial port of the controller currently in use, if any.
    :param current_baudrate: The baud rate of the controller currently in use.
    :param released_players: Collects the players powered off to free the running controller.
    """
    port = str(setup_data[CONF_SERIAL_PORT])
    baudrate = int(setup_data[CONF_BAUDRATE])
    if session.context.kind == "reconfigure" and port == current_port:
        if baudrate == current_baudrate and setup_data.get(CONTROLLER_STACK_SETUP_KEY):
            # Already verified; the running broadcaster may hold the port open
            return
        released_players.extend(await _release_running_controller(session))
    setup_data.pop(CONTROLLER_STACK_SETUP_KEY, None)
    try:
        result = await asyncio.wait_for(probe_controller(port, baudrate), timeout=PROBE_TIMEOUT)
    except BROADCAST_ERRORS as err:
        raise SetupFlowError(
            f"Could not open a Bluetooth controller on {port} at {baudrate} baud: {err}"
        ) from err
    if not result.supports_auracast:
        raise SetupFlowError(
            f"The controller on {port} cannot broadcast Auracast: "
            f"missing {', '.join(result.missing_support)}"
        )
    if result.controller_stack:
        setup_data[CONTROLLER_STACK_SETUP_KEY] = result.controller_stack
    LOGGER.debug("Verified Auracast controller on %s (stack=%s)", port, result.controller_stack)


async def _release_running_controller(session: SetupSession) -> list[Player]:
    """Power off the configured provider's players so the broadcaster releases the serial port."""
    instance_id = session.context.instance_id
    provider = session.mass.get_provider(instance_id) if instance_id else None
    if provider is None:
        return []
    released = [player for player in cast("PlayerProvider", provider).players if player.powered]
    for player in released:
        await player.power(False)
    return released


async def _restore_players(players: list[Player]) -> None:
    """Power the players back on, resuming the broadcast that setup interrupted."""
    for player in players:
        await player.power(True)
