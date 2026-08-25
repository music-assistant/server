"""
Spotify Soloist playback backend for the Spotify music provider.

Runs one continuous session of ``soloist``, Spotify's official headless client,
and feeds it one track ahead so the engine plays consecutive tracks without a
break. The session renders into a private PulseAudio capture sink whose FIFO is
read back slightly above realtime pace, and that one continuous audio stream is
handed to Music Assistant as ordinary per-item streams: an item's stream ends
where the session moves on to the next track, and the next item's stream begins
there. Played back to back the items reproduce the session's audio sample for
sample. The engine itself never crossfades — Music Assistant mixes the queue's
crossfade, so every item's audio starts at its first sample and stays aligned
with its analysis.

SECURITY NOTE: the daemon takes the user's personal API key on its command
line; nothing in this module may ever log the process argv.

Shared infrastructure (binary manager, WebSocket client, audio prefs, pulse
capture) is owned by the Spotify Connect provider / core helpers and reused here.
"""

from __future__ import annotations

import array
import asyncio
import fcntl
import os
import shutil
import termios
import time
from collections import deque
from contextlib import suppress
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

from aiohttp import ClientError
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import AudioError, LoginFailed, MusicAssistantError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import (
    CONF_VALUE_DISABLED,
    CONF_VALUE_ENABLED,
    CONF_VOLUME_NORMALIZATION,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.helpers.pulse_capture import (
    CAPTURE_CHANNELS,
    CAPTURE_SAMPLE_RATE,
    PipeSink,
    get_pulse_capture_server,
)
from music_assistant.models.music_provider import MusicProvider, ProviderStreamLimitError
from music_assistant.providers.spotify.constants import (
    CONF_AUDIO_QUALITY,
    CONF_SOLOIST_API_KEY,
    CONF_SOLOIST_CONSENT,
    CONF_SOLOIST_SESSION_DIR,
    SOLOIST_DATA_DIR_NAME,
    SOLOIST_DEVICE_NAME,
)
from music_assistant.providers.spotify.helpers import soloist_session_present
from music_assistant.providers.spotify_connect.base import (
    AUDIO_QUALITY_LOSSLESS,
    spotify_source_audio_format,
)
from music_assistant.providers.spotify_connect.soloist import (
    SoloistBinaryManager,
    SoloistClient,
    SoloistError,
    write_audio_prefs,
)
from music_assistant.providers.spotify_connect.soloist.runtime import (
    EXIT_CODE_BUILD_EXPIRED,
    WS_ADDR_FILE,
    WS_PORT_FILE,
    SoloistAuthState,
    SoloistDeviceChanged,
    SoloistOptionsChanged,
    SoloistPlaybackOptions,
    SoloistPlaybackState,
    SoloistPositionSync,
    SoloistTrackChanged,
    SoloistVolumeChanged,
)

from .base import SpotifyPlaybackBackend

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator

    from music_assistant_models.queue_item import QueueItem
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.helpers.json import SerializableType
    from music_assistant.helpers.pulse_capture import PulseCaptureServer
    from music_assistant.providers.spotify.provider import SpotifyProvider
    from music_assistant.providers.spotify_connect.soloist.runtime import SoloistEvent

# The capture sink delivers fixed s32le/44.1kHz/2ch PCM. Soloist decodes
# internally and never exposes the source codec or bit depth (lossless up to
# 24-bit fits the 32-bit container losslessly), so the capture format doubles
# as the display format.
_FRAME_BYTES: Final[int] = 4 * CAPTURE_CHANNELS
_BYTES_PER_SECOND: Final[int] = CAPTURE_SAMPLE_RATE * _FRAME_BYTES

# Bounded soloist playback cache (docs: 0 = unlimited, otherwise at least 100 MB).
_CACHE_SIZE_MB: Final[int] = 512

# The reader clocks the pipe sink, and thus how fast Spotify must deliver.
# Both values are the result of live listening tests against the measured
# delivery envelope (1.1x sustained is clean even on a cold cache, 1.2x+
# starves mid-track):
# - the sustained 1.1x banks a downstream cushion (~6s per minute) that
#   absorbs the source-less gap at the next track boundary; at exactly 1.0x
#   every boundary gap reaches the player, where timeline-synced outputs
#   drop the late audio instead of delaying it.
# - the burst must stay SMALL: during the burst window the read is unpaced,
#   and demanding audio faster than the warming fetch pipeline can deliver
#   destabilizes it — large bursts audibly worsened track starts.
_PACE_RATE: Final[float] = 1.1
_PACE_BURST_S: Final[float] = 1.0

_READ_CHUNK_SIZE: Final[int] = 32768
# one wait slice on the FIFO read; state (process exit, errors) is checked between slices
_READ_SLICE_S: Final[float] = 1.0
# a suspended sink delivers nothing while soloist rebuffers; give recovery some room
_STALL_TIMEOUT_S: Final[float] = 30.0
# waiting for the daemon's WS endpoint, events and the requested item to appear
_STARTUP_TIMEOUT_S: Final[float] = 30.0
# how often to check whether the events task has the WebSocket up yet
_CONNECT_POLL_S: Final[float] = 0.05
_SEEK_CONFIRM_TIMEOUT_S: Final[float] = 15.0
# a seek is re-sent at this interval until a position anchor confirms it
_SEEK_RETRY_INTERVAL_S: Final[float] = 2.0
# position reported by a seek anchor may fall slightly before the requested target
_SEEK_TOLERANCE_MS: Final[int] = 2000
# Infrastructure silence precedes the session's first decoded sample; trim at
# most this much, once per session. Kept small on purpose: the trim cannot tell
# capture pre-roll from a genuinely digitally-silent intro, so the budget bounds
# what an intro can lose while still covering the measured pre-roll (~140 ms).
_MAX_LEAD_TRIM_S: Final[float] = 0.5
# how far the last observed playback position may fall short of an item's
# duration before its delivered PCM is rejected as incomplete
_INCOMPLETE_TOLERANCE_MS: Final[int] = 10000
# after the last item of a run ends, how long to keep draining its tail
_DRAIN_TIMEOUT_S: Final[float] = 2.0
# How long a session nothing is reading is kept alive, so a follow-up item can
# continue on it instead of paying a cold start. Kept short: the engine plays on
# for this long after playback stops - burning the account's one active stream
# and showing as a playing device in the Spotify app - while the gap it exists to
# cover is the handover between two items, which is milliseconds.
_IDLE_TIMEOUT_S: Final[float] = 5.0
# an item's stream may run past its nominal duration (reported durations are
# approximate), but never unboundedly: without the session reporting a track
# change by then, something is wrong and the item fails
_ITEM_OVERRUN_S: Final[float] = 30.0
# how far ahead of the playing item to look for the one being streamed: a flow
# stream runs ahead of the player, a per-item stream is the playing item or its
# successor
_FOLLOWER_SEARCH_DEPTH: Final[int] = 4
# audio held for an item whose stream has not been opened (or reopened) yet;
# beyond this the session is considered abandoned
_UNCLAIMED_LIMIT_S: Final[float] = 60.0
# How much captured-but-undelivered audio the session may hold. Reading at
# _PACE_RATE deliberately makes the engine run ahead of the player, and that
# surplus has nowhere to go: the engine renders in real time and the capture
# FIFO applies no backpressure of its own. Past this the sink is suspended,
# which stalls the engine, so the surplus stays bounded — and with it both the
# memory held and how far the engine's item can run ahead of the queue's, which
# the URI match in _signal_ready depends on. Resume well below the cap so the
# sink is not flipped on every chunk.
_MAX_RETAINED_S: Final[float] = 20.0
_RESUME_RETAINED_S: Final[float] = 10.0
# how often the tail drain checks whether the item's own audio has all arrived
_DRAIN_POLL_S: Final[float] = 0.1
# how often an unsettled boundary re-asks the queue for the follower to feed
_FEED_RETRY_INTERVAL_S: Final[float] = 2.0
# the engine's "no repeat" value for its playback options
_REPEAT_OFF: Final[str] = "off"
# The engine allows one daemon per data directory and refuses to start otherwise,
# exiting with a plain code 1 - its message is the only way to tell that case
# apart from any other startup failure.
_DATA_DIR_BUSY_MARKER: Final[str] = "another session is running"
# A daemon that cannot log in advertises itself for pairing instead of failing,
# and then sits there until the startup budget runs out. The engine reports no
# other way that a stored session is gone.
_UNPAIRED_MARKER: Final[str] = "waiting for login"
# how long the log reader is given to catch up on a daemon's parting words
_LOG_DRAIN_TIMEOUT_S: Final[float] = 2.0
# How many pauses from the Spotify app are put back before the session gives up.
# Small on purpose: one is an accidental tap, more than that is someone who means
# it and is not going to be argued out of it.
_MAX_APP_PAUSE_RESUMES: Final[int] = 2
# How long the Spotify app keeps Music Assistant from starting another session
# after taking one over. Without it the next queue item spawns a fresh daemon
# that claims the Connect device straight back off whatever the user moved to.
_APP_CONTROL_COOLDOWN_S: Final[float] = 30.0


class SoloistSessionBusyError(ProviderStreamLimitError):
    """
    Raised when the one Soloist session is delivering a different item.

    A ProviderStreamLimitError so a speculative prepare gives up softly and the
    item is not marked unplayable, but with a message of its own: the engine
    allows a single session, which is not the same thing as the provider's
    source-stream budget (a handover legitimately holds two streams against it).
    """

    def __init__(self, provider: MusicProvider) -> None:
        """
        Initialize the error.

        :param provider: The provider whose session is busy.
        """
        # deliberately skips ProviderStreamLimitError.__init__, whose whole job is
        # to phrase the message in terms of that source-stream budget
        MusicAssistantError.__init__(
            self,
            f"{provider.name} is already playing something else",
            translation_key="soloist_session_busy",
            translation_owner="provider.spotify",
            translation_args=[provider.name],
        )
        self.provider_instance = provider.instance_id
        self.limit = 1


class SoloistAppControl(StrEnum):
    """What the Spotify app did to the session Music Assistant was playing."""

    TOOK_OVER = "soloist_app_took_over"
    PAUSED = "soloist_app_paused"


# plain-English form of each, for the log and the error message; the values of
# SoloistAppControl are the translation keys carrying the localized wording
_APP_CONTROL_MESSAGES: Final[dict[SoloistAppControl, str]] = {
    SoloistAppControl.TOOK_OVER: "{0} playback was taken over from the Spotify app",
    SoloistAppControl.PAUSED: "{0} playback was paused from the Spotify app",
}


class SoloistAppControlError(ProviderStreamLimitError):
    """
    Raised while the Spotify app is holding the session it took from Music Assistant.

    A ProviderStreamLimitError because that is exactly how the queue should treat
    it: the item is not marked unplayable, other providers get a chance at it,
    and an explicit play stops the queue with the message below rather than
    failing one item after another. Above all it keeps the next item from
    spawning a daemon that would claim the Connect device straight back.
    """

    def __init__(self, provider: MusicProvider, reason: SoloistAppControl) -> None:
        """
        Initialize the error.

        :param provider: The provider whose session the app took.
        :param reason: What the Spotify app did.
        """
        # deliberately skips ProviderStreamLimitError.__init__, whose whole job is
        # to phrase the message in terms of the provider's source-stream budget
        MusicAssistantError.__init__(
            self,
            _APP_CONTROL_MESSAGES[reason].format(provider.name),
            translation_key=reason.value,
            translation_owner="provider.spotify",
            translation_args=[provider.name],
        )
        self.provider_instance = provider.instance_id
        self.limit = 1


class SoloistBackend(SpotifyPlaybackBackend):
    """
    Fetches Spotify audio from one continuous ``soloist`` session, fed one track ahead.

    Requires a stored paired session in the per-instance data directory
    (provisioned by the setup flow via ``soloist --pair``).
    """

    _server: PulseCaptureServer | None = None
    _binary: Path | None = None
    _session: _SoloistSession | None = None

    def __init__(self, provider: SpotifyProvider) -> None:
        """
        Initialize the backend.

        :param provider: The owning Spotify provider instance.
        """
        super().__init__(provider)
        # Guards every read and write of _session AND every session teardown.
        # The engine allows one daemon per data directory, so a replacement can
        # only be spawned once the previous one is gone — holding this across
        # the teardown is what sequences that.
        self._session_lock = asyncio.Lock()
        # what the Spotify app last did to a session, and until when that holds
        # off a replacement (see note_app_control)
        self._app_control: SoloistAppControl | None = None
        self._app_control_until = 0.0

    def source_audio_format(self, media_type: MediaType) -> AudioFormat:
        """
        Return the format Spotify is asked to stream for this item.

        The engine decodes internally and never reports what it fetched, so this
        is the configured ceiling rather than a measurement — the same thing the
        Spotify apps show. Only music is served losslessly; spoken content is
        Ogg Vorbis whatever the setting says.

        :param media_type: What is being streamed.
        """
        quality = self._audio_quality
        return spotify_source_audio_format(
            quality,
            lossless=media_type == MediaType.TRACK and quality == AUDIO_QUALITY_LOSSLESS,
        )

    @property
    def handoff_audio_format(self) -> AudioFormat:
        """Return the PCM the capture sink actually delivers."""
        return AudioFormat(
            content_type=ContentType.PCM_S32LE,
            codec_type=ContentType.PCM_S32LE,
            sample_rate=CAPTURE_SAMPLE_RATE,
            bit_depth=32,
            channels=CAPTURE_CHANNELS,
        )

    @property
    def max_concurrent_streams(self) -> int:
        """
        Two: a handover holds two item streams against the one Spotify session.

        The account still runs a single Soloist session; the second slot exists
        because the item that is ending and the item that continues from it are
        two Music Assistant streams reading the same session in turn.
        """
        return 2

    @property
    def is_realtime(self) -> bool:
        """Soloist delivers at playback pace (~1.1x ceiling): no read-ahead."""
        return True

    def session_normalizes(self, streamdetails: StreamDetails) -> bool | None:
        """
        Return whether the session serving this item's queue is normalizing.

        None when no session serves that queue, in which case the configuration is
        the only thing to go on.

        :param streamdetails: Stream details of the item being asked about.
        """
        session = self._session_for(streamdetails)
        return session.engine_normalizes if session is not None else None

    async def setup(self) -> None:
        """
        Validate the binary and paired session, and start the capture server.

        :raises LoginFailed: When the API key or paired session is missing, which
            requires the user to re-run the setup flow.
        """
        if not self._api_key:
            raise LoginFailed(
                "Spotify Soloist API key missing",
                translation_key="soloist_pairing_required",
                translation_owner="provider.spotify",
            )
        # setup errors (unsupported platform, missing consent, download failure,
        # expired build) propagate so the provider load fails with a clear error
        manager = SoloistBinaryManager(self.mass)
        self._binary = await manager.ensure_fresh(self._consent)
        await self._adopt_paired_session()
        if not await asyncio.to_thread(self._has_stored_session):
            raise LoginFailed(
                "Spotify Soloist is not paired with a Spotify account",
                translation_key="soloist_pairing_required",
                translation_owner="provider.spotify",
            )
        await asyncio.to_thread(self._cache_dir.mkdir, parents=True, exist_ok=True)
        self._server = await get_pulse_capture_server(self.mass).acquire()

    async def unload(self) -> None:
        """Stop the session and release the capture server."""
        async with self._session_lock:
            if (session := self._session) is not None:
                # dropped only once the teardown finished, so a cancellation
                # part-way leaves a later stop() something to clean up
                await session.stop()
                self._session = None
        if (server := self._server) is not None:
            self._server = None
            await server.release()

    async def stream_spotify_uri(
        self,
        spotify_uri: str,
        seek_position: int = 0,
        *,
        streamdetails: StreamDetails | None = None,
    ) -> AsyncGenerator[bytes]:
        """
        Yield the PCM audio for one Spotify URI out of the continuous session.

        :param spotify_uri: Canonical Spotify URI (``spotify:track:<id>`` or
            ``spotify:episode:<id>``).
        :param seek_position: Position in seconds to start from. Any seek
            restarts the session at that position (a continuous run cannot be
            rewound without disrupting it).
        :param streamdetails: The StreamDetails this audio is requested for.
            They tell the session which queue it serves and which item of it is
            being streamed, so it can feed the engine the following track.
        """
        if self._server is None or self._binary is None:
            raise AudioError("Spotify Soloist backend is not started")
        queue_id = streamdetails.queue_id if streamdetails is not None else None
        session, item = await self._acquire(spotify_uri, seek_position, queue_id)
        try:
            # Feed before the first byte is handed over: the item's own stream
            # must not be able to reach its end before the next one is queued.
            # A follower that is not knowable yet (the queue's index still
            # settling on a fresh start, or the next item still being resolved)
            # is asked for again while the item streams.
            boundary_settled = streamdetails is None or await session.feed_after(
                streamdetails, spotify_uri
            )
            next_feed_attempt = 0.0
            async for chunk in item.read():
                if not boundary_settled and streamdetails is not None:
                    now = time.monotonic()
                    if now >= next_feed_attempt:
                        next_feed_attempt = now + _FEED_RETRY_INTERVAL_S
                        boundary_settled = await session.feed_after(streamdetails, spotify_uri)
                yield chunk
        finally:
            item.release()
        await session.validate_item(item)

    async def discard_session(self, session: _SoloistSession) -> None:
        """
        Stop a session for good, dropping it if it is still the current one.

        The teardown happens under the session lock, not after it: the engine
        refuses to start while another daemon still holds its data directory, so
        a replacement must not be spawned until this one is gone.

        :param session: The session to tear down.
        """
        async with self._session_lock:
            await session.stop()
            if self._session is session:
                self._session = None

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostic details about the backend (never any secret)."""
        session = self._session
        return {
            "soloist": SoloistBinaryManager(self.mass).diagnostics(),
            "paired": await asyncio.to_thread(self._has_stored_session),
            "session_active": session is not None and session.usable,
            "app_control": reason.value if (reason := self._held_by_app()) else None,
        }

    @property
    def _api_key(self) -> str:
        """Return the stored Soloist API key."""
        return str(self.provider.get_setup_value(CONF_SOLOIST_API_KEY) or "")

    @property
    def _audio_quality(self) -> str:
        """
        Return the configured streaming quality ceiling.

        Stated rather than left to the engine's own default, which would
        otherwise decide it silently. Spotify serves the best the account is
        entitled to below the ceiling.
        """
        return str(self.provider.config.get_value(CONF_AUDIO_QUALITY) or AUDIO_QUALITY_LOSSLESS)

    @property
    def _consent(self) -> bool:
        """Return whether the user consented to downloading the binary."""
        return bool(self.provider.get_setup_value(CONF_SOLOIST_CONSENT))

    @property
    def _data_dir(self) -> Path:
        """Return the per-instance soloist data directory (paired session)."""
        return (
            Path(self.mass.storage_path)
            / "spotify"
            / self.provider.instance_id
            / SOLOIST_DATA_DIR_NAME
        )

    @property
    def _cache_dir(self) -> Path:
        """Return the per-instance soloist playback cache directory."""
        return Path(self.mass.cache_path) / self.provider.instance_id / "soloist-cache"

    @property
    def _sink_prefix(self) -> str:
        """Return the capture sink name prefix (PA-safe form of the instance id)."""
        return "".join(
            ch if ch.isalnum() or ch in "_.-" else "_" for ch in self.provider.instance_id
        )

    def _session_for(self, streamdetails: StreamDetails) -> _SoloistSession | None:
        """
        Return the session serving this item's queue, if one can still serve it.

        A session serves one queue: another queue's session says nothing about
        this item, however much it knows about its own.

        :param streamdetails: Stream details of the item being asked about.
        """
        session = self._session
        if session is None or not session.usable or session.queue_id != streamdetails.queue_id:
            return None
        return session

    async def _acquire(
        self, spotify_uri: str, seek_position: int, queue_id: str | None
    ) -> tuple[_SoloistSession, _ItemAudio]:
        """
        Return the session and audio channel to stream this item from.

        Continues the running session when it already plays (or has been fed)
        this item for this queue; anything else — a seek, another queue, a
        skipped-to item, a session that is gone — starts a fresh session.
        """
        async with self._session_lock:
            self._raise_if_app_controlled()
            session = self._session
            if session is not None and session.usable and session.queue_id == queue_id:
                if not seek_position and (item := session.item_for(spotify_uri)) is not None:
                    item.claim()
                    return session, item
                if not seek_position and (pending := session.pending_item(spotify_uri)) is not None:
                    # skipped to the item that was fed next: the engine can jump
                    # there itself, which keeps the session instead of paying a
                    # whole respawn
                    # claimed only once the engine is there: a refused skip
                    # would otherwise leave the channel claimed for good, and
                    # the session busy and unable to expire
                    await session.skip_to(pending)
                    pending.claim()
                    return session, pending
            if session is not None:
                if session.in_use and (
                    session.queue_id != queue_id or not session.is_playing(spotify_uri)
                ):
                    # Restarting the session here would cut short whatever it is
                    # still delivering: another player's item, or an early fetch
                    # across a boundary this session does not drive (a podcast
                    # episode or audiobook chapter, which are never stitched).
                    # Reported as capacity so a speculative prepare gives up
                    # softly and the real request, made once the other item has
                    # been released, gets the session.
                    raise SoloistSessionBusyError(self.provider)
                # re-opening the item that is playing is a seek (or a replay) of
                # that same item, which is exactly a restart of the session
                await session.stop()
                self._session = None
            # cheap thanks to the shared verify cache; swaps in a fresh build when
            # the installed one is nearing its 90-day expiry
            try:
                self._binary = await SoloistBinaryManager(self.mass).ensure_fresh(self._consent)
            except SoloistError as err:
                raise AudioError(f"Spotify Soloist binary unavailable: {err}") from err
            session = _SoloistSession(self, queue_id)
            try:
                item = await session.start(spotify_uri, seek_position)
            except BaseException:
                await session.stop()
                raise
            self._session = session
            item.claim()
            return session, item

    def _note_app_control(self, reason: SoloistAppControl) -> None:
        """
        Record that the Spotify app took control, holding off a replacement session.

        Starting one right away would claim the Connect device back off whatever
        the user just moved playback to, so the items that follow are refused for
        a while instead.

        :param reason: What the Spotify app did.
        """
        self._app_control = reason
        self._app_control_until = time.monotonic() + _APP_CONTROL_COOLDOWN_S

    def _held_by_app(self) -> SoloistAppControl | None:
        """Return what the Spotify app did, for as long as that holds off a new session."""
        if self._app_control is None or time.monotonic() >= self._app_control_until:
            return None
        return self._app_control

    def _raise_if_app_controlled(self) -> None:
        """Refuse a new session while the Spotify app is still holding the one it took."""
        if (reason := self._held_by_app()) is not None:
            raise SoloistAppControlError(self.provider, reason)

    async def _adopt_paired_session(self) -> None:
        """Adopt a session paired by the setup flow into the per-instance data dir."""
        pending = str(self.provider.get_setup_value(CONF_SOLOIST_SESSION_DIR) or "")
        if not pending:
            return
        await asyncio.to_thread(self._copy_paired_session, pending)
        # config writes schedule tasks and must therefore run on the event loop
        self.provider._update_setup_data(CONF_SOLOIST_SESSION_DIR, None)

    def _copy_paired_session(self, pending: str) -> None:
        """
        Copy the paired session files into the canonical data dir (blocking).

        A copy (not a move) so the flow-private source survives a failed
        provider load: the setup flow can then retry its finish step and adopt
        the same pairing again. The source is removed when the flow ends.
        """
        source = Path(self.mass.storage_path) / pending
        canonical = self._data_dir
        if source.is_dir() and source != canonical:
            # stage first: a failed copy must never leave the canonical dir
            # half-written or destroy the currently working session
            staging = canonical.with_name(canonical.name + ".new")
            shutil.rmtree(staging, ignore_errors=True)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, staging)
            staging.chmod(0o700)
            if canonical.exists():
                shutil.rmtree(canonical)
            staging.replace(canonical)

    def _has_stored_session(self) -> bool:
        """Return whether the data dir holds a stored (paired) session (blocking)."""
        return soloist_session_present(self._data_dir)

    def _prepare_data_dir(self, *, normalize: bool) -> None:
        """
        Prepare the data dir for a fresh session spawn (blocking).

        :param normalize: Whether the engine should normalize loudness itself.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir.chmod(0o700)
        # endpoint files from a previous run would point the client at a dead port
        for endpoint_file in (WS_ADDR_FILE, WS_PORT_FILE):
            (self._data_dir / endpoint_file).unlink(missing_ok=True)
        # The engine reads its prefs at startup only, so they are refreshed on
        # every spawn. Whoever normalizes, only one of us may: with the engine
        # doing it the provider declares the audio pre-normalized, which takes
        # MA's own normalization out of the path (see
        # SpotifyProvider.delivers_normalized_audio).
        # crossfade 0: Music Assistant mixes the queue's crossfade itself, so the
        # engine plays every track clean from its first sample and an item's
        # delivered audio lines up with its analysis (waveform, beat grid, light sync)
        if not write_audio_prefs(
            self._data_dir,
            self.logger,
            crossfade_ms=0,
            loudness_normalization=normalize,
            audio_quality=self._audio_quality,
        ):
            # the provider has told the rest of the server who normalizes this
            # audio; running the engine on settings that may say otherwise would
            # mean normalizing twice, or not at all
            raise AudioError("Spotify Soloist audio settings could not be applied")

    def _session_args(self) -> list[str]:
        """
        Build the soloist session's argv.

        SECURITY: the argv carries the user's API key — it must never be logged
        or end up in any error message.
        """
        assert self._binary is not None
        return [
            str(self._binary),
            "--device-name",
            SOLOIST_DEVICE_NAME,
            "--api-key",
            self._api_key,
            "--data-dir",
            str(self._data_dir),
            "--cache-dir",
            str(self._cache_dir),
            # bounded playback cache (0 would be unlimited)
            "--cache-size",
            str(_CACHE_SIZE_MB),
            # unity volume so unaltered PCM reaches the capture sink
            "--initial-volume",
            "100",
            # local WebSocket API on a free loopback port; the daemon publishes
            # the actual endpoint in its data dir where SoloistClient finds it
            "--ws",
            "127.0.0.1:0",
        ]


class _SoloistSession:
    """
    One continuous soloist session, serving the consecutive items of one queue.

    The session reads its capture FIFO once, at ``_PACE_RATE``, and routes the
    audio to the item that the engine reports as current. An item's audio
    channel is created when the item is played or fed, and closed when the
    engine moves on — that boundary is the cut between two Music Assistant
    item streams.
    """

    def __init__(self, backend: SoloistBackend, queue_id: str | None) -> None:
        """
        Initialize a session for one queue (nothing is spawned yet).

        :param backend: The owning backend.
        :param queue_id: The player queue this session serves, when known.
        """
        self.backend = backend
        self.queue_id = queue_id
        self.mass = backend.mass
        self.logger: logging.Logger = backend.logger
        # what the engine was actually told at spawn, which is what the streams
        # core has to agree with - the setting may be toggled while this plays
        self.engine_normalizes = False
        self._items: dict[str, _ItemAudio] = {}
        self._current: _ItemAudio | None = None
        # uris handed to the engine that it has not started playing yet
        self._pending: deque[str] = deque()
        self._client: SoloistClient | None = None
        self._sink: PipeSink | None = None
        self._proc: AsyncProcess | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._log_task: asyncio.Task[None] | None = None
        self._transport: asyncio.ReadTransport | None = None
        self._reader: asyncio.StreamReader | None = None
        self._error: str | None = None
        self._stopped = False
        self._teardown_done = False
        # None until the engine reports its login state for the first time
        self._logged_in: bool | None = None
        # set once the daemon is known to have no stored session to log in with
        self._unpaired = False
        # set once this daemon is the active Connect device; losing that again is
        # the user moving playback elsewhere from their Spotify app
        self._was_active = False
        # what the Spotify app did to end this session, when it did
        self._app_control: SoloistAppControl | None = None
        # pauses from the Spotify app put back for the item being played
        self._app_pauses = 0
        self._data_dir_busy = False
        self._pin_in_flight = False
        self._options_pin_in_flight = False
        self._demand_started = False
        self._engine_playing = False
        self._sink_running = False
        self._backpressured = False
        self._sink_lock = asyncio.Lock()
        self._idle_since: float | None = None
        # while set, captured audio is dropped until the engine reports this item
        self._discard_until: str | None = None
        # bytes of the item jumped away from still to drop, measured at the jump
        self._stale_budget = 0

    @property
    def in_use(self) -> bool:
        """Return whether an item stream is reading this session right now."""
        return any(item.claimed for item in self._items.values())

    def pending_item(self, spotify_uri: str) -> _ItemAudio | None:
        """
        Return the channel of an item that was fed but has not started yet.

        The engine can be told to jump to it, which keeps the session instead of
        paying a fresh spawn.

        :param spotify_uri: The canonical Spotify URI to check.
        """
        item = self._items.get(spotify_uri)
        if item is None or item.spent or item.started.is_set():
            return None
        return item if spotify_uri in self._pending else None

    async def skip_to(self, item: _ItemAudio) -> None:
        """
        Tell the engine to move on to an item it was already fed.

        :param item: The channel of the item to jump to.
        :raises AudioError: When the engine does not get there in time.
        """
        client = self._client
        if client is None:
            raise AudioError("Spotify Soloist is not connected")
        # armed before the command: everything already rendered belongs to the
        # item being left behind
        self._discard_until = item.uri
        try:
            try:
                await client.skip_next()
            except (TimeoutError, OSError, ClientError, SoloistError) as err:
                raise AudioError(
                    f"Spotify Soloist would not skip to {item.uri}: {type(err).__name__} {err}"
                ) from err
            async with asyncio.timeout(_STARTUP_TIMEOUT_S):
                await item.started.wait()
        except TimeoutError:
            raise AudioError(f"Spotify Soloist did not reach {item.uri}") from None
        finally:
            self._discard_until = None

    def is_playing(self, spotify_uri: str) -> bool:
        """
        Return whether this is the item the engine is currently playing.

        :param spotify_uri: The canonical Spotify URI to check.
        """
        return self._current is not None and self._current.uri == spotify_uri

    @property
    def current(self) -> _ItemAudio | None:
        """Return the item the engine is currently playing, if any."""
        return self._current

    @property
    def has_pending(self) -> bool:
        """Return whether the engine was fed an item it has not started yet."""
        return bool(self._pending)

    @property
    def usable(self) -> bool:
        """Return whether this session can still serve items."""
        return not self._stopped and self._error is None

    def item_for(self, spotify_uri: str) -> _ItemAudio | None:
        """
        Return the audio channel for an item this session plays or was fed, if any.

        Two conditions. A channel is served at most once: its audio is handed
        over as it is consumed, so a stream that already read it — to the end or
        part-way — cannot be replayed. And the engine has to have reached the
        item: a fed item the engine is not playing yet means Music Assistant
        moved somewhere the session has not (a skip), and continuing there would
        hand over a channel that only fills when the current track ends.
        """
        item = self._items.get(spotify_uri)
        if item is None or item.spent or not item.started.is_set():
            return None
        return item

    async def start(self, spotify_uri: str, seek_position: int) -> _ItemAudio:
        """
        Spawn the session and start playing the given item.

        :param spotify_uri: Canonical Spotify URI to start on.
        :param seek_position: Position in seconds to start from.
        :return: The audio channel for the requested item.
        """
        backend = self.backend
        server = backend._server
        assert server is not None
        assert backend._binary is not None
        self.engine_normalizes = self._engine_normalization_enabled()
        await asyncio.to_thread(
            partial(backend._prepare_data_dir, normalize=self.engine_normalizes)
        )
        self._sink = sink = await PipeSink.create(server, backend._sink_prefix)
        # unity gain so the FIFO carries the engine's PCM unaltered; the sink
        # stays suspended until the (seeked) item is ready so no infrastructure
        # silence accumulates
        await sink.set_volume(100)
        await sink.suspend()
        self._proc = proc = AsyncProcess(
            backend._session_args(),
            # the daemon writes all of its logging to stdout and only ever puts
            # argument-parsing complaints on stderr, so the two are merged into
            # one captured stream. Capturing is what makes the redaction below
            # reachable at all: an unset stdout is inherited, which would leak
            # the daemon's output straight to the server console instead.
            stdout=True,
            stderr=asyncio.subprocess.STDOUT,
            # the explicit process name keeps AsyncProcess logging free of the
            # argv (which carries the API key)
            name=f"soloist[{backend.provider.name}]",
            env=server.child_env(sink.sink_name),
        )
        await proc.start()
        # kept out of _tasks so stop() does not cancel it before the daemon has
        # exited, but a reader that dies still has to fail the session: nothing
        # else drains stdout and the daemon would block on a full pipe
        self._log_task = asyncio.create_task(self._log_output(proc))
        self._log_task.add_done_callback(self._task_done)
        self._client = client = SoloistClient(self.mass, backend._data_dir, self.logger)
        client_ready = asyncio.Event()
        self._spawn_task(self._run_events(client, client_ready))
        item = await self._play(spotify_uri, seek_position, client_ready)
        # the reader must be attached before the sink starts producing, or the
        # sink's first writes go to a reader-less FIFO and are dropped
        self._reader, self._transport = await _open_fifo_reader(sink.fifo_path)
        self._spawn_task(self._read_capture())
        self._demand_started = True
        await self._apply_sink_state(engine_playing=item.status == "playing")
        return item

    async def feed_after(self, streamdetails: StreamDetails, spotify_uri: str) -> bool:
        """
        Hand the engine the item that follows the one being streamed, if any.

        Only consecutive tracks are stitched: the engine's queue command takes
        track URIs, and a podcast episode or audiobook chapter would not gain
        anything from being fed ahead.

        :param streamdetails: The StreamDetails of the item being streamed, used
            to locate it in the queue.
        :param spotify_uri: The URI being streamed (only tracks are fed ahead).
        :return: Whether the boundary is settled; False means the follower was
            not knowable yet and asking again later may still feed it.
        """
        return await self._feed_follower(streamdetails, spotify_uri)

    async def validate_item(self, item: _ItemAudio) -> None:
        """
        Validate that an item's delivered audio actually covered it.

        A starved session pads with silence rather than failing, so completeness
        is judged by the furthest playback position the engine reported while
        the item was current.

        :param item: The item whose stream just finished.
        :raises AudioError: When the item was cut short.
        """
        if self._error:
            raise self._session_error()
        if not item.playing_seen:
            raise AudioError(f"Spotify Soloist never started playing {item.uri}")
        if item.duration_ms is None:
            return
        tolerance_ms = min(_INCOMPLETE_TOLERANCE_MS, item.duration_ms // 2)
        if item.last_position_ms is None or item.last_position_ms + tolerance_ms < item.duration_ms:
            raise AudioError(
                f"Spotify Soloist delivered incomplete audio for {item.uri} "
                f"(reached {item.last_position_ms or 0}ms of {item.duration_ms}ms)"
            )

    async def stop(self) -> None:
        """
        Tear the session down: stop the daemon, the reader and the capture sink.

        Safe to call again after a cancelled teardown: every step is idempotent
        and the session is only marked torn down once they have all run, so a
        cancellation part-way cannot leave the daemon or the sink behind.
        """
        if self._teardown_done:
            return
        self._stopped = True
        for item in self._items.values():
            item.close()
        if self._client is not None and self._proc is not None:
            # commands travel over the events connection, so this has to happen
            # before that task is cancelled; stopping playback lets the engine
            # wind down by itself instead of on the SIGINT that close() sends
            with suppress(Exception):
                await self._client.pause()
        await _cancel_and_join(self._tasks)
        self._tasks.clear()
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if (proc := self._proc) is not None:
            # Closed straight away, with no grace period for a natural exit: a
            # session daemon serves items until it is told to stop, so pausing it
            # never makes it quit and any wait here is pure latency on every
            # seek and track change. The log reader stays
            # alive across the close: nothing else drains the daemon's stdout,
            # and a full pipe would keep it from exiting. A forced close must
            # never be judged by its exit code.
            with suppress(Exception):
                await proc.close()
            if proc.returncode is None:
                # close() has exhausted its kill attempts, so nothing here can do
                # better; the daemon keeps the data directory and the next spawn
                # reports it as busy
                self.logger.warning("The Spotify Soloist daemon could not be stopped")
            # dropped only now: a cancellation during the awaits above must leave
            # the retry something to close, or the daemon keeps the data
            # directory and every later session is refused
            self._proc = None
        if self._log_task is not None:
            await _cancel_and_join([self._log_task])
            self._log_task = None
        if (sink := self._sink) is not None:
            with suppress(Exception):
                await sink.unload()
            self._sink = None
        self._teardown_done = True

    def _spawn_task(self, coro: object) -> None:
        """
        Track a session-scoped task so stop() can cancel and join it.

        A task that dies unexpectedly fails the whole session: its work (reading
        the capture, following the engine) is what the item streams depend on,
        and stop() suppresses exceptions when it joins.
        """
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        task.add_done_callback(self._task_done)
        self._tasks.append(task)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        """Fail the session when one of its tasks died of an unexpected error."""
        if task.cancelled() or (err := task.exception()) is None:
            return
        self.logger.error("Spotify Soloist session task failed: %s", err, exc_info=err)
        self._fail(f"session task failed: {err}")

    def _fail(self, message: str) -> None:
        """
        Record a fatal session error, unblock every waiting item and tear the session down.

        The teardown runs as its own task: it cancels the very tasks this is
        called from, and the daemon has to go either way — an unusable session
        would otherwise keep playing to nobody.
        """
        if self._error is not None:
            return
        self._error = message
        for item in self._items.values():
            # started too, so an item still waiting to be reported as current
            # fails right away instead of sitting out its startup timeout
            item.started.set()
            item.close()
        self.mass.create_task(self.backend.discard_session, self)

    def _engine_normalization_enabled(self) -> bool:
        """
        Return whether the engine should normalize the loudness it delivers.

        The player's own volume normalization switch decides first: turning it
        off means nobody normalizes, not that the job passes to Spotify.
        """
        if not self.backend.provider.spotify_normalization_configured:
            return False
        if self.queue_id is None:
            # nothing to read the switch from, so the provider option stands
            return True
        return (
            self.mass.config.get_effective_player_queue_config_value(
                self.queue_id, CONF_VOLUME_NORMALIZATION, CONF_VALUE_ENABLED
            )
            != CONF_VALUE_DISABLED
        )

    async def _feed_follower(self, streamdetails: StreamDetails, spotify_uri: str) -> bool:
        """
        Feed the engine the track after this one, and report whether it will play on into it.

        :param streamdetails: The StreamDetails of the item being streamed.
        :param spotify_uri: The URI being streamed (only tracks are fed ahead).
        """
        client = self._client
        if client is None or not spotify_uri.startswith("spotify:track:"):
            return False
        next_uri = self._feedable_follower_uri(streamdetails)
        if next_uri is None:
            return False
        existing = self._items.get(next_uri)
        if existing is not None and (
            existing.claimed or existing is self._current or not existing.spent
        ):
            # nothing left to send, so whether the engine plays on rests entirely on
            # the channel this session already holds for it
            return self._engine_plays_on_into(next_uri, spotify_uri)
        if existing is not None:
            # a channel left from an earlier play of the same track: its audio was
            # handed over already, so this occurrence needs its own feed and channel
            del self._items[next_uri]
            with suppress(ValueError):
                self._pending.remove(next_uri)
        # Registered before the command goes out: the engine can reach the item
        # while it is still in flight, and the events task has to find its
        # channel rather than mistake it for something nobody asked for.
        item = self._items[next_uri] = _ItemAudio(next_uri, self)
        self._pending.append(next_uri)
        try:
            await client.add_to_queue(next_uri)
        except (TimeoutError, OSError, ClientError, SoloistError) as err:
            # a failed feed only costs the crossfade at that boundary: the next
            # item still plays, on a fresh session
            self.logger.debug("Unable to feed %s to the soloist session: %s", next_uri, err)
            if self._current is item:
                # the engine acted on the command before the failure got back to
                # us, so it does play on into this item after all
                return True
            if isinstance(err, TimeoutError):
                # the command may have landed engine-side regardless; keeping the
                # channel makes the retry settle on it instead of queueing the
                # same track a second time
                return False
            del self._items[next_uri]
            with suppress(ValueError):
                self._pending.remove(next_uri)
            return False
        self.logger.debug("Fed %s to the soloist session", next_uri)
        return True

    def _engine_plays_on_into(self, next_uri: str, streamed_uri: str) -> bool:
        """
        Return whether the engine plays on into a follower this session already holds.

        Only a channel that can still be served across the boundary is played on into:
        a drained one cannot be replayed, and neither can the item being streamed
        itself, so both start a fresh session instead.

        :param next_uri: URI of the follower.
        :param streamed_uri: URI of the item being streamed.
        """
        if next_uri == streamed_uri:
            return False
        return self.pending_item(next_uri) is not None or self.item_for(next_uri) is not None

    def _feedable_follower_uri(self, streamdetails: StreamDetails) -> str | None:
        """
        Return the URI of the track after this one, when the engine can be fed it.

        :param streamdetails: The StreamDetails of the item being streamed.
        """
        follower = self._follower(streamdetails)
        if follower is None:
            return None
        next_uri = self._track_uri(follower)
        if next_uri is None:
            return None
        if (
            follower_details := follower.streamdetails
        ) is not None and follower_details.provider != self.backend.provider.instance_id:
            # the queue has already resolved this item to another provider, so
            # feeding it here would have the engine play audio nobody reads -
            # and cut the current item short when it starts
            return None
        return next_uri

    def _follower(self, streamdetails: StreamDetails) -> QueueItem | None:
        """Return the queue item that follows the one these StreamDetails belong to."""
        queue_id = self.queue_id
        queue = self.mass.player_queues.get(queue_id) if queue_id else None
        if queue is None or queue_id is None or queue.current_index is None:
            return None
        controller = self.mass.player_queues
        # the item being streamed is the one playing or one of the few ahead of
        # it (a flow stream runs ahead of the player); identify it by the
        # StreamDetails object itself, so a repeated track cannot be mistaken
        for offset in range(_FOLLOWER_SEARCH_DEPTH):
            item = controller.get_item(queue_id, queue.current_index + offset)
            if item is None:
                break
            if item.streamdetails is streamdetails:
                return controller.get_next_item(queue_id, item.queue_item_id)
        # commonly a queue index that has not settled yet (fresh play start);
        # the caller keeps asking while the item streams
        self.logger.debug(
            "No follower for %s: item not within %s of queue index %s",
            streamdetails.uri,
            _FOLLOWER_SEARCH_DEPTH,
            queue.current_index,
        )
        return None

    def _track_uri(self, queue_item: QueueItem) -> str | None:
        """Return the Spotify track URI of a queue item on this provider instance."""
        media_item = queue_item.media_item
        if media_item is None or media_item.media_type != MediaType.TRACK:
            return None
        instance_id = self.backend.provider.instance_id
        if media_item.provider == instance_id:
            return f"spotify:track:{media_item.item_id}"
        for mapping in media_item.provider_mappings:
            if mapping.provider_instance == instance_id:
                return f"spotify:track:{mapping.item_id}"
        return None

    async def _play(
        self, spotify_uri: str, seek_position: int, client_ready: asyncio.Event
    ) -> _ItemAudio:
        """Activate the engine, start the requested item and wait until it is current."""
        client = self._client
        assert client is not None
        item = self._items[spotify_uri] = _ItemAudio(spotify_uri, self)
        self._current = item
        # Commands travel over the events connection, and the engine takes them
        # in three stages: it publishes its endpoint, then accepts a connection,
        # then restores its session and logs in. A command sent before that last
        # step is dropped and its acknowledgement never arrives, so wait for the
        # login the engine announces rather than for the socket alone.
        # A failure reported while the endpoint is still awaited - the engine
        # having no session to log in with - is watched for throughout: waiting
        # the endpoint out would outlast the queue's own patience for the audio,
        # and the item would fail with a timeout instead of its real cause.
        try:
            async with asyncio.timeout(_STARTUP_TIMEOUT_S):
                while not self._error and not (
                    client_ready.is_set() and client.connected and self._logged_in
                ):
                    await asyncio.sleep(_CONNECT_POLL_S)
        except TimeoutError:
            self._raise_startup_error("did not connect and log in", spotify_uri)
        if self._error or not client.connected:
            self._raise_startup_error("published no usable WebSocket endpoint", spotify_uri)
        try:
            # a fresh daemon is not the active Connect device yet, and play() on
            # an inactive device would start playback on whatever else is active
            await client.activate(await_result=True)
            # Latched here rather than waiting for the engine to volunteer it:
            # events and command acks share one connection, so everything the
            # daemon reported while it was still inactive arrived before this.
            self._was_active = True
            # Music Assistant owns the queue: order and repeats are decided here.
            # A session that inherited repeat from the account would replay this
            # item instead of moving on to the one fed behind it.
            await client.set_shuffle(False)
            await client.set_repeat_context(False)
            await client.set_repeat_track(False)
            if self._error:
                # the device was taken while those went out; play() would claim
                # it straight back off wherever the user moved to
                raise self._session_error()
            await client.play(spotify_uri)
        except (TimeoutError, OSError, ClientError, SoloistError) as err:
            # a bare TimeoutError stringifies to nothing, so name the type too
            raise AudioError(
                f"Spotify Soloist would not start {spotify_uri}: {type(err).__name__} {err}"
            ) from err
        await self._await_item_ready(item)
        if seek_position:
            await self._cold_seek(client, item, seek_position * 1000)
        return item

    async def _await_item_ready(self, item: _ItemAudio) -> None:
        """Wait until the engine reports the requested item as its current one."""
        proc = self._proc
        assert proc is not None
        try:
            async with asyncio.timeout(_STARTUP_TIMEOUT_S):
                exit_task = asyncio.ensure_future(proc.wait())
                item_task = asyncio.ensure_future(item.started.wait())
                try:
                    await asyncio.wait({exit_task, item_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    exit_task.cancel()
                    item_task.cancel()
        except TimeoutError:
            self._raise_startup_error("timed out waiting for playback to start", item.uri)
        if self._error or not item.started.is_set():
            if proc.returncode is not None:
                # let the log reader catch up, so the daemon's own complaint can
                # be reported instead of a generic startup failure
                await self._drain_log()
            if proc.returncode == EXIT_CODE_BUILD_EXPIRED:
                # an expired build exits with code 10 right at spawn
                await self._handle_expired_build()
            self._raise_startup_error("exited before playback started", item.uri)

    async def _drain_log(self) -> None:
        """Give the daemon's log reader a moment to catch up on its last lines."""
        if (log_task := self._log_task) is None:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(log_task), _LOG_DRAIN_TIMEOUT_S)

    async def _handle_expired_build(self) -> NoReturn:
        """Replace the expired soloist build and fail the item with an accurate message."""
        try:
            # bypass the verify cache — it would hand back the same expired binary
            self.backend._binary = await SoloistBinaryManager(self.mass).ensure_fresh(
                self.backend._consent, force=True
            )
        except SoloistError as err:
            raise AudioError(
                "Spotify Soloist build expired and no replacement could be installed"
            ) from err
        raise AudioError("Spotify Soloist build expired; a replacement was installed, retry")

    def _raise_startup_error(self, detail: str, spotify_uri: str) -> NoReturn:
        """Raise the most specific startup failure for the requested item."""
        # a pairing that never logged in is checked first: it also fails the
        # session, and its recovery (back through the setup flow) beats failing
        # every track with a generic error
        if self._unpaired or self._logged_in is False:
            # the stored session no longer logs in: route the user through the
            # setup flow instead of failing every item (mirrors librespot's
            # INVALID_CREDENTIALS handling)
            error = LoginFailed(
                "Spotify Soloist pairing lost",
                translation_key="soloist_pairing_required",
                translation_owner="provider.spotify",
            )
            provider = self.backend.provider
            if provider.available:
                provider.unload_with_error(error)
            raise error
        if self._data_dir_busy:
            # a daemon from an earlier Music Assistant process is still holding
            # this provider's data directory; nothing here can reach it
            raise AudioError(
                "Another Spotify Soloist session is still running for this provider "
                "and has to be stopped first (restarting Music Assistant clears it)"
            )
        if self._error:
            raise self._session_error()
        raise AudioError(f"Spotify Soloist {detail} for {spotify_uri}")

    async def _cold_seek(self, client: SoloistClient, item: _ItemAudio, target_ms: int) -> None:
        """
        Seek the engine to the target position before any PCM is released.

        The sink is still suspended, so no pre-seek audio enters the FIFO; PCM
        demand only starts once a position report confirms the seek landed.
        """
        item.arm_seek(target_ms)
        # the engine silently drops a seek that arrives while the track is still
        # loading (verified via event trace), so re-send it until a position
        # anchor confirms it landed
        deadline = asyncio.get_running_loop().time() + _SEEK_CONFIRM_TIMEOUT_S
        while True:
            await client.seek(target_ms)
            with suppress(TimeoutError):
                async with asyncio.timeout(_SEEK_RETRY_INTERVAL_S):
                    await item.seek_confirmed.wait()
            if item.seek_confirmed.is_set():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise AudioError(f"Spotify Soloist did not confirm seeking to {target_ms}ms")

    async def _log_output(self, proc: AsyncProcess) -> None:
        """Log the daemon's output with the API key redacted."""
        api_key = self.backend._api_key
        async for line in proc.iter_stdout():
            # the third-party binary's own output may echo argv (which carries
            # the api key), so redact it before logging
            text = line.replace(api_key, "<redacted>") if api_key else line
            if _DATA_DIR_BUSY_MARKER in text:
                self._data_dir_busy = True
            if _UNPAIRED_MARKER in text and not self._unpaired:
                await self._check_pairing_lost()
            self.logger.debug("[soloist] %s", text)

    async def _read_capture(self) -> None:
        """
        Read the capture FIFO once for the whole session and route it to the current item.

        The pace is the session's clock: the pipe sink applies no rate limit of
        its own, so how fast this reads is how fast the engine plays. Reading
        slightly above realtime is what banks the cushion that carries an item
        boundary; reading unpaced makes PulseAudio render silence instead of
        applying backpressure, and the engine runs off the end of its content.
        """
        reader = self._reader
        proc = self._proc
        assert reader is not None
        assert proc is not None
        loop = asyncio.get_running_loop()
        shaper = _CaptureShaper()
        pace_anchor: float | None = None
        paced_bytes = 0
        stalled_for = 0.0
        while not self._stopped:
            self._expire_idle()
            await self._apply_sink_state()
            if proc.returncode is not None:
                self._fail(f"the session exited with code {proc.returncode}")
                return
            try:
                chunk = await asyncio.wait_for(reader.read(_READ_CHUNK_SIZE), _READ_SLICE_S)
            except TimeoutError:
                # No data. Either the sink is suspended on purpose (the engine
                # is rebuffering or paused, or the cushion is at its cap) or the
                # session has died; only the latter is a stall.
                if self._sink_running:
                    stalled_for += _READ_SLICE_S
                    if stalled_for >= _STALL_TIMEOUT_S:
                        self._fail("audio stalled")
                        return
                # Restart the pacing clock rather than carry the gap: making up
                # lost time would mean an unpaced burst, which over-demands the
                # engine's fetch pipeline exactly when it is recovering.
                pace_anchor = None
                continue
            stalled_for = 0.0
            if not chunk:
                # writer end closed: the capture sink is gone (pulse restart)
                self._fail("the capture sink was lost mid-stream")
                return
            if not (chunk := shaper.shape(chunk)):
                continue
            if pace_anchor is None:
                pace_anchor = loop.time()
                paced_bytes = 0
            paced_bytes += len(chunk)
            self._write_if_wanted(chunk)
            del chunk
            resume_at = pace_anchor + paced_bytes / (_BYTES_PER_SECOND * _PACE_RATE) - _PACE_BURST_S
            if (delay := resume_at - loop.time()) > 0:
                await asyncio.sleep(delay)

    def _write_if_wanted(self, chunk: bytes) -> None:
        """
        Route captured audio to the current item, unless it is stale.

        :param chunk: Whole sample frames just read from the capture FIFO.
        """
        if self._discard_until is not None:
            # the marker drops everything, an earlier jump's remainder included
            self._stale_budget = max(0, self._stale_budget - len(chunk))
            return
        if self._stale_budget > 0:
            # both are whole frames - the budget floored, the chunk shaped - so
            # what is kept stays on the session's frame grid
            drop = min(self._stale_budget, len(chunk))
            self._stale_budget -= drop
            if not (chunk := chunk[drop:]):
                return
        if (item := self._current) is not None:
            item.write(chunk)

    def _expire_idle(self) -> None:
        """
        Fail a session no item stream reads from, so its daemon does not linger.

        A Spotify run ends without telling the provider: the queue simply stops
        asking for items. The grace period is what lets a follow-up item
        continue on the same session instead of paying a cold start.
        """
        if self.in_use:
            self._idle_since = None
            return
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= _IDLE_TIMEOUT_S:
            self.logger.debug("Ending the idle soloist session")
            self._fail("the session went idle")

    async def _run_events(self, client: SoloistClient, client_ready: asyncio.Event) -> None:
        """Keep the WebSocket client connected and feed its events into the session state."""
        proc = self._proc
        assert proc is not None
        if not await client.wait_until_ready(_STARTUP_TIMEOUT_S):
            self._fail("the session did not publish its WebSocket endpoint")
            client_ready.set()
            return
        client_ready.set()
        while True:
            try:
                await client.listen_events(self._handle_event)
            except asyncio.CancelledError:
                raise
            except (TimeoutError, OSError, ClientError, SoloistError) as err:
                # ordinary connection drop; reconnect while the daemon is alive
                # so the item boundaries do not go unnoticed
                self.logger.debug("soloist events connection dropped: %s", err)
            except Exception as err:
                # a defect in event handling must surface loudly and fail the
                # session: continuing would deliver audio against stale state
                self.logger.exception("Unexpected error while handling soloist events")
                self._fail(f"event handling failed: {err}")
                return
            if proc.returncode is not None:
                return
            await asyncio.sleep(1)

    async def _handle_event(self, event: SoloistEvent) -> None:
        """Track what the engine is playing and gate the capture sink on its state."""
        data = event.data
        if isinstance(data, SoloistAuthState):
            self._observe_auth_state(logged_in=data.logged_in)
            self._observe_active_device(is_active=data.is_active)
            return
        if isinstance(data, SoloistDeviceChanged):
            self._observe_active_device(is_active=data.is_active)
            return
        if isinstance(data, SoloistTrackChanged):
            if data.item is not None and data.item.uri:
                await self._observe_current(data.item.uri, _decorated_duration_ms(data.item))
            return
        if isinstance(data, SoloistPositionSync):
            if (item := self._current) is not None:
                item.observe_position(data.position.position_ms)
            return
        if isinstance(data, SoloistVolumeChanged):
            await self._repin_volume(data.volume)
            return
        if isinstance(data, SoloistOptionsChanged):
            await self._repin_options(data.options)
            return
        if isinstance(data, SoloistPlaybackState):
            await self._handle_playback_state(data)

    async def _handle_playback_state(self, data: SoloistPlaybackState) -> None:
        """Apply a playback_state snapshot: current item, position, volume and sink gating."""
        # data.is_active is deliberately left alone: it is optional and rides on
        # deltas too, so the dedicated device_changed/auth_state reports are the
        # only ones worth following.
        if data.item is not None and data.item.uri:
            await self._observe_current(data.item.uri, _decorated_duration_ms(data.item))
            if not self.usable:
                # the snapshot ended the session; nothing below it should still
                # be pinning volume or options on what the app is now driving
                return
        item = self._current
        if item is not None:
            item.status = data.status
            if data.status == "playing":
                item.playing_seen = True
            if data.position is not None:
                item.observe_position(data.position.position_ms)
        if data.volume is not None:
            await self._repin_volume(data.volume)
        if data.options is not None:
            await self._repin_options(data.options)
        if not self._demand_started or item is None:
            return
        playing = data.status == "playing"
        # recorded before the branches below: a drain returns early, and the sink
        # still has to learn that the engine is no longer producing
        was_playing = self._engine_playing
        # both read before _apply_sink_state below rewrites them
        was_backpressured = self._backpressured
        self._engine_playing = playing
        if playing:
            # the engine picked the item back up: it was only rebuffering after all
            self._cancel_tail_drain(item)
        elif item.finishing and item.at_own_end:
            # The run's last item, played out: the engine reports no track change
            # to cut it on, so end it here. This is also the branch a finished run
            # actually arrives on — its snapshot is stopped/idle, not paused.
            self._drain_last_item(item)
            return
        await self._apply_sink_state(engine_playing=playing)
        if data.status == "paused" and not item.draining and not was_backpressured:
            await self._undo_app_pause(was_playing=was_playing)

    async def _undo_app_pause(self, *, was_playing: bool) -> None:
        """
        Put back a pause that came from the Spotify app, up to a point.

        This session has no user-facing pause, so an accidental tap is undone.
        Someone who keeps pausing means it, and fighting on until the item
        starves serves nobody: the session ends instead.

        :param was_playing: Whether the engine was playing before this snapshot,
            so a repeated report of the same pause is not counted as a new one.
        """
        if not self.usable or not self._was_active:
            # A session on its way out pauses the daemon itself, and a bare
            # resume no longer reaches the Spotify apps once this one has lost
            # the Connect device: it would start local playback beside whatever
            # took the session over, on an account allowing one stream.
            return
        if was_playing:
            self._app_pauses += 1
        if self._app_pauses > _MAX_APP_PAUSE_RESUMES:
            self._end_on_app_control(SoloistAppControl.PAUSED)
            return
        if (client := self._client) is not None:
            with suppress(Exception):
                await client.resume()

    async def _apply_sink_state(self, *, engine_playing: bool | None = None) -> None:
        """
        Run the capture sink only while its audio has somewhere to go.

        Three things gate it: the engine actually playing (a suspended sink keeps
        rebuffering and pause silence out of the delivered PCM), a tail drain in
        progress (which needs the sink running to collect what is still in
        flight), and how much captured audio is still undelivered — see
        ``_MAX_RETAINED_S``.

        :param engine_playing: The engine's new playing state, when this call
            is reacting to one.
        """
        async with self._sink_lock:
            if not self._demand_started or (sink := self._sink) is None:
                return
            if engine_playing is not None:
                self._engine_playing = engine_playing
            backpressured = False
            if any(item.draining for item in self._items.values()):
                want = True
            elif not self._engine_playing:
                want = False
            else:
                limit = _MAX_RETAINED_S if self._sink_running else _RESUME_RETAINED_S
                want = self._retained_bytes() < limit * _BYTES_PER_SECOND
                backpressured = not want
            self._backpressured = backpressured
            if want == self._sink_running:
                return
            try:
                if want:
                    await sink.resume()
                else:
                    await sink.suspend()
            except Exception as err:
                # fail closed: a sink with unknown suspend state would leak stall
                # silence into (or withhold audio from) the delivered PCM
                self._fail(f"capture sink control failed: {err}")
                return
            self._sink_running = want

    def _stale_bytes(self) -> int:
        """
        Whole frames of rendered audio sitting between the capture sink and the reader.

        Measured rather than assumed: the reader's share alone ranges over
        several hundred milliseconds as its flow control fills and drains, so no
        fixed amount describes it.
        """
        stale = 0
        if self._transport is not None:
            pipe = self._transport.get_extra_info("pipe")
            if pipe is not None:
                try:
                    pending = array.array("i", [0])
                    fcntl.ioctl(pipe.fileno(), termios.FIONREAD, pending, True)
                    stale += pending[0]
                except OSError as err:
                    self.logger.debug("Could not size the capture FIFO: %s", err)
        if (reader := self._reader) is not None:
            # asyncio exposes no public view of what a StreamReader still holds.
            # It appends before it pauses at twice its limit, so it tops out a
            # further pipe read above that - the bound to stand in with if the
            # attribute ever goes, since dropping extra beats leaving the
            # previous item audible.
            held = getattr(reader, "_buffer", None)
            stale += len(held) if held is not None else 6 * _READ_CHUNK_SIZE
        return stale - (stale % _FRAME_BYTES)

    def _retained_bytes(self) -> int:
        """Return how much captured audio is buffered but not delivered yet."""
        return sum(item.buffered for item in self._items.values())

    def _drain_last_item(self, item: _ItemAudio) -> None:
        """
        Close the run's last item once its own audio has arrived.

        :param item: The item the engine stopped on.
        """
        if item.draining:
            return
        item.start_tail_drain()

        async def _drain() -> None:
            loop = asyncio.get_running_loop()
            # the tail is still travelling through the sink and the FIFO; wait
            # for it, bounded so a session that stopped short cannot hang
            deadline = loop.time() + _STALL_TIMEOUT_S
            while not item.tail_complete and loop.time() < deadline:
                await asyncio.sleep(_DRAIN_POLL_S)
            item.close()
            await self._apply_sink_state()

        item.drain_task = asyncio.create_task(_drain())
        self._tasks.append(item.drain_task)

    def _cancel_tail_drain(self, item: _ItemAudio) -> None:
        """Undo an armed tail drain because the engine resumed the item."""
        if not item.draining:
            return
        if (task := item.drain_task) is not None:
            item.drain_task = None
            task.cancel()
        item.cancel_tail_drain()

    async def _observe_current(self, uri: str, duration_ms: int | None) -> None:
        """
        Follow the engine to the item it reports as current, cutting the previous one.

        The cut lands wherever the engine says it moved on: an item's stream
        carries whatever was read up to that point and the next item's stream
        continues from there, so the two together still reproduce the session's
        audio exactly.
        """
        current = self._current
        if current is not None and current.uri == uri:
            if duration_ms:
                current.duration_ms = duration_ms
            current.started.set()
            return
        item = self._items.get(uri)
        if (item is None or item.spent) and current is not None and current.mid_play:
            # The engine left an item Music Assistant is part-way through for
            # somewhere it was never sent: the user is driving from the Spotify
            # app. Every channel this session opened deliberately — the item
            # asked for and the one fed behind it — is unspent until its stream
            # takes it, so the session's own moves never land here.
            self._end_on_app_control(SoloistAppControl.TOOK_OVER)
            return
        if item is None:
            # Something nobody asked for: the state the engine restores when it
            # starts, or its own autoplay. It gets a channel so the reader has
            # somewhere to put the audio, but it is never offered as an item's
            # audio.
            item = self._items[uri] = _ItemAudio(uri, self)
            item.spent = True
        if duration_ms:
            item.duration_ms = duration_ms
        with suppress(ValueError):
            self._pending.remove(uri)
        self._current = item
        self._app_pauses = 0
        if self._discard_until == uri:
            self._discard_until = None
            # The engine confirms a jump over the WebSocket within a few
            # milliseconds, long before the audio it describes reaches the
            # reader, so the marker above drops next to nothing on its own. The
            # engine does flush its own output, but what the sink already mixed
            # is still on its way here and belongs to the item being left
            # behind - drop that, so it cannot open this one.
            self._stale_budget = self._stale_bytes()
        item.started.set()
        if current is not None and current.started.is_set():
            # Only an item that was actually playing has a boundary to cut at.
            # A channel still waiting to start is not over - the engine simply
            # reported its own state before getting to it - and closing it would
            # end that item's stream before it had delivered anything.
            current.close()
        if not item.claimed:
            self._signal_ready(uri)

    def _signal_ready(self, uri: str) -> None:
        """
        Tell the queue that this item's audio is live, so its buffer can start filling.

        This replaces the core's blind next-item trigger for realtime sources:
        the audio of a fed item does not exist until the session reaches it, and
        the item is identified by URI because a queue reorder may have moved it.
        """
        queue_id = self.queue_id
        queue = self.mass.player_queues.get(queue_id) if queue_id else None
        if queue is None or queue_id is None or (next_item := queue.next_item) is None:
            return
        if self._track_uri(next_item) != uri:
            return
        self.mass.player_queues.prepare_next_audio_buffer(queue_id)

    async def _check_pairing_lost(self) -> None:
        """
        Fail the session when the engine has no stored session left to log in with.

        The engine advertises itself for pairing while it is still restoring a
        session too, so its report is confirmed against the stored session:
        acting on it alone would fail every playback on a pairing that is only
        moments away from logging in.
        """
        if await asyncio.to_thread(self.backend._has_stored_session):
            return
        self._unpaired = True
        self._fail("the stored session is gone")

    def _observe_auth_state(self, *, logged_in: bool) -> None:
        """
        Follow the engine's login state.

        A daemon accepts a WebSocket connection before it has finished restoring
        its session, so its first snapshot reports logged_in=False even for a
        perfectly good pairing. That is a startup race, not a lost pairing, and
        failing on it would break every playback. Only losing a login that was
        already established is fatal; a pairing that is gone altogether is caught
        by :meth:`_check_pairing_lost`.

        :param logged_in: Whether the engine reports an active login.
        """
        if logged_in:
            self._logged_in = True
            return
        was_logged_in = self._logged_in
        self._logged_in = False
        if was_logged_in:
            self._fail("the session was logged out")

    def _observe_active_device(self, *, is_active: bool) -> None:
        """
        Follow whether this session is still the active Spotify Connect device.

        The engine advertises itself as a Connect device and cannot be told not
        to, so the user can move playback to another one from their Spotify app.
        Only losing the active status :meth:`_play` claimed counts — a respawned
        daemon can report itself active from the session Spotify still has on
        the account, and arming the detector on that would fail the very first
        item of a fresh session.

        :param is_active: Whether the engine reports being the active device.
        """
        if is_active or not self._was_active:
            return
        self._was_active = False
        self._end_on_app_control(SoloistAppControl.TOOK_OVER)

    def _end_on_app_control(self, reason: SoloistAppControl) -> None:
        """
        End the session because the Spotify app took control of it.

        :param reason: What the Spotify app did.
        """
        if not self.usable:
            # a session already on its way out has nothing left to give up, and
            # its teardown pauses the daemon - which must not read as the user
            # pausing and hold off the next session
            return
        self._app_control = reason
        message = _APP_CONTROL_MESSAGES[reason].format(self.backend.provider.name)
        self.logger.info("%s; ending the playback session", message)
        self.backend._note_app_control(reason)
        self._fail(message)

    def _session_error(self) -> AudioError:
        """Return the error an item's stream fails with once the session is gone."""
        if (reason := self._app_control) is not None:
            return SoloistAppControlError(self.backend.provider, reason)
        return AudioError(f"Spotify Soloist: {self._error}")

    async def _repin_options(self, options: SoloistPlaybackOptions) -> None:
        """
        Put shuffle and repeat back to off when something turned them on.

        Music Assistant decides the order, and an engine repeating the current
        item would never advance to the one fed behind it — it would run until
        the item's overrun guard trips.
        """
        client = self._client
        if client is None or self._options_pin_in_flight:
            return
        if not options.shuffle and options.repeat == _REPEAT_OFF:
            return
        self._options_pin_in_flight = True
        try:
            if options.shuffle:
                await client.set_shuffle(False)
            if options.repeat != _REPEAT_OFF:
                await client.set_repeat_track(False)
                await client.set_repeat_context(False)
        except Exception as err:
            # not fatal: the next snapshot carries the options again and
            # re-asserts the pin. Logged because until then the engine may
            # replay this item instead of moving on.
            self.logger.warning("Unable to reset the Spotify playback options: %s", err)
        finally:
            self._options_pin_in_flight = False

    async def _repin_volume(self, volume: int) -> None:
        """
        Pin the engine back at unity volume when the Spotify app changed it.

        Off-unity volume would attenuate the captured PCM; the MA player owns
        the audible volume.
        """
        client = self._client
        if volume == 100 or self._pin_in_flight or client is None:
            return
        self._pin_in_flight = True
        try:
            await client.set_volume(100)
        except Exception as err:
            # not fatal: the next playback_state snapshot carries the volume
            # again and re-asserts the pin. Logged because until then the
            # captured PCM is attenuated by whatever the app set.
            self.logger.warning("Unable to reset the Spotify playback volume: %s", err)
        finally:
            self._pin_in_flight = False


class _ItemAudio:
    """The audio channel of one item within a session, plus what the engine said about it."""

    def __init__(self, uri: str, session: _SoloistSession) -> None:
        """
        Initialize an empty channel for one item.

        :param uri: The canonical Spotify URI of the item.
        :param session: The session this item is played by.
        """
        self.uri = uri
        self.session = session
        self.started = asyncio.Event()
        self.seek_confirmed = asyncio.Event()
        self.seek_target_ms: int | None = None
        self.duration_ms: int | None = None
        self.last_position_ms: int | None = None
        self.started_at_ms: int | None = None
        self.status: str | None = None
        self.playing_seen = False
        self.claimed = False
        # served once already: its audio was handed over and cannot be replayed
        self.spent = False
        self.drain_task: asyncio.Task[None] | None = None
        self._last_write = 0.0
        self._chunks: deque[bytes] = deque()
        self._buffered = 0
        self._written = 0
        self._delivered = 0
        self._seek_anchored = False
        self._seek_floor_ms = 0
        self._tail_target: int | None = None
        self.draining = False
        self._available = asyncio.Event()
        self._closed = False

    @property
    def finishing(self) -> bool:
        """Return whether this is the current item and nothing is queued behind it."""
        return self.session.current is self and not self.session.has_pending

    @property
    def buffered(self) -> int:
        """Return how many captured bytes are waiting to be delivered."""
        return self._buffered

    @property
    def at_own_end(self) -> bool:
        """
        Return whether the engine reported this item played (nearly) to its end.

        Tells a run that genuinely finished apart from someone pausing in the
        Spotify app part-way through the last track. Where ``mid_play`` judges a
        boundary the engine drove, this judges the run's *last* item, which no
        boundary follows.
        """
        if self.duration_ms is None or self.last_position_ms is None:
            # nothing to judge by: treat a stop as the end rather than hanging
            return True
        return self.last_position_ms + _INCOMPLETE_TOLERANCE_MS >= self.duration_ms

    @property
    def mid_play(self) -> bool:
        """
        Return whether the engine is part-way through this item.

        Distinguishes the engine being pulled off an item from it moving on at
        the item's own end, which is an ordinary boundary — one the engine reaches
        within the usual tolerance of the item's duration. Answers False whenever
        there is nothing to judge by, so an unknown position is never read as an
        interruption.
        """
        if not self.started.is_set() or self._closed or self.draining:
            return False
        if self.duration_ms is None or self.last_position_ms is None:
            return False
        # Uncapped on purpose, unlike validate_item's half-duration clamp: on an
        # item shorter than the allowance this answers False throughout, so a
        # takeover there is missed rather than every ordinary boundary on it
        # being called one.
        return self.last_position_ms + _INCOMPLETE_TOLERANCE_MS < self.duration_ms

    @property
    def tail_complete(self) -> bool:
        """Return whether everything this item is going to deliver has arrived."""
        if self._closed:
            return True
        if self._tail_target is not None:
            return self._written >= self._tail_target
        # no duration to aim at: settle for nothing new arriving
        return time.monotonic() - self._last_write >= _DRAIN_TIMEOUT_S

    def claim(self) -> None:
        """Mark this channel as being read; a channel is only ever served once."""
        self.claimed = True
        self.spent = True

    def release(self) -> None:
        """Release the channel after its stream ended (or was abandoned)."""
        self.claimed = False
        self._drop_undelivered()

    def write(self, chunk: bytes) -> None:
        """Append captured audio for this item."""
        if self._closed:
            return
        if self._tail_target is not None and self._written >= self._tail_target:
            # the item is over and its own audio has all arrived; what the sink
            # renders from here on is padding silence, not content
            return
        if not self.claimed and self._buffered >= int(_UNCLAIMED_LIMIT_S * _BYTES_PER_SECOND):
            # nobody is reading this item and nobody is going to: hold the
            # session's clock steady but stop growing
            return
        self._chunks.append(chunk)
        self._buffered += len(chunk)
        self._written += len(chunk)
        self._last_write = time.monotonic()
        self._available.set()

    def start_tail_drain(self) -> None:
        """
        Mark the item as over, accepting only the rest of its own audio.

        Used for the last item of a run, which the engine never reports a track
        change away from, so nothing else would close its channel.
        """
        self.draining = True
        self._tail_target = self._duration_bytes()
        self._last_write = time.monotonic()

    def cancel_tail_drain(self) -> None:
        """Un-arm the tail drain: the item is playing on after all."""
        self.draining = False
        self._tail_target = None

    def close(self) -> None:
        """Close the channel: its stream ends once the buffered audio is drained."""
        self._closed = True
        # a closed channel no longer holds the capture sink open for its tail
        self.draining = False
        self._available.set()
        self._drop_undelivered()

    def arm_seek(self, target_ms: int) -> None:
        """
        Arm a seek to the given position, so position reports can confirm it.

        :param target_ms: The position the engine is being seeked to.
        """
        self.seek_target_ms = target_ms
        self.seek_confirmed.clear()
        self.started_at_ms = None
        reported_ms = self.last_position_ms or 0
        # A fresh session restores the account's last playback state, so seeking
        # the item it was already playing - a resume, or a seek of the current
        # track - makes that restored position indistinguishable from the seek
        # landing. Only a position already inside the target's window has to be
        # disproved that way, by seeing the engine back below where it was;
        # every other start confirms on the first report that reaches the window.
        self._seek_floor_ms = reported_ms
        self._seek_anchored = reported_ms < max(1, target_ms - _SEEK_TOLERANCE_MS)

    def observe_position(self, position_ms: int) -> None:
        """Record a reported playback position (and confirm a pending seek)."""
        if self._closed:
            # positions reported after the cut describe the next item
            return
        # keep the furthest position: the engine's stop/idle snapshot at the end
        # of an item reports position 0 and must not erase the progress the
        # completeness validation relies on (verified live)
        self.last_position_ms = max(self.last_position_ms or 0, position_ms)
        if self.seek_target_ms is None or self.seek_confirmed.is_set():
            return
        if position_ms < self._seek_floor_ms:
            # back below where the engine was when the seek went out: it has
            # restarted the item, so what it reports from here on describes
            # where the seek is taking it
            self._seek_anchored = True
            return
        # the floor of 1 keeps a report of position 0 from landing inside the
        # tolerance window of a small seek target
        if self._seek_anchored and position_ms >= max(1, self.seek_target_ms - _SEEK_TOLERANCE_MS):
            # what the engine reports as the seek lands is where this item's own
            # audio begins
            self.started_at_ms = position_ms
            self.seek_confirmed.set()

    async def read(self) -> AsyncGenerator[bytes]:
        """
        Yield this item's audio until the session moves on to the next one.

        The stream is not capped at the item's duration: with crossfade it
        legitimately carries the head of the next track, and the next item's
        stream begins exactly where this one stops.
        """
        session = self.session
        loop = asyncio.get_running_loop()
        starving_for = 0.0
        while True:
            while self._chunks:
                starving_for = 0.0
                chunk = self._chunks.popleft()
                self._buffered -= len(chunk)
                self._delivered += len(chunk)
                yield chunk
                del chunk
                # re-read the bound every time: the engine may only report the
                # item's duration once it is under way
                overrun_bytes = self._overrun_limit()
                if overrun_bytes is not None and self._delivered >= overrun_bytes:
                    raise AudioError(
                        f"Spotify Soloist never moved on from {self.uri} "
                        f"({self._delivered // _BYTES_PER_SECOND}s delivered)"
                    )
            if self._closed:
                return
            if session._error:
                raise session._session_error()
            self._available.clear()
            deadline = loop.time() + _READ_SLICE_S
            with suppress(TimeoutError):
                async with asyncio.timeout_at(deadline):
                    await self._available.wait()
            if self._chunks or self._closed or self.draining:
                continue
            # the engine is playing something else entirely (skipped from the
            # Spotify app): this item is never going to get its audio
            starving_for += _READ_SLICE_S
            if starving_for >= _STALL_TIMEOUT_S:
                raise AudioError(f"Spotify Soloist delivered no audio for {self.uri}")

    def _drop_undelivered(self) -> None:
        """
        Free audio nothing can read any more, so it stops gating the capture sink.

        A skip leaves its channel closed with its reader gone; without this the
        buffer it had filled would count against ``_MAX_RETAINED_S`` for the rest
        of the session, and enough of them would suspend the sink for good.
        """
        if self.claimed or not self._closed or not self.spent:
            return
        self._chunks.clear()
        self._buffered = 0

    def _duration_bytes(self) -> int | None:
        """
        Return how many bytes this item's own audio amounts to, when known.

        A seeked item starts part-way in, so only what is left of it is ever
        delivered — the full duration would be a target nothing can reach.
        """
        return self._remaining_bytes(self.seek_target_ms or 0)

    def _overrun_limit(self) -> int | None:
        """Return the byte count past which this item is considered stuck."""
        # reported rather than requested: an offset the engine never confirmed
        # would otherwise shrink this bound by audio the item does deliver, and
        # cut a track that is still playing perfectly well
        if (own_audio := self._remaining_bytes(self.started_at_ms or 0)) is None:
            return None
        return own_audio + int(_ITEM_OVERRUN_S * _BYTES_PER_SECOND)

    def _remaining_bytes(self, start_ms: int) -> int | None:
        """Return the bytes of this item's audio left from the given position, when known."""
        if self.duration_ms is None:
            return None
        return max(0, self.duration_ms - start_ms) * CAPTURE_SAMPLE_RATE // 1000 * _FRAME_BYTES


def _decorated_duration_ms(item: object) -> int | None:
    """Return the item duration the engine reports in its playback decorations."""
    decorations = getattr(item, "decorations", None)
    if not isinstance(decorations, dict):
        return None
    playback = decorations.get("playback")
    if not isinstance(playback, dict):
        return None
    duration_ms = playback.get("duration_ms")
    return int(duration_ms) if isinstance(duration_ms, int | float) else None


class _CaptureShaper:
    """
    Turns raw FIFO reads into whole sample frames of real audio.

    Two jobs. It drops the infrastructure silence that precedes the session's
    first decoded sample (bounded, see ``_MAX_LEAD_TRIM_S``). And it carries a
    partial frame across reads: ``StreamReader.read`` returns whatever is
    available, which is not always a whole number of frames, so without this an
    item change between two reads would end one item mid-frame and start the
    next on the remainder - swapping its channels.
    """

    def __init__(self) -> None:
        """Initialize the shaper for one session."""
        self._lead_skipped = 0
        self._first_audio_seen = False
        self._carry = b""

    def shape(self, chunk: bytes) -> bytes:
        """
        Return the next whole frames of audio, or empty when there are none yet.

        :param chunk: The bytes just read from the capture FIFO.
        """
        if self._carry:
            chunk = self._carry + chunk
            self._carry = b""
        if not self._first_audio_seen:
            chunk, skipped = _trim_lead_silence(chunk, self._lead_skipped)
            self._lead_skipped += skipped
            if not chunk.lstrip(b"\x00"):
                # still nothing but pre-roll: hold what was left of the frame so
                # the next read continues on the same grid
                self._carry = chunk
                return b""
            self._first_audio_seen = True
        if remainder := len(chunk) % _FRAME_BYTES:
            self._carry = chunk[-remainder:]
            return chunk[:-remainder]
        self._carry = b""
        return chunk


async def _cancel_and_join(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel the given tasks and wait for them to finish."""
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError, Exception):
            await task


async def _open_fifo_reader(
    fifo_path: Path,
) -> tuple[asyncio.StreamReader, asyncio.ReadTransport]:
    """Open the sink's FIFO for non-blocking reads with a small buffer limit."""
    loop = asyncio.get_running_loop()
    fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        pipe_file = os.fdopen(fd, "rb", buffering=0)
    except OSError:
        os.close(fd)
        raise
    reader = asyncio.StreamReader(limit=_READ_CHUNK_SIZE * 2)
    try:
        transport, _ = await loop.connect_read_pipe(
            partial(asyncio.StreamReaderProtocol, reader), pipe_file
        )
    except BaseException:
        pipe_file.close()
        raise
    return reader, transport


def _trim_lead_silence(chunk: bytes, already_skipped: int) -> tuple[bytes, int]:
    """
    Drop leading infrastructure silence from the head of the session (bounded).

    :param chunk: The chunk read from the FIFO.
    :param already_skipped: Silence bytes dropped from earlier chunks.
    :return: The (possibly emptied/shortened) chunk and how many bytes were dropped.
    """
    max_lead_trim = int(_MAX_LEAD_TRIM_S * _BYTES_PER_SECOND)
    stripped = chunk.lstrip(b"\x00")
    if not stripped:
        if already_skipped + len(chunk) <= max_lead_trim:
            # whole frames only: the offset below places the first sample on the
            # session's frame grid, which holds only while everything dropped
            # before it was a whole number of frames. The leftover bytes go back
            # to the caller, which carries them into the next read.
            dropped = len(chunk) // _FRAME_BYTES * _FRAME_BYTES
            return chunk[dropped:], dropped
        # budget exhausted: this is genuine silence content, not infrastructure
        return chunk, 0
    # Keep sample-frame alignment when the audio starts mid-chunk, and never trim
    # past the budget: whatever silence is left by then is content, not pre-roll.
    remaining = max(0, max_lead_trim - already_skipped)
    offset = min(len(chunk) - len(stripped), remaining) // _FRAME_BYTES * _FRAME_BYTES
    return chunk[offset:], offset
