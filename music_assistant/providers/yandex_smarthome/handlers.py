"""Yandex Smart Home API request handlers.

Pure async functions handling the 4 Smart Home API actions:
- /user/devices      — list all exposed MA players
- /user/devices/query — return states of requested devices
- /user/devices/action — execute capability actions
- /user/unlink        — user disconnected their account
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from .device import (
    execute_capability_action,
    get_device_description,
    get_device_state,
    is_player_exposable,
    make_error_action_result,
    make_error_device_state,
)
from .schema import (
    ActionRequestPayload,
    ActionResultPayload,
    CapabilityAction,
    CapabilityActionState,
    DeviceAction,
    DeviceActionResult,
    DeviceListPayload,
    DeviceState,
    DeviceStatesPayload,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_LOGGER = logging.getLogger(__name__)


async def handle_device_list(
    mass: MusicAssistant,
    user_id: str,
    exposed_ids: set[str] | None = None,
) -> DeviceListPayload:
    """Handle /user/devices — return list of all MA players as Yandex devices."""
    devices = []
    for player in mass.players.all_players():
        state = player.state
        if not is_player_exposable(state, exposed_ids=exposed_ids):
            continue
        devices.append(get_device_description(state))
    _LOGGER.debug("Device list: %d devices exposed", len(devices))
    return DeviceListPayload(user_id=user_id, devices=devices)


async def handle_devices_query(
    mass: MusicAssistant,
    device_ids: list[str],
    exposed_ids: set[str] | None = None,
) -> DeviceStatesPayload:
    """Handle /user/devices/query — return current states for requested devices."""
    states: list[DeviceState] = []
    for device_id in device_ids:
        try:
            player = mass.players.get_player(device_id)
        except Exception:
            player = None

        if player is None:
            states.append(make_error_device_state(device_id))
            continue

        player_state = player.state if hasattr(player, "state") else player
        if not is_player_exposable(player_state, exposed_ids=exposed_ids):  # type: ignore[arg-type]
            states.append(make_error_device_state(device_id))
            continue

        states.append(get_device_state(player_state))  # type: ignore[arg-type]

    return DeviceStatesPayload(devices=states)


async def handle_devices_action(
    mass: MusicAssistant,
    payload: ActionRequestPayload,
    exposed_ids: set[str] | None = None,
) -> ActionResultPayload:
    """Handle /user/devices/action — execute capability actions on devices."""
    results: list[DeviceActionResult] = []

    for device_action in payload.devices:
        try:
            player = mass.players.get_player(device_action.id)
        except Exception:
            player = None

        if player is None:
            results.append(
                DeviceActionResult(
                    id=device_action.id,
                    capabilities=make_error_action_result(
                        device_action.id, device_action.capabilities
                    ),
                )
            )
            continue

        player_state = player.state if hasattr(player, "state") else player
        if not is_player_exposable(player_state, exposed_ids=exposed_ids):  # type: ignore[arg-type]
            results.append(
                DeviceActionResult(
                    id=device_action.id,
                    capabilities=make_error_action_result(
                        device_action.id, device_action.capabilities
                    ),
                )
            )
            continue

        current_volume = player_state.volume_level or 0
        cap_results = []
        for cap_action in device_action.capabilities:
            result = await execute_capability_action(
                mass, device_action.id, cap_action, current_volume
            )
            cap_results.append(result)

        results.append(DeviceActionResult(id=device_action.id, capabilities=cap_results))

    return ActionResultPayload(devices=results)


async def handle_user_unlink() -> dict[str, Any]:
    """Handle /user/unlink — user disconnected their Yandex account."""
    _LOGGER.info("User unlinked Yandex Smart Home account")
    return {}


def parse_action_payload(raw: dict[str, Any]) -> ActionRequestPayload:
    """Parse a raw /user/devices/action message into ActionRequestPayload."""
    devices = []
    payload_obj = raw.get("payload", raw)
    if not isinstance(payload_obj, dict):
        payload_obj = {}
    for dev_raw in payload_obj.get("devices", []):
        dev_id = dev_raw.get("id")
        if not dev_id:
            continue
        capabilities = []
        for cap_raw in dev_raw.get("capabilities", []):
            cap_type = cap_raw.get("type")
            if not cap_type:
                continue
            state_raw = cap_raw.get("state", {})
            capabilities.append(
                CapabilityAction(
                    type=cap_type,
                    state=CapabilityActionState(
                        instance=state_raw.get("instance", ""),
                        value=state_raw.get("value"),
                        relative=state_raw.get("relative", False),
                    ),
                )
            )
        devices.append(DeviceAction(id=dev_id, capabilities=capabilities))
    return ActionRequestPayload(devices=devices)


def _strip_none(obj: Any) -> Any:
    """Recursively remove None values from dicts (Yandex API rejects null fields)."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(item) for item in obj]
    return obj


def build_response(request_id: str, payload: Any) -> dict[str, Any]:
    """Wrap a handler result in the Yandex Smart Home API response envelope."""
    if payload is None:
        return {"request_id": request_id, "payload": {}}
    if isinstance(payload, dict):
        return {"request_id": request_id, "payload": _strip_none(payload)}
    return {"request_id": request_id, "payload": _strip_none(asdict(payload))}
