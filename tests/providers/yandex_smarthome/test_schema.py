"""Tests for provider/schema.py — dataclass models and enums."""

from __future__ import annotations

from dataclasses import asdict

from provider.schema import (
    ActionRequestPayload,
    ActionResult,
    CallbackPayload,
    CallbackRequest,
    CapabilityAction,
    CapabilityActionResult,
    CapabilityActionResultState,
    CapabilityActionState,
    CapabilityDescription,
    CapabilityInstanceState,
    CapabilityParameters,
    CapabilityState,
    CloudRequest,
    CloudResponse,
    DeviceAction,
    DeviceDescription,
    DeviceListPayload,
    DeviceState,
    DeviceStatesPayload,
    RangeParameters,
    YandexCapabilityType,
    YandexDeviceInfo,
    YandexDeviceType,
    YandexRangeInstance,
    YandexResponseCode,
    YandexToggleInstance,
)

# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


class TestEnums:
    """Test enum string values match Yandex Smart Home API."""

    def test_device_types(self) -> None:
        """Test device types."""
        assert YandexDeviceType.MEDIA_DEVICE == "devices.types.media_device"
        assert YandexDeviceType.MEDIA_DEVICE_RECEIVER == "devices.types.media_device.receiver"

    def test_capability_types(self) -> None:
        """Test capability types."""
        assert YandexCapabilityType.ON_OFF == "devices.capabilities.on_off"
        assert YandexCapabilityType.RANGE == "devices.capabilities.range"
        assert YandexCapabilityType.TOGGLE == "devices.capabilities.toggle"

    def test_range_instances(self) -> None:
        """Test range instances."""
        assert YandexRangeInstance.VOLUME == "volume"

    def test_toggle_instances(self) -> None:
        """Test toggle instances."""
        assert YandexToggleInstance.MUTE == "mute"
        assert YandexToggleInstance.PAUSE == "pause"

    def test_response_codes(self) -> None:
        """Test response codes."""
        assert YandexResponseCode.DONE == "DONE"
        assert YandexResponseCode.DEVICE_UNREACHABLE == "DEVICE_UNREACHABLE"
        assert YandexResponseCode.INVALID_ACTION == "INVALID_ACTION"
        assert YandexResponseCode.INTERNAL_ERROR == "INTERNAL_ERROR"
        assert YandexResponseCode.DEVICE_NOT_FOUND == "DEVICE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Serialization roundtrips
# ---------------------------------------------------------------------------


class TestDeviceDescription:
    """Test DeviceDescription serialization."""

    def test_minimal(self) -> None:
        """Test minimal."""
        desc = DeviceDescription(
            id="p1", name="Living Room", type=YandexDeviceType.MEDIA_DEVICE_RECEIVER
        )
        data = asdict(desc)
        assert data["id"] == "p1"
        assert data["name"] == "Living Room"
        assert data["type"] == "devices.types.media_device.receiver"
        assert data["capabilities"] == []

    def test_with_capabilities(self) -> None:
        """Test with capabilities."""
        desc = DeviceDescription(
            id="p1",
            name="Speaker",
            type=YandexDeviceType.MEDIA_DEVICE_RECEIVER,
            capabilities=[
                CapabilityDescription(type=YandexCapabilityType.ON_OFF),
                CapabilityDescription(
                    type=YandexCapabilityType.RANGE,
                    parameters=CapabilityParameters(
                        instance="volume",
                        range=RangeParameters(min=0, max=100, precision=1),
                        unit="unit.percent",
                    ),
                ),
            ],
            device_info=YandexDeviceInfo(manufacturer="Test", model="X1"),
        )
        data = asdict(desc)
        assert len(data["capabilities"]) == 2
        assert data["capabilities"][0]["type"] == "devices.capabilities.on_off"
        vol_cap = data["capabilities"][1]
        assert vol_cap["parameters"]["instance"] == "volume"
        assert vol_cap["parameters"]["range"]["max"] == 100
        assert data["device_info"]["manufacturer"] == "Test"


class TestDeviceState:
    """Test DeviceState serialization."""

    def test_with_capabilities(self) -> None:
        """Test with capabilities."""
        state = DeviceState(
            id="p1",
            capabilities=[
                CapabilityState(
                    type=YandexCapabilityType.ON_OFF,
                    state=CapabilityInstanceState(instance="on", value=True),
                ),
            ],
        )
        data = asdict(state)
        assert data["id"] == "p1"
        assert data["capabilities"][0]["state"]["value"] is True
        assert data["error_code"] is None

    def test_error_state(self) -> None:
        """Test error state."""
        state = DeviceState(id="p1", error_code="DEVICE_UNREACHABLE", error_message="Offline")
        data = asdict(state)
        assert data["error_code"] == "DEVICE_UNREACHABLE"
        assert data["capabilities"] == []


class TestActionRequestPayload:
    """Test action request parsing structures."""

    def test_single_device_action(self) -> None:
        """Test single device action."""
        payload = ActionRequestPayload(
            devices=[
                DeviceAction(
                    id="p1",
                    capabilities=[
                        CapabilityAction(
                            type=YandexCapabilityType.ON_OFF,
                            state=CapabilityActionState(instance="on", value=True),
                        ),
                    ],
                ),
            ],
        )
        data = asdict(payload)
        assert len(data["devices"]) == 1
        assert data["devices"][0]["id"] == "p1"
        cap = data["devices"][0]["capabilities"][0]
        assert cap["state"]["relative"] is False

    def test_relative_volume(self) -> None:
        """Test relative volume."""
        action = CapabilityAction(
            type=YandexCapabilityType.RANGE,
            state=CapabilityActionState(instance="volume", value=10, relative=True),
        )
        data = asdict(action)
        assert data["state"]["relative"] is True
        assert data["state"]["value"] == 10


class TestActionResult:
    """Test action result structures."""

    def test_success(self) -> None:
        """Test success."""
        result = ActionResult(status="DONE")
        assert asdict(result) == {"status": "DONE", "error_code": None, "error_message": None}

    def test_error(self) -> None:
        """Test error."""
        result = ActionResult(status="ERROR", error_code="INVALID_ACTION", error_message="Oops")
        data = asdict(result)
        assert data["status"] == "ERROR"
        assert data["error_code"] == "INVALID_ACTION"


class TestCallbackRequest:
    """Test callback state request."""

    def test_serialization(self) -> None:
        """Test serialization."""
        req = CallbackRequest(
            ts=1234567890.0,
            payload=CallbackPayload(
                user_id="test_user",
                devices=[DeviceState(id="p1")],
            ),
        )
        data = asdict(req)
        assert data["ts"] == 1234567890.0
        assert data["payload"]["user_id"] == "test_user"
        assert len(data["payload"]["devices"]) == 1


class TestCloudMessages:
    """Test cloud WebSocket message models."""

    def test_cloud_request(self) -> None:
        """Test cloud request."""
        req = CloudRequest(
            request_id="abc-123", action="/v1.0/user/devices", message={"key": "val"}
        )
        assert req.request_id == "abc-123"
        assert req.action == "/v1.0/user/devices"
        assert req.message == {"key": "val"}

    def test_cloud_request_no_message(self) -> None:
        """Test cloud request no message."""
        req = CloudRequest(request_id="abc", action="/v1.0/user/unlink")
        assert req.message is None

    def test_cloud_response(self) -> None:
        """Test cloud response."""
        resp = CloudResponse(request_id="abc", payload={"user_id": "u1"})
        data = asdict(resp)
        assert data["request_id"] == "abc"
        assert data["payload"]["user_id"] == "u1"


class TestDeviceListPayload:
    """Test response payload structures."""

    def test_empty(self) -> None:
        """Test empty."""
        payload = DeviceListPayload(user_id="u1")
        data = asdict(payload)
        assert data["user_id"] == "u1"
        assert data["devices"] == []

    def test_with_devices(self) -> None:
        """Test with devices."""
        payload = DeviceListPayload(
            user_id="u1",
            devices=[DeviceDescription(id="p1", name="Test", type="devices.types.media_device")],
        )
        assert len(payload.devices) == 1


class TestDeviceStatesPayload:
    """Test query response payload."""

    def test_serialization(self) -> None:
        """Test serialization."""
        payload = DeviceStatesPayload(devices=[DeviceState(id="p1")])
        data = asdict(payload)
        assert len(data["devices"]) == 1
        assert data["devices"][0]["id"] == "p1"


class TestCapabilityActionResult:
    """Test action result with default factory."""

    def test_default_result(self) -> None:
        """Test default result."""
        result = CapabilityActionResult(
            type=YandexCapabilityType.ON_OFF,
            state=CapabilityActionResultState(
                instance="on",
                value=True,
                action_result=ActionResult(status="DONE"),
            ),
        )
        assert result.state.action_result.status == "DONE"
        assert result.state.action_result.error_code is None
