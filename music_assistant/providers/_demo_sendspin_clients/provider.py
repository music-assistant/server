"""Demo Sendspin Clients provider implementation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import SENDSPIN_SERVER_PORT, WILDCARD_BIND_IPS
from music_assistant.helpers.util import format_ip_for_url
from music_assistant.models.plugin import PluginProvider

from .constants import (
    ACTION_PRESS_BUTTON,
    ACTION_REFRESH,
    ACTION_RESET,
    ACTION_SEPARATOR,
    CONF_SCENARIOS,
)
from .device import FakeSendspinDevice
from .scenarios import SCENARIOS, SCENARIOS_BY_ID

SENDSPIN_DOMAIN = "sendspin"

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .scenarios import Scenario


class DemoSendspinClientsProvider(PluginProvider):
    """
    Runs fake Sendspin clients, one per pairing scenario.

    Each device connects to this server's own Sendspin endpoint, so the Sendspin provider
    picks it up as an ordinary client and renders the real approval, pairing and device
    management screens against it.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the provider with no devices running yet."""
        super().__init__(mass, manifest, config, supported_features)
        self._devices: dict[str, FakeSendspinDevice] = {}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the scenario picker, plus a status and controls per running device."""
        entries: list[ConfigEntry] = [
            ConfigEntry(
                key=CONF_SCENARIOS,
                type=ConfigEntryType.STRING,
                label="Scenarios to run",
                description=(
                    "Each selected scenario connects one fake Sendspin device. "
                    "Reload the provider after changing this."
                ),
                multi_value=True,
                options=[
                    ConfigValueOption(value=scenario.scenario_id, title=scenario.name)
                    for scenario in SCENARIOS
                ],
                default_value=[scenario.scenario_id for scenario in SCENARIOS],
            ),
        ]
        for scenario in self._selected_scenarios():
            entries.extend(self._device_entries(scenario))
        return tuple(entries)

    async def handle_config_action(self, action: str) -> tuple[ConfigEntry, ...]:
        """Run a device control and re-render the page with the resulting state."""
        scenario_id, _, name = action.partition(ACTION_SEPARATOR)
        device = self._devices.get(scenario_id)
        if device is not None:
            if name == ACTION_PRESS_BUTTON:
                device.press_pairing_button()
            elif name == ACTION_RESET:
                await self._forget_on_server(device.client_id)
                await device.reset()
        # ACTION_REFRESH needs no side effect: re-rendering is the whole point.
        return await self.get_config_entries()

    async def loaded_in_mass(self) -> None:
        """Start every selected fake device."""
        await super().loaded_in_mass()
        storage_dir = Path(self.mass.storage_path) / self.domain
        await asyncio.to_thread(storage_dir.mkdir, parents=True, exist_ok=True)
        server_url = self._server_url()
        for scenario in self._selected_scenarios():
            device = FakeSendspinDevice(scenario, storage_dir, server_url)
            self._devices[scenario.scenario_id] = device
            await device.start()

    async def unload(self, is_removed: bool = False) -> None:
        """Disconnect every fake device."""
        devices = list(self._devices.values())
        self._devices.clear()
        await asyncio.gather(*(device.stop() for device in devices), return_exceptions=True)

    async def _forget_on_server(self, client_id: str) -> None:
        """Drop this server's own pairing and unpaired-access records for a device."""
        provider = cast("SendspinProvider | None", self.mass.get_provider(SENDSPIN_DOMAIN))
        if provider is None:
            return
        store = provider.server_api.pairing_store
        try:
            if await store.record_by_client_id(client_id) is not None:
                await provider.unpair_client(client_id)
            if await store.trusted_unpaired(client_id) is not None:
                await provider.set_trusted_unpaired(client_id, enabled=False)
        except Exception as err:
            self.logger.warning("Could not forget %s on the server: %s", client_id, err)

    def _selected_scenarios(self) -> list[Scenario]:
        """Return the scenarios the user picked, in their declared order."""
        selected = cast(
            "list[str]",
            self.config.get_value(CONF_SCENARIOS, [s.scenario_id for s in SCENARIOS]),
        )
        return [
            SCENARIOS_BY_ID[scenario_id]
            for scenario_id in selected
            if scenario_id in SCENARIOS_BY_ID
        ]

    def _server_url(self) -> str:
        """Return the WebSocket URL of this server's own Sendspin endpoint."""
        bind_ip = self.mass.streams.bind_ip
        host = "127.0.0.1" if not bind_ip or bind_ip in WILDCARD_BIND_IPS else bind_ip
        return f"ws://{format_ip_for_url(host)}:{SENDSPIN_SERVER_PORT}/sendspin"

    def _device_entries(self, scenario: Scenario) -> list[ConfigEntry]:
        """Return the status label and controls for one fake device."""
        device = self._devices.get(scenario.scenario_id)
        entries: list[ConfigEntry] = [
            ConfigEntry(
                key=f"{scenario.scenario_id}{ACTION_SEPARATOR}divider",
                type=ConfigEntryType.DIVIDER,
                label=scenario.name,
            ),
            ConfigEntry(
                key=f"{scenario.scenario_id}{ACTION_SEPARATOR}status",
                type=ConfigEntryType.LABEL,
                label=_status_text(device),
                description=scenario.description,
            ),
        ]
        if device is None:
            return entries
        if scenario.gesture_gated:
            entries.append(
                ConfigEntry(
                    key=f"{scenario.scenario_id}{ACTION_SEPARATOR}{ACTION_PRESS_BUTTON}",
                    type=ConfigEntryType.ACTION,
                    action=f"{scenario.scenario_id}{ACTION_SEPARATOR}{ACTION_PRESS_BUTTON}",
                    label="Press the pairing button on this device",
                    action_label="Press",
                )
            )
        entries.append(
            ConfigEntry(
                key=f"{scenario.scenario_id}{ACTION_SEPARATOR}{ACTION_REFRESH}",
                type=ConfigEntryType.ACTION,
                action=f"{scenario.scenario_id}{ACTION_SEPARATOR}{ACTION_REFRESH}",
                label="Refresh this device's status",
                action_label="Refresh",
            )
        )
        entries.append(
            ConfigEntry(
                key=f"{scenario.scenario_id}{ACTION_SEPARATOR}{ACTION_RESET}",
                type=ConfigEntryType.ACTION,
                action=f"{scenario.scenario_id}{ACTION_SEPARATOR}{ACTION_RESET}",
                label="Make this device forget the server and reconnect",
                action_label="Reset",
                advanced=True,
            )
        )
        return entries


def _status_text(device: FakeSendspinDevice | None) -> str:
    """Compose the one-line status shown for a device: its secrets and what it waits for."""
    if device is None:
        return "Not running."
    parts = ["Connected." if device.connected else "Not connected."]
    if device.awaiting_button:
        parts.append("Waiting for its pairing button to be pressed.")
    if device.dynamic_pin is not None:
        parts.append(f"PIN on the device right now: {device.dynamic_pin}")
    if device.static_pin is not None:
        parts.append(f"Static PIN: {device.static_pin}")
    if device.pairing_token is not None:
        parts.append(f"Pairing token: {device.pairing_token}")
    if device.last_abort is not None:
        parts.append(f"Last pairing abort: {device.last_abort.value}")
    return " ".join(parts)
