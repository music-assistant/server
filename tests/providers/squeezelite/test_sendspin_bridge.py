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
import math
import random
import struct
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from functools import partial
from itertools import pairwise
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiosendspin.clock import ManualClock
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.events import ClientGroupChangedEvent
from aiosendspin.server.roles import AudioChunk
from aioslimproto.cli import SlimProtoCLI
from aioslimproto.client import SlimClient
from aioslimproto.models import ButtonCode, PlayerStatus, RemoteCode, SlimEvent
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from music_assistant_models.enums import ContentType, IdentifierType
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_OUTPUT_CODEC, CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.timeline_clock import TimelineClock
from music_assistant.providers.sendspin.bridge_role import BRIDGE_SAMPLE_RATE
from music_assistant.providers.squeezelite.bridge_flac import BLOCK_FRAMES, HELD_BACK_BYTES
from music_assistant.providers.squeezelite.bridge_role import (
    SLIMPROTO_BRIDGE_ROLE_ID,
    SlimprotoBridgeRole,
)
from music_assistant.providers.squeezelite.bridge_sync import MAX_START_JUMP_MS, START_JUMP_MS
from music_assistant.providers.squeezelite.player import SqueezelitePlayer
from music_assistant.providers.squeezelite.provider import SqueezelitePlayerProvider
from music_assistant.providers.squeezelite.sendspin_bridge import (
    ALIGN_JUMP_MS,
    ALIGN_LEAD_US,
    ALIGN_ROOM_US,
    ALIGN_TIMEOUT_US,
    BRIDGE_MIN_BUFFER_MS,
    BRIDGE_REQUIRED_LEAD_MS,
    BRIDGE_STREAM_PATH,
    CACHE_CATEGORY_START_OFFSET,
    CLOCK_MAX_AGE_US,
    LEAD_IN_US,
    MAX_BLOCK_US,
    MAX_FLAC_BLOCKS,
    MAX_PENDING_AUDIO_US,
    MAX_PENDING_CHUNKS,
    MAX_PENDING_STATUS,
    MAX_QUICK_FAILURES,
    MAX_START_AHEAD_US,
    MIN_START_AHEAD_US,
    OUTPUT_THRESHOLD,
    PREROLL_US,
    RECONNECT_REJOIN_WINDOW,
    START_LEAD_US,
    STARVED_DECODE_LEAD_US,
    STARVED_LEAD_US,
    STATUS_POLL_INTERVAL,
    STATUS_POLL_INTERVAL_FAST,
    STATUS_TOKEN_BIT,
    STREAM_LEAD_US,
    STREAM_THRESHOLD_KB,
    BridgeSession,
    SendspinBridgeManager,
    SendspinSqueezeliteBridge,
    _pack_24_bit,
)
from tests.common import use_real_bridge_holders, use_real_create_task
from tests.providers.squeezelite.test_bridge_flac import decode_flac

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
LEAD_IN_FRAMES = SECOND * LEAD_IN_US // 1_000_000
ALIGN_LEAD_FRAMES = SECOND * ALIGN_LEAD_US // 1_000_000
ALIGN_ROOM_FRAMES = SECOND * ALIGN_ROOM_US // 1_000_000
# reports every 50 ms for longer than a start is waited for: an alignment that goes on is stuck
ALIGN_ROUNDS = ALIGN_TIMEOUT_US // 50_000 + 20
STREAM_LEAD_FRAMES = SECOND * STREAM_LEAD_US // 1_000_000
START_LEAD_FRAMES = SECOND * START_LEAD_US // 1_000_000
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


def _music(
    seconds: float, ending: str = "tone", level: int = 8000, noise: int = 16
) -> Callable[..., AudioChunk]:
    """
    Return what makes the chunks of a source of a 1 kHz tone that ends after a while.

    The tone carries a little noise, as one from a lossy source does: an encoder makes a
    fifth to a seventh of it, and a player leaves about a second of it undecoded.

    :param seconds: How long the source lasts.
    :param ending: How it ends: on the tone, on a fade-out over its last second, or on
        50 ms of digital silence.
    :param level: The peak of the tone.
    :param noise: The peak of the noise.
    """
    rng = random.Random(level)  # noqa: S311
    total = round(seconds * SECOND)
    levels = [1.0] * total
    if ending == "fade-out":
        levels[-SECOND:] = [(SECOND - at) / SECOND for at in range(SECOND)]
    elif ending == "digital silence":
        levels[-SECOND // 20 :] = [0.0] * (SECOND // 20)
    values = [
        round(
            levels[at]
            * (level * math.sin(2 * math.pi * 1000 * at / SECOND) + rng.randint(-noise, noise))
        )
        for at in range(total)
    ]
    pcm = struct.pack(f"<{2 * total}h", *(value for value in values for _ in range(2)))

    def chunk(origin_us: int, first: int, count: int) -> AudioChunk:
        """Return the chunk of the source from a frame on, as far as the source goes."""
        data = pcm[first * FRAME_SIZE : min(first + count, total) * FRAME_SIZE]
        return AudioChunk(
            data=data,
            timestamp_us=origin_us + _frame_us(first),
            duration_us=_frame_us(len(data) // FRAME_SIZE),
            byte_count=len(data),
        )

    return chunk


def _last_sound(pcm: bytes) -> int:
    """Return the index of the last frame of 16-bit stereo PCM that is not silence."""
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return max(at for at, sample in enumerate(samples) if sample) // 2


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
        supported_codecs=["pcm", "mp3", "flc"],
    )
    for command in (
        "play_url",
        "unpause_at",
        "stop",
        "request_status",
        "power",
        "mute",
        "update_metadata",
        "clear_media",
    ):
        setattr(client, command, AsyncMock())
    return client


def _real_client(rig: _Rig) -> tuple[asyncio.StreamReader, list[tuple[bytes, bytes]]]:
    """
    Give the bridge of a rig a real slimproto client instead of a mocked one.

    :param rig: The rig whose player gets the client.
    :return: The reader to feed what the player sends, and the frames the player is sent
        as they come: command and data.
    """
    sent: list[tuple[bytes, bytes]] = []
    writer = MagicMock(drain=AsyncMock())
    writer.is_closing.return_value = False
    writer.write.side_effect = lambda packet: sent.append((packet[2:6], packet[6:]))
    reader = asyncio.StreamReader()

    def _report(_client: SlimClient, event_type: SlimEventType, data: Any = None) -> None:
        rig.bridge.handle_slim_event(SlimEvent(event_type, PLAYER_ID, data))

    rig.client = rig.protocol.client = SlimClient(reader, writer, _report)
    return reader, sent


async def _player_reports(
    reader: asyncio.StreamReader, event: bytes, *, elapsed_ms: int = 0
) -> None:
    """
    Let a player report a stream event to its real slimproto client.

    :param reader: The reader of the client.
    :param event: The stream event, such as STMs.
    :param elapsed_ms: The elapsed time of its stream, as its decoder counts it.
    """
    # a status of the player that is all zero but its elapsed seconds and milliseconds
    status = struct.pack("!BBBLLLLHLLLLHLL", *[0] * 11, elapsed_ms // 1000, 0, elapsed_ms, 0)
    payload = event + status
    reader.feed_data(b"STAT" + struct.pack("!I", len(payload)) + payload)
    await _settle()


async def _player_presses(reader: asyncio.StreamReader, kind: str, key: str) -> None:
    """
    Let a player report a press of its play or pause key to its real slimproto client.

    :param reader: The reader of the client.
    :param kind: ``butn`` for a button of the player, ``ir`` for its remote.
    :param key: The name of the key: PLAY or PAUSE.
    """
    if kind == "butn":
        packet = b"BUTN" + struct.pack("!ILL", 8, 1000, ButtonCode[key])
    else:
        packet = b"IR  " + struct.pack("!ILBBL", 10, 1000, 0, 32, RemoteCode[key])
    reader.feed_data(packet)
    await _settle()


def _player_of(rig: _Rig) -> SqueezelitePlayer:
    """
    Return the squeezelite player of a rig with a real slimproto client.

    What the client reports goes through the event handling of the provider: to the
    bridge first, then to the player.

    :param rig: The rig whose client reports to the player.
    """
    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player.mass = rig.mass
    player.logger = rig.provider.logger
    player._protocol_id = PLAYER_ID
    player._player = MagicMock(player_id="kitchen")
    player.client = rig.client
    player.sendspin_bridge = rig.bridge
    # what the player makes of the state of its client is not what these tests are about
    for method in ("update_attributes", "update_state", "_store_volume"):
        setattr(player, method, MagicMock())
    provider = MagicMock(mass=rig.mass)
    provider.get_protocol.side_effect = {PLAYER_ID: player}.get

    def _report(_client: SlimClient, event_type: SlimEventType, data: Any = None) -> None:
        event = SlimEvent(event_type, PLAYER_ID, data)
        SqueezelitePlayerProvider._handle_slimproto_event(provider, event)

    rig.client.callback = _report
    return player


def _stream_commands(sent: list[tuple[bytes, bytes]]) -> list[tuple[bytes, int]]:
    """Return what a player was told about its stream, with the time it was told: no status requests."""
    return [
        (data[:1], int.from_bytes(data[14:18], "big"))
        for command, data in sent
        if command == b"strm" and data[:1] != b"t"
    ]


def _grouped_client(*, members: int = 2) -> MagicMock:
    """
    Return a Sendspin client in a group with other clients.

    Leaving moves it on to a solo group, as the real one does, and ``moved_to`` puts it in
    a group as a user does: both tell whoever listens to the client, as the real one does.
    """
    client = MagicMock()
    client.group.clients = [MagicMock() for _ in range(members)]
    client.group.has_active_stream = True
    client.group.add_client = AsyncMock()
    listeners: list[Callable[[Any, Any], None]] = []

    def _add_event_listener(listener: Callable[[Any, Any], None]) -> Callable[[], None]:
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    def _moved_to(group: MagicMock) -> None:
        client.group = group
        for listener in listeners:
            listener(client, ClientGroupChangedEvent(group))

    async def _quiesce() -> str:
        _moved_to(MagicMock(clients=[client], has_active_stream=False))
        return "group-1"

    client.add_event_listener = _add_event_listener
    client.moved_to = _moved_to
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


class _BufferedPlayer(_Response):
    """Stands in for a player that plays the stream it reads from its start instant on."""

    def __init__(self, rig: _Rig, session: BridgeSession, capacity_us: int | None = None) -> None:
        """
        Initialize the player.

        :param rig: The rig whose clock the player plays on.
        :param session: The session it plays.
        :param capacity_us: The audio its buffers hold, None for all it is sent.
        """
        super().__init__()
        self.capacity_us = capacity_us
        # its connection takes nothing
        self.stalled = False
        # the least audio it held as it played, before the bridge wrote more
        self.least_held_us = 1 << 62
        self._rig = rig
        self._session = session
        # the sizes of the frames of a FLAC stream as they were made, and how many of
        # them it decoded, with their bytes
        self._frame_sizes: list[int] = []
        self._decoded = 0
        self._decoded_bytes = 0
        if (encoder := session._encoder) is not None:
            encode_block = encoder._encode_block

            def _encode_block(block: bytes) -> bytes:
                frame = encode_block(block)
                self._frame_sizes.append(len(frame))
                return frame

            encoder._encode_block = _encode_block  # type: ignore[method-assign]

    @property
    def frames(self) -> int:
        """
        Return the number of frames of the stream the player can play.

        That is all it got of a PCM stream. A frame of a FLAC stream is decoded once the
        player holds more of the stream from that frame on than its decoder waits for.
        """
        if (encoder := self._session._encoder) is None:
            return len(self.body) // FRAME_SIZE
        got = len(self.body) - len(encoder.header)
        while (
            self._decoded < len(self._frame_sizes) and got - self._decoded_bytes > HELD_BACK_BYTES
        ):
            self._decoded_bytes += self._frame_sizes[self._decoded]
            self._decoded += 1
        return self._decoded * BLOCK_FRAMES

    @property
    def held_us(self) -> int:
        """Return the audio the player can play and did not play yet: it ran dry at 0."""
        played_us = max(0, self._rig.now_us - self._session.start_us)
        return _frame_us(self.frames) - played_us

    async def write(self, data: bytes) -> None:
        """Take in what the bridge writes, once the connection and the buffers have room for it."""
        while self.stalled or (self.capacity_us is not None and self.held_us >= self.capacity_us):
            await asyncio.sleep(0)
        self.body += data

    def pcm(self) -> bytes:
        """Return the PCM of all of the stream it got, which it decodes from a FLAC stream."""
        if self._session._encoder is None:
            return bytes(self.body)
        return decode_flac(bytes(self.body))[0]

    async def play(self, seconds: float) -> list[int]:
        """
        Play on for a while, in steps of 50 ms, and report four times a second.

        :param seconds: How long it plays.
        :return: The audio it held after every step, in microseconds.
        """
        held = []
        for _ in range(round(seconds * 20)):
            self._rig.clock.advance_us(50_000)
            played_us = self._rig.now_us - self._session.start_us
            if played_us % 250_000 == 0:
                # it reports what it played, which is no more than it got
                elapsed_us = min(played_us, _frame_us(self.frames))
                self._rig.bridge.handle_slim_event(self._rig.status(elapsed_ms=elapsed_us // 1000))
            self.least_held_us = min(self.least_held_us, self.held_us)
            await _settle()
            held.append(self.held_us)
        return held

    def audio(self) -> list[int]:
        """Return the source frames it can play, without the silence."""
        pcm = self.pcm()[: self.frames * FRAME_SIZE]
        return [frame for frame in _frames(pcm) if frame >= 0]

    def left_out(self) -> int:
        """
        Return how many frames its audio leaves out, less the ones it has twice.

        Only single frames are, which is how the sync loop corrects. The rig reports
        whole milliseconds, which the loop takes for half a millisecond early: it sends
        that many frames twice.
        """
        steps = [second - first for first, second in pairwise(self.audio())]
        assert set(steps) <= {0, 1, 2}
        return steps.count(2) - steps.count(0)


class _Rig:
    """A bridge of a player, with everything around it mocked."""

    def __init__(
        self,
        *,
        sync_adjust: int = 0,
        device_type: str = "squeezeplay",
        output_codec: str = "wav",
    ) -> None:
        """
        Initialize the rig.

        :param sync_adjust: The sync delay setting of the player.
        :param device_type: The device type the player reports.
        :param output_codec: The output codec setting of the player. Most tests are about
            the stream of a session whatever it is sent as, which PCM shows as it is.
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
        use_real_bridge_holders(self.sendspin)
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
        self.provider = provider = MagicMock(domain="squeezelite")
        provider.mass = mass
        provider.clock = self.clock
        provider.instance_id = "squeezelite"
        provider.logger = logging.getLogger("test.squeezelite")
        self.manager = provider.bridge_manager = SendspinBridgeManager(provider)
        # mocked, unless a test swaps a real client in
        self.client: Any = _slim_client(device_type)
        self.protocol = protocol = MagicMock()
        protocol.protocol_id = PLAYER_ID
        protocol.display_name = "Kitchen"
        protocol.available = True
        protocol.client = self.client
        protocol.provider = provider
        self.config = {CONF_SYNC_ADJUST: sync_adjust, CONF_OUTPUT_CODEC: output_codec}
        protocol.config.get_value = MagicMock(
            side_effect=lambda key, default=None: self.config.get(key, default)
        )
        # the Sendspin protocol of the bridge client, which knows no source yet
        self.sendspin_player = mass.players.get_protocol.return_value
        self.sendspin_player.session_bit_depth = None
        protocol.get_supported_sample_rates.return_value = [(44100, 16)]
        protocol.volume_set = AsyncMock()
        protocol.sendspin_bridge = None
        protocol.player.attached_protocols = {PLAYER_ID: protocol}
        protocol.player.current_media = None
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

    def aligning(self) -> bool:
        """Return whether the start of the current session is still to be corrected."""
        return self.session.aligning

    def jiffies(self, server_us: int | None = None) -> int:
        """Return the player's clock at a server time, now by default."""
        server_us = self.now_us if server_us is None else server_us
        return JIFFIES_AT_EPOCH + (server_us - EPOCH_US) // 1000

    def map_clock(self, *, elapsed_ms: int = 0) -> None:
        """
        Let the bridge know the player's clock, from three replies to its requests.

        :param elapsed_ms: The elapsed time of its stream by the last of them.
        """
        for number, back_us in enumerate((400_000, 200_000, 0)):
            token = STATUS_TOKEN_BIT | (1 << 30) + number
            self.bridge._pending_status[token] = self.now_us - back_us
            elapsed = max(0, elapsed_ms - back_us // 1000)
            status = self.status(elapsed_ms=elapsed, token=token, at_us=self.now_us - back_us)
            self.bridge.handle_slim_event(status)

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

    async def start_stream(self, *, answers: bool = True) -> BridgeSession:
        """
        Start a Sendspin stream and let the bridge request its stream from the player.

        :param answers: Whether the player answers the status requests the stream request
            waits for, while its clock is not mapped.
        """
        self.bridge._on_stream_start(MagicMock())
        await _settle()
        if answers and not self.session.clock_mapped.is_set():
            self.map_clock()
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
        self.fetched(session)
        self.origin_us = self.now_us + first_chunk_ahead_us
        self.bridge._on_audio_chunk(chunk(self.origin_us, 0, frames))
        self.event(SlimEventType.PLAYER_BUFFER_READY)
        await _settle()
        return session

    def knows_start_offset(self, offset_us: int) -> None:
        """Let the bridge manager know how much later than told the player starts."""
        self.manager._start_offsets_us[PLAYER_ID] = offset_us

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

    def play(
        self, session: BridgeSession, seconds: float, *, late_ms: int = 0, every_us: int = 250_000
    ) -> None:
        """
        Let the player play for a while and report four times a second.

        :param session: The session it plays.
        :param seconds: How long it plays.
        :param late_ms: How far behind its timeline it plays.
        :param every_us: The time between two reports, when it is asked more often.
        """
        for _ in range(round(seconds * 1_000_000 / every_us)):
            self.clock.advance_us(every_us)
            elapsed_ms = (self.now_us - session.start_us) // 1000 - late_ms
            self.bridge.handle_slim_event(self.status(elapsed_ms=max(0, elapsed_ms)))

    def align(self, session: BridgeSession, *, late_ms: int = 0) -> bytes:
        """
        Let the player start and report every 50 ms, until the writer has its start corrected.

        :param session: The session it plays.
        :param late_ms: How much later than its start instant the player starts.
        :return: What the writer sent to the player meanwhile, one block per report.
        """
        blocks = [self.drain(session)]
        self.clock.advance_us(max(0, session.start_us - self.now_us))
        for _ in range(ALIGN_ROUNDS):
            if not session.aligning:
                break
            self.play(session, 0.05, late_ms=late_ms, every_us=50_000)
            blocks.append(self.bridge._next_block(session))
        assert not session.aligning, "the start was not corrected"
        return b"".join(blocks)

    async def align_served(self, session: BridgeSession, *, late_ms: int = 0) -> None:
        """
        Let the player start and report every 50 ms, until the writer of its stream has its start corrected.

        :param session: The session it plays, which is served to it.
        :param late_ms: How much later than its start instant the player starts.
        """
        self.clock.advance_us(max(0, session.start_us - self.now_us))
        for _ in range(ALIGN_ROUNDS):
            if not session.aligning:
                break
            self.play(session, 0.05, late_ms=late_ms, every_us=50_000)
            await _settle()
        assert not session.aligning, "the start was not corrected"

    def follow(self, session: BridgeSession) -> None:
        """
        Let the player play on its timeline and report, until the sync loop has it there.

        :param session: The session it plays.
        """
        assert session.loop is not None
        while not session.loop.on_timeline:
            self.play(session, 0.25)
            assert self.now_us - session.start_us < 10_000_000, "the player is not followed"

    async def await_start(self) -> BridgeSession:
        """
        Take the current stream up to the start instant of the player.

        The player of a restarted stream answers its status requests, fetches the stream
        and reports its buffer ready first.
        """
        session = self.session
        if session.loop is None:
            if not session.clock_mapped.is_set():
                self.map_clock()
                await _settle()
            self.fetched(session)
            self.event(SlimEventType.PLAYER_BUFFER_READY)
            await _settle()
        assert session.loop is not None
        self.clock.advance_us(max(0, session.start_us - self.now_us))
        return session

    async def run_dry(self, *, played_s: float = 0) -> None:
        """
        Let the player play the current stream from its start instant on, then run out of audio.

        :param played_s: How long it plays, and reports that, before it runs dry.
        """
        session = await self.await_start()
        self.play(session, played_s)
        # it answers its status requests all along
        self.map_clock(elapsed_ms=round(played_s * 1000))
        self.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
        await _settle()

    def first_audio(
        self, session: BridgeSession, sent: bytes, *, late_ms: int = 0
    ) -> tuple[int, int]:
        """
        Return the first source frame a player hears, and how much later than due (us).

        :param session: The session it plays.
        :param sent: Everything the player was sent after the preroll.
        :param late_ms: How much later than its start instant the player started.
        """
        frames = _frames(sent)
        index = next(index for index, frame in enumerate(frames) if frame >= 0)
        heard_us = session.start_us + late_ms * 1000 + _frame_us(PREROLL_FRAMES + index)
        return frames[index], heard_us - self.origin_us - _frame_us(frames[index])

    def drain(self, session: BridgeSession) -> bytes:
        """Return everything the writer may send to the player now."""
        parts = []
        while block := self.bridge._next_block(session):
            parts.append(block)
            assert len(parts) < 1000, "the writer does not come to an end"
        return b"".join(parts)

    def grouped(self) -> MagicMock:
        """Let the bridge be registered, with its client in a group with other clients."""
        self.bridge._sendspin_client = client = _grouped_client()
        self.bridge._bridge_client_id = CLIENT_ID
        # as its registration does
        client.add_event_listener(self.bridge._on_client_event)
        return client

    def serve(self, session: BridgeSession, response: _Response) -> asyncio.Task[Any]:
        """Let the player fetch the stream of a session, into a response."""
        request = MagicMock(method="GET", query={"sid": session.session_id})

        # an eager start builds the response before its first await, so the patch never
        # outlives this call: a patch held across awaits leaks when tasks overlap
        with patch("aiohttp.web.StreamResponse", return_value=response):
            return asyncio.create_task(self.bridge.serve_stream(request), eager_start=True)

    async def serve_player(
        self, *, capacity_us: int | None = None, chunk_seconds: int = 1
    ) -> tuple[BridgeSession, _BufferedPlayer]:
        """
        Start a Sendspin stream and serve it to a player that plays it, its start corrected.

        Sendspin delivered 30 seconds of it.

        :param capacity_us: The audio the buffers of the player hold, None for all it is sent.
        :param chunk_seconds: The length of the chunks it was delivered in.
        """
        frames = chunk_seconds * SECOND
        session = await self.start_playing(frames=frames)
        for first in range(frames, 30 * SECOND, frames):
            self.bridge._on_audio_chunk(_chunk(self.origin_us, first, frames))
        session.claimed = session.preroll_sent = False
        player = _BufferedPlayer(self, session, capacity_us)
        self.serve(session, player)
        await _settle()
        await self.align_served(session)
        return session, player

    def lead_frames(self, session: BridgeSession) -> int:
        """Return how far ahead of what is due now the stream of a session was written, in frames."""
        assert session.loop is not None
        played = (self.now_us - session.start_us) * BRIDGE_SAMPLE_RATE // 1_000_000
        return session.loop.frames_consumed - played

    def dropped(self, session: BridgeSession) -> int:
        """Return the number of frames the sync loop of a session left out, less the ones it sent twice."""
        assert session.loop is not None
        return session.loop.frames_consumed - session.loop.frames_sent

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


@pytest.fixture
async def flac_rig() -> AsyncGenerator[_Rig]:
    """Return a bridge of a player that is set to FLAC, not registered with Sendspin yet."""
    rig = _Rig(output_codec="flac")
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
        patch(f"{MODULE}.STATUS_POLL_INTERVAL_FAST", 60),
    ):
        yield


# ---------------------------------------------------------------- policy


async def test_a_player_with_a_timed_start_gets_a_bridge(rig: _Rig) -> None:
    """A player with a MAC address as id that can start at a set instant is bridged."""
    assert rig.manager.bridge_client_id(rig.protocol) == CLIENT_ID
    assert rig.manager.qualifies(rig.protocol) is True


@pytest.mark.parametrize("protocol_id", ["kitchen", "00:00:00:00:00:00", ""])
async def test_a_player_without_a_mac_address_gets_no_bridge(rig: _Rig, protocol_id: str) -> None:
    """The bridge client id comes from the MAC address, so there is none without one."""
    rig.protocol.protocol_id = protocol_id

    assert rig.manager.bridge_client_id(rig.protocol) is None
    assert rig.manager.qualifies(rig.protocol) is False


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

    assert rig.manager.qualifies(rig.protocol) is bridged


@pytest.mark.parametrize(
    ("changed", "evaluated"),
    [("output_protocols", True), ("volume_level", False), ("playback_state", False)],
)
async def test_a_change_of_outputs_evaluates_the_bridge_again(
    rig: _Rig, changed: str, evaluated: bool
) -> None:
    """A player whose outputs change may have another protocol to carry its Sendspin output."""
    other = MagicMock(provider=MagicMock())
    player = MagicMock(attached_protocols={PLAYER_ID: rig.protocol, "other": other})

    with patch.object(rig.manager, "evaluate_bridge", new=AsyncMock()) as evaluate:
        rig.manager._on_player_state_updated(player, {changed: (None, None)})
        await _settle()

    assert evaluate.await_args_list == ([call(rig.protocol)] if evaluated else [])


@pytest.mark.parametrize(
    ("device_model", "bridged"),
    [
        ("SqueezeLite", True),
        # slimproto on a WiiM is a second protocol, but the WiiM plays the audio itself
        ("WiiM Player", True),
        # the LMS bridge tools play to a device of another protocol
        ("RaopBridge", False),
        ("CastBridge", False),
        ("UPnPBridge", False),
    ],
)
async def test_a_bridge_tool_gets_no_bridge(rig: _Rig, device_model: str, bridged: bool) -> None:
    """A squeezelite player that is a bridge to a device of another protocol is not bridged."""
    rig.client.device_model = device_model

    assert rig.manager.qualifies(rig.protocol) is bridged


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
        "register().add_event_listener",
        "register().roles_by_family",
        "role.set_callbacks",
        "role.limit_bit_depth",
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


async def test_the_client_is_renamed_with_its_player(rig: _Rig) -> None:
    """A player reports its name after it connected: its Sendspin client takes it over."""
    rig.protocol.display_name = "squeezeplay: aa:bb:cc:dd:ee:ff"
    await rig.manager.evaluate_bridge(rig.protocol)
    client = rig.sendspin_client
    hello = client.info
    assert hello.name == "squeezeplay: aa:bb:cc:dd:ee:ff (Squeezelite)"
    client.name = hello.name
    player = MagicMock(attached_protocols={PLAYER_ID: rig.protocol, "other": MagicMock()})

    rig.protocol.display_name = "Kitchen"
    rig.manager._on_player_state_updated(player, {"name": (hello.name, "Kitchen")})

    client.refresh_identity_from_hello.assert_called_once_with(
        replace(hello, name="Kitchen (Squeezelite)"),
        negotiated_roles=client.negotiated_role_ids,
    )
    # the hello is the one of the bridge otherwise: its formats are what its role plays
    assert client.refresh_identity_from_hello.call_args.args[0].player_support is (
        hello.player_support
    )
    # a name the client has already is no news, nor is anything else about the player
    client.name = "Kitchen (Squeezelite)"
    rig.manager._on_player_state_updated(player, {"name": ("Kitchen", "Kitchen")})
    rig.manager._on_player_state_updated(player, {"volume_level": (20, 30)})
    assert client.refresh_identity_from_hello.call_count == 1


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


async def test_the_timeline_clock_of_the_server_is_one_clock(rig: _Rig) -> None:
    """The squeezelite provider and Sendspin both run on the timeline clock of the server."""
    rig.server.clock = rig.bridge._clock = TimelineClock()

    await rig.bridge.start()

    assert rig.bridge.is_registered


async def test_two_timeline_clocks_are_not_one_clock(rig: _Rig) -> None:
    """A timeline clock has a rate of its own, so only the very same one will do."""
    rig.server.clock = TimelineClock()
    rig.bridge._clock = TimelineClock()

    await rig.bridge.start()

    assert not rig.bridge.is_registered


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
        metadata={},
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
    """The player gets the preroll at once, and what it is allowed as it plays."""
    session = await rig.start_playing(frames=9 * SECOND)
    session.claimed = session.preroll_sent = False
    response = _Response()

    serving = rig.serve(session, response)
    await _settle()

    assert response.prepare.await_count == 1
    # the silence before its audio at once, less the room for a late start
    assert _frames(bytes(response.body)) == [-1] * (LEAD_IN_FRAMES - ALIGN_ROOM_FRAMES)
    await rig.align_served(session)
    await _settle()
    # then the rest of the lead-in and its audio, a short lead ahead of the 300 ms it played
    audio = response.body[LEAD_IN_FRAMES * 4 :]
    assert _frames(bytes(response.body[: LEAD_IN_FRAMES * 4])) == [-1] * LEAD_IN_FRAMES
    assert _frames(bytes(audio)) == list(range(13230 + START_LEAD_FRAMES - LEAD_IN_FRAMES))
    # and the long lead once the sync loop has the player on its timeline, 2.8 seconds in
    rig.follow(session)
    await _settle()
    await _settle()
    assert rig.lead_frames(session) == STREAM_LEAD_FRAMES
    assert len(response.body) == (round(2.8 * SECOND) + STREAM_LEAD_FRAMES) * FRAME_SIZE + (
        -rig.dropped(session) * FRAME_SIZE
    )
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
        try:
            async with asyncio.timeout(10):
                head = (await reader.readuntil(b"\r\n\r\n")).decode().lower()
                preroll = await reader.readexactly(PREROLL_FRAMES * FRAME_SIZE)
                rig.map_clock()
                rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, SECOND))
                rig.event(SlimEventType.PLAYER_BUFFER_READY)
                # the player starts, shows where it is and plays for a second more
                await rig.align_served(session)
                rig.clock.advance_us(1_000_000)
                lead_in = await reader.readexactly((LEAD_IN_FRAMES - PREROLL_FRAMES) * FRAME_SIZE)
                audio = await reader.readexactly(SECOND * FRAME_SIZE)
                rig.bridge._on_stream_end()
                rest = await reader.read()
        finally:
            # whatever went wrong, the stream is not served on while the server closes
            rig.bridge._on_stream_end()
            writer.close()

    assert " 200 " in head.splitlines()[0]
    assert f"content-type: {MIME_TYPE}" in head
    assert "transfer-encoding" not in head
    assert "content-length" not in head
    assert preroll == bytes(PREROLL_FRAMES * FRAME_SIZE)
    assert lead_in == bytes((LEAD_IN_FRAMES - PREROLL_FRAMES) * FRAME_SIZE)
    assert _frames(audio) == list(range(SECOND))
    assert _frames(rest) == [-1] * (len(rest) // FRAME_SIZE)


async def test_the_first_chunk_is_heard_at_its_timestamp(rig: _Rig) -> None:
    """The stream starts a lead-in of silence before the first chunk, so that chunk is on time."""
    session = await rig.start_playing()

    assert session.start_us == rig.origin_us - LEAD_IN_US
    assert rig.session.loop is not None
    # the preroll is frame 0 and on of the stream
    assert rig.session.loop.frames_sent == PREROLL_FRAMES
    sent = _frames(rig.align(session) + rig.drain(session))
    assert sent[: LEAD_IN_FRAMES - PREROLL_FRAMES] == [-1] * (LEAD_IN_FRAMES - PREROLL_FRAMES)
    assert sent[LEAD_IN_FRAMES - PREROLL_FRAMES :][:3] == [0, 1, 2]


@pytest.mark.parametrize(("sync_adjust", "shift_us"), [(0, 0), (100, -100_000), (-80, 80_000)])
async def test_a_positive_sync_delay_plays_earlier(sync_adjust: int, shift_us: int) -> None:
    """The setting is the time the player needs to be heard, as for its own sync groups."""
    rig = _Rig(sync_adjust=sync_adjust)
    try:
        session = await rig.start_playing()

        assert session.start_us == rig.origin_us + shift_us - LEAD_IN_US
        rig.client.unpause_at.assert_awaited_once_with(rig.jiffies(session.start_us))
        sent = _frames(rig.align(session) + rig.drain(session))
        assert sent[LEAD_IN_FRAMES - PREROLL_FRAMES - 1 :][:2] == [-1, 0]
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


FLAC_PLAYER = ["flc", "pcm", "mp3"]
# what a player plays that sends no capabilities
PLAIN_PLAYER = ["pcm", "mp3"]
PCM_16 = "audio/pcm;rate=44100;bitrate=16;channels=2"
PCM_24 = "audio/pcm;rate=44100;bitrate=24;channels=2"
FLAC = "audio/flac"

# The output codec setting of a player, the codecs it says it plays, the bit depth of the
# audio of the playback session and the bit depths the player plays, with what the player
# is sent.
SENT = [
    # FLAC for a player that plays it, in the bit depth of the session
    ("flac", FLAC_PLAYER, 16, (16, 24), FLAC, 16),
    ("flac", FLAC_PLAYER, 24, (16, 24), FLAC, 24),
    ("flac", FLAC_PLAYER, 16, (16,), FLAC, 16),
    ("flac", FLAC_PLAYER, 24, (16,), FLAC, 16),
    # PCM for a player that does not say it plays FLAC
    ("flac", PLAIN_PLAYER, 16, (16, 24), PCM_16, 16),
    ("flac", PLAIN_PLAYER, 24, (16, 24), PCM_24, 24),
    ("flac", PLAIN_PLAYER, 16, (16,), PCM_16, 16),
    ("flac", PLAIN_PLAYER, 24, (16,), PCM_16, 16),
    # PCM for the setting that asks for it
    ("wav", FLAC_PLAYER, 16, (16, 24), PCM_16, 16),
    ("wav", FLAC_PLAYER, 24, (16, 24), PCM_24, 24),
    ("wav", FLAC_PLAYER, 16, (16,), PCM_16, 16),
    ("wav", FLAC_PLAYER, 24, (16,), PCM_16, 16),
    ("wav", PLAIN_PLAYER, 16, (16, 24), PCM_16, 16),
    ("wav", PLAIN_PLAYER, 24, (16, 24), PCM_24, 24),
    ("wav", PLAIN_PLAYER, 16, (16,), PCM_16, 16),
    ("wav", PLAIN_PLAYER, 24, (16,), PCM_16, 16),
    # never FLAC for a lossy setting: PCM in 16 bits, whatever the session and the player
    ("mp3", FLAC_PLAYER, 16, (16, 24), PCM_16, 16),
    ("mp3", FLAC_PLAYER, 24, (16, 24), PCM_16, 16),
    ("mp3", FLAC_PLAYER, 32, (16, 24), PCM_16, 16),
    ("mp3", FLAC_PLAYER, 24, (16,), PCM_16, 16),
    ("mp3", PLAIN_PLAYER, 16, (16, 24), PCM_16, 16),
    ("mp3", PLAIN_PLAYER, 24, (16, 24), PCM_16, 16),
    ("aac", FLAC_PLAYER, 16, (16, 24), PCM_16, 16),
    ("aac", FLAC_PLAYER, 24, (16, 24), PCM_16, 16),
    ("aac", PLAIN_PLAYER, 32, (16, 24), PCM_16, 16),
    ("aac", PLAIN_PLAYER, 24, (16,), PCM_16, 16),
    # a session whose audio is processed on its way has 32 bits, which fill the 24 of a player
    ("flac", FLAC_PLAYER, 32, (16, 24), FLAC, 24),
    ("wav", PLAIN_PLAYER, 32, (16, 24), PCM_24, 24),
    ("flac", FLAC_PLAYER, 32, (16,), FLAC, 16),
    # a session without a first item plays in all the bits of the player
    ("flac", FLAC_PLAYER, None, (16, 24), FLAC, 24),
    ("wav", PLAIN_PLAYER, None, (16, 24), PCM_24, 24),
    ("mp3", FLAC_PLAYER, None, (16, 24), PCM_16, 16),
    ("aac", PLAIN_PLAYER, None, (16, 24), PCM_16, 16),
    ("flac", FLAC_PLAYER, None, (16,), FLAC, 16),
]


def _sent_id(case: tuple[str, list[str], int | None, tuple[int, ...], str, int]) -> str:
    """Return the name of a case: its setting, its player and the bit depth of its session."""
    output_codec, codecs, session_bit_depth, depths, _mime_type, _bit_depth = case
    player = f"{'flac' if 'flc' in codecs else 'plain'} player of {max(depths)} bits"
    session = f"session of {session_bit_depth} bits" if session_bit_depth else "no first item"
    return f"{output_codec}, {player}, {session}"


@pytest.mark.parametrize(
    ("output_codec", "codecs", "session_bit_depth", "depths", "mime_type", "bit_depth"),
    SENT,
    ids=[_sent_id(case) for case in SENT],
)
async def test_the_output_codec_and_the_session_decide_what_a_player_is_sent(
    output_codec: str,
    codecs: list[str],
    session_bit_depth: int | None,
    depths: tuple[int, ...],
    mime_type: str,
    bit_depth: int,
) -> None:
    """The setting of the player picks FLAC or PCM, and the playback session the bit depth."""
    rig = _Rig(output_codec=output_codec)
    try:
        rig.client.supported_codecs = codecs
        rig.protocol.get_supported_sample_rates.return_value = [(44100, depth) for depth in depths]
        await rig.bridge.start()
        # a playback session starts: Sendspin has the player follow its first item
        rig.sendspin_player.session_bit_depth = session_bit_depth
        assert rig.role.set_preferred_format(None)

        session = await rig.start_stream()

        assert (session.mime_type, session.audio_format.bit_depth) == (mime_type, bit_depth)
        assert rig.client.play_url.await_args.kwargs["mime_type"] == mime_type
    finally:
        await rig.close()


async def test_a_flac_stream_that_fails_before_it_played_is_restarted_as_pcm(
    flac_rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A player whose FLAC stream does not get to play is sent PCM, for as long as its bridge lasts."""
    rig = flac_rig
    client = rig.grouped()
    first = await rig.start_playing()
    assert first.mime_type == "audio/flac"

    await rig.run_dry(played_s=2)

    restarted = rig.session
    assert restarted is not first
    assert (restarted.mime_type, restarted.audio_format.bit_depth) == (MIME_TYPE, 16)
    assert rig.client.play_url.await_args.kwargs["mime_type"] == MIME_TYPE
    # with the audio the player had not played of its FLAC stream
    assert [chunk.timestamp_us for chunk in restarted.chunks] == [rig.origin_us]
    (told,) = [record for record in caplog.records if "instead of FLAC" in record.getMessage()]
    assert told.levelno == logging.WARNING
    assert told.getMessage() == (
        "Kitchen is sent PCM instead of FLAC from now on: its FLAC stream failed before it "
        "played 10 s"
    )
    # the failure counts as any other: two more in a row take the player out
    await rig.run_dry()
    assert rig.owns_stream()
    assert rig.session.mime_type == MIME_TYPE
    client.quiesce_to_solo_stopped.assert_not_called()
    await rig.run_dry()
    assert not rig.owns_stream()
    client.quiesce_to_solo_stopped.assert_awaited_once()
    # the streams after it are PCM too, and nothing more is said about it
    assert (await rig.start_playing()).mime_type == MIME_TYPE
    assert len([record for record in caplog.records if "of FLAC" in record.getMessage()]) == 1
    # until the player has a new bridge
    bridge = SendspinSqueezeliteBridge(rig.provider, rig.protocol, rig.server)
    assert bridge._role_format().content_type == ContentType.FLAC


async def test_a_flac_stream_that_fails_after_it_played_stays_flac(
    flac_rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A player that played its FLAC stream for 10 seconds decodes it: its restart is FLAC."""
    rig = flac_rig
    first = await rig.start_playing()

    await rig.run_dry(played_s=10)

    assert rig.session is not first
    assert rig.session.mime_type == "audio/flac"
    assert (await rig.start_playing()).mime_type == "audio/flac"
    assert "instead of FLAC" not in caplog.text


@pytest.mark.parametrize(("output_codec", "codecs"), [("wav", FLAC_PLAYER), ("flac", PLAIN_PLAYER)])
async def test_a_pcm_stream_that_fails_before_it_played_is_restarted_as_it_was(
    output_codec: str, codecs: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A player that is sent PCM has no other stream to go to, and its next bridge none to avoid."""
    rig = _Rig(output_codec=output_codec)
    try:
        rig.client.supported_codecs = codecs
        first = await rig.start_playing()

        await rig.run_dry()

        assert rig.session is not first
        assert rig.session.mime_type == first.mime_type == MIME_TYPE
        assert "instead of FLAC" not in caplog.text
        # a FLAC stream is still what it gets once it is set to it and plays it
        rig.config[CONF_OUTPUT_CODEC] = "flac"
        rig.client.supported_codecs = FLAC_PLAYER
        assert (await rig.start_playing()).mime_type == "audio/flac"
    finally:
        await rig.close()


@pytest.mark.parametrize(
    ("output_codec", "frames"),
    [("wav", MAX_BLOCK_FRAMES), ("flac", MAX_FLAC_BLOCKS * BLOCK_FRAMES)],
)
async def test_a_round_of_the_writer_takes_no_more_than_the_event_loop_can_spare(
    output_codec: str, frames: int
) -> None:
    """A FLAC stream is encoded as it is written: a dozen blocks a round, where PCM goes half a second."""
    rig = _Rig(output_codec=output_codec)
    try:
        session = await rig.start_playing(frames=SECOND)
        for second in range(1, 30):
            rig.bridge._on_audio_chunk(_chunk(rig.origin_us, second * SECOND, SECOND))

        # the silence ahead of its start
        assert len(rig.bridge._next_block(session)) == frames * FRAME_SIZE
        # and its audio, on the way to its long lead
        rig.align(session)
        rig.follow(session)
        assert session.loop is not None
        for _ in range(2):
            taken = session.loop.frames_consumed
            assert rig.bridge._next_block(session)
            assert session.loop.frames_consumed - taken == frames
    finally:
        await rig.close()


async def test_a_stream_keeps_its_codec_and_bit_depth_while_it_plays(flac_rig: _Rig) -> None:
    """What the queue plays next and a setting that changes are for the stream after this one."""
    rig = flac_rig
    rig.protocol.get_supported_sample_rates.return_value = [(44100, 16), (44100, 24)]
    await rig.bridge.start()
    rig.sendspin_player.session_bit_depth = 16
    assert rig.role.set_preferred_format(None)
    session = await rig.start_playing()

    rig.sendspin_player.session_bit_depth = 24
    rig.config[CONF_OUTPUT_CODEC] = "wav"
    rig.bridge._on_audio_chunk(_chunk(rig.origin_us, 4 * SECOND, SECOND))
    rig.bridge.update_now_playing()
    await _settle()

    assert rig.session is session
    assert (session.mime_type, session.audio_format.bit_depth) == ("audio/flac", 16)
    assert rig.client.play_url.await_count == 1


@pytest.mark.parametrize(
    ("bit_depth", "frame_size", "preroll_frames"), [(16, 4, 21216), (24, 8, 17760)]
)
async def test_the_stream_request_tells_the_player_it_is_flac(
    bit_depth: int, frame_size: int, preroll_frames: int
) -> None:
    """What aioslimproto makes of a FLAC session: the player takes the format from the stream."""
    session = BridgeSession(
        AudioFormat(
            content_type=ContentType.FLAC, sample_rate=48000, bit_depth=bit_depth, channels=2
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
    assert start[2:7] == b"f????"
    # the encoder takes the samples as Sendspin delivers them: 24 bits in 4 bytes
    assert session.frame_size == frame_size
    # a quarter of a second, and the silence a player can not decode at the end of it
    assert len(session.preroll) == preroll_frames * frame_size


async def test_a_flac_stream_opens_with_silence_its_player_can_decode(flac_rig: _Rig) -> None:
    """The preroll is silence in full: more than the buffer waits for, a quarter of a second to play."""
    rig = flac_rig
    session = await rig.start_stream()
    request = MagicMock(method="GET", query={"sid": session.session_id})
    player = _BufferedPlayer(rig, session)

    with patch("aiohttp.web.StreamResponse", return_value=player) as response:
        serving = asyncio.create_task(rig.bridge.serve_stream(request))
        await _settle()

    assert response.call_args.kwargs["headers"] == {"Content-Type": "audio/flac"}
    assert rig.client.play_url.await_args.kwargs["stream_threshold"] == STREAM_THRESHOLD_KB
    assert session.preroll_sent
    assert player.body[:4] == b"fLaC"
    assert len(player.body) > STREAM_THRESHOLD_KB * 1024
    # the preroll is the blocks that are complete, and the player decodes the first of
    # them: the quarter of a second it has to hold, and no block more
    preroll, sample_rate, bit_depth = decode_flac(bytes(player.body))
    assert (sample_rate, bit_depth) == (44100, 16)
    assert preroll == bytes(17 * BLOCK_FRAMES * FRAME_SIZE)
    assert PREROLL_FRAMES <= player.frames < PREROLL_FRAMES + BLOCK_FRAMES
    # and the start is decided as for any stream, with the preroll as frame 0 and on
    rig.origin_us = rig.now_us + 2_000_000
    rig.bridge._on_audio_chunk(_chunk(rig.origin_us, 0, SECOND))
    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    await _settle()
    assert session.start_us == rig.origin_us - LEAD_IN_US
    assert session.loop is not None
    assert session.loop.frames_sent >= len(session.preroll) // FRAME_SIZE
    rig.bridge._on_stream_end()
    await serving


async def test_a_flac_player_has_silence_to_play_until_its_start_is_corrected(
    flac_rig: _Rig,
) -> None:
    """From its start instant on a player holds silence it can decode, as far ahead as a PCM one."""
    rig = flac_rig
    session = await rig.start_playing(frames=SECOND)
    for first in range(SECOND, 30 * SECOND, SECOND):
        rig.bridge._on_audio_chunk(_chunk(rig.origin_us, first, SECOND))
    session.claimed = session.preroll_sent = False
    player = _BufferedPlayer(rig, session)
    rig.serve(session, player)
    await _settle()
    rig.clock.advance_us(session.start_us - rig.now_us)

    held = []
    while session.aligning:
        held += await player.play(0.05)
        assert len(held) < ALIGN_ROUNDS, "the start was not corrected"

    assert session.loop is not None
    assert session.loop.start_error_ms == pytest.approx(-0.5)
    block_us = _frame_us(BLOCK_FRAMES)
    assert min(held) >= ALIGN_LEAD_US
    # all of the lead-in but the room for a late start went out before the player played
    # any of it, as it does for a PCM player: the 7 blocks at the end of it are what this
    # one can not decode yet
    at_once_us = LEAD_IN_US - ALIGN_ROOM_US - 7 * block_us
    assert held[0] == pytest.approx(at_once_us - 50_000, abs=block_us)
    assert rig.client.play_url.await_count == 1
    # its first audio is heard on the timeline, and it holds the lead of a start of it
    await player.play(2)
    index = _frames(player.pcm()).index(0)
    assert session.start_us + _frame_us(index) == rig.origin_us
    assert rig.session is session


async def test_a_flac_player_is_kept_its_lead_in_what_it_can_decode(flac_rig: _Rig) -> None:
    """A player that decodes less than it got rides out the stall a PCM player does."""
    rig = flac_rig
    session, player = await rig.serve_player()
    # a lead is kept to the block: what does not fill one waits for the audio after it
    block_us = _frame_us(BLOCK_FRAMES)
    await player.play(1)
    assert abs(player.held_us - START_LEAD_US) < block_us
    await player.play(2)
    assert abs(player.held_us - STREAM_LEAD_US) < block_us
    # what went out is further ahead than that, by what the player can not decode yet
    assert session.frames_held >= 7 * BLOCK_FRAMES
    assert rig.lead_frames(session) >= STREAM_LEAD_FRAMES + 6 * BLOCK_FRAMES

    player.stalled = True
    held = await player.play(4)
    player.stalled = False

    assert min(held) > 1_900_000
    await player.play(1)
    assert abs(player.held_us - STREAM_LEAD_US) < block_us
    assert rig.session is session


@pytest.mark.parametrize(("level", "held_ms"), [(15, 180), (3, 660), (0, 1500)])
async def test_a_live_source_reaches_a_flac_player_with_no_silence_in_its_place(
    flac_rig: _Rig, caplog: pytest.LogCaptureFixture, level: int, held_ms: int
) -> None:
    """
    Audio that is only 2 seconds ahead reaches a player as it is, loud or quiet.

    The quietest audio that goes out compressed leaves a second and a half of it undecoded,
    and its player half a second to play: the writer has no silence to send for that.
    """
    caplog.set_level(logging.DEBUG, logger="test.squeezelite")
    rig = flac_rig
    rng = random.Random(level)  # noqa: S311
    tenth = SECOND // 10
    ahead_us = BRIDGE_REQUIRED_LEAD_MS * 1000
    # ten values on one channel, the same ones in every block: frames of 580 bytes, just
    # over the eighth of a block below which a frame goes out with its samples in full
    faint = [number % 10 - 4 for number in range(BLOCK_FRAMES)]
    rng.shuffle(faint)

    def noise(origin_us: int, first: int, count: int) -> AudioChunk:
        """Return a chunk of noise of a number of bits, which is all an encoder goes by."""
        if level:
            values = [rng.randrange(-(1 << level), 1 << level) for _ in range(2 * count)]
        else:
            values = [0] * (2 * count)
            values[1::2] = [faint[frame % BLOCK_FRAMES] for frame in range(first, first + count)]
        data = struct.pack(f"<{2 * count}h", *values)
        return AudioChunk(
            data=data,
            timestamp_us=origin_us + _frame_us(first),
            duration_us=_frame_us(count),
            byte_count=len(data),
        )

    session = await rig.start_playing(first_chunk_ahead_us=ahead_us, frames=tenth, chunk=noise)
    session.claimed = session.preroll_sent = False
    player = _BufferedPlayer(rig, session)
    rig.serve(session, player)
    await _settle()
    rig.clock.advance_us(session.start_us - rig.now_us)

    held = []
    delivered = tenth
    for _ in range(200):
        # Sendspin delivers what is due up to its lead from now
        while _frame_us(delivered) < rig.now_us + ahead_us - rig.origin_us:
            rig.bridge._on_audio_chunk(noise(rig.origin_us, delivered, tenth))
            delivered += tenth
        held += await player.play(0.05)

    assert "Sendspin has no audio for Kitchen" not in caplog.text
    # all of it went out, and the player can decode it but for the end of it
    assert not session.chunks
    assert session.frames_held * 1000 // SECOND == pytest.approx(held_ms, abs=40)
    if not level:
        assert min(player._frame_sizes[-100:]) >= 4608 // 8
        assert max(player._frame_sizes[-100:]) <= 580
    # the lead of the group is its lead from when it is on its timeline, less that end
    assert min(held[80:]) > ahead_us - (held_ms + 150) * 1000
    assert min(held) >= ALIGN_LEAD_US
    assert rig.session is session
    # from its first sample on the audio has no silence in it
    samples = struct.unpack(f"<{len(player.pcm()) // 2}h", player.pcm())
    audio = samples[next(at for at, sample in enumerate(samples) if sample) :]
    quiet = max(len(run) for run in bytes(bool(sample) for sample in audio).split(b"\x01"))
    assert quiet < 20


async def test_silence_keeps_a_flac_player_fed_that_sendspin_has_nothing_for(
    flac_rig: _Rig,
) -> None:
    """Silence goes out in full, so the player decodes the audio before it and plays on."""
    rig = flac_rig
    session = await rig.start_playing(frames=3 * SECOND)
    session.claimed = session.preroll_sent = False
    player = _BufferedPlayer(rig, session)
    rig.serve(session, player)
    await _settle()
    await rig.align_served(session)

    held = await player.play(10)

    # all of the three seconds it was given, and silence from there on: half a second
    # ahead as for a PCM player, of which this one can not decode the end, and a block
    # more where its last audio ends
    assert sorted(set(player.audio())) == list(range(3 * SECOND))
    assert session._encoder is not None
    undecoded = session._encoder.silence_held + BLOCK_FRAMES
    assert min(held) > STARVED_LEAD_US - _frame_us(undecoded)
    assert rig.lead_frames(session) == STARVED_LEAD_FRAMES
    assert rig.session is session
    assert rig.client.play_url.await_count == 1


@pytest.mark.parametrize("ending", ["tone", "fade-out", "digital silence"])
async def test_a_flac_player_plays_a_source_that_ends_to_its_last_sample(
    flac_rig: _Rig, ending: str
) -> None:
    """The end of a source is decoded before it plays: the player does not run dry on it."""
    rig = flac_rig
    source = _music(4, ending)
    session = await rig.start_playing(frames=4 * SECOND, chunk=source)
    session.claimed = session.preroll_sent = False
    player = _BufferedPlayer(rig, session)
    rig.serve(session, player)
    await _settle()
    await rig.align_served(session)

    await player.play(6)

    # what it can decode is kept ahead of what it plays, in silence after the source,
    # less what it played since the last write
    assert player.least_held_us > STARVED_DECODE_LEAD_US - 50_000 - _frame_us(BLOCK_FRAMES)
    assert rig.session is session
    # it decoded the source to its last sample, where its timestamp puts it after the
    # frames the sync loop repeated: the last ones of a fade-out are 0
    end = round((rig.origin_us - session.start_us) * SECOND / 1_000_000) + 4 * SECOND
    if ending == "digital silence":
        end -= SECOND // 20
    pcm = player.pcm()[: player.frames * FRAME_SIZE]
    assert abs(_last_sound(pcm) + 1 - (end - rig.dropped(session))) < 10


async def test_a_flac_player_rides_out_a_source_that_stalls_after_audio_that_compresses_well(
    flac_rig: _Rig,
) -> None:
    """A source that stops for a while leaves the player silence, and what comes after it plays."""
    rig = flac_rig
    source = _music(12, noise=2)
    session = await rig.start_playing(frames=6 * SECOND, chunk=source)
    session.claimed = session.preroll_sent = False
    player = _BufferedPlayer(rig, session)
    rig.serve(session, player)
    await _settle()
    await rig.align_served(session)
    # the player can not decode more of it yet than a PCM player is kept ahead
    await player.play(4)
    assert session.frames_held > 2 * STARVED_LEAD_FRAMES

    # the source stops after 6 seconds for a while, then delivers the rest
    await player.play(4)
    rig.bridge._on_audio_chunk(source(rig.origin_us, 6 * SECOND, 6 * SECOND))
    await player.play(6)

    assert player.least_held_us > STARVED_DECODE_LEAD_US - 50_000 - _frame_us(BLOCK_FRAMES)
    assert rig.session is session
    # the source plays on from where the stream is, to its last sample
    end = round((rig.origin_us - session.start_us) * SECOND / 1_000_000) + 12 * SECOND
    pcm = player.pcm()[: player.frames * FRAME_SIZE]
    assert abs(_last_sound(pcm) + 1 - (end - rig.dropped(session))) < 10


async def test_a_flac_player_is_stopped_at_once_at_the_end_of_its_source(flac_rig: _Rig) -> None:
    """A stop as the source ends stops the player, which holds the silence after it."""
    rig = flac_rig
    session = await rig.start_playing(frames=2 * SECOND, chunk=_music(2, "fade-out"))
    session.claimed = session.preroll_sent = False
    player = _BufferedPlayer(rig, session)
    task = rig.serve(session, player)
    await _settle()
    await rig.align_served(session)
    end_us = rig.origin_us + 2_000_000
    await player.play((end_us - rig.now_us) / 1_000_000)

    rig.bridge._on_stream_end()
    rig.bridge._on_explicit_stop()
    await _settle()

    assert task.done()
    assert rig.bridge._session is None
    rig.client.stop.assert_awaited_once()


async def test_a_flac_stream_decodes_to_what_the_writer_made(flac_rig: _Rig) -> None:
    """Every frame of the PCM is in the stream once, the ones the sync loop repeated or dropped too."""
    rig = flac_rig
    produced = bytearray()
    encode = BridgeSession.encode

    def _encode(session: BridgeSession, pcm: bytes) -> bytes:
        produced.extend(pcm)
        return encode(session, pcm)

    with patch.object(BridgeSession, "encode", _encode):
        session, player = await rig.serve_player()
        assert session.mime_type == "audio/flac"
        await player.play(12)
        loop = session.loop
        assert loop is not None
        # all of what was made went out, but what waits for the rest of its block
        decoded = player.pcm()
        assert len(produced) == loop.frames_sent * FRAME_SIZE
        assert decoded == bytes(produced[: len(decoded)])
        assert 0 <= loop.frames_sent - len(decoded) // FRAME_SIZE < BLOCK_FRAMES
        rig.bridge._on_stream_end()
        await _settle()

    # what the rig reports makes the loop send frames twice: they are in the stream
    assert rig.dropped(session) < 0
    # one stream: its header went out once, and its end left nothing behind
    assert bytes(player.body).count(b"fLaC") == 1
    assert player.pcm() == bytes(produced)
    steps = [second - first for first, second in pairwise(_frames(player.pcm())) if first >= 0]
    assert set(steps) <= {0, 1}
    assert -steps.count(0) == rig.dropped(session)


async def test_24_bit_audio_reaches_a_flac_player_bit_for_bit(flac_rig: _Rig) -> None:
    """The encoder takes the 24 bits Sendspin delivers in 32: nothing is packed or lost."""
    rig = flac_rig
    await rig.start_hires()
    session = await rig.start_playing(frames=4 * 48000, chunk=_chunk_24)
    assert (session.mime_type, session.frame_size) == ("audio/flac", 8)

    pcm = rig.align(session) + rig.drain(session)
    stream = session.encode(session.preroll) + session.encode(pcm)

    decoded, sample_rate, bit_depth = decode_flac(stream)
    assert (sample_rate, bit_depth) == (48000, 24)
    # the lead-in of 1.45 seconds, then the audio as its chunk holds it
    audio = _chunk_24(rig.origin_us, 0, 4 * 48000).data
    assert decoded[: 69600 * 8] == bytes(69600 * 8)
    # a short lead ahead of the 300 ms it played, which is 75 blocks
    assert len(decoded) == (14400 + 72000) * 8
    assert decoded[69600 * 8 :] == audio[: len(decoded) - 69600 * 8]


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

    # on the timeline as any stream: its first frame a lead-in after its start
    assert session.start_us == rig.origin_us - LEAD_IN_US
    assert session.loop is not None
    assert session.loop.frames_sent == 12000
    # the lead-in of 1.45 seconds in frames of this stream, less the preroll, and then the
    # lead of a second and a half ahead of the 300 ms it played
    lead = rig.align(session) + rig.drain(session)
    assert _frames_24(lead) == [-1] * (69600 - 12000) + list(range(14400 + 72000 - 69600))
    # and a second more for every second that it plays
    rig.clock.advance_us(1_000_000)
    assert _frames_24(rig.drain(session)) == list(range(16800, 64800))


async def test_corrections_are_frames_of_the_format_of_the_stream(rig: _Rig) -> None:
    """The sync loop counts in frames of the stream: a start error is that many of them."""
    await rig.start_hires()
    session = await rig.start_playing(frames=4 * 48000, chunk=_chunk_24)
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)

    rig.play(session, 1.5, late_ms=50)

    assert session.loop is not None
    assert session.loop.start_error_ms == pytest.approx(49.5)
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
    # 5 ms of audio as Sendspin delivers it, and as the player takes it
    delivered = struct.pack("<2i", 0x123456 << 8, -0x123456 << 8) * 480
    sent = (0x123456).to_bytes(3, "little") + (-0x123456).to_bytes(3, "little", signed=True)
    for first in (0, 960):
        session.add(
            AudioChunk(
                data=delivered,
                timestamp_us=EPOCH_US + first * 1_000_000 // 96000,
                duration_us=5000,
                byte_count=len(delivered),
            )
        )

    assert session.frames(10_000) == 960
    assert session.take(0, 9999) == sent * 480 + bytes(6 * 480) + sent * 480


async def test_a_backlog_is_only_converted_as_it_is_sent(rig: _Rig) -> None:
    """A late joiner is handed half a minute at once: none of it is converted on arrival."""
    await rig.start_hires()
    session = await rig.start_playing(frames=48000, chunk=_chunk_24)
    # the lead-in went out by the time the start is corrected
    rig.align(session)
    rig.clock.advance_us(1_000_000)
    with patch(f"{MODULE}._pack_24_bit", wraps=_pack_24_bit) as pack:
        backlog = [_chunk_24(rig.origin_us, second * 48000, 48000) for second in range(1, 30)]
        for chunk in backlog:
            rig.bridge._on_audio_chunk(chunk)
        pack.assert_not_called()
        assert list(session.chunks)[1:] == backlog

        block = rig.bridge._next_block(session)

    # one block of half a second, in 3 bytes per sample
    assert len(block) == 24000 * 6
    assert [len(converted.args[0]) for converted in pack.call_args_list] == [24000 * 8]
    assert _frames_24(block) == list(range(24000))


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


async def test_another_format_before_the_first_audio_starts_the_stream_again(rig: _Rig) -> None:
    """A stream that changes its format before its audio arrives is requested in that format."""
    await rig.start_hires()
    first = await rig.start_stream()

    # the same format: the session the stream start request began is the one of the stream
    rig.role.on_stream_start()
    await _settle()
    assert rig.session is first

    assert rig.role.set_preferred_format(None)
    rig.role.on_stream_start()
    await _settle()

    assert rig.session is not first
    assert first.ended
    assert (first.sample_rate, rig.session.sample_rate) == (48000, 44100)
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
    assert session.loop is not None
    for second in range(1, 30):
        rig.bridge._on_audio_chunk(_chunk(rig.origin_us, second * SECOND, SECOND))

    block = rig.bridge._next_block(session)
    assert len(block) == MAX_BLOCK_FRAMES * FRAME_SIZE
    sent = block + rig.align(session) + rig.drain(session)

    # the preroll counts: with it the player holds the lead ahead of the 300 ms it played,
    # and no more
    assert len(sent) == (13230 + START_LEAD_FRAMES - PREROLL_FRAMES) * FRAME_SIZE
    assert _frames(sent)[-1] == 13230 + START_LEAD_FRAMES - LEAD_IN_FRAMES - 1
    assert len(session.chunks) == 30
    # a second later it is given a second more
    rig.clock.advance_us(1_000_000)
    assert len(rig.drain(session)) == SECOND * FRAME_SIZE
    assert len(session.chunks) == 29


# ---------------------------------------------------------------- pacing


@pytest.mark.parametrize(
    ("first_chunk_ahead_us", "lead_in_us", "held_us"),
    [
        # all of the lead-in: what a player holds is the room for a late start short of it
        (2_000_000, LEAD_IN_US, LEAD_IN_US - ALIGN_ROOM_US),
        (1_100_000, 800_000, 300_000),
        # a lead-in with less than the room past the preroll: nothing more goes out
        (950_000, 650_000, PREROLL_US),
        (400_000, 100_000, PREROLL_US),
    ],
)
async def test_before_its_start_the_player_gets_the_silence_short_of_the_room_for_a_late_start(
    rig: _Rig, first_chunk_ahead_us: int, lead_in_us: int, held_us: int
) -> None:
    """Up to the start the stream is filled once, whatever the time: with silence it can spare."""
    session = await rig.start_playing(first_chunk_ahead_us=first_chunk_ahead_us)
    assert session.loop is not None
    assert session.first_audible_us is not None
    assert session.first_audible_us - session.start_us == lead_in_us
    assert ALIGN_LEAD_FRAMES < PREROLL_FRAMES

    sent = rig.drain(session)

    assert not any(sent)
    assert session.loop.frames_sent == SECOND * held_us // 1_000_000
    rig.clock.advance_us(session.start_us - rig.now_us)
    assert rig.drain(session) == b""


async def test_a_player_is_kept_a_short_lead_of_silence_from_when_it_is_told_to_start(
    rig: _Rig,
) -> None:
    """A player takes from its stream from that instant on, whatever it reports as played."""
    rig.knows_start_offset(110_000)
    # with a lead-in that leaves no silence to send ahead
    session = await rig.start_playing(first_chunk_ahead_us=800_000)
    assert session.loop is not None
    assert session.command_us == session.start_us - 110_000
    assert rig.drain(session) == b""

    rig.clock.advance_us(session.command_us - rig.now_us + 100_000)
    assert rig.drain(session) == bytes((SECOND * 3 // 10 - PREROLL_FRAMES) * FRAME_SIZE)
    # a late player holds what it was sent: what it reports does not hold the stream back
    rig.clock.advance_us(10_000)
    assert rig.drain(session) == bytes(SECOND // 100 * FRAME_SIZE)
    for _ in range(8):
        rig.play(session, 0.05, late_ms=1000, every_us=50_000)
        assert rig.drain(session) == bytes(SECOND // 20 * FRAME_SIZE)
    assert session.aligning
    assert session.loop.frames_sent == round(0.51 * SECOND) + ALIGN_LEAD_FRAMES


async def test_after_its_start_the_player_is_kept_the_lead_ahead(rig: _Rig) -> None:
    """The stream follows the timeline: what was played is replaced."""
    session = await rig.start_playing(frames=10 * SECOND)
    assert session.loop is not None
    rig.align(session)
    rig.drain(session)

    rig.clock.advance_us(20_000)
    assert len(rig.drain(session)) == 882 * FRAME_SIZE
    rig.clock.advance_us(3_000_000)
    rig.drain(session)

    assert session.loop.frames_sent == round(3.32 * SECOND) + START_LEAD_FRAMES


async def test_corrections_count_in_what_the_player_is_allowed(rig: _Rig) -> None:
    """Frames the loop dropped were not sent: the stream is that much less far along."""
    session = await rig.start_playing(frames=40 * SECOND)
    loop = session.loop
    assert loop is not None
    rig.align(session)
    rig.drain(session)
    # a player that is 3 ms late is sent less than the timeline: frames are dropped
    for _ in range(40):
        rig.play(session, 0.25, late_ms=3)
        rig.drain(session)

    dropped = loop.frames_consumed - loop.frames_sent
    assert loop.rate_ppm > 0
    assert dropped > 0
    played = (rig.now_us - session.start_us) * BRIDGE_SAMPLE_RATE // 1_000_000
    assert loop.frames_sent == played + START_LEAD_FRAMES - dropped


async def test_silence_keeps_a_player_fed_that_sendspin_has_nothing_for(rig: _Rig) -> None:
    """Without audio the stream is kept a little ahead with silence, on the timeline."""
    session = await rig.start_playing(frames=SECOND)
    loop = session.loop
    assert loop is not None
    sent = rig.align(session)
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)
    sent += rig.drain(session)
    assert _frames(sent) == [-1] * (LEAD_IN_FRAMES - PREROLL_FRAMES) + list(range(SECOND))

    # everything was sent: nothing more while the player still holds enough
    rig.clock.advance_us(900_000)
    assert rig.drain(session) == b""
    rig.clock.advance_us(300_000)
    starved = rig.drain(session)

    assert _frames(starved) == [-1] * (len(starved) // FRAME_SIZE)
    assert loop.frames_consumed == round(2.2 * SECOND) + STARVED_LEAD_FRAMES
    # audio that arrives late is played from where the stream is by then
    late_from = loop.frames_consumed - LEAD_IN_FRAMES
    rig.bridge._on_audio_chunk(_chunk(rig.origin_us, SECOND, SECOND))
    assert _frames(rig.drain(session))[0] == late_from


async def test_a_pcm_player_is_kept_the_starved_lead_after_a_source_that_ends(rig: _Rig) -> None:
    """A PCM player decodes all it got: silence keeps its stream half a second ahead, no more."""
    session = await rig.start_playing(frames=2 * SECOND, chunk=_music(2, "fade-out"))
    session.claimed = session.preroll_sent = False
    player = _BufferedPlayer(rig, session)
    rig.serve(session, player)
    await _settle()
    await rig.align_served(session)

    await player.play(4)

    assert player.least_held_us > STARVED_LEAD_US - 50_000 - _frame_us(BLOCK_FRAMES)
    assert rig.lead_frames(session) == STARVED_LEAD_FRAMES
    assert rig.session is session


async def test_audio_that_is_still_to_arrive_is_not_replaced_by_silence(rig: _Rig) -> None:
    """A player that holds more than it needs gets no silence in the place of audio to come."""
    tenth = SECOND // 10
    session = await rig.start_playing(frames=tenth)
    loop = session.loop
    assert loop is not None
    assert session.start_us - rig.now_us == 550_000

    # all there is of the stream so far, and no silence behind it
    sent = _frames(rig.align(session) + rig.drain(session))
    assert sent == [-1] * (LEAD_IN_FRAMES - PREROLL_FRAMES) + list(range(tenth))
    rig.bridge._on_audio_chunk(_chunk(rig.origin_us, tenth, tenth))
    assert _frames(rig.drain(session)) == list(range(tenth, 2 * tenth))

    # close to the end of what it holds the player is kept fed
    rig.clock.advance_us(session.start_us - rig.now_us + 1_100_000)
    assert rig.drain(session) == b""
    rig.clock.advance_us(100_000)
    starved = rig.drain(session)
    assert starved
    assert _frames(starved) == [-1] * (len(starved) // FRAME_SIZE)
    assert loop.frames_consumed == round(1.2 * SECOND) + STARVED_LEAD_FRAMES


async def test_a_player_on_its_timeline_gets_the_long_lead_within_three_seconds(
    rig: _Rig,
) -> None:
    """Once the sync loop has little left to correct, the stream goes seconds ahead and stays there."""
    session = await rig.start_playing(frames=40 * SECOND)
    loop = session.loop
    assert loop is not None
    rig.align(session)
    rig.drain(session)
    assert rig.lead_frames(session) == START_LEAD_FRAMES

    rig.follow(session)
    assert rig.now_us - session.start_us == 2_800_000
    sent = _frames(rig.drain(session))

    assert rig.lead_frames(session) == STREAM_LEAD_FRAMES
    assert sent[-1] - sent[0] == len(sent) - 1 + rig.dropped(session)
    # reports that are off after that take none of it back: the stream follows the timeline
    for _ in range(40):
        rig.play(session, 0.25, late_ms=8)
        rig.drain(session)
        assert rig.lead_frames(session) == STREAM_LEAD_FRAMES
    assert loop.error_ms is not None
    assert loop.error_ms > 5.0
    assert loop.on_timeline


@pytest.mark.parametrize("late_ms", [8, -8])
async def test_a_player_that_is_still_corrected_keeps_the_short_lead(
    rig: _Rig, late_ms: int
) -> None:
    """What the sync loop corrects is heard within a second and a half until the player is there."""
    session = await rig.start_playing(frames=40 * SECOND)
    loop = session.loop
    assert loop is not None
    rig.align(session)

    for _ in range(40):
        rig.play(session, 0.25, late_ms=late_ms)
        rig.drain(session)

    # ten seconds on, the loop took out 4 of its 8 ms
    assert not loop.on_timeline
    assert rig.lead_frames(session) == START_LEAD_FRAMES
    assert abs(rig.dropped(session)) == pytest.approx(4.0 * 44.1, abs=30)


async def test_a_player_that_does_not_show_its_start_keeps_the_short_lead(rig: _Rig) -> None:
    """A start error that is corrected in the audio is heard soon: the long lead comes after it."""
    session = await rig.start_playing(frames=40 * SECOND)
    loop = session.loop
    assert loop is not None
    # a player that does not show where it plays gets its audio after two seconds
    rig.clock.advance_us(session.start_us - rig.now_us + ALIGN_TIMEOUT_US + 20_000)
    before = _frames(rig.drain(session))
    assert not session.aligning
    assert rig.lead_frames(session) == START_LEAD_FRAMES

    # it turns out to play 50 ms late, which is left out of what goes out next
    rig.play(session, 1.0, late_ms=50)
    after = _frames(rig.drain(session))

    assert loop.start_error_ms == pytest.approx(49.5)
    assert after[0] == before[-1] + 1 + round(49.5 * 44.1)
    assert after == list(range(after[0], after[0] + len(after)))
    assert rig.lead_frames(session) == START_LEAD_FRAMES


async def test_a_source_that_delivers_late_at_a_start_is_played_from_where_it_is(rig: _Rig) -> None:
    """Audio that comes after it was due leaves a hole of silence, and the lead fills up behind it."""
    tenth = SECOND // 10
    session = await rig.start_playing(frames=tenth)
    loop = session.loop
    assert loop is not None
    sent = _frames(rig.align(session) + rig.drain(session))
    assert sent == [-1] * (LEAD_IN_FRAMES - PREROLL_FRAMES) + list(range(tenth))

    # the rest arrives 300 ms after it was due: the player is kept fed meanwhile
    rig.clock.advance_us(rig.origin_us + 400_000 - rig.now_us)
    starved = rig.drain(session)
    assert _frames(starved) == [-1] * (
        round(1.85 * SECOND) + STARVED_LEAD_FRAMES - loop.frames_sent + len(starved) // FRAME_SIZE
    )
    for second in range(30):
        rig.bridge._on_audio_chunk(_chunk(rig.origin_us, tenth + second * SECOND, SECOND))
    late = _frames(rig.drain(session))

    # each frame at its own time: 2.35 seconds into the stream is 0.9 seconds into its audio
    assert late[0] == round(0.9 * SECOND)
    assert late == list(range(late[0], late[0] + len(late)))
    assert rig.lead_frames(session) == START_LEAD_FRAMES


async def test_a_live_source_goes_to_the_player_as_it_arrives(rig: _Rig) -> None:
    """A source that is only the lead of its group ahead has no more to send: all of it goes out."""
    tenth = SECOND // 10
    ahead_us = BRIDGE_REQUIRED_LEAD_MS * 1000
    session = await rig.start_playing(first_chunk_ahead_us=ahead_us, frames=tenth)
    delivered = tenth

    def deliver() -> None:
        """Let Sendspin deliver what is due up to its lead from now."""
        nonlocal delivered
        while _frame_us(delivered) < rig.now_us + ahead_us - rig.origin_us:
            rig.bridge._on_audio_chunk(_chunk(rig.origin_us, delivered, tenth))
            delivered += tenth

    sent = rig.align(session)
    for _ in range(50):
        rig.play(session, 0.1, every_us=100_000)
        deliver()
        sent += rig.drain(session)
    assert session.loop is not None
    assert session.loop.on_timeline
    assert not session.chunks

    # the lead-in and then its audio without a gap, as far as the lead of the group
    audio = [frame for frame in _frames(sent) if frame >= 0]
    assert _frames(sent)[: LEAD_IN_FRAMES - PREROLL_FRAMES] == [-1] * (
        LEAD_IN_FRAMES - PREROLL_FRAMES
    )
    assert (audio[0], audio[-1], len(audio)) == (0, delivered - 1, delivered - rig.dropped(session))
    assert 0 <= rig.lead_frames(session) - SECOND * ahead_us // 1_000_000 < tenth


@pytest.mark.parametrize(
    ("lead_us", "stall_s", "plays_on"),
    [
        (STREAM_LEAD_US, 1, True),
        (STREAM_LEAD_US, 2, True),
        (STREAM_LEAD_US, 4, True),
        # the control: a lead of a second and a half does not last that long
        (1_500_000, 2, False),
        (1_500_000, 4, False),
    ],
)
async def test_a_player_plays_on_while_its_connection_takes_nothing(
    rig: _Rig, lead_us: int, stall_s: int, plays_on: bool
) -> None:
    """What a player holds lasts through a stall of seconds, and the stream is back at its lead after it."""
    with patch(f"{MODULE}.STREAM_LEAD_US", lead_us):
        lead_frames = SECOND * lead_us // 1_000_000
        session, player = await rig.serve_player()
        assert min(await player.play(3)) > 1_000_000
        assert rig.lead_frames(session) == lead_frames

        player.stalled = True
        held = await player.play(stall_s)
        player.stalled = False

        assert (min(held) > 0) is plays_on
        await player.play(1)
        assert rig.lead_frames(session) == lead_frames
        assert session.write_started_us is None
        # no more than the lead again, with no audio lost or sent twice but for the
        # single frames of the sync loop
        assert player.left_out() == rig.dropped(session)
        assert (
            player.audio()[-1] + 1 == round((stall_s + 4.3) * SECOND) + lead_frames - LEAD_IN_FRAMES
        )


async def test_a_stall_three_seconds_after_a_start_is_ridden_out(rig: _Rig) -> None:
    """A player that starts on its timeline holds the long lead 3 seconds after its start instant."""
    session, player = await rig.serve_player()
    assert session.loop is not None
    await player.play(2.4)
    assert not session.loop.on_timeline
    assert rig.lead_frames(session) == START_LEAD_FRAMES

    await player.play(0.3)
    assert rig.now_us - session.start_us == 3_000_000
    assert rig.lead_frames(session) == STREAM_LEAD_FRAMES
    player.stalled = True
    held = await player.play(4)
    player.stalled = False

    assert min(held) > 1_900_000
    await player.play(1)
    assert rig.lead_frames(session) == STREAM_LEAD_FRAMES
    assert player.left_out() == rig.dropped(session)


async def test_a_player_whose_buffers_are_full_gets_its_stream_as_it_plays(rig: _Rig) -> None:
    """A player that holds less than the lead is written to as it makes room, without a gap."""
    session, player = await rig.serve_player(capacity_us=2_000_000)
    # the long lead comes when the player is on its timeline
    await player.play(3)

    held = await player.play(5)

    # it holds what its buffers take, and the writer waits with one block for the room
    assert min(held) > 2_000_000 - 50_000
    assert max(held) < 2_000_000 + MAX_BLOCK_US
    assert session.write_started_us is not None
    assert session.loop is not None
    waiting = session.loop.frames_sent * FRAME_SIZE - len(player.body)
    # a block, with the frames the sync loop sent twice in it
    assert 0 < waiting <= (MAX_BLOCK_FRAMES + SECOND // 1000) * FRAME_SIZE
    assert rig.lead_frames(session) < STREAM_LEAD_FRAMES

    # with room for all of it the stream is at its lead again, and nothing was lost
    player.capacity_us = None
    await player.play(1)
    assert rig.lead_frames(session) == STREAM_LEAD_FRAMES
    assert player.left_out() == rig.dropped(session)
    assert player.audio()[-1] + 1 == round(9.3 * SECOND) + STREAM_LEAD_FRAMES - LEAD_IN_FRAMES


@pytest.mark.parametrize(("stall_s", "logged"), [(0.5, 0), (2, 1)])
async def test_a_write_that_waits_for_over_a_second_is_logged_once(
    rig: _Rig, caplog: pytest.LogCaptureFixture, stall_s: float, logged: int
) -> None:
    """A player that takes nothing for a while shows in the log when it takes its stream again."""
    caplog.set_level(logging.DEBUG, logger="test.squeezelite")
    _session, player = await rig.serve_player()
    await player.play(1)

    player.stalled = True
    await player.play(stall_s)
    player.stalled = False
    await player.play(1)

    assert caplog.text.count("Kitchen took nothing of its stream for") == logged
    if logged:
        assert "Kitchen took nothing of its stream for 2000 ms" in caplog.text


async def test_a_stream_that_runs_out_of_audio_is_logged_once(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """Silence in the place of audio is logged when it begins, not for every block of it."""
    caplog.set_level(logging.DEBUG, logger="test.squeezelite")
    session = await rig.start_playing(frames=SECOND)
    rig.align(session)
    message = "Sendspin has no audio for Kitchen: silence keeps its stream going"

    for _ in range(20):
        rig.clock.advance_us(100_000)
        rig.drain(session)
    assert caplog.text.count(message) == 1

    # and again when it runs out after more audio came
    rig.bridge._on_audio_chunk(_chunk(rig.origin_us, 2 * SECOND, SECOND))
    assert any(rig.drain(session))
    for _ in range(20):
        rig.clock.advance_us(100_000)
        rig.drain(session)
    assert caplog.text.count(message) == 2


def test_audio_that_went_out_is_kept_until_it_is_played() -> None:
    """What a player holds can be sent again: no more than the lead of it, and none that is over."""
    session = _session(EPOCH_US)
    chunks = [_chunk(EPOCH_US, second * SECOND, SECOND) for second in range(10)]
    for chunk in chunks:
        session.add(chunk)
    session.take(0, 8 * SECOND + 100)

    # 8.1 seconds went out: the player holds 6 of them at most
    assert list(session.unplayed(EPOCH_US)) == chunks[2:]
    assert list(session.unplayed(EPOCH_US + 3_500_000)) == chunks[3:]
    assert list(session.unplayed(EPOCH_US + 9_000_000)) == chunks[8:]


def test_audio_a_flac_player_did_not_decode_yet_is_kept_too() -> None:
    """A FLAC stream is ahead by more than its lead: a restart sends that again as well."""
    session = BridgeSession(replace(CD_FORMAT, content_type=ContentType.FLAC), 0)
    session.start_us = EPOCH_US
    # noise an encoder makes a quarter of: a player can not decode the last 0.7 seconds of it
    rng = random.Random(1)  # noqa: S311
    chunks = []
    for second in range(10):
        data = struct.pack(f"<{2 * SECOND}h", *(rng.randrange(-8, 8) for _ in range(2 * SECOND)))
        chunks.append(
            AudioChunk(
                data=data,
                timestamp_us=EPOCH_US + second * 1_000_000,
                duration_us=1_000_000,
                byte_count=len(data),
            )
        )
        session.add(chunks[-1])
    for cursor in range(0, 8 * SECOND + 100, TEN_MS):
        session.encode(session.take(cursor, TEN_MS))
    assert 0.6 * SECOND < session.frames_held < 0.8 * SECOND

    # 8.1 seconds went out: the player holds 6 of them, and the ones it can not decode
    assert list(session.unplayed(EPOCH_US)) == chunks[1:]
    assert list(session.unplayed(EPOCH_US + 3_500_000)) == chunks[3:]


# ---------------------------------------------------------------- start


async def test_the_player_is_told_when_to_start_on_its_own_clock(rig: _Rig) -> None:
    """The start instant goes to the player as a reading of its clock."""
    session = await rig.start_playing()

    assert session.started
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies(rig.origin_us - LEAD_IN_US))
    rig.client.unpause_at.assert_awaited_once_with(JIFFIES_AT_EPOCH + 550)


async def test_a_start_is_at_least_300_ms_ahead(rig: _Rig) -> None:
    """A first chunk that is due too soon starts late, without the audio before the start."""
    session = await rig.start_playing(first_chunk_ahead_us=100_000)

    assert MIN_START_AHEAD_US == 300_000
    assert session.start_us == rig.now_us + MIN_START_AHEAD_US
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies() + 300)
    # the stream starts 200 ms into its audio, which follows the preroll: that much is left
    # out, and what follows is on the timeline
    first, late_us = rig.first_audio(session, rig.align(session))
    assert first == round(0.45 * SECOND)
    assert late_us == 0


async def test_a_start_that_is_past_starts_as_soon_as_it_can(rig: _Rig) -> None:
    """A first chunk that is due in the past still starts 300 ms from now."""
    session = await rig.start_playing(first_chunk_ahead_us=-1_000_000)

    assert session.start_us == rig.now_us + MIN_START_AHEAD_US
    first, late_us = rig.first_audio(session, rig.align(session))
    assert first == round(1.55 * SECOND)
    assert late_us == 0


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
        session = await rig.start_playing(first_chunk_ahead_us=8_000_000 + LEAD_IN_US)
        for _ in range(10):
            await real_sleep(0)

    assert slept == [pytest.approx(3.5)]
    assert told_at == [session.start_us - MAX_START_AHEAD_US]
    rig.client.unpause_at.assert_awaited_once_with(JIFFIES_AT_EPOCH + 8000)


async def test_a_learned_start_offset_tells_the_player_earlier(rig: _Rig) -> None:
    """A player that starts late is told to start that much earlier."""
    rig.knows_start_offset(200_000)

    session = await rig.start_playing()

    assert session.start_us == rig.origin_us - LEAD_IN_US
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies(session.start_us) - 200)


async def test_a_learned_start_offset_counts_in_the_earliest_start(rig: _Rig) -> None:
    """The command has to be 300 ms ahead, so the start itself is the offset later."""
    rig.knows_start_offset(420_000)

    session = await rig.start_playing(first_chunk_ahead_us=100_000)

    assert session.start_us == rig.now_us + MIN_START_AHEAD_US + 420_000
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies() + 300)


# ---------------------------------------------------------------- start in silence


async def test_a_late_player_plays_its_first_audio_on_its_timeline(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """Silence a late player was not sent yet is left out: its audio is on time, all of it."""
    caplog.set_level(logging.DEBUG, logger="test.squeezelite")
    session = await rig.start_playing()
    assert session.loop is not None
    assert session.first_audible_us is not None
    assert session.first_audible_us - session.start_us == LEAD_IN_US

    sent = rig.align(session, late_ms=280) + rig.drain(session)

    assert session.loop.start_error_ms == pytest.approx(279.5)
    first, late_us = rig.first_audio(session, sent, late_ms=280)
    assert first == 0
    # half a millisecond is what an elapsed time in whole milliseconds hides
    assert late_us == pytest.approx(500, abs=25)
    # the lead-in of the player is that much shorter
    assert _frames(sent).index(0) == LEAD_IN_FRAMES - PREROLL_FRAMES - round(279.5 * 44.1)
    assert "its start was +279.5 ms off" in caplog.text
    assert "silence before it, 0 ms of its first audio left out" in caplog.text


async def test_a_delay_the_silence_does_not_cover_takes_the_first_audio(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """With too little silence left for it, a late player comes in later, on its timeline."""
    caplog.set_level(logging.DEBUG, logger="test.squeezelite")
    # a start that can only be 700 ms ahead of its audio
    session = await rig.start_playing(first_chunk_ahead_us=1_000_000)
    assert session.first_audible_us is not None
    assert session.start_us == rig.now_us + MIN_START_AHEAD_US
    assert session.first_audible_us - session.start_us == 700_000

    sent = rig.align(session, late_ms=280) + rig.drain(session)

    # the player was sent 800 ms of its stream by the time its start was measured, which
    # is 600 ms after it: the 280 ms left out on top of that end 380 ms into the audio
    first, late_us = rig.first_audio(session, sent, late_ms=280)
    assert first == round(0.8 * SECOND) + round(279.5 * 44.1) - round(0.7 * SECOND)
    assert late_us == pytest.approx(500, abs=25)
    assert "silence before it, 380 ms of its first audio left out" in caplog.text


async def test_an_early_player_plays_its_first_audio_on_its_timeline(rig: _Rig) -> None:
    """A player that starts ahead of its instant gets that much more silence."""
    # ahead of its instant is not ahead of the instant it was told to start
    rig.knows_start_offset(110_000)
    session = await rig.start_playing()
    assert session.loop is not None

    sent = rig.align(session, late_ms=-40) + rig.drain(session)

    assert session.loop.start_error_ms == pytest.approx(-40.5)
    first, late_us = rig.first_audio(session, sent, late_ms=-40)
    assert first == 0
    assert late_us == pytest.approx(500, abs=25)
    assert _frames(sent).index(0) == LEAD_IN_FRAMES - PREROLL_FRAMES + round(40.5 * 44.1)


async def test_positions_of_before_the_start_do_not_count_for_it(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """As logged on a player: its first reports of a stream show 5.6 s played after 0.25 s."""
    caplog.set_level(VERBOSE_LOG_LEVEL, logger="test.squeezelite")
    rig.knows_start_offset(180_000)
    session = await rig.start_playing()
    loop = session.loop
    assert loop is not None
    sent = rig.drain(session)
    rig.clock.advance_us(session.start_us - rig.now_us + 150_000)

    for _ in range(3):
        rig.clock.advance_us(50_000)
        played_ms = (rig.now_us - session.start_us) // 1000
        rig.bridge.handle_slim_event(rig.status(elapsed_ms=played_ms + 5368))
        sent += rig.drain(session)
    await _settle()

    # no start that is 5.4 s off, and no restart for it
    assert loop.start_error_ms is None
    assert rig.aligning()
    assert rig.session is session
    # the reports of what it plays, 20 ms late, are its start
    sent += rig.align(session, late_ms=20) + rig.drain(session)
    await _settle()
    assert loop.start_error_ms == pytest.approx(19.5)
    assert rig.session is session
    assert rig.client.play_url.await_count == 1
    first, late_us = rig.first_audio(session, sent, late_ms=20)
    assert first == 0
    assert late_us == pytest.approx(500, abs=25)
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 180_000 + 19_500 // 8
    told = "Kitchen reports 5568 ms played +200 ms after its start instant: -5368.5 ms off, not counted"
    assert told in caplog.text


async def test_positions_that_stay_impossible_restart_the_stream_after_the_timeout(
    rig: _Rig,
) -> None:
    """A player that keeps reporting what it can not have played is not left unmeasured."""
    rig.knows_start_offset(180_000)
    session = await rig.start_playing()
    loop = session.loop
    assert loop is not None
    rig.drain(session)
    rig.clock.advance_us(session.start_us - rig.now_us + 150_000)

    def _report_5_seconds_too_much() -> None:
        rig.clock.advance_us(50_000)
        played_ms = (rig.now_us - session.start_us) // 1000
        rig.bridge.handle_slim_event(rig.status(elapsed_ms=played_ms + 5368))
        rig.drain(session)

    # for as long as its start is measured, such a report does not count
    while rig.now_us - session.start_us <= ALIGN_TIMEOUT_US:
        _report_5_seconds_too_much()
        assert loop.start_error_ms is None
    assert rig.session is session

    # its audio went out uncorrected: from here on it is measured as it reports
    _report_5_seconds_too_much()
    assert not rig.aligning()
    for _ in range(3):
        _report_5_seconds_too_much()
    await _settle()

    assert loop.start_error_ms == pytest.approx(-5368.5)
    assert rig.session is not session
    assert rig.client.play_url.await_count == 2


async def test_a_report_that_is_dropped_with_the_ones_before_it_is_logged_as_not_counted(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A clock that starts over drops the reports collected so far, also at a repeated report."""
    caplog.set_level(VERBOSE_LOG_LEVEL, logger="test.squeezelite")
    session = await rig.start_playing()
    rig.drain(session)
    rig.clock.advance_us(session.start_us - rig.now_us + 250_000)
    rig.bridge.handle_slim_event(rig.status(elapsed_ms=230))
    assert caplog.records[-1].getMessage().endswith("counted, 1 in a row")

    rig.bridge._player_clock._generation += 1
    rig.bridge.handle_slim_event(rig.status(elapsed_ms=230))

    assert caplog.records[-1].getMessage().endswith("not counted")


async def test_a_player_that_moves_its_position_is_measured_from_where_it_settles(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """As logged on a player: its first reports count from 126 ms too early, then it steps."""
    caplog.set_level(VERBOSE_LOG_LEVEL, logger="test.squeezelite")
    rig.knows_start_offset(127_000)
    session = await rig.start_playing()
    loop = session.loop
    assert loop is not None
    sent = rig.drain(session)

    # what the player reported, and when: it is 11 ms late from its seventh report on
    for elapsed_ms, at_ms in (
        (0, -120),
        (0, -69),
        (100, -15),
        (100, -15),
        (148, 33),
        (200, 85),
        (123, 134),
        (176, 187),
        (234, 245),
        (283, 295),
        (333, 345),
    ):
        rig.clock.advance_us(session.start_us + at_ms * 1000 - rig.now_us)
        rig.bridge.handle_slim_event(rig.status(elapsed_ms=elapsed_ms))
        sent += rig.bridge._next_block(session)
    sent += rig.drain(session)

    assert not rig.aligning()
    assert loop.start_error_ms == pytest.approx(33.5 / 3)
    # the 11 ms are left out of its silence, where a start that looks early adds to it
    assert _frames(sent).index(0) == LEAD_IN_FRAMES - PREROLL_FRAMES - round(33.5 / 3 * 44.1)
    first, late_us = rig.first_audio(session, sent, late_ms=11)
    assert first == 0
    assert abs(late_us) < 1000
    lines = [
        record.getMessage().removeprefix("Kitchen reports ").split(": ")[1]
        for record in caplog.records
        if "after its start instant" in record.getMessage()
    ]
    assert lines == [
        "-120.5 ms off, not counted",
        "-69.5 ms off, not counted",
        "-115.5 ms off, not counted",
        "-115.5 ms off, not counted",
        "-115.5 ms off, not counted",
        "-115.5 ms off, counted, 1 in a row",
        "+10.5 ms off, not counted",
        "+10.5 ms off, not counted",
        "+10.5 ms off, counted, 1 in a row",
        "+11.5 ms off, counted, 2 in a row",
        "+11.5 ms off, counted, 3 in a row",
    ]


async def test_every_report_is_logged_while_a_start_is_measured(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A verbose log shows what a player reported from when it was told to start."""
    caplog.set_level(VERBOSE_LOG_LEVEL, logger="test.squeezelite")
    rig.knows_start_offset(110_000)
    session = await rig.start_playing()

    def _logged() -> list[tuple[int, str]]:
        return [
            (record.levelno, record.getMessage().removeprefix("Kitchen reports "))
            for record in caplog.records
            if "after its start instant" in record.getMessage()
        ]

    # not before it is told to start
    rig.clock.advance_us(session.command_us - rig.now_us - 1000)
    rig.bridge.handle_slim_event(rig.status())
    assert _logged() == []
    rig.clock.advance_us(51_000)
    rig.bridge.handle_slim_event(rig.status())
    rig.align(session, late_ms=30)

    assert _logged() == [
        (
            VERBOSE_LOG_LEVEL,
            f"{played} ms played {at:+d} ms after its start instant: {off} ms off, {counted}",
        )
        for played, at, off, counted in (
            (0, -60, "-60.5", "not counted"),
            (20, 50, "+29.5", "not counted"),
            (70, 100, "+29.5", "not counted"),
            (120, 150, "+29.5", "not counted"),
            (170, 200, "+29.5", "not counted"),
            (220, 250, "+29.5", "counted, 1 in a row"),
            (270, 300, "+29.5", "counted, 2 in a row"),
            (320, 350, "+29.5", "counted, 3 in a row"),
        )
    ]
    # nor once its start is corrected
    rig.play(session, 1.0, late_ms=30)
    assert len(_logged()) == 8


@pytest.mark.parametrize(("late_ms", "corrected_ms"), [(3, 2.5), (1, 0.0), (-2, -2.5)])
async def test_a_small_start_error_is_corrected_in_the_silence_too(
    rig: _Rig, late_ms: int, corrected_ms: float
) -> None:
    """A jump in silence is not heard, so a start that is a millisecond off gets one."""
    assert ALIGN_JUMP_MS == 1.0 < START_JUMP_MS
    session = await rig.start_playing()

    sent = rig.align(session, late_ms=late_ms) + rig.drain(session)

    lead_in = LEAD_IN_FRAMES - PREROLL_FRAMES - round(corrected_ms * 44.1)
    assert _frames(sent)[lead_in - 1 :][:2] == [-1, 0]


async def _first_frame_heard(lead_in_us: int, offset_us: int, late_ms: int) -> int:
    """
    Return the first frame of its audio a player is sent, on a rig of its own.

    :param lead_in_us: The silence its stream starts ahead of its audio.
    :param offset_us: How much later than told the player is known to start.
    :param late_ms: How much later than that it starts this time.
    """
    rig = _Rig()
    try:
        rig.knows_start_offset(offset_us)
        ahead_us = lead_in_us + MIN_START_AHEAD_US + offset_us
        session = await rig.start_playing(first_chunk_ahead_us=ahead_us)
        assert session.first_audible_us is not None
        assert session.first_audible_us - session.start_us == lead_in_us
        sent = rig.align(session, late_ms=late_ms) + rig.drain(session)
        return rig.first_audio(session, sent, late_ms=late_ms)[0]
    finally:
        await rig.close()


@pytest.mark.parametrize("lead_in_us", [LEAD_IN_US, 1_000_000, 700_000])
async def test_silence_sent_ahead_costs_a_late_player_no_audio(lead_in_us: int) -> None:
    """The room that is kept unsent is enough: as much audio is left out as without the fill."""
    # which holds for a start error that takes a quarter of a second of reports or more:
    # by then the short lead alone has the stream as far along as the silence sent ahead
    assert ALIGN_ROOM_US >= (LEAD_IN_US - ALIGN_LEAD_US - 250_000) // 2
    for offset_us in (0, 110_000, 300_000):
        for late_ms in (0, 150, 300, 400, 440, 460, 600):
            filled = await _first_frame_heard(lead_in_us, offset_us, late_ms)
            with patch(f"{MODULE}.ALIGN_ROOM_US", LEAD_IN_US):
                unfilled = await _first_frame_heard(lead_in_us, offset_us, late_ms)
            assert filled == unfilled, f"offset {offset_us} us, {late_ms} ms late"
    # a player that is 336 ms late, the most that was measured, with its usual 110 ms offset
    assert await _first_frame_heard(LEAD_IN_US, 110_000, 336) == 0
    # with less room a start that is later than the room loses the rest: 300 ms less 200 ms
    with patch(f"{MODULE}.ALIGN_ROOM_US", 200_000):
        assert await _first_frame_heard(LEAD_IN_US, 0, 300) == round(99.5 * 44.1)


async def test_no_audio_goes_out_until_the_start_is_corrected(rig: _Rig) -> None:
    """A player that is not on its timeline yet gets silence, also when its audio is due."""
    session = await rig.start_playing(first_chunk_ahead_us=800_000)
    loop = session.loop
    assert loop is not None
    pending_us = session.pending_us
    blocks = [rig.bridge._next_block(session)]
    rig.clock.advance_us(session.start_us - rig.now_us)

    # a player that starts 400 ms late, with its audio due 500 ms after its start instant
    for _ in range(ALIGN_ROUNDS):
        if loop.start_error_ms is not None:
            break
        blocks.append(rig.bridge._next_block(session))
        rig.play(session, 0.05, late_ms=400, every_us=50_000)
    assert loop.start_error_ms is not None

    assert rig.now_us - rig.origin_us == 200_000
    assert session.aligning
    assert not any(b"".join(blocks))
    assert session.pending_us == pending_us
    # what is left out of its audio is what the player holds by then, and the correction
    sent = b"".join(blocks) + rig.align(session, late_ms=400) + rig.drain(session)
    first, late_us = rig.first_audio(session, sent, late_ms=400)
    assert first == round(0.9 * SECOND) + round(399.5 * 44.1) - SECOND // 2
    assert late_us == pytest.approx(500, abs=25)


async def test_a_player_that_does_not_report_gets_its_audio_after_two_seconds(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """Without reports the stream follows the timeline, in silence for as long as it waits."""
    caplog.set_level(logging.DEBUG, logger="test.squeezelite")
    assert ALIGN_TIMEOUT_US == 2_000_000
    session = await rig.start_playing()
    loop = session.loop
    assert loop is not None
    sent = rig.drain(session)
    rig.clock.advance_us(session.start_us - rig.now_us)
    for _ in range(100):
        rig.clock.advance_us(20_000)
        sent += rig.drain(session)
    assert rig.aligning()
    assert not any(sent)
    assert loop.frames_sent == 2 * SECOND + ALIGN_LEAD_FRAMES

    rig.clock.advance_us(20_000)
    sent += rig.drain(session)

    assert not rig.aligning()
    # a short lead: its start error is still to be corrected in the audio
    assert loop.frames_sent == round(2.02 * SECOND) + START_LEAD_FRAMES
    # on the timeline as far as is known: 2.22 s into the stream is 0.77 s into its audio
    assert rig.first_audio(session, sent) == (round(0.77 * SECOND), 0)
    assert "its start not corrected, 770 ms of it left out" in caplog.text
    # once the player shows where it is, its start is corrected in the audio
    assert loop.start_jump_ms == START_JUMP_MS
    rig.play(session, 1.0, late_ms=5)
    assert loop.start_error_ms == pytest.approx(4.5)
    assert loop.pending_jump == 0
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 4500 // 8


async def test_the_player_is_asked_more_often_until_its_start_is_corrected(rig: _Rig) -> None:
    """The start is measured from a few reports in a row, which are asked for every 50 ms."""
    slow, fast = STATUS_POLL_INTERVAL, STATUS_POLL_INTERVAL_FAST
    assert (slow, fast) == (0.25, 0.05)
    real_sleep = asyncio.sleep
    intervals: list[float] = []
    asked = asyncio.Event()

    async def _sleep(delay: float) -> None:
        if delay < fast:
            await real_sleep(delay)
            return
        intervals.append(delay)
        asked.clear()
        await asked.wait()

    async def _next_interval() -> float:
        asked.set()
        await _settle()
        return intervals[-1]

    with (
        patch(f"{MODULE}.STATUS_POLL_INTERVAL", slow),
        patch(f"{MODULE}.STATUS_POLL_INTERVAL_FAST", fast),
        patch("asyncio.sleep", new=_sleep),
    ):
        session = await rig.start_stream()
        rig.fetched(session)
        # with its clock mapped, a player that waits for its stream to start is not hurried
        assert await _next_interval() == slow
        rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, SECOND))
        assert await _next_interval() == slow
        rig.event(SlimEventType.PLAYER_BUFFER_READY)
        await _settle()
        # nor one that was told when to start, until that instant
        assert session.started
        assert session.command_us - rig.now_us == 550_000
        assert await _next_interval() == slow
        rig.clock.advance_us(450_000)
        assert await _next_interval() == pytest.approx(0.1)
        rig.clock.advance_us(100_000)
        assert await _next_interval() == fast
        assert await _next_interval() == fast
        rig.align(session)
        assert await _next_interval() == slow
        assert await _next_interval() == slow


async def test_the_start_waits_for_the_clock_of_the_player(rig: _Rig) -> None:
    """Without a mapping of its clock the player can not be told when to start."""
    # a player that does not answer gets its stream request without a mapping
    with patch(f"{MODULE}.CLOCK_TIMEOUT_SECONDS", 0):
        session = await rig.start_stream(answers=False)
    rig.client.play_url.assert_awaited_once()
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
    await rig.start_stream(answers=False)
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
    await rig.start_stream(answers=False)
    assert rig.bridge._player_clock is not known
    assert rig.bridge._player_clock.to_jiffies(rig.now_us) is None


async def test_the_stream_is_requested_once_the_clock_of_the_player_is_mapped(rig: _Rig) -> None:
    """The player answers late while it sets a stream up: its clock is learned before."""
    session = await rig.start_stream(answers=False)

    for token in range(3):
        rig.client.play_url.assert_not_called()
        rig.bridge._pending_status[STATUS_TOKEN_BIT | 9000 + token] = rig.now_us
        rig.bridge.handle_slim_event(rig.status(token=STATUS_TOKEN_BIT | 9000 + token))
        rig.clock.advance_us(50_000)
        await _settle()

    assert session.clock_mapped.is_set()
    rig.client.play_url.assert_awaited_once()
    assert rig.owns_stream()


async def test_a_player_that_does_not_answer_gets_its_stream_requested_anyway(rig: _Rig) -> None:
    """The stream request waits for the clock of the player for a while, not for ever."""
    with patch(f"{MODULE}.CLOCK_TIMEOUT_SECONDS", 0.2):
        session = await rig.start_stream(answers=False)
        rig.client.play_url.assert_not_called()

        await _wait_for(lambda: rig.client.play_url.await_count == 1)

    assert not session.clock_mapped.is_set()
    assert rig.owns_stream()


async def test_a_restart_with_a_known_clock_requests_its_stream_at_once(rig: _Rig) -> None:
    """A session that restarts the stream has the mapping the one before it kept up."""
    first = await rig.start_playing()
    rig.clock.advance_us(first.start_us - rig.now_us + 2_000_000)

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert rig.session is not first
    assert rig.session.clock_mapped.is_set()
    assert rig.client.play_url.await_count == 2


async def test_a_new_stream_with_a_known_clock_requests_its_stream_at_once(rig: _Rig) -> None:
    """A mapping that was kept up is used for the next stream right away."""
    await rig.start_stream()
    rig.clock.advance_us(CLOCK_MAX_AGE_US - 1)

    session = await rig.start_stream(answers=False)

    assert session.clock_mapped.is_set()
    assert rig.client.play_url.await_count == 2


async def test_a_player_that_answers_late_while_it_sets_a_stream_up_starts_on_time(
    rig: _Rig,
) -> None:
    """
    The replies a player holds back while it sets a stream up do not move its start.

    Idle, the player answers a status request at once. From the stream request until its
    buffer is ready it answers none, and then all of them at once, each with the reading
    of its clock at that moment: their round trips are hundreds of milliseconds.
    """
    real_sleep = asyncio.sleep
    setup_us = 830_000
    held: list[int] = []
    requested_at: list[int] = []

    async def _sleep(delay: float) -> None:
        # time passes on the clock of the rig, as the test advances it
        wake_us = rig.now_us + round(delay * 1_000_000)
        await real_sleep(0)
        while rig.now_us < wake_us:
            await real_sleep(0)

    def _request_status(token: int) -> None:
        if requested_at:
            held.append(token)
        else:
            asyncio.get_running_loop().call_soon(
                rig.bridge.handle_slim_event, rig.status(token=token)
            )

    rig.client.request_status.side_effect = _request_status
    rig.client.play_url.side_effect = lambda **_kwargs: requested_at.append(rig.now_us)
    with (
        patch(f"{MODULE}.STATUS_POLL_INTERVAL", STATUS_POLL_INTERVAL),
        patch(f"{MODULE}.STATUS_POLL_INTERVAL_FAST", STATUS_POLL_INTERVAL_FAST),
        patch("asyncio.sleep", new=_sleep),
    ):
        session = await rig.start_stream(answers=False)
        rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, SECOND))
        while not requested_at or rig.now_us < requested_at[0] + setup_us:
            rig.clock.advance_us(10_000)
            await _settle()
        rig.fetched(session)
        rig.event(SlimEventType.PLAYER_BUFFER_READY)
        for token in held:
            rig.bridge.handle_slim_event(rig.status(token=token))
        await _settle()

    assert len(held) >= 3
    rig.client.unpause_at.assert_awaited_once()
    told = rig.client.unpause_at.await_args.args[0]
    assert abs(told - rig.jiffies(session.command_us)) <= 2
    # as early as a start can be: the 830 ms the player took leave 770 ms of lead-in
    assert session.start_us == rig.now_us + MIN_START_AHEAD_US
    assert session.first_audible_us is not None
    assert session.first_audible_us - session.start_us == 770_000


async def test_a_player_busy_with_another_stream_has_its_clock_mapped_from_prompt_replies(
    rig: _Rig,
) -> None:
    """
    The replies a player held back for a stream of its own do not map its clock either.

    The session begins while the player still sets up another stream: the replies it
    answers all at once when that one is ready come in while the session waits for the
    mapping, and the stream is requested once the prompt replies after them mapped it.
    """
    real_sleep = asyncio.sleep
    setup_us = 830_000
    busy_until_us = rig.now_us + 330_000
    held: list[int] = []
    requested_at: list[int] = []

    async def _sleep(delay: float) -> None:
        # time passes on the clock of the rig, as the test advances it
        wake_us = rig.now_us + round(delay * 1_000_000)
        await real_sleep(0)
        while rig.now_us < wake_us:
            await real_sleep(0)

    def _request_status(token: int) -> None:
        if rig.now_us < busy_until_us or requested_at:
            held.append(token)
        else:
            asyncio.get_running_loop().call_soon(
                rig.bridge.handle_slim_event, rig.status(token=token)
            )

    rig.client.request_status.side_effect = _request_status
    rig.client.play_url.side_effect = lambda **_kwargs: requested_at.append(rig.now_us)
    with (
        patch(f"{MODULE}.STATUS_POLL_INTERVAL", STATUS_POLL_INTERVAL),
        patch(f"{MODULE}.STATUS_POLL_INTERVAL_FAST", STATUS_POLL_INTERVAL_FAST),
        patch("asyncio.sleep", new=_sleep),
    ):
        session = await rig.start_stream(answers=False)
        rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, SECOND))
        while rig.now_us < busy_until_us:
            rig.clock.advance_us(10_000)
            await _settle()
        # the other stream is ready, and the replies held back for it come in
        rig.event(SlimEventType.PLAYER_BUFFER_READY)
        held_back, held = held, []
        for token in held_back:
            rig.bridge.handle_slim_event(rig.status(token=token))
        await _settle()
        assert not session.clock_mapped.is_set()
        rig.client.play_url.assert_not_called()

        while not requested_at:
            rig.clock.advance_us(10_000)
            await _settle()
        assert session.clock_mapped.is_set()
        while rig.now_us < requested_at[0] + setup_us:
            rig.clock.advance_us(10_000)
            await _settle()
        rig.fetched(session)
        rig.event(SlimEventType.PLAYER_BUFFER_READY)
        for token in held:
            rig.bridge.handle_slim_event(rig.status(token=token))
        await _settle()

    assert len(held_back) >= 3
    rig.client.play_url.assert_awaited_once()
    rig.client.unpause_at.assert_awaited_once()
    told = rig.client.unpause_at.await_args.args[0]
    assert abs(told - rig.jiffies(session.command_us)) <= 2
    # as early as a start can be: the other stream and this one leave 420 ms of lead-in
    assert session.start_us == rig.now_us + MIN_START_AHEAD_US
    assert session.first_audible_us is not None
    assert session.first_audible_us - session.start_us == 420_000


async def test_the_start_waits_for_the_first_chunk(rig: _Rig) -> None:
    """A player that is ready before Sendspin delivered anything starts with the first chunk."""
    session = await rig.start_stream()
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
    rig.bridge._on_audio_chunk(_chunk(rig.now_us + 2_000_000, 0, SECOND))

    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    await _settle()

    assert not session.buffer_ready
    rig.client.unpause_at.assert_not_called()


async def test_a_player_that_does_not_get_ready_has_its_stream_restarted(rig: _Rig) -> None:
    """A stream that the player never fetches is requested again, twice."""
    with patch(f"{MODULE}.START_TIMEOUT_SECONDS", 0.2):
        first = await rig.start_stream()
        await _wait_for(lambda: rig.client.play_url.await_count == 2)

        assert rig.session is not first
        assert first.ended
        await _wait_for(lambda: rig.client.play_url.await_count == 3)
        rig.client.stop.assert_not_called()
        # a third stream that played nothing: the player is given up on
        await _wait_for(lambda: not rig.owns_stream())

    assert rig.client.play_url.await_count == 3
    rig.client.stop.assert_awaited_once()


# ---------------------------------------------------------------- polling and reports


async def test_the_player_is_asked_for_its_status_with_tokens_of_its_own(rig: _Rig) -> None:
    """The tokens can not be those of the heartbeat of aioslimproto, which counts up from 1."""
    with patch(f"{MODULE}.STATUS_POLL_INTERVAL_FAST", 0):
        await rig.start_stream(answers=False)
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
    with patch(f"{MODULE}.STATUS_POLL_INTERVAL_FAST", 0):
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
    await rig.start_stream(answers=False)
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
    await rig.start_stream(answers=False)
    asked = list(rig.bridge._pending_status)
    assert asked == [STATUS_TOKEN_BIT | 1]

    for token in (0, 1, 2, 3, STATUS_TOKEN_BIT | 8):
        rig.bridge.handle_slim_event(rig.status(token=token))

    assert rig.bridge._player_clock.to_jiffies(rig.now_us) is None
    assert list(rig.bridge._pending_status) == asked


async def test_requests_without_a_reply_are_forgotten(rig: _Rig) -> None:
    """Replies come in order, so a reply closes the requests before it; the rest is capped."""
    await rig.start_stream(answers=False)
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
    rig.align(session)
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)

    rig.play(session, 1.5, late_ms=3)

    # elapsed is truncated to whole milliseconds, which the loop takes for half of one
    assert session.loop.start_error_ms == pytest.approx(-0.5)
    assert session.loop.error_ms == pytest.approx(2.5)
    assert session.loop.rate_ppm == pytest.approx(250.0)


async def test_corrections_of_the_loop_are_in_the_stream(rig: _Rig) -> None:
    """A late player is sent audio with frames taken out."""
    session = await rig.start_playing(frames=20 * SECOND)
    rig.align(session)
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
    """Some of how late a player starts is taken off the instant it is told the next time."""
    session = await rig.start_playing()
    rig.clock.advance_us(session.start_us - rig.now_us + 1_000_000)

    rig.play(session, 1.5, late_ms=50)

    assert rig.manager.get_start_offset_us(PLAYER_ID) == 49_500 // 8
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
    assert session.command_us == session.start_us - 49_500 // 8
    rig.client.unpause_at.assert_awaited_once_with(rig.jiffies(session.start_us) - 6)


async def test_a_start_offset_settles_on_the_shortest_the_player_takes(rig: _Rig) -> None:
    """A start that was early is taken whole at once, a late one for an eighth."""
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 0
    assert rig.manager.learn_start_offset(PLAYER_ID, 80.0) == 10_000
    assert rig.manager.learn_start_offset(PLAYER_ID, 8.0) == 11_000
    assert rig.manager.learn_start_offset(PLAYER_ID, -6.0) == 5_000
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 5_000
    assert rig.manager.get_start_offset_us("other") == 0


async def test_a_start_offset_is_never_negative(rig: _Rig) -> None:
    """A player does not start before it is told to, whatever one start looks like."""
    rig.knows_start_offset(130_000)

    assert rig.manager.learn_start_offset(PLAYER_ID, -1200.0) == 0
    assert rig.manager.get_start_offset_us(PLAYER_ID) == 0
    # its next start is told at its instant, and measured as that of a player without an offset
    session = await rig.start_playing()
    assert session.command_us == session.start_us
    assert rig.manager.learn_start_offset(PLAYER_ID, 136.0) == 17_000


async def test_a_slow_start_hardly_moves_the_next_one(rig: _Rig) -> None:
    """After a start that took far longer than usual, the next one is early by little."""
    # a player that usually starts 110 ms late, and 436 ms after a change of sample rate
    rig.knows_start_offset(110_000)

    assert rig.manager.learn_start_offset(PLAYER_ID, 436.0 - 110.0) == 150_750
    # its next start is early by an eighth of that, well within the audio it holds
    early_us = 150_750 - 110_000
    assert early_us < ALIGN_LEAD_US // 4
    # and takes the offset back to what the player usually does
    assert rig.manager.learn_start_offset(PLAYER_ID, -early_us / 1000) == 110_000


async def test_a_start_offset_is_kept_in_the_cache_when_it_changes(rig: _Rig) -> None:
    """What was learned is stored for the next run of the server, once per start that moved it."""
    rig.manager.learn_start_offset(PLAYER_ID, 420.0)
    await _settle()

    rig.mass.cache.set.assert_awaited_once_with(
        key=PLAYER_ID,
        data=52_500,
        provider="squeezelite",
        category=CACHE_CATEGORY_START_OFFSET,
    )
    rig.manager.learn_start_offset(PLAYER_ID, 0.0)
    await _settle()
    assert rig.mass.cache.set.await_count == 1
    rig.manager.learn_start_offset(PLAYER_ID, -10.0)
    await _settle()
    assert rig.mass.cache.set.await_count == 2
    assert rig.mass.cache.set.await_args.kwargs["data"] == 42_500


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
    # what the player does from here on moves it, as an offset that was learned
    assert rig.manager.learn_start_offset(PLAYER_ID, -10.0) == 410_000


async def test_a_start_offset_that_is_known_is_not_restored(rig: _Rig) -> None:
    """The cache is read once for a player: what was learned since is newer."""
    rig.mass.cache.get.return_value = 420_000
    rig.manager.learn_start_offset(PLAYER_ID, 240.0)

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
    # and learns from its first start as from any other
    assert rig.manager.learn_start_offset(PLAYER_ID, 80.0) == 10_000


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
    # the player answers its status requests all along
    rig.map_clock()

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
    # as its manager records once the bridge registered
    rig.sendspin.hold_bridge_client(CLIENT_ID, PLAYER_ID)
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


# ---------------------------------------------------------------- now playing


def _media(title: str) -> PlayerMedia:
    """Return what a player plays for a queue item, as the player of the bridge shows it."""
    return PlayerMedia(
        uri=f"library://track/{title}",
        title=title,
        artist="The Band",
        album="The Album",
        image_url=f"http://192.168.1.10:8095/imageproxy?title={title}",
        duration=240,
        source_id="queue-of-the-group",
        queue_item_id=f"item-{title}",
    )


def _metadata(title: str, *, samplerate: str = "44100", samplesize: str = "16") -> dict[str, Any]:
    """
    Return the metadata a slimproto player gets for that media.

    :param title: The title of the media.
    :param samplerate: The sample rate of the stream of the bridge, in Hz.
    :param samplesize: The bit depth of that stream.
    """
    return {
        "item_id": f"library://track/{title}",
        "title": title,
        "album": "The Album",
        "artist": "The Band",
        "image_url": f"http://192.168.1.10:8095/imageproxy?title={title}",
        "duration": 240,
        "samplerate": samplerate,
        "samplesize": samplesize,
    }


async def test_the_stream_starts_with_what_the_group_plays(rig: _Rig) -> None:
    """The player gets the track of its player with the stream, not the queue item of it."""
    rig.protocol.player.current_media = _media("One")

    await rig.start_stream()

    assert rig.client.play_url.await_args.kwargs["metadata"] == _metadata("One")


async def test_a_track_change_reaches_the_player(rig: _Rig) -> None:
    """The stream plays on from track to track: the player is told what it plays now."""
    rig.protocol.player.current_media = _media("One")
    await rig.start_playing()

    rig.protocol.player.current_media = _media("Two")
    rig.bridge.update_now_playing()
    await _settle()

    rig.client.update_metadata.assert_awaited_once_with(_metadata("Two"))
    # also while the stream is not there yet: what its request said may be old by then
    rig.client.update_metadata.reset_mock()
    await rig.start_stream()
    rig.protocol.player.current_media = None
    rig.bridge.update_now_playing()
    await _settle()
    rig.client.update_metadata.assert_awaited_once_with({})


async def test_a_track_change_while_the_stream_is_requested_reaches_the_player(rig: _Rig) -> None:
    """The request of a stream holds the track of its moment: a change during it follows."""
    rig.protocol.player.current_media = _media("One")

    async def _play_url(**_kwargs: Any) -> None:
        # the group moves on while the request goes out to the player
        rig.protocol.player.current_media = _media("Two")

    rig.client.play_url.side_effect = _play_url
    await rig.start_stream()

    assert rig.client.play_url.await_args.kwargs["metadata"] == _metadata("One")
    rig.client.update_metadata.assert_awaited_once_with(_metadata("Two"))


async def test_the_player_shows_the_format_of_the_stream_of_the_bridge(rig: _Rig) -> None:
    """Clients of the player show the format of the PCM stream the bridge plays on it."""
    await rig.start_hires()
    rig.protocol.player.current_media = _media("One")

    await rig.start_stream()
    rig.protocol.player.current_media = _media("Two")
    rig.bridge.update_now_playing()
    await _settle()

    assert rig.client.play_url.await_args.kwargs["metadata"] == _metadata(
        "One", samplerate="48000", samplesize="24"
    )
    rig.client.update_metadata.assert_awaited_once_with(
        _metadata("Two", samplerate="48000", samplesize="24")
    )


async def test_a_bridge_that_does_not_play_shows_nothing(rig: _Rig) -> None:
    """What the player of an idle bridge plays is its own: the bridge keeps out of it."""
    rig.protocol.player.current_media = _media("One")

    rig.bridge.update_now_playing()
    await _settle()

    rig.client.update_metadata.assert_not_called()


@pytest.mark.parametrize(
    "end",
    ["taken back", "taken out of its group", "bridge removed", "ended before its start", "gave up"],
)
async def test_nothing_is_left_on_the_player_when_its_stream_is_over(rig: _Rig, end: str) -> None:
    """A stop keeps the media of a player: the bridge has it forgotten with its session."""
    client = rig.grouped()
    session = await rig.start_playing()
    rig.client.clear_media.assert_not_called()

    if end == "taken back":
        assert rig.bridge.stop_streaming()
    elif end == "taken out of its group":
        # the player lists the stream it plays
        rig.client.current_media.url = (
            f"{BASE_URL}{BRIDGE_STREAM_PATH}?player_id={PLAYER_ID}&sid={session.session_id}"
        )
        # as the group removes a member: its stream ends, it moves on, then it is stopped
        rig.bridge._on_stream_end()
        client.moved_to(MagicMock(clients=[client], has_active_stream=False))
        rig.bridge._on_explicit_stop()
    elif end == "bridge removed":
        await rig.bridge.stop()
    elif end == "ended before its start":
        session.started = False
        rig.bridge._on_stream_end()
    else:
        for _ in range(MAX_QUICK_FAILURES - 1):
            await rig.run_dry()
            rig.client.clear_media.assert_not_called()
        await rig.run_dry()
    await _settle()

    assert not rig.owns_stream()
    rig.client.clear_media.assert_awaited_once_with()


async def test_a_stream_that_plays_out_keeps_its_track_until_the_end(rig: _Rig) -> None:
    """The end of the queue is played out with its track shown, and forgotten after it."""
    await rig.start_playing()

    rig.bridge._on_stream_end()
    await _settle()

    rig.client.clear_media.assert_not_called()
    rig.client.state = SlimPlayerState.STOPPED
    rig.event(SlimEventType.PLAYER_UPDATED)
    await _settle()
    assert not rig.owns_stream()
    rig.client.clear_media.assert_awaited_once_with()


@pytest.mark.parametrize("replaced_by", ["a new stream", "a restart"])
async def test_a_session_that_is_replaced_keeps_what_the_player_shows(
    rig: _Rig, replaced_by: str
) -> None:
    """A seek or a restart goes on with the same track: the player is not blanked in between."""
    first = await rig.start_playing()

    if replaced_by == "a new stream":
        rig.bridge._on_stream_start(MagicMock())
    else:
        rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert rig.session is not first
    rig.client.clear_media.assert_not_called()


async def test_the_player_shows_the_track_of_the_group_and_keeps_it_on_a_stop(rig: _Rig) -> None:
    """With a real slimproto client: what its status and display report, start to stop."""
    reader, _sent = _real_client(rig)
    client = rig.client

    def shown() -> tuple[str, Any] | None:
        """Return the url and metadata of the media the client reports for its player."""
        media = client.current_media
        return (media.url, media.metadata) if media else None

    try:
        rig.protocol.player.current_media = _media("One")
        session = await rig.start_playing()
        url = f"{BASE_URL}{BRIDGE_STREAM_PATH}?player_id={PLAYER_ID}&sid={session.session_id}"
        # nothing of the stream shows before the player started it
        assert shown() is None
        await _player_reports(reader, b"STMs")
        assert shown() == (url, _metadata("One"))

        rig.protocol.player.current_media = _media("Two")
        rig.bridge.update_now_playing()
        await _settle()
        assert shown() == (url, _metadata("Two"))

        rig.bridge._on_stream_end()
        rig.bridge._on_explicit_stop()
        await _settle()
        assert client.state == SlimPlayerState.STOPPED
        assert shown() == (url, _metadata("Two"))
    finally:
        client._reader_task.cancel()


async def test_the_play_key_of_a_player_resumes_its_stopped_group(rig: _Rig) -> None:
    """A player only sends play for a track its status lists: a group stop leaves it one."""
    reader, sent = _real_client(rig)
    client = rig.client
    server = MagicMock(logger=logging.getLogger("test.aioslimproto"))
    server.get_player.side_effect = {PLAYER_ID: client}.get
    cli = SlimProtoCLI(
        server,
        cli_port=None,
        cli_port_json=None,
        command_handler=partial(SqueezelitePlayerProvider._handle_cli_command, rig.provider),
    )
    rig.protocol.player.player_id = "kitchen"
    rig.mass.players.cmd_play = AsyncMock()
    try:
        rig.protocol.player.current_media = _media("One")
        rig.grouped()
        await rig.start_playing()
        await _player_reports(reader, b"STMs")

        rig.bridge._on_stream_end()
        rig.bridge._on_explicit_stop()
        await _settle()
        # the player confirms the stop of its stream
        await _player_reports(reader, b"STMf")
        status = await cli._handle_command((PLAYER_ID, ["status", "-", 10, "menu:menu"]))

        assert (status["mode"], status["playlist_tracks"]) == ("stop", 1)
        assert [item["track"] for item in status["item_loop"]] == ["One"]
        sent.clear()
        await cli._handle_command((PLAYER_ID, ["mode", "play"]))
        # the player of the bridge, which Music Assistant hands on to the player that
        # owns its playback
        rig.mass.players.cmd_play.assert_awaited_once_with("kitchen")
        assert _stream_commands(sent) == []
    finally:
        client._reader_task.cancel()


@pytest.mark.parametrize("leaves_by", ["taken out of the group", "bridge removed"])
async def test_a_player_that_leaves_its_stopped_group_shows_nothing(
    rig: _Rig, leaves_by: str
) -> None:
    """The stopped track of a group is not left on a player that is no longer in it."""
    reader, _sent = _real_client(rig)
    client = rig.client
    try:
        rig.protocol.player.current_media = _media("One")
        sendspin_client = rig.grouped()
        await rig.start_playing()
        await _player_reports(reader, b"STMs")
        rig.bridge._on_stream_end()
        rig.bridge._on_explicit_stop()
        await _settle()
        await _player_reports(reader, b"STMf")
        assert client.current_media is not None

        if leaves_by == "taken out of the group":
            # as the group removes a member, with no stream left to end or stop
            rig.bridge._on_stream_end()
            sendspin_client.moved_to(MagicMock(clients=[sendspin_client], has_active_stream=False))
            rig.bridge._on_explicit_stop()
        else:
            await rig.bridge.stop()
        await _settle()

        assert client.current_media is None
    finally:
        client._reader_task.cancel()


@pytest.mark.parametrize("change", ["put into a group", "bridge removed"])
async def test_a_player_keeps_a_track_of_its_own_when_its_bridge_moves_on(
    rig: _Rig, change: str
) -> None:
    """What the player plays on its own is not for the bridge to clear."""
    reader, _sent = _real_client(rig)
    client = rig.client
    try:
        sendspin_client = rig.grouped()
        sendspin_client.moved_to(MagicMock(clients=[sendspin_client], has_active_stream=False))
        await client.play_url(f"{BASE_URL}/track.flac")
        await _player_reports(reader, b"STMs")
        media = client.current_media
        assert media is not None

        if change == "put into a group":
            sendspin_client.moved_to(
                MagicMock(clients=[sendspin_client, MagicMock()], has_active_stream=False)
            )
        else:
            await rig.bridge.stop()
        await _settle()

        assert client.current_media is media
    finally:
        client._reader_task.cancel()


# ---------------------------------------------------------------- position


def _media_at(title: str, position_s: int, *, seconds_ago: float = 0) -> PlayerMedia:
    """
    Return that media at a position in its track, as Music Assistant shows it.

    :param title: The title of the media.
    :param position_s: The position in the track.
    :param seconds_ago: How long ago that position was taken.
    """
    return replace(
        _media(title),
        elapsed_time=position_s,
        elapsed_time_last_updated=time.time() - seconds_ago,
    )


def _cli_client(rig: _Rig, player_id: str) -> tuple[asyncio.StreamReader, SlimClient]:
    """
    Give the player of a rig a real slimproto client, with the id the CLI knows it by.

    Its heartbeats do not reach the bridge: they are on the clock of the real client, and
    the bridge of a rig runs on a manual one.

    :param rig: The rig whose player gets the client.
    :param player_id: The slimproto id of the player.
    :return: The reader to feed what the player sends, and the client.
    """
    reader, _sent = _real_client(rig)
    client: SlimClient = rig.client
    client._player_id = player_id
    report = client.callback

    def _report(_client: SlimClient, event_type: SlimEventType, data: Any = None) -> None:
        if event_type != SlimEventType.PLAYER_HEARTBEAT:
            report(_client, event_type, data)

    client.callback = _report
    return reader, client


def _cli(*rigs: _Rig) -> SlimProtoCLI:
    """
    Return the CLI of aioslimproto for the clients of rigs, with the elapsed lookup of the provider.

    :param rigs: The rigs whose players the CLI answers for.
    """
    server = MagicMock(logger=logging.getLogger("test.aioslimproto"))
    server.get_player.side_effect = {rig.client.player_id: rig.client for rig in rigs}.get
    provider = MagicMock()
    provider.bridge_manager.get_bridge.side_effect = {
        rig.client.player_id: rig.bridge for rig in rigs
    }.get
    cli = SlimProtoCLI(server, cli_port=None, cli_port_json=None)
    cli.elapsed_lookup = partial(SqueezelitePlayerProvider._lookup_elapsed, provider)
    return cli


async def _reported_position(cli: SlimProtoCLI, client: SlimClient) -> tuple[int, int | None]:
    """Return the position the CLI reports for a player: to `time ?`, and in its status."""
    answer = await cli._handle_command((client.player_id, ["time", "?"]))
    status = await cli._handle_command((client.player_id, ["status", "-", 1, "tags:d"]))
    # a status without a track has no time
    return answer["_time"], status.get("time")


async def test_the_players_of_a_group_report_its_position_in_the_track() -> None:
    """
    A player reports the position Music Assistant shows, whenever its stream started.

    The group plays the track at 2:10. One player started its stream with the track, the
    other joined the group 30 s later: its stream has played 1:40.
    """
    first, late = _Rig(), _Rig()
    first_reader, first_client = _cli_client(first, "aa:bb:cc:dd:ee:01")
    late_reader, late_client = _cli_client(late, "aa:bb:cc:dd:ee:02")
    try:
        for rig, reader in ((first, first_reader), (late, late_reader)):
            rig.protocol.player.current_media = _media("One")
            await rig.start_playing()
            await _player_reports(reader, b"STMs")
        # the members of a group show the media of the player that leads it
        media = _media_at("One", 130)
        first.protocol.player.current_media = late.protocol.player.current_media = media
        await _player_reports(first_reader, b"STMt", elapsed_ms=130_000)
        await _player_reports(late_reader, b"STMt", elapsed_ms=100_000)
        cli = _cli(first, late)

        assert await _reported_position(cli, first_client) == (130, 130)
        assert await _reported_position(cli, late_client) == (130, 130)
        # the bridge syncs a stream by the elapsed time the player counts
        assert (int(first_client.elapsed_seconds), int(late_client.elapsed_seconds)) == (130, 100)
    finally:
        for rig in (first, late):
            rig.client._reader_task.cancel()
            await rig.close()


async def test_a_player_reports_its_position_in_the_next_track_of_its_stream(rig: _Rig) -> None:
    """The stream of the bridge plays on into the next track: the position is in that track."""
    reader, client = _cli_client(rig, PLAYER_ID)
    try:
        rig.protocol.player.current_media = _media("One")
        await rig.start_playing()
        await _player_reports(reader, b"STMs")

        # 20 s into Two, which follows the 4:00 of One in the same stream
        rig.protocol.player.current_media = _media_at("Two", 20)
        rig.bridge.update_now_playing()
        await _settle()
        await _player_reports(reader, b"STMt", elapsed_ms=260_000)

        assert await _reported_position(_cli(rig), client) == (20, 20)
        assert int(client.elapsed_seconds) == 260
    finally:
        client._reader_task.cancel()


async def test_a_player_of_a_stopped_group_reports_where_the_group_stopped(rig: _Rig) -> None:
    """
    A pause of a Sendspin group stops it: its players report the position it stopped at.

    The stream of the player played One and 20 s of Two before the group stopped, a
    minute ago.
    """
    reader, client = _cli_client(rig, PLAYER_ID)
    try:
        rig.protocol.player.current_media = _media("One")
        await rig.start_playing()
        await _player_reports(reader, b"STMs")
        rig.protocol.player.current_media = _media_at("Two", 20)
        rig.bridge.update_now_playing()
        await _settle()
        await _player_reports(reader, b"STMt", elapsed_ms=260_000)

        rig.bridge._on_stream_end()
        rig.bridge._on_explicit_stop()
        await _settle()
        await _player_reports(reader, b"STMf")
        rig.protocol.player.current_media = _media_at("Two", 20, seconds_ago=60)

        assert not rig.owns_stream()
        assert await _reported_position(_cli(rig), client) == (20, 20)
    finally:
        client._reader_task.cancel()


@pytest.mark.parametrize(
    ("plays", "reported"),
    [
        pytest.param("a stream of its own", (100, 100), id="a stream of its own"),
        pytest.param("nothing", (100, None), id="nothing, out of the group it stopped with"),
    ],
)
async def test_a_player_reports_its_own_elapsed_time_without_a_track_of_its_bridge(
    rig: _Rig, plays: str, reported: tuple[int, int | None]
) -> None:
    """
    A player that plays a track of its own, or has no track of its bridge, reports its own time.

    :param plays: What the player plays: a stream of its own, or nothing after it left the
        group it stopped with.
    :param reported: What it reports to `time ?`, and in its status.
    """
    reader, client = _cli_client(rig, PLAYER_ID)
    try:
        if plays == "a stream of its own":
            await client.play_url(f"{BASE_URL}/track.flac")
            await _player_reports(reader, b"STMs")
            await _player_reports(reader, b"STMt", elapsed_ms=100_000)
        else:
            rig.protocol.player.current_media = _media("One")
            sendspin_client = rig.grouped()
            await rig.start_playing()
            await _player_reports(reader, b"STMs")
            await _player_reports(reader, b"STMt", elapsed_ms=100_000)
            rig.bridge._on_stream_end()
            rig.bridge._on_explicit_stop()
            await _settle()
            await _player_reports(reader, b"STMf")
            # as the group removes a member, with no stream left to end or stop
            rig.bridge._on_stream_end()
            sendspin_client.moved_to(MagicMock(clients=[sendspin_client], has_active_stream=False))
            rig.bridge._on_explicit_stop()
            await _settle()
        rig.protocol.player.current_media = _media_at("One", 130)

        assert not rig.owns_stream()
        assert await _reported_position(_cli(rig), client) == reported
    finally:
        client._reader_task.cancel()


# ---------------------------------------------------------------- failure paths


async def test_an_underrun_restarts_the_stream_on_the_timeline(rig: _Rig) -> None:
    """A player that ran dry is off the timeline: it gets a new stream of what is left."""
    first = await rig.start_playing(frames=SECOND)
    for second in range(1, 6):
        rig.bridge._on_audio_chunk(_chunk(rig.origin_us, second * SECOND, SECOND))
    rig.align(first)
    rig.clock.advance_us(first.start_us - rig.now_us + 1_950_000)
    rig.drain(first)

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    second_session = rig.session
    assert second_session is not first
    assert first.ended
    assert rig.client.play_url.await_count == 2
    rig.client.stop.assert_not_called()
    # the audio that was not played out yet goes on in the new stream, at its own time:
    # 2 seconds of it went out, and the player was half a second into it
    assert len(second_session.chunks) == 6
    rig.fetched(second_session)
    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    await _settle()
    # as soon as it can, which is 0.8 seconds into its audio
    assert second_session.start_us == rig.now_us + MIN_START_AHEAD_US
    assert second_session.start_us == rig.origin_us + 800_000
    # the audio is heard from where it is once the preroll is played
    sent = rig.align(second_session) + rig.drain(second_session)
    assert rig.first_audio(second_session, sent) == (round(1.05 * SECOND), 0)


async def test_a_restart_into_audio_that_is_due_goes_out_a_lead_ahead_from_its_first_write(
    rig: _Rig,
) -> None:
    """A restart has no silence before its audio to correct its start in: it is corrected in the audio."""
    first = await rig.start_playing(frames=SECOND)
    for second in range(1, 6):
        rig.bridge._on_audio_chunk(_chunk(rig.origin_us, second * SECOND, SECOND))
    rig.align(first)
    rig.clock.advance_us(first.start_us - rig.now_us + 1_950_000)
    rig.drain(first)
    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()
    session = rig.session
    rig.fetched(session)
    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    await _settle()
    loop = session.loop
    assert loop is not None
    assert session.first_audible_us is not None
    assert session.first_audible_us - session.start_us == -800_000

    sent = rig.drain(session)

    # before its start instant: its audio on the timeline from the end of the preroll on,
    # as far as the lead
    first_frame = round(1.05 * SECOND)
    assert _frames(sent) == list(
        range(first_frame, first_frame + START_LEAD_FRAMES - PREROLL_FRAMES)
    )
    assert rig.first_audio(session, sent) == (first_frame, 0)
    rig.clock.advance_us(session.start_us - rig.now_us)
    assert rig.lead_frames(session) == START_LEAD_FRAMES
    # a player that starts 5 ms late is corrected by the sync loop, not in a jump that is heard
    rig.play(session, 1.0, late_ms=5)
    assert loop.start_error_ms == pytest.approx(4.5)
    assert loop.pending_jump == 0


async def test_a_restarted_player_plays_on_through_a_stall_right_after_its_start(rig: _Rig) -> None:
    """A player that takes nothing for 1.3 s from the start of its restarted stream does not run dry."""
    first, player = await rig.serve_player()
    await player.play(3)
    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    rig.map_clock()
    await _settle()
    session = rig.session
    assert session is not first
    restarted = _BufferedPlayer(rig, session)
    rig.serve(session, restarted)
    await _settle()
    rig.event(SlimEventType.PLAYER_BUFFER_READY)
    await _settle()
    assert session.first_audible_us is not None
    assert session.first_audible_us < session.start_us
    rig.clock.advance_us(session.start_us - rig.now_us)
    await _settle()

    restarted.stalled = True
    held = await restarted.play(1.3)
    restarted.stalled = False

    # what it got before its start instant: the lead, of which it played 1.3 s
    assert min(held) == START_LEAD_US - 1_300_000


async def test_positions_that_stay_impossible_after_a_restart_count_only_after_the_timeout(
    rig: _Rig,
) -> None:
    """A restart into its audio does not take a position of the stream before for its start."""
    first = await rig.start_playing(frames=SECOND)
    for second in range(1, 6):
        rig.bridge._on_audio_chunk(_chunk(rig.origin_us, second * SECOND, SECOND))
    rig.align(first)
    rig.clock.advance_us(first.start_us - rig.now_us + 1_950_000)
    rig.drain(first)
    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()
    session = await rig.await_start()
    loop = session.loop
    assert loop is not None
    assert not session.aligning

    def _report_5_seconds_too_much() -> None:
        rig.clock.advance_us(50_000)
        played_ms = (rig.now_us - session.start_us) // 1000
        rig.bridge.handle_slim_event(rig.status(elapsed_ms=played_ms + 5368))
        rig.drain(session)

    # for as long as a start is waited for, such a report does not count
    while rig.now_us - session.start_us <= ALIGN_TIMEOUT_US:
        _report_5_seconds_too_much()
        assert loop.start_error_ms is None
    assert rig.session is session

    # from then on it is measured as it reports
    for _ in range(3):
        _report_5_seconds_too_much()
    await _settle()

    assert loop.start_error_ms == pytest.approx(-5368.5)
    assert rig.session is not session
    assert rig.client.play_url.await_count == 3


async def test_an_underrun_tells_what_the_bridge_knew(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """The restart is logged with the supply of the player: what was written, taken, held and left."""
    session, player = await rig.serve_player()
    await player.play(3)
    player.stalled = True
    await player.play(1.25)
    written = len(player.body)
    # its last status, 100 ms before it reports that it ran dry
    status = replace(
        rig.status(elapsed_ms=4550).data,
        bytes_received=1_000_000,
        stream_buffer_size=3_145_728,
        stream_buffer_fullness=65_536,
        output_buffer_size=3_528_000,
        output_buffer_fullness=1_234,
    )
    rig.bridge.handle_slim_event(SlimEvent(SlimEventType.PLAYER_HEARTBEAT, PLAYER_ID, status))
    rig.clock.advance_us(100_000)

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert rig.session is not session
    # the stream was 6 seconds ahead when the player stopped taking it, and the block of
    # 50 ms after that waits: 7 of the 30 chunks of a second went out whole
    assert (
        "Sendspin stream on Kitchen is restarted after it played 4.5 s (failure 1 of 3 in a row "
        "before 10 s): it ran out of audio (prepared 4700 ms ahead, "
        f"{written} bytes written, a write waits for 1300 ms, 23000 ms of audio waiting, the player "
        "got 1000000 bytes and held 65536 of 3145728 in its stream buffer and 1234 of 3528000 "
        "in its output buffer 100 ms ago)"
    ) in caplog.text


@pytest.mark.parametrize(("output_codec", "plays_s"), [("wav", 3), ("flac", 3), ("flac", 9)])
@pytest.mark.parametrize("sync_adjust", [0, 100, -80])
async def test_a_player_that_ran_dry_carries_on_with_what_it_did_not_play(
    sync_adjust: int, output_codec: str, plays_s: int
) -> None:
    """
    A stall longer than the lead ends in an underrun: the new stream neither skips nor repeats audio.

    A FLAC stream that ran dry within 10 seconds is restarted as PCM, a later one as FLAC.
    """
    rig = _Rig(sync_adjust=sync_adjust, output_codec=output_codec)
    # a FLAC player decodes whole blocks, and the silence a new stream opens with takes
    # 0.2 seconds more to get through its decoder
    block = BLOCK_FRAMES if output_codec == "flac" else 0
    restarted_as = "flac" if output_codec == "flac" and plays_s == 9 else "wav"
    resumes_s = 8.6 if restarted_as == "flac" else 8.4
    # what it plays longer before the stall, in whole chunks, comes on top of every position
    later = (plays_s - 3) * SECOND
    try:
        first, player = await rig.serve_player(chunk_seconds=3)
        await player.play(plays_s)

        # nothing reaches the player, which reports where it plays until it has no more
        player.stalled = True
        while player.held_us > 0:
            await player.play(0.05)
        assert rig.session is first
        played = player.audio()
        # 6 seconds after the 3.3 it played: in the middle of the chunk that was partly taken
        assert rig.now_us - first.start_us == pytest.approx(
            9_300_000 + _frame_us(later), abs=50_000
        )
        assert abs(played[-1] + 1 - round(7.85 * SECOND) - later) <= block
        assert -22 - later // 2000 <= player.left_out() <= 0
        assert len(first.chunks) == 8 - (plays_s - 3) // 3
        # a setting that changed meanwhile is for the stream after this one
        rig.config[CONF_OUTPUT_CODEC] = "mp3"
        rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
        # it answers its status requests all along: its clock is known
        rig.map_clock()
        await _settle()
        player.stalled = False

        second = rig.session
        assert second is not first
        assert second.mime_type == ("audio/flac" if restarted_as == "flac" else MIME_TYPE)
        assert len(second.chunks) == 8 - (plays_s - 3) // 3
        restarted = _BufferedPlayer(rig, second)
        rig.serve(second, restarted)
        await _settle()
        rig.event(SlimEventType.PLAYER_BUFFER_READY)
        await _settle()
        await rig.align_served(second)
        await restarted.play(2)

        # nothing twice, and all of it from where the new stream could begin: 0.55 seconds
        # for its start and its preroll, in the chunk the first stream had begun
        audio = restarted.audio()
        assert played[-1] < audio[0] < 9 * SECOND + later
        assert audio[0] == pytest.approx(resumes_s * SECOND + later, abs=SECOND // 10)
        assert audio == list(range(audio[0], audio[0] + len(audio)))
        if restarted_as == "wav":
            assert rig.lead_frames(second) == START_LEAD_FRAMES
        else:
            assert abs(restarted.held_us - START_LEAD_US) < _frame_us(BLOCK_FRAMES)
        # each frame at its own time, the sync delay of the player included
        index = _frames(restarted.pcm()).index(audio[0])
        heard_us = second.start_us + _frame_us(index)
        assert heard_us == rig.origin_us - sync_adjust * 1000 + _frame_us(audio[0])
    finally:
        await rig.close()


async def test_an_underrun_of_a_stream_without_audio_says_so(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A player that ran dry while Sendspin had nothing for it shows that in the log."""
    session = await rig.start_playing(frames=SECOND)
    rig.clock.advance_us(session.start_us - rig.now_us + 4_000_000)
    rig.drain(session)

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert (
        "it ran out of audio (prepared 500 ms ahead, 0 bytes written, the last write took 0 ms, "
        "0 ms of audio waiting, no status of the player)"
    ) in caplog.text


async def test_an_underrun_of_a_flac_stream_tells_what_its_player_could_not_decode(
    flac_rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """The supply of a FLAC player is what went out less what its decoder waits with."""
    rig = flac_rig
    session, player = await rig.serve_player()
    await player.play(3)
    player.stalled = True
    await player.play(1.25)
    held_ms = session.frames_held * 1000 // SECOND
    assert 180 < held_ms < 240

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert rig.session is not session
    # 6 seconds ahead in what the player can decode, when it stopped taking its stream
    told = f"it ran out of audio (prepared 4990 ms ahead with {held_ms} ms it can not decode yet, "
    assert told in caplog.text


async def test_an_underrun_is_answered_before_the_stream_is_restarted(rig: _Rig) -> None:
    """
    A Squeezebox Radio that ran dry is resumed by aioslimproto and restarted by the bridge.

    The resume reaches the player before the new stream does, and nothing resumes that one
    ahead of the instant the bridge starts it at.
    """
    reader, sent = _real_client(rig)
    try:
        first = await rig.start_playing()
        await _player_reports(reader, b"STMs")
        rig.clock.advance_us(first.start_us - rig.now_us + 2_000_000)
        sent.clear()

        await _player_reports(reader, b"STMo")

        restarted = rig.session
        assert restarted is not first
        # resumed at once, then flushed and given the stream of the new session
        assert [command for command, _ in _stream_commands(sent)] == [b"u", b"f", b"q", b"s"]
        assert _stream_commands(sent)[0] == (b"u", 0)
        requests = [data for command, data in sent if command == b"strm" and data[:1] == b"s"]
        assert restarted.session_id.encode() in requests[-1]
        # the player reports the underrun of the stream that was flushed once more
        await _player_reports(reader, b"STMo")
        assert rig.session is restarted
        assert len(_stream_commands(sent)) == 4
        # its new stream starts when the bridge says so, on the clock of the player
        rig.fetched(restarted)
        await _player_reports(reader, b"STMl")
        assert restarted.start_decided.is_set()
        assert _stream_commands(sent)[4:] == [(b"u", rig.jiffies(restarted.command_us))]
    finally:
        rig.client._reader_task.cancel()


async def test_an_underrun_before_the_start_is_not_one_of_the_stream(rig: _Rig) -> None:
    """Until the stream has a start, an underrun is about what the player played before."""
    session = await rig.start_stream()

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert rig.session is session
    assert rig.client.play_url.await_count == 1


@pytest.mark.parametrize("played_s", [0, 9.75])
async def test_three_streams_in_a_row_that_fail_early_give_up_and_rejoin_later(
    rig: _Rig, played_s: float
) -> None:
    """A third stream in a row that fails before it played 10 seconds takes the player out of the session."""
    client = rig.grouped()
    group = client.group
    await rig.start_playing()
    for _ in range(MAX_QUICK_FAILURES - 1):
        await rig.run_dry(played_s=played_s)
    assert rig.owns_stream()
    assert rig.client.play_url.await_count == 3
    rig.client.stop.assert_not_called()
    client.quiesce_to_solo_stopped.assert_not_called()

    with patch(f"{MODULE}.REJOIN_ATTEMPT_DELAYS", (0,)):
        await rig.run_dry(played_s=played_s)

    assert not rig.owns_stream()
    assert rig.client.play_url.await_count == 3
    rig.client.stop.assert_awaited_once_with()
    client.quiesce_to_solo_stopped.assert_awaited_once()
    group.add_client.assert_awaited_once_with(client)


async def test_a_player_whose_streams_play_10_seconds_stays_in(rig: _Rig) -> None:
    """A stream that failed after it played 10 seconds is restarted, however often that happens."""
    client = rig.grouped()
    await rig.start_playing()

    for _ in range(2 * MAX_QUICK_FAILURES):
        await rig.run_dry(played_s=10)

    assert rig.owns_stream()
    assert rig.client.play_url.await_count == 1 + 2 * MAX_QUICK_FAILURES
    rig.client.stop.assert_not_called()
    client.quiesce_to_solo_stopped.assert_not_called()


async def test_a_stream_that_played_10_seconds_starts_the_count_again(rig: _Rig) -> None:
    """Two early failures are forgotten once a stream played long enough: it takes three more."""
    client = rig.grouped()
    await rig.start_playing()
    await rig.run_dry()
    await rig.run_dry(played_s=3)

    await rig.run_dry(played_s=10)
    await rig.run_dry()
    await rig.run_dry(played_s=3)

    assert rig.owns_stream()
    assert rig.client.play_url.await_count == 6
    client.quiesce_to_solo_stopped.assert_not_called()
    await rig.run_dry()
    assert not rig.owns_stream()
    client.quiesce_to_solo_stopped.assert_awaited_once()


async def test_a_stream_that_played_long_keeps_that_when_the_counter_of_its_player_starts_over(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A player counts the frames it played in 32 bits: after 27 hours its elapsed time starts at 0 again."""
    client = rig.grouped()
    session = await rig.start_playing()
    await rig.await_start()
    wrap_us = (1 << 32) * 1_000_000 // SECOND
    rig.clock.advance_us(wrap_us - 250_000)
    rig.bridge.handle_slim_event(rig.status(elapsed_ms=(wrap_us - 250_000) // 1000))
    rig.clock.advance_us(500_000)
    rig.bridge.handle_slim_event(rig.status(elapsed_ms=250))

    rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
    await _settle()

    assert rig.session is not session
    assert "Sendspin stream on Kitchen is restarted after it played 97391.3 s: " in caplog.text
    # it did not fail early: it takes three streams that do to take the player out
    for _ in range(MAX_QUICK_FAILURES - 1):
        await rig.run_dry()
    assert rig.owns_stream()
    client.quiesce_to_solo_stopped.assert_not_called()
    await rig.run_dry()
    assert not rig.owns_stream()


@pytest.mark.parametrize("position", ["stuck", "running"])
async def test_a_position_of_another_stream_that_a_player_keeps_reporting_is_not_played(
    rig: _Rig, position: str
) -> None:
    """A player that goes on reporting where another stream was has its stream restarted before that counts."""
    client = rig.grouped()
    await rig.start_playing(frames=60 * SECOND)

    for _ in range(MAX_QUICK_FAILURES):
        assert rig.owns_stream()
        session = await rig.await_start()
        # an hour played, from the instant it was told to start on, while its stream is written
        while not session.ended:
            rig.clock.advance_us(250_000)
            rig.bridge._next_block(session)
            since_start_ms = (rig.now_us - session.start_us) // 1000
            elapsed_ms = 3_600_000 + (since_start_ms if position == "running" else 0)
            rig.bridge.handle_slim_event(rig.status(elapsed_ms=elapsed_ms))
            assert since_start_ms < 9000, "it goes on playing"
        await _settle()

    assert not rig.owns_stream()
    assert rig.client.play_url.await_count == 3
    client.quiesce_to_solo_stopped.assert_awaited_once()


@pytest.mark.parametrize("reports", ["nothing", "nothing played", "the stream before"])
async def test_only_what_a_player_reports_of_its_stream_counts_as_played(
    rig: _Rig, reports: str
) -> None:
    """The time a player takes to get going is not played, and neither is a position of another stream."""
    client = rig.grouped()
    await rig.start_playing()

    for _ in range(MAX_QUICK_FAILURES):
        assert rig.owns_stream()
        session = await rig.await_start()
        if reports == "nothing":
            rig.clock.advance_us(30_000_000)
        elif reports == "nothing played":
            rig.play(session, 30, late_ms=60_000)
        else:
            # an hour played, a quarter of a second after it was told to start
            rig.play(session, 0.25, late_ms=-3_600_000)
        rig.event(SlimEventType.PLAYER_OUTPUT_UNDERRUN)
        await _settle()

    assert not rig.owns_stream()
    assert rig.client.play_url.await_count == 3
    client.quiesce_to_solo_stopped.assert_awaited_once()


async def test_a_player_that_ends_up_off_its_timeline_every_time_is_taken_out(rig: _Rig) -> None:
    """A stream that is restarted for a player that stays off its timeline did not play 10 seconds."""
    client = rig.grouped()
    await rig.start_playing()

    for _ in range(MAX_QUICK_FAILURES):
        assert rig.owns_stream()
        session = await rig.await_start()
        # it plays for two seconds and stalls without running dry
        rig.play(session, 2)
        for _ in range(18):
            rig.clock.advance_us(250_000)
            rig.bridge.handle_slim_event(rig.status(elapsed_ms=2000))
        await _settle()
        assert session.ended

    assert not rig.owns_stream()
    assert rig.client.play_url.await_count == 3
    client.quiesce_to_solo_stopped.assert_awaited_once()


async def test_the_log_tells_why_a_stream_was_restarted_and_why_its_player_left(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A restart tells what the player did and how long it had played, a give-up how often it failed."""
    rig.grouped()
    await rig.start_playing()
    supply = "it ran out of audio (prepared "

    await rig.run_dry(played_s=12.5)
    await rig.run_dry(played_s=2)
    await rig.run_dry()
    records = [record for record in caplog.records if "is restarted" in record.getMessage()]
    assert [record.levelno for record in records] == [logging.WARNING] * 3
    restarts = [record.getMessage() for record in records]
    assert [message.partition(supply)[0] for message in restarts] == [
        "Sendspin stream on Kitchen is restarted after it played 12.5 s: ",
        "Sendspin stream on Kitchen is restarted after it played 2.0 s (failure 1 of 3 in a row "
        "before 10 s): ",
        "Sendspin stream on Kitchen is restarted after it played 0.0 s (failure 2 of 3 in a row "
        "before 10 s): ",
    ]
    assert all(supply in message for message in restarts)
    assert not [record for record in caplog.records if record.levelno > logging.WARNING]

    await rig.run_dry(played_s=0.5)
    (gave_up,) = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert gave_up.getMessage().startswith(
        "Giving up on Kitchen and taking it out of the Sendspin session: its stream failed 3 "
        f"times in a row before it played 10 s, now after 0.5 s: {supply}"
    )
    # the reason as a restart gives it, with nothing around it
    assert gave_up.getMessage().count("(") == gave_up.getMessage().count(")") == 1
    assert gave_up.getMessage().endswith(" ms ago)")
    assert len([record for record in caplog.records if "is restarted" in record.getMessage()]) == 3


async def test_a_new_sendspin_stream_has_its_own_recovery_budget(rig: _Rig) -> None:
    """A failure on the stream before says nothing about this one."""
    await rig.start_playing()
    for _ in range(MAX_QUICK_FAILURES - 1):
        await rig.run_dry()

    await rig.start_playing()
    for _ in range(MAX_QUICK_FAILURES - 1):
        await rig.run_dry()

    assert rig.owns_stream()
    assert rig.client.play_url.await_count == 2 * MAX_QUICK_FAILURES


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
    assert rig.bridge._rejoined


async def test_a_bridge_that_gives_up_again_before_it_played_stays_out(
    rig: _Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A player that was put back and gives up again without 10 seconds played since is left out."""
    client = rig.grouped()
    group = client.group
    with patch(f"{MODULE}.REJOIN_ATTEMPT_DELAYS", (0,)):
        await rig.start_playing()
        for _ in range(MAX_QUICK_FAILURES):
            await rig.run_dry()
        group.add_client.assert_awaited_once_with(client)
        assert "gave up again" not in caplog.text

        # its return starts a stream, which plays a little short of that before it fails
        client.group = group
        await rig.start_playing()
        await rig.run_dry(played_s=9.75)
        await rig.run_dry()
        assert rig.owns_stream()
        await rig.run_dry()

    assert not rig.owns_stream()
    assert client.quiesce_to_solo_stopped.await_count == 2
    group.add_client.assert_awaited_once_with(client)
    (left_out,) = [record for record in caplog.records if "gave up again" in record.getMessage()]
    assert left_out.levelno == logging.WARNING
    assert left_out.getMessage() == (
        "Kitchen gave up again before it played 10 s since it was re-joined, leaving it out of "
        "the group until it is grouped again"
    )


@pytest.mark.parametrize(
    ("then", "returns"),
    [("grouped again", 2), ("ungrouped and grouped again", 2), ("a new stream", 1)],
)
async def test_a_bridge_that_is_grouped_by_its_user_gets_a_return_of_its_own(
    rig: _Rig, then: str, returns: int
) -> None:
    """A return that did not work out is forgotten once the user takes the player out of a group or puts it in one."""
    client = rig.grouped()
    group = client.group
    with patch(f"{MODULE}.REJOIN_ATTEMPT_DELAYS", (0,)):
        await rig.start_playing()
        for _ in range(MAX_QUICK_FAILURES):
            await rig.run_dry()
        group.add_client.assert_awaited_once_with(client)
        # its return starts a stream
        client.group = group
        await rig.start_playing()

        if then == "grouped again":
            # it gives up again, which leaves it out, and its user groups it again
            for _ in range(MAX_QUICK_FAILURES):
                await rig.run_dry()
            assert not rig.owns_stream()
            group.add_client.assert_awaited_once_with(client)
            client.moved_to(group)
        elif then == "ungrouped and grouped again":
            # before its stream played 10 seconds, without a failure
            rig.play(await rig.await_start(), 5)
            client.moved_to(MagicMock(clients=[client], has_active_stream=False))
            rig.bridge._on_stream_end()
            client.moved_to(group)
        await rig.start_playing()
        for _ in range(MAX_QUICK_FAILURES):
            assert rig.owns_stream()
            await rig.run_dry()

    assert not rig.owns_stream()
    assert group.add_client.await_count == returns


async def test_the_bridge_follows_its_client_from_group_to_group(rig: _Rig) -> None:
    """The bridge listens to its Sendspin client for as long as it is registered."""
    listeners: list[Any] = []

    def _add_event_listener(listener: Any) -> Callable[[], None]:
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    rig.sendspin_client.add_event_listener.side_effect = _add_event_listener

    await rig.bridge.start()
    (listener,) = listeners
    rig.bridge._rejoined = True
    listener(rig.sendspin_client, ClientGroupChangedEvent(MagicMock()))
    assert not rig.bridge._rejoined

    await rig.bridge.stop()
    assert not listeners


@pytest.mark.parametrize("ended_by", ["a failure", "a new stream"])
async def test_a_bridge_that_played_since_its_return_is_put_back_again(
    rig: _Rig, ended_by: str
) -> None:
    """A return that led to 10 seconds of playing worked: the next give-up gets a return of its own."""
    client = rig.grouped()
    group = client.group
    with patch(f"{MODULE}.REJOIN_ATTEMPT_DELAYS", (0,)):
        await rig.start_playing()
        for _ in range(MAX_QUICK_FAILURES):
            await rig.run_dry()
        group.add_client.assert_awaited_once_with(client)

        client.group = group
        await rig.start_playing()
        if ended_by == "a failure":
            await rig.run_dry(played_s=10)
        else:
            rig.play(await rig.await_start(), 10)
            await rig.start_playing()
        for _ in range(MAX_QUICK_FAILURES):
            assert rig.owns_stream()
            await rig.run_dry()

    assert not rig.owns_stream()
    assert client.quiesce_to_solo_stopped.await_count == 2
    assert group.add_client.await_count == 2


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
    assert rig.bridge._rejoined


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
    rig.clock.advance_us(session.start_us - rig.now_us + 100_000)

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
    rig.protocol.player = MagicMock(attached_protocols={PLAYER_ID: rig.protocol})
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
    rig.mass.players.get_protocol.return_value = MagicMock(
        underlying_protocol_id=None, session_bit_depth=None
    )
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
    rig.protocol.player = MagicMock(attached_protocols={PLAYER_ID: rig.protocol})
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


@pytest.mark.parametrize("resume_key", ["PLAY", "PAUSE"])
@pytest.mark.parametrize("kind", ["butn", "ir"])
async def test_the_keys_of_the_player_pause_and_resume_its_sendspin_group(
    rig: _Rig, kind: str, resume_key: str
) -> None:
    """
    Pause on the player pauses its Sendspin group, which ends the stream of the bridge.

    The player has nothing of its own to resume then: its next press resumes the group.
    """
    reader, sent = _real_client(rig)
    client = rig.client
    _player_of(rig)
    await rig.bridge.start()
    bridge_client = MagicMock(synced_to="spb_leader")
    bridge_client.player.player_id = "kitchen"
    rig.mass.players.get_protocol.side_effect = {CLIENT_ID: bridge_client}.get
    leader = MagicMock(player_id="living_room")
    rig.mass.players.get_player_by_protocol.side_effect = {"spb_leader": leader}.get
    rig.mass.players.cmd_play = AsyncMock()
    rig.mass.players.cmd_play_pause = AsyncMock()
    # play plays, and pause toggles what Music Assistant shows for the player
    resume = rig.mass.players.cmd_play if resume_key == "PLAY" else rig.mass.players.cmd_play_pause

    async def _pause_group(_player_id: str) -> None:
        # Sendspin has no pause: the stream of every bridge in the group ends
        rig.bridge._on_stream_end()
        rig.bridge._on_explicit_stop()

    rig.mass.players.cmd_pause.side_effect = _pause_group
    try:
        await rig.start_playing()
        await _player_reports(reader, b"STMs")
        assert client.state == SlimPlayerState.PLAYING
        sent.clear()

        await _player_presses(reader, kind, "PAUSE")

        rig.mass.players.cmd_pause.assert_awaited_once_with("living_room")
        assert [command for command, _ in _stream_commands(sent)] == [b"p", b"q"]
        assert client.state == SlimPlayerState.STOPPED
        rig.mass.players.cmd_play.assert_not_called()
        rig.mass.players.cmd_play_pause.assert_not_called()
        sent.clear()

        await _player_presses(reader, kind, resume_key)

        # the player of the bridge, which Music Assistant hands on to the player that
        # owns its playback
        resume.assert_awaited_once_with("kitchen")
        assert (
            rig.mass.players.cmd_play.await_count + (rig.mass.players.cmd_play_pause.await_count)
            == 1
        )
        assert _stream_commands(sent) == []
        rig.mass.players.cmd_pause.assert_awaited_once()
    finally:
        client._reader_task.cancel()


@pytest.mark.parametrize("kind", ["butn", "ir"])
async def test_a_player_that_plays_on_its_own_pauses_and_resumes_itself(
    rig: _Rig, kind: str
) -> None:
    """Outside a Sendspin group, pause and play on the player stay with its client."""
    reader, sent = _real_client(rig)
    client = rig.client
    _player_of(rig)
    await rig.bridge.start()
    rig.mass.players.cmd_play = AsyncMock()
    try:
        await client.play_url(f"{BASE_URL}/track.flac")
        await _player_reports(reader, b"STMs")
        sent.clear()

        await _player_presses(reader, kind, "PAUSE")
        assert client.state == SlimPlayerState.PAUSED
        await _player_presses(reader, kind, "PLAY")

        assert client.state == SlimPlayerState.PLAYING
        assert _stream_commands(sent) == [(b"p", 0), (b"u", 0)]
        rig.mass.players.cmd_pause.assert_not_called()
        rig.mass.players.cmd_play.assert_not_called()
    finally:
        client._reader_task.cancel()


@pytest.mark.parametrize("kind", ["butn", "ir"])
async def test_play_on_a_stopped_player_plays_its_player(rig: _Rig, kind: str) -> None:
    """A stopped player has nothing to resume: Music Assistant decides what it plays."""
    reader, sent = _real_client(rig)
    client = rig.client
    _player_of(rig)
    await rig.bridge.start()
    rig.mass.players.cmd_play = AsyncMock()
    try:
        await _player_presses(reader, kind, "PLAY")

        rig.mass.players.cmd_play.assert_awaited_once_with("kitchen")
        assert _stream_commands(sent) == []
        assert client.state == SlimPlayerState.STOPPED
    finally:
        client._reader_task.cancel()


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
