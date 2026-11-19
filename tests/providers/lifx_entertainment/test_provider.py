"""
Integration tests of the provider against a real Music Assistant server package.

The Music Assistant core (config storage, the player registry) is mocked, but the setup
flow engine, the Sendspin server, its bridge roles and the visualizer feature extraction
are the real ones; the lights are fake LIFX devices on localhost.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server import SendspinServer
from aiosendspin.server.roles.base import AudioChunk
from lifx_entertainment import LifxDevice, ZoneSupport, discover_devices
from lifx_entertainment.constants import MessageType
from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
from music_assistant_models.enums import FlowStepType, PlayerType, ProviderType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_LOG_LEVEL, DEFAULT_PROVIDER_CONFIG_ENTRIES
from music_assistant.models.provider import Provider
from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.lifx_entertainment import bridge as bridge_module
from music_assistant.providers.lifx_entertainment import provider as provider_module
from music_assistant.providers.lifx_entertainment import setup_flow
from music_assistant.providers.lifx_entertainment.constants import (
    CONF_BRIGHTNESS,
    CONF_BROADCAST,
    CONF_COLOR_MODE,
    CONF_DEVICES,
    CONF_GROUPING,
    CONF_LIGHT_LATENCY_MS,
    CONF_LIGHTS,
    CONF_TRANSITION_MS,
)
from music_assistant.providers.lifx_entertainment.provider import LifxEntertainmentProvider
from tests.providers.lifx_entertainment.fakes import FakeLifxDevice

_PROVIDER_DIR = Path(provider_module.__file__).parent
_INSTANCE_ID = "lifx_entertainment--test"
_SAMPLE_RATE = 48_000
_CHUNK_US = 25_000


class _ConfigStore:
    """The slice of Music Assistant's config controller the provider uses."""

    def __init__(self, setup_data: dict[str, Any]) -> None:
        self.data: dict[str, Any] = {"providers": {_INSTANCE_ID: {"setup_data": dict(setup_data)}}}

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("/"):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any, **_kwargs: Any) -> None:
        *parents, leaf = path.split("/")
        node = self.data
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value

    def encrypt_string(self, value: str) -> str:
        return value

    def decrypt_string(self, value: str) -> str:
        return value


async def _make_config(setup_data: dict[str, Any] | None = None) -> ProviderConfig:
    """Build a ProviderConfig holding the provider's own entries plus the log level."""
    provider = LifxEntertainmentProvider.__new__(LifxEntertainmentProvider)
    entries = [*await provider.get_config_entries(), *deepcopy(DEFAULT_PROVIDER_CONFIG_ENTRIES)]
    values = {entry.key: entry for entry in entries}
    for entry in values.values():
        entry.value = entry.default_value
    return ProviderConfig(
        type=ProviderType.PLUGIN,
        domain="lifx_entertainment",
        instance_id=_INSTANCE_ID,
        values=values,
        setup_data=dict(setup_data or {}),
    )


@pytest.fixture
async def sendspin_server() -> AsyncIterator[SendspinServer]:
    """Real Sendspin server, without network listeners."""
    server = SendspinServer(
        asyncio.get_running_loop(),
        Identity.generate(),
        "test",
        pairing_store=InMemoryServerPairingStore(),
    )
    yield server
    await server.close()


@pytest.fixture
async def bulb() -> AsyncIterator[FakeLifxDevice]:
    """Fake single-zone bulb, initially off."""
    device = FakeLifxDevice("d073d5aaaaaa", "Bulb", power=0)
    await device.start()
    yield device
    device.close()


@pytest.fixture
async def strip() -> AsyncIterator[FakeLifxDevice]:
    """Fake 16-zone multizone strip."""
    device = FakeLifxDevice("d073d5bbbbbb", "Strip", zones=16)
    await device.start()
    yield device
    device.close()


def _make_mass(sendspin_server: SendspinServer, setup_data: dict[str, Any]) -> MagicMock:
    mass = MagicMock()
    mass.loop = asyncio.get_running_loop()
    mass.create_task = lambda coro, *_args, **_kwargs: asyncio.ensure_future(coro)
    mass.config = _ConfigStore(setup_data)
    sendspin = MagicMock()
    sendspin.server_api = sendspin_server
    mass.get_provider.side_effect = lambda domain: sendspin if domain == "sendspin" else None
    return mass


def _manifest() -> ProviderManifest:
    return ProviderManifest.from_dict(
        json.loads((_PROVIDER_DIR / "manifest.json").read_text(encoding="utf-8"))
    )


def _stored(fake: FakeLifxDevice, **kwargs: Any) -> dict[str, Any]:
    device = LifxDevice(
        fake.serial,
        "127.0.0.1",
        port=fake.port,
        label=fake.label,
        group_id=fake.group_id.hex(),
        group_label=fake.group,
        **kwargs,
    )
    return device.to_dict()


def _music(start_us: int, seconds: float) -> list[AudioChunk]:
    """Stereo 16-bit PCM: a kick drum twice a second over a quiet chord."""
    samples = int(seconds * _SAMPLE_RATE)
    t = np.arange(samples) / _SAMPLE_RATE
    pad = 0.05 * (np.sin(2 * np.pi * 220 * t) + np.sin(2 * np.pi * 330 * t))
    since_kick = t % 0.5
    kick = 0.8 * np.sin(2 * np.pi * 60 * t) * np.exp(-since_kick * 25)
    pcm = (np.clip(pad + kick, -1, 1) * 32767).astype("<i2")
    frames = np.repeat(pcm[:, None], 2, axis=1).tobytes()
    per_chunk = _SAMPLE_RATE * _CHUNK_US // 1_000_000 * 4
    return [
        AudioChunk(
            data=frames[offset : offset + per_chunk],
            timestamp_us=start_us + index * _CHUNK_US,
            duration_us=_CHUNK_US,
            byte_count=per_chunk,
        )
        for index, offset in enumerate(range(0, len(frames), per_chunk))
    ]


async def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    """Poll state owned by the provider/fake devices until it matches."""
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError


async def test_provider_streams_music_to_the_lights(
    monkeypatch: pytest.MonkeyPatch,
    sendspin_server: SendspinServer,
    bulb: FakeLifxDevice,
    strip: FakeLifxDevice,
) -> None:
    """Load the provider, play audio through the Sendspin roles and check the lights."""
    monkeypatch.setattr(bridge_module, "_STOP_DEBOUNCE_S", 0.1)
    monkeypatch.setattr(provider_module, "DISCOVERY_TIMEOUT_S", 0.3)
    setup_data = {
        # discovery stays on localhost: nothing answers there, the stored addresses are used
        CONF_BROADCAST: "127.0.0.1",
        CONF_GROUPING: "group",
        CONF_DEVICES: json.dumps(
            [
                _stored(bulb),
                _stored(strip, zone_support=ZoneSupport.EXTENDED, zone_count=16),
            ]
        ),
    }
    mass = _make_mass(sendspin_server, setup_data)
    config = await _make_config(setup_data)
    provider = LifxEntertainmentProvider(mass, _manifest(), config, set())
    original_bulb, original_zones = bulb.color, list(strip.zones)

    await provider.handle_async_init()
    try:
        # one light player for the "Living room" group, registered as a LIGHT player
        clients = [c for c in sendspin_server.clients if c.client_id.startswith("lifx-")]
        assert len(clients) == 1
        client = clients[0]
        assert client.name == "LIFX: Living room"
        sendspin = mass.get_provider("sendspin")
        sendspin.register_bridge_player_type.assert_called_once_with(
            client.client_id, PlayerType.LIGHT
        )

        viz_role = client.roles_by_family("visualizer")[0]
        viz_role.on_stream_start()
        await _wait_for(lambda: bulb.power == 65535)

        now = sendspin_server.clock.now_us()
        for chunk in _music(now, 3.0):
            viz_role.on_audio_chunk(chunk)
        await asyncio.sleep(1.5)

        frames = [h for h, _ in bulb.of_type(MessageType.LIGHT_SET_COLOR) if not h.ack_required]
        zone_frames = strip.of_type(MessageType.SET_EXTENDED_COLOR_ZONES)
        assert len(frames) >= 5
        assert len(zone_frames) >= 5
        # the music drives the lights: they are not stuck on one colour
        assert len({(c.hue, c.brightness) for c in _bulb_colors(bulb)}) > 2
        assert len({z.brightness for z in strip.zones}) >= 1

        viz_role.on_stream_end()
        await _wait_for(lambda: bulb.power == 0)
        await _wait_for(lambda: strip.zones == original_zones)
        assert bulb.color == original_bulb
    finally:
        await provider.unload()
    assert not [c for c in sendspin_server.clients if c.client_id.startswith("lifx-")]


def _bulb_colors(bulb: FakeLifxDevice) -> list[Any]:
    from lifx_entertainment import protocol  # noqa: PLC0415

    return [
        protocol.decode_hsbk(payload, 1)
        for header, payload in bulb.of_type(MessageType.LIGHT_SET_COLOR)
        if not header.ack_required
    ]


async def test_setup_flow_discovers_and_stores_lights(
    monkeypatch: pytest.MonkeyPatch, strip: FakeLifxDevice
) -> None:
    """Drive the setup flow through Music Assistant's real SetupSession."""
    monkeypatch.setattr(setup_flow, "discover_devices", partial(discover_devices, port=strip.port))
    finish = AsyncMock(return_value={"instance_id": _INSTANCE_ID})
    session = SetupSession(
        MagicMock(),
        "flow-1",
        SetupFlowContext(kind="setup", reason="user", domain="lifx_entertainment"),
        finish,
    )
    flow = asyncio.create_task(setup_flow.run_setup(session))
    try:
        await _wait_for(lambda: _form(session, "user"))
        assert session.handle_submit({CONF_BROADCAST: "127.0.0.1"}) is None

        await _wait_for(lambda: _form(session, "select_lights"))
        step = session.current_step
        assert step is not None
        assert step.translation_params == ["1"]
        assert step.translation_owner == "provider.lifx_entertainment"
        lights = next(e for e in step.entries if e.key == CONF_LIGHTS)
        assert [o.value for o in lights.options] == [strip.serial]
        assert lights.options is not None
        assert "16 zones" in str(lights.options[0].title)
        assert session.handle_submit({CONF_LIGHTS: [strip.serial], CONF_GROUPING: "light"}) is None

        await asyncio.wait_for(flow, 5)
    finally:
        flow.cancel()
    assert finish.await_args is not None
    values = finish.await_args.args[1]
    assert values[CONF_GROUPING] == "light"
    assert values[CONF_BROADCAST] == "127.0.0.1"
    devices = setup_flow.parse_devices(values[CONF_DEVICES])
    assert [(d.serial, d.port, d.zone_count) for d in devices] == [(strip.serial, strip.port, 16)]


async def test_setup_flow_reports_no_lights(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty discovery loops back to the first form with an error."""
    monkeypatch.setattr(setup_flow, "discover_devices", AsyncMock(return_value=[]))
    session = SetupSession(
        MagicMock(),
        "flow-2",
        SetupFlowContext(kind="setup", reason="user", domain="lifx_entertainment"),
        AsyncMock(),
    )
    flow = asyncio.create_task(setup_flow.run_setup(session))
    try:
        await _wait_for(lambda: _form(session, "user"))
        session.handle_submit({})
        await _wait_for(
            lambda: _form(session, "user") and bool(getattr(session.current_step, "errors", None))
        )
        assert session.current_step is not None
        assert session.current_step.errors == {"base": "no_lights_found"}
    finally:
        flow.cancel()


def _form(session: SetupSession, step_id: str) -> bool:
    step = session.current_step
    return (
        step is not None
        and step.type == FlowStepType.FORM
        and step.step_id == step_id
        and session._input_future is not None
        and not session._input_future.done()
    )


def _make_provider(config: ProviderConfig) -> tuple[LifxEntertainmentProvider, MagicMock]:
    """Build a provider with a mocked bridge manager, skipping the framework init."""
    provider = LifxEntertainmentProvider.__new__(LifxEntertainmentProvider)
    provider.mass = MagicMock()
    provider.config = config
    provider.logger = logging.getLogger("test")
    bridge_manager = MagicMock()
    provider._bridge_manager = bridge_manager
    return provider, bridge_manager


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({CONF_BRIGHTNESS: 50}, {"brightness": 50}),
        ({CONF_COLOR_MODE: "ambient"}, {"color_mode": "ambient"}),
        ({CONF_LIGHT_LATENCY_MS: 250}, {"light_latency_ms": 250}),
        ({CONF_TRANSITION_MS: 0}, {"transition_ms": 0}),
    ],
    ids=["brightness", "color_mode", "latency", "transition"],
)
async def test_live_settings_skip_the_reload(
    update: dict[str, ConfigValueType], expected: dict[str, ConfigValueType]
) -> None:
    """Live settings are applied to the bridges without reloading the provider."""
    config = await _make_config()
    provider, bridge_manager = _make_provider(config)
    changed_keys = config.update(update)

    with patch.object(Provider, "update_config", new_callable=AsyncMock) as base_update:
        await provider.update_config(config, changed_keys)

    kwargs = bridge_manager.update_settings.call_args.kwargs
    assert {key: kwargs[key] for key in expected} == expected
    base_update.assert_not_awaited()


async def test_other_change_reloads() -> None:
    """A change that cannot be applied live goes to the base implementation."""
    config = await _make_config()
    provider, bridge_manager = _make_provider(config)
    changed_keys = config.update({CONF_BRIGHTNESS: 50, CONF_LOG_LEVEL: "DEBUG"})

    with patch.object(Provider, "update_config", new_callable=AsyncMock) as base_update:
        await provider.update_config(config, changed_keys)

    bridge_manager.update_settings.assert_not_called()
    base_update.assert_awaited_once_with(config, changed_keys)


class _FakeSession:
    """Records the order of session starts and restorations; restoring takes a while."""

    events: list[str]
    restore_started: asyncio.Event

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        """Give the session the next number."""
        self.index = sum(1 for e in self.events if e.endswith(":created"))
        self.events.append(f"{self.index}:created")
        self.is_streaming = False
        self.transition_ms = 0

    async def start(self) -> None:
        """Snapshot the lights and start streaming."""
        self.events.append(f"{self.index}:start")
        self.is_streaming = True

    async def aclose(self) -> None:
        """Restore the lights, slowly."""
        if not self.is_streaming:
            return
        self.events.append(f"{self.index}:restore")
        self.restore_started.set()
        await asyncio.sleep(0.2)
        self.is_streaming = False
        self.events.append(f"{self.index}:restored")

    def send(self, _commands: list[Any]) -> None:
        """Ignore frames."""


async def test_restart_during_restoration_waits_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A stream starting during the restoration waits for it to finish.

    It neither cuts the restoration short nor snapshots half-restored lights.
    """
    from lifx_entertainment import EntertainmentArea, LightChannel  # noqa: PLC0415

    monkeypatch.setattr(bridge_module, "_STOP_DEBOUNCE_S", 0.01)
    monkeypatch.setattr(_FakeSession, "events", [], raising=False)
    monkeypatch.setattr(_FakeSession, "restore_started", asyncio.Event(), raising=False)
    monkeypatch.setattr(bridge_module, "EntertainmentSession", _FakeSession)

    provider = MagicMock()
    provider.mass.loop = asyncio.get_running_loop()
    provider.mass.create_task = lambda coro, *_args, **_kwargs: asyncio.ensure_future(coro)
    provider.instance_id = _INSTANCE_ID
    provider.config = await _make_config()
    provider.logger = logging.getLogger("test")
    area = EntertainmentArea(
        "area", "Room", channels=[LightChannel(channel_id=0, service_id="s", name="Bulb")]
    )
    bridge = bridge_module.LifxEntertainmentBridge(provider, MagicMock(), area)

    bridge._on_stream_start()
    await _wait_for(lambda: bridge._is_streaming)
    bridge._on_stream_end()
    await asyncio.wait_for(_FakeSession.restore_started.wait(), 5)
    # the next track starts while the first session is still restoring the lights
    bridge._on_stream_start()
    await _wait_for(lambda: bridge._is_streaming)
    await bridge.stop()

    assert _FakeSession.events[:5] == [
        "0:created",
        "0:start",
        "0:restore",
        "0:restored",
        "1:created",
    ]
    assert _FakeSession.events[5] == "1:start"


class _FailingTransport:
    """A LAN transport whose socket can not be opened."""

    instances: ClassVar[list[_FailingTransport]] = []

    def __init__(self) -> None:
        """Track the instance."""
        self.closed = False
        self.instances.append(self)

    async def open(self) -> None:
        """Fail like a port already in use."""
        raise OSError("address in use")

    def close(self) -> None:
        """Record the close."""
        self.closed = True


async def test_setup_fails_when_the_lan_socket_can_not_open(
    monkeypatch: pytest.MonkeyPatch, sendspin_server: SendspinServer, bulb: FakeLifxDevice
) -> None:
    """A LAN failure is reported through setup, and the partial transport is closed."""
    monkeypatch.setattr(provider_module, "LifxTransport", _FailingTransport)
    setup_data = {CONF_DEVICES: json.dumps([_stored(bulb)])}
    config = await _make_config(setup_data)
    provider = LifxEntertainmentProvider(
        _make_mass(sendspin_server, setup_data), _manifest(), config, set()
    )

    with pytest.raises(SetupFailedError):
        await provider.handle_async_init()
    assert _FailingTransport.instances[-1].closed
    assert provider._transport is None


async def test_setup_fails_without_lights(sendspin_server: SendspinServer) -> None:
    """A provider with no lights stored fails its setup instead of loading inactive."""
    config = await _make_config({})
    provider = LifxEntertainmentProvider(
        _make_mass(sendspin_server, {}), _manifest(), config, set()
    )

    with pytest.raises(SetupFailedError):
        await provider.handle_async_init()
