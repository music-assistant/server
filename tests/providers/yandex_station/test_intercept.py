"""
Tests for the experimental Alice-playback intercept feature.

When Alice (Yandex voice assistant) starts music on a Station, the intercept
feature stops the Station's native player, resolves the track via the
``yandex_music`` MA music provider, and starts playback on a configured target
player.  Volume / seek / pause changes on the Station mirror to the target.

The feature is gated by two switches: a provider-level master toggle
(``intercept_feature_enabled``, default OFF) and a per-player toggle
(``intercept_enabled``).  Both must be ON for any intercept action to happen.
"""
# Tests use MagicMock to stand in for MA core objects whose real types are
# Callable / Player / etc.  Mypy strict-mode flags every ``assert_awaited_*`` as
# attr-defined, every mock reassignment as method-assign, and the master-switch
# branch in ``test_intercept_master_switch_off`` as unreachable (because
# ``_intercept_enabled`` returns False there).  All three are expected here.
# mypy: disable-error-code="attr-defined,method-assign,unreachable"

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.errors import UnsupportedFeaturedException

if TYPE_CHECKING:
    import pytest

from music_assistant.providers.yandex_station.constants import (
    CONF_INTERCEPT_ENABLED,
    CONF_INTERCEPT_TARGET,
)
from music_assistant.providers.yandex_station.player import (
    YandexStationPlayer,
    _parse_yandex_track_id,
)

# ── Fixtures ──────────────────────────────────────────────────────────


def _make_intercept_player(
    *,
    feature_enabled: bool = True,
    per_player_enabled: bool = True,
    target_player_id: str | None = "target_player",
    yandex_music_present: bool = True,
    external_playing: bool = False,
) -> YandexStationPlayer:
    """Build a player with intercept-related state and mocked mass."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._player_id = "yandex_station_1"
    player._external_playing = external_playing
    player._external_media = None
    player._intercept_active = False
    player._last_intercepted_track_id = None
    player._last_intercept_time = 0.0
    player._last_mirrored_volume = None
    player._last_progress = 0
    player._last_progress_wall = 0.0
    player._intercept_lock = asyncio.Lock()
    player._alice_active_pause_sent = False
    player._saved_station_volume = None
    player._station_muted_by_intercept = False
    player._prev_alice_state = ""
    player._attr_volume_level = 50  # baseline for saved-volume capture

    # Mock provider config (master switch) and player config (per-player toggle)
    provider_config = MagicMock()
    provider_config.get_value = MagicMock(return_value=feature_enabled)
    provider = MagicMock()
    provider.config = provider_config
    player._provider = provider

    def _player_cfg_get(key: str, default: object = None) -> object:
        if key == CONF_INTERCEPT_ENABLED:
            return per_player_enabled
        if key == CONF_INTERCEPT_TARGET:
            return target_player_id
        return default

    player_config = MagicMock()
    player_config.get_value = MagicMock(side_effect=_player_cfg_get)
    player._config = player_config

    # Mock mass with the four touchpoints intercept uses
    mass = MagicMock()
    mass.get_provider = MagicMock(return_value=MagicMock() if yandex_music_present else None)
    fake_track = MagicMock(name="resolved_track")
    mass.music = MagicMock()
    mass.music.get_item = AsyncMock(return_value=fake_track)
    mass.player_queues = MagicMock()
    mass.player_queues.play_media = AsyncMock()
    mass.players = MagicMock()
    mass.players.cmd_pause = AsyncMock()
    mass.players.cmd_volume_set = AsyncMock()
    mass.players.cmd_seek = AsyncMock()
    # Default: target player exists (intercept pre-validation passes).
    mass.players.get_player = MagicMock(return_value=MagicMock(name="target_player_obj"))
    player.mass = mass

    # Mock glagol with successful stop
    player.glagol = MagicMock()
    player.glagol.send = AsyncMock(return_value={"status": "SUCCESS"})

    return player


def _state(
    *,
    track_id: str = "12345",
    playing: bool = True,
    volume: float | None = 0.5,
    progress: int = 0,
    alice_state: str = "IDLE",
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Build a (state, player_state, playing) tuple for _handle_intercept_tick."""
    player_state = {"id": track_id, "progress": progress, "title": "Some Track"}
    state: dict[str, Any] = {
        "playerState": player_state,
        "playing": playing,
        "aliceState": alice_state,
    }
    if volume is not None:
        state["volume"] = volume
    return state, player_state, playing


# ── Helper: track_id parser ───────────────────────────────────────────


def test_parse_yandex_track_id_plain() -> None:
    """Plain numeric ID passes through unchanged."""
    assert _parse_yandex_track_id("12345") == "12345"


def test_parse_yandex_track_id_with_album_suffix() -> None:
    """`track:album` form drops the album suffix."""
    assert _parse_yandex_track_id("12345:67890") == "12345"


def test_parse_yandex_track_id_strips_whitespace() -> None:
    """Surrounding whitespace is trimmed."""
    assert _parse_yandex_track_id(" 12345 ") == "12345"


def test_parse_yandex_track_id_empty() -> None:
    """Empty input maps to empty string (callers must guard)."""
    assert _parse_yandex_track_id("") == ""


# ── Toggle / kill switch behaviour ────────────────────────────────────


async def test_intercept_triggers_on_alice_play() -> None:
    """Both switches ON, target set, yandex_music present → full intercept flow."""
    player = _make_intercept_player()
    state, player_state, playing = _state(track_id="12345")

    await player._handle_intercept_tick(state, player_state, playing)

    # Only mute(0) is sent — no `stop`, so the Station keeps emitting
    # playerState ticks for each next track Alice queues (continuous handoff).
    sent_payloads = [c.args[0] for c in player.glagol.send.await_args_list]
    assert sent_payloads == [{"command": "setVolume", "volume": 0.0}]
    player.mass.music.get_item.assert_awaited_once()
    kwargs = player.mass.music.get_item.await_args.kwargs
    assert kwargs["item_id"] == "12345"
    assert kwargs["provider_instance_id_or_domain"] == "yandex_music"
    player.mass.player_queues.play_media.assert_awaited_once()
    play_kwargs = player.mass.player_queues.play_media.await_args.kwargs
    assert play_kwargs["queue_id"] == "target_player"
    assert player._intercept_active is True
    assert player._last_intercepted_track_id == "12345"


async def test_intercept_master_switch_off() -> None:
    """Provider master toggle OFF → no action, even with per-player ON."""
    player = _make_intercept_player(feature_enabled=False)
    state, player_state, playing = _state()

    # Real entrypoint guard is in _on_glagol_update, but verify _intercept_enabled
    assert player._intercept_enabled is False

    # Simulate the guard explicitly: tick should not be dispatched
    if player._intercept_enabled and player._intercept_target_player_id:
        await player._handle_intercept_tick(state, player_state, playing)

    player.glagol.send.assert_not_awaited()
    player.mass.music.get_item.assert_not_awaited()
    player.mass.player_queues.play_media.assert_not_awaited()


async def test_intercept_disabled_per_player() -> None:
    """Master toggle ON, per-player OFF → no action."""
    player = _make_intercept_player(per_player_enabled=False)

    assert player._intercept_enabled is False


# ── Failure paths ─────────────────────────────────────────────────────


async def test_intercept_no_yandex_music_provider() -> None:
    """Missing yandex_music provider → no stop, no play, just log."""
    player = _make_intercept_player(yandex_music_present=False)
    state, player_state, playing = _state()

    await player._handle_intercept_tick(state, player_state, playing)

    player.glagol.send.assert_not_awaited()
    player.mass.music.get_item.assert_not_awaited()
    player.mass.player_queues.play_media.assert_not_awaited()
    assert player._intercept_active is False


async def test_intercept_no_target_configured() -> None:
    """Target player_id unset → no action."""
    player = _make_intercept_player(target_player_id=None)
    state, player_state, playing = _state()

    await player._handle_intercept_tick(state, player_state, playing)

    player.glagol.send.assert_not_awaited()
    assert player._intercept_active is False


async def test_intercept_during_external_playing() -> None:
    """Our own bypass stream is playing → never intercept (anti-loop)."""
    player = _make_intercept_player(external_playing=True)
    state, player_state, playing = _state()

    await player._handle_intercept_tick(state, player_state, playing)

    player.glagol.send.assert_not_awaited()
    player.mass.music.get_item.assert_not_awaited()


async def test_intercept_dedup_same_track_within_window() -> None:
    """Same track_id within 5s → second call is a no-op."""
    player = _make_intercept_player()
    state, player_state, playing = _state(track_id="999")

    await player._handle_intercept_tick(state, player_state, playing)
    await player._handle_intercept_tick(state, player_state, playing)

    assert player.mass.player_queues.play_media.await_count == 1
    # 1 send (mute(0) only — no stop) on the first tick; second debounced.
    assert player.glagol.send.await_count == 1


async def test_intercept_resolve_failure_does_not_silence_station() -> None:
    """If get_item raises, the Station is left playing — never silenced."""
    player = _make_intercept_player()
    player.mass.music.get_item = AsyncMock(side_effect=RuntimeError("not found"))
    state, player_state, playing = _state()

    await player._handle_intercept_tick(state, player_state, playing)

    # Resolve happens FIRST — failure means the Station stays playing.
    player.glagol.send.assert_not_awaited()
    player.mass.player_queues.play_media.assert_not_awaited()
    assert player._intercept_active is False


async def test_intercept_resolved_track_without_uri_skips_handoff() -> None:
    """Resolved track with no uri → log warning, leave Station playing."""
    player = _make_intercept_player()
    bad_track = MagicMock(name="track_no_uri")
    bad_track.uri = None
    player.mass.music.get_item = AsyncMock(return_value=bad_track)
    state, player_state, playing = _state()

    await player._handle_intercept_tick(state, player_state, playing)

    player.glagol.send.assert_not_awaited()
    player.mass.player_queues.play_media.assert_not_awaited()
    assert player._intercept_active is False


# ── Mirroring ─────────────────────────────────────────────────────────


async def test_volume_mirror_swallows_unsupported_feature() -> None:
    """Targets without VOLUME_SET raise UnsupportedFeaturedException → log + no-op."""
    player = _make_intercept_player()
    player._intercept_active = True
    player.mass.players.cmd_volume_set = AsyncMock(side_effect=UnsupportedFeaturedException("nope"))

    # Should not raise
    await player._maybe_mirror_volume(0.5)

    player.mass.players.cmd_volume_set.assert_awaited_once()
    # Stamp is still updated so we don't retry on every tick.
    assert player._last_mirrored_volume == 50


async def test_seek_mirror_swallows_unsupported_feature() -> None:
    """Targets without SEEK raise UnsupportedFeaturedException → log + no-op."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_progress = 10
    player._last_progress_wall = time.time() - 1  # 1s ago
    player.mass.players.cmd_seek = AsyncMock(side_effect=UnsupportedFeaturedException("nope"))

    # progress jump ~50s in 1s → would normally trigger cmd_seek
    await player._maybe_mirror_seek(60)

    player.mass.players.cmd_seek.assert_awaited_once()


async def test_volume_mirror_after_intercept() -> None:
    """Volume changes on the Station mirror to the target while active."""
    player = _make_intercept_player()
    state, player_state, _ = _state(track_id="1", volume=0.4)

    # First tick triggers intercept
    await player._handle_intercept_tick(state, player_state, True)
    assert player._intercept_active is True
    # Volume mirror happens in same tick
    player.mass.players.cmd_volume_set.assert_awaited_with("target_player", 40)

    # New tick with same track_id (within debounce) and different volume
    state2, ps2, _ = _state(track_id="1", volume=0.7)
    await player._handle_intercept_tick(state2, ps2, True)
    player.mass.players.cmd_volume_set.assert_awaited_with("target_player", 70)


async def test_volume_mirror_skipped_when_unchanged() -> None:
    """Identical volume in consecutive ticks → only one cmd_volume_set call."""
    player = _make_intercept_player()
    state, player_state, _ = _state(track_id="1", volume=0.5)

    await player._handle_intercept_tick(state, player_state, True)
    await player._handle_intercept_tick(state, player_state, True)

    # exactly one volume command for the value 50
    calls = [c.args for c in player.mass.players.cmd_volume_set.await_args_list]
    assert calls == [("target_player", 50)]


async def test_seek_mirror_on_progress_jump() -> None:
    """Progress jumps far ahead of wall-clock prediction → cmd_seek on target."""
    player = _make_intercept_player()
    # First tick establishes intercept and the progress baseline
    state1, ps1, _ = _state(track_id="1", progress=10)
    await player._handle_intercept_tick(state1, ps1, True)
    assert player._intercept_active is True

    # Same track, but progress jumped to 60 — must be detected as a seek
    state2, ps2, _ = _state(track_id="1", progress=60)
    await player._handle_intercept_tick(state2, ps2, True)

    player.mass.players.cmd_seek.assert_awaited_with("target_player", 60)


# ── Voice interrupt + intercept ───────────────────────────────────────


async def test_alice_speaks_during_intercept_pauses_target_via_dispatcher() -> None:
    """
    Alice activity arrives via Glagol state — dispatcher pauses target.

    This drives ``_handle_intercept_tick`` (the actual entry point), not the
    bypass-only ``_handle_voice_interrupt`` helper.  The intercept session
    must remain active so a follow-up Alice-initiated track resumes it.
    """
    player = _make_intercept_player()
    player._intercept_active = True

    # Same track_id as last_intercepted → debounce skips re-intercept;
    # Alice activity must still pause the target.
    player._last_intercepted_track_id = "12345"
    player._last_intercept_time = time.time()
    state, player_state, _ = _state(track_id="12345", alice_state="LISTENING")

    await player._handle_intercept_tick(state, player_state, True)

    player.mass.players.cmd_pause.assert_awaited_with("target_player")
    # Session stays open so the next Alice track can resume it
    assert player._intercept_active is True


async def test_alice_idle_during_intercept_does_not_pause_target() -> None:
    """No Alice activity → no spurious pause on the target."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_intercepted_track_id = "12345"
    player._last_intercept_time = time.time()
    state, player_state, _ = _state(track_id="12345", alice_state="IDLE")

    await player._handle_intercept_tick(state, player_state, True)

    player.mass.players.cmd_pause.assert_not_awaited()


# ── Stale session / debounce on failure / serialisation ──────────────


async def test_failed_intercept_debounces_to_avoid_log_spam() -> None:
    """
    Repeated WS ticks for the same failing track → only one resolve attempt.

    Failed lookups must update the debounce timestamp; otherwise every Glagol
    tick (~1Hz) would re-run get_item and emit a fresh warning.
    """
    player = _make_intercept_player()
    player.mass.music.get_item = AsyncMock(side_effect=RuntimeError("not found"))
    state, player_state, _ = _state(track_id="bad")

    await player._handle_intercept_tick(state, player_state, True)
    await player._handle_intercept_tick(state, player_state, True)
    await player._handle_intercept_tick(state, player_state, True)

    assert player.mass.music.get_item.await_count == 1


async def test_target_player_unavailable_does_not_silence_station() -> None:
    """Pre-validation: if get_player(target) returns None, no Glagol stop."""
    player = _make_intercept_player()
    player.mass.players.get_player = MagicMock(return_value=None)
    state, player_state, _ = _state()

    await player._handle_intercept_tick(state, player_state, True)

    player.glagol.send.assert_not_awaited()
    player.mass.player_queues.play_media.assert_not_awaited()


async def test_failed_intercept_on_new_track_ends_stale_session() -> None:
    """
    A new track that fails to resolve must pause the target from the prior session.

    Otherwise mirror updates from the Station's native fallback playback would
    keep being forwarded to the target that's still on the previous track.
    """
    player = _make_intercept_player()
    # Simulate a prior successful intercept on track A.
    player._intercept_active = True
    player._last_intercepted_track_id = "A"
    player._last_intercept_time = 0.0  # well outside debounce
    # New track B fails to resolve.
    player.mass.music.get_item = AsyncMock(side_effect=RuntimeError("nope"))
    state, player_state, _ = _state(track_id="B")

    await player._handle_intercept_tick(state, player_state, True)

    player.mass.players.cmd_pause.assert_awaited_with("target_player")
    assert player._intercept_active is False


async def test_handoff_failure_clears_intercept_active() -> None:
    """
    Failed handoff after mute must clear intercept_active.

    Otherwise mirror code would forward state to a target that isn't playing.
    Volume is also restored via _end_intercept_session so the Station isn't
    stuck muted with no way for the user to recover.
    """
    player = _make_intercept_player()
    player.mass.player_queues.play_media = AsyncMock(side_effect=RuntimeError("boom"))
    state, player_state, _ = _state()

    await player._handle_intercept_tick(state, player_state, True)

    # mute(0) fired (resolve succeeded) + restore on session-end cleanup → 2 sends.
    sent_payloads = [c.args[0] for c in player.glagol.send.await_args_list]
    assert sent_payloads == [
        {"command": "setVolume", "volume": 0.0},
        {"command": "setVolume", "volume": 0.5},  # 50/100 from fixture baseline
    ]
    assert player._intercept_active is False


async def test_session_end_clears_debounce_for_quick_resume() -> None:
    """
    End of session must clear the debounce for quick same-track resumes.

    Otherwise a follow-up of the same track within 5s would be debounced and
    left playing on the Station instead of being handed back to the target.
    """
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_intercepted_track_id = "X"
    player._last_intercept_time = time.time()

    await player._pause_target(clear_session=True, clear_debounce=True)

    assert player._intercept_active is False
    assert player._last_intercepted_track_id is None
    assert player._last_intercept_time == 0.0


async def test_concurrent_ticks_do_not_double_handoff() -> None:
    """
    Two near-simultaneous WS ticks for the same track must only stop+play once.

    Without the lock both tasks would pass the dedup check, both would call
    glagol.send(stop) and play_media.  The lock + early debounce-mark serialise
    them so the second one short-circuits.
    """
    player = _make_intercept_player()
    state, player_state, _ = _state(track_id="X")

    # Make get_item slow so the second tick definitely arrives mid-handoff.
    resolve_started = asyncio.Event()
    resolve_release = asyncio.Event()

    async def slow_get_item(**_kwargs: Any) -> Any:
        resolve_started.set()
        await resolve_release.wait()
        return MagicMock(uri="yandex_music://track/X")

    player.mass.music.get_item = AsyncMock(side_effect=slow_get_item)

    t1 = asyncio.create_task(player._handle_intercept_tick(state, player_state, True))
    await resolve_started.wait()
    # Second tick fires while first is still inside _maybe_intercept's lock.
    t2 = asyncio.create_task(player._handle_intercept_tick(state, player_state, True))
    # Give t2 a chance to acquire the lock and hit the dedup check.
    await asyncio.sleep(0)
    resolve_release.set()
    await asyncio.gather(t1, t2)

    # First tick: mute(0) only = 1 send (no stop in continuous-playback mode).
    # Second tick: short-circuits via debounce → still 1 total.
    assert player.glagol.send.await_count == 1
    assert player.mass.player_queues.play_media.await_count == 1


# ── Continuous playback (v1.4.14) ────────────────────────────────────


async def test_intercept_does_not_send_stop() -> None:
    """
    Continuous-playback contract: a handoff must never send {"command":"stop"}.

    Sending stop pauses the Station's queue → no more playerState ticks → no
    next-track handoff.  We only ever mute via setVolume(0).
    """
    player = _make_intercept_player()
    state, player_state, _ = _state(track_id="42")

    await player._handle_intercept_tick(state, player_state, True)

    sent = [c.args[0] for c in player.glagol.send.await_args_list]
    assert all(p.get("command") != "stop" for p in sent)
    assert {"command": "setVolume", "volume": 0.0} in sent


async def test_continuous_handoff_on_track_id_change() -> None:
    """Subsequent track_id → second handoff. Station muted ONCE per session."""
    player = _make_intercept_player()

    state_a, ps_a, _ = _state(track_id="trackA")
    await player._handle_intercept_tick(state_a, ps_a, True)
    # Move beyond the 5s same-track debounce by rewinding _last_intercept_time.
    player._last_intercept_time -= 10
    state_b, ps_b, _ = _state(track_id="trackB")
    await player._handle_intercept_tick(state_b, ps_b, True)

    # Two handoffs, one per track.
    assert player.mass.player_queues.play_media.await_count == 2
    # Mute(0) is sent only ONCE — at session start.  Subsequent handoffs
    # don't re-mute (Station already at vol=0).
    mute_sends = [
        c.args[0]
        for c in player.glagol.send.await_args_list
        if c.args[0] == {"command": "setVolume", "volume": 0.0}
    ]
    assert len(mute_sends) == 1
    assert player._last_intercepted_track_id == "trackB"


async def test_same_track_during_active_session_is_no_op() -> None:
    """
    Same playerState.id on every WS tick must NOT re-trigger handoff.

    Regression guard for the live-station bug where the target's audio
    stuttered every ~5s.  Glagol emits ``playerState`` once per second
    for the entire track duration (3-5min) carrying the same ``id``;
    the original 5-second failure-debounce expired mid-track and let
    every subsequent tick fire a fresh ``play_media(REPLACE)``.  Once a
    track is handed off (``_intercept_active=True``), the same id must
    short-circuit regardless of how much time has passed.
    """
    player = _make_intercept_player()
    # Establish a session with track X already handed off.
    player._intercept_active = True
    player._last_intercepted_track_id = "X"
    player._last_intercept_time = time.time() - 100  # well past 5s debounce

    state, player_state, _ = _state(track_id="X")
    await player._handle_intercept_tick(state, player_state, True)

    # No new handoff, no API churn.
    player.mass.music.get_item.assert_not_awaited()
    player.mass.player_queues.play_media.assert_not_awaited()
    player.glagol.send.assert_not_awaited()


async def test_session_end_restores_station_volume() -> None:
    """_end_intercept_session must send setVolume(saved/100) back to the Station."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._saved_station_volume = 70
    player._station_muted_by_intercept = True

    await player._end_intercept_session(clear_debounce=True)

    sent = [c.args[0] for c in player.glagol.send.await_args_list]
    assert {"command": "setVolume", "volume": 0.7} in sent
    assert player._intercept_active is False
    assert player._saved_station_volume is None
    assert player._station_muted_by_intercept is False
    assert player._last_intercepted_track_id is None


async def test_session_end_when_not_muted_does_not_send_volume() -> None:
    """If we never muted (e.g. user beat us to it via app), don't send volume."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._saved_station_volume = 70
    player._station_muted_by_intercept = False

    await player._end_intercept_session(clear_debounce=False)

    player.glagol.send.assert_not_awaited()
    assert player._intercept_active is False


async def test_volume_mirror_skips_zero_during_session() -> None:
    """Self-induced mute must not propagate to target."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._station_muted_by_intercept = True
    player._saved_station_volume = 60

    await player._maybe_mirror_volume(0.0)

    player.mass.players.cmd_volume_set.assert_not_awaited()


async def test_volume_mirror_allows_zero_when_no_session() -> None:
    """Outside a session, vol=0 must mirror normally."""
    player = _make_intercept_player()
    player._intercept_active = False

    await player._maybe_mirror_volume(0.0)

    player.mass.players.cmd_volume_set.assert_awaited_once_with("target_player", 0)


async def test_user_unmute_via_yandex_app_clears_self_mute_flag() -> None:
    """Station vol > 0 mid-session: clear self-mute flag, update saved baseline."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._station_muted_by_intercept = True
    player._saved_station_volume = 50

    await player._maybe_mirror_volume(0.8)

    player.mass.players.cmd_volume_set.assert_awaited_once_with("target_player", 80)
    assert player._station_muted_by_intercept is False
    assert player._saved_station_volume == 80


async def test_alice_active_unmutes_station() -> None:
    """LISTENING/SPEAKING during a session → restore Station volume for Alice TTS."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._saved_station_volume = 70
    player._station_muted_by_intercept = True
    state, player_state, _ = _state(track_id="X", alice_state="LISTENING")

    await player._handle_intercept_tick(state, player_state, True)

    sent = [c.args[0] for c in player.glagol.send.await_args_list]
    assert {"command": "setVolume", "volume": 0.7} in sent
    assert player._station_muted_by_intercept is False
    # mirror baseline pre-set so the next vol-tick doesn't bounce to target
    assert player._last_mirrored_volume == 70
    # target paused once
    player.mass.players.cmd_pause.assert_awaited_once_with("target_player")


async def test_alice_idle_remutes_station() -> None:
    """
    LISTENING → IDLE edge: re-mute Station now that Alice is done.

    Previous alice state is threaded in as a parameter (snapshot taken
    *before* the dispatcher overwrote ``_prev_alice_state``), since the
    dispatcher schedules the tick via ``mass.create_task`` and the field
    would otherwise read the post-assignment current state by the time
    the tick runs.
    """
    player = _make_intercept_player()
    player._intercept_active = True
    player._saved_station_volume = 70
    player._station_muted_by_intercept = False  # currently unmuted (alice was active)
    # Prevent the playing=False session-end branch from firing in this test —
    # without an established track id, the early-return short-circuits.
    player._last_intercepted_track_id = None
    state, player_state, _ = _state(track_id="X", alice_state="IDLE", playing=False)

    await player._handle_intercept_tick(state, player_state, False, prev_alice_state="SPEAKING")

    sent = [c.args[0] for c in player.glagol.send.await_args_list]
    assert {"command": "setVolume", "volume": 0.0} in sent
    assert player._station_muted_by_intercept is True


async def test_playing_false_during_session_ends_session_and_pauses_target() -> None:
    """
    Physical pause / 'Алиса, пауза' / end-of-queue → end session.

    We can't reliably distinguish a transient user pause from end-of-queue
    on a single ``playing=False`` event, so always end the session — that
    way Station volume is restored even when the queue ends for good.
    Cost: ~one WS round-trip of native audio on quick resume before the
    new session's mute(0) lands.
    """
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_intercepted_track_id = "X"  # established session
    player._last_intercept_time = time.time()
    player._saved_station_volume = 50
    player._station_muted_by_intercept = True
    state, player_state, _ = _state(track_id="X", playing=False, alice_state="IDLE")

    await player._handle_intercept_tick(state, player_state, False)

    player.mass.players.cmd_pause.assert_awaited_once_with("target_player")
    # Session ended → Station volume restored, flags cleared.
    sent = [c.args[0] for c in player.glagol.send.await_args_list]
    assert {"command": "setVolume", "volume": 0.5} in sent
    assert player._intercept_active is False
    assert player._last_intercepted_track_id is None
    assert player._saved_station_volume is None
    assert player._station_muted_by_intercept is False


async def test_playing_false_without_established_session_does_not_pause() -> None:
    """
    Lingering playing=False before any track was intercepted is a no-op.

    Replaces the old test_session_survives_lingering_playing_false but with the
    correct invariant: we only treat playing=False as 'user paused' when a
    session has actually established a track (debounce non-None).
    """
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_intercepted_track_id = None  # no track yet
    state, player_state, _ = _state(track_id="X", playing=False, alice_state="IDLE")

    await player._handle_intercept_tick(state, player_state, False)

    player.mass.players.cmd_pause.assert_not_awaited()
    assert player._intercept_active is True


async def test_pause_target_clear_session_restores_station_volume() -> None:
    """_pause_target(clear_session=True) funnels through _end_intercept_session."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._saved_station_volume = 40
    player._station_muted_by_intercept = True

    await player._pause_target(clear_session=True, clear_debounce=False)

    player.mass.players.cmd_pause.assert_awaited_once_with("target_player")
    sent = [c.args[0] for c in player.glagol.send.await_args_list]
    assert {"command": "setVolume", "volume": 0.4} in sent
    assert player._intercept_active is False
    assert player._saved_station_volume is None


# ── glagol.send() result validation (PR #3605 review) ────────────────


async def test_handoff_aborts_on_mute_send_error() -> None:
    """
    Mute-send transport error must abort the handoff, not silently proceed.

    glagol.send returns {"error": ...} for transport failures rather than
    raising — without explicit validation we'd flip _station_muted_by_intercept
    to True and start the target while the Station is still audible.
    _raise_if_failed must convert the error into PlayerCommandFailed so the
    surrounding handoff try/except runs the session-end cleanup path.
    """
    player = _make_intercept_player()
    # Mute send fails with the standard transport-error envelope
    player.glagol.send = AsyncMock(return_value={"error": "timeout"})
    state, player_state, _ = _state(track_id="42")

    await player._handle_intercept_tick(state, player_state, True)

    # Mute call attempted; play_media never reached because mute failed
    player.glagol.send.assert_awaited_once_with({"command": "setVolume", "volume": 0.0})
    player.mass.player_queues.play_media.assert_not_awaited()
    # Session not active, mute flag not set, debounce still recorded
    assert player._intercept_active is False
    assert player._station_muted_by_intercept is False


async def test_alice_unmute_keeps_flag_when_send_errors() -> None:
    """
    Alice activates and our setVolume(saved/100) returns {"error": ...}.

    The flag must NOT flip — an inconsistent flag would prevent the
    edge-IDLE re-mute branch from firing later (because it gates on
    `not _station_muted_by_intercept`).
    """
    player = _make_intercept_player()
    player._intercept_active = True
    player._saved_station_volume = 70
    player._station_muted_by_intercept = True
    player._last_intercepted_track_id = None  # avoid playing=False session-end branch
    player.glagol.send = AsyncMock(return_value={"error": "not_connected"})
    state, player_state, _ = _state(track_id="X", alice_state="LISTENING")

    await player._handle_intercept_tick(state, player_state, True)

    player.glagol.send.assert_awaited_once_with({"command": "setVolume", "volume": 0.7})
    # Flag preserved — Station is still (presumably) muted; next attempt
    # to unmute can happen on the next alice tick.
    assert player._station_muted_by_intercept is True


async def test_alice_remute_keeps_flag_when_send_errors() -> None:
    """Edge LISTENING/SPEAKING → IDLE: re-mute send fails → flag stays False."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._saved_station_volume = 70
    player._station_muted_by_intercept = False
    player._last_intercepted_track_id = None  # avoid playing=False session-end branch
    player.glagol.send = AsyncMock(return_value={"error": "timeout"})
    state, player_state, _ = _state(track_id="X", alice_state="IDLE", playing=False)

    await player._handle_intercept_tick(state, player_state, False, prev_alice_state="SPEAKING")

    player.glagol.send.assert_awaited_once_with({"command": "setVolume", "volume": 0.0})
    # Re-mute didn't actually land → flag stays False so the next IDLE-edge
    # tick can attempt it again (the prev_alice_state parameter would no
    # longer be LISTENING/SPEAKING, but a future alice activation will reset
    # the cycle).
    assert player._station_muted_by_intercept is False


async def test_restore_station_volume_logs_warning_on_send_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Volume-restore transport error must log at WARNING for operator visibility.

    A stuck-muted Station is user-visible, so we surface the failure loudly
    (not DEBUG).  Validates that _restore_station_volume catches the error
    envelope from glagol.send and emits a WARNING-level log line.
    """
    player = _make_intercept_player()
    player.glagol.send = AsyncMock(return_value={"error": "not_connected"})

    with caplog.at_level(logging.WARNING):
        await player._restore_station_volume(50)

    # The send was attempted; the transport error surfaced in a WARNING.
    player.glagol.send.assert_awaited_once_with({"command": "setVolume", "volume": 0.5})
    assert any("failed to restore Station volume" in r.message for r in caplog.records)


# ── Real entrypoint ──────────────────────────────────────────────────


async def test_on_glagol_update_dispatches_intercept_tick_via_create_task() -> None:
    """
    _on_glagol_update must hand intercept work off through mass.create_task.

    This covers the integration boundary the other tests bypass by calling
    _handle_intercept_tick directly.
    """
    player = _make_intercept_player()
    # Stub out _update_playback_state and friends so we only observe the
    # intercept dispatch.  ``update_state`` / ``set_current_media`` are
    # declared @final on Player, so use setattr() to dodge mypy's [misc]
    # error in upstream's strict-mode CI.
    player._update_playback_state = MagicMock()
    setattr(player, "update_state", MagicMock())  # noqa: B010
    setattr(player, "set_current_media", MagicMock())  # noqa: B010
    player._attr_available = False
    player._attr_powered = True
    player._attr_volume_level = 0
    player._attr_playback_state = PlaybackState.IDLE
    player._attr_elapsed_time = 0
    player._attr_elapsed_time_last_updated = 0.0
    player._attr_current_media = None
    player._prev_alice_state = ""
    player._voice_resume_task = None
    player._voice_control_enabled_cache = False  # not the real attr but harmless

    captured: list[Any] = []
    player.mass.create_task = MagicMock(side_effect=captured.append)

    raw_state = {
        "state": {
            "playerState": {"id": "12345", "title": "X", "progress": 0, "duration": 60},
            "playing": True,
            "volume": 0.5,
            "aliceState": "IDLE",
        }
    }
    player._on_glagol_update(raw_state)

    # At least one create_task call must be the intercept tick coroutine.
    # Coroutine objects expose `__name__` only on Py3.8+; `cr_code.co_name` is
    # the portable introspection point for any coroutine across Python versions.
    coro_names = [getattr(getattr(c, "cr_code", None), "co_name", "") for c in captured]
    assert "_handle_intercept_tick" in coro_names, coro_names
    # Cleanup never-awaited coroutines so pytest doesn't warn.
    for coro in captured:
        if hasattr(coro, "close"):
            coro.close()


async def test_dispatcher_threads_prev_alice_state_snapshot() -> None:
    """
    _on_glagol_update must pass the *pre-assignment* alice state to the tick.

    The dispatcher overwrites self._prev_alice_state with the current
    aliceState before scheduling the tick coroutine.  If the tick read
    self._prev_alice_state directly, the LISTENING/SPEAKING → IDLE edge
    re-mute branch would be dead code in production (always reading the
    current state).  This test pins down the snapshot mechanism: when
    prev was SPEAKING and current is IDLE, the tick must receive
    prev=SPEAKING via parameter — the fix for Copilot's #57 review.
    """
    player = _make_intercept_player()
    player._update_playback_state = MagicMock()
    setattr(player, "update_state", MagicMock())  # noqa: B010
    setattr(player, "set_current_media", MagicMock())  # noqa: B010
    player._attr_available = False
    player._attr_powered = True
    player._attr_volume_level = 0
    player._attr_playback_state = PlaybackState.IDLE
    player._attr_elapsed_time = 0
    player._attr_elapsed_time_last_updated = 0.0
    player._attr_current_media = None
    player._prev_alice_state = "SPEAKING"  # pre-assignment value
    player._voice_resume_task = None
    player._voice_control_enabled_cache = False

    captured: list[Any] = []
    player.mass.create_task = MagicMock(side_effect=captured.append)

    raw_state = {
        "state": {
            "playerState": {"id": "12345", "title": "X", "progress": 0, "duration": 60},
            "playing": False,  # alice IDLE, not playing → idle-edge scenario
            "volume": 0.5,
            "aliceState": "IDLE",  # current
        }
    }
    player._on_glagol_update(raw_state)

    # The coroutine has frozen its arguments; cr_frame.f_locals exposes them.
    intercept_coros = [
        c
        for c in captured
        if getattr(getattr(c, "cr_code", None), "co_name", "") == "_handle_intercept_tick"
    ]
    assert intercept_coros, "intercept tick was not scheduled"
    locals_dict = intercept_coros[0].cr_frame.f_locals
    assert locals_dict["prev_alice_state"] == "SPEAKING", (
        f"snapshot leaked: got {locals_dict['prev_alice_state']!r}"
    )
    # Field itself was overwritten with current state, as expected.
    assert player._prev_alice_state == "IDLE"
    for coro in captured:
        if hasattr(coro, "close"):
            coro.close()


# ── Round 3: alice handling, target.available, debounce preservation ─


async def test_alice_pause_is_idempotent_across_ticks() -> None:
    """Alice talks for several WS ticks → only one cmd_pause to the target."""
    player = _make_intercept_player()
    player._intercept_active = True
    state, player_state, _ = _state(track_id="X", alice_state="LISTENING")

    await player._handle_intercept_tick(state, player_state, True)
    await player._handle_intercept_tick(state, player_state, True)
    await player._handle_intercept_tick(state, player_state, True)

    assert player.mass.players.cmd_pause.await_count == 1
    # Session stays open so the next Alice track resumes it
    assert player._intercept_active is True
    assert player._alice_active_pause_sent is True


async def test_alice_pause_flag_clears_on_idle() -> None:
    """After alice goes IDLE, the pause-flag resets so the next interaction repauses."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._alice_active_pause_sent = True

    state_idle, ps_idle, _ = _state(track_id="X", alice_state="IDLE")
    await player._handle_intercept_tick(state_idle, ps_idle, True)
    assert player._alice_active_pause_sent is False


async def test_alice_voice_clears_debounce() -> None:
    """After voice-pause, a same-track resume must not be blocked by debounce."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_intercepted_track_id = "Y"
    player._last_intercept_time = time.time()
    state, player_state, _ = _state(track_id="Y", alice_state="LISTENING")

    await player._handle_intercept_tick(state, player_state, True)

    assert player._last_intercepted_track_id is None
    assert player._last_intercept_time == 0.0


async def test_alice_active_blocks_new_handoff_in_same_tick() -> None:
    """A fresh playerState.id arriving alongside alice activity must not start a handoff."""
    player = _make_intercept_player()
    player._intercept_active = True
    # Same tick: alice listening AND a new track id appeared.
    state, player_state, _ = _state(track_id="NEW", alice_state="LISTENING")

    await player._handle_intercept_tick(state, player_state, True)

    # Target paused for alice, but no handoff started for the new track.
    player.mass.players.cmd_pause.assert_awaited()
    player.glagol.send.assert_not_awaited()
    player.mass.music.get_item.assert_not_awaited()
    player.mass.player_queues.play_media.assert_not_awaited()


async def test_target_with_available_false_is_rejected() -> None:
    """get_player can return an object with available=False — must not silence Station."""
    player = _make_intercept_player()
    unavailable = MagicMock()
    unavailable.available = False
    player.mass.players.get_player = MagicMock(return_value=unavailable)
    state, player_state, _ = _state()

    await player._handle_intercept_tick(state, player_state, True)

    player.glagol.send.assert_not_awaited()
    player.mass.player_queues.play_media.assert_not_awaited()


async def test_failed_intercept_on_new_track_preserves_debounce() -> None:
    """New-track failure pauses old session but keeps the new track's debounce."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_intercepted_track_id = "OLD"
    player._last_intercept_time = 0.0  # well outside the 5s window
    player.mass.music.get_item = AsyncMock(side_effect=RuntimeError("nope"))
    state, player_state, _ = _state(track_id="NEW")

    await player._handle_intercept_tick(state, player_state, True)
    # Old session ended
    player.mass.players.cmd_pause.assert_awaited()
    assert player._intercept_active is False
    # NEW track's debounce stamp survives so the next tick is a no-op
    assert player._last_intercepted_track_id == "NEW"

    await player._handle_intercept_tick(state, player_state, True)
    # Still only one resolve attempt — second tick was debounced
    assert player.mass.music.get_item.await_count == 1


async def test_pause_target_helper_flag_combinations() -> None:
    """The two flags on _pause_target are independent."""
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_intercepted_track_id = "X"
    player._last_intercept_time = time.time()

    # clear_session=False, clear_debounce=True → keeps active, clears debounce
    await player._pause_target(clear_session=False, clear_debounce=True)
    assert player._intercept_active is True
    assert player._last_intercepted_track_id is None

    # Re-establish state
    player._last_intercepted_track_id = "Y"
    player._last_intercept_time = time.time()

    # clear_session=True, clear_debounce=False → clears active, keeps debounce
    await player._pause_target(clear_session=True, clear_debounce=False)
    assert player._intercept_active is False
    assert player._last_intercepted_track_id == "Y"


# ── Round 4: serialisation, fault tolerance, dropdown filter ──────────


async def test_concurrent_alice_ticks_send_one_pause() -> None:
    """
    Two parallel LISTENING ticks → only one cmd_pause to the target.

    Without the tick-level lock, both tasks would see
    `_alice_active_pause_sent=False` before either await completes and both
    would issue cmd_pause.  With the lock + flag-set-before-await, the second
    task sees the flag set and short-circuits.
    """
    player = _make_intercept_player()
    player._intercept_active = True
    state, player_state, _ = _state(track_id="X", alice_state="LISTENING")

    pause_started = asyncio.Event()
    pause_release = asyncio.Event()

    async def slow_pause(*_args: Any, **_kwargs: Any) -> None:
        pause_started.set()
        await pause_release.wait()

    player.mass.players.cmd_pause = AsyncMock(side_effect=slow_pause)

    t1 = asyncio.create_task(player._handle_intercept_tick(state, player_state, True))
    await pause_started.wait()
    # T2 fires while T1 is still inside cmd_pause holding the lock.
    t2 = asyncio.create_task(player._handle_intercept_tick(state, player_state, True))
    await asyncio.sleep(0)  # let t2 try to acquire the lock
    pause_release.set()
    await asyncio.gather(t1, t2)

    assert player.mass.players.cmd_pause.await_count == 1


async def test_pause_target_cleanup_runs_when_cmd_pause_raises() -> None:
    """
    If cmd_pause raises, the state-cleanup must still happen.

    Otherwise _intercept_active stays stale and every later WS update retries
    the failing path forever.
    """
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_intercepted_track_id = "X"
    player._last_intercept_time = time.time()
    player.mass.players.cmd_pause = AsyncMock(side_effect=RuntimeError("gone"))

    await player._pause_target(clear_session=True, clear_debounce=True)

    # Despite the raise, both flags were cleared in `finally`.
    assert player._intercept_active is False
    assert player._last_intercepted_track_id is None


async def test_target_dropdown_lists_all_players_except_self() -> None:
    """
    Every registered player except the Station itself shows in the dropdown.

    Intercept dispatches via ``mass.player_queues.play_media(queue_id=...)``
    which routes through the per-player queue, so any registered player is
    a valid target regardless of which playback features it advertises.
    A feature filter here only ends up hiding legitimate targets (AirPlay /
    DLNA / BT bridges that don't expose ``PLAY_MEDIA`` directly).  Mirror
    helpers (volume / pause / seek) gracefully no-op via
    ``UnsupportedFeaturedException`` when the chosen target lacks them.
    Non-audio player types (capture-only sources, lights, displays) are
    the exception: they can never render a track, so they are excluded.
    The list is sorted by display name for predictable UX.
    """
    player = _make_intercept_player()

    full = MagicMock()
    full.player_id = "full"
    full.display_name = "Full"
    full.type = PlayerType.PLAYER
    full.supported_features = {
        PlayerFeature.PLAY_MEDIA,
        PlayerFeature.PAUSE,
        PlayerFeature.VOLUME_SET,
        PlayerFeature.SEEK,
    }
    play_media_only = MagicMock()
    play_media_only.player_id = "minimal"
    play_media_only.display_name = "Minimal"
    play_media_only.type = PlayerType.PLAYER
    play_media_only.supported_features = {PlayerFeature.PLAY_MEDIA}
    no_play_media = MagicMock()
    no_play_media.player_id = "no_play_media"
    no_play_media.display_name = "No Play Media"
    no_play_media.type = PlayerType.PLAYER
    no_play_media.supported_features = {PlayerFeature.PAUSE, PlayerFeature.VOLUME_SET}
    self_player = MagicMock()
    self_player.player_id = player.player_id
    self_player.display_name = "Self"
    self_player.type = PlayerType.PLAYER
    self_player.supported_features = {PlayerFeature.PLAY_MEDIA}
    source_player = MagicMock()
    source_player.player_id = "turntable"
    source_player.display_name = "Turntable"
    source_player.type = PlayerType.SOURCE
    source_player.supported_features = set()
    player.mass.players.all_players = MagicMock(
        return_value=[full, play_media_only, no_play_media, self_player, source_player]
    )

    entries = await YandexStationPlayer.get_config_entries(player)
    target_entry = next(e for e in entries if getattr(e, "key", None) == CONF_INTERCEPT_TARGET)
    listed_ids = [opt.value for opt in target_entry.options]

    # Every non-self player appears, regardless of supported_features,
    # sorted alphabetically by display name (Full, Minimal, No Play Media).
    # The capture-only source player is excluded by type.
    assert listed_ids == ["full", "minimal", "no_play_media"]


async def test_concurrent_mirror_volume_serialised() -> None:
    """
    Back-to-back volume updates must be applied in order.

    Without the tick-level lock, an older volume task could finish after a
    newer one and leave the target stale.  With the lock, the second tick
    blocks until the first finishes — guaranteeing in-order application.
    """
    player = _make_intercept_player()
    player._intercept_active = True
    player._last_intercepted_track_id = "X"
    player._last_intercept_time = time.time()

    applied: list[int] = []
    first_started = asyncio.Event()
    first_release = asyncio.Event()

    async def slow_first(*_args: Any, **kwargs: Any) -> None:  # noqa: ARG001
        applied.append(_args[1])
        first_started.set()
        await first_release.wait()

    async def fast(*_args: Any, **kwargs: Any) -> None:  # noqa: ARG001
        applied.append(_args[1])

    cmds = AsyncMock(side_effect=slow_first)
    player.mass.players.cmd_volume_set = cmds

    state1, ps1, _ = _state(track_id="X", volume=0.3)
    t1 = asyncio.create_task(player._handle_intercept_tick(state1, ps1, True))
    await first_started.wait()
    cmds.side_effect = fast  # next call uses the fast handler
    state2, ps2, _ = _state(track_id="X", volume=0.6)
    t2 = asyncio.create_task(player._handle_intercept_tick(state2, ps2, True))
    await asyncio.sleep(0)
    first_release.set()
    await asyncio.gather(t1, t2)

    # In-order application: 30 then 60 (not the reverse).
    assert applied == [30, 60]
