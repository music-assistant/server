"""
Tests for the Sendspin bridge of the squeezelite provider.

A real bridge and bridge manager run over a mocked provider, player and slimproto client,
on a ``ManualClock`` that both Sendspin and the player's clock mapping read, so every
instant in these tests is exact. The player is simulated by the events it would send:
status reports of a clock that runs a fixed offset from the server clock, buffer ready,
output underrun and state updates.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import AsyncGenerator, Callable
from itertools import pairwise
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiosendspin.clock import ManualClock, RawMonotonicClock
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.roles import AudioChunk
from aioslimproto.client import SlimClient
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from aioslimproto.models import PlayerStatus, SlimEvent
from music_assistant_models.enums import ContentType, IdentifierType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.sendspin.bridge_role import BRIDGE_SAMPLE_RATE
from music_assistant.providers.squeezelite.bridge_role import (
    SLIMPROTO_BRIDGE_ROLE_ID,
    SlimprotoBridgeRole,
)
from music_assistant.providers.squeezelite.bridge_sync import MAX_START_JUMP_MS
from music_assistant.providers.squeezelite.sendspin_bridge import (
    BRIDGE_MIN_BUFFER_MS,
    BRIDGE_REQUIRED_LEAD_MS,
    BRIDGE_STREAM_PATH,
    CACHE_CATEGORY_START_OFFSET,
    CLOCK_MAX_AGE_US,
    MAX_BLOCK_US,
    MAX_PENDING_AUDIO_US,
    MAX_PENDING_CHUNKS,
    MAX_PENDING_STATUS,
    MAX_START_AHEAD_US,
    MIN_START_AHEAD_US,
    OUTPUT_THRESHOLD,
    PREROLL_US,
    RECONNECT_REJOIN_WINDOW,
    RECOVERY_GUARD_US,
    STARVED_LEAD_US,
    STATUS_TOKEN_BIT,
    STREAM_LEAD_US,
    STREAM_THRESHOLD_KB,
    BridgeSession,
    SendspinBridgeManager,
    SendspinSqueezeliteBridge,
)
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from aiosendspin.models.core import ClientHelloPayload

MODULE = "music_assistant.providers.squeezelite.sendspin_bridge"

# A large, arbitrary reading of the server clock (microseconds): nothing may depend on it.
EPOCH_US = 5_000_000_000_000
# The player's clock (milliseconds) at that instant.
JIFFIES_AT_EPOCH = 7_000_000
PLAYER_ID = "aa:bb:cc:dd:ee:ff"
CLIENT_ID = "spb_aabbccddeeff"
BASE_URL = "http://192.168.1.10:8097"
# The format of a stream in these tests, unless a test is about another one: what a
# player that plays nothing else gets.
CD_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE, sample_rate=BRIDGE_SAMPLE_RATE, bit_depth=16, channels=2
)
MIME_TYPE = "audio/pcm;rate=44100;bitrate=16;channels=2"
FRAME_SIZE = 4
# 10 ms of audio: chunks of a multiple of it have timestamps in whole microseconds
TEN_MS = BRIDGE_SAMPLE_RATE // 100
# what the player is sent of a second of audio
SECOND = BRIDGE_SAMPLE_RATE
PREROLL_FRAMES = SECOND * PREROLL_US // 1_000_000
STREAM_LEAD_FRAMES = SECOND * STREAM_LEAD_US // 1_000_000
STARVED_LEAD_FRAMES = SECOND * STARVED_LEAD_US // 1_000_000
MAX_BLOCK_FRAMES = SECOND * MAX_BLOCK_US // 1_000_000


# ---------------------------------------------------------------- helpers


async def _settle() -> None:
    """Let the tasks that are ready run."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _wait_for(condition: Callable[[], bool]) -> None:
    """Wait until a condition holds, for what only time brings about."""
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


def _frame_us(frames: int) -> int:
    """Return the duration of a number of frames in microseconds."""
    return round(frames * 1_000_000 / BRIDGE_SAMPLE_RATE)


def _chunk(origin_us: int, first: int, count: int, *, shift_us: int = 0) -> AudioChunk:
    """
    Return a chunk of a source whose frame 0 is due at ``origin_us``.

    Every frame holds its source frame number plus one, so 0 is silence.

    :param origin_us: The timestamp of source frame 0.
    :param first: The first source frame of the chunk.
    :param count: The number of frames of the chunk.
    :param shift_us: How far off its place the timestamp of the chunk is.
    """
    data = struct.pack(f"<{count}I", *range(first + 1, first + count + 1))
    return AudioChunk(
        data=data,
        timestamp_us=origin_us + _frame_us(first) + shift_us,
        duration_us=_frame_us(count),
        byte_count=len(data),
    )


def _frames(pcm: bytes) -> list[int]:
    """Return the source frame number of every frame of PCM, -1 for silence."""
    return [value - 1 for (value,) in struct.iter_unpack("<I", pcm)]


def _chunk_24(origin_us: int, first: int, count: int) -> AudioChunk:
    """
    Return a 48 kHz chunk of 24-bit audio as Sendspin delivers it: 32 bits per sample.

    Both samples of a frame hold its source frame number plus one, in their upper 24 bits.

    :param origin_us: The timestamp of source frame 0.
    :param first: The first source frame of the chunk.
    :param count: The number of frames of the chunk.
    """
    samples = [number << 8 for number in range(first + 1, first + count + 1) for _ in range(2)]
    data = struct.pack(f"<{2 * count}i", *samples)
    return AudioChunk(
        data=data,
        timestamp_us=origin_us + first * 1_000_000 // 48000,
        duration_us=count * 1_000_000 // 48000,
        byte_count=len(data),
    )


def _frames_24(pcm: bytes) -> list[int]:
    """Return the source frame number of every frame of 24-bit stereo PCM, -1 for silence."""
    assert len(pcm) % 6 == 0
    frames = []
    for at in range(0, len(pcm), 6):
        assert pcm[at : at + 3] == pcm[at + 3 : at + 6]
        frames.append(int.from_bytes(pcm[at : at + 3], "little") - 1)
    return frames


def _slim_client(device_type: str = "squeezeplay") -> MagicMock:
    """Return a connected, stopped slimproto client."""
    client = MagicMock(
        player_id=PLAYER_ID,
        device_type=device_type,
        device_model="SqueezeLite",
        connected=True,
        powered=True,
        state=SlimPlayerState.STOPPED,
        volume_level=20,
        muted=False,
    )
    for command in ("play_url", "unpause_at", "stop", "request_status", "power", "mute"):
        setattr(client, command, AsyncMock())
    return client


def _grouped_client(*, members: int = 2) -> MagicMock:
    """
    Return a Sendspin client in a group with other clients.

    Leaving moves it on to a solo group, as the real one does.
    """
    client = MagicMock()
    client.group.clients = [MagicMock() for _ in range(members)]
    client.group.has_active_stream = True
    client.group.add_client = AsyncMock()

    async def _quiesce() -> str:
        client.group = MagicMock(clients=[client], has_active_stream=False)
        return "group-1"

    client.quiesce_to_solo_stopped = AsyncMock(side_effect=_quiesce)
    return client


class _Response:
    """Stands in for the HTTP response of the stream: keeps what is written to it."""

    def __init__(self, fail_after: int | None = None) -> None:
        """
        Initialize the response.

        :param fail_after: The number of writes after which the connection is gone.
        """
        self.body = bytearray()
        self.prepare = AsyncMock()
        self._writes_left = fail_after

    async def write(self, data: bytes) -> None:
        """Take in what the bridge writes."""
        if self._writes_left is not None:
            if not self._writes_left:
                raise ConnectionResetError
            self._writes_left -= 1
        self.body += data


class _Rig:
    """A bridge of a player, with everything around it mocked."""

    def __init__(self, *, sync_adjust: int = 0, device_type: str = "squeezeplay") -> None:
        """
        Initialize the rig.

        :param sync_adjust: The sync delay setting of the player.
        :param device_type: The device type the player reports.
        """
        self.clock = ManualClock(now_us_value=EPOCH_US)
        self.mass = mass = MagicMock()
        use_real_create_task(mass)
        mass.loop = asyncio.get_running_loop()
        mass.closing = False
        mass.streams.base_url = BASE_URL
        mass.players.cmd_pause = AsyncMock()
        mass.players.remove_protocol = AsyncMock()
        mass.cache.get = AsyncMock(return_value=None)
        mass.cache.set = AsyncMock()
        # the Sendspin provider and its server
        self.sendspin = MagicMock()
        self.server = server = self.sendspin.server_api
        server.clock = self.clock
        server.remove_client = AsyncMock()
        server.is_external_player.return_value = False
        self.sendspin_client = server.register_external_player.return_value
        server.register_external_player.side_effect = self._register
        self.role = SlimprotoBridgeRole(client=self.sendspin_client)
        self.sendspin_client.roles_by_family.return_value = [self.role]
        mass.get_provider.return_value = self.sendspin
        # the squeezelite provider, its player and the slimproto client of that player
        self.provider = provider = MagicMock()
        provider.mass = mass
        provider.clock = self.clock
        provider.instance_id = "squeezelite"
        provider.logger = logging.getLogger("test.squeezelite")
        self.manager = provider.bridge_manager = SendspinBridgeManager(provider)
        self.client = _slim_client(device_type)
        self.protocol = protocol = MagicMock()
        protocol.protocol_id = PLAYER_ID
        protocol.display_name = "Kitchen"
        protocol.available = True
        protocol.client = self.client
        protocol.provider = provider
        protocol.config.get_value = MagicMock(return_value=sync_adjust)
        protocol.get_supported_sample_rates.return_value = [(44100, 16)]
        protocol.volume_set = AsyncMock()
        protocol.release_native_playback = AsyncMock()
        protocol.sendspin_bridge = None
        protocol.player.get_output_protocol_by_domain.return_value = None
        protocol._on_detach_callbacks = []
        provider.get_protocol.side_effect = {PLAYER_ID: protocol}.get
        provider.protocols = [protocol]
        self.bridge = SendspinSqueezeliteBridge(provider, protocol, server)

    def _register(self, hello: ClientHelloPayload, **_kwargs: Any) -> MagicMock:
        """Stand in for the registration with Sendspin: its client knows the hello from then on."""
        client: MagicMock = self.server.register_external_player.return_value
        client.info = hello
        return client

    @property
    def session(self) -> BridgeSession:
        """Return the current session of the bridge."""
        session = self.bridge._session
        assert session is not None
        return session

    @property
    def now_us(self) -> int:
        """Return the time on the server clock."""
        return self.clock.now_us()

    def owns_stream(self) -> bool:
        """Return whether the stream on the player is that of the bridge."""
        return self.bridge.owns_stream

    def jiffies(self, server_us: int | None = None) -> int:
        """Return the player's clock at a server time, now by default."""
        server_us = self.now_us if server_us is None else server_us
        return JIFFIES_AT_EPOCH + (server_us - EPOCH_US) // 1000

    def map_clock(self) -> None:
        """Let the bridge know the player's clock, from three replies to its requests."""
        for number, back_us in enumerate((400_000, 200_000, 0)):
            token = STATUS_TOKEN_BIT | (1 << 30) + number
            self.bridge._pending_status[token] = self.now_us - back_us
            self.bridge.handle_slim_event(self.status(token=token, at_us=self.now_us - back_us))

    def status(self, *, elapsed_ms: int = 0, token: int = 0, at_us: int | None = None) -> SlimEvent:
        """
        Return a status the player reports.

        :param elapsed_ms: The elapsed time of its stream.
        :param token: The token of the request it answers, 0 when it was not asked.
        :param at_us: The time it reports at, now by default.
        """
        at_us = self.now_us if at_us is None else at_us
        status = PlayerStatus(
            received=at_us / 1_000_000,
            jiffies=self.jiffies(at_us),
            elapsed_seconds=elapsed_ms // 1000,
            elapsed_milliseconds=elapsed_ms,
            bytes_received=0,
            stream_buffer_size=0,
            stream_buffer_fullness=0,
            output_buffer_size=0,
            output_buffer_fullness=0,
            server_timestamp=token,
        )
        return SlimEvent(SlimEventType.PLAYER_HEARTBEAT, PLAYER_ID, status)

    def event(self, event_type: SlimEventType) -> None:
        """Let the player report an event."""
        self.bridge.handle_slim_event(SlimEvent(event_type, PLAYER_ID))

    async def start_stream(self) -> BridgeSession:
        """Start a Sendspin stream and let the bridge request its stream from the player."""
        self.bridge._on_stream_start(MagicMock())
        await _settle()
        return self.session

    async def start_playing(
        self,
        *,
        first_chunk_ahead_us: int = 2_000_000,
        frames: int = 4 * SECOND,
        chunk: Callable[[int, int, int], AudioChunk] = _chunk,
    ) -> BridgeSession:
        """
        Start a Sendspin stream and take it up to the start of the player.

        The player's clock is known, it fetched the stream and got its preroll, a first
        chunk arrived and it reported its buffer ready.

        :param first_chunk_ahead_us: How far ahead of now the first chunk is due.
        :param frames: The number of frames of that chunk.
        :param chunk: What makes that chunk, in the format of the stream.
        """
        session = await self.start_stream()
        self.map_clock()
        self.fetched(session)
        self.origin_us = self.now_us + first_chunk_ahead_us
        self.bridge._on_audio_chunk(chunk(self.origin_us, 0, frames))
        self.event(SlimEventType.PLAYER_BUFFER_READY)
        await _settle()
        return session

    async def start_hires(self) -> None:
        """Register the bridge of a player that plays up to 48 kHz / 24 bit, which Sendspin picks."""
        self.protocol.get_supported_sample_rates.return_value = [
            (44100, 16),
            (44100, 24),
            (48000, 16),
            (48000, 24),
        ]
        await self.bridge.start()
        picked = SendspinAudioFormat(sample_rate=48000, bit_depth=24, channels=2)
        assert self.role.set_preferred_format(picked, AudioCodec.PCM)

    def fetched(self, session: BridgeSession) -> None:
        """Let the player have fetched the stream of a session and have got its preroll."""
        session.claimed = True
        session.preroll_sent = True

    def play(self, session: BridgeSession, seconds: float, *, late_ms: int = 0) -> None:
        """
        Let the player play for a while and report four times a second.

        :param session: The session it plays.
        :param seconds: How long it plays.
        :param late_ms: How far behind its timeline it plays.
        """
        for _ in range(round(seconds * 4)):
            self.clock.advance_us(250_000)
            elapsed_ms = (self.now_us - session.start_us) // 1000 - late_ms
            self.bridge.handle_slim_event(self.status(elapsed_ms=max(0, elapsed_ms)))

    def drain(self, session: BridgeSession) -> bytes:
        """Return everything the writer may send to the player now."""
        parts = []
        while block := self.bridge._next_block(session):
            parts.append(block)
        return b"".join(parts)

    def grouped(self) -> MagicMock:
        """Let the bridge be registered, with its client in a group with other clients."""
        self.bridge._sendspin_client = client = _grouped_client()
        self.bridge._bridge_client_id = CLIENT_ID
        return client

    def serve(self, session: BridgeSession, response: _Response) -> asyncio.Task[Any]:
        """Let the player fetch the stream of a session, into a response."""
        request = MagicMock(method="GET", query={"sid": session.session_id})

        async def _serve() -> Any:
            with patch("aiohttp.web.StreamResponse", return_value=response):
                return await self.bridge.serve_stream(request)

        return asyncio.create_task(_serve())

    async def close(self) -> None:
        """Stop the bridge and everything that was started around it."""
        await self.bridge.stop()
        await self.manager.close()
        tasks = list(self.mass._tracked_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
async def rig() -> AsyncGenerator[_Rig]:
    """Return a bridge of a player that is not registered with Sendspin yet."""
    rig = _Rig()
    yield rig
    await rig.close()


@pytest.fixture(autouse=True)
def _timing() -> Any:
    """
    Take real time out of the bridge.

    The writer of a stream runs without waiting between its rounds, and the player is
    asked for its status once per session: a test that is about polling sets its own pace.
    """
    with (
        patch(f"{MODULE}.WRITE_INTERVAL", 0),
        patch(f"{MODULE}.STATUS_POLL_INTERVAL", 60),
        patch(f"{MODULE}.STATUS_POLL_INTERVAL_UNMAPPED", 60),
    ):
        yield


# ---------------------------------------------------------------- policy


async def test_a_player_with_a_timed_start_gets_a_bridge(rig: _Rig) -> None:
    """A player with a MAC address as id that can start at a set instant is bridged."""
    assert rig.manager._bridge_client_id(rig.protocol) == CLIENT_ID
    assert rig.manager._should_have_bridge(rig.protocol) is True


@pytest.mark.parametrize("protocol_id", ["kitchen", "00:00:00:00:00:00", ""])
async def test_a_player_without_a_mac_address_gets_no_bridge(rig: _Rig, protocol_id: str) -> None:
    """The bridge client id comes from the MAC address, so there is none without one."""
    rig.protocol.protocol_id = protocol_id

    assert rig.manager._bridge_client_id(rig.protocol) is None
    assert rig.manager._should_have_bridge(rig.protocol) is False


@pytest.mark.parametrize(
    ("device_type", "bridged"),
    [
        ("squeezeplay", True),
        ("squeezeesp32", True),
        ("squeezebox2", True),
        ("boom", True),
        ("controller", True),
        # no timed start: the first Squeezebox, SqueezeSlave and whatever is unknown
        ("squeezebox", False),
        ("squeezeslave", False),
        ("unknown device", False),
    ],
)
async def test_only_a_device_with_a_timed_start_gets_a_bridge(
    rig: _Rig, device_type: str, bridged: bool
) -> None:
    """The bridge starts a stream at a set instant, which not every device type can do."""
    rig.client.device_type = device_type

    assert rig.manager._should_have_bridge(rig.protocol) is bridged


async def test_a_player_with_an_airplay_output_gets_no_bridge(rig: _Rig) -> None:
    """A device with AirPlay gets its Sendspin support over AirPlay."""
    outputs = rig.protocol.player.get_output_protocol_by_domain
    outputs.return_value = MagicMock()

    assert rig.manager._should_have_bridge(rig.protocol) is False
    outputs.assert_called_once_with("airplay")


@pytest.mark.parametrize(
    ("registered", "bridged"),
    [
        (CLIENT_ID, False),
        # the same MAC address with its locally administered bit set
        ("spb_a8bbccddeeff", False),
        ("spb_aabbccddee00", True),
    ],
)
async def test_a_device_another_bridge_registered_gets_no_bridge(
    rig: _Rig, registered: str, bridged: bool
) -> None:
    """One device has one Sendspin client, whichever form of its MAC address that is under."""
    rig.server.is_external_player.side_effect = lambda client_id: client_id == registered

    assert rig.manager._should_have_bridge(rig.protocol) is bridged


async def test_its_own_client_does_not_deny_the_bridge(rig: _Rig) -> None:
    """The client of the bridge that is in place is not that of another bridge."""
    await rig.manager.evaluate_bridge(rig.protocol)
    rig.server.is_external_player.return_value = True

    assert rig.manager._should_have_bridge(rig.protocol) is True


async def test_the_bridge_goes_when_airplay_shows_up_and_returns_when_it_goes(rig: _Rig) -> None:
    """The bridge follows the outputs of its player."""
    await rig.manager.evaluate_bridge(rig.protocol)
    assert rig.manager.get_bridge(PLAYER_ID) is not None
    # the Sendspin protocol of the bridge client, which rides on the squeezelite protocol
    rig.mass.players.get_protocol.return_value = MagicMock(underlying_protocol_id=PLAYER_ID)

    rig.protocol.player.get_output_protocol_by_domain.return_value = MagicMock()
    await rig.manager.evaluate_bridge(rig.protocol)

    assert rig.manager.get_bridge(PLAYER_ID) is None
    rig.server.remove_client.assert_awaited_once_with(CLIENT_ID)
    rig.mass.players.remove_protocol.assert_awaited_once_with(CLIENT_ID)

    rig.protocol.player.get_output_protocol_by_domain.return_value = None
    await rig.manager.evaluate_bridge(rig.protocol)

    assert rig.manager.get_bridge(PLAYER_ID) is not None


@pytest.mark.parametrize(
    ("changed", "evaluated"),
    [("output_protocols", True), ("volume_level", False), ("playback_state", False)],
)
async def test_a_change_of_outputs_evaluates_the_bridge_again(
    rig: _Rig, changed: str, evaluated: bool
) -> None:
    """A player whose outputs change may have gained or lost its AirPlay output."""
    other = MagicMock(provider=MagicMock())
    player = MagicMock(attached_protocols={PLAYER_ID: rig.protocol, "other": other})

    with patch.object(rig.manager, "evaluate_bridge", new=AsyncMock()) as evaluate:
        rig.manager._on_player_state_updated(player, {changed: (None, None)})
        await _settle()

    assert evaluate.await_args_list == ([call(rig.protocol)] if evaluated else [])


# ---------------------------------------------------------------- registration


async def test_registration_hands_sendspin_what_it_needs_in_order(rig: _Rig) -> None:
    """The Sendspin provider has to know the bridge client before that client appears."""
    steps = MagicMock()
    steps.attach_mock(rig.sendspin.register_bridge_identifiers, "identifiers")
    steps.attach_mock(rig.sendspin.register_bridge_underlying_protocol, "underlying")
    steps.attach_mock(rig.server.register_external_player, "register")
    role = MagicMock()
    rig.sendspin_client.roles_by_family.return_value = [role]
    steps.attach_mock(role, "role")

    await rig.bridge.start()

    assert [step[0] for step in steps.mock_calls] == [
        "identifiers",
        "underlying",
        "register",
        "register().roles_by_family",
        "role.set_callbacks",
        "role.set_preferred_format",
        "role.set_timing",
    ]
    role.set_timing.assert_called_once_with(required_lead_time_ms=2000, min_buffer_ms=2000)
    rig.sendspin.register_bridge_identifiers.assert_called_once_with(
        CLIENT_ID, {IdentifierType.MAC_ADDRESS: PLAYER_ID}
    )
    rig.sendspin.register_bridge_underlying_protocol.assert_called_once_with(CLIENT_ID, PLAYER_ID)
    assert rig.server.register_external_player.call_args.kwargs == {
        "on_stream_start": rig.bridge._on_stream_start
    }
    assert rig.bridge.is_registered
    assert rig.bridge.bridge_client_id == CLIENT_ID
    assert rig.protocol.sendspin_bridge is rig.bridge


async def test_the_hello_offers_volume_and_no_mute(rig: _Rig) -> None:
    """Muting a slimproto player switches its output off, so Sendspin is not offered a mute."""
    await rig.bridge.start()

    hello = rig.server.register_external_player.call_args.args[0]
    assert hello.client_id == CLIENT_ID
    assert hello.name == "Kitchen (Squeezelite)"
    assert hello.supported_roles == [SLIMPROTO_BRIDGE_ROLE_ID, "player@v1"]
    assert hello.player_support.supported_commands == [PlayerCommand.VOLUME]
    (audio_format,) = hello.player_support.supported_formats
    assert (audio_format.codec, audio_format.sample_rate) == (AudioCodec.PCM, 44100)
    assert (audio_format.bit_depth, audio_format.channels) == (16, 2)


async def test_the_role_is_set_up_for_the_player(rig: _Rig) -> None:
    """The role gets the audio format, the timing and the volume the player is at."""
    rig.client.volume_level = 35

    await rig.bridge.start()

    requirements = rig.role.get_audio_requirements()
    assert requirements is not None
    assert (requirements.sample_rate, requirements.bit_depth, requirements.channels) == (
        44100,
        16,
        2,
    )
    assert rig.role.get_required_lead_time_us() == BRIDGE_REQUIRED_LEAD_MS * 1000
    assert rig.role.get_min_buffer_us() == BRIDGE_MIN_BUFFER_MS * 1000
    assert rig.role.get_player_volume() == 35
    assert rig.role.get_player_muted() is False


async def test_no_bridge_on_another_clock_than_that_of_sendspin(rig: _Rig) -> None:
    """Times of the player and of Sendspin are only compared when they are on one clock."""
    rig.server.clock = ManualClock(now_us_value=1)

    await rig.bridge.start()

    assert not rig.bridge.is_registered
    rig.server.register_external_player.assert_not_called()
    assert rig.protocol.sendspin_bridge is None


async def test_two_raw_monotonic_clocks_are_one_clock(rig: _Rig) -> None:
    """The squeezelite provider and Sendspin each make their own clock of the same kind."""
    rig.server.clock = RawMonotonicClock()
    rig.bridge._clock = RawMonotonicClock()

    await rig.bridge.start()

    assert rig.bridge.is_registered


async def test_a_volume_from_sendspin_goes_to_the_protocol_itself(rig: _Rig) -> None:
    """The level passed the player controller already, so it is not sent through it again."""
    await rig.bridge.start()

    rig.role.set_player_volume(35)
    await _settle()

    rig.protocol.volume_set.assert_awaited_once_with(35)
    rig.mass.players.cmd_volume_set.assert_not_called()


async def test_a_volume_set_on_the_player_reaches_the_role(rig: _Rig) -> None:
    """A volume the player reports is adopted by the role, not sent back to the player."""
    await rig.bridge.start()
    rig.client.volume_level = 55

    rig.event(SlimEventType.PLAYER_UPDATED)
    await _settle()

    assert rig.role.get_player_volume() == 55
    rig.protocol.volume_set.assert_not_called()


async def test_a_group_mute_is_handed_back(rig: _Rig) -> None:
    """A mute of the Sendspin group reaches the role, which the player can not follow."""
    await rig.bridge.start()

    rig.role.set_player_mute(True)
    await _settle()

    assert rig.role.get_player_muted() is False
    rig.client.mute.assert_not_called()
    rig.client.power.assert_not_called()


# ---------------------------------------------------------------- session start


async def test_a_stream_start_requests_the_stream_from_the_player(rig: _Rig) -> None:
    """The player is given a PCM stream it does not start by itself, after a flush."""
    steps = MagicMock()
    steps.attach_mock(rig.protocol.release_native_playback, "release")
    steps.attach_mock(rig.client.play_url, "play_url")

    session = await rig.start_stream()

    assert [step[0] for step in steps.mock_calls] == ["release", "play_url"]
    rig.client.play_url.assert_awaited_once_with(
        url=f"{BASE_URL}{BRIDGE_STREAM_PATH}?player_id={PLAYER_ID}&sid={session.session_id}",
        mime_type="audio/pcm;rate=44100;bitrate=16;channels=2",
        send_flush=True,
        autostart=False,
        stream_threshold=STREAM_THRESHOLD_KB,
        output_threshold=OUTPUT_THRESHOLD,
    )
    assert rig.owns_stream()
    # the preroll has to take the player past its threshold
    assert PREROLL_FRAMES * FRAME_SIZE > 2 * STREAM_THRESHOLD_KB * 1024


async def test_the_stream_request_flushes_the_player_and_powers_it_on() -> None:
    """What the bridge counts on aioslimproto to send for the stream request it makes."""
    sent: list[tuple[bytes, bytes]] = []
    writer = MagicMock(drain=AsyncMock())
    writer.is_closing.return_value = False
    writer.write.side_effect = lambda packet: sent.append((packet[2:6], packet[6:]))
    # a player that just connected: aioslimproto has it powered off
    client = SlimClient(asyncio.StreamReader(), writer, MagicMock())
    try:
        await client.play_url(
            url=f"{BASE_URL}{BRIDGE_STREAM_PATH}?player_id={PLAYER_ID}&sid=0123456789abcdef",
            mime_type=MIME_TYPE,
            send_flush=True,
            autostart=False,
            stream_threshold=STREAM_THRESHOLD_KB,
            output_threshold=OUTPUT_THRESHOLD,
        )
    finally:
        client._reader_task.cancel()

    commands = [(command, data[:1]) for command, data in sent if command in (b"strm", b"aude")]
    assert commands == [(b"strm", b"f"), (b"strm", b"q"), (b"aude", b"\x01"), (b"strm", b"s")]
    assert client.powered
    start = sent[-1][1]
    # no autostart, 16 bit little endian stereo PCM at 44.1 kHz, and the thresholds
    assert start[1:2] == b"2"
    assert start[2:7] == b"p1321"
    assert start[7] == STREAM_THRESHOLD_KB
    assert start[12] == OUTPUT_THRESHOLD
    request = start[24:].decode()
    assert request.startswith(
        f"GET {BRIDGE_STREAM_PATH}?player_id={PLAYER_ID}&sid=0123456789abcdef HTTP/1.0\r\n"
    )


async def test_no_stream_for_a_player_that_is_not_available(rig: _Rig) -> None:
    """A player that is gone gets no stream."""
    rig.protocol.available = False

    rig.bridge._on_stream_start(MagicMock())
    rig.bridge._on_role_stream_start()
    await _settle()

    assert not rig.owns_stream()
    rig.client.play_url.assert_not_called()


async def test_the_role_starts_a_stream_that_came_without_a_request(rig: _Rig) -> None:
    """A stream start of the role alone begins a session, one after a request does not."""
    first = await rig.start_stream()
    rig.bridge._on_role_stream_start()
    assert rig.session is first

    rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, TEN_MS))
    rig.bridge._on_role_stream_start()
    await _settle()

    assert rig.session is not first
    assert first.ended
    assert rig.client.play_url.await_count == 2


async def test_the_stream_is_served_to_the_player_that_asks_for_the_session(rig: _Rig) -> None:
    """The player gets the preroll at once and its audio as far as it is allowed."""
    session = await rig.start_playing()
    session.claimed = session.preroll_sent = False
    response = _Response()

    serving = rig.serve(session, response)
    await _settle()

    assert response.prepare.await_count == 1
    preroll, audio = response.body[: PREROLL_FRAMES * 4], response.body[PREROLL_FRAMES * 4 :]
    assert _frames(bytes(preroll)) == [-1] * PREROLL_FRAMES
    assert _frames(bytes(audio)) == list(range(STREAM_LEAD_FRAMES - PREROLL_FRAMES))
    rig.bridge._on_stream_end()
    await serving


async def test_the_stream_has_the_pcm_format_in_its_content_type(rig: _Rig) -> None:
    """Players take the PCM format from the content type."""
    session = await rig.start_stream()
    request = MagicMock(method="GET", query={"sid": session.session_id})

    with patch("aiohttp.web.StreamResponse", return_value=_Response()) as response:
        serving = asyncio.create_task(rig.bridge.serve_stream(request))
        await _settle()
        rig.bridge._on_stream_end()
        await serving

    assert response.call_args.kwargs["headers"] == {"Content-Type": MIME_TYPE}


async def test_a_request_for_another_session_is_not_found(rig: _Rig) -> None:
    """Only the current session is served, and only once."""
    session = await rig.start_stream()
    stale = MagicMock(method="GET", query={"sid": "0123456789abcdef"})
    current = MagicMock(method="GET", query={"sid": session.session_id})

    with pytest.raises(web.HTTPNotFound):
        await rig.bridge.serve_stream(stale)
    with pytest.raises(web.HTTPNotFound):
        await rig.bridge.serve_stream(MagicMock(method="GET", query={}))

    serving = rig.serve(session, _Response())
    await _settle()
    with pytest.raises(web.HTTPNotFound):
        await rig.bridge.serve_stream(current)

    replaced_by = await rig.start_stream()
    await serving
    assert replaced_by is not session
    with pytest.raises(web.HTTPNotFound):
        await rig.bridge.serve_stream(current)


async def test_a_session_is_fetched_once_by_the_request_that_names_it() -> None:
    """The id in the query has to be that of the session, which is open and not fetched yet."""
    session = BridgeSession(CD_FORMAT, 0)
    request = MagicMock(query={"sid": session.session_id})

    assert session.is_fetched_by(request)
    assert not session.is_fetched_by(
        MagicMock(query={"sid": BridgeSession(CD_FORMAT, 0).session_id})
    )
    assert not session.is_fetched_by(MagicMock(query={}))
    session.claimed = True
    assert not session.is_fetched_by(request)
    session.claimed = False
    session.close()
    assert not session.is_fetched_by(request)


async def test_the_stream_of_a_session_that_ended_is_not_found(rig: _Rig) -> None:
    """A session that plays out has no stream to fetch anymore."""
    session = await rig.start_playing()
    session.claimed = False
    rig.bridge._on_stream_end()

    with pytest.raises(web.HTTPNotFound):
        await rig.bridge.serve_stream(MagicMock(method="GET", query={"sid": session.session_id}))


async def test_the_stream_is_a_plain_http_body(rig: _Rig) -> None:
    """Players read the body to its end: no chunked transfer encoding and no content length."""
    session = await rig.start_stream()
    app = web.Application()
    app.router.add_route("*", BRIDGE_STREAM_PATH, rig.bridge.serve_stream)
    async with TestServer(app) as server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        # the request aioslimproto lets the player send
        writer.write(
            f"GET {BRIDGE_STREAM_PATH}?player_id={PLAYER_ID}&sid={session.session_id} HTTP/1.0\r\n"
            "Host: 192.168.1.10:8097\r\nConnection: close\r\nAccept: */*\r\n"
            "Cache-Control: no-cache\r\nUser-Agent: VLC/3.0.9 LibVLC/3.0.9\r\n"
            "Range: bytes=0-\r\n\r\n".encode()
        )
        head = (await reader.readuntil(b"\r\n\r\n")).decode().lower()
        preroll = await reader.readexactly(PREROLL_FRAMES * FRAME_SIZE)
        rig.map_clock()
        rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, SECOND))
        rig.event(SlimEventType.PLAYER_BUFFER_READY)
        audio = await reader.readexactly(SECOND * FRAME_SIZE)
        rig.bridge._on_stream_end()
        rest = await reader.read()
        writer.close()

    assert " 200 " in head.splitlines()[0]
    assert f"content-type: {MIME_TYPE}" in head
    assert "transfer-encoding" not in head
    assert "content-length" not in head
    assert preroll == bytes(PREROLL_FRAMES * FRAME_SIZE)
    assert _frames(audio) == list(range(SECOND))
    assert _frames(rest) == [-1] * (len(rest) // FRAME_SIZE)


async def test_the_first_chunk_is_heard_at_its_timestamp(rig: _Rig) -> None:
    """The stream starts one preroll before the first chunk, so that chunk is on time."""
    session = await rig.start_playing()

    assert session.start_us == rig.origin_us - PREROLL_US
    assert rig.session.loop is not None
    # the preroll is frame 0 and on of the stream
    assert rig.session.loop.frames_sent == PREROLL_FRAMES
    assert _frames(rig.bridge._next_block(session))[:3] == [0, 1, 2]


@pytest.mark.parametrize(("sync_adjust", "shift_us"), [(0, 0), (100, -100_000), (-80, 80_000)])
async def test_a_positive_sync_delay_plays_earlier(sync_adjust: int, shift_us: int) -> None:
    """The setting is the time the player needs to be heard, as for its own sync groups."""
    rig = _Rig(sync_adjust=sync_adjust)
    try:
        session = await rig.start_playing()

        assert session.start_us == rig.origin_us + shift_us - PREROLL_US
        rig.client.unpause_at.assert_awaited_once_with(rig.jiffies(session.start_us))
        assert _frames(rig.bridge._next_block(session))[0] == 0
    finally:
        await rig.close()


# ---------------------------------------------------------------- format


@pytest.mark.parametrize(
    ("plays", "listed"),
    [
        (
            [(48000, 16), (44100, 16), (96000, 24), (44100, 24), (48000, 24), (96000, 16)],
            [(44100, 24), (44100, 16), (48000, 24), (48000, 16), (96000, 24), (96000, 16)],
        ),
        # a player without the CD sample rate plays its lowest one by default
        ([(96000, 24), (48000, 16), (48000, 24)], [(48000, 24), (48000, 16), (96000, 24)]),
        ([], [(44100, 16)]),
    ],
)
async def test_the_hello_lists_the_formats_of_the_player(
    rig: _Rig, plays: list[tuple[int, int]], listed: list[tuple[int, int]]
) -> None:
    """Sendspin picks from what the player plays, and the bridge plays the first by default."""
    rig.protocol.get_supported_sample_rates.return_value = plays

    await rig.bridge.start()

    hello = rig.server.register_external_player.call_args.args[0]
    formats = hello.player_support.supported_formats
    assert [(fmt.sample_rate, fmt.bit_depth) for fmt in formats] == listed
    assert {(fmt.codec, fmt.channels) for fmt in formats} == {(AudioCodec.PCM, 2)}
    requirements = rig.role.get_audio_requirements()
    assert requirements is not None
    assert (requirements.sample_rate, requirements.bit_depth) == listed[0]


async def test_a_stream_is_in_the_format_sendspin_prefers(rig: _Rig) -> None:
    """The player is asked for and served the format the role has when the stream starts."""
    await rig.start_hires()

    session = await rig.start_stream()

    mime_type = "audio/pcm;rate=48000;bitrate=24;channels=2"
    assert rig.client.play_url.await_args.kwargs["mime_type"] == mime_type
    assert (session.sample_rate, session.frame_size) == (48000, 6)
    response = _Response()
    request = MagicMock(method="GET", query={"sid": session.session_id})
    with patch("aiohttp.web.StreamResponse", return_value=response) as response_class:
        serving = asyncio.create_task(rig.bridge.serve_stream(request))
        await _settle()
        rig.bridge._on_stream_end()
        await serving
    assert response_class.call_args.kwargs["headers"] == {"Content-Type": mime_type}
    # the preroll is a quarter of a second of that format
    assert bytes(response.body) == bytes(12000 * 6)
    assert bytes(response.body) == session.preroll


@pytest.mark.parametrize(
    ("sample_rate", "bit_depth", "codec"),
    [
        (44100, 16, b"p1321"),
        (44100, 24, b"p2321"),
        (48000, 16, b"p1421"),
        (48000, 24, b"p2421"),
        (88200, 24, b"p2:21"),
        (96000, 24, b"p2921"),
        (176400, 24, b"p2;21"),
        (192000, 24, b"p2<21"),
    ],
)
async def test_the_stream_request_tells_the_player_the_format(
    sample_rate: int, bit_depth: int, codec: bytes
) -> None:
    """What aioslimproto makes of the format of a session: little endian stereo PCM."""
    session = BridgeSession(
        AudioFormat(
            content_type=ContentType.from_bit_depth(bit_depth),
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=2,
        ),
        0,
    )
    sent: list[tuple[bytes, bytes]] = []
    writer = MagicMock(drain=AsyncMock())
    writer.is_closing.return_value = False
    writer.write.side_effect = lambda packet: sent.append((packet[2:6], packet[6:]))
    client = SlimClient(asyncio.StreamReader(), writer, MagicMock())
    try:
        await client.play_url(
            url=f"{BASE_URL}{BRIDGE_STREAM_PATH}?player_id={PLAYER_ID}&sid={session.session_id}",
            mime_type=session.mime_type,
            autostart=False,
        )
    finally:
        client._reader_task.cancel()

    command, start = sent[-1]
    assert command == b"strm"
    assert start[:1] == b"s"
    assert start[2:7] == codec
    assert session.frame_size == 2 * bit_depth // 8
    assert len(session.preroll) == sample_rate // 4 * session.frame_size


async def test_24_bit_audio_reaches_the_player_in_3_bytes_per_sample(rig: _Rig) -> None:
    """Sendspin delivers a 24-bit sample in 32 bits, of which a slimproto player takes 24."""
    await rig.start_hires()

    session = await rig.start_playing(frames=4 * 48000, chunk=_chunk_24)

    # on the timeline as any stream: its first frame a preroll after its start
    assert session.start_us == rig.origin_us - PREROLL_US
    assert session.loop is not None
    assert session.loop.frames_sent == 12000
    # the lead of a second and a half in frames of this stream, less the preroll
    lead = rig.drain(session)
    assert _frames_24(lead) == list(range(72000 - 12000))
    # and a second more for every second that it plays
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)
    assert _frames_24(rig.drain(session)) == list(range(60000, 108000))


async def test_corrections_are_frames_of_the_format_of_the_stream(rig: _Rig) -> None:
    """The sync loop counts in frames of the stream: a start error is that many of them."""
    await rig.start_hires()
    session = await rig.start_playing(frames=4 * 48000, chunk=_chunk_24)
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)

    rig.play(session, 1.5, late_ms=50)

    assert rig.manager.get_start_offset_us(PLAYER_ID) == 49_500
    assert session.loop is not None
    session.loop.process(bytes(48000 * 6))
    assert session.loop.frames_consumed - session.loop.frames_sent == round(49.5 * 48)
    assert session.loop.frames_sent == 12000 + 48000 - round(49.5 * 48)


def test_a_hole_is_silence_in_the_format_of_the_stream() -> None:
    """Chunks are placed by the sample rate of their stream, in frames of its size."""
    hires = AudioFormat(
        content_type=ContentType.PCM_S24LE, sample_rate=96000, bit_depth=24, channels=2
    )
    session = BridgeSession(hires, 0)
    session.start_us = EPOCH_US
    frame = bytes(range(1, 7))
    for first in (0, 960):
        session.add(
            AudioChunk(
                data=frame * 480,
                timestamp_us=EPOCH_US + first * 1_000_000 // 96000,
                duration_us=5000,
                byte_count=6 * 480,
            )
        )

    assert session.frames(10_000) == 960
    assert session.take(0, 9999) == frame * 480 + bytes(6 * 480) + frame * 480


async def test_a_stream_keeps_its_format_when_it_is_restarted(rig: _Rig) -> None:
    """A restart carries on with the chunks of the stream, which came in its format."""
    await rig.start_hires()
    first = await rig.start_playing(frames=4 * 48000, chunk=_chunk_24)
    # Sendspin prefers another format meanwhile, for the stream it starts next
    assert rig.role.set_preferred_format(None)

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    restarted = rig.session
    assert restarted is not first
    assert restarted.audio_format is first.audio_format
    assert restarted.chunks
    assert rig.client.play_url.await_args.kwargs["mime_type"] == first.mime_type


async def test_another_format_starts_the_stream_again(rig: _Rig) -> None:
    """Sendspin starts the stream of a role anew for a format that changed while it plays."""
    await rig.start_hires()
    rig.sendspin_client.group.on_role_format_changed.reset_mock()
    first = await rig.start_stream()
    rig.bridge._on_audio_chunk(_chunk_24(rig.now_us + 2_000_000, 0, 4800))

    assert rig.role.set_preferred_format(None)
    rig.sendspin_client.group.on_role_format_changed.assert_called_once_with(rig.role)
    # which Sendspin answers with a new start of the stream of the role
    rig.role.on_stream_start()
    await _settle()

    assert rig.session is not first
    assert first.ended
    assert (first.sample_rate, first.frame_size) == (48000, 6)
    assert (rig.session.sample_rate, rig.session.frame_size) == (44100, 6)
    assert rig.client.play_url.await_args.kwargs["mime_type"] == (
        "audio/pcm;rate=44100;bitrate=24;channels=2"
    )


# ---------------------------------------------------------------- chunk placement


def _session(origin_us: int) -> BridgeSession:
    """Return a session whose timeline starts at the first frame of its source."""
    session = BridgeSession(CD_FORMAT, 0)
    session.start_us = origin_us
    return session


def test_chunks_that_follow_each_other_are_played_as_they_are() -> None:
    """Contiguous chunks give contiguous audio, in whatever pieces it is asked for."""
    session = _session(EPOCH_US)
    for first in range(0, 5 * TEN_MS, TEN_MS):
        session.add(_chunk(EPOCH_US, first, TEN_MS))

    taken = session.take(0, 700) + session.take(700, 3 * TEN_MS) + session.take(2023, 9999)

    assert _frames(taken) == list(range(5 * TEN_MS))
    assert not session.chunks
    assert session.pending_us == 0


def test_a_hole_in_the_timeline_is_silence() -> None:
    """A chunk that comes later than the one before it ended is played at its own time."""
    session = _session(EPOCH_US)
    session.add(_chunk(EPOCH_US, 0, TEN_MS))
    session.add(_chunk(EPOCH_US, 3 * TEN_MS, TEN_MS))

    taken = _frames(session.take(0, 9999))

    assert taken == [
        *range(TEN_MS),
        *[-1] * (2 * TEN_MS),
        *range(3 * TEN_MS, 4 * TEN_MS),
    ]


def test_a_long_hole_is_filled_as_far_as_is_asked() -> None:
    """Silence is handed out like audio: no more than what is asked for at a time."""
    session = _session(EPOCH_US)
    session.add(_chunk(EPOCH_US, 40 * SECOND, TEN_MS))

    assert session.take(0, 1000) == bytes(1000 * FRAME_SIZE)
    assert len(session.take(1000, 3 * SECOND)) == 3 * SECOND * FRAME_SIZE
    assert len(session.chunks) == 1


def test_an_overlap_is_trimmed() -> None:
    """A chunk that starts before the one before it ended does not repeat that audio."""
    session = _session(EPOCH_US)
    session.add(_chunk(EPOCH_US, 0, 2 * TEN_MS))
    session.add(_chunk(EPOCH_US, TEN_MS, 2 * TEN_MS))

    assert _frames(session.take(0, 9999)) == list(range(3 * TEN_MS))


def test_audio_before_the_cursor_is_dropped() -> None:
    """What is due before the stream reaches it is left out, a chunk that straddles is cut."""
    session = _session(EPOCH_US)
    session.add(_chunk(EPOCH_US, 0, TEN_MS))
    session.add(_chunk(EPOCH_US, TEN_MS, TEN_MS))
    session.add(_chunk(EPOCH_US, 2 * TEN_MS, TEN_MS))

    cursor = TEN_MS + 100
    assert _frames(session.take(cursor, 9999)) == list(range(cursor, 3 * TEN_MS))


@pytest.mark.parametrize("shift_us", [-20, 20])
def test_a_timestamp_one_frame_off_is_no_jump(shift_us: int) -> None:
    """Timestamps are whole microseconds: a chunk a frame off its place is played as it is."""
    session = _session(EPOCH_US)
    session.add(_chunk(EPOCH_US, 0, TEN_MS))
    session.add(_chunk(EPOCH_US, TEN_MS, TEN_MS, shift_us=shift_us))

    assert _frames(session.take(0, 9999)) == list(range(2 * TEN_MS))


def test_the_audio_that_waits_is_capped() -> None:
    """Beyond the caps the oldest chunks go, and a hole takes their place."""
    session = _session(EPOCH_US)
    for index in range(MAX_PENDING_CHUNKS + 10):
        session.add(_chunk(EPOCH_US, index * TEN_MS, TEN_MS))
    assert len(session.chunks) == MAX_PENDING_CHUNKS
    assert _frames(session.take(0, 10 * TEN_MS + 2)) == [*[-1] * (10 * TEN_MS), 4410, 4411]

    session = _session(EPOCH_US)
    seconds = MAX_PENDING_AUDIO_US // 1_000_000
    for index in range(seconds + 5):
        session.add(_chunk(EPOCH_US, index * SECOND, SECOND))
    assert len(session.chunks) == seconds
    assert session.pending_us == MAX_PENDING_AUDIO_US


async def test_a_late_join_backlog_is_paced(rig: _Rig) -> None:
    """Sendspin hands a late joiner up to 30 seconds at once: the player gets a lead of it."""
    session = await rig.start_playing(frames=SECOND)
    for second in range(1, 30):
        rig.bridge._on_audio_chunk(_chunk(rig.origin_us, second * SECOND, SECOND))

    block = rig.bridge._next_block(session)
    assert len(block) == MAX_BLOCK_FRAMES * FRAME_SIZE
    sent = block + rig.drain(session)

    # the preroll counts: with it the player holds the lead, and no more
    assert len(sent) == (STREAM_LEAD_FRAMES - PREROLL_FRAMES) * FRAME_SIZE
    assert len(session.chunks) == 29
    # a second into the stream it is given a second more
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)
    assert len(rig.drain(session)) == SECOND * FRAME_SIZE


# ---------------------------------------------------------------- pacing


async def test_before_its_start_the_player_gets_the_lead(rig: _Rig) -> None:
    """Up to the start the stream is filled to the lead once, whatever the time."""
    session = await rig.start_playing()
    assert session.loop is not None

    assert len(rig.drain(session)) == (STREAM_LEAD_FRAMES - PREROLL_FRAMES) * FRAME_SIZE
    assert session.loop.frames_sent == STREAM_LEAD_FRAMES
    rig.clock.advance_us(session.start_us - rig.now_us)
    assert rig.drain(session) == b""


async def test_after_its_start_the_player_is_kept_the_lead_ahead(rig: _Rig) -> None:
    """The stream follows the timeline: what was played is replaced."""
    session = await rig.start_playing(frames=10 * SECOND)
    assert session.loop is not None
    rig.drain(session)

    rig.clock.advance_us(session.start_us - rig.now_us + 20_000)
    assert len(rig.drain(session)) == 882 * FRAME_SIZE
    rig.clock.advance_us(3_000_000)
    rig.drain(session)

    assert session.loop.frames_sent == round(3.02 * SECOND) + STREAM_LEAD_FRAMES


async def test_corrections_count_in_what_the_player_is_allowed(rig: _Rig) -> None:
    """Frames the loop dropped were not sent: the stream is that much less far along."""
    session = await rig.start_playing(frames=40 * SECOND)
    loop = session.loop
    assert loop is not None
    rig.drain(session)
    # a player that is 3 ms late is sent less than the timeline: frames are dropped
    for _ in range(40):
        rig.play(session, 0.25, late_ms=3)
        rig.drain(session)

    dropped = loop.frames_consumed - loop.frames_sent
    assert loop.rate_ppm > 0
    assert dropped > 0
    played = (rig.now_us - session.start_us) * BRIDGE_SAMPLE_RATE // 1_000_000
    assert loop.frames_sent == played + STREAM_LEAD_FRAMES - dropped


async def test_silence_keeps_a_player_fed_that_sendspin_has_nothing_for(rig: _Rig) -> None:
    """Without audio the stream is kept a little ahead with silence, on the timeline."""
    session = await rig.start_playing(frames=SECOND)
    loop = session.loop
    assert loop is not None
    assert _frames(rig.drain(session)) == list(range(SECOND))

    # everything was sent: nothing more while the player still holds enough
    rig.clock.advance_us(session.start_us - rig.now_us + 700_000)
    assert rig.drain(session) == b""
    rig.clock.advance_us(300_000)
    starved = rig.drain(session)

    assert _frames(starved) == [-1] * (len(starved) // FRAME_SIZE)
    assert loop.frames_consumed == SECOND + STARVED_LEAD_FRAMES
    # audio that arrives late is played from where the stream is by then
    late_from = loop.frames_consumed - PREROLL_FRAMES
    rig.bridge._on_audio_chunk(_chunk(rig.origin_us, SECOND, SECOND))
    assert _frames(rig.drain(session))[0] == late_from


# ---------------------------------------------------------------- start


async def test_the_player_is_told_when_to_start_on_its_own_clock(rig: _Rig) -> None:
    """The start instant goes to the player as a reading of its clock."""
    session = await rig.start_playing()

    assert session.started
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies(rig.origin_us - PREROLL_US))
    rig.client.unpause_at.assert_awaited_once_with(JIFFIES_AT_EPOCH + 1750)


async def test_a_start_is_at_least_300_ms_ahead(rig: _Rig) -> None:
    """A first chunk that is due too soon starts late, without the audio before the start."""
    session = await rig.start_playing(first_chunk_ahead_us=100_000)

    assert MIN_START_AHEAD_US == 300_000
    assert session.start_us == rig.now_us + MIN_START_AHEAD_US
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies() + 300)
    # the stream is 450 ms late for the first chunk: that much of it is left out
    skipped = (session.start_us + PREROLL_US - rig.origin_us) * BRIDGE_SAMPLE_RATE // 1_000_000
    assert skipped == 19845
    assert _frames(rig.bridge._next_block(session))[:2] == [skipped, skipped + 1]


async def test_a_start_that_is_past_starts_as_soon_as_it_can(rig: _Rig) -> None:
    """A first chunk that is due in the past still starts 300 ms from now."""
    session = await rig.start_playing(first_chunk_ahead_us=-1_000_000)

    assert session.start_us == rig.now_us + MIN_START_AHEAD_US
    assert _frames(rig.bridge._next_block(session))[0] == round(1.55 * SECOND)


async def test_a_start_is_at_most_5_seconds_ahead(rig: _Rig) -> None:
    """A Squeezebox Radio starts at once when told too far ahead: it is told later instead."""
    assert MAX_START_AHEAD_US < 5_000_000
    real_sleep = asyncio.sleep
    slept: list[float] = []
    told_at: list[int] = []

    async def _sleep(delay: float) -> None:
        if delay >= 60:
            # the status poller, which is not what this is about
            await asyncio.Event().wait()
        if delay > 1:
            slept.append(delay)
            rig.clock.advance_us(round(delay * 1_000_000))
        await real_sleep(0)

    rig.client.unpause_at.side_effect = lambda _jiffies: told_at.append(rig.now_us)
    with patch("asyncio.sleep", new=_sleep):
        session = await rig.start_playing(first_chunk_ahead_us=8_250_000)
        for _ in range(10):
            await real_sleep(0)

    assert slept == [pytest.approx(3.5)]
    assert told_at == [session.start_us - MAX_START_AHEAD_US]
    rig.client.unpause_at.assert_awaited_once_with(JIFFIES_AT_EPOCH + 8000)


async def test_a_learned_start_offset_tells_the_player_earlier(rig: _Rig) -> None:
    """A player that starts late is told to start that much earlier."""
    rig.manager.learn_start_offset(PLAYER_ID, 420.0)

    session = await rig.start_playing()

    assert session.start_us == rig.origin_us - PREROLL_US
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies(session.start_us) - 420)


async def test_a_learned_start_offset_counts_in_the_earliest_start(rig: _Rig) -> None:
    """The command has to be 300 ms ahead, so the start itself is the offset later."""
    rig.manager.learn_start_offset(PLAYER_ID, 420.0)

    session = await rig.start_playing(first_chunk_ahead_us=100_000)

    assert session.start_us == rig.now_us + MIN_START_AHEAD_US + 420_000
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies() + 300)


async def test_the_start_waits_for_the_clock_of_the_player(rig: _Rig) -> None:
    """Without a mapping of its clock the player can not be told when to start."""
    session = await rig.start_stream()
    rig.fetched(session)
    rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, SECOND))
    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    await _settle()
    assert not session.start_decided.is_set()
    rig.client.unpause_at.assert_not_called()

    # the third reply to a status request maps the clock
    for token in range(3):
        rig.bridge._pending_status[STATUS_TOKEN_BIT | 9000 + token] = rig.now_us
        rig.bridge.handle_slim_event(rig.status(token=STATUS_TOKEN_BIT | 9000 + token))
        rig.clock.advance_us(50_000)
    await _settle()

    assert session.start_decided.is_set()
    rig.client.unpause_at.assert_awaited_once()


async def test_a_clock_mapping_that_was_not_kept_up_is_learned_again(rig: _Rig) -> None:
    """The clocks drift apart while the player is not asked: an old mapping is not used."""
    await rig.start_stream()
    for token in (101, 102, 103):
        rig.bridge._pending_status[STATUS_TOKEN_BIT | token] = rig.now_us
        rig.bridge.handle_slim_event(rig.status(token=STATUS_TOKEN_BIT | token))
        rig.clock.advance_us(50_000)
    known = rig.bridge._player_clock
    assert known.to_jiffies(rig.now_us) is not None

    rig.clock.advance_us(CLOCK_MAX_AGE_US - 150_000)
    await rig.start_stream()
    assert rig.bridge._player_clock is known

    rig.clock.advance_us(CLOCK_MAX_AGE_US + 1)
    await rig.start_stream()
    assert rig.bridge._player_clock is not known
    assert rig.bridge._player_clock.to_jiffies(rig.now_us) is None


async def test_the_start_waits_for_the_first_chunk(rig: _Rig) -> None:
    """A player that is ready before Sendspin delivered anything starts with the first chunk."""
    session = await rig.start_stream()
    rig.map_clock()
    rig.fetched(session)
    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    await _settle()
    assert not session.start_decided.is_set()

    rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, SECOND))
    await _settle()

    rig.client.unpause_at.assert_awaited_once()


async def test_a_buffer_ready_of_the_stream_before_is_ignored(rig: _Rig) -> None:
    """Until the player fetched this stream, its buffer ready is about another one."""
    session = await rig.start_stream()
    rig.map_clock()
    rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, SECOND))

    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    await _settle()

    assert not session.buffer_ready
    rig.client.unpause_at.assert_not_called()


async def test_a_player_that_does_not_get_ready_has_its_stream_restarted(rig: _Rig) -> None:
    """A stream that the player never fetches is requested again, once."""
    with patch(f"{MODULE}.START_TIMEOUT_SECONDS", 0.2):
        first = await rig.start_stream()
        await _wait_for(lambda: rig.client.play_url.await_count == 2)

        assert rig.session is not first
        assert first.ended
        rig.client.stop.assert_not_called()
        # again this soon: the player is given up on
        await _wait_for(lambda: not rig.owns_stream())

    assert rig.client.play_url.await_count == 2
    rig.client.stop.assert_awaited_once()


# ---------------------------------------------------------------- polling and reports


async def test_the_player_is_asked_for_its_status_with_tokens_of_its_own(rig: _Rig) -> None:
    """The tokens can not be those of the heartbeat of aioslimproto, which counts up from 1."""
    with patch(f"{MODULE}.STATUS_POLL_INTERVAL_UNMAPPED", 0):
        await rig.start_stream()
        await _settle()

    tokens = [request.args[0] for request in rig.client.request_status.await_args_list]
    assert len(tokens) >= 3
    assert all(token & STATUS_TOKEN_BIT and token < 1 << 32 for token in tokens)
    assert tokens == sorted(set(tokens))
    assert list(rig.bridge._pending_status) == tokens[-MAX_PENDING_STATUS:]
    assert all(sent_us == EPOCH_US for sent_us in rig.bridge._pending_status.values())


async def test_the_status_is_polled_while_a_session_lasts(rig: _Rig) -> None:
    """Polling starts with the stream and stops when it ends."""
    rig.client.request_status.assert_not_called()
    with patch(f"{MODULE}.STATUS_POLL_INTERVAL_UNMAPPED", 0):
        session = await rig.start_stream()
        tasks = list(session.tasks)
        await _settle()
        assert rig.client.request_status.await_count > 1

        rig.bridge._on_stream_end()
        await _settle()
        polled = rig.client.request_status.await_count
        await _settle()

    assert rig.client.request_status.await_count == polled
    assert len(tasks) == 2
    assert all(task.done() for task in tasks)


async def test_a_reply_to_a_request_maps_the_clock_of_the_player(rig: _Rig) -> None:
    """A status with the token of a request is a sample of the player's clock."""
    await rig.start_stream()
    clock = rig.bridge._player_clock
    for token in (101, 102, 103):
        rig.bridge._pending_status[STATUS_TOKEN_BIT | token] = rig.now_us
        rig.clock.advance_us(2000)
        rig.bridge.handle_slim_event(rig.status(token=STATUS_TOKEN_BIT | token))
        rig.clock.advance_us(98_000)

    # the reply arrived 2 ms after its request: the reading is from the middle of that
    assert clock.to_server_us(JIFFIES_AT_EPOCH + 2) == EPOCH_US + 1000
    assert not rig.bridge._pending_status


async def test_a_status_that_was_not_asked_for_is_no_clock_sample(rig: _Rig) -> None:
    """Only a reply has a known round trip: the heartbeat and an unasked status have none."""
    await rig.start_stream()
    asked = list(rig.bridge._pending_status)
    assert asked == [STATUS_TOKEN_BIT | 1]

    for token in (0, 1, 2, 3, STATUS_TOKEN_BIT | 8):
        rig.bridge.handle_slim_event(rig.status(token=token))

    assert rig.bridge._player_clock.to_jiffies(rig.now_us) is None
    assert list(rig.bridge._pending_status) == asked


async def test_requests_without_a_reply_are_forgotten(rig: _Rig) -> None:
    """Replies come in order, so a reply closes the requests before it; the rest is capped."""
    await rig.start_stream()
    rig.bridge._pending_status.clear()
    for token in (1, 2, 3, 4):
        rig.bridge._pending_status[STATUS_TOKEN_BIT | token] = rig.now_us

    rig.bridge.handle_slim_event(rig.status(token=STATUS_TOKEN_BIT | 3))
    assert list(rig.bridge._pending_status) == [STATUS_TOKEN_BIT | 4]
    # the reply to a request that was given up on is no sample
    rig.bridge.handle_slim_event(rig.status(token=STATUS_TOKEN_BIT | 1))
    rig.bridge.handle_slim_event(rig.status(token=STATUS_TOKEN_BIT | 2))

    assert rig.bridge._player_clock.to_jiffies(rig.now_us) is None


async def test_reports_reach_the_sync_loop(rig: _Rig) -> None:
    """Every status of the player during a session tells the loop where it is."""
    session = await rig.start_playing()
    assert session.loop is not None
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)

    rig.play(session, 1.5, late_ms=3)

    # elapsed is truncated to whole milliseconds, which the loop takes for half of one
    assert session.loop.error_ms == pytest.approx(2.5)
    assert session.loop.start_error_ms == pytest.approx(2.5)
    assert session.loop.rate_ppm == pytest.approx(250.0)


async def test_corrections_of_the_loop_are_in_the_stream(rig: _Rig) -> None:
    """A late player is sent audio with frames taken out."""
    session = await rig.start_playing(frames=20 * SECOND)
    rig.drain(session)
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)
    before = rig.drain(session)
    assert _frames(before) == list(range(_frames(before)[0], _frames(before)[-1] + 1))

    rig.play(session, 1.5, late_ms=3)
    corrected = _frames(rig.drain(session))

    steps = [second - first for first, second in pairwise(corrected)]
    assert steps.count(2) >= 1
    assert set(steps) == {1, 2}


async def test_a_start_error_is_learned_for_the_next_start(rig: _Rig) -> None:
    """How late a player starts is taken off the instant it is told the next time."""
    session = await rig.start_playing()
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)

    rig.play(session, 1.5, late_ms=50)

    assert rig.manager.get_start_offset_us(PLAYER_ID) == 49_500
    # corrected in this session by one jump
    assert session.loop is not None
    session.loop.process(bytes(SECOND * FRAME_SIZE))
    assert session.loop.frames_consumed - session.loop.frames_sent == round(49.5 * 44.1)
    # the offset is that of the player, not of a bridge
    rig.client.unpause_at.reset_mock()
    later = SendspinSqueezeliteBridge(rig.provider, rig.protocol, rig.server)
    rig.bridge, old = later, rig.bridge
    await old.stop()
    session = await rig.start_playing()
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies(session.start_us - 49_500))


async def test_a_start_offset_settles_on_what_the_player_does(rig: _Rig) -> None:
    """The first start error is taken whole, the next ones for half."""
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 0
    assert rig.manager.learn_start_offset(PLAYER_ID, 420.0) == 420_000
    assert rig.manager.learn_start_offset(PLAYER_ID, 10.0) == 425_000
    assert rig.manager.learn_start_offset(PLAYER_ID, -6.0) == 422_000
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 422_000
    assert rig.manager.get_start_offset_us("other") == 0


async def test_a_start_offset_is_kept_in_the_cache_when_it_changes(rig: _Rig) -> None:
    """What was learned is stored for the next run of the server, once per start that moved it."""
    rig.manager.learn_start_offset(PLAYER_ID, 420.0)
    await _settle()

    rig.mass.cache.set.assert_awaited_once_with(
        key=PLAYER_ID,
        data=420_000,
        provider="squeezelite",
        category=CACHE_CATEGORY_START_OFFSET,
    )
    rig.manager.learn_start_offset(PLAYER_ID, 0.0)
    await _settle()
    assert rig.mass.cache.set.await_count == 1
    rig.manager.learn_start_offset(PLAYER_ID, 10.0)
    await _settle()
    assert rig.mass.cache.set.await_count == 2
    assert rig.mass.cache.set.await_args.kwargs["data"] == 425_000


async def test_the_start_offset_of_a_player_is_restored_before_its_bridge(rig: _Rig) -> None:
    """A bridge tells its player earlier from its first start on, by what the cache holds."""
    rig.mass.cache.get.return_value = 420_000
    steps = MagicMock()
    steps.attach_mock(rig.mass.cache.get, "restore")
    steps.attach_mock(rig.server.register_external_player, "register")

    await rig.manager.evaluate_bridge(rig.protocol)

    assert [step[0] for step in steps.mock_calls][:2] == ["restore", "register"]
    rig.mass.cache.get.assert_awaited_once_with(
        key=PLAYER_ID, provider="squeezelite", category=CACHE_CATEGORY_START_OFFSET
    )
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 420_000
    session = await rig.start_playing()
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies(session.start_us - 420_000))
    # what the player does from here on moves it by halves, as an offset that was learned
    assert rig.manager.learn_start_offset(PLAYER_ID, 10.0) == 425_000


async def test_a_start_offset_that_is_known_is_not_restored(rig: _Rig) -> None:
    """The cache is read once for a player: what was learned since is newer."""
    rig.mass.cache.get.return_value = 420_000
    rig.manager.learn_start_offset(PLAYER_ID, 30.0)

    await rig.manager.evaluate_bridge(rig.protocol)

    rig.mass.cache.get.assert_not_awaited()
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 30_000


@pytest.mark.parametrize("cached", [None, "420000", 420.5, {"offset": 420_000}])
async def test_a_cache_without_a_start_offset_leaves_none(rig: _Rig, cached: Any) -> None:
    """Without a usable value the player starts as one that was never measured."""
    rig.mass.cache.get.return_value = cached

    await rig.manager.evaluate_bridge(rig.protocol)

    assert rig.manager.get_bridge(PLAYER_ID) is not None
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 0
    # so its first start error is taken whole
    assert rig.manager.learn_start_offset(PLAYER_ID, 50.0) == 50_000


async def test_a_start_that_is_far_off_restarts_the_stream(rig: _Rig) -> None:
    """A start error the loop does not correct makes the session unusable."""
    session = await rig.start_playing()
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000 + 3_000_000)

    rig.play(session, 1.5, late_ms=int(MAX_START_JUMP_MS) + 500)
    await _settle()

    assert rig.session is not session
    assert session.ended
    assert rig.client.play_url.await_count == 2
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 0


@pytest.mark.parametrize("late_ms", [3000, -3000])
async def test_a_player_that_stays_off_its_timeline_has_its_stream_restarted(
    rig: _Rig, late_ms: int
) -> None:
    """A player that stalled without running dry stays off its timeline: it gets a new stream."""
    session = await rig.start_playing()
    rig.clock.advance_us(session.start_us - rig.now_us + 4_000_000)
    rig.play(session, 10)
    await _settle()
    assert rig.session is session

    # four seconds of reports that are all far off
    rig.play(session, 4.25, late_ms=late_ms)
    await _settle()
    assert rig.session is session

    rig.play(session, 0.25, late_ms=late_ms)
    await _settle()

    assert rig.session is not session
    assert session.ended
    assert rig.client.play_url.await_count == 2


async def test_a_start_that_is_corrected_in_a_jump_is_no_reason_to_restart(rig: _Rig) -> None:
    """Until the jump of a late start is heard, the player reports the start error."""
    session = await rig.start_playing()
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)

    for _ in range(40):
        # the writer runs on: the jump goes out, and is heard once the lead has played
        rig.drain(session)
        rig.play(session, 0.25, late_ms=500)
    await _settle()

    assert rig.session is session
    assert rig.client.play_url.await_count == 1
    assert session.loop is not None
    assert session.loop.error_ms == pytest.approx(0.0, abs=1.0)


async def test_reports_that_are_off_now_and_then_are_no_reason_to_restart(rig: _Rig) -> None:
    """Only a player that is off its timeline in every report for seconds is restarted."""
    session = await rig.start_playing()
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)
    rig.play(session, 2)

    # two reports that are a second off, five seconds apart
    for _ in range(2):
        rig.play(session, 0.25, late_ms=1000)
        rig.play(session, 4.75)
    await _settle()

    assert rig.session is session
    assert rig.client.play_url.await_count == 1


# ---------------------------------------------------------------- end and stop


async def test_a_stream_that_ends_lets_the_player_play_out(rig: _Rig) -> None:
    """The stream ends and the player stops by itself: it is not told to stop."""
    session = await rig.start_playing()
    response = _Response()
    session.claimed = False
    serving = rig.serve(session, response)
    await _settle()

    rig.bridge._on_stream_end()
    await serving
    await _settle()

    assert session.ended
    assert not session.chunks
    rig.client.stop.assert_not_called()
    # the player is still the bridge's until it reports that it stopped
    assert rig.owns_stream()
    rig.client.state = SlimPlayerState.PLAYING
    rig.event(SlimEventType.PLAYER_UPDATED)
    assert rig.owns_stream()
    rig.client.state = SlimPlayerState.STOPPED
    rig.event(SlimEventType.PLAYER_UPDATED)
    assert not rig.owns_stream()
    rig.client.stop.assert_not_called()


async def test_an_explicit_stop_stops_the_player_at_once(rig: _Rig) -> None:
    """A stop the user asked for does not wait for the player to play out."""
    await rig.start_playing()
    rig.bridge._on_stream_end()

    rig.bridge._on_explicit_stop()
    rig.bridge._on_explicit_stop()
    await _settle()

    rig.client.stop.assert_awaited_once_with()
    assert not rig.owns_stream()
    # never by a power or mute command, which switch the output of the player off
    rig.client.power.assert_not_called()
    rig.client.mute.assert_not_called()


async def test_an_explicit_stop_leaves_a_new_stream_alone(rig: _Rig) -> None:
    """A stop that arrives after a new stream took the bridge over is not for that stream."""
    await rig.start_playing()
    rig.bridge._on_stream_end()
    session = await rig.start_stream()

    rig.bridge._on_explicit_stop()
    await _settle()

    rig.client.stop.assert_not_called()
    assert rig.session is session
    assert not session.ended


async def test_a_stream_that_ends_before_its_start_stops_the_player(rig: _Rig) -> None:
    """A player that was not told to start would hold its stream forever."""
    session = await rig.start_stream()

    rig.bridge._on_stream_end()
    await _settle()

    assert session.ended
    assert not rig.owns_stream()
    rig.client.stop.assert_awaited_once_with()
    rig.client.unpause_at.assert_not_called()


async def test_a_stream_that_ends_before_it_was_requested_leaves_the_player_alone(
    rig: _Rig,
) -> None:
    """A player that was never asked for the stream has nothing of it to stop."""
    rig.bridge._on_stream_start(MagicMock())
    rig.bridge._on_stream_end()
    await _settle()

    assert not rig.owns_stream()
    rig.client.play_url.assert_not_called()
    rig.client.stop.assert_not_called()


async def test_a_new_stream_replaces_the_session(rig: _Rig) -> None:
    """A seek or a new track is a new stream: the player is flushed by its request."""
    first = await rig.start_playing()
    first.claimed = first.preroll_sent = False
    serving = rig.serve(first, _Response())
    await _settle()
    tasks = list(first.tasks)
    first_url = rig.client.play_url.await_args.kwargs["url"]

    second = await rig.start_stream()
    await serving

    assert first.ended
    assert not first.chunks
    assert all(task.done() for task in tasks)
    assert second is not first
    assert second.first_audible_us is None
    assert rig.client.play_url.await_count == 2
    assert rig.client.play_url.await_args.kwargs["url"] != first_url
    assert rig.client.play_url.await_args.kwargs["send_flush"] is True
    rig.client.stop.assert_not_called()


async def test_chunks_of_a_stream_that_ended_are_dropped(rig: _Rig) -> None:
    """Nothing is kept for a player without a stream."""
    rig.bridge._on_audio_chunk(_chunk(rig.now_us, 0, TEN_MS))
    assert not rig.owns_stream()

    session = await rig.start_playing()
    rig.bridge._on_stream_end()
    rig.bridge._on_audio_chunk(_chunk(rig.now_us, 0, TEN_MS))

    assert not session.chunks


async def test_stopping_the_bridge_twice_ends_everything_once(rig: _Rig) -> None:
    """A bridge that is stopped leaves no task, client or reference behind."""
    await rig.bridge.start()
    session = await rig.start_playing()
    tasks = list(session.tasks)
    rig.bridge.return_to_group(MagicMock())
    rejoin = rig.bridge._rejoin_task
    assert rejoin is not None

    await rig.bridge.stop()
    await rig.bridge.stop()
    await _settle()

    assert session.ended
    assert all(task.done() for task in tasks)
    assert rejoin.cancelled()
    assert not rig.owns_stream()
    assert not rig.bridge.is_registered
    assert rig.protocol.sendspin_bridge is None
    rig.server.remove_client.assert_awaited_once_with(CLIENT_ID)
    rig.client.stop.assert_awaited_once_with()


async def test_the_protocol_takes_its_player_back(rig: _Rig) -> None:
    """A stop or a stream of the protocol itself ends the stream of the bridge and its session."""
    rig.bridge._sendspin_client = client = _grouped_client()
    assert rig.bridge.stop_streaming() is False
    session = await rig.start_playing()

    assert rig.bridge.stop_streaming() is True
    await _settle()

    assert session.ended
    assert not rig.owns_stream()
    client.quiesce_to_solo_stopped.assert_awaited_once()
    assert rig.bridge._rejoin_task is None
    # the player is the caller's to stop or to play on
    rig.client.stop.assert_not_called()
    assert rig.manager.stop_streaming("other") is False


# ---------------------------------------------------------------- failure paths


async def test_an_underrun_restarts_the_stream_on_the_timeline(rig: _Rig) -> None:
    """A player that ran dry is off the timeline: it gets a new stream of what is left."""
    first = await rig.start_playing(frames=SECOND)
    for second in range(1, 6):
        rig.bridge._on_audio_chunk(_chunk(rig.origin_us, second * SECOND, SECOND))
    rig.drain(first)
    rig.clock.advance_us(first.start_us - rig.now_us + 2_000_000)
    rig.drain(first)

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    second_session = rig.session
    assert second_session is not first
    assert first.ended
    assert rig.client.play_url.await_count == 2
    rig.client.stop.assert_not_called()
    # the audio that was not played out yet goes on in the new stream, at its own time:
    # the first stream was 3.25 seconds into its audio
    assert len(second_session.chunks) == 3
    rig.fetched(second_session)
    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    await _settle()
    assert second_session.start_us == rig.origin_us + 3_000_000 - PREROLL_US
    assert _frames(rig.bridge._next_block(second_session))[0] == 3 * SECOND


async def test_an_underrun_before_the_start_is_not_one_of_the_stream(rig: _Rig) -> None:
    """Until the stream has a start, an underrun is about what the player played before."""
    session = await rig.start_stream()

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert rig.session is session
    assert rig.client.play_url.await_count == 1


async def test_a_second_underrun_gives_up_and_rejoins_later(rig: _Rig) -> None:
    """A stream that fails again within 30 seconds takes the player out of the session."""
    client = rig.grouped()
    group = client.group
    await rig.start_playing()
    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()
    rig.fetched(rig.session)
    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    rig.clock.advance_us(RECOVERY_GUARD_US - 1)

    with patch(f"{MODULE}.REJOIN_ATTEMPT_DELAYS", (0,)):
        rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
        await _settle()

    assert not rig.owns_stream()
    assert rig.client.play_url.await_count == 2
    rig.client.stop.assert_awaited_once_with()
    client.quiesce_to_solo_stopped.assert_awaited_once()
    group.add_client.assert_awaited_once_with(client)


async def test_an_underrun_long_after_a_restart_restarts_again(rig: _Rig) -> None:
    """A restart that held for 30 seconds worked."""
    rig.bridge._sendspin_client = client = _grouped_client()
    await rig.start_playing()
    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()
    rig.fetched(rig.session)
    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    rig.clock.advance_us(RECOVERY_GUARD_US)

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert rig.owns_stream()
    assert rig.client.play_url.await_count == 3
    client.quiesce_to_solo_stopped.assert_not_called()


async def test_a_new_sendspin_stream_has_its_own_recovery_budget(rig: _Rig) -> None:
    """A failure on the stream before says nothing about this one."""
    await rig.start_playing()
    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    await rig.start_playing()
    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert rig.owns_stream()
    assert rig.client.play_url.await_count == 4


async def test_a_return_survives_the_stream_start_it_causes(rig: _Rig) -> None:
    """Joining a playing group starts a stream at once, which must not cancel the join itself."""
    client = rig.grouped()
    group = client.group

    async def _add_client(_client: Any) -> None:
        # the group asks the bridge for a stream before the join is done
        rig.bridge._on_stream_start(MagicMock())
        await asyncio.sleep(0)

    group.add_client = AsyncMock(side_effect=_add_client)

    with patch(f"{MODULE}.REJOIN_ATTEMPT_DELAYS", (0,)):
        await rig.bridge._leave_sendspin_session()
        await _settle()

    group.add_client.assert_awaited_once_with(client)
    assert rig.bridge._last_rejoin_us == rig.now_us


async def test_a_bridge_that_gave_up_right_after_its_return_stays_out(rig: _Rig) -> None:
    """A player that fails again within 30 seconds of being put back is left out."""
    client = rig.grouped()
    group = client.group
    rig.bridge._last_rejoin_us = rig.now_us - RECOVERY_GUARD_US + 1

    with patch(f"{MODULE}.REJOIN_ATTEMPT_DELAYS", (0,)):
        await rig.bridge._leave_sendspin_session()
        await _settle()

    client.quiesce_to_solo_stopped.assert_awaited_once()
    group.add_client.assert_not_called()

    # half a minute after its return it is given another go
    client = rig.grouped()
    group = client.group
    rig.bridge._last_rejoin_us = rig.now_us - RECOVERY_GUARD_US
    with patch(f"{MODULE}.REJOIN_ATTEMPT_DELAYS", (0,)):
        await rig.bridge._leave_sendspin_session()
        await _settle()

    group.add_client.assert_awaited_once_with(client)


@pytest.mark.parametrize("blocker", ["grouped", "streaming", "playing", "group gone"])
async def test_a_bridge_does_not_return_to_what_it_would_disturb(rig: _Rig, blocker: str) -> None:
    """The return is dropped for a player that is in use again, or a group that is gone."""
    rig.bridge._sendspin_client = client = _grouped_client()
    rig.bridge._bridge_client_id = CLIENT_ID
    group = client.group
    await client.quiesce_to_solo_stopped()
    if blocker == "grouped":
        client.group.clients = [client, MagicMock()]
    elif blocker == "streaming":
        client.group.has_active_stream = True
    elif blocker == "playing":
        rig.client.state = SlimPlayerState.PLAYING
    else:
        group.clients = []

    with patch(f"{MODULE}.RECONNECT_REJOIN_DELAYS", (0,)):
        rig.bridge.return_to_group(group)
        await _settle()

    group.add_client.assert_not_called()
    assert rig.bridge._rejoin_task is None
    assert rig.bridge._rejoin_group is None


async def test_the_return_waits_for_the_sendspin_protocol_of_the_bridge(rig: _Rig) -> None:
    """A new bridge is only put back once its Sendspin protocol is attached to its player."""
    rig.bridge._sendspin_client = client = _grouped_client()
    rig.bridge._bridge_client_id = CLIENT_ID
    group = client.group
    await client.quiesce_to_solo_stopped()
    attached = iter([None, MagicMock(player=None), MagicMock()])
    rig.mass.players.get_protocol.side_effect = lambda _client_id: next(attached)

    with patch(f"{MODULE}.RECONNECT_REJOIN_DELAYS", (0, 0, 0)):
        rig.bridge.return_to_group(group)
        assert rig.bridge.shared_group == group
        await _settle()

    group.add_client.assert_awaited_once_with(client)
    # on the third attempt, the first one that found the protocol attached
    assert rig.mass.players.get_protocol.call_count == 3
    assert rig.bridge._last_rejoin_us == rig.now_us


async def test_a_stream_start_cancels_a_pending_return(rig: _Rig) -> None:
    """Joining a session by any means supersedes a return that was lined up."""
    rig.bridge._sendspin_client = _grouped_client()
    rig.bridge.return_to_group(MagicMock())
    rejoin = rig.bridge._rejoin_task
    assert rejoin is not None

    await rig.start_stream()

    assert rejoin.cancelled()
    assert rig.bridge._rejoin_task is None


async def test_a_lost_stream_connection_restarts_the_stream(rig: _Rig) -> None:
    """A player that closes its stream mid-session gets a new one."""
    session = await rig.start_playing()
    session.claimed = session.preroll_sent = False

    await rig.serve(session, _Response(fail_after=1))
    await _settle()

    assert session.ended
    assert rig.session is not session
    assert rig.client.play_url.await_count == 2


async def test_a_failure_of_a_session_that_is_over_is_ignored(rig: _Rig) -> None:
    """A stream that closes after it ended or was replaced is not restarted."""
    first = await rig.start_playing()
    second = await rig.start_playing()

    rig.bridge._recover(first, "it closed its stream")
    assert rig.session is second
    rig.bridge._on_stream_end()
    rig.bridge._recover(second, "it closed its stream")
    await _settle()

    assert rig.session is second
    assert rig.client.play_url.await_count == 2


async def test_the_bridge_of_a_reconnected_player_returns_to_its_group(rig: _Rig) -> None:
    """A player that connects again is a new protocol with a new bridge: it is put back."""
    await rig.manager.evaluate_bridge(rig.protocol)
    old = rig.manager.get_bridge(PLAYER_ID)
    assert old is not None
    group = MagicMock(clients=[rig.sendspin_client, MagicMock()], add_client=AsyncMock())
    rig.sendspin_client.group = group
    # the player disconnected: its protocol is detached, which removes its bridge
    rig.protocol.player = None
    await rig.manager.remove_bridge(PLAYER_ID)
    assert rig.manager.get_bridge(PLAYER_ID) is None

    # its new connection: a new client, which Sendspin puts in a group of its own
    new_client = MagicMock()
    new_client.group = MagicMock(clients=[new_client], has_active_stream=False)
    new_client.roles_by_family.return_value = [SlimprotoBridgeRole(client=new_client)]
    rig.server.register_external_player.return_value = new_client
    rig.protocol.player = MagicMock()
    rig.protocol.player.get_output_protocol_by_domain.return_value = None
    with patch(f"{MODULE}.RECONNECT_REJOIN_DELAYS", (0,)):
        await rig.manager.evaluate_bridge(rig.protocol)
        await _settle()

    new = rig.manager.get_bridge(PLAYER_ID)
    assert new is not None
    assert new is not old
    group.add_client.assert_awaited_once_with(new_client)
    # once: the group is not kept for a later bridge
    assert not rig.manager._groups_to_return_to


@pytest.mark.parametrize("case", ["solo", "attached", "removed for good", "too late"])
async def test_a_group_is_only_returned_to_after_a_reconnect(rig: _Rig, case: str) -> None:
    """No return without a shared group, for a bridge the user took away or after a minute."""
    await rig.manager.evaluate_bridge(rig.protocol)
    group = MagicMock(clients=[rig.sendspin_client, MagicMock()], add_client=AsyncMock())
    rig.sendspin_client.group = group
    rig.mass.players.get_protocol.return_value = MagicMock(underlying_protocol_id=None)
    if case == "solo":
        group.clients = [rig.sendspin_client]
    if case != "attached":
        rig.protocol.player = None

    await rig.manager.remove_bridge(PLAYER_ID)
    if case == "removed for good":
        await rig.manager.remove_bridge(PLAYER_ID, permanent=True)
    if case == "too late":
        _group, until = rig.manager._groups_to_return_to[PLAYER_ID]
        assert until == pytest.approx(rig.mass.loop.time() + RECONNECT_REJOIN_WINDOW, abs=1)
        rig.manager._groups_to_return_to[PLAYER_ID] = (group, rig.mass.loop.time() - 1)
    # the bridge that follows has a client of its own, in a group of its own
    new_client = MagicMock()
    new_client.group = MagicMock(clients=[new_client], has_active_stream=False)
    new_client.roles_by_family.return_value = [SlimprotoBridgeRole(client=new_client)]
    rig.server.register_external_player.return_value = new_client
    rig.protocol.player = MagicMock()
    rig.protocol.player.get_output_protocol_by_domain.return_value = None
    with patch(f"{MODULE}.RECONNECT_REJOIN_DELAYS", (0,)):
        await rig.manager.evaluate_bridge(rig.protocol)
        await _settle()

    assert rig.manager.get_bridge(PLAYER_ID) is not None
    group.add_client.assert_not_called()
    assert not rig.manager._groups_to_return_to


# ---------------------------------------------------------------- commands and state


async def test_commands_go_to_the_player_that_leads_the_sendspin_group(rig: _Rig) -> None:
    """While the bridge plays, the transport of its player is that of the Sendspin group."""
    await rig.manager.evaluate_bridge(rig.protocol)
    rig.bridge = rig.manager.get_bridge(PLAYER_ID)  # type: ignore[assignment]
    bridge_client = MagicMock(synced_to="spb_leader")
    bridge_client.player.player_id = "kitchen"
    rig.mass.players.get_protocol.side_effect = {CLIENT_ID: bridge_client}.get
    leader = MagicMock(player_id="living_room")
    rig.mass.players.get_player_by_protocol.side_effect = {"spb_leader": leader}.get

    assert rig.manager.get_transport_command_target(PLAYER_ID) is None
    await rig.start_stream()

    assert rig.manager.get_transport_command_target(PLAYER_ID) == "living_room"
    bridge_client.synced_to = None
    assert rig.manager.get_transport_command_target(PLAYER_ID) == "kitchen"
    assert rig.manager.get_transport_command_target("other") is None
    rig.mass.players.get_protocol.side_effect = None
    rig.mass.players.get_protocol.return_value = None
    assert rig.manager.get_transport_command_target(PLAYER_ID) is None


async def test_a_pause_on_the_player_pauses_the_sendspin_group(rig: _Rig) -> None:
    """Aioslimproto pauses the player for its own pause button: the group has to follow."""
    await rig.bridge.start()
    bridge_client = MagicMock(synced_to=None)
    bridge_client.player.player_id = "kitchen"
    rig.mass.players.get_protocol.side_effect = {CLIENT_ID: bridge_client}.get
    await rig.start_playing()

    rig.client.state = SlimPlayerState.PAUSED
    rig.event(SlimEventType.PLAYER_UPDATED)
    rig.event(SlimEventType.PLAYER_UPDATED)
    await _settle()

    rig.mass.players.cmd_pause.assert_awaited_once_with("kitchen")


async def test_a_pause_that_fails_is_logged(rig: _Rig, caplog: pytest.LogCaptureFixture) -> None:
    """A pause the player controller refuses does not fail the event of the player."""
    await rig.bridge.start()
    rig.mass.players.cmd_pause.side_effect = PlayerUnavailableError("unavailable")
    await rig.start_playing()

    rig.client.state = SlimPlayerState.PAUSED
    rig.event(SlimEventType.PLAYER_UPDATED)
    await _settle()

    assert "Failed to handle the pause of Kitchen" in caplog.text


async def test_a_state_before_the_stream_was_requested_is_not_followed(rig: _Rig) -> None:
    """What the player was doing before its stream was requested is not about that stream."""
    rig.bridge._sendspin_client = client = _grouped_client()
    rig.client.powered = False
    rig.client.state = SlimPlayerState.PAUSED
    rig.bridge._on_stream_start(MagicMock())

    rig.event(SlimEventType.PLAYER_UPDATED)
    await _settle()

    assert rig.owns_stream()
    rig.mass.players.cmd_pause.assert_not_called()
    client.quiesce_to_solo_stopped.assert_not_called()


async def test_a_player_that_is_switched_off_leaves_the_session(rig: _Rig) -> None:
    """Restarting the stream of a player that was switched off would switch it on again."""
    rig.bridge._sendspin_client = client = _grouped_client()
    await rig.start_playing()

    rig.client.powered = False
    rig.client.state = SlimPlayerState.STOPPED
    rig.event(SlimEventType.PLAYER_UPDATED)
    await _settle()

    assert not rig.owns_stream()
    client.quiesce_to_solo_stopped.assert_awaited_once()
    assert rig.bridge._rejoin_task is None
    assert rig.client.play_url.await_count == 1


def test_the_bridge_reads_the_clock_of_the_provider() -> None:
    """The bridge takes its time from the clock the provider gave to aioslimproto."""
    provider = MagicMock()
    provider.clock = clock = ManualClock(now_us_value=123)

    bridge = SendspinSqueezeliteBridge(provider, MagicMock(), MagicMock())

    assert bridge._clock is clock
