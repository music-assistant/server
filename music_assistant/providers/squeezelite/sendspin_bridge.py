"""
Sendspin bridge for slimproto players: lets Sendspin stream to Squeezebox and squeezelite.

The bridge registers a slimproto player as an external Sendspin client. The Sendspin
provider creates a SendspinPlayer for that client, which rides on the squeezelite
protocol and is attached to its player as one of its outputs. Sendspin handles the
synchronization and the timing; the bridge puts its timeline on the player.

Every Sendspin stream is a session: one PCM stream over HTTP that the player starts
at an instant of its own clock. The bridge keeps that stream a short lead ahead of
what the player plays, and the sync loop holds it on the timeline from the positions
the player reports.

Audio flow:
Sendspin PushStream -> BridgePlayerRole.on_audio_chunk -> session -> HTTP stream -> player
"""

from __future__ import annotations

import asyncio
import secrets
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aioslimproto.models import SQUEEZEBOX2_PLAYER_TYPES, PlayerStatus
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from music_assistant_models.enums import ContentType, IdentifierType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.providers.sendspin.bridge_manager import SendspinBridgeManagerBase
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_SAMPLE_RATE,
)
from music_assistant.providers.sendspin.helpers import (
    bridge_client_id_from_mac,
    bridge_client_id_variants,
)

from .bridge_role import SLIMPROTO_BRIDGE_ROLE_ID, SlimprotoBridgeRole
from .bridge_sync import MAX_START_JUMP_MS, PlayerClock, SyncLoop

if TYPE_CHECKING:
    from aiosendspin.server import (
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
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .player import SqueezelitePlayer
    from .provider import SqueezelitePlayerProvider


# The stream of a session is served on this path, to the player and session of its query.
BRIDGE_STREAM_PATH = "/slimproto/bridge"

# Audio the stream is kept ahead of what the player plays. A correction of the sync loop
# is only heard once this much has played, so it is short; it still has to ride out a
# hiccup of the network.
STREAM_LEAD_US: int = 1_500_000

# Silence the stream opens with. A player only reports its buffer ready once it holds
# more than the stream threshold, and the instant its first audio is heard can only be
# decided then: the silence is what it holds by that time.
PREROLL_US: int = 250_000
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
# flush, the stream request with its HTTP connect, the preroll that fills the player to
# its threshold and a start MIN_START_AHEAD_US ahead, plus what the player is known to
# start late. The buffer is what Sendspin keeps the bridge supplied with during the
# stream: the stream lead and a margin.
BRIDGE_REQUIRED_LEAD_MS: int = 2000
BRIDGE_MIN_BUFFER_MS: int = 2000

# The player is asked for its status this often (seconds) during a session, faster while
# its clock is not mapped yet, so the first start does not have to wait for it.
STATUS_POLL_INTERVAL: float = 0.25
STATUS_POLL_INTERVAL_UNMAPPED: float = 0.05
# Status requests still waiting for their reply that are remembered.
MAX_PENDING_STATUS: int = 8
# Status requests carry a token with this bit set: the heartbeat of aioslimproto counts
# up from 1 and a status the player sends unasked carries 0.
STATUS_TOKEN_BIT: int = 1 << 31
# A mapping of the player's clock that got no reply for this long is learned again: the
# clocks drift apart, by a millisecond in ten seconds at 100 ppm.
CLOCK_MAX_AGE_US: int = 10_000_000

# The writer hands the player what it is allowed this often (seconds), at most this
# much audio at a time.
WRITE_INTERVAL: float = 0.02
MAX_BLOCK_US: int = 500_000
# While Sendspin has nothing to play, silence keeps the stream this far ahead, so the
# player does not run dry and stays on the timeline.
STARVED_LEAD_US: int = 500_000

# Caps on the audio that waits for its turn. Sendspin delivers a buffered source up to
# 30 seconds ahead; beyond the caps the oldest chunks go. The count bounds a run of
# chunks without duration.
MAX_PENDING_AUDIO_US: int = 45_000_000
MAX_PENDING_CHUNKS: int = 4_000

# A player that stays this far off its timeline (ms) for this long has its stream
# restarted: the sync loop would need minutes to pull it back. A player that stalled
# without running out of audio ends up like that. The time covers a start error that is
# corrected in a jump, which is only heard once the stream lead has played.
MAX_TIMELINE_ERROR_MS: float = 100.0
MAX_OFF_TIMELINE_US: int = 4_000_000

# A session that fails again this soon after it was restarted is not going to work by
# restarting it once more: the bridge leaves the Sendspin session instead. The same
# window guards its return: a bridge that gives up again this soon after it was put back
# stays out.
RECOVERY_GUARD_US: int = 30_000_000
# Delays (seconds) before each attempt to put a bridge back into the Sendspin group a
# give-up took it out of.
REJOIN_ATTEMPT_DELAYS: tuple[int, ...] = (5, 30)
# The same for the bridge of a player that connected again: it only has to wait for its
# Sendspin protocol to be attached.
RECONNECT_REJOIN_DELAYS: tuple[int, ...] = (1, 5, 30)
# How long (seconds) the group of a bridge is remembered after its player disconnected.
RECONNECT_REJOIN_WINDOW: float = 60.0

# The cache category of what was learned about the start of a player, by protocol id.
CACHE_CATEGORY_START_OFFSET = 2

# Player state fields the bridge policy depends on: an AirPlay output denies the bridge.
_BRIDGE_POLICY_STATE_FIELDS: frozenset[str] = frozenset({"output_protocols"})


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

        :param audio_format: The PCM format of the stream, which its chunks come in.
        :param timeline_shift_us: How much later than its Sendspin timestamp audio is due
            on this player.
        :param chunks: Chunks a session before this one did not play yet.
        """
        self.session_id = secrets.token_hex(8)
        self.audio_format = audio_format
        self.sample_rate = audio_format.sample_rate
        self.frame_size = audio_format.channels * audio_format.bit_depth // 8
        # Players take the PCM format from the content type, which goes in the stream
        # request too.
        self.mime_type = (
            f"audio/pcm;rate={audio_format.sample_rate};bitrate={audio_format.bit_depth}"
            f";channels={audio_format.channels}"
        )
        self.preroll = bytes(self.frames(PREROLL_US) * self.frame_size)
        self.chunks: deque[AudioChunk] = deque()
        self.pending_us = 0
        # instant the first chunk of the stream is due
        self.first_audible_us: int | None = None
        # set once the start is decided: the instant frame 0 of the stream is played, the
        # instant the player is told to start for it and the loop that holds it there
        self.start_us = 0
        self.command_us = 0
        self.loop: SyncLoop | None = None
        self.start_decided = asyncio.Event()
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
        self.start_error_taken = False
        # report time since which every report was far off the timeline
        self.off_timeline_since_us: int | None = None
        self.pause_forwarded = False
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
        size = self.frame_size
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
                self._drop_first()
            else:
                self._head_taken = first + count
        return b"".join(parts)

    def is_fetched_by(self, request: web.Request) -> bool:
        """
        Return whether a request is the one that fetches the stream of this session.

        :param request: A request for the stream of a session.
        """
        return not self.ended and not self.claimed and request.query.get("sid") == self.session_id

    def close(self) -> None:
        """End the session: its stream ends and everything it started is cancelled."""
        self.ended = True
        self.chunks.clear()
        self.pending_us = 0
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()
        self.tasks.clear()

    def _drop_first(self) -> None:
        """Drop the first chunk that waits."""
        self.pending_us -= self.chunks.popleft().duration_us
        self._head_taken = 0


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
        # time of the last restart of a failed session, None when the stream had none yet
        self._last_recovery_us: int | None = None
        self._rejoin_task: asyncio.Task[None] | None = None
        # the group a pending re-join returns the bridge to
        self._rejoin_group: SendspinGroup | None = None
        # time the bridge was last put back into the group a give-up took it out of
        self._last_rejoin_us: int | None = None

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
    def shared_group(self) -> SendspinGroup | None:
        """Return the Sendspin group the bridge shares with other clients or is to return to."""
        if self._rejoin_group is not None:
            return self._rejoin_group
        if (client := self._sendspin_client) is None:
            return None
        return client.group if len(client.group.clients) > 1 else None

    async def start(self) -> None:
        """Register the squeezelite player as an external Sendspin client."""
        bridge_client_id = get_bridge_client_id(self.protocol)
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
        if self.protocol.sendspin_bridge is self:
            self.protocol.sendspin_bridge = None
        if self._sendspin_client is not None and (client_id := self._bridge_client_id):
            self._sendspin_client = None
            self._bridge_role = None
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
            if session.loop is not None:
                self._recover(session, "it ran out of audio")
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
            await resp.write(session.preroll)
            session.preroll_sent = True
            while not session.ended:
                if block := self._next_block(session):
                    await resp.write(block)
                await asyncio.sleep(WRITE_INTERVAL)
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
        self._last_recovery_us = None
        # Joining a session by any means supersedes a pending re-join.
        self._cancel_rejoin()
        self._begin_session()

    def _on_role_stream_start(self) -> None:
        """Start a session for a stream that begins without a stream start request."""
        session = self._session
        if session is not None and not session.ended and session.first_audible_us is None:
            # the session the stream start request of this stream began
            return
        self._begin_session()

    def _on_audio_chunk(self, chunk: AudioChunk) -> None:
        """Handle an audio chunk from the Sendspin PushStream."""
        session = self._session
        if session is None or session.ended:
            return
        if session.audio_format.bit_depth == 24:
            # Sendspin delivers a 24-bit sample in 32 bits, the player takes it in 24
            chunk = replace(chunk, data=_pack_24_bit(chunk.data))
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
        self._close_session(session, stop_player=True)

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
        :param audio_format: The PCM format of that stream. A new stream comes in the
            format the role of the bridge asks Sendspin for.
        """
        if (old := self._session) is not None:
            # no stop for the player: the stream request of the new session flushes it
            self._close_session(old, replaced=True)
        if not self.protocol.available:
            self.logger.warning(
                "Cannot start Sendspin stream for %s: player not available",
                self.protocol.display_name,
            )
            return
        if self._clock.now_us() - self._clock_sampled_us > CLOCK_MAX_AGE_US:
            self._player_clock = PlayerClock()
        sync_adjust = self.protocol.config.get_value(CONF_SYNC_ADJUST, 0)
        # A positive sync adjust plays earlier, as it does for the squeezelite protocol
        # itself, where it is the time the player takes to make its audio heard.
        shift_us = -sync_adjust * 1000 if isinstance(sync_adjust, int) else 0
        if audio_format is None:
            audio_format = self._role_format()
        self._session = session = BridgeSession(audio_format, shift_us, chunks)
        self.logger.debug(
            "Sendspin stream on %s is %d Hz / %d bit",
            self.protocol.display_name,
            audio_format.sample_rate,
            audio_format.bit_depth,
        )
        # Started on the next loop iteration, when the session is in place.
        session.tasks = [
            self.mass.create_task(self._start_session(session), eager_start=False),
            self.mass.create_task(self._poll_status(session), eager_start=False),
        ]

    def _close_session(
        self, session: BridgeSession, *, stop_player: bool = False, replaced: bool = False
    ) -> None:
        """
        End a session and let go of the player.

        :param session: The session to end.
        :param stop_player: Stop the player at once instead of leaving it to what follows.
        :param replaced: Another session takes over: the player shows what plays on.
        """
        session.close()
        if self._session is session:
            self._session = None
        self._pending_status.clear()
        if stop_player:
            # never a power or mute command: those switch the output of the player off
            self.mass.create_task(self._client.stop())
        if not replaced:
            # a stop keeps the media that played: the player would go on showing the
            # track of a group it left
            self.mass.create_task(self._client.clear_media())

    def _role_format(self) -> AudioFormat:
        """Return the PCM format the role of the bridge asks Sendspin for."""
        requirements = self._bridge_role.preferred_format if self._bridge_role else None
        sample_rate = requirements.sample_rate if requirements else BRIDGE_SAMPLE_RATE
        bit_depth = requirements.bit_depth if requirements else BRIDGE_BIT_DEPTH
        return AudioFormat(
            content_type=ContentType.from_bit_depth(bit_depth),
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            channels=BRIDGE_CHANNELS,
        )

    async def _start_session(self, session: BridgeSession) -> None:
        """Put the stream of a session on the player and start it at its instant."""
        try:
            await self.protocol.release_native_playback()
            url = (
                f"{self.mass.streams.base_url}{BRIDGE_STREAM_PATH}"
                f"?player_id={self.protocol.protocol_id}&sid={session.session_id}"
            )
            # flushes what the player holds and powers it on
            await self._client.play_url(
                url=url,
                mime_type=session.mime_type,
                metadata=self._now_playing(),
                send_flush=True,
                autostart=False,
                stream_threshold=STREAM_THRESHOLD_KB,
                output_threshold=OUTPUT_THRESHOLD,
            )
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
        except Exception as err:
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
        return cast(
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
        # the preroll plays first, so the first chunk is heard at its own instant
        wanted_us = session.first_audible_us - PREROLL_US
        start_us = max(wanted_us, now_us + MIN_START_AHEAD_US + offset_us)
        session.start_us = start_us
        # the player starts a little later than it is told to, which was learned before
        session.command_us = start_us - offset_us
        session.loop = SyncLoop(session.sample_rate, session.frame_size, start_us)
        # the preroll went out before the timeline was known: it holds frame 0 and on
        session.loop.process(session.preroll)
        session.start_decided.set()
        self.logger.debug(
            "Sendspin stream on %s starts in %.0f ms, %.0f ms into its audio",
            self.protocol.display_name,
            (start_us - now_us) / 1000,
            (start_us - wanted_us) / 1000,
        )

    def _next_block(self, session: BridgeSession) -> bytes:
        """Return the audio to send to the player now, nothing while it holds enough."""
        if (loop := session.loop) is None:
            return b""
        elapsed_us = max(0, self._clock.now_us() - session.start_us)
        played = session.frames(elapsed_us)
        cursor = loop.frames_consumed
        max_frames = session.frames(MAX_BLOCK_US)
        if (frames := min(played + session.frames(STREAM_LEAD_US) - cursor, max_frames)) <= 0:
            return b""
        if not (pcm := session.take(cursor, frames)):
            starved_lead = session.frames(STARVED_LEAD_US)
            if (frames := min(played + starved_lead - cursor, max_frames)) <= 0:
                return b""
            pcm = bytes(frames * session.frame_size)
        return loop.process(pcm)

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
            mapped = self._player_clock.to_jiffies(now_us) is not None
            await asyncio.sleep(STATUS_POLL_INTERVAL if mapped else STATUS_POLL_INTERVAL_UNMAPPED)

    def _on_status(self, status: PlayerStatus) -> None:
        """Take in a status of the player: its clock, and how far it played the stream."""
        if (sent_us := self._pending_status.get(status.server_timestamp)) is not None:
            # replies come in the order of their requests: the ones before it are lost
            for token in list(self._pending_status):
                del self._pending_status[token]
                if token == status.server_timestamp:
                    break
            self._clock_sampled_us = round(status.received * 1_000_000)
            self._player_clock.add_sample(status.jiffies, sent_us, self._clock_sampled_us)
        session = self._session
        if session is None or session.ended:
            return
        if (loop := session.loop) is None:
            self._decide_start(session)
            return
        if (server_us := self._player_clock.to_server_us(status.jiffies)) is None:
            return
        report_ms = loop.on_report(
            server_us, status.elapsed_milliseconds, self._player_clock.generation
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
        Restart a session that failed, unless the player keeps failing.

        :param session: The session that failed.
        :param reason: What the player did, for the log.
        """
        if session is not self._session or session.ended:
            return
        now_us = self._clock.now_us()
        last_recovery_us = self._last_recovery_us
        if last_recovery_us is not None and now_us - last_recovery_us < RECOVERY_GUARD_US:
            self.logger.error(
                "Sendspin stream on %s failed again within %.0fs of its restart (%s), "
                "giving up and taking it out of the Sendspin session",
                self.protocol.display_name,
                RECOVERY_GUARD_US / 1_000_000,
                reason,
            )
            # Sendspin reports playback from the state of the group, so a bridge that
            # only stopped feeding would leave its player on playing with nothing heard.
            self._close_session(session, stop_player=True)
            self.mass.create_task(self._leave_sendspin_session())
            return
        self._last_recovery_us = now_us
        self.logger.warning(
            "Sendspin stream on %s is restarted: %s", self.protocol.display_name, reason
        )
        chunks, session.chunks = session.chunks, deque()
        self._begin_session(chunks, session.audio_format)

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
        # Captured before leaving, which is what moves the client to a solo group.
        group = client.group
        try:
            previous_group_id = await client.quiesce_to_solo_stopped()
        except Exception as err:
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
        last_rejoin_us = self._last_rejoin_us
        if last_rejoin_us is not None and self._clock.now_us() - last_rejoin_us < RECOVERY_GUARD_US:
            self.logger.warning(
                "%s gave up again within %.0fs of being re-joined, leaving it out of the group",
                self.protocol.display_name,
                RECOVERY_GUARD_US / 1_000_000,
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
                except Exception as err:
                    self.logger.warning(
                        "Could not re-join %s to its Sendspin group (attempt %d/%d): %s",
                        name,
                        attempt,
                        len(delays),
                        err,
                    )
                    continue
                self._last_rejoin_us = self._clock.now_us()
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
        self._unsubs.append(
            self.mass.players.subscribe_player_state_update(self._on_player_state_updated)
        )

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
        if (offset_us := self._start_offsets_us.get(protocol_id)) is None:
            offset_us = error_us
        else:
            # a start scatters by milliseconds: only half of it is taken, so the offset
            # settles on what the player does on average
            offset_us += error_us // 2
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

    def _bridge_client_id(self, protocol: PlayerProtocol) -> str | None:
        """Return the Sendspin client_id used to bridge a squeezelite player."""
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

    def _should_have_bridge(self, protocol: PlayerProtocol) -> bool:
        """Return whether a squeezelite player should have a Sendspin bridge."""
        squeezelite_player = cast("SqueezelitePlayer", protocol)
        if not (bridge_client_id := get_bridge_client_id(squeezelite_player)):
            return False
        # the bridge starts a stream at a set instant, which the first Squeezebox, a
        # SqueezeSlave and an unknown device can not do
        if squeezelite_player.client.device_type not in SQUEEZEBOX2_PLAYER_TYPES:
            return False
        # a device with AirPlay gets its Sendspin support over AirPlay
        if (player := protocol.player) and player.get_output_protocol_by_domain("airplay"):
            return False
        # Same deny before the AirPlay protocol joined the player: another bridge
        # already registered a client for this device.
        return not self._has_foreign_bridge_client(protocol.protocol_id, bridge_client_id)

    def _has_foreign_bridge_client(self, protocol_id: str, bridge_client_id: str) -> bool:
        """
        Return whether another bridge registered a Sendspin client for this device.

        :param protocol_id: The id of the squeezelite protocol being evaluated.
        :param bridge_client_id: The Sendspin client_id its bridge would use.
        """
        if protocol_id in self._bridges or not (sendspin_server := self.sendspin_server):
            return False
        return any(
            sendspin_server.is_external_player(client_id)
            for client_id in bridge_client_id_variants(bridge_client_id)
        )

    def _on_player_state_updated(
        self, updated_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Follow a player with the bridges of its squeezelite protocols: its name and its outputs."""
        if "name" in changed_values:
            for protocol_id in updated_player.attached_protocols:
                if bridge := self._bridges.get(protocol_id):
                    bridge.update_name()
        if not changed_values.keys() & _BRIDGE_POLICY_STATE_FIELDS:
            return
        for protocol in list(updated_player.attached_protocols.values()):
            if protocol.provider is self.provider:
                self.mass.create_task(self.evaluate_bridge(protocol))


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
