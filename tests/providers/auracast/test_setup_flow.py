"""Tests for the Auracast setup flow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from serial.tools import list_ports

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.auracast import setup_flow
from music_assistant.providers.auracast.broadcaster import ControllerProbeResult

SUPPORTED = ControllerProbeResult((), "Bluetooth Core 5.2; company ID 0x0057")


def create_session(
    kind: str = "setup",
    *,
    setup_data: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
    forms: list[dict[str, Any] | SetupFlowError],
) -> MagicMock:
    """Build a setup session stub answering each form with the next submission."""
    session = MagicMock()
    session.context.kind = kind
    session.context.instance_id = "auracast" if kind == "reconfigure" else None
    session.context.setup_data = setup_data or {}
    session.context.values = values or {}
    session.form = AsyncMock(side_effect=forms)
    session.finish = AsyncMock()
    return session


def patch_ports(monkeypatch: pytest.MonkeyPatch, ports: dict[str, str]) -> None:
    """Replace serial port listing with the given device-to-label mapping."""
    monkeypatch.setattr(setup_flow, "list_serial_ports", lambda _current="": ports)


def test_list_serial_ports_only_offers_supported_controllers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Offer only allowlisted USB controllers and keep the configured port selectable."""
    monkeypatch.setattr(setup_flow, "SERIAL_BY_ID_DIR", tmp_path / "missing")
    monkeypatch.setattr(setup_flow, "SUPPORTED_USB_IDS", frozenset({(0x1915, 0x521F)}))
    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [
            MagicMock(device="/dev/ttyUSB1", description="Controller", vid=0x1915, pid=0x521F),
            MagicMock(device="/dev/ttyS0", description="n/a", vid=None, pid=None),
            MagicMock(device="/dev/ttyACM0", description="Zigbee stick", vid=0x10C4, pid=0xEA60),
            MagicMock(device="/dev/ttyACM1", description="Other Nordic", vid=0x1915, pid=0x0001),
        ],
    )

    assert setup_flow.list_serial_ports("/dev/missing") == {
        "/dev/ttyUSB1": "Controller (/dev/ttyUSB1)",
        "/dev/missing": "/dev/missing",
    }


def test_list_serial_ports_returns_stable_by_id_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Store the /dev/serial/by-id link instead of the re-plug dependent tty name."""
    real_device = tmp_path / "ttyACM0"
    real_device.touch()
    by_id_dir = tmp_path / "by-id"
    by_id_dir.mkdir()
    link = by_id_dir / "usb-Controller_1234-if00"
    link.symlink_to(real_device)
    monkeypatch.setattr(setup_flow, "SERIAL_BY_ID_DIR", by_id_dir)
    monkeypatch.setattr(setup_flow, "SUPPORTED_USB_IDS", frozenset({(0x1915, 0x521F)}))
    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [
            MagicMock(device=str(real_device), description="Controller", vid=0x1915, pid=0x521F)
        ],
    )

    assert setup_flow.list_serial_ports() == {str(link): f"Controller ({real_device})"}


@pytest.mark.asyncio
async def test_setup_without_serial_devices_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not show a form when there is no serial device to choose from."""
    patch_ports(monkeypatch, {})
    session = create_session(forms=[])

    with pytest.raises(SetupFlowError, match="No supported Auracast controller"):
        await setup_flow.run_setup(cast("Any", session))

    session.form.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_probes_only_the_selected_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open just the chosen port and persist the answers with the controller stack."""
    patch_ports(monkeypatch, {"/dev/controller": "Controller", "/dev/zigbee": "Zigbee stick"})
    probe = AsyncMock(return_value=SUPPORTED)
    monkeypatch.setattr(setup_flow, "probe_controller", probe)
    session = create_session(
        forms=[
            {
                "serial_port": "/dev/controller",
                "baudrate": 921_600,
                "broadcast_name": "Living room",
                "manufacturer_data": "",
            }
        ]
    )

    await setup_flow.run_setup(cast("Any", session))

    probe.assert_awaited_once_with("/dev/controller", 921_600)
    session.finish.assert_awaited_once_with(
        {
            "serial_port": "/dev/controller",
            "baudrate": 921_600,
            "broadcast_name": "Living room",
            "manufacturer_data": "",
            "_controller_stack": SUPPORTED.controller_stack,
        }
    )
    entries = {entry.key: entry for entry in session.form.await_args.args[0]}
    assert [option.value for option in entries["serial_port"].options] == [
        "/dev/controller",
        "/dev/zigbee",
    ]
    assert entries["baudrate"].validate is not None
    assert entries["manufacturer_data"].validate is not None


@pytest.mark.asyncio
async def test_setup_retries_with_another_baudrate_after_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show the failure on the form and let the user pick another baud rate."""
    patch_ports(monkeypatch, {"/dev/controller": "Controller"})
    probe = AsyncMock(side_effect=[TimeoutError("no response"), SUPPORTED])
    monkeypatch.setattr(setup_flow, "probe_controller", probe)
    answer = {"serial_port": "/dev/controller", "broadcast_name": "Test", "manufacturer_data": ""}
    session = create_session(
        forms=[{**answer, "baudrate": 115_200}, {**answer, "baudrate": 1_000_000}]
    )

    await setup_flow.run_setup(cast("Any", session))

    assert [call.args for call in probe.await_args_list] == [
        ("/dev/controller", 115_200),
        ("/dev/controller", 1_000_000),
    ]
    first_errors = session.form.await_args_list[0].kwargs["errors"]
    second_errors = session.form.await_args_list[1].kwargs["errors"]
    assert first_errors is None
    assert "no response" in str(second_errors["base"])
    assert session.finish.await_args.args[0]["baudrate"] == 1_000_000


@pytest.mark.asyncio
async def test_setup_rejects_controller_without_broadcast_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name the missing capabilities when the controller cannot broadcast."""
    patch_ports(monkeypatch, {"/dev/controller": "Controller"})
    unsupported = ControllerProbeResult(("Isochronous Broadcaster",), None)
    monkeypatch.setattr(setup_flow, "probe_controller", AsyncMock(return_value=unsupported))
    answer = {
        "serial_port": "/dev/controller",
        "baudrate": 1_000_000,
        "broadcast_name": "Test",
        "manufacturer_data": "",
    }
    session = create_session(forms=[answer, SetupFlowError("stop")])

    with pytest.raises(SetupFlowError, match="stop"):
        await setup_flow.run_setup(cast("Any", session))

    assert "Isochronous Broadcaster" in str(session.form.await_args.kwargs["errors"]["base"])
    session.finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_prefers_current_options_over_original_setup_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefill the form with values edited in settings, not the original setup answers."""
    patch_ports(monkeypatch, {"/dev/controller": "Controller"})
    monkeypatch.setattr(setup_flow, "probe_controller", AsyncMock(return_value=SUPPORTED))
    answer = {
        "serial_port": "/dev/controller",
        "baudrate": 1_000_000,
        "broadcast_name": "Edited",
        "manufacturer_data": "",
    }
    session = create_session(
        "reconfigure",
        setup_data={"serial_port": "/dev/controller", "broadcast_name": "Original"},
        values={"broadcast_name": "Edited"},
        forms=[answer],
    )

    await setup_flow.run_setup(cast("Any", session))

    entries = {entry.key: entry for entry in session.form.await_args.args[0]}
    assert entries["broadcast_name"].value == "Edited"


@pytest.mark.asyncio
async def test_reconfigure_skips_probe_when_controller_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not reopen the UART that the running broadcaster may hold."""
    patch_ports(monkeypatch, {"/dev/controller": "Controller"})
    probe = AsyncMock()
    monkeypatch.setattr(setup_flow, "probe_controller", probe)
    answer = {
        "serial_port": "/dev/controller",
        "baudrate": 1_000_000,
        "broadcast_name": "New name",
        "manufacturer_data": "",
    }
    session = create_session(
        "reconfigure",
        setup_data={**answer, "_controller_stack": "Known stack"},
        values={"broadcast_name": "Old name"},
        forms=[answer],
    )

    await setup_flow.run_setup(cast("Any", session))

    probe.assert_not_awaited()
    session.mass.get_provider.assert_not_called()
    session.mass.config.set_raw_provider_config_value.assert_any_call(
        "auracast", "broadcast_name", "New name"
    )
    assert session.finish.await_args.args[0]["_controller_stack"] == "Known stack"


@pytest.mark.asyncio
async def test_reconfigure_releases_running_controller_before_probing_same_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop the broadcaster holding the port before probing it at another baud rate."""
    patch_ports(monkeypatch, {"/dev/controller": "Controller"})
    events: list[str] = []

    async def stop() -> None:
        events.append("stop")

    async def probe_controller(*_args: Any) -> ControllerProbeResult:
        events.append("probe")
        return SUPPORTED

    player = MagicMock()
    player.stop = stop
    monkeypatch.setattr(setup_flow, "probe_controller", probe_controller)
    answer = {
        "serial_port": "/dev/controller",
        "baudrate": 921_600,
        "broadcast_name": "Test",
        "manufacturer_data": "",
    }
    session = create_session(
        "reconfigure",
        setup_data={"serial_port": "/dev/controller", "baudrate": 1_000_000},
        forms=[answer],
    )
    session.mass.get_provider.return_value.players = [player]

    await setup_flow.run_setup(cast("Any", session))

    assert events == ["stop", "probe"]


@pytest.mark.asyncio
async def test_reconfigure_restores_options_when_provider_fails_to_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll the written options back and show the error when applying them fails."""
    patch_ports(monkeypatch, {"/dev/controller": "Controller"})
    monkeypatch.setattr(setup_flow, "probe_controller", AsyncMock(return_value=SUPPORTED))
    answer = {
        "serial_port": "/dev/controller",
        "baudrate": 1_000_000,
        "broadcast_name": "New name",
        "manufacturer_data": "",
    }
    session = create_session(
        "reconfigure",
        setup_data={"serial_port": "/dev/old", "baudrate": 1_000_000},
        values={"serial_port": "/dev/old", "broadcast_name": "Old name"},
        forms=[answer, SetupFlowError("stop")],
    )
    session.finish = AsyncMock(side_effect=SetupFlowError("Provider failed to load"))

    with pytest.raises(SetupFlowError, match="stop"):
        await setup_flow.run_setup(cast("Any", session))

    session.mass.config.set_raw_provider_config_value.assert_any_call(
        "auracast", "broadcast_name", "Old name"
    )
    session.mass.config.set_raw_provider_config_value.assert_any_call(
        "auracast", "serial_port", "/dev/old"
    )
    assert "Provider failed to load" in str(session.form.await_args.kwargs["errors"]["base"])
