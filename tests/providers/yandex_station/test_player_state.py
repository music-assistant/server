"""
Tests for YandexStationPlayer state transitions during external playback.

Covers physical pause detection: when the user presses pause on the speaker
while MA is streaming via ``radio_play``, Glagol reports ``playing=False`` with
``aliceState="IDLE"``. The player must propagate ``PlaybackState.PAUSED`` to MA
instead of silently holding the optimistic PLAYING state.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.yandex_station.player import YandexStationPlayer
from music_assistant.providers.yandex_station.protobuf import loads

if TYPE_CHECKING:
    from music_assistant.models.player import PlayerMedia
    from music_assistant.models.player_provider import PlayerProvider
    from music_assistant.providers.yandex_station.glagol import YandexGlagol


def _make_player() -> YandexStationPlayer:
    """Build a bare player skipping Player.__init__ (avoids MA core deps)."""
    player = YandexStationPlayer.__new__(YandexStationPlayer)
    player._player_id = "test_player"
    player._external_playing = False
    player._external_media = None
    player._external_play_confirmed = False
    vars(player)["_external_stop_observed"] = False
    vars(player)["_audio_client"] = False
    vars(player)["_external_audio_client"] = False
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
    player._attr_current_media = None
    object.__setattr__(player, "set_current_media", lambda **_kwargs: None)
    object.__setattr__(player, "update_state", lambda: None)
    return player


class _StubConfig:
    def get_value(self, key: str, default: object = None) -> object:
        return False


def _disable_voice_control(player: YandexStationPlayer) -> None:
    player._config = _StubConfig()  # type: ignore[assignment]


def _make_play_media_player(
    command_results: list[dict[str, Any]],
) -> tuple[YandexStationPlayer, list[dict[str, Any]]]:
    """Build a player that records the real commands emitted by play_media()."""
    commands: list[dict[str, Any]] = []

    async def send(payload: dict[str, Any]) -> dict[str, Any]:
        commands.append(payload)
        return command_results.pop(0)

    async def resolve_stream_url(_player_id: str, _media: PlayerMedia) -> str:
        return "http://192.168.10.229:8097/single/session/queue/item/test_player.flac"

    player = _make_player()
    provider = cast(
        "PlayerProvider",
        SimpleNamespace(
            mass=SimpleNamespace(streams=SimpleNamespace(resolve_stream_url=resolve_stream_url))
        ),
    )
    player._provider = provider
    player.mass = provider.mass
    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send))
    return player, commands


def _decode_directive(command: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return the external directive name and JSON payload."""
    decoded = loads(base64.b64decode(command["data"]))
    name = decoded[1]
    payload = decoded[2]
    assert isinstance(name, bytes)
    assert isinstance(payload, bytes)
    return name.decode(), json.loads(payload)


def test_glagol_update_enables_audio_client_capability() -> None:
    """The advertised audio client feature selects current-firmware playback."""
    player = _make_player()

    player._on_glagol_update({"supported_features": ["audio_client"], "state": {}})

    assert vars(player)["_audio_client"] is True


def test_invalid_feature_update_preserves_learned_capability() -> None:
    """Partial Glagol responses must not erase a previously learned feature."""
    player = _make_player()
    vars(player)["_audio_client"] = True

    player._on_glagol_update({"supported_features": None, "state": {}})

    assert vars(player)["_audio_client"] is True


async def test_play_media_uses_audio_play_without_preliminary_stop() -> None:
    """Current firmware receives audio_play directly so it can replace the source."""
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    vars(player)["_audio_client"] = True
    media = cast(
        "PlayerMedia",
        SimpleNamespace(
            uri="yandex_music://track/1",
            title="Track",
            artist="Artist",
            duration=180,
            image_url=None,
        ),
    )

    await player.play_media(media)

    assert [command["command"] for command in commands] == ["externalCommandBypass"]
    name, payload = _decode_directive(commands[0])
    assert name == "audio_play"
    assert payload["stream"]["format"] == "MP3"
    assert vars(player)["_external_audio_client"] is True


async def test_audio_play_from_idle_confirms_on_first_playing_state() -> None:
    """An idle Station has already crossed the native playback boundary."""
    player, _ = _make_play_media_player([{"status": "SUCCESS"}])
    _disable_voice_control(player)
    vars(player)["_audio_client"] = True
    media = cast(
        "PlayerMedia",
        SimpleNamespace(
            uri="yandex_music://track/1",
            title="Track",
            artist="Artist",
            duration=180,
            image_url=None,
        ),
    )

    await player.play_media(media)
    update_playback_state = cast("Any", player._update_playback_state)
    update_playback_state(
        playing=True,
        alice_state="IDLE",
        external_media_matches=True,
    )

    assert player._external_play_confirmed is True


async def test_audio_play_attributes_state_while_command_is_pending() -> None:
    """State updates received before the command response belong to the requested media."""
    player, _ = _make_play_media_player([{"status": "SUCCESS"}])
    _disable_voice_control(player)
    object.__setattr__(player._provider, "config", _StubConfig())
    vars(player)["_audio_client"] = True
    player._attr_playback_state = PlaybackState.PLAYING
    media = cast(
        "PlayerMedia",
        SimpleNamespace(
            uri="yandex_music://track/2",
            title="New Track",
            artist="Artist",
            duration=180,
            image_url=None,
        ),
    )

    async def send_with_stale_updates(_payload: dict[str, Any]) -> dict[str, Any]:
        for playing in (False, True):
            player._on_glagol_update(
                {
                    "supported_features": ["audio_client"],
                    "state": {
                        "playing": playing,
                        "aliceState": "IDLE",
                        "playerState": {
                            "progress": 120,
                            "duration": 180,
                            "title": "Old Track",
                        },
                    },
                }
            )
        return {"status": "SUCCESS"}

    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send_with_stale_updates))

    await player.play_media(media)

    assert player._external_play_confirmed is False


async def test_play_media_falls_back_to_legacy_radio_play() -> None:
    """Old firmware keeps the legacy payload and does not receive a native stop."""
    player, commands = _make_play_media_player([{"status": "ERROR"}])
    media = cast(
        "PlayerMedia",
        SimpleNamespace(
            uri="yandex_music://track/1",
            title="Track",
            artist="Artist",
            duration=180,
            image_url=None,
        ),
    )

    with pytest.raises(PlayerCommandFailed, match="radio_play"):
        await player.play_media(media)

    assert [command["command"] for command in commands] == ["externalCommandBypass"]
    name, payload = _decode_directive(commands[0])
    assert name == "radio_play"
    assert payload["streamUrl"].endswith(".flac")
    assert vars(player)["_external_audio_client"] is False


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


def test_stale_native_playing_does_not_confirm_external_playback() -> None:
    """Native playing=True before its stop is observed cannot confirm the stream."""
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = False

    player._update_playback_state(playing=True, alice_state="IDLE")

    assert player._external_play_confirmed is False
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_powered is True


def test_external_playing_confirms_after_native_stop_is_observed() -> None:
    """A new playing=True state may confirm only after the native source stopped."""
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True

    player._update_playback_state(playing=False, alice_state="IDLE")
    player._update_playback_state(playing=True, alice_state="IDLE")

    assert player._external_play_confirmed is True


def test_audio_play_ignores_stale_state_during_track_handoff() -> None:
    """Old-track state must not turn new-track startup into a physical pause."""
    player = _make_player()
    _disable_voice_control(player)
    player._provider = cast("PlayerProvider", SimpleNamespace(config=_StubConfig()))
    player._external_playing = True
    vars(player)["_external_audio_client"] = True
    requested_media = cast(
        "PlayerMedia",
        SimpleNamespace(
            uri="yandex_music://track/2",
            title="New Track",
            artist="Artist",
            duration=180,
            image_url=None,
        ),
    )
    player._external_media = requested_media

    def update(*, title: str, playing: bool) -> None:
        player._on_glagol_update(
            {
                "supported_features": ["audio_client"],
                "state": {
                    "playing": playing,
                    "aliceState": "IDLE",
                    "playerState": {"progress": 0, "duration": 180, "title": title},
                },
            }
        )

    update(title="Old Track", playing=True)
    assert vars(player)["_external_play_confirmed"] is False

    update(title="New Track", playing=False)
    assert player._external_playing is True
    assert player._external_media is requested_media
    assert player._needs_replay is False

    update(title="New Track", playing=True)

    assert player._external_play_confirmed is True
    assert player._attr_playback_state == PlaybackState.PLAYING


def test_audio_play_requires_a_state_boundary_before_title_confirmation() -> None:
    """A same-title old track cannot confirm the new audio_play session."""
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    vars(player)["_external_audio_client"] = True
    update_playback_state = cast("Any", player._update_playback_state)

    update_playback_state(
        playing=True,
        alice_state="IDLE",
        external_media_matches=True,
    )

    assert player._external_play_confirmed is False

    update_playback_state(
        playing=False,
        alice_state="IDLE",
        external_media_matches=True,
    )
    update_playback_state(
        playing=True,
        alice_state="IDLE",
        external_media_matches=True,
    )

    assert player._external_play_confirmed is True


def test_audio_play_end_of_track_sets_idle_without_replay() -> None:
    """A confirmed stream ending at its duration must let MA advance the queue."""
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = True
    vars(player)["_external_audio_client"] = True
    player._attr_playback_state = PlaybackState.PLAYING
    update_playback_state = cast("Any", player._update_playback_state)

    update_playback_state(
        playing=False,
        alice_state="IDLE",
        external_media_matches=True,
        progress=180,
        duration=180,
    )

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._external_playing is False
    assert player._needs_replay is False


def test_audio_play_uses_reported_progress() -> None:
    """Current firmware progress replaces the legacy optimistic clock."""
    player = _make_player()
    _disable_voice_control(player)
    player._provider = cast("PlayerProvider", SimpleNamespace(config=_StubConfig()))
    player._external_playing = True
    vars(player)["_external_audio_client"] = True
    player._external_media = cast(
        "PlayerMedia",
        SimpleNamespace(
            uri="yandex_music://track/1",
            title="Track",
            artist="Artist",
            duration=180,
            image_url=None,
        ),
    )
    player._attr_elapsed_time = 0

    player._on_glagol_update(
        {
            "supported_features": ["audio_client"],
            "state": {
                "playing": True,
                "aliceState": "IDLE",
                "playerState": {"progress": 12.5, "duration": 180, "title": "Track"},
            },
        }
    )

    assert player._attr_elapsed_time == 12.5


async def test_pause_uses_native_stop_for_audio_play_session() -> None:
    """The invalid radio URL workaround must not be used on current firmware."""
    player = _make_player()
    commands: list[dict[str, Any]] = []

    async def send(command: dict[str, Any]) -> dict[str, Any]:
        commands.append(command)
        return {"status": "SUCCESS"}

    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send))
    player._external_playing = True
    vars(player)["_external_audio_client"] = True

    await player.pause()

    assert commands == [{"command": "stop"}]
    assert vars(player)["_external_audio_client"] is False
    assert player._needs_replay is True


async def test_stop_uses_native_stop_for_audio_play_session() -> None:
    """Stopping audio_play must address the native audio client."""
    player = _make_player()
    commands: list[dict[str, Any]] = []

    async def send(command: dict[str, Any]) -> dict[str, Any]:
        commands.append(command)
        return {"status": "SUCCESS"}

    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send))
    player._external_playing = True
    vars(player)["_external_audio_client"] = True

    await player.stop()

    assert commands == [{"command": "stop"}]
    assert vars(player)["_external_audio_client"] is False
    assert player._needs_replay is False


async def test_pause_retains_legacy_radio_play_stop_workaround() -> None:
    """Old firmware still needs its bypass stream replaced by an invalid URL."""
    player = _make_player()
    commands: list[dict[str, Any]] = []

    async def send(command: dict[str, Any]) -> dict[str, Any]:
        commands.append(command)
        return {"status": "SUCCESS"}

    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send))
    player._external_playing = True

    await player.pause()

    name, payload = _decode_directive(commands[0])
    assert name == "radio_play"
    assert payload == {"streamUrl": "http://0.0.0.0/stop.flac"}


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
