"""
Tests for YandexStationPlayer state transitions driven by the Station's reports.

Covers Music Assistant streams (startup, completion, a stop on the speaker),
the Station's own playback, transport commands and voice control.
"""
# Mypy resolves this package through the installed Music Assistant, whose bundled
# copy of the provider lags this repository, and MagicMock stands in for the core
# controllers. The disabled codes cover both.
# mypy: disable-error-code="attr-defined,method-assign,call-arg,assignment,comparison-overlap"

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import PlayerMedia as MediaItem

from music_assistant.providers.yandex_station.alice import AliceTracker
from music_assistant.providers.yandex_station.player import (
    STATION_SOURCE_ID,
    VOICE_RESUME_DELAY,
    YandexStationPlayer,
)
from music_assistant.providers.yandex_station.protobuf import loads
from music_assistant.providers.yandex_station.voice import VoiceRecovery, VoiceRecoveryPhase

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
    vars(player)["_external_play_generation"] = 0
    vars(player)["_resolving_play"] = None
    vars(player)["_stops_in_flight"] = 0
    player._external_play_confirmed = False
    vars(player)["_external_stop_observed"] = False
    vars(player)["_audio_client"] = False
    vars(player)["_external_audio_client"] = False
    player._alice = AliceTracker()
    player._voice = VoiceRecovery()
    player._station_track_id = ""
    vars(player)["_station_playback_starts"] = 0
    player._intercept_active = False
    player._last_intercepted_track_id = None
    player._last_intercept_time = 0.0
    player._last_mirrored_volume = None
    player._last_progress = 0
    player._last_progress_wall = 0.0
    vars(player)["_station_unmuted_for_phrase"] = False
    player._saved_station_volume = None
    player._station_muted_by_intercept = False
    player._attr_playback_state = PlaybackState.IDLE
    player._attr_powered = False
    player._attr_volume_level = 0
    player._attr_current_media = None
    player._attr_active_source = None
    player._attr_supported_features = {PlayerFeature.PAUSE}
    player.mass = MagicMock()
    _disable_voice_control(player)
    object.__setattr__(player, "set_current_media", lambda **_kwargs: None)
    object.__setattr__(player, "update_state", lambda: None)
    return player


def _report(
    player: YandexStationPlayer,
    *,
    playing: bool,
    alice_state: str = "IDLE",
    stream_reported: bool | None = None,
    progress: float = 0,
    duration: float = 0,
    native_track_id: str = "",
) -> None:
    """Feed one Station report through Alice's tracker, as the WebSocket handler does."""
    player._update_playback_state(
        playing,
        player._alice.feed(alice_state),
        stream_reported,
        progress,
        duration,
        native_track_id,
    )


class _StubConfig:
    def get_value(self, key: str, default: object = None) -> object:
        return False


def _disable_voice_control(player: YandexStationPlayer) -> None:
    player._config = _StubConfig()


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
    player.mass.streams = SimpleNamespace(resolve_stream_url=resolve_stream_url)
    player._provider = cast(
        "PlayerProvider", SimpleNamespace(mass=player.mass, linked_music_instance=None)
    )
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
    media = MediaItem(
        uri="yandex_music://track/1",
        title="Track",
        artist="Artist",
        duration=180,
        image_url=None,
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
    media = MediaItem(
        uri="yandex_music://track/1",
        title="Track",
        artist="Artist",
        duration=180,
        image_url=None,
    )

    await player.play_media(media)
    _report(
        player,
        playing=True,
        alice_state="IDLE",
        stream_reported=True,
    )

    assert player._external_play_confirmed is True


async def test_audio_play_attributes_state_while_command_is_pending() -> None:
    """State updates received before the command response belong to the requested media."""
    player, _ = _make_play_media_player([{"status": "SUCCESS"}])
    _disable_voice_control(player)
    object.__setattr__(player._provider, "config", _StubConfig())
    vars(player)["_audio_client"] = True
    player._attr_playback_state = PlaybackState.PLAYING
    media = MediaItem(
        uri="yandex_music://track/2",
        title="New Track",
        artist="Artist",
        duration=180,
        image_url=None,
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


@pytest.mark.parametrize(
    ("progress", "expected_state"),
    [(30, PlaybackState.IDLE), (180, PlaybackState.IDLE)],
    ids=["stopped", "finished"],
)
async def test_terminal_state_during_pending_play_command_is_preserved(
    progress: int, expected_state: PlaybackState
) -> None:
    """A late command acknowledgement must not undo a terminal state."""
    player, _ = _make_play_media_player([{"status": "SUCCESS"}])
    _disable_voice_control(player)
    object.__setattr__(player._provider, "config", _StubConfig())
    vars(player)["_audio_client"] = True
    media = MediaItem(
        uri="yandex_music://track/3",
        title="Requested Track",
        artist="Artist",
        duration=180,
        image_url=None,
    )

    async def send_with_pause(_payload: dict[str, Any]) -> dict[str, Any]:
        for playing in (True, False):
            player._on_glagol_update(
                {
                    "supported_features": ["audio_client"],
                    "state": {
                        "playing": playing,
                        "aliceState": "IDLE",
                        "playerState": {
                            "id": "",
                            "progress": progress,
                            "duration": 180,
                            "title": "Requested Track",
                        },
                    },
                }
            )
        return {"status": "SUCCESS"}

    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send_with_pause))

    await player.play_media(media)

    assert player._attr_playback_state == expected_state


@pytest.mark.parametrize("first_fails", [False, True])
async def test_late_play_command_response_preserves_newer_request(first_fails: bool) -> None:
    """A late response cannot publish or clear a newer playback request."""
    player, _ = _make_play_media_player([{"status": "SUCCESS"}])
    first_media = MediaItem(
        uri="yandex_music://track/old",
        title="Old Track",
        artist="Artist",
        duration=180,
        image_url=None,
    )
    new_media = MediaItem(
        uri="yandex_music://track/new",
        title="New Track",
        artist="Artist",
        duration=180,
        image_url=None,
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    published_media: list[str] = []
    object.__setattr__(
        player,
        "set_current_media",
        lambda **kwargs: published_media.append(kwargs["uri"]),
    )

    async def send_in_order(_payload: dict[str, Any]) -> dict[str, Any]:
        if not first_started.is_set():
            first_started.set()
            await release_first.wait()
            return {"status": "ERROR" if first_fails else "SUCCESS"}
        return {"status": "SUCCESS"}

    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send_in_order))
    first_task = asyncio.create_task(player.play_media(first_media))
    await asyncio.wait_for(first_started.wait(), 1)
    await player.play_media(new_media)
    release_first.set()
    if first_fails:
        with pytest.raises(PlayerCommandFailed):
            await first_task
    else:
        await first_task

    assert published_media == ["yandex_music://track/new"]
    assert player._external_media is new_media
    assert player._external_playing is True


@pytest.mark.parametrize("audio_client", [False, True])
async def test_late_stream_resolution_preserves_newer_request(audio_client: bool) -> None:
    """A superseded URL resolution cannot send or publish the older track."""
    player, commands = _make_play_media_player([{"status": "SUCCESS"}] * 2)
    vars(player)["_audio_client"] = audio_client
    first_media = MediaItem(uri="old", title="Old Track", artist="", duration=180, image_url=None)
    new_media = MediaItem(uri="new", title="New Track", artist="", duration=180, image_url=None)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    published_media: list[str] = []
    object.__setattr__(
        player,
        "set_current_media",
        lambda **kwargs: published_media.append(kwargs["uri"]),
    )

    async def resolve_stream_url(_player_id: str, media: PlayerMedia) -> str:
        if media is first_media:
            first_started.set()
            await release_first.wait()
        return f"http://192.168.1.2:8097/{media.uri}.wav"

    object.__setattr__(player.mass.streams, "resolve_stream_url", resolve_stream_url)
    first_task = asyncio.create_task(player.play_media(first_media))
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        await player.play_media(new_media)
    finally:
        release_first.set()
        await first_task

    assert len(commands) == 1
    assert published_media == ["new"]
    assert player._external_media is new_media
    assert player._attr_playback_state == PlaybackState.PLAYING


@pytest.mark.parametrize(
    ("command", "expected_state"),
    [("pause", PlaybackState.IDLE), ("stop", PlaybackState.IDLE)],
)
async def test_transport_command_discards_pending_stream_resolution(
    command: str, expected_state: PlaybackState
) -> None:
    """A URL resolved after pause or stop must not restart playback."""
    player, commands = _make_play_media_player([{"status": "SUCCESS"}] * 2)
    media = MediaItem(uri="track", title="Track", artist="", duration=180, image_url=None)
    resolution_started = asyncio.Event()
    release_resolution = asyncio.Event()
    published_media: list[str] = []
    object.__setattr__(
        player,
        "set_current_media",
        lambda **kwargs: published_media.append(kwargs["uri"]),
    )

    async def resolve_stream_url(_player_id: str, _media: PlayerMedia) -> str:
        resolution_started.set()
        await release_resolution.wait()
        return "http://192.168.1.2:8097/track.wav"

    object.__setattr__(player.mass.streams, "resolve_stream_url", resolve_stream_url)
    task = asyncio.create_task(player.play_media(media))
    try:
        await asyncio.wait_for(resolution_started.wait(), 1)
        await getattr(player, command)()
    finally:
        release_resolution.set()
        await task

    assert commands == [{"command": "stop"}]
    assert published_media == []
    assert player._attr_playback_state == expected_state
    assert player._external_media is None


class _VoiceControlConfig:
    def get_value(self, key: str, default: object = None) -> object:
        return True


@pytest.mark.parametrize(
    ("voice_control", "alice_state"),
    [(False, "IDLE"), (True, "LISTENING")],
    ids=["physical_pause", "voice_interrupt"],
)
async def test_station_interruption_discards_pending_stream_resolution(
    voice_control: bool, alice_state: str
) -> None:
    """A URL resolved after a physical pause or Alice interruption must not start playback."""
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    if voice_control:
        player._config = _VoiceControlConfig()
    else:
        _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = True
    player._attr_playback_state = PlaybackState.PLAYING
    media = MediaItem(uri="next", title="Next Track", artist="", duration=180, image_url=None)
    resolution_started = asyncio.Event()
    release_resolution = asyncio.Event()
    published_media: list[str] = []
    object.__setattr__(
        player,
        "set_current_media",
        lambda **kwargs: published_media.append(kwargs["uri"]),
    )

    async def resolve_stream_url(_player_id: str, _media: PlayerMedia) -> str:
        resolution_started.set()
        await release_resolution.wait()
        return "http://192.168.1.2:8097/next.wav"

    object.__setattr__(player.mass.streams, "resolve_stream_url", resolve_stream_url)
    task = asyncio.create_task(player.play_media(media))
    try:
        await asyncio.wait_for(resolution_started.wait(), 1)
        _report(player, playing=False, alice_state=alice_state)
    finally:
        release_resolution.set()
        await task

    assert commands == []
    assert published_media == []
    # Alice's interruption waits for her answer as paused, so a user's stop reaches us.
    assert player._attr_playback_state == (
        PlaybackState.PAUSED if voice_control else PlaybackState.IDLE
    )
    assert player._external_media is None
    assert (player._voice.phase == VoiceRecoveryPhase.AWAITING_ANSWER) is voice_control


async def test_voice_activation_without_pending_request_keeps_native_state() -> None:
    """Alice activation with no pending request leaves an idle station untouched."""
    player = _make_player()
    player._config = _VoiceControlConfig()

    _report(player, playing=False, alice_state="LISTENING")

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._voice.phase == VoiceRecoveryPhase.NONE


def _voice_tick(
    player: YandexStationPlayer, *, playing: bool, alice_state: str, track_id: str
) -> None:
    """Feed one Glagol state update, tracking the previous Alice state like the WS loop."""
    _report(player, playing=playing, alice_state=alice_state, native_track_id=track_id)


@pytest.mark.parametrize("audio_client", [False, True])
async def test_cancelled_play_command_clears_active_external_media(audio_client: bool) -> None:
    """Cancelling a pending send clears its media and propagates cancellation."""
    player, _ = _make_play_media_player([])
    vars(player)["_audio_client"] = audio_client
    media = MediaItem(
        uri="yandex_music://track/cancelled",
        title="Cancelled Track",
        artist="Artist",
        duration=180,
        image_url=None,
    )
    send_started = asyncio.Event()
    published_media: list[str] = []
    object.__setattr__(
        player,
        "set_current_media",
        lambda **kwargs: published_media.append(kwargs["uri"]),
    )

    async def pending_send(_payload: dict[str, Any]) -> dict[str, Any]:
        send_started.set()
        await asyncio.Event().wait()
        return {"status": "SUCCESS"}

    player.glagol = cast("YandexGlagol", SimpleNamespace(send=pending_send))
    task = asyncio.create_task(player.play_media(media))
    await asyncio.wait_for(send_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert player._external_media is None
    assert player._external_playing is False
    assert vars(player)["_external_audio_client"] is False
    assert player._external_play_confirmed is False
    assert vars(player)["_external_stop_observed"] is False
    assert published_media == []


async def test_cancelled_old_play_command_preserves_newer_request() -> None:
    """Cancelling an older pending send cannot clear its successful replacement."""
    player, _ = _make_play_media_player([])
    vars(player)["_audio_client"] = True
    first_media = MediaItem(
        uri="yandex_music://track/old",
        title="Old Track",
        artist="Artist",
        duration=180,
        image_url=None,
    )
    new_media = MediaItem(
        uri="yandex_music://track/new",
        title="New Track",
        artist="Artist",
        duration=180,
        image_url=None,
    )
    first_started = asyncio.Event()
    published_media: list[str] = []
    object.__setattr__(
        player,
        "set_current_media",
        lambda **kwargs: published_media.append(kwargs["uri"]),
    )

    async def send_in_order(_payload: dict[str, Any]) -> dict[str, Any]:
        if not first_started.is_set():
            first_started.set()
            await asyncio.Event().wait()
        return {"status": "SUCCESS"}

    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send_in_order))
    first_task = asyncio.create_task(player.play_media(first_media))
    await asyncio.wait_for(first_started.wait(), 1)
    await player.play_media(new_media)
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task

    assert player._external_media is new_media
    assert player._external_playing is True
    assert vars(player)["_external_audio_client"] is True
    assert published_media == ["yandex_music://track/new"]


async def test_invalid_stream_url_leaves_no_external_session() -> None:
    """A stream URL the payload builder rejects sends nothing and starts no session."""
    player, commands = _make_play_media_player([])
    vars(player)["_audio_client"] = True

    async def resolve_stream_url(_player_id: str, _media: PlayerMedia) -> str:
        return "http://[::1/item.flac"

    object.__setattr__(player.mass.streams, "resolve_stream_url", resolve_stream_url)
    media = MediaItem(uri="track", title="Track", artist=None)

    with pytest.raises(ValueError, match="IPv6"):
        await player.play_media(media)

    assert commands == []
    assert player._external_playing is False
    assert player._external_media is None
    assert player._attr_playback_state == PlaybackState.IDLE


async def test_play_media_falls_back_to_legacy_radio_play() -> None:
    """Old firmware keeps the legacy payload and does not receive a native stop."""
    player, commands = _make_play_media_player([{"status": "ERROR"}])
    media = MediaItem(
        uri="yandex_music://track/1",
        title="Track",
        artist="Artist",
        duration=180,
        image_url=None,
    )

    with pytest.raises(PlayerCommandFailed, match="radio_play"):
        await player.play_media(media)

    assert [command["command"] for command in commands] == ["externalCommandBypass"]
    name, payload = _decode_directive(commands[0])
    assert name == "radio_play"
    assert payload["streamUrl"].endswith(".flac")
    assert vars(player)["_external_audio_client"] is False


# ── External playback: physical pause detection ────────────────────


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

    _report(player, playing=False, alice_state="IDLE")

    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._external_playing is True


def test_stale_native_playing_does_not_confirm_external_playback() -> None:
    """Native playing=True before its stop is observed cannot confirm the stream."""
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = False

    _report(player, playing=True, alice_state="IDLE")

    assert player._external_play_confirmed is False
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_powered is True


def test_external_playing_confirms_after_native_stop_is_observed() -> None:
    """A new playing=True state may confirm only after the native source stopped."""
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True

    _report(player, playing=False, alice_state="IDLE")
    _report(player, playing=True, alice_state="IDLE")

    assert player._external_play_confirmed is True


def _station_report(player: YandexStationPlayer, *, track_id: str, playing: bool) -> None:
    """Feed a report the way the Station sends it: its own track id, empty for our stream."""
    player._on_glagol_update(
        {
            "supported_features": ["audio_client"],
            "state": {
                "playing": playing,
                "aliceState": "IDLE",
                "playerState": {"id": track_id, "progress": 0, "duration": 180, "title": "T"},
            },
        }
    )


def test_audio_play_over_station_playback_is_confirmed_by_its_empty_track_id() -> None:
    """The Station's own track keeps its id until our stream, with an empty id, starts."""
    player = _make_player()
    player._provider = cast(
        "PlayerProvider", SimpleNamespace(config=_StubConfig(), linked_music_instance=None)
    )
    player._attr_playback_state = PlaybackState.PLAYING
    player._start_external_session(_media(), audio_client=True)

    _station_report(player, track_id="60112", playing=True)
    assert vars(player)["_external_play_confirmed"] is False
    assert player._external_playing is True

    _station_report(player, track_id="", playing=True)

    assert player._external_play_confirmed is True
    assert player._attr_playback_state == PlaybackState.PLAYING


def test_audio_play_after_another_ma_stream_needs_a_stopped_report() -> None:
    """
    Two MA streams both report an empty track id.

    The new one is only attributed after the Station reported a stop, so the
    old stream still playing cannot confirm it, and its end is not a user stop.
    """
    player = _make_player()
    player._provider = cast(
        "PlayerProvider", SimpleNamespace(config=_StubConfig(), linked_music_instance=None)
    )
    _start_confirmed_stream(player)
    player._start_external_session(_media("yandex_music://track/2", "Next"), audio_client=True)

    _station_report(player, track_id="", playing=True)
    assert vars(player)["_external_play_confirmed"] is False

    _station_report(player, track_id="", playing=False)
    assert player._external_playing is True

    _station_report(player, track_id="", playing=True)
    assert player._external_play_confirmed is True


def test_audio_play_end_of_track_sets_idle_without_replay() -> None:
    """A confirmed stream ending at its duration must let MA advance the queue."""
    player = _make_player()
    _disable_voice_control(player)
    player._external_playing = True
    player._external_play_confirmed = True
    vars(player)["_external_audio_client"] = True
    player._attr_playback_state = PlaybackState.PLAYING
    _report(
        player,
        playing=False,
        alice_state="IDLE",
        stream_reported=True,
        progress=180,
        duration=180,
    )

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._external_playing is False


def test_audio_play_uses_reported_progress() -> None:
    """Current firmware progress replaces the legacy optimistic clock."""
    player = _make_player()
    _disable_voice_control(player)
    player._provider = cast(
        "PlayerProvider", SimpleNamespace(config=_StubConfig(), linked_music_instance=None)
    )
    player._external_playing = True
    vars(player)["_external_audio_client"] = True
    player._external_media = MediaItem(
        uri="yandex_music://track/1",
        title="Track",
        artist="Artist",
        duration=180,
        image_url=None,
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


# ── Transport: the Station's reports decide the state ─────────────────


def _media(uri: str = "yandex_music://track/1", title: str = "Track") -> PlayerMedia:
    return MediaItem(uri=uri, title=title, artist="Artist", duration=180, image_url=None)


def _start_confirmed_stream(player: YandexStationPlayer, *, audio_client: bool = True) -> None:
    """Put the player in a Music Assistant stream the Station confirmed playing."""
    player._start_external_session(_media(), audio_client=audio_client)
    player._external_play_confirmed = True
    player._attr_playback_state = PlaybackState.PLAYING


def _record_commands(
    player: YandexStationPlayer, outcome: str = "success", *reports: dict[str, Any]
) -> list[dict[str, Any]]:
    """Answer commands with ``outcome`` after delivering ``reports`` while they are pending."""
    commands: list[dict[str, Any]] = []

    async def send(command: dict[str, Any]) -> dict[str, Any]:
        commands.append(command)
        for report in reports:
            _report(player, **report)
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome == "timeout":
            return {"error": "timeout"}
        return {"status": "SUCCESS"}

    player.glagol = cast("YandexGlagol", SimpleNamespace(send=send))
    return commands


async def test_pause_is_advertised_only_while_no_ma_stream_plays() -> None:
    """The Station cannot pause an MA stream, so the core stops it instead."""
    player, _ = _make_play_media_player([{"status": "SUCCESS"}])
    vars(player)["_audio_client"] = True

    await player.play_media(_media())

    assert PlayerFeature.PAUSE not in player._attr_supported_features
    assert player._attr_active_source is None

    player._external_play_confirmed = True
    _report(player, playing=False, stream_reported=True, progress=30, duration=180)

    assert PlayerFeature.PAUSE in player._attr_supported_features
    assert player._external_playing is False


def test_stream_stopped_on_the_speaker_is_left_to_the_core() -> None:
    """
    A stream the user stops on the Station is gone; the player reports IDLE.

    The core resumes the queue on the next play. A next track MA was preparing
    must not start either.
    """
    player = _make_player()
    _start_confirmed_stream(player)

    _report(player, playing=False, stream_reported=True, progress=30, duration=180)

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._external_playing is False
    assert vars(player)["_external_play_generation"] == 1


def test_finished_stream_lets_the_queue_advance() -> None:
    """A stream that played to its end does not drop the next track MA prepares."""
    player = _make_player()
    _start_confirmed_stream(player)

    _report(player, playing=False, stream_reported=True, progress=180, duration=180)

    assert player._attr_playback_state == PlaybackState.IDLE
    assert vars(player)["_external_play_generation"] == 0


@pytest.mark.parametrize(
    ("audio_client", "expected_command"),
    [(True, "stop"), (False, "radio_play")],
    ids=["audio_play", "legacy_radio_play"],
)
@pytest.mark.parametrize("command", ["pause", "stop"])
async def test_ma_stream_stops_with_the_firmware_stop_command(
    command: str, audio_client: bool, expected_command: str
) -> None:
    """pause() and stop() end an MA stream; legacy firmware needs an invalid radio URL."""
    player = _make_player()
    _start_confirmed_stream(player, audio_client=audio_client)
    commands = _record_commands(player)

    await getattr(player, command)()

    assert len(commands) == 1
    if expected_command == "stop":
        assert commands[0] == {"command": "stop"}
    else:
        name, payload = _decode_directive(commands[0])
        assert name == "radio_play"
        assert payload["streamUrl"] == "http://0.0.0.0/stop.flac"
    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._external_playing is False
    assert PlayerFeature.PAUSE in player._attr_supported_features


@pytest.mark.parametrize("outcome", ["timeout", "cancelled"])
@pytest.mark.parametrize("command", ["pause", "stop"])
async def test_command_that_does_not_reach_the_station_changes_nothing(
    command: str, outcome: str
) -> None:
    """A rejected or cancelled stop leaves the stream the Station keeps playing."""
    player = _make_player()
    _start_confirmed_stream(player)
    _record_commands(player, outcome, {"playing": True, "stream_reported": True})

    with pytest.raises(PlayerCommandFailed if outcome == "timeout" else asyncio.CancelledError):
        await getattr(player, command)()

    assert player._external_playing is True
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert PlayerFeature.PAUSE not in player._attr_supported_features


async def test_stop_confirmed_with_the_response_ends_the_stream_once() -> None:
    """A stopped report delivered with the response and the response agree on IDLE."""
    player = _make_player()
    _start_confirmed_stream(player)
    _record_commands(
        player,
        "success",
        {"playing": False, "stream_reported": True, "progress": 30, "duration": 180},
    )

    await player.stop()

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._external_playing is False


def test_native_playback_is_reported_as_the_station_source() -> None:
    """The Station's own playback takes over from the MA queue and can be paused."""
    player = _make_player()

    _report(player, playing=True, native_track_id="yandex-1")

    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_active_source == STATION_SOURCE_ID

    _report(player, playing=False, native_track_id="yandex-1")

    assert player._attr_playback_state == PlaybackState.PAUSED


async def test_play_resumes_the_station_without_touching_the_queue() -> None:
    """Resuming an MA queue is the core's decision, not the player's."""
    player = _make_player()
    commands = _record_commands(player)

    await player.play()

    assert commands == [{"command": "play"}]
    player.mass.player_queues.resume.assert_not_called()


async def test_native_stop_sets_idle() -> None:
    """stop() of the Station's own playback leaves it idle, not paused."""
    player = _make_player()
    _report(player, playing=True, native_track_id="yandex-1")
    _record_commands(player, "success", {"playing": False, "native_track_id": "yandex-1"})

    await player.stop()

    assert player._attr_playback_state == PlaybackState.IDLE


# ── Voice control ─────────────────────────────────────────────────────


def _enable_voice_control(player: YandexStationPlayer) -> None:
    player._config = _VoiceControlConfig()


def test_voice_request_over_ducked_stream_keeps_the_stream() -> None:
    """Alice only ducks the stream while she answers; the session stays."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)

    for alice_state in ("LISTENING", "BUSY", "NONE", "LISTENING", "IDLE"):
        _report(player, playing=True, alice_state=alice_state, stream_reported=True)

    assert player._external_playing is True
    assert player._attr_playback_state == PlaybackState.PLAYING
    player.mass.call_later.assert_not_called()


def test_voice_request_that_stops_the_stream_resumes_it_after_the_answer() -> None:
    """The stream Alice stopped is resumed through the core once she has answered."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)

    _report(player, playing=False, alice_state="LISTENING")

    assert player._attr_playback_state == PlaybackState.PAUSED
    assert player._external_playing is False
    assert vars(player)["_external_play_generation"] == 1

    for alice_state in ("BUSY", "NONE", "LISTENING", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)

    player.mass.call_later.assert_called_once_with(
        VOICE_RESUME_DELAY,
        player._resume_after_voice,
        task_id=player._voice_resume_task_id,
    )


async def test_resume_after_voice_resumes_the_ma_queue_through_the_core() -> None:
    """The auto-resume asks the core to resume this player's queue."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)
    player.mass.players.cmd_resume = AsyncMock()
    for alice_state in ("LISTENING", "NONE", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)

    await player._resume_after_voice()

    player.mass.players.cmd_resume.assert_awaited_once_with(
        player.player_id, source=player.player_id
    )


def test_silent_voice_request_leaves_the_stream_stopped() -> None:
    """A request Alice did not answer, such as "Alice, stop", does not resume."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)

    for alice_state in ("LISTENING", "BUSY", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)

    player.mass.call_later.assert_not_called()
    assert player._voice.phase == VoiceRecoveryPhase.NONE


def test_track_alice_was_asked_for_replaces_the_stream() -> None:
    """Native playback that starts during the request means Alice was asked for it."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)

    _report(player, playing=False, alice_state="LISTENING")
    _report(player, playing=True, alice_state="NONE", native_track_id="yandex-2")
    _report(player, playing=True, alice_state="IDLE", native_track_id="yandex-2")

    player.mass.call_later.assert_not_called()
    assert player._attr_active_source == STATION_SOURCE_ID


def test_wake_word_less_stop_leaves_the_stream_stopped() -> None:
    """A "стоп" without the wake word never leaves IDLE: a plain stop, no resume."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)

    _report(player, playing=False, stream_reported=True, progress=30, duration=180)

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._voice.phase == VoiceRecoveryPhase.NONE


def test_server_action_phrase_keeps_the_stream() -> None:
    """A phrase spoken through ``say`` only ducks the stream."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)

    for alice_state in ("NONE", "NONE", "IDLE"):
        _report(player, playing=True, alice_state=alice_state, stream_reported=True)

    assert player._external_playing is True
    player.mass.call_later.assert_not_called()


def test_stop_on_the_speaker_during_a_phrase_ends_the_stream() -> None:
    """A phrase is not a request: a stop reported meanwhile is a plain stop."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)

    _report(player, playing=False, alice_state="NONE", progress=30, duration=180)

    assert player._external_playing is False
    assert player._voice.phase == VoiceRecoveryPhase.NONE


async def test_alice_request_defers_a_pending_play_media() -> None:
    """
    A track MA asks for while Alice handles a request starts once she is done.

    The Station keeps its own track ducked through the request; that track
    continuing is not a new one Alice was asked for.
    """
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    _enable_voice_control(player)
    _report(player, playing=True, native_track_id="yandex-1")
    _report(player, playing=True, alice_state="LISTENING", native_track_id="yandex-1")

    await player.play_media(_media())

    assert commands == []
    for alice_state in ("BUSY", "NONE", "LISTENING", "IDLE"):
        _report(player, playing=True, alice_state=alice_state, native_track_id="yandex-1")

    player.mass.call_later.assert_called_once()


@pytest.mark.parametrize("command", ["pause", "stop"])
@pytest.mark.parametrize("outcome", ["success", "timeout"])
async def test_transport_command_cancels_the_auto_resume(command: str, outcome: str) -> None:
    """The user's command supersedes the auto-resume after Alice, whatever its outcome."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)
    _report(player, playing=False, alice_state="LISTENING")
    _record_commands(player, outcome)

    try:
        await getattr(player, command)()
    except PlayerCommandFailed:
        assert outcome == "timeout"

    player.mass.cancel_timer.assert_called_with(player._voice_resume_task_id)
    for alice_state in ("NONE", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)
    player.mass.call_later.assert_not_called()


def test_station_playback_that_replaces_the_stream_ends_it() -> None:
    """
    Music Alice was asked for replaces the MA stream without a stop in between.

    Recorded on a Station: the reports go from an empty track id to the new
    track's id with playing=True throughout. The stream is gone and the
    Station's own playback takes over.
    """
    player = _make_player()
    player._provider = cast(
        "PlayerProvider", SimpleNamespace(config=_StubConfig(), linked_music_instance=None)
    )
    _start_confirmed_stream(player)

    _station_report(player, track_id="", playing=True)
    _station_report(player, track_id="138389651", playing=True)

    assert player._external_playing is False
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_active_source == STATION_SOURCE_ID
    assert PlayerFeature.PAUSE in player._attr_supported_features
    assert vars(player)["_external_play_generation"] == 1


def test_report_without_a_track_id_keeps_the_stream() -> None:
    """A report that does not name a track says nothing about the source."""
    player = _make_player()
    player._provider = cast(
        "PlayerProvider", SimpleNamespace(config=_StubConfig(), linked_music_instance=None)
    )
    _start_confirmed_stream(player)

    player._on_glagol_update(
        {"state": {"playing": True, "aliceState": "IDLE", "playerState": {"title": "Other"}}}
    )

    assert player._external_playing is True


async def test_stream_reports_after_a_stop_are_not_station_playback() -> None:
    """
    Reports of the stopped stream that arrive late do not start the Station's source.

    Recorded on a Station: after a successful stop the Station still sent two
    playing=True reports with an empty track id, then playing=False. The player
    must stay IDLE on the MA queue so play resumes the queue.
    """
    player = _make_player()
    player._provider = cast(
        "PlayerProvider", SimpleNamespace(config=_StubConfig(), linked_music_instance=None)
    )
    vars(player)["_audio_client"] = True
    _start_confirmed_stream(player)
    _record_commands(player)

    await player.stop()
    _station_report(player, track_id="", playing=True)
    _station_report(player, track_id="", playing=True)
    _station_report(player, track_id="", playing=False)

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._attr_active_source is None


# ── The Station's own track as a Yandex Music item (spec 0004) ────────


def _report_station_track(
    player: YandexStationPlayer,
    *,
    track_id: str,
    track_type: str = "Track",
    cover: str | None = "avatars.yandex.net/get-music-content/1/abc/%%",
) -> dict[str, Any]:
    """Feed a report of the Station's own track and return the published current media."""
    published: dict[str, Any] = {}
    object.__setattr__(player, "set_current_media", lambda **kwargs: published.update(kwargs))
    extra = {"coverURI": cover} if cover is not None else {}
    player._on_glagol_update(
        {
            "state": {
                "playing": True,
                "aliceState": "IDLE",
                "playerState": {
                    "id": track_id,
                    "type": track_type,
                    "title": "Двор",
                    "subtitle": "Пилот",
                    "duration": 194,
                    "extra": extra,
                },
            }
        }
    )
    return published


def _with_linked_music_instance(player: YandexStationPlayer, instance: str | None) -> None:
    player._provider = cast(
        "PlayerProvider",
        SimpleNamespace(config=_StubConfig(), linked_music_instance=instance),
    )


@pytest.mark.parametrize("track_id", ["20934579", "20934579:3046143"])
def test_station_track_is_a_linked_yandex_music_item(track_id: str) -> None:
    """With a linked Yandex Music instance the track is that instance's item."""
    player = _make_player()
    _with_linked_music_instance(player, "yandex_music--sNU5Q6nm")

    media = _report_station_track(player, track_id=track_id)

    assert media["uri"] == "yandex_music--sNU5Q6nm://track/20934579"
    assert media["media_type"] == MediaType.TRACK
    assert media["title"] == "Двор"
    assert media["artist"] == "Пилот"


def test_station_track_without_a_linked_instance_keeps_the_reported_id() -> None:
    """With its own login the provider knows no instance that shares the account."""
    player = _make_player()
    _with_linked_music_instance(player, None)

    media = _report_station_track(player, track_id="20934579")

    assert media["uri"] == "20934579"
    assert media["media_type"] == MediaType.UNKNOWN


def test_station_radio_is_not_mapped_to_a_track() -> None:
    """Only a reported Track names a Yandex Music track."""
    player = _make_player()
    _with_linked_music_instance(player, "yandex_music--sNU5Q6nm")

    media = _report_station_track(player, track_id="user:onyourwave", track_type="FmRadio")

    assert media["uri"] == "user:onyourwave"
    assert media["media_type"] == MediaType.UNKNOWN


def test_station_track_cover_is_an_https_image_url() -> None:
    """The reported cover template becomes an image URL with a concrete size."""
    player = _make_player()
    _with_linked_music_instance(player, None)

    media = _report_station_track(player, track_id="20934579")

    assert media["image_url"] == ("https://avatars.yandex.net/get-music-content/1/abc/400x400")


def test_station_track_without_a_cover_has_no_image() -> None:
    """A report without a cover publishes no image."""
    player = _make_player()
    _with_linked_music_instance(player, None)

    media = _report_station_track(player, track_id="20934579", cover=None)

    assert media["image_url"] is None


def test_ma_stream_keeps_the_requested_media() -> None:
    """While an MA stream plays, its report never replaces the requested item."""
    player = _make_player()
    _with_linked_music_instance(player, "yandex_music--sNU5Q6nm")
    _start_confirmed_stream(player)

    media = _report_station_track(player, track_id="")

    assert media["uri"] == "yandex_music://track/1"


# ── Copilot round 6: voice resume ownership and media replacement ────


def test_new_alice_request_takes_over_a_scheduled_resume() -> None:
    """
    A second request right after Alice answered decides whether playback resumes.

    "Alice, what time is it?" schedules the resume; "Alice, stop" within the delay
    cancels it instead of being overruled by the earlier answer.
    """
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)
    for alice_state in ("LISTENING", "NONE", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)
    player.mass.call_later.assert_called_once()

    for alice_state in ("LISTENING", "BUSY", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)

    player.mass.cancel_timer.assert_called_with(player._voice_resume_task_id)
    player.mass.call_later.assert_called_once()
    assert player._attr_playback_state == PlaybackState.IDLE


def test_answered_second_request_still_resumes() -> None:
    """A second request Alice answers keeps the resume, scheduled after it ends."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)
    for alice_state in ("LISTENING", "NONE", "IDLE", "LISTENING", "NONE", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)

    assert player.mass.call_later.call_count == 2
    assert player._voice.phase == VoiceRecoveryPhase.SCHEDULED


def test_stream_stopped_by_alice_waits_paused_for_her_answer() -> None:
    """
    The player reports PAUSED while the resume is pending.

    The core passes stop and pause on to a player only while it is not idle; that
    command is what cancels the resume.
    """
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)

    for alice_state in ("LISTENING", "NONE", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)

    assert player._attr_playback_state == PlaybackState.PAUSED


@pytest.mark.parametrize("command", ["pause", "stop"])
async def test_users_command_during_the_pending_resume_cancels_it(command: str) -> None:
    """The user's pause or stop while the resume is pending cancels it."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)
    for alice_state in ("LISTENING", "NONE", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)
    _record_commands(player)

    await getattr(player, command)()

    player.mass.cancel_timer.assert_called_with(player._voice_resume_task_id)
    assert player._voice.phase == VoiceRecoveryPhase.NONE


async def test_resume_after_voice_resumes_the_queue_that_owned_the_stream() -> None:
    """A group stream is resumed through the group's queue, not the Station's own."""
    player = _make_player()
    _enable_voice_control(player)
    player.mass.players.cmd_resume = AsyncMock()
    player._start_external_session(
        MediaItem(uri="ma://track/1", title="T", source_id="universal_group_1"),
        audio_client=True,
    )
    player._external_play_confirmed = True
    player._attr_playback_state = PlaybackState.PLAYING
    for alice_state in ("LISTENING", "NONE", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)

    await player._resume_after_voice()

    player.mass.players.cmd_resume.assert_awaited_once_with(
        player.player_id, source="universal_group_1"
    )


def test_station_radio_after_a_track_starts_from_clean_media() -> None:
    """
    A different item replaces the current media instead of merging into it.

    The core's setter keeps the previous type and cover when it gets an unknown
    type or no image, so a new URI must clear the media.
    """
    player = _make_player()
    _with_linked_music_instance(player, "yandex_music--sNU5Q6nm")
    calls: list[dict[str, Any]] = []

    def set_current_media(**kwargs: Any) -> None:
        calls.append(kwargs)
        player._attr_current_media = MediaItem(uri=kwargs["uri"])

    object.__setattr__(player, "set_current_media", set_current_media)

    _report_station_track_real(player, track_id="20934579")
    _report_station_track_real(player, track_id="20934579")
    _report_station_track_real(player, track_id="user:onyourwave", track_type="FmRadio", cover=None)

    assert [call["clear_all"] for call in calls] == [True, False, True]
    assert calls[-1]["media_type"] == MediaType.UNKNOWN
    assert calls[-1]["image_url"] is None


def _report_station_track_real(
    player: YandexStationPlayer,
    *,
    track_id: str,
    track_type: str = "Track",
    cover: str | None = "avatars.yandex.net/get-music-content/1/abc/%%",
) -> None:
    extra = {"coverURI": cover} if cover is not None else {}
    player._on_glagol_update(
        {
            "state": {
                "playing": True,
                "aliceState": "IDLE",
                "playerState": {
                    "id": track_id,
                    "type": track_type,
                    "title": "T",
                    "subtitle": "A",
                    "duration": 100,
                    "extra": extra,
                },
            }
        }
    )


# ── Copilot round 7: running resume, play during the wait, volume baseline ──


def _answered_interruption(player: YandexStationPlayer, source: str | None = None) -> None:
    """Stop the stream through an Alice request that she answered."""
    _enable_voice_control(player)
    player._start_external_session(
        MediaItem(uri="ma://track/1", title="T", source_id=source), audio_client=True
    )
    player._external_play_confirmed = True
    player._attr_playback_state = PlaybackState.PLAYING
    for alice_state in ("LISTENING", "NONE", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)


async def test_station_takeover_during_a_running_resume_drops_its_stream() -> None:
    """
    Station playback that starts while the resume is running is not replaced.

    The core cannot recall the resume once it runs; the stream it asks for is
    dropped instead when it reaches the player.
    """
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    _answered_interruption(player)

    async def cmd_resume(_player_id: str, source: str | None = None) -> None:
        _report(player, playing=True, native_track_id="yandex-9")
        await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id=source))

    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    assert commands == []
    assert player._external_playing is False
    assert player._attr_active_source == STATION_SOURCE_ID
    assert player._voice.phase == VoiceRecoveryPhase.NONE


async def test_running_resume_starts_its_stream_when_nothing_took_over() -> None:
    """The resume's own stream is not mistaken for something that supersedes it."""
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    _answered_interruption(player)

    async def cmd_resume(_player_id: str, source: str | None = None) -> None:
        await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id=source))

    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    assert len(commands) == 1
    assert player._external_playing is True


async def test_play_during_the_wait_resumes_the_owning_queue_now() -> None:
    """
    Play while the music waits for Alice resumes the queue instead of the Station.

    The stream Alice stopped is gone from the Station, so a native play would not
    bring it back; the queue that owned it is resumed through the core.
    """
    player = _make_player()
    commands = _record_commands(player)
    _answered_interruption(player, source="universal_group_1")
    player.mass.players.cmd_resume = AsyncMock()

    await player.play()

    assert commands == []
    player.mass.players.cmd_resume.assert_awaited_once_with(
        player.player_id, source="universal_group_1"
    )
    player.mass.cancel_timer.assert_called_with(player._voice_resume_task_id)


def test_silent_stop_after_a_volume_request_does_not_resume() -> None:
    """A louder request followed by a silent stop leaves the music stopped."""
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)
    player._attr_volume_level = 40
    _report(player, playing=False, alice_state="LISTENING")
    player._attr_volume_level = 50
    _report(player, playing=False, alice_state="IDLE")
    player.mass.call_later.assert_called_once()

    for alice_state in ("LISTENING", "BUSY", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)

    player.mass.call_later.assert_called_once()
    assert player._voice.phase == VoiceRecoveryPhase.NONE
    assert player._attr_playback_state == PlaybackState.IDLE


# ── Copilot round 8 ──────────────────────────────────────────────────


async def test_pause_while_the_music_waits_for_alice_stops_it() -> None:
    """
    Pausing a stream Alice stopped is stopping it: the player reports IDLE.

    The stream is gone from the Station, so after the pause the core's play must
    resume the queue (a player that is not paused) instead of a native play.
    """
    player = _make_player()
    _answered_interruption(player)
    _record_commands(player)

    await player.pause()

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._voice.phase == VoiceRecoveryPhase.NONE
    player.mass.cancel_timer.assert_called_with(player._voice_resume_task_id)


async def test_silent_request_during_a_running_resume_drops_its_stream() -> None:
    """
    "Alice, stop" that finishes while the core prepares the resumed item wins.

    The stream the running resume asks for is dropped and the music stays stopped.
    """
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    _answered_interruption(player)

    async def cmd_resume(_player_id: str, source: str | None = None) -> None:
        for alice_state in ("LISTENING", "BUSY", "IDLE"):
            _report(player, playing=False, alice_state=alice_state)
        await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id=source))

    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    assert commands == []
    assert player._external_playing is False
    assert player._voice.phase == VoiceRecoveryPhase.NONE


async def test_answered_request_during_a_running_resume_schedules_no_second_resume() -> None:
    """
    A second answered request while the core runs the resume keeps that resume.

    The core's scheduler starts a timer's task with abort_existing under the same
    task id, so a second scheduled resume would cancel the running one midway and
    leave the music stopped. The running resume's stream plays instead.
    """
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    _answered_interruption(player)
    player.mass.call_later.reset_mock()

    async def cmd_resume(_player_id: str, source: str | None = None) -> None:
        for alice_state in ("LISTENING", "NONE", "IDLE"):
            _report(player, playing=False, alice_state=alice_state)
        await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id=source))

    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    player.mass.call_later.assert_not_called()
    assert len(commands) == 1
    assert player._external_playing is True


# ── Copilot round 10 ─────────────────────────────────────────────────


async def test_station_track_that_starts_during_stop_keeps_playing() -> None:
    """
    A native track the Station starts while stop() waits for its response stands.

    The stopped report confirms the stop, but the playing report of the next track
    after it is newer than the command; the response must not overwrite it.
    """
    player = _make_player()
    _report(player, playing=True, native_track_id="yandex-1")
    _record_commands(
        player,
        "success",
        {"playing": False, "native_track_id": "yandex-1"},
        {"playing": True, "native_track_id": "yandex-2"},
    )

    await player.stop()

    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_active_source == STATION_SOURCE_ID


async def test_answer_after_a_dropped_resume_stream_resumes_again() -> None:
    """
    Alice answering a request that made the running resume drop its stream resumes.

    The stream is gone, so the running resume cannot play any more; a new resume
    is scheduled once it has returned, so it does not cancel the running one.
    """
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    _answered_interruption(player)
    player.mass.call_later.reset_mock()
    scheduled_while_running: list[bool] = []

    async def cmd_resume(_player_id: str, source: str | None = None) -> None:
        _report(player, playing=False, alice_state="LISTENING")
        await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id=source))
        for alice_state in ("NONE", "IDLE"):
            _report(player, playing=False, alice_state=alice_state)
        scheduled_while_running.append(player.mass.call_later.called)

    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    assert commands == []
    assert scheduled_while_running == [False]
    player.mass.call_later.assert_called_once()
    assert player._voice.phase == VoiceRecoveryPhase.SCHEDULED
    assert player._attr_playback_state == PlaybackState.PAUSED


# ── Copilot round 11 ─────────────────────────────────────────────────


async def test_station_track_during_the_resumed_stream_resolution_drops_it() -> None:
    """
    Station playback that starts while the resumed stream's URL resolves stands.

    The takeover comes after the running resume reached play_media(), so the drop
    is decided again once the URL is resolved.
    """
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    _answered_interruption(player)

    async def resolve_stream_url(_player_id: str, _media: PlayerMedia) -> str:
        _report(player, playing=True, native_track_id="yandex-9")
        return "http://192.168.1.2:8097/next.wav"

    async def cmd_resume(_player_id: str, source: str | None = None) -> None:
        await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id=source))

    object.__setattr__(player.mass.streams, "resolve_stream_url", resolve_stream_url)
    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    assert commands == []
    assert player._external_playing is False
    assert player._attr_active_source == STATION_SOURCE_ID


async def test_request_that_stops_the_resumed_stream_resumes_after_the_answer() -> None:
    """
    A request that stops the resumed stream while it is being handed over decides.

    The stream is gone once the Station stops it for Alice; if she answers before
    the core's resume returns, a new resume follows once it has returned.
    """
    player, _ = _make_play_media_player([])
    _answered_interruption(player)
    player.mass.call_later.reset_mock()
    commands = _record_commands(player, "success", {"playing": False, "alice_state": "LISTENING"})
    scheduled_while_running: list[bool] = []

    async def cmd_resume(_player_id: str, source: str | None = None) -> None:
        await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id=source))
        for alice_state in ("NONE", "IDLE"):
            _report(player, playing=False, alice_state=alice_state)
        scheduled_while_running.append(player.mass.call_later.called)

    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    assert len(commands) == 1
    assert scheduled_while_running == [False]
    player.mass.call_later.assert_called_once()
    assert player._voice.phase == VoiceRecoveryPhase.SCHEDULED
    assert player._attr_playback_state == PlaybackState.PAUSED


# ── Copilot round 12 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("alice_states", "resumes"),
    [(("LISTENING", "BUSY", "IDLE"), False), (("LISTENING", "NONE", "IDLE"), True)],
)
async def test_request_during_the_url_resolution_decides_the_pending_stream(
    alice_states: tuple[str, ...], resumes: bool
) -> None:
    """
    An Alice request that runs entirely while the stream URL resolves decides it.

    "Alice, stop" leaves the music stopped; a question Alice answers resumes the
    queue afterwards. The stream is not sent over the request either way.
    """
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    _enable_voice_control(player)

    async def resolve_stream_url(_player_id: str, _media: PlayerMedia) -> str:
        for alice_state in alice_states:
            _report(player, playing=False, alice_state=alice_state)
        return "http://192.168.1.2:8097/next.wav"

    object.__setattr__(player.mass.streams, "resolve_stream_url", resolve_stream_url)

    await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id="queue_1"))

    assert commands == []
    assert player.mass.call_later.called is resumes
    assert player._voice.phase == (
        VoiceRecoveryPhase.SCHEDULED if resumes else VoiceRecoveryPhase.NONE
    )


def test_ma_stream_leaves_the_source_to_the_core() -> None:
    """
    An MA stream reports no source of its own, also while Alice holds it.

    A universal group stops a paused member whose reported source differs from
    the group's, which would cancel the resume of the group's own stream.
    """
    player = _make_player()
    _answered_interruption(player, source="universal_group_1")

    assert player._attr_playback_state == PlaybackState.PAUSED
    assert player._attr_active_source is None


@pytest.mark.parametrize("command", ["stop", "pause"])
async def test_users_stop_during_a_ducked_request_does_not_start_a_resume(command: str) -> None:
    """
    The Station's stopped report for the user's own stop is not Alice's interruption.

    Without that, her answer after the stop would resume the music the user stopped.
    """
    player = _make_player()
    _enable_voice_control(player)
    _start_confirmed_stream(player)
    _report(player, playing=True, alice_state="LISTENING")
    _record_commands(player, "success", {"playing": False, "alice_state": "LISTENING"})

    await getattr(player, command)()
    for alice_state in ("NONE", "IDLE"):
        _report(player, playing=False, alice_state=alice_state)

    player.mass.call_later.assert_not_called()
    assert player._voice.phase == VoiceRecoveryPhase.NONE
    assert player._attr_playback_state == PlaybackState.IDLE


async def test_request_after_a_station_takeover_does_not_revive_the_resume() -> None:
    """
    A request Alice answers over the Station's own track leaves that track playing.

    The takeover ended the running resume for good; the request is about the
    Station's playback, not about the music the resume asked for.
    """
    player, commands = _make_play_media_player([{"status": "SUCCESS"}])
    _answered_interruption(player)
    player.mass.call_later.reset_mock()

    async def cmd_resume(_player_id: str, source: str | None = None) -> None:
        _report(player, playing=True, native_track_id="yandex-9")
        for alice_state in ("LISTENING", "NONE", "IDLE"):
            _report(player, playing=True, alice_state=alice_state, native_track_id="yandex-9")
        await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id=source))

    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    assert commands == []
    player.mass.call_later.assert_not_called()
    assert player._attr_active_source == STATION_SOURCE_ID
    assert player._voice.phase == VoiceRecoveryPhase.NONE


# ── Copilot round 13 ─────────────────────────────────────────────────


async def test_pause_while_the_resume_prepares_the_queue_stops() -> None:
    """
    Pausing while the automatic resume prepares its item is stopping, as during the wait.

    The stream Alice stopped is still gone; a native pause would leave the player
    paused, and the next play would send a native play that cannot bring it back.
    """
    player, _ = _make_play_media_player([])
    _answered_interruption(player)
    commands = _record_commands(player)

    async def cmd_resume(_player_id: str, source: str | None = None) -> None:
        await player.pause()
        await player.play_media(MediaItem(uri="ma://track/1", title="T", source_id=source))

    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    assert commands == [{"command": "stop"}]
    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._external_playing is False


async def test_pause_after_station_playback_took_over_the_resume_pauses_it() -> None:
    """Station playback that took over from the running resume is paused natively."""
    player, _ = _make_play_media_player([])
    _answered_interruption(player)
    commands = _record_commands(player)

    async def cmd_resume(*_args: object, **_kwargs: object) -> None:
        _report(player, playing=True, native_track_id="yandex-9")
        await player.pause()

    player.mass.players.cmd_resume = AsyncMock(side_effect=cmd_resume)

    await player._resume_after_voice()

    assert commands == [{"command": "stop"}]
    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_active_source == STATION_SOURCE_ID
