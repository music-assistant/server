"""
Sendspin Sync plugin provider.

Declares a dependency on the Sendspin player provider, so it only loads once
Sendspin is up. It exposes a single AudioSource - the calibration chirp track -
which plays through the regular playback path so the latency measured from it
matches the latency of real music on the same player.

On top of that source it orchestrates calibration sessions: a set of Sendspin
speakers is grouped onto a hidden anchor, the chirp train runs once for the whole
session, and the API commands registered here walk the speakers one at a time
while a phone-side probe measures each arrival. See session.py for the session
itself.
"""

from __future__ import annotations

import asyncio
import json
from itertools import cycle
from typing import TYPE_CHECKING, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    ActionUnavailable,
    MediaNotFoundError,
    PlayerUnavailableError,
    ResourceBusyError,
)
from music_assistant_models.media_items import AudioSource, ProviderMapping
from music_assistant_models.media_items.audio_format import AudioFormat
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.controllers.streams.audio import AUDIO_SOURCE_CHUNK_SECONDS
from music_assistant.helpers.shared_playback import SENDSPIN_DOMAIN
from music_assistant.models.plugin import PluginProvider

from .chirp import BIT_DEPTH, CHANNELS, SAMPLE_RATE, build_chirp_period
from .session import (
    CalibrationPlayer,
    CalibrationResult,
    CalibrationSession,
    CalibrationSessionState,
    StaticDelaySnapshot,
    is_busy,
    is_eligible,
    is_silent,
    put_static_delay_back,
    resolve_sendspin_protocol,
    resolve_static_delays,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.sendspin.provider import SendspinProvider


SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# stable id of the single AudioSource this provider exposes; combined with the
# provider instance_id it forms the persistent browse/play uri
AUDIO_SOURCE_ID = "calibration"

# A session holds a user's speakers muted and regrouped, so it may never outlive
# the phone that is driving it. A client says it is still there with keep_alive, a
# few times within this window, so a phone that walked out of range for a moment
# does not lose the run and one that has gone gives the speakers back soon.
SESSION_TIMEOUT_SECONDS = 180

# Key of the static delays a session took off and has not put back yet, among the
# stored values of this provider. They are only held in memory otherwise, and the
# zero in their place is stored with each speaker.
CONF_OWED_STATIC_DELAYS = "owed_static_delays"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SendspinSyncProvider(mass, manifest, config, SUPPORTED_FEATURES)


class SendspinSyncProvider(PluginProvider):
    """Sendspin Sync plugin provider for Music Assistant."""

    # tracks which queue currently owns the exclusive AudioSource. Set in
    # on_source_selected (NOT in get_stream_details - that path also runs from
    # queue preload, where claiming would block a later cross-queue handoff).
    _in_use_by_queue: str | None = None
    # the active stream_session_id, paired with _in_use_by_queue: same-queue
    # reconnects refresh this token without changing _in_use_by_queue, so the
    # stream loop and its teardown guard on both still matching.
    _active_session_id: str | None = None

    _audio_format: AudioFormat
    _audio_source: AudioSource
    _period_chunks: list[bytes]

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the Sendspin Sync plugin."""
        super().__init__(mass, manifest, config, supported_features)
        self._unregister_handles: list[Callable[[], None]] = []
        self._session: CalibrationSession | None = None
        self._session_lock = asyncio.Lock()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries for this provider instance."""
        return (CONF_ENTRY_WARN_PREVIEW,)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._audio_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=SAMPLE_RATE,
            bit_depth=BIT_DEPTH,
            channels=CHANNELS,
        )
        self._audio_source = AudioSource(
            item_id=AUDIO_SOURCE_ID,
            provider=self.instance_id,
            name=self.name,
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIO_SOURCE_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._audio_format,
                )
            },
            # pausing would corrupt the phase the measurement reads, and seeking
            # is meaningless on an endless source
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            exclusive=True,
            allow_external_trigger=False,
            # the track is generated locally, so MA can start it on demand
            can_initiate=True,
        )
        # The track is one period generated once and then replayed verbatim:
        # re-synthesising per chunk would let the chirp spacing drift. Synthesis
        # is a tight per-frame math loop, so it runs off the event loop; slicing
        # it up front into the chunk size the streams controller paces
        # AudioSources at leaves the stream loop handing out ready-made chunks.
        period = await asyncio.to_thread(build_chirp_period)
        chunk_size = int(SAMPLE_RATE * AUDIO_SOURCE_CHUNK_SECONDS) * CHANNELS * (BIT_DEPTH // 8)
        self._period_chunks = [
            period[offset : offset + chunk_size] for offset in range(0, len(period), chunk_size)
        ]

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # ahead of the commands, so no session can start over delays still owed
        await self._put_owed_static_delays_back()
        api_handlers = (
            ("sendspin_sync/eligible_players", self.get_eligible_players, Scope.PLAYERS_READ),
            ("sendspin_sync/session", self.get_session, Scope.PLAYERS_READ),
            # CONFIG_PLAYERS_WRITE, unlike the read-only commands above. Starting a
            # session zeroes the static delays and stopping one puts them back, so both
            # persist player config just as applying a result does - exactly what
            # config/players/save is gated on. solo_player writes nothing itself, but is
            # only reachable inside a session that has, so it is raised with them rather
            # than left as a half-open door. PLAYERS_CONTROL is a guest scope, and an
            # in-process call to the Sendspin provider is not re-checked against the
            # caller's scopes.
            ("sendspin_sync/start_session", self.start_session, Scope.CONFIG_PLAYERS_WRITE),
            ("sendspin_sync/solo_player", self.solo_player, Scope.CONFIG_PLAYERS_WRITE),
            ("sendspin_sync/keep_alive", self.keep_alive, Scope.CONFIG_PLAYERS_WRITE),
            (
                "sendspin_sync/apply_measurements",
                self.apply_measurements,
                Scope.CONFIG_PLAYERS_WRITE,
            ),
            ("sendspin_sync/stop_session", self.stop_session, Scope.CONFIG_PLAYERS_WRITE),
        )
        for command, handler, required_scope in api_handlers:
            self._unregister_handles.append(
                self.mass.register_api_command(command, handler, required_scope=required_scope)
            )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Call when the provider is being unloaded.

        :param is_removed: Whether the provider is being removed (vs just reloaded).
        """
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        # call_later timers are not swept by mass.stop(), so the pending timeout is
        # cancelled explicitly before the session it would have torn down is stopped
        self._cancel_session_timeout()
        async with self._session_lock:
            if self._session is not None:
                await self._session.stop()
                self._session = None
        await super().unload(is_removed)

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        return [self._audio_source]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return StreamDetails for the calibration AudioSource.

        Raises MediaNotFoundError for any other item_id.
        """
        if item_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {item_id}")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=StreamMetadata(title=self.name),
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Yield the calibration track as raw PCM, looping the precomputed period."""
        consumer_queue = self._in_use_by_queue
        captured_session_id = self._active_session_id
        chunks = cycle(self._period_chunks)
        try:
            # Unlike every other AudioSource, nothing upstream rate-limits this
            # one - it is synthesised, not received. The streams controller paces
            # it instead, through realtime_pcm_pacer on the format-match path and
            # ffmpeg's -re on the resampling path, so the loop must not self-pace
            # on top of that.
            while (
                self._in_use_by_queue == consumer_queue
                and self._active_session_id == captured_session_id
            ):
                yield next(chunks)
            self.logger.debug(
                "Stopping calibration stream for queue %s: this session no longer owns the source",
                consumer_queue,
            )
        finally:
            if (
                self._in_use_by_queue == consumer_queue
                and self._active_session_id == captured_session_id
            ):
                self._in_use_by_queue = None

    async def on_source_selected(
        self, source_id: str, player_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """React to the calibration AudioSource being selected for playback on a player."""
        if source_id != AUDIO_SOURCE_ID:
            return
        self._in_use_by_queue = queue_id
        self._active_session_id = stream_session_id

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """React to MA tearing down the calibration AudioSource's stream from a queue."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if self._active_session_id != stream_session_id:
            return
        self._active_session_id = None
        if self._in_use_by_queue == queue_id:
            self._in_use_by_queue = None
        # The stream carries the phase reference, so a calibration session can not
        # outlive it. It may end without us asking - every client disconnecting tears
        # the group's playback down - and the session must then restore the speakers
        # instead of sitting there believing it is still live.
        session = self._session
        if session is not None and queue_id == session.queue_id and not session.stopped:
            self.logger.debug("Calibration stream ended externally, stopping the session")
            self.mass.create_task(self._stop_session(session), task_id=self._session_teardown_id)

    async def get_eligible_players(self) -> list[CalibrationPlayer]:
        """
        Return the players a calibration session can be run against.

        Only speakers that play over Sendspin qualify, and only those that can be taken
        out of the mix - a player with neither a mute nor a volume control can never be
        isolated. Every speaker is offered as the player the user knows it by, not as the
        Sendspin protocol it plays through.

        A speaker whose client does not accept a static delay is still offered, with
        ``adjustable`` false: it can be measured like any other, and its result comes
        back from ``apply_measurements`` for the user to apply on the device itself.
        """
        sendspin = cast("SendspinProvider | None", self.mass.get_provider(SENDSPIN_DOMAIN))
        if sendspin is None:
            # nothing to measure without it, and nothing to ask about a speaker either
            return []
        offered: list[CalibrationPlayer] = []
        # all_players (not iter_players) so a non-admin user is only offered the
        # speakers they are allowed to see. Unfiltered by provider: a speaker that plays
        # over Sendspin can be a player of any provider.
        for player in self.mass.players.all_players(return_unavailable=False):
            protocol = resolve_sendspin_protocol(player)
            if protocol is None or not is_eligible(self.mass, player):
                continue
            offered.append(
                CalibrationPlayer(
                    player_id=player.player_id,
                    name=player.state.name,
                    busy=is_busy(player),
                    # asked of the speaker's Sendspin side, which is the only object
                    # that carries a static delay
                    adjustable=sendspin.supports_player_static_delay(protocol.protocol_id),
                    silent=is_silent(player),
                )
            )
        return offered

    async def get_session(self) -> CalibrationSessionState | None:
        """Return the state of the running calibration session, or None if there is none."""
        if self._session is None:
            return None
        return self._session.state

    async def start_session(
        self, player_ids: list[str], force: bool = False
    ) -> CalibrationSessionState:
        """
        Start a calibration session over the given Sendspin players.

        The calibration track starts once and runs for the whole session; use
        ``solo_player`` to walk the speakers without restarting it. Every speaker is
        silenced before the track starts, so nothing is heard until ``solo_player``
        opens one up. The session is torn down automatically after a period of
        inactivity, so a phone that goes away can not leave the speakers muted or
        measuring from a delay of zero.

        Every speaker MA can write a static delay to is measured from zero, so a delay
        already in place can not push its arrival outside the window the probe counts in.
        Stopping the session puts each of those back, unless ``apply_measurements`` has
        meanwhile written a correction over it.

        :param player_ids: The speakers to calibrate, in the order given, as
            ``eligible_players`` offers them.
        :param force: Take over players that are busy playing the user's own content.
        :raises ResourceBusyError: If a calibration session is already running.
        :raises InvalidDataError: If no players were given, or the same player twice.
        :raises PlayerUnavailableError: If a given player is unknown or unavailable.
        :raises UnsupportedFeaturedException: If a given player does not render over
            Sendspin, or has neither a mute nor a volume control.
        :raises ActionUnavailable: If a player is already muted or turned all the way
            down, is playing and ``force`` was not set, could not be silenced, or the
            Sendspin provider is not loaded.
        :return: The state of the started session.
        """
        async with self._session_lock:
            if self._session is not None:
                raise ResourceBusyError(
                    "A calibration session is already running",
                    translation_key="session_already_running",
                    translation_owner=self.translation_owner,
                )
            session = await CalibrationSession.create(
                self.mass,
                self.logger,
                self.translation_owner,
                owner_instance_id=self.instance_id,
                player_ids=player_ids,
                force=force,
                on_owed_delays_changed=self._store_owed_static_delays,
            )
            # Registered before the stream starts: starting it fires the source
            # lifecycle hooks, and on_source_unselected can only tear the session
            # down again if it can already see it.
            self._session = session
            self._arm_session_timeout()
            try:
                await session.begin(self._audio_source)
            except BaseException:
                # also a cancellation: the session stays registered until it is torn
                # down, so an unload that follows can finish a stop that was cut short
                self._cancel_session_timeout()
                await session.stop()
                self._session = None
                raise
            return session.state

    async def solo_player(self, player_id: str) -> CalibrationSessionState:
        """
        Isolate one speaker in the running session so only it is audible.

        Brings the target out of the silence the session started it in, leaves every
        other member silent, and never mutes the target itself. Leaves the calibration
        stream running.

        :param player_id: The session member to isolate.
        :raises ActionUnavailable: If no calibration session is running.
        :raises InvalidDataError: If the given player is not part of the session.
        :return: The state of the session.
        """
        async with self._session_lock:
            if self._session is None:
                raise ActionUnavailable(
                    "No calibration session is running",
                    translation_key="no_session",
                    translation_owner=self.translation_owner,
                )
            # ahead of the solo: one that fails part-way leaves a session that is still
            # being driven, by a client that is told what went wrong
            self._arm_session_timeout()
            await self._session.solo(player_id)
            return self._session.state

    async def keep_alive(self) -> CalibrationSessionState:
        """
        Say that the client driving the running session is still there.

        A session gives the speakers back when nothing has driven it for a while, and
        this is the command a client sends in between: it changes nothing else.

        :raises ActionUnavailable: If no calibration session is running.
        :return: The state of the session.
        """
        if self._session is None or self._session.stopped:
            raise ActionUnavailable(
                "No calibration session is running",
                translation_key="no_session",
                translation_owner=self.translation_owner,
            )
        self._arm_session_timeout()
        return self._session.state

    async def apply_measurements(self, offsets_ms: dict[str, float]) -> CalibrationResult:
        """
        Turn the session's measured arrival offsets into static delays and apply them.

        The offsets are *relative*: the probe measures every speaker against one
        arbitrary shared baseline, so only the differences between them mean anything.
        Each player's current static delay is folded in before normalising, which keeps
        re-running a calibration convergent instead of drifting. Starting a session takes
        off every static delay MA can write, so on a first apply those come in at 0 and
        the correction falls out of the measurement alone - replacing whatever delay MA
        held for that speaker, including one the user set by hand. What a speaker's own
        firmware applies is neither readable nor removable and is already inside the
        arrival that was measured.

        A correction supersedes the session's restore for the speakers it was written to;
        one left unmeasured, or whose write failed, still goes back to what it had.

        Every speaker in the session is normalised the same way, including one whose
        client does not accept a static delay - it belongs in the baseline the others
        are measured against just as much, and may well be the earliest arrival that
        defines it. Only the last step splits: the delays MA can write are written and
        come back under ``applied``, and the rest come back under ``manual`` for the
        user to set on the device itself. An ``applied`` value replaces the delay its
        speaker carried; a ``manual`` value is added to whatever its firmware already
        applies. See :class:`CalibrationResult`.

        Every member of the session must be measured, and nothing is written until every
        value has been computed and accepted, so no rejected measurement can leave the
        group half corrected. A player that drops off mid-apply still can: the write that
        fails raises and the players already written keep their new delay. The returned
        result is therefore only meaningful on success.

        Leaves the session running so the result can be verified in place: measure
        again, and every speaker should now report the same arrival. The session's
        inactivity timeout still applies, so a client has to apply within it.

        :param offsets_ms: Measured arrival offset per player id, in milliseconds.
        :raises ActionUnavailable: If no calibration session is running, or the Sendspin
            provider is not available.
        :raises InvalidDataError: If the measurements do not cover exactly the session's
            members, a value is not finite, or one implies a delay Sendspin can not carry.
        :raises PlayerUnavailableError: If a member of the session has meanwhile gone.
        :raises UnsupportedFeaturedException: If a member stopped accepting a static
            delay between being checked and being used, which a reconnect can do.
        :return: The resolved static delay per player, split into the ones MA applied and
            the ones the user has to apply themselves.
        """
        async with self._session_lock:
            if self._session is None:
                raise ActionUnavailable(
                    "No calibration session is running",
                    translation_key="no_session",
                    translation_owner=self.translation_owner,
                )
            sendspin = self._sendspin_provider()
            delay_targets = self._delay_targets(sendspin, self._session.player_ids)
            # The Sendspin provider refuses to read a delay off a speaker it can not
            # write one to: it holds no value for such a speaker, so a read would report
            # its configured default, and a bridge client can set that to a non-zero
            # value the device is not actually carrying. Substituting 0 is what that
            # refusal leaves, and it is the honest number - whatever delay the firmware
            # applies is already inside the arrival this run measured.
            current_delays_ms = {
                player_id: sendspin.get_player_static_delay(target) if target else 0
                for player_id, target in delay_targets.items()
            }
            delays_ms = resolve_static_delays(offsets_ms, current_delays_ms, self.translation_owner)
            result = CalibrationResult(applied={}, previous={}, manual={})
            # Saving these mid-session does not disturb the measurement: the static delay
            # config entry is immediate_apply and carries no requires_reload, so the
            # config controller pushes it straight to the client instead of restarting
            # the queue - which would end the stream the measurements are phased against.
            for player_id, delay_ms in delays_ms.items():
                target = delay_targets[player_id]
                if target is None:
                    result.manual[player_id] = delay_ms
                    continue
                await sendspin.set_player_static_delay(target, delay_ms)
                # after the write, never before: only a correction that landed may
                # supersede the session's restore. See keep_applied_static_delay.
                self._session.keep_applied_static_delay(player_id)
                result.applied[player_id] = delay_ms
                # a member that only started accepting a delay mid-session had nothing
                # taken off, so what it carried going into this apply is what it had
                before_ms = self._session.static_delay_before(player_id)
                result.previous[player_id] = (
                    current_delays_ms[player_id] if before_ms is None else before_ms
                )
            self._arm_session_timeout()
            return result

    async def stop_session(self) -> None:
        """
        Stop the running calibration session and restore every speaker it took over.

        Does nothing when no session is running.
        """
        await self._stop_session()

    def _sendspin_provider(self) -> SendspinProvider:
        """
        Return the Sendspin provider a measured correction is applied through.

        :raises ActionUnavailable: If the Sendspin provider is not available.
        """
        sendspin = cast("SendspinProvider | None", self.mass.get_provider(SENDSPIN_DOMAIN))
        if sendspin is None:
            raise ActionUnavailable(
                "The Sendspin provider is not available",
                translation_key="sendspin_unavailable",
                translation_owner=self.translation_owner,
            )
        return sendspin

    def _delay_targets(
        self, sendspin: SendspinProvider, player_ids: list[str]
    ) -> dict[str, str | None]:
        """
        Return the Sendspin protocol each session member's static delay is written to.

        A member is the speaker as the user sees it, so the Sendspin protocol it plays
        through is resolved here. The value is None for a member MA can not write a
        delay to, which is what splits the result of an apply.

        Resolved in one pass up front, because a client is free to change its answer
        across a reconnect and this one answer governs both halves of the calculation:
        asking again later could fold a speaker's current delay in as 0 and then write
        the result to it as an absolute, which are two different numbers.

        :param sendspin: The Sendspin provider to ask.
        :param player_ids: The session members to resolve.
        :raises PlayerUnavailableError: If a member has meanwhile gone, or lost the
            Sendspin side a delay would be written to. Sendspin answers the same False
            for a speaker that has left as for one that refuses a delay, and reporting
            the first as "set this one by hand" would be a lie.
        :raises ActionUnavailable: If another protocol has taken over the output of a
            bridged member since the session started. Its measurement was taken over the
            carrier before, and each carrier has a latency and a static delay of its own.
        """
        assert self._session is not None
        targets: dict[str, str | None] = {}
        for player_id in player_ids:
            player = self.mass.players.get_player(player_id)
            protocol = (
                resolve_sendspin_protocol(player)
                if player is not None and player.state.available
                else None
            )
            if protocol is None:
                name = player.state.name if player is not None else player_id
                raise PlayerUnavailableError(
                    f"Player {name} is not available",
                    translation_key="player_unavailable",
                    translation_args=[name],
                    translation_owner=self.translation_owner,
                )
            if not sendspin.supports_player_static_delay(protocol.protocol_id):
                targets[player_id] = None
                continue
            carrier_id = sendspin.get_player_static_delay_carrier(protocol.protocol_id)
            if not self._session.is_measured_over(player_id, carrier_id):
                raise ActionUnavailable(
                    f"Player {player_id} plays through another protocol than it was measured over",
                    translation_key="player_output_changed",
                    translation_args=[player.state.name if player else player_id],
                    translation_owner=self.translation_owner,
                )
            targets[player_id] = protocol.protocol_id
        if fixed := [player_id for player_id, target in targets.items() if target is None]:
            # a speaker reported back for the user to adjust by hand leaves no other
            # trace, so a client whose support flapped can be told apart from one that
            # never advertised the command in the first place
            self.logger.debug("Calibration can not write a static delay to: %s", ", ".join(fixed))
        return targets

    def _arm_session_timeout(self) -> None:
        """(Re)start the inactivity timeout that tears a forgotten session down."""
        self.mass.call_later(
            SESSION_TIMEOUT_SECONDS,
            self._handle_session_timeout,
            task_id=self._session_timeout_id,
        )

    def _cancel_session_timeout(self) -> None:
        """Cancel a pending session timeout."""
        self.mass.cancel_timer(self._session_timeout_id)

    async def _handle_session_timeout(self) -> None:
        """Tear down a session that has not been driven for a while."""
        if (session := self._session) is None:
            return
        self.logger.warning(
            "Calibration session timed out after %s seconds without activity, restoring players",
            SESSION_TIMEOUT_SECONDS,
        )
        await self._stop_session(session)

    async def _stop_session(self, session: CalibrationSession | None = None) -> None:
        """
        Stop and clear the running session, if any.

        :param session: The session to stop, None for whichever one is running. A
            teardown that was queued for one session waits for the lock, and may not
            stop the session that was started in the meantime.
        """
        async with self._session_lock:
            if self._session is None or session not in (None, self._session):
                return
            self._cancel_session_timeout()
            await self._session.stop()
            self._session = None

    def _store_owed_static_delays(self, owed: Mapping[str, StaticDelaySnapshot]) -> None:
        """
        Keep the static delays the running session still has to put back.

        Stored at once, not with the next save of the config: a server that goes down in
        between would come back with speakers at zero and nothing that knows their delay.

        :param owed: The delay taken off each member, by member id.
        """
        entries = [
            [snapshot.sendspin_protocol_id, snapshot.delay_ms, snapshot.carrier_id]
            for snapshot in owed.values()
        ]
        try:
            self.mass.config.set_raw_provider_config_value(
                self.instance_id, CONF_OWED_STATIC_DELAYS, json.dumps(entries), immediate=True
            )
        except KeyError:
            # the provider is being removed, and its stored config went first
            self.logger.debug("No stored config left to keep the owed static delays in")

    async def _put_owed_static_delays_back(self) -> None:
        """
        Put back the static delays a session that never got to stop left at zero.

        A session takes the delays off for as long as it measures, and a server that
        went down mid-session - or a speaker whose provider was gone when it stopped -
        leaves them off. Each is written to what is stored of its speaker, and to the
        speaker itself when it is already there.
        """
        stored = self.mass.config.get_raw_provider_config_value(
            self.instance_id, CONF_OWED_STATIC_DELAYS
        )
        if not isinstance(stored, str) or not (entries := json.loads(stored)):
            return
        sendspin = cast("SendspinProvider | None", self.mass.get_provider(SENDSPIN_DOMAIN))
        if sendspin is None:
            # kept for the next load: this provider only loads once Sendspin has
            return
        for protocol_id, delay_ms, carrier_id in entries:
            self.logger.warning(
                "Putting back the static delay of %s ms a calibration left off %s",
                delay_ms,
                protocol_id,
            )
            try:
                await put_static_delay_back(
                    sendspin, StaticDelaySnapshot(protocol_id, delay_ms, carrier_id)
                )
            except Exception as err:
                # deliberately broad: one speaker that can not take its delay back may
                # not keep the provider from loading, nor the others from theirs
                self.logger.warning(
                    "Could not put the static delay of %s back; set it to %s ms by hand: %s",
                    protocol_id,
                    delay_ms,
                    err,
                )
        self._store_owed_static_delays({})

    @property
    def _session_timeout_id(self) -> str:
        """Return the timer id of this instance's session timeout."""
        return f"{self.instance_id}_calibration_timeout"

    @property
    def _session_teardown_id(self) -> str:
        """Return the task id that dedupes teardowns triggered by the stream ending."""
        return f"{self.instance_id}_calibration_teardown"
