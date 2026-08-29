"""
Tests for YandexStationPlayer state transitions during external playback.

Covers physical pause detection: when the user presses pause on the speaker
while MA is streaming via ``radio_play``, Glagol reports ``playing=False`` with
``aliceState="IDLE"``. The player must propagate ``PlaybackState.PAUSED`` to MA
instead of silently holding the optimistic PLAYING state.
"""

from __future__ import annotations

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.yandex_station.player import YandexStationPlayer


def _make_player() -> YandexStationPlayer:
    """Build a bare player skipping Player.__init__ (avoids MA core deps)."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._player_id = "test_player"
    player._external_playing = False
    player._external_media = None
    player._external_play_confirmed = False
    player._needs_replay = False
    player._prev_alice_state = ""
    player._voice_resume_task = None
    player._alice_spoke = False
    player._pre_voice_volume = 0
    player._intercept_active = False
    player._last_intercepted_track_id = None
    player._last_intercept_time = 0.0
    player._last_mirrored_volume = None
    player._last_progress = 0
    player._last_progress_wall = 0.0
    player._alice_active_pause_sent = False
    player._saved_station_volume = None
    player._station_muted_by_intercept = False
    player._attr_playback_state = PlaybackState.IDLE
    player._attr_powered = False
    player._attr_volume_level = 0
    return player


class _StubConfig:
    def get_value(self, key: str, default: object = None) -> object:
        return False


def _disable_voice_control(player: YandexStationPlayer) -> None:
    player._config = _StubConfig()  # type: ignore[assignment]


# ── External playback: physical pause detection ────────────────────


def test_physical_pause_during_external_playback_sets_paused() -> None:
    """Pressing pause on the speaker must flip MA state to PAUSED."""
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = True  # stream has been observed playing
    player._attr_playback_state = PlaybackState.PLAYING

    player._update_playback_state(playing=False, alice_state="IDLE")

    assert player._attr_playback_state == PlaybackState.PAUSED
    assert player._external_playing is False
    assert player._external_media is None
    assert player._external_play_confirmed is False
    assert player._needs_replay is True


def test_external_startup_window_stays_playing_until_confirmed() -> None:
    """
    Before the station starts fetching the stream, playing=False is expected.

    We must not misread that initial pre-stream window as a physical pause.
    """
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = False  # station hasn't confirmed yet
    player._attr_playback_state = PlaybackState.PLAYING

    player._update_playback_state(playing=False, alice_state="IDLE")

    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._external_playing is True
    assert player._needs_replay is False


def test_external_playing_true_sets_confirmed() -> None:
    """The first playing=True update during external playback flips confirmed."""
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = False

    player._update_playback_state(playing=True, alice_state="IDLE")

    assert player._external_play_confirmed is True
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_powered is True


def test_physical_pause_cancels_pending_voice_resume_task() -> None:
    """
    A pending auto-resume from a prior voice interaction must be cancelled.

    Without this, the task could wake up after the user physically paused and
    resume MA queue playback unexpectedly.
    """
    cancelled = False

    class _FakeTask:
        def cancel(self) -> None:
            nonlocal cancelled
            cancelled = True

    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = True
    player._voice_resume_task = _FakeTask()  # type: ignore[assignment]

    player._update_playback_state(playing=False, alice_state="IDLE")

    assert cancelled is True
    assert player._voice_resume_task is None
    assert player._attr_playback_state == PlaybackState.PAUSED


def test_physical_pause_ignored_during_voice_interaction() -> None:
    """
    Alice speaking/listening must not be treated as a physical pause.

    Voice control is disabled here, so the fallthrough branch must keep PLAYING
    (voice handling itself is tested elsewhere).
    """
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = True

    player._update_playback_state(playing=False, alice_state="LISTENING")

    # With voice control disabled, non-IDLE alice during external playback
    # falls through to the startup/pause branch. Since alice != IDLE, we skip
    # the physical-pause branch — state stays optimistically PLAYING.
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._external_playing is True
