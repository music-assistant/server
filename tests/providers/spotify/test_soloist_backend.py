"""
Unit tests for the Spotify Soloist single-track playback backend.

The backend spawns one soloist daemon per item and reads its PCM back from a
capture sink; these tests lock down the pure logic around that: lead-silence
trimming, WebSocket-observed track state, event handling, result validation,
paired-session adoption and the prefs-based normalization opt-out. No real
process or PulseAudio is involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import AudioError, LoginFailed

from music_assistant.providers.spotify.backends import soloist as soloist_backend
from music_assistant.providers.spotify.backends.soloist import (
    _BYTES_PER_SECOND,
    _FRAME_BYTES,
    _MAX_LEAD_TRIM_S,
    SoloistSingleTrackBackend,
    _TrackState,
    _trim_lead_silence,
)
from music_assistant.providers.spotify.constants import (
    CONF_SOLOIST_API_KEY,
    CONF_SOLOIST_CONSENT,
    CONF_SOLOIST_SESSION_DIR,
    SOLOIST_DATA_DIR_NAME,
)
from music_assistant.providers.spotify.helpers import soloist_session_present
from music_assistant.providers.spotify.provider import SpotifyProvider
from music_assistant.providers.spotify_connect.soloist.runtime import (
    WS_ADDR_FILE,
    WS_PORT_FILE,
    SoloistAuthState,
    SoloistEvent,
    SoloistPlaybackState,
)


def test_trim_drops_an_all_zero_chunk_within_the_bound() -> None:
    """A pure-silence chunk inside the trim budget is dropped entirely."""
    chunk = bytes(1024)
    assert _trim_lead_silence(chunk, 0) == (b"", 1024)


def test_trim_keeps_frame_alignment_when_audio_starts_mid_chunk() -> None:
    """The trim snaps down to a sample-frame boundary when audio starts mid-chunk."""
    chunk = bytes(13) + b"\x7f" + bytes(50)
    trimmed, skipped = _trim_lead_silence(chunk, 0)
    # 13 leading zero bytes, aligned down to one full 8-byte frame
    assert skipped == 8
    assert skipped % _FRAME_BYTES == 0
    assert trimmed == chunk[8:]


def test_trim_passes_silence_through_once_the_bound_is_exceeded() -> None:
    """A pure-silence chunk past the trim budget is track audio and passes unchanged."""
    bound = int(_MAX_LEAD_TRIM_S * _BYTES_PER_SECOND)
    chunk = bytes(1024)
    assert _trim_lead_silence(chunk, bound) == (chunk, 0)


def test_seek_is_confirmed_only_within_tolerance() -> None:
    """A position report confirms a pending seek only once it lands near the target."""
    state = _TrackState("spotify:track:abc")
    state.seek_target_ms = 60_000
    state.observe_position(57_999)
    assert state.last_position_ms == 57_999
    assert not state.seek_confirmed.is_set()
    # exactly target minus tolerance is close enough
    state.observe_position(58_000)
    assert state.seek_confirmed.is_set()


def test_small_seek_target_is_not_confirmed_by_a_pre_seek_zero_report() -> None:
    """A position 0 report never confirms a small seek target inside the tolerance."""
    state = _TrackState("spotify:track:abc")
    state.seek_target_ms = 1_500
    state.observe_position(0)
    assert not state.seek_confirmed.is_set()
    state.observe_position(1_400)
    assert state.seek_confirmed.is_set()


def test_expected_bytes_math() -> None:
    """The expected PCM byte count follows duration and seek; unknown duration is None."""
    state = _TrackState("spotify:track:abc")
    assert state.expected_bytes(0) is None
    state.duration_ms = 1000
    assert state.expected_bytes(0) == 44100 * 8
    # seeking past the end leaves nothing to deliver
    assert state.expected_bytes(2) == 0


def test_observe_item_tracks_the_requested_uri(tmp_path: Path) -> None:
    """Only the requested URI marks the item seen; a later other URI ends the item."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    backend._observe_item(state, "spotify:track:other", None)
    assert not state.item_seen.is_set()
    assert not state.ended.is_set()
    backend._observe_item(state, "spotify:track:abc", 123_000)
    assert state.item_seen.is_set()
    assert state.duration_ms == 123_000
    # Spotify moved on to another item: everything for the requested one is delivered
    backend._observe_item(state, "spotify:track:next", None)
    assert state.ended.is_set()


async def test_auth_event_records_logout(tmp_path: Path) -> None:
    """An auth_state event with logged_in=False marks the stored session logged out."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    sink = AsyncMock()
    event = SoloistEvent(
        type="auth_state", data=SoloistAuthState(logged_in=False, is_active=False), raw={}
    )
    await backend._handle_event(state, sink, event)
    assert state.logged_out is True


async def test_buffering_gates_the_sink_once_demand_started(tmp_path: Path) -> None:
    """Once PCM demand started, buffering suspends the sink and playing resumes it."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    state.demand_started = True
    sink = AsyncMock()
    await backend._handle_event(state, sink, _playback_event("buffering"))
    sink.suspend.assert_awaited_once()
    sink.resume.assert_not_awaited()
    await backend._handle_event(state, sink, _playback_event("playing"))
    sink.resume.assert_awaited_once()
    assert state.playing_seen is True


async def test_sink_is_not_gated_before_demand_started(tmp_path: Path) -> None:
    """Buffering/playing before PCM demand leave the (still suspended) sink alone."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    sink = AsyncMock()
    await backend._handle_event(state, sink, _playback_event("buffering"))
    await backend._handle_event(state, sink, _playback_event("playing"))
    sink.suspend.assert_not_awaited()
    sink.resume.assert_not_awaited()
    # the latest status is recorded either way, so the stream startup can
    # decide whether resuming the sink is safe yet
    assert state.last_status == "playing"


async def test_failed_sink_control_fails_the_stream(tmp_path: Path) -> None:
    """A failed suspend/resume fails the stream instead of leaking stall silence."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    state.demand_started = True
    sink = AsyncMock()
    sink.suspend.side_effect = RuntimeError("pactl failed")
    await backend._handle_event(state, sink, _playback_event("buffering"))
    assert state.error is not None
    assert "capture sink control failed" in state.error


async def test_expired_build_is_replaced_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit code 10 refreshes the binary bypassing the verify cache and fails the item."""
    backend = _make_backend(tmp_path, {CONF_SOLOIST_API_KEY: "k" * 20, CONF_SOLOIST_CONSENT: True})
    manager = _install_fake_binary_manager(monkeypatch)
    state = _TrackState("spotify:track:abc")
    with pytest.raises(AudioError, match="expired"):
        await backend._evaluate_result(state, _make_proc(10))
    # without force=True the verify cache would hand back the same expired binary
    manager.ensure_fresh.assert_awaited_once_with(True, force=True)


async def test_expired_build_at_spawn_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired build exiting 10 right at spawn triggers the forced refresh too."""
    backend = _make_backend(tmp_path, {CONF_SOLOIST_API_KEY: "k" * 20, CONF_SOLOIST_CONSENT: True})
    manager = _install_fake_binary_manager(monkeypatch)
    state = _TrackState("spotify:track:abc")
    proc = _make_proc(10)
    proc.wait = AsyncMock(return_value=10)
    with pytest.raises(AudioError, match="expired"):
        await backend._await_item_ready(state, proc)
    manager.ensure_fresh.assert_awaited_once_with(True, force=True)


async def test_forced_close_does_not_fail_a_complete_delivery(tmp_path: Path) -> None:
    """A daemon that had to be closed forcefully is not judged by its signal exit code."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    state.playing_seen = True
    state.duration_ms = 200_000
    state.last_position_ms = 195_000
    proc = MagicMock()
    proc.returncode = None
    proc.wait_with_timeout = AsyncMock(side_effect=TimeoutError)

    async def _close() -> None:
        proc.returncode = -2

    proc.close = AsyncMock(side_effect=_close)
    await backend._evaluate_result(state, proc)


async def test_nonzero_exit_fails_the_item(tmp_path: Path) -> None:
    """A nonzero daemon exit code rejects the delivered PCM."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    with pytest.raises(AudioError, match="code 7"):
        await backend._evaluate_result(state, _make_proc(7))


async def test_clean_exit_without_playing_is_rejected(tmp_path: Path) -> None:
    """Exit code 0 is no proof of audio: playback must have been observed."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    with pytest.raises(AudioError, match="never started playing"):
        await backend._evaluate_result(state, _make_proc(0))


async def test_short_delivery_is_rejected_as_incomplete(tmp_path: Path) -> None:
    """A last position far short of the duration rejects the PCM as incomplete."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    state.playing_seen = True
    state.duration_ms = 200_000
    state.last_position_ms = 100_000
    with pytest.raises(AudioError, match="incomplete"):
        await backend._evaluate_result(state, _make_proc(0))


async def test_missing_position_is_rejected_as_incomplete(tmp_path: Path) -> None:
    """Without any position report there is no evidence the item played to its end."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    state.playing_seen = True
    state.duration_ms = 200_000
    with pytest.raises(AudioError, match="incomplete"):
        await backend._evaluate_result(state, _make_proc(0))


async def test_delivery_within_tolerance_passes(tmp_path: Path) -> None:
    """A last position within the incomplete-tolerance of the duration passes."""
    backend = _make_backend(tmp_path)
    state = _TrackState("spotify:track:abc")
    state.playing_seen = True
    state.duration_ms = 200_000
    state.last_position_ms = 195_000
    await backend._evaluate_result(state, _make_proc(0))


async def test_adopt_paired_session_moves_into_the_canonical_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session paired by the setup flow is adopted into the per-instance data dir."""
    storage = tmp_path / "storage"
    pending = storage / "spotify" / "pairing" / "flow1"
    pending.mkdir(parents=True)
    (pending / "session.bin").write_bytes(b"session")
    prov = _make_provider(tmp_path, {CONF_SOLOIST_SESSION_DIR: "spotify/pairing/flow1"})
    update_setup_data = MagicMock()
    monkeypatch.setattr(prov, "_update_setup_data", update_setup_data)
    backend = SoloistSingleTrackBackend(prov)
    await backend._adopt_paired_session()
    canonical = storage / "spotify" / "spotify--test" / SOLOIST_DATA_DIR_NAME
    assert (canonical / "session.bin").read_bytes() == b"session"
    # a copy, not a move: the flow-private source must survive a failed
    # provider load so the setup flow can retry (the flow removes it at its end)
    assert (pending / "session.bin").exists()
    update_setup_data.assert_called_once_with(CONF_SOLOIST_SESSION_DIR, None)


def test_normalization_is_disabled_in_the_prefs_store(tmp_path: Path) -> None:
    """The account's loudness normalization is switched off; other prefs stay untouched."""
    backend = _make_backend(tmp_path)
    prefs = backend._data_dir / "settings" / "Users" / "alice-user" / "prefs"
    prefs.parent.mkdir(parents=True)
    prefs.write_text("audio.crossfade_v2=true\naudio.normalize_v2=true", encoding="utf-8")
    backend._disable_engine_normalization()
    content = prefs.read_text(encoding="utf-8")
    assert content == "audio.crossfade_v2=true\naudio.normalize_v2=false\n"


def test_already_disabled_normalization_is_not_rewritten(tmp_path: Path) -> None:
    """A prefs file that already disables normalization is left byte-identical."""
    backend = _make_backend(tmp_path)
    prefs = backend._data_dir / "settings" / "Users" / "alice-user" / "prefs"
    prefs.parent.mkdir(parents=True)
    # no trailing newline: a rewrite would append one, so identity proves no write
    original = "audio.crossfade_v2=true\naudio.normalize_v2=false"
    prefs.write_text(original, encoding="utf-8")
    backend._disable_engine_normalization()
    assert prefs.read_text(encoding="utf-8") == original


async def test_setup_requires_an_api_key(tmp_path: Path) -> None:
    """Without a stored API key the user must be sent back through the setup flow."""
    backend = _make_backend(tmp_path)
    with pytest.raises(LoginFailed) as err:
        await backend.setup()
    assert err.value.translation_key == "soloist_pairing_required"


async def test_setup_requires_a_paired_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An API key without a paired session also routes back to the setup flow."""
    backend = _make_backend(tmp_path, {CONF_SOLOIST_API_KEY: "k" * 20, CONF_SOLOIST_CONSENT: True})
    _install_fake_binary_manager(monkeypatch)
    with pytest.raises(LoginFailed) as err:
        await backend.setup()
    assert err.value.translation_key == "soloist_pairing_required"


def test_session_present_detection(tmp_path: Path) -> None:
    """Only a dir holding something besides the WS endpoint files counts as paired."""
    data_dir = tmp_path / "soloist-data"
    assert soloist_session_present(data_dir) is False
    data_dir.mkdir()
    (data_dir / WS_ADDR_FILE).write_text("127.0.0.1", encoding="utf-8")
    (data_dir / WS_PORT_FILE).write_text("1234", encoding="utf-8")
    assert soloist_session_present(data_dir) is False
    (data_dir / "session.bin").write_bytes(b"x")
    assert soloist_session_present(data_dir) is True


def _make_provider(tmp_path: Path, setup_data: dict[str, Any] | None = None) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) with the given setup_data."""
    prov = object.__new__(SpotifyProvider)
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(return_value=None)
    config.values = {}
    prov.config = config
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov.available = True
    mass = MagicMock()
    mass.storage_path = str(tmp_path / "storage")
    mass.cache_path = str(tmp_path / "cache")
    # get_setup_value reads the live setup_data blob from the store
    mass.config.get = MagicMock(return_value=setup_data or {})
    mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
    # the store keeps values encrypted; decrypt is an identity map for the test
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    prov.mass = mass
    return prov


def _make_backend(
    tmp_path: Path, setup_data: dict[str, Any] | None = None
) -> SoloistSingleTrackBackend:
    """Return a SoloistSingleTrackBackend on a mocked provider."""
    return SoloistSingleTrackBackend(_make_provider(tmp_path, setup_data))


def _make_proc(returncode: int) -> MagicMock:
    """Return a finished AsyncProcess stand-in with the given exit code."""
    proc = MagicMock()
    proc.close = AsyncMock()
    proc.wait_with_timeout = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    return proc


def _playback_event(status: str) -> SoloistEvent:
    """Return a minimal playback_state event with the given status."""
    return SoloistEvent(type="playback_state", data=SoloistPlaybackState(status=status), raw={})


def _install_fake_binary_manager(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace SoloistBinaryManager in the backend module, returning the instance mock."""
    manager = MagicMock()
    manager.ensure_fresh = AsyncMock(return_value=Path("/fake/soloist"))
    monkeypatch.setattr(soloist_backend, "SoloistBinaryManager", MagicMock(return_value=manager))
    return manager
