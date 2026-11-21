"""
Setup flow for the LIFX Lights Sync provider.

The picked devices are stored with their last known IP, so they stay reachable when a
broadcast is missed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from lifx_entertainment import LifxDevice, discover_devices
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError

from .constants import (
    CONF_BROADCAST,
    CONF_DEVICES,
    CONF_EXTRA_HOSTS,
    CONF_GROUPING,
    CONF_LIGHTS,
    DEFAULT_BROADCAST,
    DEFAULT_GROUPING,
    DISCOVERY_TIMEOUT_S,
    GROUPINGS,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

# a touch above the discovery window (which also fetches every light's details)
_DISCOVERY_STEP_TIMEOUT = DISCOVERY_TIMEOUT_S + 12.0


async def run_setup(session: SetupSession) -> None:
    """Run the LIFX discovery + light selection flow."""
    setup_data = session.context.setup_data
    broadcast_default = str(setup_data.get(CONF_BROADCAST) or DEFAULT_BROADCAST)
    hosts_default = str(setup_data.get(CONF_EXTRA_HOSTS) or "")
    previous = {d.serial for d in parse_devices(setup_data.get(CONF_DEVICES))}
    grouping_default = str(setup_data.get(CONF_GROUPING) or DEFAULT_GROUPING)
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(key="discover_intro", type=ConfigEntryType.LABEL),
                ConfigEntry(
                    key=CONF_BROADCAST,
                    type=ConfigEntryType.STRING,
                    required=False,
                    default_value=DEFAULT_BROADCAST,
                    value=broadcast_default,
                ),
                ConfigEntry(
                    key=CONF_EXTRA_HOSTS,
                    type=ConfigEntryType.STRING,
                    required=False,
                    value=hosts_default or None,
                ),
            ],
            step_id="user",
            errors=errors,
        )
        broadcast_default = str(values.get(CONF_BROADCAST) or DEFAULT_BROADCAST).strip()
        hosts_default = str(values.get(CONF_EXTRA_HOSTS) or "").strip()
        try:
            devices = await session.progress_until(
                discover_devices(
                    timeout=DISCOVERY_TIMEOUT_S,
                    broadcast=broadcast_default,
                    hosts=parse_hosts(hosts_default),
                ),
                step_id="discovering",
                text="discovering",
                expires_in=_DISCOVERY_STEP_TIMEOUT,
            )
        except StepExpiredError, TimeoutError:
            errors = {"base": "no_lights_found"}
            continue
        except OSError as err:
            errors = {"base": str(err)}
            continue
        if not devices:
            errors = {"base": "no_lights_found"}
            continue

        by_serial = {d.serial: d for d in devices}
        selected_default = [s for s in by_serial if s in previous] or list(by_serial)
        selection = await session.form(
            [
                ConfigEntry(
                    key=CONF_LIGHTS,
                    type=ConfigEntryType.STRING,
                    multi_value=True,
                    default_value=selected_default,
                    value=selected_default,
                    options=[
                        ConfigValueOption(title=_device_title(d), value=d.serial) for d in devices
                    ],
                ),
                ConfigEntry(
                    key=CONF_GROUPING,
                    type=ConfigEntryType.STRING,
                    default_value=DEFAULT_GROUPING,
                    value=grouping_default,
                    options=[ConfigValueOption(title=g, value=g) for g in GROUPINGS],
                ),
            ],
            step_id="select_lights",
            last_step=True,
            translation_params=[str(len(devices))],
        )
        chosen = selection.get(CONF_LIGHTS) or []
        serials = [str(s) for s in (chosen if isinstance(chosen, list) else [chosen])]
        picked = [by_serial[s] for s in serials if s in by_serial]
        if not picked:
            errors = {"base": "no_lights_selected"}
            continue
        grouping_default = str(selection.get(CONF_GROUPING) or DEFAULT_GROUPING)
        collected: dict[str, ConfigValueType] = {
            CONF_BROADCAST: broadcast_default,
            CONF_EXTRA_HOSTS: hosts_default,
            CONF_GROUPING: grouping_default,
            CONF_DEVICES: json.dumps([d.to_dict() for d in picked]),
        }
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


def parse_devices(raw: object) -> list[LifxDevice]:
    """Parse the devices stored as setup data (best effort)."""
    if not raw:
        return []
    try:
        return [LifxDevice.from_dict(item) for item in json.loads(str(raw))]
    except ValueError, KeyError, TypeError:
        return []


def parse_hosts(raw: object) -> list[str]:
    """Parse a comma/space separated list of IP addresses."""
    if not raw:
        return []
    return [h for h in str(raw).replace(",", " ").split() if h]


def _device_title(device: LifxDevice) -> str:
    """Return a display title for a light in the selection list."""
    parts = [device.label or device.serial]
    if device.group_label:
        parts.append(f"({device.group_label})")
    if device.is_multizone:
        parts.append(f"- {device.zone_count} zones")
    parts.append(f"- {device.host}")
    return " ".join(parts)
