"""Errors for the Music Quiz provider."""

from __future__ import annotations

from music_assistant_models.errors import InvalidDataError, MusicAssistantError

# all Music Quiz error messages resolve against the provider's own strings.json
TRANSLATION_OWNER = "provider.music_quiz"


class MusicQuizNoGameError(MusicAssistantError):
    """Raised when there is no active Music Quiz game."""

    # provider-specific code, far above the core error codes in music_assistant_models
    error_code = 1001
    translation_key = "music_quiz_no_game"
    translation_owner = TRANSLATION_OWNER


class MusicQuizGameActiveError(InvalidDataError):
    """Raised when an action requires the current game to be finished first."""

    error_code = 1002
    translation_key = "music_quiz_game_active"
    translation_owner = TRANSLATION_OWNER


# the webserver localizes error details from the (generic) translation_key of
# the error type, losing the specific message text: guest-facing errors need
# their own stable codes so the frontend can present actionable messages


class MusicQuizNameTakenError(InvalidDataError):
    """Raised when a player name is already used in the game."""

    error_code = 1003
    translation_key = "music_quiz_name_taken"
    translation_owner = TRANSLATION_OWNER


class MusicQuizGameFullError(InvalidDataError):
    """Raised when a game reached its player limit."""

    error_code = 1004
    translation_key = "music_quiz_game_full"
    translation_owner = TRANSLATION_OWNER


class MusicQuizAlreadyAnsweredError(InvalidDataError):
    """Raised when a player answers a round twice."""

    error_code = 1005
    translation_key = "music_quiz_already_answered"
    translation_owner = TRANSLATION_OWNER


class MusicQuizNotActiveThisRoundError(InvalidDataError):
    """Raised when a late joiner answers the round that was already playing."""

    error_code = 1006
    translation_key = "music_quiz_not_active_this_round"
    translation_owner = TRANSLATION_OWNER


class MusicQuizWrongPhaseError(InvalidDataError):
    """Raised when an action does not match the game's current phase."""

    error_code = 1007
    translation_key = "music_quiz_wrong_phase"
    translation_owner = TRANSLATION_OWNER


class MusicQuizNoPlaybackTargetError(InvalidDataError):
    """Raised when a round cannot start because no playback target is available."""

    error_code = 1008
    translation_key = "music_quiz_no_playback_target"
    translation_owner = TRANSLATION_OWNER


class MusicQuizUnknownPlayerError(InvalidDataError):
    """Raised when a guest refers to a player that is not in the game."""

    error_code = 1009
    translation_key = "music_quiz_unknown_player"
    translation_owner = TRANSLATION_OWNER


class MusicQuizUnknownSuggestionError(InvalidDataError):
    """Raised when a guest answers with a suggestion that is not part of the round."""

    error_code = 1010
    translation_key = "music_quiz_unknown_suggestion"
    translation_owner = TRANSLATION_OWNER


class MusicQuizInvalidAnswerError(InvalidDataError):
    """Raised when an answer submission is malformed or mismatched."""

    error_code = 1011
    translation_key = "music_quiz_invalid_answer"
    translation_owner = TRANSLATION_OWNER


class MusicQuizAIUnavailableError(InvalidDataError):
    """Raised when no usable AI provider is available for Music Trivia."""

    error_code = 1012
    translation_key = "music_quiz_ai_unavailable"
    translation_owner = TRANSLATION_OWNER
