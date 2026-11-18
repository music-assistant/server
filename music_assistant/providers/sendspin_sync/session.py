"""
Calibration session orchestration for the Sendspin Sync plugin.

A session commandeers a set of Sendspin players, groups them onto a hidden
Sendspin virtual player and starts the calibration track on that anchor's queue
exactly once. The phone-side probe then measures one speaker at a time, driven
by :meth:`CalibrationSession.solo`.

A member is the speaker as the user sees it, not the Sendspin protocol it plays
through: grouping, volume and mute are all driven at that level and the controller
translates them to the protocol itself. Only a static delay is written to the
Sendspin side, resolved with :func:`resolve_sendspin_protocol`.

A speaker that plays over Sendspin through a bridge (AirPlay, squeezelite, Chromecast)
keeps one static delay per protocol that carries its output. A session measures a
speaker over whatever carries it when the session starts, and everything it reads or
writes is the delay of that carrier.

Two properties of the orchestration are load-bearing for the measurement:

- **The stream is started once and never restarted.** The chirp train is a
  metronome running on the server clock and the probe recovers a speaker's
  latency as a phase offset against it, so a restart resets the timeline and
  invalidates every measurement taken against the old one. Isolation therefore
  only ever touches per-player mute/volume, never the queue.
- **At most one member is audible at a time**, so an arrival can be attributed to a
  specific speaker. Every member is silenced as it is commandeered, so the chirp
  train starts out inaudible and a solo is what opens up a single speaker.

The anchor is a virtual player rather than one of the targets on purpose. It
owns its own queue, so no user queue is ever replaced, and it never renders
audio itself, so every target is a uniform group member reached through the same
sync path - a real leader would render through the leader path instead and its
latency would not be comparable to the members'.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING, Any, cast

from mashumaro import DataClassDictMixin
from music_assistant_models.constants import PLAYER_CONTROL_NONE
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.enums import QueueOption as MAQueueOption
from music_assistant_models.errors import (
    ActionUnavailable,
    InvalidDataError,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.constants import ATTR_MUTE_LOCK
from music_assistant.helpers.shared_playback import (
    SENDSPIN_DOMAIN,
    SharedPlaybackSession,
    is_remote_session_host,
)
from music_assistant.providers.sendspin.constants import MAX_SENDSPIN_STATIC_DELAY

if TYPE_CHECKING:
    import logging
    from collections.abc import Coroutine, Mapping

    from music_assistant_models.media_items import AudioSource
    from music_assistant_models.player import PlayerMedia

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player
    from music_assistant.models.player_protocol import PlayerProtocol
    from music_assistant.providers.sendspin.provider import SendspinProvider

# Name of the hidden anchor player. It is never shown in the UI, but it does end
# up in logs and diagnostics, so it names what it is.
ANCHOR_DISPLAY_NAME = "Sendspin Calibration"

# Player types that render audio and can therefore be calibrated. A session drives
# the player a user sees. Sendspin also registers display, visualizer and light
# players, none of which is a speaker; an allowlist keeps a future player type out
# until it is considered.
CALIBRATABLE_PLAYER_TYPES = (PlayerType.PLAYER, PlayerType.STEREO_PAIR)

# States in which a player is considered busy with the user's own content.
BUSY_PLAYBACK_STATES = (PlaybackState.PLAYING, PlaybackState.PAUSED)


class SilenceMethod(StrEnum):
    """How a session took a player out of the mix, and therefore how to put it back."""

    MUTE = "mute"
    VOLUME = "volume"


@dataclass
class CalibrationPlayer(DataClassDictMixin):
    """
    A speaker a calibration session can be run against.

    Identified and named as the user sees it: the player, not the Sendspin protocol it
    plays through.
    """

    player_id: str
    name: str
    # whether the speaker is busy with the user's own content, so a client can warn
    # before a session takes it over. Mirrors the rule start_session enforces, so a
    # client never offers a speaker that the server will then refuse.
    busy: bool
    # whether the measured correction can be written to the speaker. A client shows
    # this at pick time, so a user learns which speakers they will have to correct by
    # hand before walking the house rather than when the result comes back.
    adjustable: bool


@dataclass
class CalibrationResult(DataClassDictMixin):
    """
    The static delays a session resolved, split by whether MA could write them.

    Mind that the two halves are not the same kind of number. An ``applied`` value is
    the absolute delay the speaker now carries, and replaces whatever it had. A
    ``manual`` value is how much further forward that speaker has to come, so it is
    *added* to any delay its firmware already applies - that one is already inside the
    arrival the probe measured, and MA can not read it.
    """

    applied: dict[str, int]
    # the delay resolved for each speaker MA can not write to, for the user to set on
    # the device itself - by opting its client into an adjustable delay and re-running,
    # or by adding the value to its firmware configuration
    manual: dict[str, int]


@dataclass
class CalibrationSessionState(DataClassDictMixin):
    """The state of a calibration session, as rendered by a client."""

    anchor_player_id: str
    queue_id: str
    player_ids: list[str]
    soloed_player_id: str | None
    streaming: bool


@dataclass(frozen=True, slots=True)
class StaticDelaySnapshot:
    """The static delay a session took off a member, and where to write it back to."""

    # the Sendspin protocol the delay came off, held rather than resolved again at
    # teardown: a member whose Sendspin side has meanwhile gone resolves to nothing, and
    # one that reconnected in between could resolve to a different client than the delay
    # was taken from
    sendspin_protocol_id: str
    delay_ms: int
    # the protocol that carried the output when the delay came off, None for a Sendspin
    # client of its own. The delay goes back to that carrier, also when another one has
    # taken the output over since.
    carrier_id: str | None


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    """Everything a calibration session must put back on a player it commandeered."""

    player_id: str
    powered: bool
    synced_to: str | None
    active_group: str | None
    active_source: str | None
    current_media: PlayerMedia | None
    volume_level: int | None
    volume_muted: bool
    was_playing: bool


class CalibrationSession:
    """
    One running calibration session over a group of Sendspin players.

    Build it with :meth:`create`, start its stream with :meth:`begin`, walk the
    speakers with :meth:`solo`, and end it with :meth:`stop`. :meth:`stop` restores
    every player the session touched and is safe to call more than once.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        logger: logging.Logger,
        translation_owner: str,
        shared_session: SharedPlaybackSession,
        snapshots: dict[str, PlayerSnapshot],
    ) -> None:
        """Initialize the session. Use the :meth:`create` factory instead."""
        self.mass = mass
        self.logger = logger
        self.translation_owner = translation_owner
        self._shared_session = shared_session
        self._snapshots = snapshots
        self._silenced: dict[str, SilenceMethod] = {}
        # what this session zeroed and still owes back, by member id. Both records at
        # once, like _silenced: an entry means the delay was taken off and not yet put
        # back, and it is dropped by the restore or by an apply that superseded it.
        self._zeroed_delays: dict[str, StaticDelaySnapshot] = {}
        # the protocol that carried the output of each member MA can write a delay to
        # when the session took it, by member id: what the member is measured over
        self._measured_carriers: dict[str, str | None] = {}
        self._soloed_player_id: str | None = None
        self._stopped = False

    @classmethod
    async def create(
        cls,
        mass: MusicAssistant,
        logger: logging.Logger,
        translation_owner: str,
        owner_instance_id: str,
        player_ids: list[str],
        force: bool = False,
    ) -> CalibrationSession:
        """
        Commandeer the given players and group them onto a fresh calibration anchor.

        Every member is silenced on the way in, so the track :meth:`begin` starts can not
        be heard until a :meth:`solo` opens up a speaker. Every member MA can write a
        static delay to is measured from zero, so what it takes off is put back by
        :meth:`stop`.

        :param mass: MusicAssistant instance.
        :param logger: Logger of the owning provider.
        :param translation_owner: Namespace whose strings.json localizes this session's errors.
        :param owner_instance_id: Instance id of the provider that owns the session;
            the anchor player is removed when it unloads.
        :param player_ids: The speakers to calibrate, in the order given, as the user
            sees them - not the Sendspin protocols they play through.
        :param force: Take over players that are busy playing the user's own content.
        :raises InvalidDataError: If no players were given, or the same player twice.
        :raises PlayerUnavailableError: If a given player is unknown or unavailable.
        :raises UnsupportedFeaturedException: If a given player does not render over
            Sendspin, or has neither a mute nor a volume control.
        :raises ActionUnavailable: If a player is busy playing and ``force`` was not set,
            if one could not be silenced, or if the Sendspin provider is not loaded.
        :return: The commandeered, not yet streaming session.
        """
        if not player_ids:
            raise InvalidDataError(
                "A calibration session needs at least one player",
                translation_key="no_players",
                translation_owner=translation_owner,
            )
        if len(set(player_ids)) != len(player_ids):
            raise InvalidDataError(
                "Duplicate player ids in the calibration session",
                translation_key="duplicate_players",
                translation_owner=translation_owner,
            )
        players = [_validate_player(mass, player_id, translation_owner) for player_id in player_ids]
        _reject_silent_players(players, translation_owner)
        if not force:
            _reject_busy_players(players, translation_owner)
        sendspin = _require_sendspin(mass, translation_owner)
        snapshots = {player.player_id: _snapshot(player) for player in players}
        shared_session = await SharedPlaybackSession.create_remote(
            mass,
            owner_instance_id=owner_instance_id,
            display_name=ANCHOR_DISPLAY_NAME,
        )
        # the session only owns what it managed to group and zero, so a failure halfway
        # restores those players and leaves the untouched ones alone
        session = cls(mass, logger, translation_owner, shared_session, {})
        try:
            for player in players:
                await shared_session.add_guest_listener(player.player_id)
                session._snapshots[player.player_id] = snapshots[player.player_id]
                # taken out of the mix as it joins, so the track begin() starts is
                # inaudible from its first chirp rather than from the first solo. After
                # the grouping, not before: a mute only takes the lock that survives a
                # group volume change once the player is already in the group.
                await session._silence_member(player)
                await session._zero_static_delay(sendspin, player)
        except Exception:
            await session.stop()
            raise
        return session

    @property
    def anchor_player_id(self) -> str:
        """Return the player_id of the hidden anchor that leads the calibration group."""
        return self._shared_session.player_id

    @property
    def queue_id(self) -> str:
        """Return the queue_id the calibration track streams on."""
        return self._shared_session.queue_id

    @property
    def player_ids(self) -> list[str]:
        """Return the players this session commandeered, in the order given."""
        return list(self._snapshots)

    @property
    def soloed_player_id(self) -> str | None:
        """Return the player currently isolated for measurement, if any."""
        return self._soloed_player_id

    @property
    def stopped(self) -> bool:
        """Return whether this session has already been torn down."""
        return self._stopped

    @property
    def streaming(self) -> bool:
        """Return whether the anchor's queue is still playing the calibration track."""
        queue = self.mass.player_queues.get(self.queue_id)
        return (
            queue is not None
            and queue.state == PlaybackState.PLAYING
            and queue.current_item is not None
            and queue.current_item.media_type == MediaType.AUDIO_SOURCE
        )

    @property
    def state(self) -> CalibrationSessionState:
        """Return the session state a client renders its progress from."""
        return CalibrationSessionState(
            anchor_player_id=self.anchor_player_id,
            queue_id=self.queue_id,
            player_ids=self.player_ids,
            soloed_player_id=self._soloed_player_id,
            streaming=self.streaming,
        )

    async def begin(self, audio_source: AudioSource) -> None:
        """
        Start the calibration track on the anchor's queue.

        Called once per session. Nothing afterwards may touch the anchor's queue:
        the phase reference every measurement is taken against lives in this stream.

        :param audio_source: The calibration AudioSource to stream for the session.
        """
        # REPLACE explicitly: the enqueue default for live sources is user
        # configurable, and anything but REPLACE would not start the track.
        await self.mass.player_queues.play_media(
            self.queue_id, audio_source, option=MAQueueOption.REPLACE
        )

    async def solo(self, player_id: str) -> None:
        """
        Isolate one member of the session so only it is audible.

        Mutes every other member and issues no mute command against the target, so
        the speaker being measured is never put into mute as part of isolating it:
        clients that stop feeding their DAC while muted shift their timing on
        resume, which would corrupt the number being measured. The target is brought
        out of the silence it is in - every member starts silenced, and a later solo
        puts the previous one back - before the others go down, so it has the longest
        settling window this call can give it.

        That window is only as long as the mute commands that follow it, so the
        probe consuming this session is expected to discard the first chirp periods
        after a solo rather than treat the speaker as settled immediately.

        Never touches the queue, so the calibration stream keeps running.

        :param player_id: The member to isolate.
        :raises ActionUnavailable: If the session has been stopped.
        :raises InvalidDataError: If the given player is not a member of the session.
        """
        if self._stopped:
            raise ActionUnavailable(
                "This calibration session has been stopped",
                translation_key="session_stopped",
                translation_owner=self.translation_owner,
            )
        if player_id not in self._snapshots:
            raise InvalidDataError(
                f"Player {player_id} is not part of this calibration session",
                translation_key="player_not_in_session",
                translation_args=[player_id],
                translation_owner=self.translation_owner,
            )
        await self._unsilence(player_id)
        for other_id in self._snapshots:
            if other_id != player_id:
                await self._silence(other_id)
        self._soloed_player_id = player_id

    def is_measured_over(self, player_id: str, carrier_id: str | None) -> bool:
        """
        Return whether a member is still carried by what this session measured it over.

        A bridged speaker has one latency, and one static delay, per protocol that
        carries its Sendspin output. A measurement taken over one carrier is no
        correction for another, so a member whose output was handed over mid-session
        has to be measured again.

        :param player_id: The session member to check.
        :param carrier_id: Id of the protocol that carries the member's output now, None
            for a Sendspin client of its own.
        """
        return self._measured_carriers.get(player_id) == carrier_id

    def keep_applied_static_delay(self, player_id: str) -> None:
        """
        Take a member out of the static delay restore, once a correction was written to it.

        A correction is an absolute delay written over the zero this session put in
        place, so putting the member's pre-session delay back at teardown would overwrite
        it with the very value it was computed to replace. Call this for each member a
        correction actually landed on; the rest are left to go back to what they had.

        Idempotent, and a no-op for a member this session never zeroed.

        :param player_id: The session member whose new delay must survive teardown.
        """
        self._zeroed_delays.pop(player_id, None)

    async def stop(self) -> None:
        """
        End the session and restore every player it commandeered.

        Each restore step is logged rather than raised on failure, so one player
        that has meanwhile disconnected can not strand the rest muted or grouped,
        and the restore runs even when stopping the stream fails. Safe to call on
        an already stopped session.
        """
        if self._stopped:
            return
        # Set before anything is torn down: stopping the stream fires
        # on_source_unselected, whose session teardown guards on this flag to avoid
        # re-entering the stop it was triggered by.
        self._stopped = True
        self._soloed_player_id = None
        # nested so that neither the stream stop nor the restore can leave the
        # hidden anchor player (and its queue) registered behind them
        try:
            await self._stop_calibration_stream()
        finally:
            try:
                await self._restore_players()
            finally:
                await self._close_shared_session()

    async def _stop_calibration_stream(self) -> None:
        """Stop the calibration track on the anchor's queue."""
        if self.mass.player_queues.get(self.queue_id) is None:
            # the anchor (and its queue) is already gone, e.g. because the Sendspin
            # provider that hosts it reloaded underneath the session
            return
        try:
            await self.mass.player_queues.stop(self.queue_id)
        except Exception as err:
            # deliberately broad: this may never mask the restore below, and the queue
            # controller reaches into the player provider, which is free to surface
            # anything its client library raises. CancelledError still propagates.
            self.logger.warning("Could not stop the calibration stream: %s", err)

    async def _restore_players(self) -> None:
        """Put delay, volume, mute, group membership, power and playback back as found."""
        for snapshot in self._snapshots.values():
            await self._restore_player(snapshot)
        self._report_stranded_static_delays()

    async def _restore_player(self, snapshot: PlayerSnapshot) -> None:
        """
        Put a single commandeered player back the way it was found.

        Every step is attempted independently: they all reach into the player
        provider, and a speaker that fails to leave the group must still have its
        volume and mute put back rather than be left silent.
        """
        player = self.mass.players.get_player(snapshot.player_id)
        if player is None or not player.state.available:
            self.logger.warning(
                "Can not restore player %s after calibration: it is no longer available",
                snapshot.player_id,
            )
            self._release_stale_mute_lock(player)
            return
        # ahead of the steps below: releasing a player from the group and putting its
        # power back can take its Sendspin client offline, and the delay can not be
        # written to a client that is no longer there
        await self._restore_step(
            player, "restore the static delay", self._restore_static_delay(snapshot.player_id)
        )
        await self._restore_step(
            player, "release from the calibration group", self._ungroup(player)
        )
        await self._restore_step(
            player, "restore the volume", self._restore_volume_and_mute(player, snapshot)
        )
        await self._restore_step(
            player, "restore the grouping", self._restore_grouping(player, snapshot)
        )
        await self._restore_step(
            player, "restore playback", self._restore_power_and_playback(player, snapshot)
        )

    async def _restore_step(
        self, player: Player, what: str, step: Coroutine[Any, Any, None]
    ) -> None:
        """
        Run one restore step, logging rather than raising when it fails.

        :param player: The player being restored, for the log message.
        :param what: What the step was trying to do, for the log message.
        :param step: The step to run.
        """
        try:
            await step
        except Exception as err:
            # deliberately broad and per step: every step reaches into the player
            # provider, which is free to surface anything its client library raises,
            # and one failure may not strand the rest of the restore.
            # CancelledError is a BaseException and still propagates.
            self.logger.warning("Could not %s for player %s: %s", what, player.state.name, err)

    async def _zero_static_delay(self, sendspin: SendspinProvider, player: Player) -> None:
        """
        Take MA's static delay off a member, so it is measured from zero.

        A static delay advances a client, so a large one - a 250 ms factory default, say -
        moves the speaker's arrival outside the window a probe can place a chirp period
        in, and the reading folds to a plausible number that passes every check. What is
        left once the delay is gone is the speaker's own latency, which is small enough
        to stay inside that window. Folding the delay back in afterwards can not recover
        a folded reading, so this has to happen before anything is measured.

        Runs before the stream starts, which also keeps the timing shift the client makes
        on taking the new value out of the settling window a :meth:`solo` allows for.

        Does nothing for a member MA can not write a delay to: there is nothing to take
        off, and whatever its firmware applies is inside the arrival that gets measured.
        Nor for one that is already at zero, which also keeps the session from pinning a
        value into the config of a speaker that was still tracking its model default.

        :param sendspin: The Sendspin provider the delay is written through.
        :param player: The member to zero, as the user sees it.
        """
        protocol = resolve_sendspin_protocol(player)
        if protocol is None or not sendspin.supports_player_static_delay(protocol.protocol_id):
            return
        carrier_id = sendspin.get_player_static_delay_carrier(protocol.protocol_id)
        self._measured_carriers[player.player_id] = carrier_id
        delay_ms = sendspin.get_player_static_delay(protocol.protocol_id)
        if delay_ms == 0:
            return
        await sendspin.set_player_static_delay(protocol.protocol_id, 0)
        # recorded only once the write landed, so a failed one leaves nothing to undo
        self._zeroed_delays[player.player_id] = StaticDelaySnapshot(
            protocol.protocol_id, delay_ms, carrier_id
        )

    async def _restore_static_delay(self, player_id: str) -> None:
        """
        Give a member back the static delay this session took off it.

        The value goes back as an explicit one, so a member that was still tracking the
        default its client advertises is left carrying that default pinned.

        A no-op for a member that was never zeroed, or whose delay a correction has
        already superseded - see :meth:`keep_applied_static_delay`.
        """
        zeroed = self._zeroed_delays.get(player_id)
        if zeroed is None:
            return
        # resolved again rather than held from the start of the session: a session runs
        # for as long as it takes to walk a house, and the Sendspin provider that carries
        # the delay may have reloaded in the meantime
        sendspin = _require_sendspin(self.mass, self.translation_owner)
        # named by its carrier: a bridged speaker whose output another protocol took
        # over since gets the delay back for the carrier it came off, not for the new one
        await sendspin.set_player_static_delay(
            zeroed.sendspin_protocol_id, zeroed.delay_ms, carrier_id=zeroed.carrier_id
        )
        # dropped only once the value is back, so anything that raised above is still
        # owed and gets named by the sweep at the end of the restore
        del self._zeroed_delays[player_id]

    def _report_stranded_static_delays(self) -> None:
        """
        Warn about every static delay this session zeroed and could not put back.

        Zeroing is persisted in the player's config, so a speaker that has meanwhile gone
        stays at zero until something sets it again - and a user has no reason to suspect
        it. Naming the value it should carry is all a finished session can still do.
        """
        for player_id, zeroed in self._zeroed_delays.items():
            self.logger.warning(
                "Player %s is left with a static delay of 0 after calibration; "
                "set it back to %s ms by hand",
                player_id,
                zeroed.delay_ms,
            )

    async def _ungroup(self, player: Player) -> None:
        """Release a player from the calibration group."""
        await self._shared_session.remove_guest_listener(player.player_id)

    async def _restore_volume_and_mute(self, player: Player, snapshot: PlayerSnapshot) -> None:
        """
        Undo the silencing this session applied to a player, and nothing more.

        Only the control the session actually drove is put back, so a volume the
        user changed mid-session is not clobbered by a player that was merely muted.
        """
        method = self._silenced.pop(player.player_id, None)
        if method is None:
            return
        try:
            if method == SilenceMethod.VOLUME and snapshot.volume_level is not None:
                await self.mass.players.cmd_volume_set(player.player_id, snapshot.volume_level)
            elif method == SilenceMethod.MUTE:
                await self.mass.players.cmd_volume_mute(player.player_id, snapshot.volume_muted)
        except Exception:
            # keep the record so the eventual stop still tries to undo this, even
            # though the solo or restore that got here treats it as a failure
            self._silenced[player.player_id] = method
            raise

    async def _restore_grouping(self, player: Player, snapshot: PlayerSnapshot) -> None:
        """
        Rejoin a player to the sync leader or group player it belonged to.

        A group that can not take members back is restarted instead, which re-forms
        it from its own configuration.
        """
        previous_leader = snapshot.synced_to or snapshot.active_group
        if previous_leader is None:
            return
        leader = self.mass.players.get_player(previous_leader)
        if leader is not None and PlayerFeature.SET_MEMBERS not in leader.state.supported_features:
            if snapshot.active_group == previous_leader and snapshot.was_playing:
                await self.mass.players.cmd_play(previous_leader)
            return
        await self.mass.players.cmd_set_members(
            previous_leader, player_ids_to_add=[player.player_id]
        )

    async def _restore_power_and_playback(self, player: Player, snapshot: PlayerSnapshot) -> None:
        """
        Put a player's power state back, and resume what it was playing.

        Grouping powers a player on, so a speaker that was off when the session
        took it over is switched back off instead of resumed. A player that was a
        member of a group is left to the group it was rejoined to, which restarts
        it itself.
        """
        if not snapshot.powered:
            await self.mass.players.cmd_power(player.player_id, False)
            return
        if not snapshot.was_playing or snapshot.synced_to or snapshot.active_group:
            return
        # the player may have been on another plugin's source rather than its own
        # queue, so resume through the player controller with what it was playing
        await self.mass.players.cmd_resume(
            player.player_id, snapshot.active_source, snapshot.current_media
        )

    async def _close_shared_session(self) -> None:
        """Remove the hidden anchor player and its queue."""
        try:
            await self._shared_session.close()
        except Exception as err:
            # deliberately broad: the anchor lives in the Sendspin provider, whose
            # removal path can surface anything; a failure here may not propagate out
            # of a teardown. CancelledError still propagates.
            self.logger.warning("Could not remove the calibration anchor player: %s", err)

    def _release_stale_mute_lock(self, player: Player | None) -> None:
        """
        Drop the mute lock a session-applied mute left on an unreachable player.

        Muting a grouped player sets a lock that suppresses the auto-unmute when its
        volume changes later. The normal unmute clears it; a player that vanished
        mid-session never gets that far, and would stay silent in the next group the
        user puts it in for no visible reason.
        """
        if player is not None and self._silenced.pop(player.player_id, None) == SilenceMethod.MUTE:
            player.extra_data.pop(ATTR_MUTE_LOCK, None)

    async def _silence_member(self, player: Player) -> None:
        """
        Take a member out of the mix as the session claims it, or refuse to start at all.

        Unlike the silencing a :meth:`solo` does, a member that can not be silenced up
        front is fatal rather than merely logged: the probe attributes each arrival to
        the one speaker it believes is audible, so a member left in the mix corrupts the
        measurement of every other one instead of only its own.

        :param player: The member to silence.
        :raises ActionUnavailable: If the member could not be made inaudible.
        """
        await self._silence(player.player_id)
        if player.player_id not in self._silenced:
            raise ActionUnavailable(
                f"Player {player.state.name} could not be silenced for calibration",
                translation_key="player_not_silenced",
                translation_args=[player.state.name],
                translation_owner=self.translation_owner,
            )

    async def _silence(self, player_id: str) -> None:
        """
        Make a member inaudible, preferring its mute control over its volume.

        Whether a player is already down is decided from this session's own record,
        never from live player state: a volume command only lands once the client
        acknowledges it, so a live read can still show the previous solo's zero and
        would drop the player from the bookkeeping - after which it would rise back
        to full volume mid-measurement.
        """
        if player_id in self._silenced:
            return
        snapshot = self._snapshots[player_id]
        player = self.mass.players.get_player(player_id)
        if player is None or not player.state.available:
            return
        if player.state.mute_control != PLAYER_CONTROL_NONE:
            method = SilenceMethod.MUTE
        elif snapshot.volume_level is not None:
            # Ordinary Sendspin clients only advertise a mute control when they
            # implement PlayerCommand.MUTE; volume zero silences the rest without
            # interrupting the stream they are being fed.
            method = SilenceMethod.VOLUME
        else:
            self.logger.warning(
                "Can not take player %s out of the calibration mix: it has no usable control",
                player.state.name,
            )
            return
        try:
            if method == SilenceMethod.MUTE:
                await self.mass.players.cmd_volume_mute(player_id, True)
            else:
                await self.mass.players.cmd_volume_set(player_id, 0)
        except Exception as err:
            # deliberately broad: a failure to silence one speaker may not abort the
            # solo of the speaker being measured. CancelledError still propagates.
            self.logger.warning("Could not silence player %s: %s", player.state.name, err)
            return
        self._silenced[player_id] = method

    async def _unsilence(self, player_id: str) -> None:
        """
        Undo a :meth:`_silence` on the player that is about to be measured.

        Deliberately not guarded: a target that can not be made audible yields no
        measurement at all, so the solo must fail rather than report a speaker as
        isolated while it is still silent.
        """
        player = self.mass.players.get_player(player_id)
        if player is None:
            return
        await self._restore_volume_and_mute(player, self._snapshots[player_id])


def resolve_sendspin_protocol(player: Player) -> PlayerProtocol | None:
    """
    Return the Sendspin protocol the given player renders audio through.

    A player is the speaker as the user sees it, made up of the protocols that reach
    its device. Grouping, volume and mute are driven on the player, while a static
    delay is written to its Sendspin protocol. A session therefore holds the player and
    resolves it here when it needs the speaker's Sendspin side: the client of a
    Sendspin speaker, web or app player, or the bridge of a speaker that plays over
    Sendspin through AirPlay, squeezelite or Chromecast.

    :param player: The player to resolve.
    :return: The Sendspin protocol, or None when this player has no Sendspin output to
        render through - it plays over other protocols only, or its Sendspin client is
        gone or not ready to take a stream.
    """
    output = player.get_output_protocol_by_domain(SENDSPIN_DOMAIN)
    if output is None or not output.available:
        return None
    return player.get_output_protocol(output.output_protocol_id)


def is_eligible(mass: MusicAssistant, player: Player) -> bool:
    """
    Return whether the given player can take part in a calibration session.

    :param mass: MusicAssistant instance.
    :param player: The player to check.
    """
    if not player.state.available:
        return False
    if resolve_sendspin_protocol(player) is None:
        # only a speaker that renders over Sendspin can be given a Sendspin static delay
        return False
    if player.state.type not in CALIBRATABLE_PLAYER_TYPES:
        return False
    if is_remote_session_host(mass, player.player_id):
        # the hidden anchor of a session (this one or another plugin's) is not a speaker
        return False
    # Deliberately no check on whether the speaker accepts a static delay. Offsets are
    # relative, so dropping a speaker here would not merely deny it a correction - it
    # would take it out of the picture the other speakers are normalised against, and
    # the user could not even learn how far out it is. CalibrationPlayer.adjustable
    # carries that up to the client instead, so the warning still lands at pick time.

    # isolation drives mute where the client implements it and volume otherwise,
    # so a player with neither can never be taken out of the mix
    return (
        player.state.mute_control != PLAYER_CONTROL_NONE
        or player.state.volume_control != PLAYER_CONTROL_NONE
    )


def is_busy(player: Player) -> bool:
    """
    Return whether the given player is holding content of the user's own.

    :param player: The player to check.
    """
    return player.state.playback_state in BUSY_PLAYBACK_STATES


def resolve_static_delays(
    offsets_ms: Mapping[str, float],
    current_delays_ms: Mapping[str, int],
    translation_owner: str,
) -> dict[str, int]:
    """
    Normalise a session's measured arrival offsets into absolute static delays.

    A probe reports one offset per speaker, measured against a baseline it picked for
    itself, so only the differences between them carry meaning. A Sendspin static
    delay *advances* a player - the client subtracts it from the server timestamp, so
    a larger value makes the sound leave that speaker earlier - and the protocol
    carries no negative value. Equalising a group is therefore a matter of leaving the
    earliest speaker where it is and pulling every later one forward to meet it, so the
    whole group converges on the earliest arrival and that speaker is given zero.

    The delay each player already carries is part of the sum, which makes this converge
    rather than drift: handed a group's current delays, re-measuring it yields those same
    delays again instead of stacking a second correction on the first. A calibration
    session takes off every delay it can write before it measures, so those players come
    in at 0 and their correction falls out of the measurement alone.

    Refuses rather than clamps. A measurement implying a delay past the end of the
    supported range is not a speaker latency, and the largest plausible-looking value
    would bury the bad measurement instead of reporting it.

    :param offsets_ms: Measured arrival offset per player id, in milliseconds.
    :param current_delays_ms: The static delay MA currently applies to each player of
        the calibrated group - 0 for a player MA does not apply one to, whose own delay
        is already inside its measured offset. Its keys define the group, and
        ``offsets_ms`` must cover exactly the same players.
    :param translation_owner: Translation owner for the errors raised here.
    :raises InvalidDataError: If no measurements were given, if ``offsets_ms`` does not
        cover exactly the group, if a measurement is not a finite number, or if one
        implies a delay beyond the range Sendspin carries.
    :return: The absolute static delay to apply per player, the earliest arrival at 0.
    """
    if not offsets_ms:
        raise InvalidDataError(
            "No measurements were given to apply",
            translation_key="no_measurements",
            translation_owner=translation_owner,
        )
    _reject_uncovered_group(offsets_ms, current_delays_ms, translation_owner)
    # ahead of the minimum below, which a NaN would poison in a way that depends on
    # iteration order: min() returns NaN only when it comes first
    _reject_unusable_offsets(offsets_ms, translation_owner)
    totals_ms = {
        player_id: offset_ms + current_delays_ms[player_id]
        for player_id, offset_ms in offsets_ms.items()
    }
    earliest_ms = min(totals_ms.values())
    # subtracting the minimum puts the earliest speaker at exactly 0, which is
    # MIN_SENDSPIN_STATIC_DELAY, and no value can fall below it - so only the top of
    # the range can be exceeded
    delays_ms = {
        player_id: round(total_ms - earliest_ms) for player_id, total_ms in totals_ms.items()
    }
    for player_id, delay_ms in delays_ms.items():
        if delay_ms > MAX_SENDSPIN_STATIC_DELAY:
            raise InvalidDataError(
                f"Measurements put player {player_id} {delay_ms} ms behind the earliest speaker, "
                f"more than the {MAX_SENDSPIN_STATIC_DELAY} ms Sendspin can correct",
                translation_key="delay_out_of_range",
                translation_args=[player_id, delay_ms, MAX_SENDSPIN_STATIC_DELAY],
                translation_owner=translation_owner,
            )
    return delays_ms


def _require_sendspin(mass: MusicAssistant, translation_owner: str) -> SendspinProvider:
    """
    Return the Sendspin provider a session reads and writes static delays through.

    :param mass: MusicAssistant instance.
    :param translation_owner: Translation owner for the error raised here.
    :raises ActionUnavailable: If the Sendspin provider is not loaded.
    """
    sendspin = cast("SendspinProvider | None", mass.get_provider(SENDSPIN_DOMAIN))
    if sendspin is None:
        raise ActionUnavailable(
            "The Sendspin provider is not available",
            translation_key="sendspin_unavailable",
            translation_owner=translation_owner,
        )
    return sendspin


def _validate_player(mass: MusicAssistant, player_id: str, translation_owner: str) -> Player:
    """Return the player with the given id, if it can take part in a calibration session."""
    player = mass.players.get_player(player_id)
    if player is None or not player.state.available:
        raise PlayerUnavailableError(
            f"Player {player_id} is not available",
            translation_key="player_unavailable",
            translation_args=[player_id],
            translation_owner=translation_owner,
        )
    if not is_eligible(mass, player):
        raise UnsupportedFeaturedException(
            f"Player {player.state.name} can not take part in a calibration session",
            translation_key="player_not_eligible",
            translation_args=[player.state.name],
            translation_owner=translation_owner,
        )
    return player


def _reject_silent_players(players: list[Player], translation_owner: str) -> None:
    """
    Raise when any of the given players is already inaudible.

    A session refuses to claim a mute or a zero volume the user set themselves, so a
    speaker that starts out silent could be soloed but never actually heard.
    """
    silent = [p for p in players if p.state.volume_muted or p.state.volume_level == 0]
    if not silent:
        return
    names = ", ".join(p.state.name for p in silent)
    raise ActionUnavailable(
        f"{names} is muted or turned all the way down; turn it up before calibrating",
        translation_key="player_silent",
        translation_args=[names],
        translation_owner=translation_owner,
    )


def _reject_busy_players(players: list[Player], translation_owner: str) -> None:
    """Raise when any of the given players is busy with the user's own content."""
    busy = [p for p in players if is_busy(p)]
    if not busy:
        return
    names = ", ".join(p.state.name for p in busy)
    raise ActionUnavailable(
        f"{names} is currently playing; stop it first or start the session with force",
        translation_key="player_busy",
        translation_args=[names],
        translation_owner=translation_owner,
    )


def _reject_uncovered_group(
    offsets_ms: Mapping[str, float],
    current_delays_ms: Mapping[str, int],
    translation_owner: str,
) -> None:
    """
    Raise unless the measurements line up exactly with the calibrated group.

    Normalisation is only meaningful across the whole group: correcting a subset
    leaves the rest sitting against a different baseline, so a partly measured group
    would come out less aligned than it went in.
    """
    if unknown_ids := offsets_ms.keys() - current_delays_ms.keys():
        player_id = min(unknown_ids)
        raise InvalidDataError(
            f"Player {player_id} is not part of this calibration session",
            translation_key="player_not_in_session",
            translation_args=[player_id],
            translation_owner=translation_owner,
        )
    if unmeasured_ids := current_delays_ms.keys() - offsets_ms.keys():
        player_id = min(unmeasured_ids)
        raise InvalidDataError(
            f"Player {player_id} was not measured, so the group can not be equalised",
            translation_key="player_not_measured",
            translation_args=[player_id],
            translation_owner=translation_owner,
        )


def _reject_unusable_offsets(offsets_ms: Mapping[str, float], translation_owner: str) -> None:
    """Raise when any measured offset is not a finite number."""
    for player_id, offset_ms in offsets_ms.items():
        if not isfinite(offset_ms):
            raise InvalidDataError(
                f"Measured offset {offset_ms} for player {player_id} is not a finite number",
                translation_key="offset_not_finite",
                translation_args=[player_id],
                translation_owner=translation_owner,
            )


def _snapshot(player: Player) -> PlayerSnapshot:
    """Capture everything a session must put back on the given player."""
    return PlayerSnapshot(
        player_id=player.player_id,
        # a player without power control has no power state to restore, so it counts
        # as powered - otherwise the restore would switch it off for good
        powered=player.state.power_control == PLAYER_CONTROL_NONE or bool(player.state.powered),
        synced_to=player.state.synced_to,
        active_group=player.state.active_group,
        active_source=player.state.active_source,
        current_media=player.state.current_media,
        volume_level=player.state.volume_level,
        volume_muted=bool(player.state.volume_muted),
        was_playing=player.state.playback_state == PlaybackState.PLAYING,
    )
