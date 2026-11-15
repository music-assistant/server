"""Helpers for Sendspin provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiosendspin.models.types import PairAbortReason, PairingCodeFormat, PairMethod
from aiosendspin.noise.driver import HandshakeAbortedError
from aiosendspin.noise.pairing import PairingAbortError, PairingError, PairingTimeoutError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .constants import BRIDGE_PREFIX

if TYPE_CHECKING:
    from aiosendspin.models.core import (
        ClientHelloPayload,
        DynamicPairMethodDescriptor,
        PairMethodDescriptor,
    )
    from aiosendspin.models.management import ManagementResultData
    from aiosendspin.server import SendspinClient
    from aiosendspin.server.roles import Role


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
    info: ClientHelloPayload | None, config: ManagementResultData | None
) -> dict[PairMethod, PairMethodDescriptor | DynamicPairMethodDescriptor | None]:
    """
    Return the pairing methods the device currently offers, with their hello descriptors.

    A pairing config fetched over a management session on the current connection is
    authoritative; the hello advertisement cannot reflect config changes until reconnect.
    A method the config enables beyond the hello maps to None. The dynamic pairing code is
    left out when the hello rules out entering it as digits.
    """
    offered = info.supported_pair_methods if info is not None else None
    advertised: dict[PairMethod, PairMethodDescriptor | DynamicPairMethodDescriptor | None] = {}
    if offered is not None:
        advertised = {
            PairMethod.PAIRING_PSK: offered.pairing_psk,
            PairMethod.STATIC_PAIRING_CODE: offered.static_pairing_code,
            PairMethod.DYNAMIC_PAIRING_CODE: offered.dynamic_pairing_code,
        }
    methods: dict[PairMethod, PairMethodDescriptor | DynamicPairMethodDescriptor | None]
    if config is None:
        methods = {method: d for method, d in advertised.items() if d is not None}
    else:
        enabled = {
            PairMethod.PAIRING_PSK: config.pairing_psk,
            PairMethod.STATIC_PAIRING_CODE: config.static_pairing_code,
            PairMethod.DYNAMIC_PAIRING_CODE: config.dynamic_pairing_code,
        }
        methods = {
            method: advertised.get(method)
            for method, method_config in enabled.items()
            if method_config is not None and method_config.enabled
        }
    dynamic = offered.dynamic_pairing_code if offered is not None else None
    if dynamic is not None and PairingCodeFormat.DIGITS.value not in dynamic.formats:
        methods.pop(PairMethod.DYNAMIC_PAIRING_CODE, None)
    return methods


def pin_code_format(length: int) -> str:
    """Return the PAIRING_CODE entry format for a numeric PIN of `length` digits."""
    if length >= 6 and length % 2 == 0:
        half = length // 2
        return f"{'#' * half}-{'#' * half}"
    return "#" * length


def effective_unpaired_access(
    info: ClientHelloPayload | None, config: ManagementResultData | None
) -> bool:
    """
    Whether the device currently offers unpaired access.

    A pairing config fetched over a management session on the current connection is
    authoritative; the hello advertisement cannot reflect config changes until reconnect.
    """
    if config is not None and config.unpaired_access is not None:
        return config.unpaired_access.enabled
    return info is not None and info.unpaired_access.enabled


# DEPRECATED(spec-pr-183): drop with the management activity itself.
def speaks_legacy_wire(info: ClientHelloPayload | None) -> bool:
    """
    Whether the device's hello arrived on the pre-1.0 wire.

    The management activity is deprecated and no 1.0 device implements it. Asking one
    anyway costs the operator a request that never answers, and aiosendspin drops the
    connection to resync the reply channel; a device that does answer is flagged
    non-compliant, which a strict server turns into a rejection. Both hello shapes below
    are themselves flagged, so a device that carries one only connects at all while
    legacy clients are allowed.
    """
    if info is None:
        return False
    return bool(info.legacy_support_keys_used) or bool(info.legacy_pair_methods_list_used)


def bridge_client_id_from_mac(mac: str) -> str:
    """Generate a Sendspin bridge client ID from a MAC address."""
    return f"{BRIDGE_PREFIX}{mac.replace(':', '').lower()}"


def bridge_client_id_from_uuid(uuid: str) -> str:
    """Generate a Sendspin bridge client ID from a UUID."""
    return f"{BRIDGE_PREFIX}{uuid.replace('-', '').lower()}"


def bridge_client_id_variants(client_id: str) -> list[str]:
    """
    Return the Sendspin bridge client ids the bridges of one device can register under.

    The protocols of a device do not all report the same form of its MAC address, so a
    MAC based id comes with the id of that MAC with the locally administered bit toggled.

    :param client_id: A Sendspin bridge client id.
    :return: The client id itself, followed by its variant when it has one.
    """
    mac_hex = client_id[len(BRIDGE_PREFIX) :]
    if len(mac_hex) != 12:
        return [client_id]
    try:
        toggled = int(mac_hex[:2], 16) ^ 0x02
    except ValueError:
        return [client_id]
    return [client_id, f"{BRIDGE_PREFIX}{toggled:02x}{mac_hex[2:]}"]


def holds_bridge_client(client: SendspinClient, role: Role | None) -> bool:
    """
    Return whether a bridge still holds the Sendspin client it registered.

    Another bridge of the device can register the same client id while no connection is
    attached to the client: the server then hands that bridge the client, with roles of its
    own. A connection that attaches, such as that of the Sendspin Cast receiver, brings roles
    of its own too, but leaves the client to the bridge that registered it. A bridge that
    stops only removes a client it still holds.

    :param client: The client the registration of the bridge returned.
    :param role: The role the bridge got with that client, if any.
    """
    # nobody can register an id while a connection is attached to its client
    return (
        role is None
        or client.connection is not None
        or role in client.roles_by_family(role.role_family)
    )


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
