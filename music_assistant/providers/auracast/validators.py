"""Validators shared by the Auracast setup flow and provider options."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .broadcaster import parse_manufacturer_data
from .constants import MAX_BAUDRATE, MIN_BAUDRATE

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType


def validate_manufacturer_data(value: ConfigValueType) -> bool:
    """Return whether optional manufacturer data uses the supported format."""
    try:
        parse_manufacturer_data(str(value or ""))
    except ValueError:
        return False
    return True


def validate_baudrate(value: ConfigValueType) -> bool:
    """Return whether a configured UART baud rate is within the supported range."""
    if not isinstance(value, str | int) or isinstance(value, bool):
        return False
    try:
        numeric_value = int(value)
    except ValueError:
        return False
    return MIN_BAUDRATE <= numeric_value <= MAX_BAUDRATE
