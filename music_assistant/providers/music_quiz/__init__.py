"""
Music Quiz Plugin Provider for Music Assistant.

Provides the backend game engine for multiplayer music quiz games. Guests
join with a QR code on their own device and play the selected quiz type:
guess-the-song uses multiple-choice answers, while Hitster uses a shared
chronological timeline with optional artist and title bonuses.

Playback is hosted by a SharedPlaybackSession in one of two modes
(provider config):

- venue: a configured real player plays the rounds out loud; guests may
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
  ``answer_type``, ``mode`` (venue/remote), ``round_count``, ``answer_duration``
  and public player progress. Private player IDs never appear in broadcasts.
- answering rounds expose common timing and question fields plus a strategy
  fragment. Multiple-choice exposes opaque ``suggestions``. Timeline exposes
  the revealed shared ``timeline`` and redacted ``bonus_definitions``; the
  current song, year, correct placement and bonus answers remain protected.
- reveal/finished rounds additionally expose common ``answer_label``,
  ``track_uri``, ``image_url``, ``duration`` and ``ended_at`` fields. The
  answer strategy adds the revealed correct option or timeline entry and
  answer-specific player results.

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
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope, UserRole
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import ConfigEntryType, PlaybackState, QueueOption
from music_assistant_models.errors import InvalidDataError, SetupFailedError
from music_assistant_models.media_items import Track

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers import guest_access
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.shared_playback import SharedPlaybackMode, SharedPlaybackSession
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz.answer_types import get_answer_type
from music_assistant.providers.music_quiz.answer_types.base import (
    QuizAnswerSubmission,
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
    MusicQuizAnswerType,
    MusicQuizConfig,
    MusicQuizDifficulty,
    MusicQuizGame,
    MusicQuizPhase,
    MusicQuizPlayer,
    MusicQuizRound,
    MusicQuizSource,
    TimelineBonusMode,
)
from music_assistant.providers.music_quiz.quiz_types import get_quiz_type
from music_assistant.providers.music_quiz.quiz_types.base import QuizType

if TYPE_CHECKING:
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()

_ApiHandler = Callable[..., Coroutine[Any, Any, Any]]

CONF_MODE = "mode"
CONF_PLAYER = "player"
CONF_PLAYER_AUTO = "__auto__"
CONF_USE_AI_DISTRACTORS = "use_ai_distractors"

MUSIC_QUIZ_GUEST_USER = "music_quiz_guest"
MUSIC_QUIZ_GUEST_DISPLAY_NAME = "Music Quiz Guest"

# defence-in-depth cap: a leaked join URL must not be able to flood a game
MAX_PLAYER_COUNT = 100
# the joined name is broadcast to every client on each state update; bound it
MAX_PLAYER_NAME_LENGTH = 40
PLAYER_RECONNECT_GRACE_SECONDS = 60.0

# minimum time players get to see the reveal/scoreboard before the game
# advances, even when the round track has (almost) finished playing
MIN_REVEAL_SECONDS = 10.0


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
    return (
        ConfigEntry(
            key=CONF_MODE,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=SharedPlaybackMode.VENUE.value,
            options=[
                ConfigValueOption(SharedPlaybackMode.VENUE.value),
                ConfigValueOption(SharedPlaybackMode.REMOTE.value),
            ],
        ),
        ConfigEntry(
            key=CONF_PLAYER,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=CONF_PLAYER_AUTO,
            depends_on=CONF_MODE,
            depends_on_value=SharedPlaybackMode.VENUE.value,
            options=[
                ConfigValueOption(CONF_PLAYER_AUTO),
                *[
                    ConfigValueOption(player.player_id, title=player.display_name)
                    for player in sorted(
                        mass.players.all_players(False, False),
                        key=lambda p: p.display_name.lower(),
                    )
                ],
            ],
        ),
        ConfigEntry(
            key=CONF_USE_AI_DISTRACTORS,
            type=ConfigEntryType.BOOLEAN,
            required=False,
            default_value=False,
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
        self._playback_session: SharedPlaybackSession | None = None
        self._playback_lock = asyncio.Lock()
        self._next_round_task: asyncio.Task[MusicQuizRound] | None = None
        self._unregister_handles: list[Callable[[], None]] = []

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        host_commands: tuple[tuple[str, _ApiHandler], ...] = (
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
        # guest game commands: any authenticated user passes the API layer,
        # the handlers themselves validate the caller is the quiz guest user
        guest_commands: tuple[tuple[str, _ApiHandler], ...] = (
            ("music_quiz/info", self.get_game_info),
            ("music_quiz/join", self.join_game),
            ("music_quiz/state", self.get_player_state),
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
            self._cancel_timers()
            self._cancel_next_round_task()
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

    async def create_game(
        self,
        quiz_type: str = "guess_the_song",
        round_count: int = 5,
        suggestion_count: int = 4,
        answer_duration: int = 30,
        source_uris: list[str] | None = None,
        name: str | None = None,
        difficulty: str = MusicQuizDifficulty.NORMAL.value,
        artist_bonus_mode: str = TimelineBonusMode.OFF.value,
        title_bonus_mode: str = TimelineBonusMode.OFF.value,
    ) -> dict[str, Any]:
        """
        Create a new Music Quiz game, replacing a previous (finished) game.

        :param quiz_type: The quiz type to play (e.g. "guess_the_song").
        :param round_count: Number of rounds to play.
        :param suggestion_count: Number of answer suggestions per round.
        :param answer_duration: Answering duration in seconds.
        :param source_uris: Track or playlist URIs to draw the rounds from.
        :param name: Optional game name.
        :param difficulty: Guess-the-song difficulty ("easy", "normal" or "hard").
        :param artist_bonus_mode: Hitster artist bonus mode.
        :param title_bonus_mode: Hitster title bonus mode.
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
        game_config = quiz_type_class.normalize_config(
            MusicQuizConfig(
                round_count=round_count,
                suggestion_count=suggestion_count,
                answer_duration=answer_duration,
                source_uris=source_uris or [],
                name=_clean_game_name(name),
                difficulty=difficulty,
                use_ai_distractors=bool(self.config.get_value(CONF_USE_AI_DISTRACTORS)),
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
            quiz_strategy, answer_strategy = self._resolve_game_strategies(game)
            await quiz_strategy.initialize()
            self._cancel_timers()
            self._cancel_next_round_task()
            self._game = game
            self._quiz_type = quiz_strategy
            self._answer_type = answer_strategy
            self._prefetch_round(0)
            self._signal_game_updated()
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
            game = self._require_game()
            if game.phase != MusicQuizPhase.LOBBY:
                raise MusicQuizWrongPhaseError("The game has already started")
            await self._start_next_round()
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

    async def reset(self) -> dict[str, Any]:
        """Reset the current game for a new run with the same settings and players."""
        async with self._game_lock:
            game = self._require_game()
            quiz_strategy, answer_strategy = self._resolve_game_strategies(game)
            await quiz_strategy.initialize()
            self._cancel_timers()
            self._cancel_next_round_task()
            await self._stop_playback()
            reset_game(game)
            self._quiz_type = quiz_strategy
            self._answer_type = answer_strategy
            self._prefetch_round(0)
            self._schedule_presence_expiry()
            self._signal_game_updated()
            return await self._host_state()

    async def delete_game(self) -> None:
        """Delete the current game and stop its playback."""
        async with self._game_lock:
            self._require_game()
            self._cancel_timers()
            self._cancel_next_round_task()
            # clear game state before tearing down the session so a guest listen-in
            # racing with delete cannot (re)create or join a session mid-teardown
            self._game = None
            self._quiz_type = None
            self._answer_type = None
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
                "mode": self._mode,
                "player_count": len(game.players),
                "round_count": game.config.round_count,
            }

    async def join_game(self, name: str) -> dict[str, Any]:
        """
        Join the current game as a player.

        :param name: Unique player display name.
        :return: The player's private player_id (their credential for further
            game commands) and their personalized game state.
        """
        self._validate_guest_access()
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
                "state": _player_state(game, player, self._mode, answer_type),
            }

    async def get_player_state(self, player_id: str) -> dict[str, Any]:
        """
        Return the personalized game state for a player (initial load/reconnect).

        :param player_id: The player's private player_id.
        """
        self._validate_guest_access()
        async with self._game_lock:
            game, _, answer_type = self._require_game_strategies()
            player = _get_player(game, player_id)
            self._refresh_player_presence(player)
            return _player_state(game, player, self._mode, answer_type)

    async def heartbeat(self, player_id: str) -> bool:
        """
        Refresh a player's reconnect grace period.

        :param player_id: The player's private player_id.
        :return: True when the player still exists, otherwise False.
        """
        self._validate_guest_access()
        async with self._game_lock:
            if self._game is None or (player := _find_player(self._game, player_id)) is None:
                return False
            self._refresh_player_presence(player)
            return True

    async def submit_answer(
        self,
        player_id: str,
        submission: dict[str, object],
    ) -> dict[str, SerializableType]:
        """
        Submit a typed answer for the current round.

        :param player_id: The player's private player_id.
        :param submission: Discriminated answer submission.
        """
        self._validate_guest_access()
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
        self._validate_guest_access()
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
        self._validate_guest_access()
        async with self._game_lock:
            game, _, answer_type = self._require_game_strategies()
            player = _get_player(game, player_id)
            self._refresh_player_presence(player)
            # a repeat ready is a no-op: it cannot newly satisfy the all-ready
            # check, so return current state without re-broadcasting
            if game.phase != MusicQuizPhase.REVEAL or player.ready:
                return _player_state(game, player, self._mode, answer_type)
            mark_player_ready(game, player.player_id)
            # advance early when every player is ready for the next round
            if are_active_players_ready(game):
                await self._advance_from_reveal()
            else:
                self._signal_game_updated()
            return _player_state(game, player, self._mode, answer_type)

    async def listen_in(self, web_player_id: str) -> None:
        """
        Attach a guest's web player to the game audio.

        :param web_player_id: The player_id of the guest's web player.
        """
        self._validate_guest_access()
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
        self._validate_guest_access()
        async with self._playback_lock:
            if self._playback_session is not None:
                await self._playback_session.remove_guest_listener(web_player_id)

    async def can_listen_in(self, web_player_id: str) -> bool:
        """
        Return whether the given guest web player can listen in on the game audio.

        :param web_player_id: The player_id of the guest's web player.
        """
        self._validate_guest_access()
        async with self._playback_lock:
            session = await self._get_or_create_session_locked()
            return session is not None and session.can_listen_in(web_player_id)

    # ==================== Internals ====================

    def _require_game(self) -> MusicQuizGame:
        """Return the current game or raise when there is none."""
        if self._game is None:
            raise MusicQuizNoGameError("There is no active Music Quiz game")
        return self._game

    def _resolve_game_strategies(self, game: MusicQuizGame) -> tuple[QuizType, QuizAnswerType]:
        """
        Resolve the strategies declared by game state.

        :param game: Game whose strategy identities should be resolved.
        """
        quiz_type_class = get_quiz_type(game.quiz_type)
        answer_type_class = get_answer_type(game.answer_type)
        if quiz_type_class.answer_type != game.answer_type:
            raise InvalidDataError("Quiz type answer type does not match the game")
        return quiz_type_class(self.mass, game.config), answer_type_class()

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

    @property
    def _mode(self) -> str:
        """Return the configured playback mode (venue/remote)."""
        return cast("str", self.config.get_value(CONF_MODE))

    @staticmethod
    def _validate_guest_access() -> None:
        """
        Validate the current user is an authenticated dedicated guest.

        :raises InvalidDataError: If the user is not a dedicated guest.
        """
        user = get_current_user()
        if not user or user.role != UserRole.GUEST:
            raise InvalidDataError(
                "This action is only available to Music Quiz guests",
                translation_key="music_quiz_guest_only",
                translation_owner=TRANSLATION_OWNER,
            )

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
            self._do_reveal()
        else:
            self._signal_game_updated()
        return _player_state(game, player, self._mode, answer_type)

    async def _host_state(self) -> dict[str, Any]:
        """Return the host-visible state of the current game."""
        game, _, answer_type = self._require_game_strategies()
        return {
            **_public_state(game, self._mode, answer_type),
            "created_at": game.created_at,
            "sources": [source.to_dict() for source in game.sources],
            "join_url": await self._get_join_url(),
            "rounds": [_host_round(game_round, answer_type) for game_round in game.rounds],
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
            {"event": "game_updated", "state": _public_state(game, self._mode, answer_type)}
        )

    async def _resolve_sources(self, source_uris: list[str]) -> list[MusicQuizSource]:
        """Resolve configured source URIs into host-visible source metadata."""
        sources: list[MusicQuizSource] = []
        for source_uri in source_uris:
            try:
                media_item = await self.mass.music.get_item_by_uri(source_uri)
            except Exception as err:
                # the real failure otherwise only surfaces at round start,
                # minutes later and far from the cause
                self.logger.warning("Could not resolve Music Quiz source %s: %s", source_uri, err)
                sources.append(MusicQuizSource(uri=source_uri, name=source_uri))
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

    async def _start_next_round(self) -> None:
        """Prepare the next round, start its playback (if any) and open the answering phase."""
        game, quiz_type, answer_type = self._require_game_strategies()
        round_index = len(game.rounds)
        next_round = await self._get_prepared_round(round_index)
        if next_round.track_uri:
            await self._play_track(next_round.track_uri)
        start_round(game, next_round, time.time(), answer_type)
        answer_window = _answer_window(game, next_round)
        self.mass.call_later(
            answer_window,
            self._on_answer_deadline,
            round_index,
            task_id=self._reveal_timer_id,
        )
        if next_round.track_uri and quiz_type.warm_up_lyrics:
            self._warm_up_lyrics(next_round.track_uri)
        self._prefetch_round(round_index + 1)
        self._signal_game_updated()

    def _do_reveal(self) -> None:
        """Reveal the current round, apply scoring and schedule the auto-advance."""
        game, _, answer_type = self._require_game_strategies()
        reveal_round(game, answer_type)
        self.mass.cancel_timer(self._reveal_timer_id)
        current_round = get_current_round(game)
        # let the revealed track play out before auto-advancing; without a
        # known duration the game advances on all-ready or a host command
        if current_round.duration and current_round.started_at:
            remaining = current_round.started_at + current_round.duration - time.time()
            self.mass.call_later(
                max(remaining, MIN_REVEAL_SECONDS),
                self._on_reveal_finished,
                current_round.round_index,
                task_id=self._advance_timer_id,
            )
        self._signal_game_updated()

    async def _advance_from_reveal(self) -> None:
        """Advance a revealed game to the next round or finish it."""
        game = self._require_game()
        self.mass.cancel_timer(self._advance_timer_id)
        if len(game.rounds) >= game.config.round_count:
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
                if player.last_seen + PLAYER_RECONNECT_GRACE_SECONDS <= now
            ]
            if not expired_player_ids:
                self._schedule_presence_expiry(now)
                return

            for player_id in expired_player_ids:
                remove_game_player(game, player_id, answer_type)

            if game.players and game.phase == MusicQuizPhase.ANSWERING:
                if all_active_players_complete(game, answer_type):
                    self._do_reveal()
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

    async def _on_answer_deadline(self, round_index: int) -> None:
        """Reveal the round when the answering deadline passed."""
        async with self._game_lock:
            if not self._is_current_round(round_index, MusicQuizPhase.ANSWERING):
                return
            self._do_reveal()

    async def _on_reveal_finished(self, round_index: int) -> None:
        """Advance the game when the revealed track finished playing."""
        async with self._game_lock:
            if not self._is_current_round(round_index, MusicQuizPhase.REVEAL):
                return
            try:
                await self._advance_from_reveal()
            except Exception as err:
                # leave the game in reveal so the host can retry via next/reset
                self.logger.error("Could not advance Music Quiz game: %s", err, exc_info=err)

    def _is_current_round(self, round_index: int, phase: MusicQuizPhase) -> bool:
        """Return whether the game is still in the given round and phase."""
        return (
            self._game is not None
            and self._game.phase == phase
            and self._game.current_round_index == round_index
        )

    # ---------- round preparation ----------

    def _prefetch_round(self, round_index: int) -> None:
        """Prepare an upcoming round in the background."""
        self._cancel_next_round_task()
        if self._game is None or round_index >= self._game.config.round_count:
            return
        game, quiz_type, _ = self._require_game_strategies()
        self._next_round_task = self.mass.create_task(
            quiz_type.prepare_round(round_index, list(game.rounds))
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
                pass
            except Exception as err:
                self.logger.warning(
                    "Prefetched Music Quiz round failed, preparing a fresh one: %s", err
                )
        return await quiz_type.prepare_round(round_index, list(game.rounds))

    def _cancel_next_round_task(self) -> None:
        """Cancel a pending round prefetch task."""
        if self._next_round_task is not None:
            self._next_round_task.cancel()
            # retrieve the result/exception once the task settles so a prefetch that
            # already failed is not reported by asyncio as an unhandled exception
            self._next_round_task.add_done_callback(_consume_task_exception)
            self._next_round_task = None

    def _warm_up_lyrics(self, track_uri: str) -> None:
        """Fetch/caches the track lyrics so they are ready when revealed."""
        self.mass.create_task(
            self._fetch_lyrics(track_uri),
            task_id=f"music_quiz_lyrics_{self.instance_id}",
            abort_existing=True,
        )

    async def _fetch_lyrics(self, track_uri: str) -> None:
        """Best-effort lyrics warm-up for the given track."""
        try:
            track = await self.mass.music.get_item_by_uri(track_uri)
            if isinstance(track, Track):
                await self.mass.metadata.get_track_lyrics(track)
        except Exception as err:
            self.logger.debug("Lyrics warm-up failed for %s: %s", track_uri, err)

    # ---------- playback ----------

    async def _play_track(self, track_uri: str) -> None:
        """Play the given track on the game's playback session."""
        session = await self._get_playback_session()
        if session is None:
            raise MusicQuizNoPlaybackTargetError(
                "No playback target is available for the Music Quiz game"
            )
        await self.mass.player_queues.play_media(
            session.queue_id, track_uri, option=QueueOption.REPLACE
        )

    async def _stop_playback(self) -> None:
        """Stop playback on the game's playback session, if any."""
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
            if self._playback_session is not None:
                await self._playback_session.close()
                self._playback_session = None

    async def _get_playback_session(self) -> SharedPlaybackSession | None:
        """
        Get the shared playback session for the quiz, creating it if needed.

        In remote mode the session is backed by a hidden virtual player; the
        session is (re)created here when that player does not exist (e.g.
        after a Sendspin provider reload). In venue mode a session only exists
        when a configured or auto-selected venue player is available.

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
        if self._game is None:
            return None
        # drop a stale session whose player no longer exists
        if self._playback_session is not None and (
            self.mass.players.get_player(self._playback_session.player_id) is None
        ):
            await self._playback_session.close()
            self._playback_session = None

        if self._playback_session is not None:
            return self._playback_session

        if self.config.get_value(CONF_MODE) == SharedPlaybackMode.REMOTE.value:
            game_name = self._game.config.name if self._game else None
            try:
                self._playback_session = await SharedPlaybackSession.create_remote(
                    self.mass,
                    owner_instance_id=self.instance_id,
                    display_name=game_name or "Music Quiz",
                    session_id=self.instance_id,
                )
            except SetupFailedError as err:
                self.logger.warning("Unable to create remote quiz session: %s", err)
        elif player_id := self._resolve_venue_player_id():
            try:
                self._playback_session = await SharedPlaybackSession.create_venue(
                    self.mass, player_id
                )
            except SetupFailedError as err:
                self.logger.warning("Unable to create venue quiz session: %s", err)
        return self._playback_session

    def _resolve_venue_player_id(self) -> str | None:
        """
        Resolve the venue player, honoring the "auto" fallback.

        :return: The player_id to host venue playback, or None when no player is available.
        """
        player_id = cast("str | None", self.config.get_value(CONF_PLAYER))
        if player_id and player_id != CONF_PLAYER_AUTO:
            return player_id
        # auto: prefer a player that is already playing, then paused, then any available
        fallback: str | None = None
        fallback_priority = -1
        for player in self.mass.players.all_players(False, False):
            if player.playback_state == PlaybackState.PLAYING:
                return player.player_id
            if player.playback_state == PlaybackState.PAUSED and fallback_priority < 1:
                fallback, fallback_priority = player.player_id, 1
            elif fallback_priority < 0:
                fallback, fallback_priority = player.player_id, 0
        return fallback

    # ---------- timers ----------

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

    def _cancel_timers(self) -> None:
        """Cancel all scheduled game timers."""
        self.mass.cancel_timer(self._reveal_timer_id)
        self.mass.cancel_timer(self._advance_timer_id)
        self._cancel_presence_expiry(cancel_task=True)


def _clean_game_name(name: str | None) -> str | None:
    """Return a normalized optional game name."""
    if not name:
        return None
    return name.strip() or None


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Retrieve a settled task's exception so asyncio does not report it as unhandled."""
    if not task.cancelled():
        task.exception()


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
    }


def _public_state(game: MusicQuizGame, mode: str, answer_type: QuizAnswerType) -> dict[str, Any]:
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
        "mode": mode,
        "round_count": game.config.round_count,
        "answer_duration": game.config.answer_duration,
        **answer_type.serialize_game_config(game),
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
    mode: str,
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
    return {**_public_state(game, mode, answer_type), "you": you}
