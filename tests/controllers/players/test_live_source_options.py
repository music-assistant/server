"""
Tests for recording the ordering a live source reports for its own session.

Plugin providers push the shuffle/repeat state their session is in, identified by
their instance id, and the update lands only when that source is the one playing on
the player — a late callback must never stamp one session's state onto another's.
Accepted from the moment the source is selected, before any stream exists.
"""

from unittest.mock import MagicMock

from music_assistant_models.enums import RepeatMode
from music_assistant_models.media_items import AudioSource

from music_assistant.controllers.players.audio_sources import (
    AudioSourceMixin,
    AudioSourceSession,
)

PLAYER_ID = "player_1"
SOURCE_ID = "main"
INSTANCE_ID = "spotify_connect--test"


class _Controller(AudioSourceMixin):
    """Minimal stand-in for the parts of PlayerController the mixin relies on."""

    def __init__(self) -> None:
        self._source_sessions: dict[str, AudioSourceSession] = {}
        self.mass = MagicMock()
        self.log_mock = MagicMock()
        self.logger = self.log_mock
        self.updated_players: list[str] = []

    def trigger_player_update(self, player_id: str) -> None:
        """Record that a player update was signalled."""
        self.updated_players.append(player_id)


def _with_session(
    *, provider: str = INSTANCE_ID, item_id: str = SOURCE_ID
) -> tuple[_Controller, AudioSourceSession]:
    ctrl = _Controller()
    session = ctrl._start_audio_source_session(
        PLAYER_ID,
        AudioSource(item_id=item_id, provider=provider, name="live", provider_mappings=set()),
        provider,
    )
    return ctrl, session


def test_a_matching_update_records_both_options_and_signals_once() -> None:
    """The session carries what the source reported, and clients are told once."""
    ctrl, session = _with_session()

    ctrl.update_source_options(
        PLAYER_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert session.shuffle_enabled is True
    assert session.repeat_mode is RepeatMode.ALL
    assert ctrl.updated_players == [PLAYER_ID]


def test_no_change_signals_nothing() -> None:
    """Re-reporting the state the session is already in is not news."""
    ctrl, session = _with_session()
    session.shuffle_enabled = True
    session.repeat_mode = RepeatMode.ALL

    ctrl.update_source_options(
        PLAYER_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert ctrl.updated_players == []


def test_none_leaves_an_option_as_it_was() -> None:
    """A source reporting only one of the two says nothing about the other."""
    ctrl, session = _with_session()
    session.repeat_mode = RepeatMode.ONE

    ctrl.update_source_options(
        PLAYER_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=None
    )

    assert session.shuffle_enabled is True
    assert session.repeat_mode is RepeatMode.ONE


def test_an_unknown_repeat_mode_is_not_a_report() -> None:
    """UNKNOWN means the source could not say, so it must not overwrite what it did say."""
    ctrl, session = _with_session()
    session.repeat_mode = RepeatMode.ALL

    ctrl.update_source_options(
        PLAYER_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=None, repeat_mode=RepeatMode.UNKNOWN
    )

    assert session.repeat_mode is RepeatMode.ALL
    assert ctrl.updated_players == []


def test_options_for_another_source_are_rejected() -> None:
    """A source that is not the one playing does not get to set the session's ordering."""
    ctrl, session = _with_session()

    ctrl.update_source_options(
        PLAYER_ID, "some-other-source", INSTANCE_ID, shuffle_enabled=True, repeat_mode=None
    )

    assert session.shuffle_enabled is None
    assert ctrl.updated_players == []
    assert ctrl.log_mock.debug.called


def test_options_from_another_provider_are_rejected() -> None:
    """A provider that does not own the session does not get to set its ordering."""
    ctrl, session = _with_session()

    ctrl.update_source_options(
        PLAYER_ID, SOURCE_ID, "airplay_receiver--xyz", shuffle_enabled=True, repeat_mode=None
    )

    assert session.shuffle_enabled is None
    assert ctrl.updated_players == []
    assert ctrl.log_mock.debug.called


def test_options_for_a_player_with_nothing_playing_are_dropped() -> None:
    """A late callback for a player that moved on is dropped without raising."""
    ctrl = _Controller()

    ctrl.update_source_options(
        PLAYER_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert ctrl.get_audio_source_session(PLAYER_ID) is None
    assert ctrl.updated_players == []
    assert ctrl.log_mock.debug.called


def test_options_are_accepted_before_any_stream_exists() -> None:
    """A provider can report the session's ordering the moment it claims the player."""
    ctrl, session = _with_session()

    ctrl.update_source_options(
        PLAYER_ID, SOURCE_ID, INSTANCE_ID, shuffle_enabled=True, repeat_mode=RepeatMode.ALL
    )

    assert session.streamdetails is None
    assert session.shuffle_enabled is True
