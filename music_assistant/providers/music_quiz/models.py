"""Data models for the Music Quiz provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from mashumaro import DataClassDictMixin, field_options
from mashumaro.config import BaseConfig
from mashumaro.types import Discriminator


class MusicQuizPhase(StrEnum):
    """Music Quiz game phases."""

    LOBBY = "lobby"
    ANSWERING = "answering"
    REVEAL = "reveal"
    FINISHED = "finished"


class MusicQuizAnswerType(StrEnum):
    """Supported Music Quiz answer types."""

    MULTIPLE_CHOICE = "multiple_choice"


class MusicQuizDifficulty(StrEnum):
    """Difficulty levels for the guess-the-song quiz type."""

    EASY = "easy"
    NORMAL = "normal"
    HARD = "hard"


@dataclass
class MusicQuizConfig(DataClassDictMixin):
    """Configuration for a Music Quiz game."""

    round_count: int = 5
    suggestion_count: int = 4
    answer_duration: int = 30
    source_uris: list[str] = field(default_factory=list)
    name: str | None = None
    # guess-the-song specific; other quiz types ignore these
    difficulty: str = MusicQuizDifficulty.NORMAL.value
    use_ai_distractors: bool = False


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
class MusicQuizRound(DataClassDictMixin):
    """A single Music Quiz round."""

    round_index: int
    answer_label: str
    answer_state: QuizRoundAnswerState
    # a round plays a track (audio round) and/or poses a text question;
    # non-audio quiz types leave track_uri unset
    track_uri: str | None = None
    question: str | None = None
    image_url: str | None = None
    duration: float | None = None
    started_at: float | None = None
    ended_at: float | None = None

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
    players: dict[str, MusicQuizPlayer] = field(default_factory=dict)
    rounds: list[MusicQuizRound] = field(default_factory=list)
    sources: list[MusicQuizSource] = field(default_factory=list)
    current_round_index: int | None = None
