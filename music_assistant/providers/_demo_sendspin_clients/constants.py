"""Constants for the Demo Sendspin Clients provider."""

from __future__ import annotations

from typing import Final

CONF_SCENARIOS: Final[str] = "scenarios"

# Action ids are suffixed onto a scenario id, so one entry key stays unique per device.
ACTION_SEPARATOR: Final[str] = "__"
ACTION_PRESS_BUTTON: Final[str] = "press_button"
ACTION_RESET: Final[str] = "reset"
ACTION_REFRESH: Final[str] = "refresh"

# Manufacturer reported by every fake device. Deliberately not "Music Assistant": the Sendspin
# provider reads that (and a handful of product names) as a web/app player and then skips the
# whole pairing UI, which is exactly what these devices exist to show.
DEVICE_MANUFACTURER: Final[str] = "Sendspin Demo"

# Fixed PIN for the static-PIN scenarios. aiosendspin requires exactly 8 decimal digits.
STATIC_PIN: Final[str] = "13571357"

# Seconds between reconnect attempts while the Sendspin server is unreachable.
RECONNECT_INTERVAL: Final[float] = 5.0
