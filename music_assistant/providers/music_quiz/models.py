"""Data models for the Music Quiz provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mashumaro import DataClassDictMixin


class MusicQuizPhase(StrEnum):
    """Music Quiz game phases."""

    LOBBY = "lobby"
    ANSWERING = "answering"
    REVEAL = "reveal"
    FINISHED = "finished"


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


@dataclass
class MusicQuizSuggestion(DataClassDictMixin):
    """A possible answer for a Music Quiz round."""

    suggestion_id: str
    label: str
    uri: str | None = None
    is_correct: bool = False


@dataclass
class MusicQuizAnswer(DataClassDictMixin):
    """A locked player answer for a Music Quiz round."""

    player_id: str
    suggestion_id: str
    answered_at: float
    is_correct: bool
    points: int = 0


@dataclass
class MusicQuizRound(DataClassDictMixin):
    """A single Music Quiz round."""

    round_index: int
    answer_label: str
    suggestions: list[MusicQuizSuggestion]
    # a round plays a track (audio round) and/or poses a text question;
    # non-audio quiz types leave track_uri unset
    track_uri: str | None = None
    question: str | None = None
    image_url: str | None = None
    duration: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    answers: dict[str, MusicQuizAnswer] = field(default_factory=dict)


@dataclass
class MusicQuizGame(DataClassDictMixin):
    """A Music Quiz game."""

    config: MusicQuizConfig
    quiz_type: str
    phase: MusicQuizPhase = MusicQuizPhase.LOBBY
    created_at: float = 0
    players: dict[str, MusicQuizPlayer] = field(default_factory=dict)
    rounds: list[MusicQuizRound] = field(default_factory=list)
    sources: list[MusicQuizSource] = field(default_factory=list)
    current_round_index: int | None = None
