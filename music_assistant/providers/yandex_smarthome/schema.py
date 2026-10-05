"""
Dataclass models for the Yandex Smart Home API.

Covers device descriptions, capability states, action requests/results,
callback payloads, and cloud WebSocket messages.

Reference: https://yandex.ru/dev/dialogs/smart-home/doc/concepts/platform-protocol.html
Reference: https://github.com/dext0r/yandex_smart_home
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class YandexDeviceType(StrEnum):
    """Yandex Smart Home device types relevant to MA players."""

    MEDIA_DEVICE = "devices.types.media_device"
    MEDIA_DEVICE_RECEIVER = "devices.types.media_device.receiver"


class YandexCapabilityType(StrEnum):
    """Yandex Smart Home capability types."""

    ON_OFF = "devices.capabilities.on_off"
    RANGE = "devices.capabilities.range"
    TOGGLE = "devices.capabilities.toggle"
    MODE = "devices.capabilities.mode"


class YandexRangeInstance(StrEnum):
    """Range capability instances."""

    VOLUME = "volume"
    CHANNEL = "channel"


class YandexModeInstance(StrEnum):
    """Mode capability instances."""

    INPUT_SOURCE = "input_source"


class YandexToggleInstance(StrEnum):
    """Toggle capability instances."""

    MUTE = "mute"
    PAUSE = "pause"


class YandexResponseCode(StrEnum):
    """Yandex Smart Home API response/error codes."""

    DONE = "DONE"
    DEVICE_UNREACHABLE = "DEVICE_UNREACHABLE"
    INVALID_ACTION = "INVALID_ACTION"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Device description — returned by /user/devices
# ---------------------------------------------------------------------------


@dataclass
class RangeParameters:
    """Range capability parameters (min/max/precision)."""

    min: float = 0
    max: float = 100
    precision: float = 1


@dataclass
class ModeValue:
    """A single mode value for mode capabilities."""

    value: str


@dataclass
class CapabilityParameters:
    """Parameters block inside a capability description."""

    instance: str
    range: RangeParameters | None = None
    unit: str | None = None
    random_access: bool | None = None
    modes: list[ModeValue] | None = None


@dataclass
class CapabilityDescription:
    """A single capability in a device description."""

    type: str
    retrievable: bool = True
    reportable: bool = True
    parameters: CapabilityParameters | None = None


@dataclass
class YandexDeviceInfo:
    """Device info block."""

    manufacturer: str = "Music Assistant"
    model: str = "MA Player"
    sw_version: str | None = None


@dataclass
class DeviceDescription:
    """Full device description for /user/devices response."""

    id: str
    name: str
    type: str
    capabilities: list[CapabilityDescription] = field(default_factory=list)
    device_info: YandexDeviceInfo | None = None
    room: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Capability state — for /user/devices/query and state callbacks
# ---------------------------------------------------------------------------


@dataclass
class CapabilityInstanceState:
    """State of a specific capability instance."""

    instance: str
    value: Any


@dataclass
class CapabilityState:
    """A capability with its current state."""

    type: str
    state: CapabilityInstanceState


@dataclass
class DeviceState:
    """State of a single device (for query or callback)."""

    id: str
    capabilities: list[CapabilityState] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Action request — from /user/devices/action
# ---------------------------------------------------------------------------


@dataclass
class CapabilityActionState:
    """State portion of an action request capability."""

    instance: str
    value: Any
    relative: bool = False


@dataclass
class CapabilityAction:
    """A single capability action from Yandex."""

    type: str
    state: CapabilityActionState


@dataclass
class DeviceAction:
    """Action request for a single device."""

    id: str
    capabilities: list[CapabilityAction] = field(default_factory=list)


@dataclass
class ActionRequestPayload:
    """Payload of /user/devices/action request."""

    devices: list[DeviceAction] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Action result
# ---------------------------------------------------------------------------


@dataclass
class ActionResult:
    """Result of executing a single capability action."""

    status: str = "DONE"
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class CapabilityActionResultState:
    """
    State with action result for a single capability in an action response.

    Per Yandex Smart Home API, action_result goes inside 'state' alongside instance.
    """

    instance: str
    value: Any = None
    action_result: ActionResult = field(default_factory=ActionResult)


@dataclass
class CapabilityActionResult:
    """Result for a single capability in an action response."""

    type: str
    state: CapabilityActionResultState


@dataclass
class DeviceActionResult:
    """Action results for a single device."""

    id: str
    capabilities: list[CapabilityActionResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Response payloads
# ---------------------------------------------------------------------------


@dataclass
class DeviceListPayload:
    """Payload for /user/devices response."""

    user_id: str
    devices: list[DeviceDescription] = field(default_factory=list)


@dataclass
class DeviceStatesPayload:
    """Payload for /user/devices/query response."""

    devices: list[DeviceState] = field(default_factory=list)


@dataclass
class ActionResultPayload:
    """Payload for /user/devices/action response."""

    devices: list[DeviceActionResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Callback — state reporting to Yandex
# ---------------------------------------------------------------------------


@dataclass
class CallbackPayload:
    """Payload for callback/state POST."""

    user_id: str
    devices: list[DeviceState] = field(default_factory=list)


@dataclass
class CallbackRequest:
    """Full callback state request body."""

    ts: float
    payload: CallbackPayload


# ---------------------------------------------------------------------------
# Cloud WebSocket messages
# ---------------------------------------------------------------------------


@dataclass
class CloudRequest:
    """Incoming message from yaha-cloud.ru WebSocket."""

    request_id: str
    action: str
    message: dict[str, Any] | None = None


@dataclass
class CloudResponse:
    """Outgoing response to yaha-cloud.ru WebSocket."""

    request_id: str
    payload: dict[str, Any] = field(default_factory=dict)
