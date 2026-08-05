"""Tests for event-driven renderer lifecycle management."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from music_assistant_models.enums import EventType, IdentifierType
from music_assistant_models.event import MassEvent

from music_assistant.providers.dlna_receiver.lifecycle import (
    RendererCallbacks,
    RendererRegistry,
    deterministic_udn,
)


class _FakeRenderer:
    """Network-free renderer with observable lifecycle state."""

    actions: ClassVar[list[tuple[str, str]]] = []
    start_gate: ClassVar[asyncio.Event | None] = None
    fail_start_udn: ClassVar[str | None] = None
    fail_stop_udns: ClassVar[set[str]] = set()

    def __init__(
        self,
        friendly_name: str,
        bind_ip: str,
        http_port: int,
        udn: str,
        session: object,
    ) -> None:
        del bind_ip, session
        self.friendly_name = friendly_name
        self.http_port = http_port
        self.udn = udn
        self.description_url = f"http://192.0.2.10:{http_port}/description.xml"

    async def start(self) -> None:
        """Record startup."""
        self.actions.append(("renderer-start", self.udn))
        if self.udn == self.fail_start_udn:
            raise RuntimeError("renderer start failed")
        if self.start_gate is not None:
            await self.start_gate.wait()

    async def stop(self) -> None:
        """Record shutdown."""
        self.actions.append(("renderer-stop", self.udn))
        if self.udn in self.fail_stop_udns:
            raise RuntimeError("renderer stop failed")


class _FakeSSDP:
    """Network-free SSDP advertiser with observable lifecycle state."""

    actions = _FakeRenderer.actions
    fail_start_udn: ClassVar[str | None] = None
    fail_stop_udns: ClassVar[set[str]] = set()

    def __init__(self, udn: str, description_url: str, bind_ip: str) -> None:
        del bind_ip
        self.udn = udn
        self.description_url = description_url

    async def start(self) -> None:
        """Record startup."""
        self.actions.append(("ssdp-start", self.udn))
        if self.udn == self.fail_start_udn:
            raise RuntimeError("SSDP start failed")

    async def stop(self) -> None:
        """Record byebye and shutdown."""
        self.actions.append(("ssdp-stop", self.udn))
        if self.udn in self.fail_stop_udns:
            raise RuntimeError("SSDP stop failed")


class _Players:
    """Mutable player controller used by lifecycle tests."""

    def __init__(self, players: list[object]) -> None:
        self.items = {cast("Any", player).player_id: player for player in players}
        self.on_scan: Any = None

    def all_players(self, **_kwargs: object) -> list[object]:
        if self.on_scan:
            self.on_scan()
        return list(self.items.values())

    def get_player(self, player_id: str) -> object | None:
        return self.items.get(player_id)


class _Mass:
    """Minimal event bus and task tracker used by RendererRegistry."""

    def __init__(self, players: list[object]) -> None:
        self.players = _Players(players)
        self.http_session = object()
        self.subscription: tuple[object, tuple[EventType, ...]] | None = None
        self.tasks: list[asyncio.Task[None]] = []
        self.tasks_by_id: dict[str, asyncio.Task[None]] = {}

    def subscribe(
        self,
        callback: object,
        event_filter: tuple[EventType, ...],
    ) -> object:
        self.subscription = (callback, event_filter)

        def _unsubscribe() -> None:
            self.subscription = None

        return _unsubscribe

    def create_task(
        self,
        target: Any,
        *args: object,
        task_id: str | None = None,
        abort_existing: bool = False,
        **_kwargs: object,
    ) -> asyncio.Task[None]:
        if task_id and abort_existing and (existing := self.tasks_by_id.get(task_id)):
            existing.cancel()
        coroutine = target(*args) if callable(target) else target
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        if task_id:
            self.tasks_by_id[task_id] = task
        return task

    async def emit(self, event: EventType, player_id: str) -> None:
        assert self.subscription is not None
        callback = cast("Any", self.subscription[0])
        callback(MassEvent(event=event, object_id=player_id))
        await asyncio.gather(*self.tasks)


def _player(player_id: str, name: str, uuid_value: str) -> object:
    """Build a complete player shape consumed by RendererRegistry."""
    return SimpleNamespace(
        player_id=player_id,
        display_name=name,
        name=name,
        device_info=SimpleNamespace(identifiers={IdentifierType.UUID: uuid_value}),
    )


async def _noop(*_args: object) -> None:
    """Do nothing for callbacks not under test."""


async def _play(*_args: object) -> bool:
    """Accept Play for callbacks not under test."""
    return True


def _callbacks() -> RendererCallbacks:
    """Return callback bindings accepted by fake renderers."""
    return RendererCallbacks(
        on_set_av_transport_uri=cast("Any", _noop),
        on_play=cast("Any", _play),
        on_pause=cast("Any", _noop),
        on_stop=cast("Any", _noop),
        on_get_position=lambda _instance: (0, 0),
        on_set_volume=cast("Any", _noop),
        on_set_mute=cast("Any", _noop),
        on_instance_removed=lambda _source_id, _instance: None,
    )


@pytest.fixture(autouse=True)
def _network_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace network services while retaining registry behavior."""
    from music_assistant.providers.dlna_receiver import lifecycle  # noqa: PLC0415

    _FakeRenderer.actions.clear()
    _FakeRenderer.start_gate = None
    _FakeRenderer.fail_start_udn = None
    _FakeRenderer.fail_stop_udns.clear()
    _FakeSSDP.fail_start_udn = None
    _FakeSSDP.fail_stop_udns.clear()
    monkeypatch.setattr(lifecycle, "UPnPRenderer", _FakeRenderer)
    monkeypatch.setattr(lifecycle, "SSDPAdvertiser", _FakeSSDP)


async def test_start_subscribes_before_initial_player_scan() -> None:
    """No player event can be missed between subscription and initial scan."""
    mass = _Mass([_player("kitchen", "Kitchen", "device-kitchen")])
    mass.players.on_scan = lambda: (
        mass.subscription is not None or pytest.fail("initial scan ran before event subscription")
    )
    registry = RendererRegistry(
        mass=cast("Any", mass),
        target_spec="*",
        friendly_prefix="Music Assistant",
        bind_ip="192.0.2.10",
        base_port=8298,
        callbacks=_callbacks(),
    )

    await registry.start()

    assert set(registry.instances) == {"kitchen"}
    assert mass.subscription is not None
    assert mass.subscription[1] == (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED)
    await registry.stop()


async def test_start_rolls_back_started_instances_and_subscription_on_failure() -> None:
    """A failed initial renderer cannot leave earlier renderers or subscriptions alive."""
    mass = _Mass(
        [
            _player("kitchen", "Kitchen", "device-kitchen"),
            _player("bedroom", "Bedroom", "device-bedroom"),
        ]
    )
    registry = RendererRegistry(
        mass=cast("Any", mass),
        target_spec="*",
        friendly_prefix="Music Assistant",
        bind_ip="192.0.2.10",
        base_port=8298,
        callbacks=_callbacks(),
    )
    kitchen_udn = deterministic_udn("kitchen")
    _FakeSSDP.fail_start_udn = deterministic_udn("bedroom")

    with pytest.raises(RuntimeError, match="SSDP start failed"):
        await registry.start()

    assert registry.instances == {}
    assert mass.subscription is None
    assert ("ssdp-stop", kitchen_udn) in _FakeRenderer.actions
    assert ("renderer-stop", kitchen_udn) in _FakeRenderer.actions


async def test_renderer_start_failure_stops_partial_renderer() -> None:
    """A renderer start failure cannot leave its partially initialized HTTP server alive."""
    mass = _Mass([_player("kitchen", "Kitchen", "device-kitchen")])
    registry = RendererRegistry(
        mass=cast("Any", mass),
        target_spec="*",
        friendly_prefix="Music Assistant",
        bind_ip="192.0.2.10",
        base_port=8298,
        callbacks=_callbacks(),
    )
    kitchen_udn = deterministic_udn("kitchen")
    _FakeRenderer.fail_start_udn = kitchen_udn

    with pytest.raises(RuntimeError, match="renderer start failed"):
        await registry.start()

    assert ("renderer-stop", kitchen_udn) in _FakeRenderer.actions
    assert registry.instances == {}
    assert mass.subscription is None


async def test_player_events_remove_immediately_and_reuse_port_on_add() -> None:
    """Remove sends byebye before HTTP shutdown and re-add keeps the port."""
    player = _player("kitchen", "Kitchen", "device-kitchen")
    mass = _Mass([player])
    registry = RendererRegistry(
        mass=cast("Any", mass),
        target_spec="*",
        friendly_prefix="Music Assistant",
        bind_ip="192.0.2.10",
        base_port=8298,
        callbacks=_callbacks(),
    )
    await registry.start()
    first_port = registry.instances["kitchen"].renderer.http_port

    del mass.players.items["kitchen"]
    await mass.emit(EventType.PLAYER_REMOVED, "kitchen")

    assert registry.instances == {}
    udn = deterministic_udn("kitchen")
    assert _FakeRenderer.actions[-2:] == [("ssdp-stop", udn), ("renderer-stop", udn)]

    mass.players.items["kitchen"] = player
    await mass.emit(EventType.PLAYER_ADDED, "kitchen")

    assert registry.instances["kitchen"].renderer.http_port == first_port
    await registry.stop()


async def test_stop_attempts_every_cleanup_and_reports_all_failures() -> None:
    """One teardown failure cannot skip another resource, callback, or instance."""
    mass = _Mass(
        [
            _player("kitchen", "Kitchen", "device-kitchen"),
            _player("bedroom", "Bedroom", "device-bedroom"),
        ]
    )
    removed: list[str] = []
    callbacks = replace(
        _callbacks(),
        on_instance_removed=lambda source_id, _instance: removed.append(source_id),
    )
    registry = RendererRegistry(
        mass=cast("Any", mass),
        target_spec="*",
        friendly_prefix="Music Assistant",
        bind_ip="192.0.2.10",
        base_port=8298,
        callbacks=callbacks,
    )
    await registry.start()
    kitchen_udn = deterministic_udn("kitchen")
    bedroom_udn = deterministic_udn("bedroom")
    _FakeSSDP.fail_stop_udns.add(kitchen_udn)
    _FakeRenderer.fail_stop_udns.add(bedroom_udn)

    with pytest.raises(ExceptionGroup) as exc_info:
        await registry.stop()

    assert registry.instances == {}
    assert mass.subscription is None
    assert removed == ["kitchen", "bedroom"]
    for udn in (kitchen_udn, bedroom_udn):
        assert ("ssdp-stop", udn) in _FakeRenderer.actions
        assert ("renderer-stop", udn) in _FakeRenderer.actions
    assert len(exc_info.value.exceptions) == 2


async def test_all_players_filters_renderer_by_uuid_identifier() -> None:
    """A universal player's arbitrary ID cannot hide its renderer UUID."""
    native = _player("kitchen", "Kitchen", "physical-device")
    own_renderer = _player(
        "up-arbitrary-controller-id",
        "Music Assistant — Kitchen",
        deterministic_udn("kitchen").removeprefix("uuid:").upper(),
    )
    mass = _Mass([native, own_renderer])
    registry = RendererRegistry(
        mass=cast("Any", mass),
        target_spec="*",
        friendly_prefix="Music Assistant",
        bind_ip="192.0.2.10",
        base_port=8298,
        callbacks=_callbacks(),
    )

    await registry.start()

    assert set(registry.instances) == {"kitchen"}
    await registry.stop()


async def test_stop_unsubscribes_and_ignores_late_events() -> None:
    """Events arriving after shutdown cannot create new renderers."""
    mass = _Mass([])
    registry = RendererRegistry(
        mass=cast("Any", mass),
        target_spec="*",
        friendly_prefix="Music Assistant",
        bind_ip="192.0.2.10",
        base_port=8298,
        callbacks=_callbacks(),
    )
    await registry.start()
    assert mass.subscription is not None
    callback = cast("Any", mass.subscription[0])

    await registry.stop()
    mass.players.items["kitchen"] = _player("kitchen", "Kitchen", "device-kitchen")
    callback(MassEvent(event=EventType.PLAYER_ADDED, object_id="kitchen"))
    await asyncio.sleep(0)

    assert registry.instances == {}
    assert mass.subscription is None


async def test_remove_during_start_finishes_then_cleans_up_renderer() -> None:
    """A removal racing startup cannot cancel cleanup and leak the HTTP renderer."""
    mass = _Mass([])
    registry = RendererRegistry(
        mass=cast("Any", mass),
        target_spec="*",
        friendly_prefix="Music Assistant",
        bind_ip="192.0.2.10",
        base_port=8298,
        callbacks=_callbacks(),
    )
    await registry.start()
    assert mass.subscription is not None
    callback = cast("Any", mass.subscription[0])
    player = _player("kitchen", "Kitchen", "device-kitchen")
    mass.players.items["kitchen"] = player
    _FakeRenderer.start_gate = asyncio.Event()

    callback(MassEvent(event=EventType.PLAYER_ADDED, object_id="kitchen"))
    while not _FakeRenderer.actions:
        await asyncio.sleep(0)

    del mass.players.items["kitchen"]
    callback(MassEvent(event=EventType.PLAYER_REMOVED, object_id="kitchen"))
    _FakeRenderer.start_gate.set()
    await asyncio.gather(*mass.tasks, return_exceptions=True)

    assert registry.instances == {}
    udn = deterministic_udn("kitchen")
    assert ("ssdp-stop", udn) in _FakeRenderer.actions
    assert ("renderer-stop", udn) in _FakeRenderer.actions
    await registry.stop()
