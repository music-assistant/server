"""Data models for the Music Quiz provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, TypedDict

from mashumaro import DataClassDictMixin, field_options
from mashumaro.config import BaseConfig
from mashumaro.types import Discriminator

from music_assistant.helpers.shared_playback import SharedPlaybackMode

DEFAULT_TRIVIA_LANGUAGE = "en"


class MusicQuizPhase(StrEnum):
    """Music Quiz game phases."""

    LOBBY = "lobby"
    ANSWERING = "answering"
    REVEAL = "reveal"
    FINISHED = "finished"


class MusicQuizAnswerType(StrEnum):
    """Supported Music Quiz answer types."""

    MULTIPLE_CHOICE = "multiple_choice"
    TIMELINE = "timeline"


class MusicQuizDifficulty(StrEnum):
    """Difficulty levels for the guess-the-song quiz type."""

    EASY = "easy"
    NORMAL = "normal"
    HARD = "hard"


class TimelineBonusMode(StrEnum):
    """Supported timeline bonus answer modes."""

    OFF = "off"
    FREE_TEXT = "free_text"
    MULTIPLE_CHOICE = "multiple_choice"


class TimelineBonusType(StrEnum):
    """Supported timeline bonus identities."""

    ARTIST = "artist"
    TITLE = "title"


class MusicQuizVenuePlayerOption(TypedDict):
    """A venue player available for Music Quiz playback."""

    player_id: str
    name: str


class MusicQuizPlaybackOptions(TypedDict):
    """Host-visible options for configuring Music Quiz playback."""

    default_playback_mode: str
    default_venue_player_id: str | None
    venue_available: bool
    remote_available: bool
    venue_players: list[MusicQuizVenuePlayerOption]


class MusicQuizPlaybackSummary(TypedDict):
    """Host-only summary of a game's playback selection."""

    mode: str
    venue_player_id: str | None
    venue_player_name: str | None


@dataclass
class MusicQuizConfig(DataClassDictMixin):
    """Configuration for a Music Quiz game."""

    round_count: int = 5
    suggestion_count: int = 4
    answer_duration: int = 30
    source_uris: list[str] = field(default_factory=list)
    include_similar_music: bool = False
    name: str | None = None
    playback_mode: SharedPlaybackMode = SharedPlaybackMode.VENUE
    venue_player_id: str | None = None
    venue_player_name: str | None = None
    # difficulty is guess-the-song specific; AI distractors also apply to timeline bonuses
    difficulty: str = MusicQuizDifficulty.NORMAL.value
    use_ai_distractors: bool = False
    # the AI engine uid selected in the provider config, or None when no engine is available
    ai_engine: str | None = None
    # trivia specific; other quiz types ignore this
    language: str = DEFAULT_TRIVIA_LANGUAGE
    play_reveal_audio: bool = True
    # timeline specific; other answer types ignore these
    artist_bonus_mode: TimelineBonusMode = TimelineBonusMode.OFF
    title_bonus_mode: TimelineBonusMode = TimelineBonusMode.OFF


@dataclass
class MusicQuizSource(DataClassDictMixin):
    """A music source selected for a Music Quiz game."""

    uri: str
    name: str
    media_type: str | None = None


@dataclass
class MusicQuizPlayer(DataClassDictMixin):
    """A player participating in a Music Quiz game."""

    # the player_id doubles as the player's private credential: it is only
    # ever returned to the guest that joined and must never appear in
    # broadcast payloads (those key players by their unique display name)
    player_id: str
    name: str
    joined_at: float
    active_from_round: int
    score: int = 0
    ready: bool = False
    last_seen: float = field(
        default=0,
        compare=False,
        repr=False,
        metadata=field_options(serialize="omit"),
    )


@dataclass
class QuizRoundAnswerState(DataClassDictMixin):
    """Answer state persisted for a Music Quiz round."""

    answer_type: MusicQuizAnswerType

    class Config(BaseConfig):
        """Mashumaro configuration."""

        discriminator = Discriminator(field="answer_type", include_subtypes=True)
        forbid_extra_keys = True


@dataclass
class MultipleChoiceSuggestion(DataClassDictMixin):
    """A possible answer for a multiple-choice round."""

    suggestion_id: str
    label: str
    uri: str | None = None
    is_correct: bool = False

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class MultipleChoiceAnswer(DataClassDictMixin):
    """A locked player answer for a multiple-choice round."""

    player_id: str
    suggestion_id: str
    answered_at: float
    is_correct: bool
    points: int = 0

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class MultipleChoiceRoundState(QuizRoundAnswerState):
    """Persisted state for a multiple-choice round."""

    answer_type: Literal[MusicQuizAnswerType.MULTIPLE_CHOICE] = field(
        default=MusicQuizAnswerType.MULTIPLE_CHOICE,
        init=False,
    )
    suggestions: list[MultipleChoiceSuggestion]
    answers: dict[str, MultipleChoiceAnswer] = field(default_factory=dict)


@dataclass
class TimelineEntry(DataClassDictMixin):
    """A revealed entry on a chronological music timeline."""

    entry_id: str
    release_year: int
    title: str
    artist: str
    track_uri: str
    image_url: str | None
    is_anchor: bool = False

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class TimelinePlacementAnswer(DataClassDictMixin):
    """A player's locked placement against a timeline snapshot."""

    previous_entry_id: str | None
    next_entry_id: str | None
    answered_at: float

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class TimelineCandidate(DataClassDictMixin):
    """Protected candidate and accepted truths for a timeline round."""

    entry: TimelineEntry
    artist_answers: list[str]
    title_answers: list[str]

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class TimelineBonusOption(DataClassDictMixin):
    """A possible answer for a multiple-choice timeline bonus."""

    option_id: str
    label: str
    is_correct: bool = False

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class TimelineBonusDefinition(DataClassDictMixin):
    """Protected definition of one enabled timeline bonus."""

    bonus_type: TimelineBonusType
    mode: TimelineBonusMode

    class Config(BaseConfig):
        """Mashumaro configuration."""

        discriminator = Discriminator(field="mode", include_subtypes=True)
        forbid_extra_keys = True


@dataclass
class TimelineFreeTextBonusDefinition(TimelineBonusDefinition):
    """Definition of a free-text timeline bonus."""

    mode: Literal[TimelineBonusMode.FREE_TEXT] = field(
        default=TimelineBonusMode.FREE_TEXT,
        init=False,
    )


@dataclass
class TimelineMultipleChoiceBonusDefinition(TimelineBonusDefinition):
    """Definition of a multiple-choice timeline bonus."""

    mode: Literal[TimelineBonusMode.MULTIPLE_CHOICE] = field(
        default=TimelineBonusMode.MULTIPLE_CHOICE,
        init=False,
    )
    options: list[TimelineBonusOption]


@dataclass
class TimelineBonusAnswer(DataClassDictMixin):
    """A player's persisted answer to one timeline bonus."""

    bonus_type: TimelineBonusType
    action: str
    submitted_at: float

    class Config(BaseConfig):
        """Mashumaro configuration."""

        discriminator = Discriminator(field="action", include_subtypes=True)
        forbid_extra_keys = True


@dataclass
class TimelineTextBonusAnswer(TimelineBonusAnswer):
    """A persisted free-text timeline bonus answer."""

    action: Literal["bonus_text"] = field(default="bonus_text", init=False)
    value: str


@dataclass
class TimelineChoiceBonusAnswer(TimelineBonusAnswer):
    """A persisted multiple-choice timeline bonus answer."""

    action: Literal["bonus_choice"] = field(default="bonus_choice", init=False)
    option_id: str


@dataclass
class TimelinePlacementResult(DataClassDictMixin):
    """Result of a player's timeline placement."""

    previous_entry_id: str | None
    next_entry_id: str | None
    is_correct: bool
    points: int

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class TimelineBonusResult(DataClassDictMixin):
    """Result of a player's submitted timeline bonus."""

    bonus_type: TimelineBonusType
    is_correct: bool
    points: int

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class TimelineAnswerResult(DataClassDictMixin):
    """Revealed result of a player's timeline answer."""

    placement: TimelinePlacementResult
    bonuses: list[TimelineBonusResult] = field(default_factory=list)

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class TimelineRoundState(QuizRoundAnswerState):
    """Persisted state for a timeline round."""

    answer_type: Literal[MusicQuizAnswerType.TIMELINE] = field(
        default=MusicQuizAnswerType.TIMELINE,
        init=False,
    )
    placement_snapshot: list[TimelineEntry]
    candidate: TimelineCandidate
    bonus_definitions: list[TimelineBonusDefinition] = field(default_factory=list)
    placements: dict[str, TimelinePlacementAnswer] = field(default_factory=dict)
    bonus_answers: dict[str, list[TimelineBonusAnswer]] = field(default_factory=dict)
    finished_at: dict[str, float] = field(default_factory=dict)
    results: dict[str, TimelineAnswerResult] = field(default_factory=dict)
    revealed: bool = False


@dataclass
class MusicQuizRound(DataClassDictMixin):
    """A single Music Quiz round."""

    round_index: int
    answer_label: str
    answer_state: QuizRoundAnswerState
    # a round may play its track while answering or after reveal, and/or pose
    # a text question; fully text-only rounds leave track_uri unset
    track_uri: str | None = None
    question: str | None = None
    image_url: str | None = None
    duration: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    auto_advance_at: float | None = None

    class Config(BaseConfig):
        """Mashumaro configuration."""

        forbid_extra_keys = True


@dataclass
class MusicQuizGame(DataClassDictMixin):
    """A Music Quiz game."""

    config: MusicQuizConfig
    quiz_type: str
    answer_type: MusicQuizAnswerType
    phase: MusicQuizPhase = MusicQuizPhase.LOBBY
    created_at: float = 0
    auto_start_at: float | None = None
    # set while a reset loads the sources and first round of the next run
    preparing: bool = False
    players: dict[str, MusicQuizPlayer] = field(default_factory=dict)
    rounds: list[MusicQuizRound] = field(default_factory=list)
    sources: list[MusicQuizSource] = field(default_factory=list)
    current_round_index: int | None = None
