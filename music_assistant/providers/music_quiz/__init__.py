"""
Music Quiz Plugin Provider for Music Assistant.

Provides the backend game engine for multiplayer music quiz games. Guests
join with a QR code on their own device and play the selected quiz type:
guess-the-song uses multiple-choice answers, while Music Timeline uses a shared
chronological timeline with optional artist and title bonuses. Trivia uses
AI-worded multiple-choice questions grounded in selected library metadata.

Playback is hosted by a SharedPlaybackSession in one of two modes selected for
each game:

- venue: a selected real player plays the rounds out loud; guests may
  optionally listen in on their own device when the player supports grouping.
- remote: a hidden virtual player leads the rounds and every guest listens
  on their own device (silent-disco style).

Game state changes are pushed to all connected clients as PROVIDER_EVENT
events with ``object_id`` set to this provider's instance_id. Event payload
contract (all payloads are JSON objects)::

    {"event": "game_updated", "state": {<public game state>}}
    {"event": "game_removed"}

The public game state is guest-safe by construction. Common state contains:

- always: ``phase`` (lobby/answering/reveal/finished), ``name``, ``quiz_type``,
  ``answer_type``, ``mode`` (venue/remote), ``round_count``, ``answer_duration``,
  ``include_similar_music`` and public player progress. ``auto_start_at`` contains
  the authoritative replay deadline while a lobby countdown is active. Private
  player IDs never appear in broadcasts. Trivia additionally exposes its canonical
  ``language`` and ``play_reveal_audio`` setting.
- answering rounds expose common timing and question fields plus a strategy
  fragment. Multiple-choice exposes opaque ``suggestions``. Timeline exposes
  the revealed shared ``timeline`` and redacted ``bonus_definitions``; the
  current song, year, correct placement and bonus answers remain protected.
- reveal/finished rounds additionally expose common ``answer_label``,
  ``track_uri``, ``image_url``, ``duration`` and ``ended_at`` fields. The
  answer strategy adds the revealed correct option or timeline entry and
  answer-specific player results. ``auto_advance_at`` contains the authoritative
  next-round deadline when the backend scheduled automatic advancement.

Guests authenticate through the standard guest access flow (join code in
the join URL) and register themselves as quiz player via ``music_quiz/join``,
which returns their private ``player_id``. That ID acts as the player's
credential for ``music_quiz/submit_answer``, ``music_quiz/ready``,
``music_quiz/heartbeat`` and ``music_quiz/state`` and must be kept client-side.
The compatibility ``music_quiz/answer`` command remains multiple-choice only.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Coroutine, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, cast

from music_assistant_models.auth import Scope
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import ConfigEntryType, PlayerType, ProviderFeature, QueueOption
from music_assistant_models.errors import (
    AudioError,
    InvalidDataError,
    MediaNotFoundError,
    SetupFailedError,
)
from music_assistant_models.media_items import Track

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    impersonated_user,
)
from music_assistant.helpers import guest_access
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.shared_playback import (
    SENDSPIN_DOMAIN,
    SharedPlaybackMode,
    SharedPlaybackSession,
)
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz.answer_types import get_answer_type
from music_assistant.providers.music_quiz.answer_types.base import (
    QuizAnswerSubmission,
    QuizAnswerSubmissionPayload,
    QuizAnswerType,
)
from music_assistant.providers.music_quiz.answer_types.multiple_choice import (
    MultipleChoiceSubmission,
)
from music_assistant.providers.music_quiz.errors import (
    TRANSLATION_OWNER,
    MusicQuizGameActiveError,
    MusicQuizGameFullError,
    MusicQuizInvalidAnswerError,
    MusicQuizNoGameError,
    MusicQuizNoPlaybackTargetError,
    MusicQuizUnknownPlayerError,
    MusicQuizWrongPhaseError,
)
from music_assistant.providers.music_quiz.game import (
    add_player,
    all_active_players_complete,
    are_active_players_ready,
    finish_game,
    get_current_round,
    mark_player_ready,
    reset_game,
    reveal_round,
    start_round,
)
from music_assistant.providers.music_quiz.game import (
    remove_player as remove_game_player,
)
from music_assistant.providers.music_quiz.game import (
    submit_answer as submit_game_answer,
)
from music_assistant.providers.music_quiz.models import (
    DEFAULT_TRIVIA_LANGUAGE,
    MusicQuizAnswerType,
    MusicQuizConfig,
    MusicQuizDifficulty,
    MusicQuizGame,
    MusicQuizPhase,
    MusicQuizPlaybackOptions,
    MusicQuizPlaybackSummary,
    MusicQuizPlayer,
    MusicQuizRound,
    MusicQuizSource,
    MusicQuizVenuePlayerOption,
    TimelineBonusMode,
)
from music_assistant.providers.music_quiz.quiz_types import (
    get_available_quiz_types,
    get_quiz_type,
)
from music_assistant.providers.music_quiz.quiz_types.base import (
    QuizType,
    is_supported_source,
)

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.models.player import Player

SUPPORTED_FEATURES: set[ProviderFeature] = set()

_ApiHandler = Callable[..., Coroutine[Any, Any, Any]]

CONF_MODE = "mode"
CONF_PLAYER = "player"
CONF_PLAYER_AUTO = "__auto__"
CONF_USE_AI_DISTRACTORS = "use_ai_distractors"

PLAYBACK_PREFERENCE_CACHE_KEY = "playback_preference"
PLAYBACK_PREFERENCE_CACHE_EXPIRATION = 86400 * 3650

MUSIC_QUIZ_GUEST_USER = "music_quiz_guest"
MUSIC_QUIZ_GUEST_DISPLAY_NAME = "Music Quiz Guest"

# defence-in-depth cap: a leaked join URL must not be able to flood a game
MAX_PLAYER_COUNT = 100
# the joined name is broadcast to every client on each state update; bound it
MAX_PLAYER_NAME_LENGTH = 40
PLAYER_RECONNECT_GRACE_SECONDS = 60.0
MAX_PLAYBACK_ATTEMPTS = 5
REPLAY_AUTO_START_SECONDS = 30

# minimum time players get to see the reveal/scoreboard before the game
# advances, even when the round track has (almost) finished playing
MIN_REVEAL_SECONDS = 10.0


class _PlaybackPreference(TypedDict):
    """Persisted playback preference for this provider instance."""

    playback_mode: str
    venue_player_id: str | None


@dataclass(frozen=True, slots=True)
class _PlaybackDefaults:
    """Resolved playback defaults and the venue preference they came from."""

    options: MusicQuizPlaybackOptions
    stored_venue_player_id: str | None


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MusicQuizPlugin(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: ID of an existing provider instance (None if new instance setup).
    :param action: Optional action key called from config entries UI.
    :param values: The (intermediate) raw values for config entries sent with the action.
    """
    ai_available = any(
        isinstance(provider, PluginProvider)
        for provider in mass.get_providers_supporting_feature(ProviderFeature.AI_QUERY)
    )
    ai_setting = ConfigEntry(
        key=CONF_USE_AI_DISTRACTORS,
        type=ConfigEntryType.BOOLEAN,
        required=False,
        default_value=False,
        read_only=not ai_available,
    )
    if ai_available:
        return (ai_setting,)
    return (
        ai_setting,
        ConfigEntry(
            key="ai_unavailable",
            type=ConfigEntryType.ALERT,
        ),
    )


class MusicQuizPlugin(PluginProvider):
    """Music Quiz plugin provider for Music Assistant."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize the Music Quiz plugin."""
        super().__init__(mass, manifest, config, supported_features)
        self._game: MusicQuizGame | None = None
        self._quiz_type: QuizType | None = None
        self._answer_type: QuizAnswerType | None = None
        self._game_lock = asyncio.Lock()
        self._game_generation = 0
        self._playback_session: SharedPlaybackSession | None = None
        self._playback_lock = asyncio.Lock()
        self._next_round_task: asyncio.Task[MusicQuizRound] | None = None
        self._reveal_playback_task: asyncio.Task[None] | None = None
        self._unregister_handles: list[Callable[[], None]] = []

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await self._migrate_legacy_playback_preference()
        host_commands: tuple[tuple[str, _ApiHandler], ...] = (
            ("music_quiz/available_quiz_types", self.available_quiz_types),
            ("music_quiz/playback_options", self.playback_options),
            ("music_quiz/create", self.create_game),
            ("music_quiz/get", self.get_game),
            ("music_quiz/start", self.start_game),
            ("music_quiz/reveal", self.reveal),
            ("music_quiz/next", self.next_round),
            ("music_quiz/reset", self.reset),
            ("music_quiz/delete", self.delete_game),
        )
        for command, handler in host_commands:
            self._unregister_handles.append(
                self.mass.register_api_command(command, handler, required_scope=Scope.USERS_INVITE)
            )
        # Participant game commands are available to any authenticated user.
        guest_commands: tuple[tuple[str, _ApiHandler], ...] = (
            ("music_quiz/info", self.get_game_info),
            ("music_quiz/join", self.join_game),
            ("music_quiz/state", self.get_player_state),
            ("music_quiz/public_state", self.get_public_state),
            ("music_quiz/heartbeat", self.heartbeat),
            ("music_quiz/submit_answer", self.submit_answer),
            ("music_quiz/answer", self.answer),
            ("music_quiz/ready", self.ready),
        )
        for command, handler in guest_commands:
            self._unregister_handles.append(self.mass.register_api_command(command, handler))
        # listen-in commands control playback on the guest's own device
        listen_in_commands: tuple[tuple[str, _ApiHandler], ...] = (
            ("music_quiz/listen_in", self.listen_in),
            ("music_quiz/stop_listen_in", self.stop_listen_in),
            ("music_quiz/can_listen_in", self.can_listen_in),
        )
        for command, handler in listen_in_commands:
            self._unregister_handles.append(
                self.mass.register_api_command(
                    command, handler, required_scope=Scope.PLAYERS_CONTROL
                )
            )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Call when the provider is being unloaded.

        :param is_removed: Whether the provider is being removed (vs just reloaded).
        """
        for unregister in self._unregister_handles:
            unregister()
        self._unregister_handles.clear()
        async with self._game_lock:
            quiz_type = self._quiz_type
            self._cancel_timers()
            self._cancel_next_round_task()
            await self._cancel_reveal_playback_task()
            if quiz_type is None or quiz_type.uses_audio:
                await self._stop_playback()
            self._game_generation += 1
            # clear game state before tearing down the session so a guest listen-in
            # racing with unload cannot (re)create or join a session mid-teardown
            self._game = None
            self._quiz_type = None
            self._answer_type = None
        await self._close_playback_session()
        if is_removed:
            await guest_access.revoke_guest_access(self.mass, MUSIC_QUIZ_GUEST_USER)
        await super().unload(is_removed)

    # ==================== Host API Commands ====================

    async def available_quiz_types(self) -> list[str]:
        """Return quiz types currently available for game creation."""
        return get_available_quiz_types(self.mass)

    async def playback_options(self) -> MusicQuizPlaybackOptions:
        """Return the host's available and recommended playback options."""
        return (await self._resolve_playback_defaults()).options

    async def create_game(  # noqa: PLR0913
        self,
        quiz_type: str = "guess_the_song",
        round_count: int = 5,
        suggestion_count: int = 4,
        answer_duration: int = 30,
        source_uris: list[str] | None = None,
        include_similar_music: bool = False,
        name: str | None = None,
        difficulty: str = MusicQuizDifficulty.NORMAL.value,
        language: str = DEFAULT_TRIVIA_LANGUAGE,
        play_reveal_audio: bool = True,
        artist_bonus_mode: str = TimelineBonusMode.OFF.value,
        title_bonus_mode: str = TimelineBonusMode.OFF.value,
        playback_mode: str | None = None,
        venue_player_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a new Music Quiz game, replacing a previous (finished) game.

        :param quiz_type: The quiz type to play (e.g. "guess_the_song").
        :param round_count: Number of rounds to play.
        :param suggestion_count: Number of answer suggestions per round.
        :param answer_duration: Answering duration in seconds.
        :param source_uris: Track, playlist, album, artist or genre URIs to draw rounds from.
        :param include_similar_music: Add bounded similar tracks to the selected source pool.
        :param name: Optional game name.
        :param difficulty: Guess-the-song difficulty ("easy", "normal" or "hard").
        :param language: Language tag for Trivia question content.
        :param play_reveal_audio: Play Trivia's grounded track during each reveal.
        :param artist_bonus_mode: Music Timeline artist bonus mode.
        :param title_bonus_mode: Music Timeline title bonus mode.
        :param playback_mode: Playback mode for this game ("venue" or "remote").
        :param venue_player_id: Venue player selected for this game.
        """
        quiz_type_class = get_quiz_type(quiz_type)
        get_answer_type(quiz_type_class.answer_type)
        try:
            parsed_artist_bonus_mode = TimelineBonusMode(artist_bonus_mode)
            parsed_title_bonus_mode = TimelineBonusMode(title_bonus_mode)
        except ValueError as err:
            raise InvalidDataError(
                "Unknown timeline bonus mode",
                translation_key="music_quiz_invalid_bonus_mode",
                translation_owner=TRANSLATION_OWNER,
            ) from err
        playback_defaults = await self._resolve_playback_defaults()
        effective_mode, effective_player_id, effective_player_name = self._resolve_create_playback(
            playback_mode,
            venue_player_id,
            playback_defaults.options,
        )
        game_config = quiz_type_class.normalize_config(
            MusicQuizConfig(
                round_count=round_count,
                suggestion_count=suggestion_count,
                answer_duration=answer_duration,
                source_uris=source_uris or [],
                include_similar_music=include_similar_music,
                name=_clean_game_name(name),
                playback_mode=effective_mode,
                venue_player_id=effective_player_id,
                venue_player_name=effective_player_name,
                difficulty=difficulty,
                use_ai_distractors=bool(self.config.get_value(CONF_USE_AI_DISTRACTORS)),
                language=language,
                play_reveal_audio=play_reveal_audio,
                artist_bonus_mode=parsed_artist_bonus_mode,
                title_bonus_mode=parsed_title_bonus_mode,
            )
        )
        quiz_type_class.validate_config(game_config)
        async with self._game_lock:
            if self._game is not None and self._game.phase in (
                MusicQuizPhase.ANSWERING,
                MusicQuizPhase.REVEAL,
            ):
                raise MusicQuizGameActiveError("A Music Quiz game is already in progress")
            game = MusicQuizGame(
                config=game_config,
                quiz_type=quiz_type,
                answer_type=quiz_type_class.answer_type,
                sources=await self._resolve_sources(game_config.source_uris),
                created_at=time.time(),
            )
            quiz_strategy, answer_strategy = self._resolve_game_strategies(
                game,
                recent_track_uris=self._recent_track_uris_for_game(self._game),
            )
            initial_round_task = await self._prepare_initial_round(quiz_strategy)
            selected_player = self._validate_playback_config(game_config)
            if selected_player is not None:
                game_config.venue_player_name = selected_player.display_name
            remembered_venue_player_id = (
                game_config.venue_player_id
                if game_config.playback_mode == SharedPlaybackMode.VENUE
                else playback_defaults.stored_venue_player_id
            )
            previous_game = self._game
            previous_quiz_type = self._quiz_type
            await self._cancel_reveal_playback_task()
            if previous_quiz_type is not None and previous_quiz_type.uses_audio:
                await self._stop_playback()
            async with self._playback_lock:
                if not quiz_strategy.uses_audio or (
                    previous_game is not None and _playback_selection_changed(previous_game, game)
                ):
                    await self._close_playback_session_locked()
                self._cancel_timers()
                self._cancel_next_round_task()
                self._game_generation += 1
                self._game = game
                self._quiz_type = quiz_strategy
                self._answer_type = answer_strategy
                self._next_round_task = initial_round_task
                self._signal_game_updated()
            await self._store_playback_preference(
                game_config.playback_mode,
                remembered_venue_player_id,
            )
            return await self._host_state()

    async def get_game(self) -> dict[str, Any] | None:
        """
        Return the host-visible state of the current game.

        :return: The current host state, or None when no game is active.
        """
        # take the game lock so the empty/active decision and snapshot cannot
        # tear against a lifecycle or state change while _host_state awaits the join URL
        async with self._game_lock:
            if self._game is None:
                return None
            return await self._host_state()

    async def start_game(self) -> dict[str, Any]:
        """Start the first round of the current game."""
        async with self._game_lock:
            await self._start_game_from_lobby()
            return await self._host_state()

    async def reveal(self) -> dict[str, Any]:
        """Reveal the current round and apply scoring."""
        async with self._game_lock:
            self._require_game()
            self._do_reveal()
            return await self._host_state()

    async def next_round(self) -> dict[str, Any]:
        """Advance to the next round or finish the game."""
        async with self._game_lock:
            game = self._require_game()
            if game.phase != MusicQuizPhase.REVEAL:
                raise MusicQuizWrongPhaseError("Next round can only start after reveal")
            await self._advance_from_reveal()
            return await self._host_state()

    async def reset(self, auto_start: bool = False) -> dict[str, Any]:
        """
        Reset the current game for a new run with the same settings and players.

        :param auto_start: Start a replay countdown when an active player remains.
        """
        async with self._game_lock:
            game = self._require_game()
            quiz_strategy, answer_strategy = self._resolve_game_strategies(
                game,
                recent_track_uris=self._recent_track_uris_for_game(game),
            )
            initial_round_task = await self._prepare_initial_round(quiz_strategy)
            self._cancel_timers()
            self._cancel_next_round_task()
            await self._cancel_reveal_playback_task()
            if quiz_strategy.uses_audio:
                await self._stop_playback()
            now = time.time()
            reset_game(game)
            self._game_generation += 1
            self._quiz_type = quiz_strategy
            self._answer_type = answer_strategy
            self._next_round_task = initial_round_task
            self._schedule_presence_expiry(now)
            if auto_start and _has_active_players(game, now):
                self._schedule_replay_auto_start(game, now)
            self._signal_game_updated()
            return await self._host_state()

    async def delete_game(self) -> None:
        """Delete the current game and stop its playback."""
        async with self._game_lock:
            self._require_game()
            uses_audio = self._quiz_type is None or self._quiz_type.uses_audio
            self._cancel_timers()
            self._cancel_next_round_task()
            await self._cancel_reveal_playback_task()
            self._game_generation += 1
            # clear game state before tearing down the session so a guest listen-in
            # racing with delete cannot (re)create or join a session mid-teardown
            self._game = None
            self._quiz_type = None
            self._answer_type = None
            if uses_audio:
                await self._stop_playback()
            # tear down the shared session so its virtual player / listen-in
            # guests do not linger once the game is gone
            await self._close_playback_session()
            self.signal_provider_event({"event": "game_removed"})

    # ==================== Guest API Commands ====================

    async def get_game_info(self) -> dict[str, Any] | None:
        """Return public metadata of the current game (e.g. for the join screen)."""
        async with self._game_lock:
            if (game := self._game) is None:
                return None
            return {
                "name": game.config.name,
                "quiz_type": game.quiz_type,
                "answer_type": game.answer_type.value,
                "phase": game.phase.value,
                "mode": game.config.playback_mode.value,
                "player_count": len(game.players),
                "round_count": game.config.round_count,
                "auto_start_at": game.auto_start_at,
                **get_quiz_type(game.quiz_type).serialize_game_config(game),
            }

    async def join_game(self, name: str) -> dict[str, Any]:
        """
        Join the current game as a player.

        :param name: Unique player display name.
        :return: The player's private player_id (their credential for further
            game commands) and their personalized game state.
        """
        async with self._game_lock:
            game, _, answer_type = self._require_game_strategies()
            player_name = name.strip()[:MAX_PLAYER_NAME_LENGTH]
            if not player_name:
                raise InvalidDataError(
                    "Player name is required",
                    translation_key="music_quiz_name_required",
                    translation_owner=TRANSLATION_OWNER,
                )
            if len(game.players) >= MAX_PLAYER_COUNT:
                raise MusicQuizGameFullError("Music Quiz game is full")
            joined_at = time.time()
            player = MusicQuizPlayer(
                player_id=secrets.token_urlsafe(24),
                name=player_name,
                joined_at=joined_at,
                active_from_round=_get_join_round(game),
                last_seen=joined_at,
            )
            add_player(game, player)
            self._schedule_presence_expiry(joined_at)
            self._signal_game_updated()
            return {
                "player_id": player.player_id,
                "state": _player_state(game, player, answer_type),
            }

    async def get_player_state(self, player_id: str) -> dict[str, Any]:
        """
        Return the personalized game state for a player (initial load/reconnect).

        :param player_id: The player's private player_id.
        """
        async with self._game_lock:
            game, _, answer_type = self._require_game_strategies()
            player = _get_player(game, player_id)
            self._refresh_player_presence(player)
            return _player_state(game, player, answer_type)

    async def get_public_state(self) -> dict[str, Any] | None:
        """
        Return the guest-safe public game state for a non-participant display.

        Mirrors the ``game_updated`` broadcast payload so a display client (e.g. a
        lobby dashboard) can render the full current state on cold launch without
        joining as a player.

        :return: The full public game state, or None when no game is active.
        """
        async with self._game_lock:
            if self._game is None:
                return None
            game, _, answer_type = self._require_game_strategies()
            return _public_state(game, answer_type)

    async def heartbeat(self, player_id: str) -> bool:
        """
        Refresh a player's reconnect grace period.

        :param player_id: The player's private player_id.
        :return: True when the player still exists, otherwise False.
        """
        async with self._game_lock:
            if self._game is None or (player := _find_player(self._game, player_id)) is None:
                return False
            self._refresh_player_presence(player)
            return True

    async def submit_answer(
        self,
        player_id: str,
        submission: QuizAnswerSubmissionPayload,
    ) -> dict[str, SerializableType]:
        """
        Submit a typed answer for the current round.

        :param player_id: The player's private player_id.
        :param submission: Discriminated answer submission.
        """
        async with self._game_lock:
            game, _, answer_type = self._require_game_strategies()
            submission_type = submission.get("answer_type")
            if not isinstance(submission_type, str):
                raise MusicQuizInvalidAnswerError(
                    "Answer submission requires an answer_type string"
                )
            submitted_answer_type = get_answer_type(submission_type)
            if submitted_answer_type.answer_type != game.answer_type:
                raise MusicQuizInvalidAnswerError("Submission answer type does not match the game")
            parsed_submission = answer_type.parse_submission(submission)
            player = _get_player(game, player_id)
            return self._submit_player_answer(game, player, parsed_submission, answer_type)

    async def answer(self, player_id: str, suggestion_id: str) -> dict[str, Any]:
        """
        Submit and lock a player's answer for the current round.

        :param player_id: The player's private player_id.
        :param suggestion_id: Selected suggestion ID.
        """
        async with self._game_lock:
            game, _, answer_type = self._require_game_strategies()
            if game.answer_type != MusicQuizAnswerType.MULTIPLE_CHOICE:
                raise MusicQuizInvalidAnswerError(
                    "The compatibility answer command requires multiple_choice"
                )
            player = _get_player(game, player_id)
            submission = MultipleChoiceSubmission(suggestion_id=suggestion_id)
            return self._submit_player_answer(game, player, submission, answer_type)

    async def ready(self, player_id: str) -> dict[str, Any]:
        """
        Mark a player ready for the next round during reveal.

        :param player_id: The player's private player_id.
        """
        async with self._game_lock:
            game, _, answer_type = self._require_game_strategies()
            player = _get_player(game, player_id)
            self._refresh_player_presence(player)
            # a repeat ready is a no-op: it cannot newly satisfy the all-ready
            # check, so return current state without re-broadcasting
            if game.phase != MusicQuizPhase.REVEAL or player.ready:
                return _player_state(game, player, answer_type)
            mark_player_ready(game, player.player_id)
            # advance early when every player is ready for the next round
            if are_active_players_ready(game):
                await self._advance_from_reveal()
            else:
                self._signal_game_updated()
            return _player_state(game, player, answer_type)

    async def listen_in(self, web_player_id: str) -> None:
        """
        Attach a guest's web player to the game audio.

        :param web_player_id: The player_id of the guest's web player.
        """
        # hold the playback lock across resolving and joining the session so a
        # guest can never be attached to a session that is being torn down
        async with self._playback_lock:
            self._require_game()
            session = await self._get_or_create_session_locked()
            if session is None:
                raise MusicQuizNoPlaybackTargetError("Listen-in is not available for this game")
            await session.add_guest_listener(web_player_id)

    async def stop_listen_in(self, web_player_id: str) -> None:
        """
        Detach a guest's web player from the game audio.

        :param web_player_id: The player_id of the guest's web player.
        """
        async with self._playback_lock:
            if self._playback_session is not None:
                await self._playback_session.remove_guest_listener(web_player_id)

    async def can_listen_in(self, web_player_id: str) -> bool:
        """
        Return whether the given guest web player can listen in on the game audio.

        :param web_player_id: The player_id of the guest's web player.
        """
        async with self._playback_lock:
            session = await self._get_or_create_session_locked()
            return session is not None and session.can_listen_in(web_player_id)

    # ==================== Internals ====================

    def _require_game(self) -> MusicQuizGame:
        """Return the current game or raise when there is none."""
        if self._game is None:
            raise MusicQuizNoGameError("There is no active Music Quiz game")
        return self._game

    def _resolve_game_strategies(
        self,
        game: MusicQuizGame,
        *,
        recent_track_uris: Iterable[str] = (),
    ) -> tuple[QuizType, QuizAnswerType]:
        """
        Resolve the strategies declared by game state.

        :param game: Game whose strategy identities should be resolved.
        :param recent_track_uris: Earlier game tracks to deprioritize.
        """
        quiz_type_class = get_quiz_type(game.quiz_type)
        answer_type_class = get_answer_type(game.answer_type)
        if quiz_type_class.answer_type != game.answer_type:
            raise InvalidDataError("Quiz type answer type does not match the game")
        quiz_type = quiz_type_class(self.mass, game.config)
        quiz_type.add_recent_track_uris(recent_track_uris)
        return quiz_type, answer_type_class()

    def _recent_track_uris_for_game(self, game: MusicQuizGame | None) -> set[str]:
        """Return source tracks represented by an earlier game."""
        if game is None:
            return set()
        quiz_type_class = get_quiz_type(game.quiz_type)
        quiz_type = self._quiz_type
        if quiz_type is None or type(quiz_type) is not quiz_type_class:
            quiz_type = quiz_type_class(self.mass, game.config)
        return quiz_type.get_recent_track_uris(game.rounds)

    def _require_game_strategies(
        self,
    ) -> tuple[MusicQuizGame, QuizType, QuizAnswerType]:
        """Return the game and matching cached strategies."""
        game = self._require_game()
        if self._quiz_type is None or self._answer_type is None:
            raise InvalidDataError("Music Quiz game strategies are unavailable")
        quiz_type_class = get_quiz_type(game.quiz_type)
        answer_type_class = get_answer_type(game.answer_type)
        if (
            quiz_type_class.answer_type != game.answer_type
            or type(self._quiz_type) is not quiz_type_class
            or type(self._answer_type) is not answer_type_class
        ):
            raise InvalidDataError("Music Quiz game strategy identity mismatch")
        return game, self._quiz_type, self._answer_type

    def _submit_player_answer(
        self,
        game: MusicQuizGame,
        player: MusicQuizPlayer,
        submission: QuizAnswerSubmission,
        answer_type: QuizAnswerType,
    ) -> dict[str, SerializableType]:
        """
        Apply a typed submission and return personalized state.

        :param game: Game receiving the submission.
        :param player: Player submitting the answer.
        :param submission: Validated answer submission.
        :param answer_type: Answer strategy for the game.
        """
        submitted_at = time.time()
        submit_game_answer(game, player.player_id, submission, submitted_at, answer_type)
        self._refresh_player_presence(player, submitted_at)
        if all_active_players_complete(game, answer_type):
            self._do_reveal(completed=True)
        else:
            self._signal_game_updated()
        return _player_state(game, player, answer_type)

    async def _host_state(self) -> dict[str, Any]:
        """Return the host-visible state of the current game."""
        game, _, answer_type = self._require_game_strategies()
        return {
            **_public_state(game, answer_type),
            "created_at": game.created_at,
            "sources": [source.to_dict() for source in game.sources],
            "join_url": await self._get_join_url(),
            "rounds": [_host_round(game_round, answer_type) for game_round in game.rounds],
            "playback": _playback_summary(game),
        }

    async def _get_join_url(self) -> str:
        """Return the guest join URL, creating the guest user and join code if needed."""
        guest_user = await guest_access.get_or_create_guest_user(
            self.mass, MUSIC_QUIZ_GUEST_USER, MUSIC_QUIZ_GUEST_DISPLAY_NAME
        )
        code = await guest_access.get_or_create_join_code(
            self.mass, guest_user, device_name="Music Quiz Guest"
        )
        return guest_access.build_join_url(self.mass, code)

    def _signal_game_updated(self) -> None:
        """Broadcast the public game state to all connected clients."""
        if self._game is None:
            return
        game, _, answer_type = self._require_game_strategies()
        self.signal_provider_event(
            {"event": "game_updated", "state": _public_state(game, answer_type)}
        )

    async def _resolve_sources(self, source_uris: list[str]) -> list[MusicQuizSource]:
        """Resolve configured source URIs into host-visible source metadata."""
        sources: list[MusicQuizSource] = []
        for source_uri in source_uris:
            try:
                source_media_type, provider_instance, item_id = await parse_uri(source_uri)
            except Exception as err:
                self.logger.warning("Ignoring invalid Music Quiz source %s: %s", source_uri, err)
                continue
            if not is_supported_source(source_media_type, provider_instance):
                self.logger.warning(
                    "Ignoring unsupported Music Quiz source %s (%s)",
                    source_uri,
                    source_media_type,
                )
                continue
            try:
                media_item = await self.mass.music.get_item(
                    media_type=source_media_type,
                    item_id=item_id,
                    provider_instance_id_or_domain=provider_instance,
                    allow_update_metadata=False,
                )
            except Exception as err:
                # the real failure otherwise only surfaces at round start,
                # minutes later and far from the cause
                self.logger.warning("Could not resolve Music Quiz source %s: %s", source_uri, err)
                sources.append(MusicQuizSource(uri=source_uri, name=source_uri))
                continue
            if not is_supported_source(media_item.media_type, media_item.provider):
                self.logger.warning(
                    "Ignoring unsupported Music Quiz source %s (%s)",
                    source_uri,
                    media_item.media_type,
                )
                continue
            sources.append(
                MusicQuizSource(
                    uri=source_uri,
                    name=media_item.name or source_uri,
                    media_type=media_item.media_type.value,
                )
            )
        return sources

    # ---------- round/phase progression (call with self._game_lock held) ----------

    async def _start_game_from_lobby(self, *, timer_owned: bool = False) -> None:
        """Start the first round from the lobby."""
        game = self._require_game()
        if game.phase != MusicQuizPhase.LOBBY:
            raise MusicQuizWrongPhaseError("The game has already started")
        self._cancel_replay_auto_start(cancel_task=not timer_owned)
        try:
            await self._start_next_round()
        except Exception:
            if self._game is game and game.phase == MusicQuizPhase.LOBBY:
                self._signal_game_updated()
            raise

    async def _start_next_round(self) -> None:
        """Prepare the next round, start its playback (if any) and open the answering phase."""
        with _system_auth_context():
            game, _, answer_type = self._require_game_strategies()
            round_index = len(game.rounds)
            next_round = await self._prepare_playable_round(round_index)
            started_at = time.time()
            start_round(game, next_round, started_at, answer_type)
            answer_window = _answer_window(game, next_round)
            self.mass.call_later(
                answer_window,
                self._on_answer_deadline,
                game,
                self._game_generation,
                round_index,
                started_at + answer_window,
                task_id=self._reveal_timer_id,
            )
            self._prefetch_round(round_index + 1)
            self._signal_game_updated()

    async def _prepare_playable_round(self, round_index: int) -> MusicQuizRound:
        """Return a prepared round after its audio starts successfully."""
        _, quiz_type, _ = self._require_game_strategies()
        if not quiz_type.plays_track_before_answering:
            return await self._get_prepared_round(round_index)
        rejected_uris: set[str] = set()
        last_error: AudioError | MediaNotFoundError | None = None
        for _attempt in range(MAX_PLAYBACK_ATTEMPTS):
            next_round = await self._get_prepared_round(round_index)
            track_uri = next_round.track_uri
            if track_uri is None:
                raise InvalidDataError("Prepared audio round is missing a track URI")
            if track_uri in rejected_uris:
                quiz_type.reject_track(track_uri)
                continue
            try:
                # This public queue operation is both the production resolution boundary and
                # the intended start of playback. A temporary QueueItem would bypass URI,
                # user/provider and target resolution performed by this path.
                await self._play_track(track_uri)
            except (AudioError, MediaNotFoundError) as err:
                rejected_uris.add(track_uri)
                last_error = err
                quiz_type.reject_track(track_uri)
                self.logger.warning(
                    "Could not play Music Quiz track %s; preparing a replacement: %s",
                    track_uri,
                    err,
                )
                continue
            return next_round
        raise MediaNotFoundError(
            f"No playable Music Quiz track found after {MAX_PLAYBACK_ATTEMPTS} attempts"
        ) from last_error

    def _do_reveal(self, *, completed: bool = False) -> None:
        """Reveal the current round, apply scoring and schedule the auto-advance."""
        game, quiz_type, answer_type = self._require_game_strategies()
        reveal_round(game, answer_type)
        self.mass.cancel_timer(self._reveal_timer_id)
        current_round = get_current_round(game)
        current_round.auto_advance_at = None
        now = time.time()
        advance_delay = quiz_type.completed_reveal_auto_advance_delay if completed else None
        if advance_delay is None:
            advance_delay = quiz_type.reveal_auto_advance_delay
        if advance_delay is None and current_round.duration and current_round.started_at:
            remaining = current_round.started_at + current_round.duration - now
            advance_delay = max(remaining, MIN_REVEAL_SECONDS)
        if advance_delay is not None:
            auto_advance_at = now + advance_delay
            self.mass.call_later(
                advance_delay,
                self._on_reveal_auto_advance,
                game,
                self._game_generation,
                current_round.round_index,
                auto_advance_at,
                task_id=self._advance_timer_id,
            )
            current_round.auto_advance_at = auto_advance_at
        self._signal_game_updated()
        if quiz_type.plays_track_on_reveal:
            if current_round.track_uri is None:
                self.logger.warning("Prepared Music Quiz reveal round is missing a track URI")
            else:
                self._start_reveal_playback(
                    game,
                    self._game_generation,
                    current_round.round_index,
                    current_round.track_uri,
                )

    async def _advance_from_reveal(self) -> None:
        """Advance a revealed game to the next round or finish it."""
        game, quiz_type, _ = self._require_game_strategies()
        get_current_round(game).auto_advance_at = None
        self.mass.cancel_timer(self._advance_timer_id)
        if quiz_type.plays_track_on_reveal:
            await self._cancel_reveal_playback_task()
            await self._stop_playback()
        if len(game.rounds) >= game.config.round_count:
            if quiz_type.uses_audio and not quiz_type.plays_track_on_reveal:
                await self._stop_playback()
            finish_game(game)
            self._cancel_presence_expiry()
            self._signal_game_updated()
            return
        await self._start_next_round()

    async def _expire_inactive_players(self) -> None:
        """Remove players whose reconnect grace period elapsed."""
        async with self._game_lock:
            if self._game is None or self._game.phase == MusicQuizPhase.FINISHED:
                self._cancel_presence_expiry()
                return
            game, _, answer_type = self._require_game_strategies()
            now = time.time()
            expired_player_ids = [
                player.player_id
                for player in game.players.values()
                if not _is_player_active(player, now)
            ]
            if not expired_player_ids:
                self._schedule_presence_expiry(now)
                return

            for player_id in expired_player_ids:
                remove_game_player(game, player_id, answer_type)

            if (
                game.phase == MusicQuizPhase.LOBBY
                and game.auto_start_at is not None
                and not _has_active_players(game, now)
            ):
                self._cancel_replay_auto_start(cancel_task=True)

            if game.players and game.phase == MusicQuizPhase.ANSWERING:
                if all_active_players_complete(game, answer_type):
                    self._do_reveal(completed=True)
                else:
                    self._signal_game_updated()
            elif game.players and game.phase == MusicQuizPhase.REVEAL:
                if are_active_players_ready(game):
                    await self._advance_from_reveal()
                else:
                    self._signal_game_updated()
            else:
                self._signal_game_updated()
            self._schedule_presence_expiry()

    async def _on_replay_auto_start(
        self,
        game: MusicQuizGame,
        generation: int,
        auto_start_at: float,
    ) -> None:
        """
        Start a replay whose authoritative countdown reached its deadline.

        :param game: Game for which the countdown was scheduled.
        :param generation: Lifecycle generation for which the countdown was scheduled.
        :param auto_start_at: Authoritative deadline for this countdown.
        """
        with _system_auth_context():
            async with self._game_lock:
                if (
                    self._game is not game
                    or self._game_generation != generation
                    or game.phase != MusicQuizPhase.LOBBY
                    or game.auto_start_at != auto_start_at
                ):
                    return
                if not _has_active_players(game, time.time()):
                    self._cancel_replay_auto_start(cancel_task=False)
                    self._signal_game_updated()
                    return
                try:
                    await self._start_game_from_lobby(timer_owned=True)
                except Exception as err:
                    self.logger.error(
                        "Could not automatically start Music Quiz replay: %s",
                        err,
                        exc_info=err,
                    )

    async def _on_answer_deadline(
        self,
        game: MusicQuizGame,
        generation: int,
        round_index: int,
        answer_deadline: float,
    ) -> None:
        """
        Reveal an answering round whose authoritative deadline was reached.

        :param game: Game for which the deadline was scheduled.
        :param generation: Lifecycle generation for which the deadline was scheduled.
        :param round_index: Answering round index.
        :param answer_deadline: Authoritative deadline for this answer window.
        """
        async with self._game_lock:
            if (
                self._game is not game
                or self._game_generation != generation
                or not self._is_current_round(round_index, MusicQuizPhase.ANSWERING)
            ):
                return
            current_round = get_current_round(game)
            if (
                current_round.started_at is None
                or current_round.started_at + _answer_window(game, current_round) != answer_deadline
            ):
                return
            self._do_reveal()

    async def _on_reveal_auto_advance(
        self,
        game: MusicQuizGame,
        generation: int,
        round_index: int,
        auto_advance_at: float,
    ) -> None:
        """
        Advance a reveal whose authoritative deadline was reached.

        :param game: Game for which advancement was scheduled.
        :param generation: Lifecycle generation for which advancement was scheduled.
        :param round_index: Revealed round index.
        :param auto_advance_at: Authoritative deadline for this reveal.
        """
        async with self._game_lock:
            if (
                self._game is not game
                or self._game_generation != generation
                or not self._is_current_round(round_index, MusicQuizPhase.REVEAL)
                or get_current_round(game).auto_advance_at != auto_advance_at
            ):
                return
            try:
                await self._advance_from_reveal()
            except Exception as err:
                # leave the game in reveal so the host or players can retry manually
                self.logger.error("Could not advance Music Quiz game: %s", err, exc_info=err)
                if (
                    self._game is game
                    and self._game_generation == generation
                    and self._is_current_round(round_index, MusicQuizPhase.REVEAL)
                ):
                    get_current_round(game).auto_advance_at = None
                    self._signal_game_updated()

    def _is_current_round(self, round_index: int, phase: MusicQuizPhase) -> bool:
        """Return whether the game is still in the given round and phase."""
        return (
            self._game is not None
            and self._game.phase == phase
            and self._game.current_round_index == round_index
        )

    # ---------- round preparation ----------

    async def _prepare_initial_round(
        self,
        quiz_type: QuizType,
    ) -> asyncio.Task[MusicQuizRound]:
        """Initialize a quiz type and complete its first round before opening the lobby."""
        with _system_auth_context():
            await quiz_type.initialize()
            task = self.mass.create_task(self._prepare_round(quiz_type, 0, []))
            await task
        return task

    async def _prepare_round(
        self,
        quiz_type: QuizType,
        round_index: int,
        previous_rounds: list[MusicQuizRound],
    ) -> MusicQuizRound:
        """Prepare a round and its required assets."""
        game_round = await quiz_type.prepare_round(round_index, previous_rounds)
        if game_round.track_uri and quiz_type.prefetch_lyrics:
            await self._fetch_lyrics(game_round.track_uri)
        return game_round

    def _prefetch_round(self, round_index: int) -> None:
        """Prepare an upcoming round in the background."""
        self._cancel_next_round_task()
        if self._game is None or round_index >= self._game.config.round_count:
            return
        game, quiz_type, _ = self._require_game_strategies()
        with _system_auth_context():
            self._next_round_task = self.mass.create_task(
                self._prepare_round(quiz_type, round_index, list(game.rounds))
            )

    async def _get_prepared_round(self, round_index: int) -> MusicQuizRound:
        """Return the (prefetched) round with the given index."""
        game, quiz_type, _ = self._require_game_strategies()
        task = self._next_round_task
        self._next_round_task = None
        if task is not None:
            try:
                prepared = await task
                if prepared.round_index == round_index:
                    return prepared
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is None or current_task.cancelling():
                    raise
            except Exception as err:
                self.logger.warning(
                    "Prefetched Music Quiz round failed, preparing a fresh one: %s", err
                )
        with _system_auth_context():
            return await self._prepare_round(quiz_type, round_index, list(game.rounds))

    def _cancel_next_round_task(self) -> None:
        """Cancel a pending round prefetch task."""
        if self._next_round_task is not None:
            self._next_round_task.cancel()
            # retrieve the result/exception once the task settles so a prefetch that
            # already failed is not reported by asyncio as an unhandled exception
            self._next_round_task.add_done_callback(_consume_task_exception)
            self._next_round_task = None

    def _start_reveal_playback(
        self,
        game: MusicQuizGame,
        generation: int,
        round_index: int,
        track_uri: str,
    ) -> None:
        """
        Start best-effort reveal playback for the current Trivia round.

        :param game: Game owning the revealed round.
        :param generation: Lifecycle generation owning the playback.
        :param round_index: Revealed round index.
        :param track_uri: Grounded track to play.
        """
        if self._reveal_playback_task is not None:
            self._reveal_playback_task.cancel()
            self._reveal_playback_task.add_done_callback(_consume_task_exception)
            self._reveal_playback_task = None
        playback = self._play_reveal_track(game, generation, round_index, track_uri)
        try:
            with _system_auth_context():
                self._reveal_playback_task = self.mass.create_task(
                    playback,
                    eager_start=False,
                )
        except Exception as err:
            playback.close()
            self.logger.warning(
                "Could not start Music Quiz reveal playback for %s: %s",
                track_uri,
                err,
            )

    async def _play_reveal_track(
        self,
        game: MusicQuizGame,
        generation: int,
        round_index: int,
        track_uri: str,
    ) -> None:
        """
        Play a track while its owning Trivia reveal remains current.

        :param game: Game owning the revealed round.
        :param generation: Lifecycle generation owning the playback.
        :param round_index: Revealed round index.
        :param track_uri: Grounded track to play.
        """
        with _system_auth_context():
            if not self._is_reveal_playback_current(game, generation, round_index):
                return
            try:
                await self._play_track(track_uri)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.warning(
                    "Could not play Music Quiz reveal track %s: %s",
                    track_uri,
                    err,
                )
                return
            if not self._is_reveal_playback_current(game, generation, round_index):
                await self._stop_playback()

    async def _cancel_reveal_playback_task(self) -> None:
        """Cancel and await the provider-owned reveal playback task, if any."""
        task = self._reveal_playback_task
        self._reveal_playback_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def _is_reveal_playback_current(
        self,
        game: MusicQuizGame,
        generation: int,
        round_index: int,
    ) -> bool:
        """
        Return whether reveal playback still belongs to the active round.

        :param game: Game owning the playback.
        :param generation: Lifecycle generation owning the playback.
        :param round_index: Revealed round index.
        """
        return (
            self._game is game
            and self._game_generation == generation
            and game.phase == MusicQuizPhase.REVEAL
            and game.current_round_index == round_index
        )

    async def _fetch_lyrics(self, track_uri: str) -> None:
        """Fetch lyrics for a prepared round."""
        try:
            track = await self.mass.music.get_item_by_uri(track_uri)
            if isinstance(track, Track):
                await self.mass.metadata.get_track_lyrics(track)
        except Exception as err:
            self.logger.debug("Lyrics prefetch failed for %s: %s", track_uri, err)

    # ---------- playback ----------

    async def _play_track(self, track_uri: str) -> None:
        """Play the given track on the game's playback session."""
        with _system_auth_context():
            session = await self._get_playback_session()
            if session is None:
                raise MusicQuizNoPlaybackTargetError(
                    "No playback target is available for the Music Quiz game"
                )
            player = self.mass.players.get_player(session.player_id)
            if player is None or not player.state.available:
                raise MusicQuizNoPlaybackTargetError(
                    "No playback target is available for the Music Quiz game"
                )
            if player.extra_data.get(ATTR_ANNOUNCEMENT_IN_PROGRESS):
                raise MusicQuizNoPlaybackTargetError(
                    "The Music Quiz playback target is handling an announcement"
                )
            async with self._playback_lock:
                if self._playback_session is not session:
                    raise MusicQuizNoPlaybackTargetError(
                        "No playback target is available for the Music Quiz game"
                    )
                await session.restore_guest_listeners()
            await self.mass.player_queues.play_media(
                session.queue_id, track_uri, option=QueueOption.REPLACE
            )

    async def _stop_playback(self) -> None:
        """Stop playback on the game's playback session, if any."""
        with _system_auth_context():
            if self._playback_session is None:
                return
            if self.mass.players.get_player(self._playback_session.player_id) is None:
                return
            try:
                await self.mass.player_queues.stop(self._playback_session.queue_id)
            except Exception as err:
                self.logger.warning("Could not stop Music Quiz playback: %s", err)

    async def _close_playback_session(self) -> None:
        """Close and drop the shared playback session under the playback lock."""
        # use the same lock that guards session creation/refresh so a concurrent
        # _get_playback_session() cannot resurrect a session we are tearing down
        async with self._playback_lock:
            await self._close_playback_session_locked()

    async def _close_playback_session_locked(self) -> None:
        """Close and drop the shared playback session while holding the playback lock."""
        if self._playback_session is not None:
            await self._playback_session.close()
            self._playback_session = None

    async def _get_playback_session(self) -> SharedPlaybackSession | None:
        """
        Get the shared playback session for the quiz, creating it if needed.

        In remote mode the session is backed by a hidden virtual player; the
        session is (re)created here when that player does not exist (e.g.
        after a Sendspin provider reload). In venue mode a session only exists
        while the player selected for the game remains eligible.

        :return: The session, or None when no session is available.
        """
        async with self._playback_lock:
            return await self._get_or_create_session_locked()

    async def _get_or_create_session_locked(self) -> SharedPlaybackSession | None:
        """
        Get or (re)create the shared playback session.

        The caller must hold ``_playback_lock``; guest listen-in and session
        teardown share the lock so a session is never created or joined while it
        is being closed.

        :return: The session, or None when no game is active or none is available.
        """
        # a session only makes sense while a game is active; without one, never
        # (re)create it - this also stops a guest listen-in that races with game
        # teardown from leaking a fresh session / virtual player
        if (game := self._game) is None:
            return None
        if self._quiz_type is not None and not self._quiz_type.uses_audio:
            return None
        if self._quiz_type is None:
            quiz_type = get_quiz_type(game.quiz_type)(self.mass, game.config)
            if not quiz_type.uses_audio:
                return None
        # drop a stale session whose player no longer exists
        if self._playback_session is not None and (
            self.mass.players.get_player(self._playback_session.player_id) is None
        ):
            await self._playback_session.close()
            self._playback_session = None

        if (
            self._playback_session is not None
            and game.config.playback_mode == SharedPlaybackMode.VENUE
            and (
                self._playback_session.player_id != game.config.venue_player_id
                or not self._is_eligible_venue_player_id(self._playback_session.player_id)
            )
        ):
            await self._playback_session.close()
            self._playback_session = None

        if self._playback_session is not None:
            return self._playback_session

        if game.config.playback_mode == SharedPlaybackMode.REMOTE:
            if not self._remote_playback_available():
                return None
            try:
                self._playback_session = await self._create_remote_playback_session()
            except SetupFailedError as err:
                self.logger.warning("Unable to create remote quiz session: %s", err)
        elif (player_id := game.config.venue_player_id) and self._is_eligible_venue_player_id(
            player_id
        ):
            try:
                self._playback_session = await SharedPlaybackSession.create_venue(
                    self.mass, player_id
                )
            except SetupFailedError as err:
                self.logger.warning("Unable to create venue quiz session: %s", err)
        return self._playback_session

    async def _create_remote_playback_session(self) -> SharedPlaybackSession:
        """Create the remote playback session for the active game."""
        game_name = self._game.config.name if self._game else None
        return await SharedPlaybackSession.create_remote(
            self.mass,
            owner_instance_id=self.instance_id,
            display_name=game_name or "Music Quiz",
            session_id=self.instance_id,
        )

    async def _resolve_playback_defaults(self) -> _PlaybackDefaults:
        """Resolve current playback availability and the recommended defaults."""
        preference = await self._load_playback_preference()
        legacy_mode, legacy_player_id, _ = self._legacy_playback_preference()
        eligible_players = self._eligible_venue_players()
        venue_players: list[MusicQuizVenuePlayerOption] = [
            {"player_id": player.player_id, "name": player.display_name}
            for player in eligible_players
        ]
        eligible_player_ids = {player["player_id"] for player in venue_players}
        stored_venue_player_id = (
            preference["venue_player_id"] if preference is not None else legacy_player_id
        )
        default_venue_player_id = next(
            (
                player_id
                for player_id in (
                    preference["venue_player_id"] if preference is not None else None,
                    legacy_player_id,
                )
                if player_id in eligible_player_ids
            ),
            venue_players[0]["player_id"] if venue_players else None,
        )
        venue_available = bool(venue_players)
        remote_available = self._remote_playback_available()
        preferred_mode = (
            SharedPlaybackMode(preference["playback_mode"])
            if preference is not None
            else legacy_mode
        )
        if preferred_mode == SharedPlaybackMode.VENUE and venue_available:
            default_mode = SharedPlaybackMode.VENUE
        elif preferred_mode == SharedPlaybackMode.REMOTE and remote_available:
            default_mode = SharedPlaybackMode.REMOTE
        elif venue_available:
            default_mode = SharedPlaybackMode.VENUE
        elif remote_available:
            default_mode = SharedPlaybackMode.REMOTE
        else:
            default_mode = preferred_mode
        return _PlaybackDefaults(
            options={
                "default_playback_mode": default_mode.value,
                "default_venue_player_id": default_venue_player_id,
                "venue_available": venue_available,
                "remote_available": remote_available,
                "venue_players": venue_players,
            },
            stored_venue_player_id=stored_venue_player_id,
        )

    def _resolve_create_playback(
        self,
        playback_mode: str | None,
        venue_player_id: str | None,
        options: MusicQuizPlaybackOptions,
    ) -> tuple[SharedPlaybackMode, str | None, str | None]:
        """
        Resolve and validate playback requested for a new game.

        :param playback_mode: Requested playback mode, or None for the recommended default.
        :param venue_player_id: Requested venue player.
        :param options: Current playback options.
        :return: Effective mode, venue player ID and venue player name.
        """
        if playback_mode is None:
            mode = SharedPlaybackMode(options["default_playback_mode"])
        else:
            try:
                mode = SharedPlaybackMode(playback_mode)
            except ValueError as err:
                raise InvalidDataError(
                    f"Unknown Music Quiz playback mode: {playback_mode}",
                    translation_key="music_quiz_invalid_playback_mode",
                    translation_owner=TRANSLATION_OWNER,
                ) from err
        if mode == SharedPlaybackMode.REMOTE:
            if not options["remote_available"]:
                raise MusicQuizNoPlaybackTargetError("Remote Music Quiz playback is not available")
            return mode, None, None

        selected_player_id = venue_player_id
        if selected_player_id is None and playback_mode is None:
            selected_player_id = options["default_venue_player_id"]
        for player in options["venue_players"]:
            if player["player_id"] == selected_player_id:
                return mode, player["player_id"], player["name"]
        raise MusicQuizNoPlaybackTargetError(
            "The selected Music Quiz venue player is not available"
        )

    def _validate_playback_config(self, config: MusicQuizConfig) -> Player | None:
        """
        Validate a game's playback target immediately before publication.

        :param config: Game configuration to validate.
        :return: The selected venue player, or None for remote playback.
        """
        if config.playback_mode == SharedPlaybackMode.REMOTE:
            if not self._remote_playback_available():
                raise MusicQuizNoPlaybackTargetError("Remote Music Quiz playback is not available")
            return None
        if config.venue_player_id:
            for player in self._eligible_venue_players():
                if player.player_id == config.venue_player_id:
                    return player
        raise MusicQuizNoPlaybackTargetError(
            "The selected Music Quiz venue player is not available"
        )

    def _eligible_venue_players(self) -> list[Player]:
        """Return stable, visible and playable venue targets."""
        eligible_players = [
            player
            for player in self.mass.players.all_players(False, False)
            if self._is_eligible_venue_player(player)
        ]
        return sorted(
            eligible_players,
            key=lambda player: (player.display_name.casefold(), player.player_id),
        )

    def _is_eligible_venue_player_id(self, player_id: str) -> bool:
        """
        Return whether a player remains eligible for venue playback.

        :param player_id: Player to validate.
        """
        player = self.mass.players.get_player(player_id)
        return player is not None and self._is_eligible_venue_player(player)

    @staticmethod
    def _is_eligible_venue_player(player: Player) -> bool:
        """
        Return whether a player is a real venue playback target.

        :param player: Player to inspect.
        """
        state = player.state
        return (
            state.available
            and state.enabled
            and not state.hide_in_ui
            and not state.needs_setup
            and state.synced_to is None
            and state.active_group is None
            and state.type in (PlayerType.PLAYER, PlayerType.STEREO_PAIR, PlayerType.GROUP)
            and player.is_native_player
            and player.provider.domain != SENDSPIN_DOMAIN
        )

    def _remote_playback_available(self) -> bool:
        """Return whether remote playback can create its Sendspin session."""
        return self.mass.get_provider(SENDSPIN_DOMAIN) is not None

    async def _load_playback_preference(self) -> _PlaybackPreference | None:
        """Return the valid persisted playback preference for this provider instance."""
        data = await self.mass.cache.get(
            PLAYBACK_PREFERENCE_CACHE_KEY,
            provider=self.instance_id,
        )
        if data is None:
            return None
        if not isinstance(data, dict):
            self.logger.warning("Ignoring invalid Music Quiz playback preference")
            return None
        playback_mode = data.get("playback_mode")
        venue_player_id = data.get("venue_player_id")
        valid_venue_player_id = venue_player_id is None or (
            isinstance(venue_player_id, str) and bool(venue_player_id)
        )
        if (
            playback_mode not in {mode.value for mode in SharedPlaybackMode}
            or not valid_venue_player_id
        ):
            self.logger.warning("Ignoring invalid Music Quiz playback preference")
            return None
        return {
            "playback_mode": cast("str", playback_mode),
            "venue_player_id": venue_player_id,
        }

    async def _store_playback_preference(
        self,
        playback_mode: SharedPlaybackMode,
        venue_player_id: str | None,
    ) -> None:
        """
        Best-effort persist a successful playback selection for this provider instance.

        :param playback_mode: Effective mode selected for the game.
        :param venue_player_id: Last successful venue player, if any.
        """
        try:
            await self.mass.cache.set(
                PLAYBACK_PREFERENCE_CACHE_KEY,
                {
                    "playback_mode": playback_mode.value,
                    "venue_player_id": venue_player_id,
                },
                expiration=PLAYBACK_PREFERENCE_CACHE_EXPIRATION,
                provider=self.instance_id,
                persistent=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.logger.warning("Could not persist Music Quiz playback preference: %s", err)

    async def _migrate_legacy_playback_preference(self) -> None:
        """Persist legacy provider playback values when no preference exists yet."""
        if await self._load_playback_preference() is not None:
            return
        playback_mode, venue_player_id, has_legacy_value = self._legacy_playback_preference()
        if has_legacy_value:
            await self._store_playback_preference(playback_mode, venue_player_id)

    def _legacy_playback_preference(self) -> tuple[SharedPlaybackMode, str | None, bool]:
        """Return compatible playback defaults from legacy provider settings."""
        raw_mode = self.config.get_value(CONF_MODE)
        raw_player_id = self.config.get_value(CONF_PLAYER)
        if isinstance(raw_mode, str):
            try:
                playback_mode = SharedPlaybackMode(raw_mode)
            except ValueError:
                playback_mode = SharedPlaybackMode.VENUE
        else:
            playback_mode = SharedPlaybackMode.VENUE
        venue_player_id = (
            raw_player_id
            if isinstance(raw_player_id, str)
            and raw_player_id
            and raw_player_id != CONF_PLAYER_AUTO
            else None
        )
        return playback_mode, venue_player_id, raw_mode is not None or raw_player_id is not None

    # ---------- timers ----------

    def _schedule_replay_auto_start(self, game: MusicQuizGame, now: float) -> None:
        """
        Schedule automatic replay startup for a lobby.

        :param game: Lobby game to start.
        :param now: Current server timestamp.
        """
        auto_start_at = now + REPLAY_AUTO_START_SECONDS
        self.mass.call_later(
            REPLAY_AUTO_START_SECONDS,
            self._on_replay_auto_start,
            game,
            self._game_generation,
            auto_start_at,
            task_id=self._replay_auto_start_timer_id,
        )
        game.auto_start_at = auto_start_at

    def _cancel_replay_auto_start(self, *, cancel_task: bool) -> None:
        """
        Cancel and clear automatic replay startup.

        :param cancel_task: Also cancel a callback that already started.
        """
        had_countdown = self._game is not None and self._game.auto_start_at is not None
        self.mass.cancel_timer(self._replay_auto_start_timer_id)
        if cancel_task and had_countdown:
            self.mass.cancel_task(self._replay_auto_start_timer_id)
        if self._game is not None:
            self._game.auto_start_at = None

    def _refresh_player_presence(
        self,
        player: MusicQuizPlayer,
        seen_at: float | None = None,
    ) -> None:
        """
        Refresh a player's reconnect grace period.

        :param player: Player whose presence should be refreshed.
        :param seen_at: Server timestamp of the activity.
        """
        player.last_seen = seen_at if seen_at is not None else time.time()
        self._schedule_presence_expiry(player.last_seen)

    def _schedule_presence_expiry(self, now: float | None = None) -> None:
        """
        Schedule the next inactive-player expiry.

        :param now: Current server timestamp.
        """
        if (
            self._game is None
            or self._game.phase == MusicQuizPhase.FINISHED
            or not self._game.players
        ):
            self.mass.cancel_timer(self._presence_timer_id)
            return
        current_time = now if now is not None else time.time()
        expires_at = min(
            player.last_seen + PLAYER_RECONNECT_GRACE_SECONDS
            for player in self._game.players.values()
        )
        self.mass.call_later(
            max(expires_at - current_time, 0),
            self._expire_inactive_players,
            task_id=self._presence_timer_id,
        )

    def _cancel_presence_expiry(self, *, cancel_task: bool = False) -> None:
        """
        Cancel scheduled player expiry work.

        :param cancel_task: Also cancel an expiry callback that already started.
        """
        self.mass.cancel_timer(self._presence_timer_id)
        if cancel_task:
            self.mass.cancel_task(self._presence_timer_id)

    @property
    def _reveal_timer_id(self) -> str:
        """Return the task_id of the answering deadline timer."""
        return f"music_quiz_reveal_{self.instance_id}"

    @property
    def _advance_timer_id(self) -> str:
        """Return the task_id of the reveal auto-advance timer."""
        return f"music_quiz_advance_{self.instance_id}"

    @property
    def _presence_timer_id(self) -> str:
        """Return the task_id of the player presence timer."""
        return f"music_quiz_presence_{self.instance_id}"

    @property
    def _replay_auto_start_timer_id(self) -> str:
        """Return the task_id of the replay auto-start timer."""
        return f"music_quiz_replay_{self.instance_id}"

    def _cancel_timers(self) -> None:
        """Cancel all scheduled game timers."""
        self.mass.cancel_timer(self._reveal_timer_id)
        self.mass.cancel_timer(self._advance_timer_id)
        self._cancel_replay_auto_start(cancel_task=True)
        self._cancel_presence_expiry(cancel_task=True)


def _playback_selection_changed(previous: MusicQuizGame, current: MusicQuizGame) -> bool:
    """Return whether two games require different playback sessions."""
    return (
        previous.config.playback_mode != current.config.playback_mode
        or previous.config.venue_player_id != current.config.venue_player_id
    )


def _playback_summary(game: MusicQuizGame) -> MusicQuizPlaybackSummary:
    """Return the host-only summary of a game's playback selection."""
    is_venue = game.config.playback_mode == SharedPlaybackMode.VENUE
    return {
        "mode": game.config.playback_mode.value,
        "venue_player_id": game.config.venue_player_id if is_venue else None,
        "venue_player_name": game.config.venue_player_name if is_venue else None,
    }


def _clean_game_name(name: str | None) -> str | None:
    """Return a normalized optional game name."""
    if not name:
        return None
    return name.strip() or None


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Retrieve a settled task's exception so asyncio does not report it as unhandled."""
    if not task.cancelled():
        task.exception()


@contextmanager
def _system_auth_context() -> Iterator[None]:
    """Temporarily run provider-owned work without a requesting user."""
    current_user_token = current_user.set(None)
    impersonated_user_token = impersonated_user.set(None)
    try:
        yield
    finally:
        impersonated_user.reset(impersonated_user_token)
        current_user.reset(current_user_token)


def _get_join_round(game: MusicQuizGame) -> int:
    """Return the first round a newly joined player may answer."""
    if game.phase == MusicQuizPhase.LOBBY:
        return 0
    if game.current_round_index is None:
        return len(game.rounds)
    return game.current_round_index + 1


def _get_player(game: MusicQuizGame, player_id: str) -> MusicQuizPlayer:
    """Return a player by their private player_id."""
    if player := _find_player(game, player_id):
        return player
    raise MusicQuizUnknownPlayerError("Unknown Music Quiz player")


def _find_player(game: MusicQuizGame, player_id: str) -> MusicQuizPlayer | None:
    """Return a player by their private player_id, if present."""
    for player in game.players.values():
        if secrets.compare_digest(player.player_id, player_id):
            return player
    return None


def _is_player_active(player: MusicQuizPlayer, now: float) -> bool:
    """
    Return whether a player's reconnect grace period is active.

    :param player: Player to inspect.
    :param now: Current server timestamp.
    """
    return player.last_seen + PLAYER_RECONNECT_GRACE_SECONDS > now


def _has_active_players(game: MusicQuizGame, now: float) -> bool:
    """
    Return whether the game has a player within the reconnect grace period.

    :param game: Game to inspect.
    :param now: Current server timestamp.
    """
    return any(_is_player_active(player, now) for player in game.players.values())


def _answer_window(game: MusicQuizGame, game_round: MusicQuizRound) -> float:
    """Return the effective answering window of a round in seconds."""
    answer_window = float(game.config.answer_duration)
    if game_round.duration and game_round.duration > 0:
        answer_window = min(answer_window, game_round.duration)
    return answer_window


def _host_round(
    game_round: MusicQuizRound,
    answer_type: QuizAnswerType,
) -> dict[str, SerializableType]:
    """Return the host-visible flat representation of a round."""
    return {
        "round_index": game_round.round_index,
        "answer_label": game_round.answer_label,
        **answer_type.serialize_host_round(game_round.answer_state),
        "track_uri": game_round.track_uri,
        "question": game_round.question,
        "image_url": game_round.image_url,
        "duration": game_round.duration,
        "started_at": game_round.started_at,
        "ended_at": game_round.ended_at,
        "auto_advance_at": game_round.auto_advance_at,
    }


def _public_state(game: MusicQuizGame, answer_type: QuizAnswerType) -> dict[str, Any]:
    """Return the guest-safe public game state (see the module docstring)."""
    current_round = (
        game.rounds[game.current_round_index] if game.current_round_index is not None else None
    )
    answer_state = current_round.answer_state if current_round else None
    revealed = game.phase in (MusicQuizPhase.REVEAL, MusicQuizPhase.FINISHED)
    players = []
    for player in sorted(game.players.values(), key=lambda item: item.joined_at):
        entry: dict[str, Any] = {
            "name": player.name,
            "score": player.score,
            "ready": player.ready,
            "active_from_round": player.active_from_round,
            **answer_type.serialize_public_player(
                answer_state,
                player.player_id,
                revealed=revealed,
            ),
        }
        players.append(entry)
    return {
        "phase": game.phase.value,
        "name": game.config.name,
        "quiz_type": game.quiz_type,
        "answer_type": game.answer_type.value,
        "mode": game.config.playback_mode.value,
        "round_count": game.config.round_count,
        "answer_duration": game.config.answer_duration,
        "auto_start_at": game.auto_start_at,
        **answer_type.serialize_game_config(game),
        **get_quiz_type(game.quiz_type).serialize_game_config(game),
        "players": players,
        "current_round": _public_round(
            game,
            current_round,
            answer_type,
            revealed=revealed,
        ),
    }


def _public_round(
    game: MusicQuizGame,
    game_round: MusicQuizRound | None,
    answer_type: QuizAnswerType,
    *,
    revealed: bool,
) -> dict[str, Any] | None:
    """Return the guest-safe view of a round, redacted while unrevealed."""
    if game_round is None:
        return None
    state: dict[str, Any] = {
        "round_index": game_round.round_index,
        "started_at": game_round.started_at,
        "deadline": (game_round.started_at or 0) + _answer_window(game, game_round),
        "auto_advance_at": game_round.auto_advance_at,
        "question": game_round.question,
        **answer_type.serialize_round(game_round.answer_state, revealed=revealed),
    }
    if revealed:
        state["answer_label"] = game_round.answer_label
        state["track_uri"] = game_round.track_uri
        state["image_url"] = game_round.image_url
        state["duration"] = game_round.duration
        state["ended_at"] = game_round.ended_at
    return state


def _player_state(
    game: MusicQuizGame,
    player: MusicQuizPlayer,
    answer_type: QuizAnswerType,
) -> dict[str, Any]:
    """Return the personalized (still guest-safe) game state for a player."""
    current_round = (
        game.rounds[game.current_round_index] if game.current_round_index is not None else None
    )
    answer_state = current_round.answer_state if current_round else None
    revealed = game.phase in (MusicQuizPhase.REVEAL, MusicQuizPhase.FINISHED)
    you: dict[str, Any] = {
        "name": player.name,
        "score": player.score,
        "ready": player.ready,
        "active_from_round": player.active_from_round,
        **answer_type.serialize_personal_player(
            answer_state,
            player.player_id,
            revealed=revealed,
        ),
    }
    return {**_public_state(game, answer_type), "you": you}
