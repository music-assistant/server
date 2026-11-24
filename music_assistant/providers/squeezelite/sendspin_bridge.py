"""
Sendspin bridge for slimproto players: lets Sendspin stream to Squeezebox and squeezelite.

The bridge registers a slimproto player as an external Sendspin client. The Sendspin
provider creates a SendspinPlayer for that client, which rides on the squeezelite
protocol and is attached to its player as one of its outputs. Sendspin handles the
synchronization and the timing; the bridge puts its timeline on the player.

Every Sendspin stream is a session: one stream over HTTP that the player starts
at an instant of its own clock, on silence. A player starts later than it is told, by
an amount that differs from start to start: the bridge measures that from the first
positions the player reports and corrects it in the silence, so the first audio is
heard on the timeline. A stream whose audio is already due at its start, such as a
restart, gets that audio at once and has its start corrected in it. The sync loop holds
it there from the positions the player
reports, and once it has little left to correct the bridge keeps the stream seconds ahead
of what the player plays, as far as Sendspin delivered its audio, so the player plays on
through a stall of its network.

The stream is FLAC or PCM, by the output codec setting of the player and what the player
says it plays, in no more bits than the audio of its playback session has. A player whose
FLAC stream fails before it played ``MIN_PLAYED_US`` is sent PCM from then on.

Audio flow:
Sendspin PushStream -> BridgePlayerRole.on_audio_chunk -> session -> HTTP stream -> player
"""

from __future__ import annotations

import asyncio
import secrets
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.server.events import ClientGroupChangedEvent
from aioslimproto.models import SQUEEZEBOX2_PLAYER_TYPES, PlayerStatus
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from music_assistant_models.enums import ContentType, IdentifierType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_OUTPUT_CODEC, CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.providers.sendspin.bridge_manager import SendspinBridgeManagerBase
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_SAMPLE_RATE,
)
from music_assistant.providers.sendspin.helpers import (
    bridge_client_id_from_mac,
    holds_bridge_client,
)

from .bridge_flac import BLOCK_FRAMES, FlacEncoder
from .bridge_role import SLIMPROTO_BRIDGE_ROLE_ID, SlimprotoBridgeRole
from .bridge_sync import MAX_START_JUMP_MS, START_JUMP_MS, PlayerClock, SyncLoop
from .player import is_bridge_tool

if TYPE_CHECKING:
    from aiosendspin.server import (
        ClientEvent,
        ExternalStreamStartRequest,
        SendspinClient,
        SendspinGroup,
        SendspinServer,
    )
    from aiosendspin.server.roles import AudioChunk
    from aioslimproto.client import SlimClient
    from aioslimproto.models import MediaMetadata, SlimEvent

    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol
    from music_assistant.providers.sendspin.player import SendspinPlayer
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .player import SqueezelitePlayer
    from .provider import SqueezelitePlayerProvider


# The stream of a session is served on this path, to the player and session of its query.
BRIDGE_STREAM_PATH = "/slimproto/bridge"

# Audio the stream is kept ahead of what a player on its timeline plays, as far as there
# is audio for it: what the player holds to play on while nothing reaches it. A correction
# of the sync loop is only heard once this much has played. At 96 kHz / 24 bit it is
# 3.5 MB, which the stream and output buffers of a player hold together; a player that
# holds less takes it as it plays.
STREAM_LEAD_US: int = 6_000_000
# The same until the sync loop has the player on its timeline: what it corrects of the
# start is heard once this much has played, so it is short. A stream stays at its long
# lead once it is there, as a correction goes into the audio that follows what was sent
# and is heard no sooner for sending that later.
START_LEAD_US: int = 1_500_000

# Silence the stream opens with. A player only reports its buffer ready once it holds
# more than the stream threshold, and the instant its stream starts can only be decided
# then: the silence is what it holds by that time.
PREROLL_US: int = 250_000
# Silence a player plays before its first audio, the preroll included and as far as there
# is time for it. The start is measured and corrected in it. That takes the start offset,
# the lead below and the reports the start error is taken from, about 0.6 s together, and
# twice what the player starts later than that offset.
LEAD_IN_US: int = 1_450_000
# Silence the stream is at least kept ahead of what the player can have taken from it
# until its start is corrected, which is all of it since the instant it was told to
# start: a player reports what is heard, and its output holds more. What a late player
# was sent can not be taken back, so the lead is short.
ALIGN_LEAD_US: int = 200_000
# Silence before the first audio that is not sent until the start is corrected: the room
# to leave out what a player starts late. The silence before it goes to the player at
# once, so it holds more than the lead above through a hiccup of the network. The room
# is half of what the lead-in is longer than that lead and the quarter of a second of
# reports the start error is taken from: no more audio is left out than with the lead alone.
ALIGN_ROOM_US: int = 500_000
# A start that is not corrected this long after its instant is given its audio as it is:
# its error is corrected in the audio when the player shows where it is.
ALIGN_TIMEOUT_US: int = 2_000_000
# Start error (ms) above which it is corrected while only silence went out.
ALIGN_JUMP_MS: float = 1.0
# Buffer levels a player waits for: kilobytes of the stream before it reports its buffer
# ready, and tenths of a second of decoded audio before a Squeezebox Radio or Touch starts.
STREAM_THRESHOLD_KB: int = 16
OUTPUT_THRESHOLD: int = 1

# A timed start has to reach the player this long before its instant (squeezelite-esp32).
MIN_START_AHEAD_US: int = 300_000
# A Squeezebox Radio or Touch starts at once when told to start 5 seconds or more ahead.
MAX_START_AHEAD_US: int = 4_500_000
# A player that does not report its buffer ready in this time is not going to.
START_TIMEOUT_SECONDS: float = 10.0

# Lead and buffer (ms) reported to Sendspin. The lead is how long before its audible
# instant the first chunk of a stream arrives: it has to cover a cold start, which is the
# mapping of the player's clock, the flush, the stream request with its HTTP connect, the
# preroll that fills the player to its threshold and a start MIN_START_AHEAD_US ahead,
# plus what the player is known to start late. What is left of it is the lead-in the
# start is corrected in. The buffer is the least Sendspin keeps the bridge supplied with
# during the stream, which delays what every player of its group plays: the stream lead
# is filled from what a buffered source delivers beyond it.
BRIDGE_REQUIRED_LEAD_MS: int = 2000
BRIDGE_MIN_BUFFER_MS: int = 2000

# The player is asked for its status this often (seconds) during a session. Faster while
# its clock is not mapped yet, so the first start does not have to wait for it, and from
# the instant it is told to start until its start is corrected, which takes a few reports.
STATUS_POLL_INTERVAL: float = 0.25
STATUS_POLL_INTERVAL_FAST: float = 0.05
# Status requests still waiting for their reply that are remembered.
MAX_PENDING_STATUS: int = 8
# Status requests carry a token with this bit set: the heartbeat of aioslimproto counts
# up from 1 and a status the player sends unasked carries 0.
STATUS_TOKEN_BIT: int = 1 << 31
# A mapping of the player's clock that got no reply for this long is learned again: the
# clocks drift apart, by a millisecond in ten seconds at 100 ppm.
CLOCK_MAX_AGE_US: int = 10_000_000
# A session waits this long (seconds) for the mapping of the player's clock before it
# requests its stream. A player answers late while it sets a stream up, and a mapping
# learned from those replies is off by half of what they took.
CLOCK_TIMEOUT_SECONDS: float = 0.5

# The writer hands the player what it is allowed this often (seconds), at most this
# much audio at a time.
WRITE_INTERVAL: float = 0.02
MAX_BLOCK_US: int = 500_000
# And at most this many blocks of a FLAC stream. Encoding keeps the event loop for every
# block, some 40 to 75 microseconds on a desktop processor at any sample rate, and the
# players of a group start together. At 192 kHz this is still over three times the audio
# that plays meanwhile.
MAX_FLAC_BLOCKS: int = 12
# While Sendspin has nothing to play, silence keeps the stream this far ahead, so the
# player does not run dry and stays on the timeline.
STARVED_LEAD_US: int = 500_000
# And what the player can decode of a FLAC stream at least this far, so it decodes the
# end of the audio before it plays it. Audio Sendspin delivers its buffer ahead leaves at
# most a second and a half of it undecoded, so it can be decoded half a second ahead: no
# silence takes its place.
STARVED_DECODE_LEAD_US: int = 250_000
# A write the player takes this long over is logged.
SLOW_WRITE_US: int = 1_000_000

# Caps on the audio that waits for its turn. Sendspin delivers a buffered source up to
# 30 seconds ahead; beyond the caps the oldest chunks go. The count bounds a run of
# chunks without duration.
MAX_PENDING_AUDIO_US: int = 45_000_000
MAX_PENDING_CHUNKS: int = 4_000

# A player that stays this far off its timeline (ms) for this long has its stream
# restarted: the sync loop would need minutes to pull it back. A player that stalled
# without running out of audio ends up like that. The time covers a start error that
# could only be corrected in the audio, in a jump that is heard once the lead of a start
# has played.
MAX_TIMELINE_ERROR_MS: float = 100.0
MAX_OFF_TIMELINE_US: int = 4_000_000

# A stream that fails is restarted, and its player stays in its Sendspin group for as long
# as its streams play this much before they fail, by what the player reports to have
# played. One that played this much got past its start and played what it was sent. One
# that fails sooner did not, which includes the 5 seconds or so after which a player that
# stays off its timeline is restarted.
MIN_PLAYED_US: int = 10_000_000
# Streams in a row that fail sooner than that before the bridge leaves the Sendspin
# session: restarting once more is not going to work.
MAX_QUICK_FAILURES: int = 3
# Delays (seconds) before each attempt to put a bridge back into the Sendspin group a
# give-up took it out of. A bridge that gives up again before a stream played that much
# since it was put back stays out, until its player is taken out of a group or put into one.
REJOIN_ATTEMPT_DELAYS: tuple[int, ...] = (5, 30)
# The same for the bridge of a player that connected again: it only has to wait for its
# Sendspin protocol to be attached.
RECONNECT_REJOIN_DELAYS: tuple[int, ...] = (1, 5, 30)
# How long (seconds) the group of a bridge is remembered after its player disconnected.
RECONNECT_REJOIN_WINDOW: float = 60.0

# The cache category of what was learned about the start of a player, by protocol id.
CACHE_CATEGORY_START_OFFSET = 2


def get_bridge_client_id(player: SqueezelitePlayer) -> str | None:
    """
    Get the Sendspin bridge client id for a squeezelite player.

    :param player: The squeezelite player to bridge.
    :return: The client id, or None when the id of the player is no valid MAC address.
    """
    if is_valid_mac_address(player.protocol_id):
        return bridge_client_id_from_mac(player.protocol_id)
    return None


class BridgeSession:
    """
    One Sendspin stream on its way to the player.

    Holds the chunks that wait for their turn and places them on the timeline of the
    stream: frame ``n`` of that timeline is due at ``start_us + n / sample rate``.
    """

    def __init__(
        self,
        audio_format: AudioFormat,
        timeline_shift_us: int,
        chunks: deque[AudioChunk] | None = None,
    ) -> None:
        """
        Initialize a session that waits for its player and its first chunk.

        :param audio_format: The format of the stream to the player: FLAC, or PCM.
        :param timeline_shift_us: How much later than its Sendspin timestamp audio is due
            on this player.
        :param chunks: Chunks a session before this one did not play yet.
        """
        self.session_id = secrets.token_hex(8)
        self.audio_format = audio_format
        self.sample_rate = audio_format.sample_rate
        self.frame_size = audio_format.channels * audio_format.bit_depth // 8
        # Sendspin delivers a 24-bit sample in 32 bits, the player takes it in 24
        self._chunk_frame_size = (
            audio_format.channels * 4 if audio_format.bit_depth == 24 else self.frame_size
        )
        # Players take the PCM format from the content type, which goes in the stream
        # request too.
        self.mime_type = (
            f"audio/pcm;rate={audio_format.sample_rate};bitrate={audio_format.bit_depth}"
            f";channels={audio_format.channels}"
        )
        self._encoder: FlacEncoder | None = None
        self._header = b""
        preroll_frames = self.frames(PREROLL_US)
        # the most frames the writer prepares at a time
        self.max_block = self.frames(MAX_BLOCK_US)
        if audio_format.content_type == ContentType.FLAC:
            self.max_block = min(self.max_block, MAX_FLAC_BLOCKS * BLOCK_FRAMES)
            # the encoder takes the samples as Sendspin delivers them
            self.frame_size = self._chunk_frame_size
            self.mime_type = "audio/flac"
            self._encoder = FlacEncoder(
                audio_format.sample_rate, audio_format.bit_depth, audio_format.channels
            )
            self._header = self._encoder.header
            # the player holds the preroll, and has to decode that much of it
            preroll_frames += self._encoder.silence_held
        self.preroll = bytes(preroll_frames * self.frame_size)
        self.chunks: deque[AudioChunk] = deque()
        self.pending_us = 0
        # chunks that went to the player, as far back as it can still hold them
        self._sent: deque[AudioChunk] = deque()
        self._sent_us = 0
        # instant the first chunk of the stream is due
        self.first_audible_us: int | None = None
        # set once the start is decided: the instant frame 0 of the stream is played, the
        # instant the player is told to start for it and the loop that holds it there
        self.start_us = 0
        self.command_us = 0
        self.loop: SyncLoop | None = None
        self.start_decided = asyncio.Event()
        # the clock of the player is mapped: its stream can be requested
        self.clock_mapped = asyncio.Event()
        # the stream was requested from the player, which answers for it from then on
        self.stream_requested = False
        # the player fetched the stream, got its preroll and reported its buffer ready
        self.claimed = False
        self.preroll_sent = False
        self.buffer_ready = False
        # the player was told when to start
        self.started = False
        # no more audio goes to the player: its stream ends
        self.ended = False
        # only silence goes to the player: where it is on its timeline is not corrected yet
        self.aligning = True
        self.start_error_taken = False
        # report time since which every report was far off the timeline
        self.off_timeline_since_us: int | None = None
        self.pause_forwarded = False
        # the stream as the player got it: the bytes it took, the time the write that
        # waits for it began, how long the last one took and whether it is fed silence
        self.bytes_written = 0
        self.write_started_us: int | None = None
        self.write_took_us = 0
        self.starved = False
        # the last status of the player while it played the stream
        self.status: PlayerStatus | None = None
        # the most the player reported to have played of the stream: its elapsed time
        # starts over when the counter of its frames wraps
        self.played_us = 0
        self.tasks: list[asyncio.Task[None]] = []
        self._shift_us = timeline_shift_us
        # frames of the first chunk that were played already
        self._head_taken = 0
        for chunk in chunks or ():
            self.add(chunk)

    def add(self, chunk: AudioChunk) -> None:
        """
        Add a chunk of the stream.

        :param chunk: The next chunk Sendspin delivered.
        """
        if self.first_audible_us is None:
            self.first_audible_us = chunk.timestamp_us + self._shift_us
        self.chunks.append(chunk)
        self.pending_us += chunk.duration_us
        while len(self.chunks) > 1 and (
            self.pending_us > MAX_PENDING_AUDIO_US or len(self.chunks) > MAX_PENDING_CHUNKS
        ):
            self._drop_first()

    def frames(self, duration_us: int) -> int:
        """
        Return the number of frames of the stream that play in a duration.

        :param duration_us: The duration in microseconds.
        """
        return duration_us * self.sample_rate // 1_000_000

    def take(self, cursor: int, frames: int) -> bytes:
        """
        Return the audio of the timeline from a frame on, as far as it is known.

        Every chunk is placed where its timestamp puts it: a hole before it is silence,
        and what lies before the cursor is left out.

        :param cursor: The timeline frame the audio has to start at.
        :param frames: The most frames to return.
        """
        size = self._chunk_frame_size
        parts: list[bytes | memoryview] = []
        while frames > 0 and self.chunks:
            chunk = self.chunks[0]
            chunk_frames = len(chunk.data) // size
            first = self._head_taken
            if not first:
                audible_us = chunk.timestamp_us + self._shift_us - self.start_us
                behind = cursor - round(audible_us * self.sample_rate / 1_000_000)
                if behind < -1:
                    count = min(-behind, frames)
                    parts.append(bytes(count * size))
                    cursor += count
                    frames -= count
                    continue
                # one frame either way is the rounding of the timestamps, not a jump
                first = behind if behind > 1 else 0
            count = min(chunk_frames - first, frames)
            if count > 0:
                data = memoryview(chunk.data)
                parts.append(data[first * size : (first + count) * size])
                cursor += count
                frames -= count
            if first + count >= chunk_frames:
                self._drop_first(sent=True)
            else:
                self._head_taken = first + count
        pcm = b"".join(parts)
        # only what goes to the player now: a late joiner is handed half a minute at once
        return _pack_24_bit(pcm) if size != self.frame_size else pcm

    def encode(self, pcm: bytes) -> bytes:
        """
        Return what goes to the player for the next PCM of the stream.

        :param pcm: The next PCM of the stream, a whole number of frames.
        :return: The bytes to write, nothing while a FLAC stream waits for more PCM.
        """
        if (encoder := self._encoder) is None:
            return pcm
        header, self._header = self._header, b""
        return header + encoder.encode(pcm)

    def flush(self) -> bytes:
        """Return what is left to write of a stream that ended: the PCM a FLAC stream waited with."""
        return self._encoder.flush() if self._encoder else b""

    @property
    def frames_held(self) -> int:
        """
        Return the number of frames at the end of what went out that the player can not decode yet.

        A player decodes a FLAC stream only as far as more of it follows.
        """
        return self._encoder.frames_held if self._encoder else 0

    def is_fetched_by(self, request: web.Request) -> bool:
        """
        Return whether a request is the one that fetches the stream of this session.

        :param request: A request for the stream of a session.
        """
        return not self.ended and not self.claimed and request.query.get("sid") == self.session_id

    def unplayed(self, now_us: int) -> deque[AudioChunk]:
        """
        Return the chunks of the stream that are not played out yet.

        :param now_us: The time on the server clock.
        """
        chunks = deque(
            chunk
            for chunk in self._sent
            if chunk.timestamp_us + self._shift_us + chunk.duration_us > now_us
        )
        chunks.extend(self.chunks)
        return chunks

    def close(self) -> None:
        """End the session: its stream ends and everything it started is cancelled."""
        self.ended = True
        self.chunks.clear()
        self._sent.clear()
        self.pending_us = 0
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()
        self.tasks.clear()

    def _drop_first(self, *, sent: bool = False) -> None:
        """
        Drop the first chunk that waits.

        :param sent: Whether the chunk went to the player, which has yet to play it.
        """
        chunk = self.chunks.popleft()
        self.pending_us -= chunk.duration_us
        self._head_taken = 0
        if not sent:
            return
        self._sent.append(chunk)
        self._sent_us += chunk.duration_us
        # the stream is never further ahead of what the player plays than its lead, and
        # what the player can not decode of it yet
        lead_us = STREAM_LEAD_US + self.frames_held * 1_000_000 // self.sample_rate
        while (
            self._sent_us - self._sent[0].duration_us >= lead_us
            or len(self._sent) > MAX_PENDING_CHUNKS
        ):
            self._sent_us -= self._sent.popleft().duration_us


class SendspinSqueezeliteBridge:
    """
    Manages the Sendspin to slimproto bridge for a single player.

    Registers the squeezelite player as an external Sendspin client and plays every
    Sendspin stream that client is part of on the player, on the timeline of Sendspin.
    """

    def __init__(
        self,
        provider: SqueezelitePlayerProvider,
        protocol: SqueezelitePlayer,
        sendspin_server: SendspinServer,
    ) -> None:
        """
        Initialize the bridge.

        :param provider: The squeezelite provider instance.
        :param protocol: The squeezelite player to bridge.
        :param sendspin_server: The Sendspin server to register with.
        """
        self.provider = provider
        self.mass = provider.mass
        self.protocol = protocol
        self.sendspin_server = sendspin_server
        self.logger = provider.logger.getChild(f"bridge.{protocol.protocol_id}")

        self._clock = provider.clock
        self._sendspin_client: SendspinClient | None = None
        self._unsub_client_events: Callable[[], None] | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: SlimprotoBridgeRole | None = None
        # the mapping of the player's clock is kept for as long as its connection lasts
        self._player_clock = PlayerClock()
        # time of the last reply that went into it
        self._clock_sampled_us = 0
        self._session: BridgeSession | None = None
        # status requests waiting for their reply: token -> time it was sent
        self._pending_status: dict[int, int] = {}
        self._status_token = 0
        self._reports = 0
        # streams in a row that failed before they played long enough
        self._quick_failures = 0
        self._rejoin_task: asyncio.Task[None] | None = None
        # the group a pending re-join returns the bridge to
        self._rejoin_group: SendspinGroup | None = None
        # put back into a group by the bridge, with no stream that played long enough and
        # no other change of its group since
        self._rejoined = False
        # a FLAC stream failed before it played long enough: the player is sent PCM
        self._flac_failed = False

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    @property
    def bridge_client_id(self) -> str | None:
        """Return the id of the Sendspin client registered for this bridge."""
        return self._bridge_client_id

    @property
    def owns_stream(self) -> bool:
        """Return whether the stream on the player is that of the bridge."""
        return self._session is not None

    @property
    def position_in_track(self) -> float | None:
        """
        Return the position in the track the player plays or holds for the bridge, in seconds.

        It is the position Music Assistant shows for its player: the elapsed time of the
        player counts from the start of a stream of the bridge, which starts mid-track and
        plays on from track to track. None while the player has nothing of the bridge.
        """
        if (player := self.protocol.player) is None or (media := player.current_media) is None:
            return None
        if self._session is not None:
            return media.corrected_elapsed_time
        if self._holds_bridge_track():
            # a stopped group stays where it stopped, and corrected_elapsed_time runs on
            # whatever the playback state is
            return media.elapsed_time
        return None

    @property
    def shared_group(self) -> SendspinGroup | None:
        """Return the Sendspin group the bridge shares with other clients or is to return to."""
        if self._rejoin_group is not None:
            return self._rejoin_group
        if (client := self._sendspin_client) is None:
            return None
        return client.group if len(client.group.clients) > 1 else None

    async def start(self) -> None:
        """Register the squeezelite player as an external Sendspin client."""
        bridge_client_id = self.provider.bridge_manager.bridge_client_id(self.protocol)
        if not bridge_client_id:
            self.logger.warning(
                "Cannot create Sendspin bridge for %s: no valid MAC address",
                self.protocol.display_name,
            )
            return
        if self.sendspin_server.clock != self._clock:
            # the bridge compares the times of the reports of the player with the
            # timestamps of Sendspin, which only works on one and the same clock
            self.logger.warning(
                "Cannot create Sendspin bridge for %s: Sendspin runs on another clock",
                self.protocol.display_name,
            )
            return

        hello = ClientHelloPayload(
            client_id=bridge_client_id,
            name=self._client_name,
            version=1,
            # While the player@v1 role will never be used for bridges, it is required by
            # aiosendspin to parse the player@v1_support/ClientHelloPlayerSupport object
            supported_roles=[SLIMPROTO_BRIDGE_ROLE_ID, "player@v1"],
            device_info=SendspinDeviceInfo(
                product_name=self.protocol.device_info.model,
                manufacturer=self.protocol.device_info.manufacturer,
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=_supported_formats(self.protocol),
                buffer_capacity=1_000,
                # no mute: muting a slimproto player switches its output off, which
                # stops its playback
                supported_commands=[PlayerCommand.VOLUME],
            ),
        )

        self.logger.debug(
            "Registering Sendspin bridge for %s with client_id=%s",
            self.protocol.display_name,
            bridge_client_id,
        )

        # Pre-register the MAC address and the squeezelite protocol, so the resulting
        # SendspinPlayer keeps the features of this hello and rides on that protocol.
        if sendspin_prov := cast("SendspinProvider | None", self.mass.get_provider("sendspin")):
            sendspin_prov.register_bridge_identifiers(
                bridge_client_id, {IdentifierType.MAC_ADDRESS: self.protocol.protocol_id}
            )
            sendspin_prov.register_bridge_underlying_protocol(
                bridge_client_id, self.protocol.protocol_id
            )

        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_stream_start
        )
        self._bridge_client_id = bridge_client_id
        self._unsub_client_events = self._sendspin_client.add_event_listener(self._on_client_event)

        # Role is created by register_external_player via the factory registry.
        # Retrieve it and wire up the bridge callbacks.
        roles = self._sendspin_client.roles_by_family("player")
        if roles:
            self._bridge_role = cast("SlimprotoBridgeRole", roles[0])
            self._bridge_role.set_callbacks(
                on_audio_chunk=self._on_audio_chunk,
                on_volume_change=self._on_volume_change,
                on_mute_change=self._on_mute_change,
                on_stream_start=self._on_role_stream_start,
                on_stream_end=self._on_stream_end,
                on_explicit_stop=self._on_explicit_stop,
                initial_volume=self._client.volume_level,
            )
            self._bridge_role.limit_bit_depth(self._bit_depth_limit)
            # its default format, until Sendspin prefers another one for a session
            self._bridge_role.set_preferred_format(None)
            self._bridge_role.set_timing(
                required_lead_time_ms=BRIDGE_REQUIRED_LEAD_MS,
                min_buffer_ms=BRIDGE_MIN_BUFFER_MS,
            )
        self.protocol.sendspin_bridge = self

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.protocol.display_name,
            bridge_client_id,
        )

    async def stop(self) -> None:
        """Stop and unregister the Sendspin bridge."""
        self._cancel_rejoin()
        if (session := self._session) is not None:
            self._close_session(session, stop_player=True)
        else:
            self._clear_stopped_track()
        if self.protocol.sendspin_bridge is self:
            self.protocol.sendspin_bridge = None
        if self._unsub_client_events is not None:
            self._unsub_client_events()
            self._unsub_client_events = None
        if self._sendspin_client is not None and (client_id := self._bridge_client_id):
            holds_client = holds_bridge_client(self.mass, client_id, self.protocol.protocol_id)
            self._sendspin_client = None
            self._bridge_role = None
            if holds_client:
                await self.sendspin_server.remove_client(client_id)
        self.logger.debug("Sendspin bridge stopped for %s", self.protocol.display_name)

    def stop_streaming(self) -> bool:
        """
        Give the player back, for a stop or a stream of the squeezelite protocol itself.

        The stream of the bridge ends and the bridge leaves its Sendspin session without
        lining up a return. The player itself is left to the caller, which stops it or
        starts another stream on it.

        :return: Whether the bridge had a stream on the player.
        """
        self._cancel_rejoin()
        if (session := self._session) is None:
            return False
        self._close_session(session)
        self.mass.create_task(self._leave_sendspin_session(rejoin=False))
        return True

    def return_to_group(self, group: SendspinGroup) -> None:
        """
        Put the bridge back into the Sendspin group the bridge before it was part of.

        :param group: The Sendspin group to return to.
        """
        self._schedule_rejoin(group, RECONNECT_REJOIN_DELAYS)

    def update_name(self) -> None:
        """
        Give the Sendspin client of the bridge the name its player has now.

        A slimproto player reports its name only after it connected, and its player can
        be renamed. Sendspin shows the name of the client, also as that of its group.
        """
        if (client := self._sendspin_client) is None or client.name == self._client_name:
            return
        client.refresh_identity_from_hello(
            replace(client.info, name=self._client_name),
            negotiated_roles=client.negotiated_role_ids,
        )

    def transport_command_target(self) -> str | None:
        """
        Return the player whose Sendspin session controls the player of the bridge.

        :return: The id of the player of the Sendspin sync leader of the bridge client,
            else of the player the bridge client belongs to; None when Sendspin does not
            currently control the player.
        """
        if self._session is None or not (bridge_client_id := self._bridge_client_id):
            return None
        if not (bridge_client := self.mass.players.get_protocol(bridge_client_id)):
            return None
        if (sync_leader_id := bridge_client.synced_to) and (
            sync_leader := self.mass.players.get_player_by_protocol(sync_leader_id)
        ):
            return sync_leader.player_id
        return bridge_client.player.player_id if bridge_client.player else None

    def sync_role_volume_state(self) -> None:
        """
        Adopt the volume of the player into the bridge role.

        The role is what the Sendspin protocol of the bridge reports, so without this a
        volume change made on the player itself would not show.
        """
        if self._bridge_role is not None:
            self._bridge_role.update_player_state(volume=self._client.volume_level)

    def update_now_playing(self) -> None:
        """
        Show on the player what its player in Music Assistant plays now.

        The stream of the bridge plays on while the Sendspin group moves from track to
        track: a display of the player and the clients that control it follow through here.
        """
        if self._session is not None:
            self.mass.create_task(self._client.update_metadata(self._now_playing()))

    def handle_slim_event(self, event: SlimEvent) -> None:
        """
        Handle an event of the player of the bridge.

        :param event: The event aioslimproto reported for the player.
        """
        if event.type == SlimEventType.PLAYER_HEARTBEAT:
            if isinstance(event.data, PlayerStatus):
                self._on_status(event.data)
            return
        self.sync_role_volume_state()
        if (session := self._session) is None:
            return
        if session.ended:
            if self._client.state == SlimPlayerState.STOPPED:
                # the player played out what was left of the stream
                self._close_session(session)
        elif event.type == SlimEventType.PLAYER_BUFFER_READY:
            # only counts for the stream of this session: a player reports the stream
            # before it ahead of fetching this one
            if session.preroll_sent:
                session.buffer_ready = True
                self._decide_start(session)
        elif event.type == SlimEventType.PLAYER_OUTPUT_UNDERRUN:
            if (loop := session.loop) is not None:
                self._recover(session, f"it ran out of audio ({self._supply(session, loop)})")
        elif session.stream_requested:
            self._follow_player(session)

    async def serve_stream(self, request: web.Request) -> web.StreamResponse:
        """
        Serve the stream of the current session to the player.

        :param request: The request of the player, with the id of the session in its query.
        :raises web.HTTPNotFound: When the session is not the current one, or was fetched before.
        """
        session = self._session
        if session is None or not session.is_fetched_by(request):
            raise web.HTTPNotFound(reason="No such stream")
        # no content length and no chunked encoding (the request is HTTP/1.0): players
        # read the plain body until the connection closes
        resp = web.StreamResponse(
            status=200, reason="OK", headers={"Content-Type": session.mime_type}
        )
        if request.method != "GET":
            await resp.prepare(request)
            return resp
        session.claimed = True
        try:
            await resp.prepare(request)
            await self._write(session, resp, session.encode(session.preroll))
            session.preroll_sent = True
            while not session.ended:
                if (block := self._next_block(session)) and (data := session.encode(block)):
                    await self._write(session, resp, data)
                await asyncio.sleep(WRITE_INTERVAL)
            if data := session.flush():
                await self._write(session, resp, data)
        except ConnectionError:
            pass
        finally:
            # nothing to restart for a session that ended
            if not self.mass.closing:
                self._recover(session, "it closed its stream")
        return resp

    @property
    def _client(self) -> SlimClient:
        """Return the slimproto client of the player."""
        return self.protocol.client

    @property
    def _client_name(self) -> str:
        """Return the name of the Sendspin client of the bridge."""
        return f"{self.protocol.display_name} (Squeezelite)"

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """
        Handle a stream start request from the Sendspin server.

        Called for every new Sendspin stream this bridge is part of: playback that
        starts, seeks or resumes, new media, and the bridge joining a playing group.
        """
        self.logger.debug(
            "Sendspin stream start request for %s (reason=%s)",
            self.protocol.display_name,
            request.connection_reason,
        )
        # Each Sendspin stream gets its own recovery budget: a failure on the previous
        # one says nothing about the player on this one.
        self._quick_failures = 0
        # Joining a session by any means supersedes a pending re-join.
        self._cancel_rejoin()
        self._begin_session()

    def _on_client_event(self, _client: SendspinClient, event: ClientEvent) -> None:
        """Handle an event of the Sendspin client of the bridge."""
        if isinstance(event, ClientGroupChangedEvent):
            # Taken out of a group or put into one: a return that did not work out says
            # nothing about the player there. A return of the bridge itself is noted once
            # the client is in its group.
            self._rejoined = False
            # Sendspin ends the stream of a client before it moves it: what is left on the
            # player is of the group it was in
            self._clear_stopped_track()

    def _on_role_stream_start(self) -> None:
        """Start a session for a stream that begins without a stream start request."""
        session = self._session
        if (
            session is not None
            and not session.ended
            and session.first_audible_us is None
            and session.audio_format == self._role_format()
        ):
            # the session the stream start request of this stream began
            return
        self._begin_session()

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Handle an audio chunk from the Sendspin PushStream."""
        session = self._session
        if session is None or session.ended:
            return
        first = session.first_audible_us is None
        session.add(chunk)
        if first:
            self._decide_start(session)

    def _on_stream_end(self) -> None:
        """Handle the Sendspin stream ending: the player plays out what it has."""
        session = self._session
        if session is None or session.ended:
            return
        self.logger.debug("Sendspin stream ended for %s", self.protocol.display_name)
        if session.started:
            session.close()
        else:
            # a player that was not told to start has nothing to play out
            self._close_session(session, stop_player=session.stream_requested)

    def _on_explicit_stop(self) -> None:
        """Stop the player at once for a stop the user asked for."""
        session = self._session
        if session is None or not session.ended:
            # nothing is left playing, or a new stream took the bridge over already
            return
        self.logger.debug("Explicit stop for %s", self.protocol.display_name)
        # the player keeps the stopped track: its play key does nothing on an empty playlist
        self._close_session(session, stop_player=True, keep_media=True)

    def _on_volume_change(self, volume: int) -> None:
        """Forward a volume change to the squeezelite player."""
        # Applied on the protocol itself: while the bridge streams, its Sendspin protocol
        # is the active output of the player, so a volume from Music Assistant passed the
        # player controller already and was scaled into the volume range of the player.
        # Sending it through the controller again would scale it a second time.
        self.mass.create_task(self.protocol.volume_set(volume))

    def _on_mute_change(self, muted: bool) -> None:
        """Handle a mute of the Sendspin group, which the player can not follow."""
        # The hello offers no mute, but a group mute reaches every player role. The
        # role took the mute already: hand it back, so it reports what is heard.
        if muted and self._bridge_role is not None:
            self._bridge_role.update_player_state(muted=False)

    def _begin_session(
        self,
        chunks: deque[AudioChunk] | None = None,
        audio_format: AudioFormat | None = None,
    ) -> None:
        """
        Begin a new session, which replaces the current one.

        :param chunks: Chunks of the stream the current session did not play yet, for a
            session that carries on with the same stream.
        :param audio_format: The format of that stream. A new stream comes in the format
            the role of the bridge asks Sendspin for, as the codec the player is set to.
        """
        if (old := self._session) is not None:
            # no stop for the player: the stream request of the new session flushes it
            self._close_session(old, keep_media=True)
        if not self.protocol.available:
            self.logger.warning(
                "Cannot start Sendspin stream for %s: player not available",
                self.protocol.display_name,
            )
            return
        if self._clock.now_us() - self._clock_sampled_us > CLOCK_MAX_AGE_US:
            self._player_clock = PlayerClock()
        sync_adjust = self.protocol.config.get_value(CONF_SYNC_ADJUST, 0)
        # A positive sync adjust plays earlier: it is the time the player takes to make
        # its audio heard.
        shift_us = -sync_adjust * 1000 if isinstance(sync_adjust, int) else 0
        if audio_format is None:
            audio_format = self._role_format()
        self._session = session = BridgeSession(audio_format, shift_us, chunks)
        if self._player_clock.to_jiffies(self._clock.now_us()) is not None:
            session.clock_mapped.set()
        self.logger.debug(
            "Sendspin stream on %s is %d Hz / %d bit, sent as %s",
            self.protocol.display_name,
            audio_format.sample_rate,
            audio_format.bit_depth,
            "FLAC" if audio_format.content_type == ContentType.FLAC else "PCM",
        )
        # Started on the next loop iteration, when the session is in place.
        session.tasks = [
            self.mass.create_task(self._start_session(session), eager_start=False),
            self.mass.create_task(self._poll_status(session), eager_start=False),
        ]

    def _close_session(
        self, session: BridgeSession, *, stop_player: bool = False, keep_media: bool = False
    ) -> None:
        """
        End a session and let go of the player.

        :param session: The session to end.
        :param stop_player: Stop the player at once instead of leaving it to what follows.
        :param keep_media: Leave the track on the player: another session takes over, or
            its group stopped.
        """
        session.close()
        if self._session is session:
            self._session = None
        self._pending_status.clear()
        if stop_player:
            # never a power or mute command: those switch the output of the player off
            self.mass.create_task(self._client.stop())
        if not keep_media:
            # a stop keeps the media that played: the player would go on showing the
            # track of a group it left
            self.mass.create_task(self._client.clear_media())

    def _clear_stopped_track(self) -> None:
        """Clear the track a stop of its group left on the player."""
        if self._holds_bridge_track():
            self.mass.create_task(self._client.clear_media())

    def _holds_bridge_track(self) -> bool:
        """Return whether the track on the player is that of a stream of the bridge."""
        return (media := self._client.current_media) is not None and BRIDGE_STREAM_PATH in media.url

    def _bit_depth_limit(self) -> int | None:
        """
        Return the most bits a sample of the next stream has, None when that is not known.

        That is what a lossless output of its playback session has: the bits of its first
        item, and for a track all of them once volume normalization or DSP works on it. A
        player that is set to a lossy output codec gets PCM in its place, in 16 bits
        whatever plays.
        """
        if self.protocol.config.get_value(CONF_OUTPUT_CODEC, "flac") in ("mp3", "aac"):
            return 16
        if not (client_id := self._bridge_client_id):
            return None
        bridge_client = cast("SendspinPlayer | None", self.mass.players.get_protocol(client_id))
        return bridge_client.session_bit_depth if bridge_client else None

    def _role_format(self) -> AudioFormat:
        """
        Return the format of a new stream.

        That is the PCM format the role of the bridge asks Sendspin for, sent as FLAC to a
        player that is set to it and says that it plays it, unless a FLAC stream failed
        on it before it played ``MIN_PLAYED_US``.
        """
        requirements = self._bridge_role.preferred_format if self._bridge_role else None
        sample_rate = requirements.sample_rate if requirements else BRIDGE_SAMPLE_RATE
        bit_depth = requirements.bit_depth if requirements else BRIDGE_BIT_DEPTH
        flac = (
            self.protocol.config.get_value(CONF_OUTPUT_CODEC, "flac") == "flac"
            and "flc" in self._client.supported_codecs
            and not self._flac_failed
        )
        return AudioFormat(
            content_type=ContentType.FLAC if flac else ContentType.from_bit_depth(bit_depth),
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=BRIDGE_CHANNELS,
        )

    async def _start_session(self, session: BridgeSession) -> None:
        """Put the stream of a session on the player and start it at its instant."""
        # the replies of a player that sets a stream up are no good to learn its clock from
        with suppress(TimeoutError):
            async with asyncio.timeout(CLOCK_TIMEOUT_SECONDS):
                await session.clock_mapped.wait()
        try:
            self.protocol.release_native_playback()
            url = (
                f"{self.mass.streams.base_url}{BRIDGE_STREAM_PATH}"
                f"?player_id={self.protocol.protocol_id}&sid={session.session_id}"
            )
            now_playing = self._now_playing()
            # flushes what the player holds and powers it on
            await self._client.play_url(
                url=url,
                mime_type=session.mime_type,
                metadata=now_playing,
                send_flush=True,
                autostart=False,
                stream_threshold=STREAM_THRESHOLD_KB,
                output_threshold=OUTPUT_THRESHOLD,
            )
            if (current := self._now_playing()) != now_playing:
                # the group moved on while the request went out, which that update missed
                await self._client.update_metadata(current)
            session.stream_requested = True
            async with asyncio.timeout(START_TIMEOUT_SECONDS):
                await session.start_decided.wait()
            if (ahead_us := session.command_us - self._clock.now_us()) > MAX_START_AHEAD_US:
                await asyncio.sleep((ahead_us - MAX_START_AHEAD_US) / 1_000_000)
            jiffies = self._player_clock.to_jiffies(session.command_us)
            assert jiffies is not None  # the start is only decided with a mapped clock
            await self._client.unpause_at(jiffies)
            session.started = True
        except TimeoutError:
            self._recover(session, "it did not get ready to play")
        except Exception as err:  # noqa: BLE001 - any failure recovers the player
            self.logger.warning(
                "Failed to start the Sendspin stream on %s: %s", self.protocol.display_name, err
            )
            self._recover(session, "its stream did not start")

    def _now_playing(self) -> MediaMetadata:
        """Return what the player of the bridge plays now, as the metadata of its stream."""
        player = self.protocol.player
        if player is None or (media := player.current_media) is None:
            return cast("MediaMetadata", {})
        # as for a stream of the protocol itself, less the queue item: that one is not
        # played by this protocol
        metadata = cast(
            "MediaMetadata",
            {
                "item_id": media.uri,
                "title": media.title,
                "album": media.album,
                "artist": media.artist,
                "image_url": media.image_url,
                "duration": media.duration,
            },
        )
        if (session := self._session) is not None:
            # the format the player gets, which need not be that of the source
            metadata["samplerate"] = str(session.audio_format.sample_rate)
            metadata["samplesize"] = str(session.audio_format.bit_depth)
        return metadata

    def _decide_start(self, session: BridgeSession) -> None:
        """Decide when the stream of a session starts, once everything that takes is known."""
        if (
            session.start_decided.is_set()
            or not session.buffer_ready
            or session.first_audible_us is None
        ):
            return
        now_us = self._clock.now_us()
        if self._player_clock.to_jiffies(now_us) is None:
            return
        offset_us = self.provider.bridge_manager.get_start_offset_us(self.protocol.protocol_id)
        # the lead-in plays first: silence up to the instant of the first chunk
        wanted_us = session.first_audible_us - LEAD_IN_US
        start_us = max(wanted_us, now_us + MIN_START_AHEAD_US + offset_us)
        session.start_us = start_us
        # the player starts a little later than it is told to, which was learned before
        session.command_us = start_us - offset_us
        # audio that is due by the start leaves no silence to correct the start in: it
        # goes out from the first write, and the start is corrected in it
        session.aligning = session.first_audible_us > start_us
        session.loop = SyncLoop(
            session.sample_rate,
            session.frame_size,
            start_us,
            start_jump_ms=ALIGN_JUMP_MS if session.aligning else START_JUMP_MS,
            told_us=session.command_us,
        )
        # the preroll went out before the timeline was known: it holds frame 0 and on
        session.loop.process(session.preroll)
        session.start_decided.set()
        self.logger.debug(
            "Sendspin stream on %s starts in %.0f ms, %.0f ms ahead of its audio",
            self.protocol.display_name,
            (start_us - now_us) / 1000,
            (session.first_audible_us - start_us) / 1000,
        )

    def _next_block(self, session: BridgeSession) -> bytes:
        """Return the audio to send to the player now, nothing while it holds enough."""
        if (loop := session.loop) is None:
            return b""
        now_us = self._clock.now_us()
        if session.aligning:
            silence = self._align(session, loop, now_us)
            # once its start is corrected, its audio follows at once
            return silence if session.aligning else silence + self._next_block(session)
        # negative until the start
        since_start_us = now_us - session.start_us
        played = session.frames(max(0, since_start_us))
        cursor = loop.frames_consumed
        # the stream is as far ahead as the player can decode it
        decodable = cursor - session.frames_held
        max_frames = session.max_block
        lead_us = STREAM_LEAD_US if loop.on_timeline else START_LEAD_US
        # counted from the timeline, not from what went out before: after a write the
        # player kept waiting, the stream is back at its lead in blocks of this size
        if (frames := min(played + session.frames(lead_us) - decodable, max_frames)) <= 0:
            return b""
        if pcm := session.take(cursor, frames):
            session.starved = False
        else:
            # counted from the real time to the start: silence sent while the player still
            # waits for it would take the place of audio that is yet to arrive. And from
            # all that went out: audio for what follows it can arrive until it is this
            # close to being played, or until what the player can decode of it is that close.
            starved_until = max(
                session.frames(since_start_us + STARVED_LEAD_US),
                session.frames(since_start_us + STARVED_DECODE_LEAD_US) + session.frames_held,
            )
            if (frames := min(starved_until - cursor, max_frames)) <= 0:
                return b""
            pcm = bytes(frames * session.frame_size)
            if not session.starved:
                session.starved = True
                self.logger.debug(
                    "Sendspin has no audio for %s: silence keeps its stream going",
                    self.protocol.display_name,
                )
        return loop.process(pcm)

    async def _write(self, session: BridgeSession, resp: web.StreamResponse, data: bytes) -> None:
        """
        Write the next part of the stream of a session to its player.

        :param session: The session of the stream.
        :param resp: The response the player reads the stream from.
        :param data: The bytes to write: PCM, or the frames of a FLAC stream.
        """
        session.write_started_us = started_us = self._clock.now_us()
        # waits while the player and the connection to it hold all they can
        await resp.write(data)
        session.write_started_us = None
        session.bytes_written += len(data)
        session.write_took_us = self._clock.now_us() - started_us
        if session.write_took_us > SLOW_WRITE_US:
            self.logger.debug(
                "%s took nothing of its stream for %.0f ms",
                self.protocol.display_name,
                session.write_took_us / 1000,
            )

    def _supply(self, session: BridgeSession, loop: SyncLoop) -> str:
        """
        Return what is known of the audio a player was supplied with, for the log.

        :param session: The session of the stream.
        :param loop: The sync loop of that session.
        """
        now_us = self._clock.now_us()
        # with the block a write waits for
        prepared_us = session.start_us + loop.frames_consumed * 1_000_000 // session.sample_rate
        if (started_us := session.write_started_us) is not None:
            write = f"a write waits for {(now_us - started_us) / 1000:.0f} ms"
        else:
            write = f"the last write took {session.write_took_us / 1000:.0f} ms"
        if (status := session.status) is None:
            player = "no status of the player"
        else:
            player = (
                f"the player got {status.bytes_received} bytes and held "
                f"{status.stream_buffer_fullness} of {status.stream_buffer_size} in its "
                f"stream buffer and {status.output_buffer_fullness} of "
                f"{status.output_buffer_size} in its output buffer "
                f"{(now_us - status.received * 1_000_000) / 1000:.0f} ms ago"
            )
        held = ""
        if held_frames := session.frames_held:
            held = f" with {held_frames * 1000 // session.sample_rate} ms it can not decode yet"
        return (
            f"prepared {(prepared_us - now_us) / 1000:.0f} ms ahead{held}, "
            f"{session.bytes_written} bytes written, {write}, "
            f"{session.pending_us / 1000:.0f} ms of audio waiting, {player}"
        )

    def _played_us(self, session: BridgeSession) -> int:
        """
        Return how much of its stream a player has played by its last status, in microseconds.

        :param session: The session of the stream.
        """
        if (status := session.status) is None:
            return 0
        # a report may still be about the stream before: a player has played no more of
        # this one than the time since it was told to start
        since_told_us = round(status.received * 1_000_000) - session.command_us
        return max(0, min(status.elapsed_milliseconds * 1000, since_told_us))

    def _align(self, session: BridgeSession, loop: SyncLoop, now_us: int) -> bytes:
        """
        Return the silence to send to a player whose start is not corrected yet.

        :param session: The session of the stream.
        :param loop: The sync loop of that session.
        :param now_us: The time on the server clock.
        """
        assert session.first_audible_us is not None  # the start is decided with a first chunk
        lead_in_us = session.first_audible_us - session.start_us
        taken_us = max(0, now_us - session.command_us)
        # the lead is what the player can decode: the room for a late start stays as it is
        ahead = max(
            session.frames(taken_us + ALIGN_LEAD_US) + session.frames_held,
            session.frames(lead_in_us - ALIGN_ROOM_US),
        )
        frames = max(0, ahead - loop.frames_sent)
        frames = min(frames, session.max_block)
        # what is left out for a late player comes on top: it is sent no less for it
        frames += max(0, loop.pending_jump)
        silence = loop.process(bytes(frames * session.frame_size))
        # a start error that is known is corrected in what went out just now
        if loop.start_error_ms is not None or now_us - session.start_us > ALIGN_TIMEOUT_US:
            self._end_alignment(session, loop)
        return silence

    def _end_alignment(self, session: BridgeSession, loop: SyncLoop) -> None:
        """
        Let the audio of a stream go to its player from here on.

        :param session: The session of the stream.
        :param loop: The sync loop of that session.
        """
        session.aligning = False
        # a start error that is measured from here on is corrected in the audio
        loop.start_jump_ms = START_JUMP_MS
        assert session.first_audible_us is not None  # the start is decided with a first chunk
        sent_us = loop.frames_consumed * 1_000_000 // session.sample_rate
        left_out_ms = max(0, session.start_us + sent_us - session.first_audible_us) / 1000
        if (start_error_ms := loop.start_error_ms) is None:
            self.logger.debug(
                "%s did not show where it plays in time: its audio goes out with its start "
                "not corrected, %.0f ms of it left out",
                self.protocol.display_name,
                left_out_ms,
            )
            return
        self.logger.debug(
            "%s plays its audio on its timeline: its start was %+.1f ms off, which is "
            "corrected in the silence before it, %.0f ms of its first audio left out",
            self.protocol.display_name,
            start_error_ms,
            left_out_ms,
        )

    async def _poll_status(self, session: BridgeSession) -> None:
        """Ask the player for its status for as long as a session lasts."""
        while not session.ended:
            self._status_token = (self._status_token + 1) % STATUS_TOKEN_BIT
            token = STATUS_TOKEN_BIT | self._status_token
            now_us = self._clock.now_us()
            self._pending_status[token] = now_us
            while len(self._pending_status) > MAX_PENDING_STATUS:
                del self._pending_status[next(iter(self._pending_status))]
            await self._client.request_status(token)
            await asyncio.sleep(self._poll_interval(session, now_us))

    def _poll_interval(self, session: BridgeSession, now_us: int) -> float:
        """
        Return the time until the player is asked for its status again, in seconds.

        :param session: The session the player is asked for.
        :param now_us: The time on the server clock.
        """
        if self._player_clock.to_jiffies(now_us) is None:
            return STATUS_POLL_INTERVAL_FAST
        if not session.aligning or not session.start_decided.is_set():
            return STATUS_POLL_INTERVAL
        # the start is measured from the reports after the instant the player is told to start
        until_start = (session.command_us - now_us) / 1_000_000
        return min(STATUS_POLL_INTERVAL, max(STATUS_POLL_INTERVAL_FAST, until_start))

    def _on_status(self, status: PlayerStatus) -> None:
        """Take in a status of the player: its clock, and how far it played the stream."""
        if (sent_us := self._pending_status.get(status.server_timestamp)) is not None:
            # replies come in the order of their requests: the ones before it are lost
            for token in list(self._pending_status):
                del self._pending_status[token]
                if token == status.server_timestamp:
                    break
            self._clock_sampled_us = round(status.received * 1_000_000)
            generation = self._player_clock.generation
            self._player_clock.add_sample(status.jiffies, sent_us, self._clock_sampled_us)
            if self._player_clock.generation != generation:
                self.logger.debug(
                    "The clock of %s jumped: its mapping starts over",
                    self.protocol.display_name,
                )
        session = self._session
        if session is None or session.ended:
            return
        if (loop := session.loop) is None:
            if self._player_clock.to_jiffies(self._clock.now_us()) is not None:
                session.clock_mapped.set()
            self._decide_start(session)
            return
        session.status = status
        session.played_us = max(session.played_us, self._played_us(session))
        if session.played_us >= MIN_PLAYED_US:
            # the player plays what it is sent: what failed before says nothing now
            self._quick_failures = 0
            self._rejoined = False
        if (server_us := self._player_clock.to_server_us(status.jiffies)) is None:
            return
        if (
            loop.start_error_ms is None
            and self._clock.now_us() - session.start_us > ALIGN_TIMEOUT_US
        ):
            # a start that is not measured in time: whatever it reports from here on is
            # measured, so a start that is far off still restarts the stream
            loop.lift_start_bound()
        start_reports = loop.start_reports
        report_ms = loop.on_report(
            server_us, status.elapsed_milliseconds, self._player_clock.generation
        )
        if session.aligning and server_us >= session.command_us:
            since_start_ms = (server_us - session.start_us) / 1000
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "%s reports %d ms played %+.0f ms after its start instant: %+.1f ms off, %s",
                self.protocol.display_name,
                status.elapsed_milliseconds,
                since_start_ms,
                since_start_ms - status.elapsed_milliseconds - 0.5,
                # the first of them again when the reports before it did not agree with it
                f"counted, {len(loop.start_reports)} in a row"
                if loop.start_reports and loop.start_reports != start_reports
                else "not counted",
            )
        self._reports += 1
        if self._reports % 40 == 0 and (error_ms := loop.error_ms) is not None:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "%s is %+.2f ms off its timeline (correcting %+.0f ppm, drift %+.1f ppm, "
                "clock drift %+.1f ppm)",
                self.protocol.display_name,
                error_ms,
                loop.rate_ppm,
                loop.drift_ppm,
                self._player_clock.drift_ppm,
            )
        if report_ms is not None:
            if abs(report_ms) <= MAX_TIMELINE_ERROR_MS:
                session.off_timeline_since_us = None
            elif session.off_timeline_since_us is None:
                session.off_timeline_since_us = server_us
            elif server_us - session.off_timeline_since_us > MAX_OFF_TIMELINE_US:
                self._recover(session, f"it stays {report_ms:.0f} ms off its timeline")
                return
        if session.start_error_taken or (start_error_ms := loop.start_error_ms) is None:
            return
        session.start_error_taken = True
        if abs(start_error_ms) > MAX_START_JUMP_MS:
            self._recover(session, f"it started {start_error_ms:.0f} ms off its timeline")
            return
        offset_us = self.provider.bridge_manager.learn_start_offset(
            self.protocol.protocol_id, start_error_ms
        )
        self.logger.debug(
            "%s started %+.1f ms off its timeline, its next start is told %.0f ms earlier",
            self.protocol.display_name,
            start_error_ms,
            offset_us / 1000,
        )

    def _follow_player(self, session: BridgeSession) -> None:
        """Act on what was done on the player itself while it plays the stream."""
        if not self._client.powered:
            # switched off on the player: restarting its stream would switch it on again
            self.logger.debug("%s was switched off", self.protocol.display_name)
            self.stop_streaming()
        elif self._client.state == SlimPlayerState.PAUSED and not session.pause_forwarded:
            # paused on the player, which aioslimproto handles without asking: the group
            # plays on, so hand the pause to the player that leads it
            session.pause_forwarded = True
            if (target_id := self.transport_command_target()) is not None:
                self.mass.create_task(self._forward_pause(target_id))

    async def _forward_pause(self, player_id: str) -> None:
        """
        Pause the player that leads the Sendspin session of the bridge.

        :param player_id: The id of that player.
        """
        try:
            await self.mass.players.cmd_pause(player_id)
        except MusicAssistantError as err:
            self.logger.warning(
                "Failed to handle the pause of %s: %s", self.protocol.display_name, err
            )

    def _recover(self, session: BridgeSession, reason: str) -> None:
        """
        Restart a session that failed, unless the player keeps failing before it plays.

        A FLAC stream that failed before it played ``MIN_PLAYED_US`` is restarted as PCM.

        :param session: The session that failed.
        :param reason: What the player did, for the log.
        """
        if session is not self._session or session.ended:
            return
        played_us = session.played_us
        if quick := played_us < MIN_PLAYED_US:
            self._quick_failures += 1
        played_s = played_us / 1_000_000
        min_played_s = MIN_PLAYED_US / 1_000_000
        audio_format = session.audio_format
        if quick and audio_format.content_type == ContentType.FLAC:
            # the decoder of the player may be what kept it from playing: PCM needs none
            self._flac_failed = True
            audio_format = replace(
                audio_format, content_type=ContentType.from_bit_depth(audio_format.bit_depth)
            )
            self.logger.warning(
                "%s is sent PCM instead of FLAC from now on: its FLAC stream failed before "
                "it played %.0f s",
                self.protocol.display_name,
                min_played_s,
            )
        if self._quick_failures >= MAX_QUICK_FAILURES:
            self.logger.error(
                "Giving up on %s and taking it out of the Sendspin session: its stream failed "
                "%d times in a row before it played %.0f s, now after %.1f s: %s",
                self.protocol.display_name,
                self._quick_failures,
                min_played_s,
                played_s,
                reason,
            )
            # Sendspin reports playback from the state of the group, so a bridge that
            # only stopped feeding would leave its player on playing with nothing heard.
            self._close_session(session, stop_player=True)
            self.mass.create_task(self._leave_sendspin_session())
            return
        count = (
            f" (failure {self._quick_failures} of {MAX_QUICK_FAILURES} in a row before "
            f"{min_played_s:.0f} s)"
            if quick
            else ""
        )
        self.logger.warning(
            "Sendspin stream on %s is restarted after it played %.1f s%s: %s",
            self.protocol.display_name,
            played_s,
            count,
            reason,
        )
        self._begin_session(session.unplayed(self._clock.now_us()), audio_format)

    async def _leave_sendspin_session(self, *, rejoin: bool = True) -> None:
        """
        Take the bridge out of Sendspin playback and line up its return.

        A shared group plays on without this player, and the bridge is given a bounded
        attempt to re-join it. A solo group has nothing to re-join: leaving is what
        stops it. Either way the client stays registered.

        :param rejoin: Whether to line up a return to the group the bridge left. False
            for a stop the user asked for.
        """
        if not (client := self._sendspin_client):
            return
        # Captured before leaving, which is what moves the client to a solo group and has
        # its return forgotten with that.
        group = client.group
        rejoined = self._rejoined
        try:
            previous_group_id = await client.quiesce_to_solo_stopped()
        except Exception as err:  # noqa: BLE001 - library code, the player stays registered
            self.logger.warning(
                "Could not take %s out of its Sendspin session: %s",
                self.protocol.display_name,
                err,
            )
            return
        if previous_group_id is None:
            return
        # Whichever way this goes, an older schedule must not outlive it.
        self._cancel_rejoin()
        if not rejoin:
            return
        if rejoined:
            self.logger.warning(
                "%s gave up again before it played %.0f s since it was re-joined, leaving it "
                "out of the group until it is grouped again",
                self.protocol.display_name,
                MIN_PLAYED_US / 1_000_000,
            )
            return
        self._schedule_rejoin(group, REJOIN_ATTEMPT_DELAYS)

    def _schedule_rejoin(self, group: SendspinGroup, delays: tuple[int, ...]) -> None:
        """
        Line up the return of the bridge to a Sendspin group.

        :param group: The Sendspin group to return to.
        :param delays: Seconds to wait before each attempt.
        """
        self._cancel_rejoin()
        self._rejoin_group = group
        self._rejoin_task = self.mass.create_task(self._rejoin_attempts(group, delays))

    async def _rejoin_attempts(self, group: SendspinGroup, delays: tuple[int, ...]) -> None:
        """
        Put the bridge back into a Sendspin group.

        :param group: The Sendspin group to return to.
        :param delays: Seconds to wait before each attempt.
        """
        name = self.protocol.display_name
        try:
            for attempt, delay in enumerate(delays, start=1):
                await asyncio.sleep(delay)
                if not (client := self._sendspin_client) or not self._bridge_client_id:
                    return
                if len(client.group.clients) > 1 or client.group.has_active_stream:
                    # given a group or a stream of its own meanwhile
                    self.logger.debug("Not re-joining %s: it is in use again already", name)
                    return
                if self._client.state != SlimPlayerState.STOPPED:
                    self.logger.debug("Not re-joining %s: it plays outside the bridge", name)
                    return
                bridge_client = self.mass.players.get_protocol(self._bridge_client_id)
                if bridge_client is None or bridge_client.player is None:
                    # the Sendspin protocol of a new bridge is attached a moment after it
                    self.logger.debug("Not re-joining %s yet: it is not ready", name)
                    continue
                if not group.clients:
                    self.logger.debug("Not re-joining %s: its group no longer exists", name)
                    return
                try:
                    await group.add_client(client)
                except Exception as err:  # noqa: BLE001 - a failed re-join attempt is retried
                    self.logger.warning(
                        "Could not re-join %s to its Sendspin group (attempt %d/%d): %s",
                        name,
                        attempt,
                        len(delays),
                        err,
                    )
                    continue
                self._rejoined = True
                self.logger.info("Re-joined %s to its Sendspin group", name)
                return
            self.logger.warning(
                "Giving up on re-joining %s to its Sendspin group after %d attempt(s)",
                name,
                len(delays),
            )
        finally:
            if self._rejoin_task is asyncio.current_task():
                self._rejoin_task = None
                self._rejoin_group = None

    def _cancel_rejoin(self) -> None:
        """Drop any pending attempt to re-join a Sendspin group."""
        rejoin_task = self._rejoin_task
        # never self-cancel: a re-join announces itself through the same stream start
        # that clears stale schedules
        if rejoin_task is None or rejoin_task is asyncio.current_task():
            return
        self._rejoin_task = None
        self._rejoin_group = None
        if not rejoin_task.done():
            rejoin_task.cancel()


class SendspinBridgeManager(SendspinBridgeManagerBase[SendspinSqueezeliteBridge]):
    """Manages the Sendspin bridges of all squeezelite players."""

    def __init__(self, provider: SqueezelitePlayerProvider) -> None:
        """
        Initialize the bridge manager.

        :param provider: The squeezelite provider owning the bridged players.
        """
        super().__init__(provider)
        # protocol id -> how much later than told its player starts, in microseconds
        self._start_offsets_us: dict[str, int] = {}
        # protocol id -> the group the bridge of a disconnected player was in, and until
        # when (loop time) the bridge of its next connection returns to it
        self._groups_to_return_to: dict[str, tuple[SendspinGroup, float]] = {}

    async def setup_bridge(self, protocol: PlayerProtocol) -> None:
        """
        Set up a Sendspin bridge for the given protocol.

        The bridge of a player that connected again returns to the Sendspin group the
        bridge of its previous connection was in.

        :param protocol: The protocol to bridge.
        """
        protocol_id = protocol.protocol_id
        # known before the bridge can start a stream
        await self._restore_start_offset(protocol_id)
        await super().setup_bridge(protocol)
        if (bridge := self._bridges.get(protocol_id)) is None:
            return
        if (remembered := self._groups_to_return_to.pop(protocol_id, None)) is None:
            return
        group, until = remembered
        if self.mass.loop.time() < until:
            bridge.return_to_group(group)

    async def remove_bridge(self, protocol_id: str, permanent: bool = False) -> None:
        """
        Remove the Sendspin bridge of a protocol.

        :param protocol_id: The id of the bridged protocol.
        :param permanent: Also remove the Sendspin protocol of the bridge client and its
            config, when it rides on this protocol.
        """
        group: SendspinGroup | None = None
        if (bridge := self._bridges.get(protocol_id)) is not None and (
            bridge.protocol.player is None
        ):
            # A protocol that is detached with its bridge in place is a player that
            # disconnected. It connects again as a new protocol with a new bridge, whose
            # client is not in the group this one is in.
            group = bridge.shared_group
        await super().remove_bridge(protocol_id, permanent=permanent)
        if permanent:
            self._groups_to_return_to.pop(protocol_id, None)
        elif group is not None:
            self._groups_to_return_to[protocol_id] = (
                group,
                self.mass.loop.time() + RECONNECT_REJOIN_WINDOW,
            )

    async def close(self) -> None:
        """Stop all bridges and unsubscribe event listeners."""
        self._groups_to_return_to.clear()
        await super().close()

    def get_transport_command_target(self, protocol_id: str) -> str | None:
        """
        Return the player whose Sendspin session controls a squeezelite player.

        :param protocol_id: The id of the squeezelite protocol reporting the command.
        :return: The id of the player to send the command to; None when Sendspin does not
            currently control the squeezelite protocol.
        """
        if bridge := self._bridges.get(protocol_id):
            return bridge.transport_command_target()
        return None

    def stop_streaming(self, protocol_id: str) -> bool:
        """
        Stop streaming for a bridged squeezelite protocol, and leave its Sendspin session.

        The player itself is left to the caller.

        :param protocol_id: The id of the squeezelite protocol.
        :return: True if its bridge had a stream on the player, False otherwise.
        """
        if bridge := self._bridges.get(protocol_id):
            return bridge.stop_streaming()
        return False

    def has_stream(self) -> bool:
        """Return whether any bridge has a stream on its player."""
        return any(bridge.owns_stream for bridge in self._bridges.values())

    def get_start_offset_us(self, protocol_id: str) -> int:
        """
        Return how much later than told a player starts its stream, in microseconds.

        :param protocol_id: The id of the squeezelite protocol.
        """
        return self._start_offsets_us.get(protocol_id, 0)

    def learn_start_offset(self, protocol_id: str, start_error_ms: float) -> int:
        """
        Take in how far off its timeline a player started a stream.

        :param protocol_id: The id of the squeezelite protocol.
        :param start_error_ms: The start error of the stream, positive when it was late.
        :return: The start offset of the player from now on, in microseconds.
        """
        error_us = round(start_error_ms * 1000)
        offset_us = self._start_offsets_us.get(protocol_id, 0)
        # The offset is the shortest the player takes to start: a start that was early
        # is taken whole, a late one for an eighth. A start now and then takes hundreds
        # of milliseconds longer, and following those would start the next ones early.
        # A player does not start before it is told to.
        offset_us = max(0, offset_us + (error_us if error_us < 0 else error_us // 8))
        if offset_us != self._start_offsets_us.get(protocol_id):
            self._start_offsets_us[protocol_id] = offset_us
            self.mass.create_task(
                self.mass.cache.set(
                    key=protocol_id,
                    data=offset_us,
                    provider=self.provider.instance_id,
                    category=CACHE_CATEGORY_START_OFFSET,
                )
            )
        return offset_us

    def qualifies(self, protocol: PlayerProtocol) -> bool:
        """
        Return whether a squeezelite player qualifies for a Sendspin bridge.

        :param protocol: The squeezelite protocol to evaluate.
        """
        squeezelite_player = cast("SqueezelitePlayer", protocol)
        if not get_bridge_client_id(squeezelite_player):
            return False
        # the bridge starts a stream at a set instant, which the first Squeezebox, a
        # SqueezeSlave and an unknown device can not do
        if squeezelite_player.client.device_type not in SQUEEZEBOX2_PLAYER_TYPES:
            return False
        # a bridge tool plays to a device of another protocol, which is bridged over that
        return not is_bridge_tool(squeezelite_player.client.device_model)

    async def _restore_start_offset(self, protocol_id: str) -> None:
        """
        Take the start offset of a player from the cache, unless it is known already.

        :param protocol_id: The id of the squeezelite protocol.
        """
        if protocol_id in self._start_offsets_us:
            return
        offset_us = await self.mass.cache.get(
            key=protocol_id,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_START_OFFSET,
        )
        if isinstance(offset_us, int):
            self._start_offsets_us.setdefault(protocol_id, offset_us)

    def _own_client_id(self, protocol: PlayerProtocol) -> str | None:
        """Return the Sendspin client_id a bridge gives the device of a squeezelite player."""
        return get_bridge_client_id(cast("SqueezelitePlayer", protocol))

    def _create_bridge(self, protocol: PlayerProtocol) -> SendspinSqueezeliteBridge:
        """Create a bridge instance for a squeezelite player."""
        sendspin_server = self.sendspin_server
        assert sendspin_server is not None  # guaranteed by _lifecycle_allows_bridge
        return SendspinSqueezeliteBridge(
            cast("SqueezelitePlayerProvider", self.provider),
            cast("SqueezelitePlayer", protocol),
            sendspin_server,
        )

    def _on_player_state_updated(
        self, updated_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Follow a player with the bridges of its squeezelite protocols: its name and its outputs."""
        if "name" in changed_values:
            for protocol_id in updated_player.attached_protocols:
                if bridge := self._bridges.get(protocol_id):
                    bridge.update_name()
        super()._on_player_state_updated(updated_player, changed_values)


def _supported_formats(protocol: SqueezelitePlayer) -> list[SupportedAudioFormat]:
    """
    Return the PCM formats of a player, for the hello of its bridge client.

    The first one is what the bridge plays when Sendspin prefers no format: the CD sample
    rate where the player plays it, at its highest bit depth.

    :param protocol: The squeezelite player the bridge is for.
    """
    formats = protocol.get_supported_sample_rates() or [(BRIDGE_SAMPLE_RATE, BRIDGE_BIT_DEPTH)]
    return [
        SupportedAudioFormat(
            codec=AudioCodec.PCM,
            channels=BRIDGE_CHANNELS,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
        )
        for sample_rate, bit_depth in sorted(
            formats, key=lambda fmt: (fmt[0] != BRIDGE_SAMPLE_RATE, fmt[0], -fmt[1])
        )
    ]


def _pack_24_bit(pcm: bytes) -> bytes:
    """
    Return 24-bit samples in 3 bytes each.

    :param pcm: The samples in 4 bytes each, little endian, the lowest byte unused.
    """
    packed = bytearray(len(pcm) // 4 * 3)
    packed[0::3] = pcm[1::4]
    packed[1::3] = pcm[2::4]
    packed[2::3] = pcm[3::4]
    return bytes(packed)
