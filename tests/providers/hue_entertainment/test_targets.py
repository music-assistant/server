"""Tests for the Hue Lights Sync light targets."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from hue_entertainment import EntertainmentArea, LightsBusyError, ZigbeeLight, ZigguratError

from music_assistant.providers.hue_entertainment import targets
from music_assistant.providers.hue_entertainment.constants import CONF_CLIENTKEY, CONF_USERNAME
from music_assistant.providers.hue_entertainment.targets import (
    HueAreaTarget,
    TargetUnavailableError,
    ZigbeeTarget,
)
from music_assistant.providers.hue_entertainment.zigbee import ZhaDevice, ZhaUnavailableError

from .fakes import ZIGGURAT_URL


class FakeZigbeeSession:
    """Stands in for the lib's session; start and close take turns on one lock, like the lib."""

    def __init__(
        self, zigbee: FakeZigbee, url: str, lights: list[ZigbeeLight], *, idle_timeout: float
    ) -> None:
        """Initialize the session."""
        self.url = url
        self.lights = lights
        self.idle_timeout = idle_timeout
        self.is_streaming = False
        self.restored = False
        self.start_calls = 0
        self._zigbee = zigbee
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start streaming; held while the test holds the start gate."""
        async with self._lock:
            if self.is_streaming:
                return
            self.start_calls += 1
            self._zigbee.log.append(("start", self))
            if (gate := self._zigbee.start_gate) is not None:
                await gate.wait()
            if self._zigbee.fail_starts:
                self._zigbee.fail_starts -= 1
                msg = "Ziggurat unreachable"
                raise ZigguratError(msg)
            if self._zigbee.busy:
                raise LightsBusyError(["Desk lamp"])
            self.is_streaming = True

    def send(self, commands: list[Any]) -> None:
        """Ignore the frame."""

    async def aclose(self) -> None:
        """Stop streaming and restore the lamps; held while the test holds the close gate."""
        async with self._lock:
            self._zigbee.log.append(("close", self))
            if (gate := self._zigbee.close_gate) is not None:
                await gate.wait()
            self.is_streaming = False
            self.restored = True


class FakeZigbee:
    """ZHA's lamps and the Zigbee sessions the targets open."""

    def __init__(self) -> None:
        """Initialize with three lamps in ZHA."""
        self.devices = {
            "aa": ZhaDevice(0x0A0A, "Desk lamp"),
            "bb": ZhaDevice(0x0B0B, "Signe"),
            "cc": ZhaDevice(0x0C0C, "Go"),
        }
        self.targets: list[ZigbeeTarget] = []
        self.sessions: list[FakeZigbeeSession] = []
        self.log: list[tuple[str, FakeZigbeeSession]] = []
        # the number of session starts still to fail, as with Ziggurat unreachable
        self.fail_starts = 0
        # another app streams to the lamps while this is set
        self.busy = False
        self.info_gate: asyncio.Event | None = None
        self.info_error: Exception | None = None
        self.info_calls = 0
        self.start_gate: asyncio.Event | None = None
        self.close_gate: asyncio.Event | None = None

    async def stream_info(self, _mass: Any) -> tuple[str, dict[str, ZhaDevice]]:
        """Return the Ziggurat URL and the ZHA devices."""
        self.info_calls += 1
        if self.info_gate is not None:
            await self.info_gate.wait()
        if self.info_error is not None:
            raise self.info_error
        return ZIGGURAT_URL, dict(self.devices)

    def session(
        self, url: str, lights: list[ZigbeeLight], *, idle_timeout: float
    ) -> FakeZigbeeSession:
        """Create a session, as the target does."""
        session = FakeZigbeeSession(self, url, lights, idle_timeout=idle_timeout)
        self.sessions.append(session)
        return session

    def release_gates(self) -> None:
        """Let everything that waits on a gate continue."""
        for gate in (self.info_gate, self.start_gate, self.close_gate):
            if gate is not None:
                gate.set()


@pytest.fixture
async def zigbee(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeZigbee]:
    """Stand in for ZHA and the lib's Zigbee session; reset the shared stream slot."""
    fake = FakeZigbee()
    monkeypatch.setattr(targets, "stream_info", fake.stream_info)
    monkeypatch.setattr(targets, "ZigbeeEntertainmentSession", fake.session)
    monkeypatch.setattr(targets, "_ZIGBEE_RECOVERY_INTERVAL_S", 0.01)
    monkeypatch.setattr(ZigbeeTarget, "_holder", None)
    monkeypatch.setattr(ZigbeeTarget, "_restoring", set())
    yield fake
    fake.release_gates()
    for target in fake.targets:
        await target.aclose()


@pytest.fixture
def make_target(zigbee: FakeZigbee) -> Callable[..., ZigbeeTarget]:
    """Return a factory for Zigbee targets that the fixture closes after the test."""

    def _make(
        name: str = "Living room", lights: list[tuple[str, int]] | None = None
    ) -> ZigbeeTarget:
        provider: Any = SimpleNamespace(mass=None, instance_id=f"hue_entertainment--{name}")
        target = ZigbeeTarget(provider, lights or [("aa", 1)], name)
        zigbee.targets.append(target)
        return target

    return _make


async def _until(condition: Callable[[], bool]) -> None:
    """Let the event loop run until the condition holds."""
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0)


def _provider(instance_id: str = "hue_entertainment--AbCd1234") -> Any:
    return SimpleNamespace(mass=None, instance_id=instance_id)


def test_channels_on_an_evenly_spaced_row() -> None:
    """The channels sit evenly from left to right, the zones of a gradient lamp side by side."""
    target = ZigbeeTarget(_provider(), [("aa", 1), ("bb", 3), ("cc", 1)], "Living room")

    channels = target.channels

    assert [channel.channel_id for channel in channels] == [0, 1, 2, 3, 4]
    assert [channel.service_id for channel in channels] == ["aa", "bb", "bb", "bb", "cc"]
    assert [channel.position for channel in channels] == [
        pytest.approx((x, 0.0, 0.0)) for x in (-1.0, -0.5, 0.0, 0.5, 1.0)
    ]


def test_single_channel_sits_in_the_middle() -> None:
    """A single lamp without zones gets the centre position."""
    target = ZigbeeTarget(_provider(), [("aa", 1)], "Desk")

    assert [channel.position for channel in target.channels] == [(0.0, 0.0, 0.0)]


def test_zigbee_client_id_follows_the_instance() -> None:
    """The client id derives from the instance, so choosing other lamps keeps the player."""
    first = ZigbeeTarget(_provider(), [("aa", 1)], "Desk")
    other_lamps = ZigbeeTarget(_provider(), [("bb", 2), ("cc", 1)], "Desk")

    assert first.client_id == "hue-zb-AbCd1234"
    assert other_lamps.client_id == first.client_id


def test_hue_area_client_id_is_unchanged() -> None:
    """An entertainment area keeps the client id derived from its area id."""
    area = EntertainmentArea(id="8a3e1c2f-1b2c-4d5e-9f00-112233445566", name="TV")

    assert HueAreaTarget(_provider(), area).client_id == "hue-8a3e1c2f1b2c4d5e"


class FakeHueSession:
    """Stands in for the lib's bridge session."""

    def __init__(self, hue: FakeHue, host: str, username: str, clientkey: str, **_: Any) -> None:
        """Initialize the session."""
        self._hue = hue
        self.host = host
        self.credentials = (username, clientkey)
        self.closed = 0

    async def start(self, area_id: str) -> None:
        """Start streaming, or fail while the test makes starts fail."""
        self._hue.log.append(("start", area_id))
        if self._hue.fail_starts:
            self._hue.fail_starts -= 1
            msg = "DTLS handshake timed out"
            raise TimeoutError(msg)

    async def aclose(self) -> None:
        """Stop streaming, or fail as the test asks."""
        self.closed += 1
        if self._hue.close_error is not None:
            raise self._hue.close_error


class FakeHue:
    """The paired Hue bridge and the sessions the area target opens."""

    def __init__(self) -> None:
        """Initialize a bridge whose area is not streaming."""
        self.status = "inactive"
        self.log: list[tuple[str, str]] = []
        self.sessions: list[FakeHueSession] = []
        self.fail_starts = 0
        self.close_error: Exception | None = None
        self.host = "192.168.1.20"

    async def get_entertainment_status(self, area_id: str) -> tuple[str, str]:
        """Return the area's entertainment status."""
        return self.status, area_id

    async def stop_entertainment(self, area_id: str) -> None:
        """Stop the area's entertainment."""
        self.log.append(("stop", area_id))
        self.status = "inactive"

    def session(self, host: str, username: str, clientkey: str, **kwargs: Any) -> FakeHueSession:
        """Create a session, as the target does."""
        session = FakeHueSession(self, host, username, clientkey, **kwargs)
        self.sessions.append(session)
        return session


@pytest.fixture
def hue(monkeypatch: pytest.MonkeyPatch) -> FakeHue:
    """Stand in for the paired bridge and the lib's bridge session."""
    fake = FakeHue()
    monkeypatch.setattr(targets, "EntertainmentSession", fake.session)
    monkeypatch.setattr(targets, "_ENTERTAINMENT_STALE_COOLDOWN_S", 0)
    return fake


def _area_target(hue: FakeHue | None) -> HueAreaTarget:
    provider: Any = SimpleNamespace(
        hue_api=hue, get_setup_value={CONF_USERNAME: "user", CONF_CLIENTKEY: "key"}.get
    )
    return HueAreaTarget(provider, EntertainmentArea(id="area-1", name="TV"))


async def test_hue_area_start_without_a_bridge_is_unavailable() -> None:
    """Without a configured bridge the area can not stream, and a retry will not help."""
    with pytest.raises(TargetUnavailableError):
        await _area_target(None).start()


async def test_hue_area_start_clears_a_stale_stream_once(hue: FakeHue) -> None:
    """An area still active from a failed handshake is stopped before the stream starts."""
    hue.status = "active"
    target = _area_target(hue)

    await target.start()

    assert hue.log == [("stop", "area-1"), ("start", "area-1")]
    (session,) = hue.sessions
    assert session.host == hue.host
    assert session.credentials == ("user", "key")


async def test_hue_area_retry_reuses_the_session_until_closed(hue: FakeHue) -> None:
    """A start after a failed one reuses the session; after a close the next start is fresh."""
    hue.status = "active"
    hue.fail_starts = 1
    target = _area_target(hue)
    with pytest.raises(TimeoutError):
        await target.start()
    hue.status = "active"
    await target.start()
    assert len(hue.sessions) == 1
    assert hue.log == [("stop", "area-1"), ("start", "area-1"), ("start", "area-1")]

    await target.aclose()
    await target.start()

    first, second = hue.sessions
    assert first.closed == 1
    assert second.closed == 0
    assert hue.log[-2:] == [("stop", "area-1"), ("start", "area-1")]


async def test_hue_area_close_is_idempotent_and_never_raises(hue: FakeHue) -> None:
    """Closing twice closes the session once; an error while closing is swallowed."""
    target = _area_target(hue)
    await target.start()
    hue.close_error = RuntimeError("bridge went away")

    await target.aclose()
    await target.aclose()

    (session,) = hue.sessions
    assert session.closed == 1
    assert not target.is_streaming


async def test_start_streams_to_the_lamps_by_their_zha_name(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """The session gets each channel's current address and ZHA name; lamps gone are skipped."""
    target = make_target(lights=[("aa", 1), ("gone", 1), ("bb", 2)])

    await target.start()

    (session,) = zigbee.sessions
    assert session.url == ZIGGURAT_URL
    assert session.idle_timeout == 0
    assert session.lights == [
        ZigbeeLight(0, "aa", 0x0A0A, "Desk lamp", 0),
        ZigbeeLight(2, "bb", 0x0B0B, "Signe", 0),
        ZigbeeLight(3, "bb", 0x0B0B, "Signe", 1),
    ]
    assert target.is_streaming


async def test_start_without_any_lamp_in_zha_is_unavailable(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """When none of the lamps is in ZHA, the start fails without opening a session."""
    target = make_target(lights=[("gone", 1)])

    with pytest.raises(TargetUnavailableError):
        await target.start()

    assert not zigbee.sessions


async def test_failed_start_can_be_retried(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A start that fails on Ziggurat raises, and the next start opens a new session."""
    target = make_target()
    zigbee.fail_starts = 1

    with pytest.raises(ZigguratError):
        await target.start()
    assert not target.is_streaming

    await target.start()

    assert len(zigbee.sessions) == 2
    assert target.is_streaming


async def test_takeover_restores_the_previous_lamps_first(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A newer start restores the lamps of the target that streams before it starts its own."""
    living, kitchen = make_target("Living room"), make_target("Kitchen", [("bb", 1)])
    await living.start()
    zigbee.close_gate = asyncio.Event()

    kitchen_start = asyncio.create_task(kitchen.start())
    await _until(lambda: len(zigbee.log) == 2)
    # the previous lamps are being restored: the new stream waits for that
    assert len(zigbee.sessions) == 1
    zigbee.close_gate.set()
    await kitchen_start

    living_session, kitchen_session = zigbee.sessions
    assert zigbee.log == [
        ("start", living_session),
        ("close", living_session),
        ("start", kitchen_session),
    ]
    assert living_session.restored
    assert not living.is_streaming
    assert kitchen.is_streaming


async def test_start_waits_for_the_restore_of_a_closed_target(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A start right after another target closed waits until that one restored its lamps."""
    living, kitchen = make_target("Living room"), make_target("Kitchen", [("aa", 1)])
    await living.start()
    zigbee.close_gate = asyncio.Event()
    living_close = asyncio.create_task(living.aclose())
    await _until(lambda: len(zigbee.log) == 2)

    kitchen_start = asyncio.create_task(kitchen.start())
    await _until(lambda: zigbee.info_calls == 2)
    await asyncio.sleep(0.01)
    assert len(zigbee.sessions) == 1
    zigbee.close_gate.set()
    await living_close
    await kitchen_start

    living_session, kitchen_session = zigbee.sessions
    assert zigbee.log == [
        ("start", living_session),
        ("close", living_session),
        ("start", kitchen_session),
    ]
    assert kitchen.is_streaming


async def test_chained_takeover_waits_for_the_first_restore(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A third start during a takeover still waits for the lamps of the first target."""
    living = make_target("Living room")
    kitchen = make_target("Kitchen", [("bb", 1)])
    office = make_target("Office", [("aa", 1)])
    await living.start()
    zigbee.close_gate = asyncio.Event()
    kitchen_start = asyncio.create_task(kitchen.start())
    await _until(lambda: len(zigbee.log) == 2)

    office_start = asyncio.create_task(office.start())
    await _until(lambda: ZigbeeTarget._holder is office)
    await asyncio.sleep(0.01)
    assert len(zigbee.sessions) == 1
    zigbee.close_gate.set()
    await kitchen_start
    await office_start

    living_session, office_session = zigbee.sessions
    assert zigbee.log == [
        ("start", living_session),
        ("close", living_session),
        ("start", office_session),
    ]
    assert not kitchen.is_streaming
    assert office.is_streaming


async def test_displaced_target_close_keeps_the_new_stream(
    make_target: Callable[..., ZigbeeTarget],
) -> None:
    """Closing the displaced target leaves the stream that took over running."""
    living, kitchen = make_target("Living room"), make_target("Kitchen", [("bb", 1)])
    await living.start()
    await kitchen.start()

    await living.aclose()

    assert kitchen.is_streaming
    assert ZigbeeTarget._holder is kitchen


async def test_takeover_while_the_previous_still_starts(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A start taken over halfway returns without streaming and its lamps are restored."""
    living, kitchen = make_target("Living room"), make_target("Kitchen", [("bb", 1)])
    living_gate = zigbee.start_gate = asyncio.Event()
    living_start = asyncio.create_task(living.start())
    await _until(lambda: len(zigbee.log) == 1)
    zigbee.start_gate = None

    kitchen_start = asyncio.create_task(kitchen.start())
    await _until(lambda: ZigbeeTarget._holder is kitchen)
    living_gate.set()
    await living_start
    await kitchen_start

    living_session, _kitchen_session = zigbee.sessions
    assert living_session.restored
    assert not living.is_streaming
    assert kitchen.is_streaming
    await asyncio.sleep(0.05)
    assert living_session.start_calls == 1


async def test_failed_start_after_a_takeover_returns_quietly(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A start that fails once a newer one took over does not raise, so it is not retried."""
    living, kitchen = make_target("Living room"), make_target("Kitchen", [("bb", 1)])
    living_gate = zigbee.start_gate = asyncio.Event()
    zigbee.fail_starts = 1
    living_start = asyncio.create_task(living.start())
    await _until(lambda: len(zigbee.log) == 1)
    zigbee.start_gate = None

    kitchen_start = asyncio.create_task(kitchen.start())
    await _until(lambda: ZigbeeTarget._holder is kitchen)
    living_gate.set()
    await living_start
    await kitchen_start

    assert not living.is_streaming
    assert kitchen.is_streaming
    assert ZigbeeTarget._holder is kitchen


async def test_recovery_restarts_a_lost_stream_in_place(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A lost stream is restarted on the same session, retrying until Ziggurat is back."""
    target = make_target()
    await target.start()
    (session,) = zigbee.sessions

    zigbee.fail_starts = 1
    session.is_streaming = False
    await _until(lambda: session.is_streaming)

    assert session.start_calls == 3
    assert len(zigbee.sessions) == 1
    assert target.is_streaming


async def test_start_waits_while_another_app_streams_to_the_lamps(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A start on busy lamps returns without streaming and streams once they are free."""
    zigbee.busy = True
    living = make_target("Living room")

    await living.start()
    assert not living.is_streaming
    assert ZigbeeTarget._holder is living
    await asyncio.sleep(0.05)
    assert not living.is_streaming

    zigbee.busy = False
    await _until(lambda: living.is_streaming)
    (session,) = zigbee.sessions
    assert session.start_calls > 2


async def test_displaced_target_does_not_recover(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A target that lost the stream to a newer start stays parked."""
    living, kitchen = make_target("Living room"), make_target("Kitchen", [("bb", 1)])
    await living.start()
    await kitchen.start()

    await asyncio.sleep(0.05)

    living_session, _kitchen_session = zigbee.sessions
    assert living_session.start_calls == 1
    assert not living_session.is_streaming
    assert not living.is_streaming
    assert len(zigbee.sessions) == 2


async def test_cancelled_start_still_restores_the_previous_lamps(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """Cancelling a start while it waits for the takeover does not stop the restore."""
    living, kitchen = make_target("Living room"), make_target("Kitchen", [("bb", 1)])
    await living.start()
    zigbee.close_gate = asyncio.Event()
    kitchen_start = asyncio.create_task(kitchen.start())
    await _until(lambda: len(zigbee.log) == 2)

    kitchen_start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await kitchen_start
    zigbee.close_gate.set()
    (living_session,) = zigbee.sessions
    await _until(lambda: living_session.restored)

    await kitchen.aclose()
    assert ZigbeeTarget._holder is None
    assert len(zigbee.sessions) == 1


async def test_failing_stream_info_keeps_the_current_stream(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """A start that can not reach ZHA leaves the stream of another target running."""
    living, kitchen = make_target("Living room"), make_target("Kitchen", [("bb", 1)])
    await living.start()
    zigbee.info_error = ZhaUnavailableError("the Home Assistant plugin is not connected")

    with pytest.raises(TargetUnavailableError, match="not connected"):
        await kitchen.start()

    assert living.is_streaming
    assert ZigbeeTarget._holder is living


async def test_takeover_waits_for_stream_info(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """The current stream keeps running while a newer start still asks ZHA for the lamps."""
    living, kitchen = make_target("Living room"), make_target("Kitchen", [("bb", 1)])
    await living.start()
    zigbee.info_gate = asyncio.Event()

    kitchen_start = asyncio.create_task(kitchen.start())
    await _until(lambda: zigbee.info_calls == 2)
    await asyncio.sleep(0)
    assert living.is_streaming
    assert ZigbeeTarget._holder is living
    zigbee.info_gate.set()
    await kitchen_start

    living_session, kitchen_session = zigbee.sessions
    assert zigbee.log == [
        ("start", living_session),
        ("close", living_session),
        ("start", kitchen_session),
    ]
    assert kitchen.is_streaming


async def test_close_restores_the_lamps_and_frees_the_stream(
    zigbee: FakeZigbee, make_target: Callable[..., ZigbeeTarget]
) -> None:
    """Closing the streaming target restores its lamps and frees the stream; it is idempotent."""
    target = make_target()
    await target.start()

    await target.aclose()
    await target.aclose()

    (session,) = zigbee.sessions
    assert session.restored
    assert not target.is_streaming
    assert ZigbeeTarget._holder is None
