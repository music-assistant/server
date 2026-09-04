"""Helpers for Sendspin provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiosendspin.models.core import PairMethodDescriptor
from aiosendspin.models.types import PairAbortReason, PairMethod
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.pairing import PairingAbortError, PairingError, PairingTimeoutError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .constants import BRIDGE_PREFIX

if TYPE_CHECKING:
    from collections.abc import Iterable

    from aiosendspin.models.core import ClientHelloPayload
    from aiosendspin.models.management import ManagementResultData


class SecurityActionError(Exception):
    """A pairing/management action failure carrying a strings.json alert slug for the UI."""

    def __init__(self, alert_key: str, *, detail: str | None = None) -> None:
        """Initialize with the alert slug and optional untranslated {0}-placeholder detail."""
        super().__init__(alert_key if detail is None else f"{alert_key}: {detail}")
        self.alert_key = alert_key
        self.detail = detail


@dataclass(frozen=True)
class AlertText:
    """A strings.json slug and optional {0}-placeholder params for an operator ALERT entry."""

    key: str
    params: list[str] | None = None


_PAIR_ABORT_KEYS = {
    PairAbortReason.ATTEMPT_TIMEOUT: "pairing_error_timeout",
    PairAbortReason.CONCURRENT_ATTEMPT: "pairing_error_concurrent",
    PairAbortReason.METHOD_NOT_SUPPORTED: "pairing_error_method_unsupported",
    PairAbortReason.PAIRING_CODE_MISMATCH: "pairing_error_pin_mismatch",
    PairAbortReason.USER_CANCELLED: "pairing_error_cancelled",
}


def error_alert(err: Exception) -> AlertText:
    """Map a pairing or management failure to a localized operator alert."""
    if isinstance(err, SecurityActionError):
        return AlertText(err.alert_key, [err.detail] if err.detail is not None else None)
    if isinstance(err, PairingAbortError):
        key = _PAIR_ABORT_KEYS.get(err.reason)
        if key is not None:
            return AlertText(key)
        return AlertText("pairing_error_aborted", [err.reason.value])
    if isinstance(err, TimeoutError | PairingTimeoutError):
        return AlertText("pairing_error_timeout")
    if isinstance(err, OSError):
        return AlertText("pairing_error_storage", [str(err)])
    if isinstance(err, HandshakeAbortedError):
        return AlertText("pairing_error_handshake")
    if isinstance(err, PairingError):
        return AlertText("pairing_error_failed", [str(err)])
    return AlertText("pairing_error_generic")


def alert_entry(text: AlertText) -> ConfigEntry:
    """Build an ALERT config entry from a localized alert descriptor."""
    return ConfigEntry(key=text.key, type=ConfigEntryType.ALERT, translation_params=text.params)


def action_entry(action: str, *, advanced: bool = False) -> ConfigEntry:
    """Build an ACTION config entry whose key mirrors its action."""
    return ConfigEntry(key=action, type=ConfigEntryType.ACTION, action=action, advanced=advanced)


def effective_pair_methods(
    info: ClientHelloPayload | None,
    config: ManagementResultData | None,
) -> list[PairMethodDescriptor]:
    """
    Return the pairing methods the device currently offers.

    A pairing config fetched over a management session on the current connection is
    authoritative; the hello advertisement cannot reflect config changes until reconnect.
    Methods the config enables beyond the hello get a synthesized descriptor.
    """
    hello_methods = list(info.supported_pair_methods or []) if info is not None else []
    if config is None:
        return hello_methods
    advertised = {descriptor.method: descriptor for descriptor in hello_methods}
    methods: list[PairMethodDescriptor] = []
    for method, method_config in (
        (PairMethod.PAIRING_PSK, config.pairing_psk),
        (PairMethod.STATIC_PAIRING_CODE, config.static_pairing_code),
        (PairMethod.DYNAMIC_PAIRING_CODE, config.dynamic_pairing_code),
    ):
        if method_config is None or not method_config.enabled:
            continue
        descriptor = advertised.get(method)
        if descriptor is None:
            descriptor = PairMethodDescriptor(
                method=method,
                formats=["digits"] if method is PairMethod.DYNAMIC_PAIRING_CODE else None,
            )
        methods.append(descriptor)
    return methods


def pair_method_descriptor(
    methods: Iterable[PairMethodDescriptor],
    method: PairMethod,
) -> PairMethodDescriptor | None:
    """Return the descriptor for ``method``, or None when the device does not offer it."""
    return next((d for d in methods if d.method is method), None)


def effective_unpaired_access(
    info: ClientHelloPayload | None,
    config: ManagementResultData | None,
) -> bool:
    """
    Whether the device currently offers unpaired access.

    A pairing config fetched over a management session on the current connection is
    authoritative; the hello advertisement cannot reflect config changes until reconnect.
    """
    if config is not None and config.unpaired_access is not None:
        return config.unpaired_access.enabled
    return info is not None and info.unpaired_access.enabled


def bridge_client_id_from_mac(mac: str) -> str:
    """Generate a Sendspin bridge client ID from a MAC address."""
    return f"{BRIDGE_PREFIX}{mac.replace(':', '').lower()}"


def bridge_client_id_from_uuid(uuid: str) -> str:
    """Generate a Sendspin bridge client ID from a UUID."""
    return f"{BRIDGE_PREFIX}{uuid.replace('-', '').lower()}"


def mac_from_bridge_client_id(client_id: str) -> str | None:
    """Extract a MAC address from a Sendspin bridge client ID."""
    if not client_id.startswith(BRIDGE_PREFIX):
        return None
    mac_part = client_id[len(BRIDGE_PREFIX) :]
    if len(mac_part) != 12:
        return None
    if not all(ch in "0123456789abcdefABCDEF" for ch in mac_part):
        return None
    # Reconstruct MAC address with colons
    return ":".join(mac_part[i : i + 2] for i in range(0, 12, 2))
