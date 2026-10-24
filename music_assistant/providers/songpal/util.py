"""Pure helpers for SongPal device control.

This module does not import Music Assistant so the volume and power rules can
be tested on their own.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import TypeVar
from urllib.parse import urlsplit, urlunsplit

from .constants import ERROR_REQUEST_RETRY

_T = TypeVar("_T")

_BRAVIA_SERVICES = frozenset({"video", "videoScreen"})
_NULL_MACS = frozenset({"000000000000", "FFFFFFFFFFFF"})


def device_volume_from_level(level: int, max_volume: int) -> int:
    """Map a Music Assistant volume (0–100) to the device scale.

    Home Assistant stores the level as a fraction and sends
    ``round(fraction * maxVolume)``. A 0–100 level is that fraction times 100,
    so the device value is ``round(level / 100 * maxVolume)``. ``minVolume``
    is not used.
    """
    if max_volume <= 0:
        return 0
    return round(level / 100 * max_volume)


def level_from_device_volume(volume: int, max_volume: int) -> int:
    """Map a device volume to Music Assistant's 0–100 scale.

    Home Assistant reports ``volume / maxVolume``. The integer level is
    ``round(volume / maxVolume * 100)``.
    """
    if max_volume <= 0:
        return 0
    return round(volume / max_volume * 100)


def is_power_retry(exc: BaseException) -> bool:
    """Return True when the device is already in the requested power state.

    python-songpal stores the device error code on ``SongpalException.code``.
    Code 40000 (``ERROR_REQUEST_RETRY``) means the power request was a no-op.
    """
    return getattr(exc, "code", None) == ERROR_REQUEST_RETRY


def normalize_mac(value: str | None) -> str | None:
    """Return an uppercase colon-separated MAC, or None when it is unusable."""
    if not value:
        return None
    cleaned = value.strip().replace(":", "").replace("-", "").replace(".", "").upper()
    if len(cleaned) != 12 or cleaned in _NULL_MACS:
        return None
    try:
        int(cleaned, 16)
    except ValueError:
        return None
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def player_id_from_macs(mac_addr: str | None, wireless_mac_addr: str | None) -> str | None:
    """Pick the player id the way Home Assistant picks ``unique_id``.

    Wired ``macAddr`` wins, then ``wirelessMacAddr``.
    """
    return normalize_mac(mac_addr) or normalize_mac(wireless_mac_addr)


def is_bravia_device(services: Iterable[str] | None) -> bool:
    """Return True for Bravia TVs, which Home Assistant's SSDP step rejects.

    The ScalarWebAPI service list contains ``video`` or ``videoScreen``.
    """
    if not services:
        return False
    return any(service in _BRAVIA_SERVICES for service in services)


def parse_manual_endpoints(value: str | None) -> list[str]:
    """Split the manual-endpoint config value into URLs, one per line."""
    if not value:
        return []
    endpoints: list[str] = []
    for line in str(value).splitlines():
        endpoint = line.strip()
        if not endpoint or endpoint.startswith("#"):
            continue
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            continue
        endpoints.append(endpoint)
    return endpoints


def endpoint_key(endpoint: str) -> str:
    """Normalize an endpoint URL so SSDP and manual entries dedupe."""
    return endpoint.strip().rstrip("/")


def is_main_zone(output: str | None) -> bool:
    """Return True for the main zone (``zone=1``) or an output with no zone."""
    if not output or "zone=" not in output:
        return True
    query = output.split("?", 1)[1] if "?" in output else output
    for part in query.split("&"):
        if part.startswith("zone="):
            return part.split("=", 1)[1] == "1"
    return True


def select_main_zone(items: Iterable[_T]) -> _T | None:
    """Return the first item whose ``output`` is the main zone."""
    for item in items:
        if is_main_zone(getattr(item, "output", None)):
            return item
    return None


def replace_url_host(url: str, ip: str) -> str:
    """Rewrite a URL host to an IP address. Leave URLs that already use an IP."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return url
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return url
    userinfo = ""
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        userinfo += "@"
    host_ip = f"[{ip}]" if ":" in ip else ip
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(
        (parts.scheme, f"{userinfo}{host_ip}{port}", parts.path, parts.query, parts.fragment)
    )


def playback_state_name(state: str | None, *, is_idle: bool) -> str | None:
    """Map a SongPal playback state to ``idle``, ``paused``, or ``playing``.

    ``PlayInfo.is_idle`` (no title) is idle. ``None`` means the report did not
    say, and the caller should keep the current state.
    """
    if is_idle:
        return "idle"
    if not state:
        return None
    normalized = state.strip().upper()
    if normalized in {"PLAYING", "STARTED"}:
        return "playing"
    if "PAUSE" in normalized:
        return "paused"
    if normalized in {"STOPPED", "STOP", "IDLE", "NO_MEDIA_PRESENT"}:
        return "idle"
    return None
