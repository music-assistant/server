"""Tests for the Sendspin Source provider."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from aiosendspin.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server import (
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    SignalState,
    SourceSignalChangedEvent,
    SourceStreamEndedEvent,
    SourceStreamStartedEvent,
)
from music_assistant_models.enums import MediaType, PlaybackState, QueueOption, StreamType
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.providers.sendspin_source.provider as provider_module
from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.providers.sendspin.constants import CONF_SOURCE_AUTOSTART_TARGET
from music_assistant.providers.sendspin_source import get_config_entries
from music_assistant.providers.sendspin_source.constants import (
    CONF_TARGET_LATENCY,
    DEFAULT_TARGET_LATENCY_MS,
)
from music_assistant.providers.sendspin_source.provider import OUTPUT_FORMAT

from .conftest import (
    _FakeClient,
    _FakePlayer,
    _FakeSourceRole,
    get_config,
    get_players,
    get_queues,
    get_server_api,
    make_provider,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MARKER_BYTE = b"\x7f"
NATIVE_FORMAT = SendspinAudioFormat(sample_rate=44100, bit_depth=16, channels=2)


class _StubBridge:
    """Bridge stand-in returning marker bytes and recording feeds."""

    occupancy_us = 0

    def __init__(self) -> None:
        self.fed: list[tuple[bytes, int]] = []
        self.flush_count = 0

    def feed(self, pcm: bytes, capture_timestamp_us: int) -> None:
        self.fed.append((pcm, capture_timestamp_us))

    def read(self, frames: int) -> bytes:
        return MARKER_BYTE * (frames * 4)

    def flush(self) -> None:
        self.flush_count += 1


def _stream_details(item_id: str = "client-1") -> StreamDetails:
    return StreamDetails(
        provider="sendspin_source",
        item_id=item_id,
        audio_format=OUTPUT_FORMAT,
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.CUSTOM,
    )


async def test_config_entries_start_with_preview_warning() -> None:
    """The alpha provider warns users before showing its options."""
    mass: Any = None
    entries = await get_config_entries(mass)
    assert entries[0] is CONF_ENTRY_WARN_PREVIEW


async def _take(stream: AsyncGenerator[bytes], count: int) -> list[bytes]:
    chunks: list[bytes] = []
    try:
        async for chunk in stream:
            chunks.append(chunk)
            if len(chunks) >= count:
                break
    finally:
        await stream.aclose()
    return chunks


async def _fake_handle(chunks: list[tuple[bytes, int]]) -> Any:
    for chunk in chunks:
        yield chunk


async def test_audio_sources_follow_role_activation() -> None:
    """Only connected clients with an active source role are listed."""
    provider = await make_provider(
        [
            _FakeClient("with-role", name="Turntable"),
            _FakeClient("no-role", name="Speaker", has_source_role=False),
            _FakeClient("offline", name="Gone", connected=False),
        ]
    )
    sources = await provider.get_audio_sources()
    assert [s.item_id for s in sources] == ["with-role"]
    assert sources[0].exclusive is True
    assert sources[0].can_initiate is True


async def test_source_name_follows_a_player_rename(fake_client: _FakeClient) -> None:
    """The source is named after its player, and a rename reaches Browse and now-playing."""
    provider = await make_provider([fake_client])
    get_players(provider).players["client-1"].display_name = "Living Room"
    assert [s.name for s in await provider.get_audio_sources()] == ["Sendspin Source (Living Room)"]
    get_players(provider).players["client-1"].display_name = "Den"
    assert [s.name for s in await provider.get_audio_sources()] == ["Sendspin Source (Den)"]
    details = await provider.get_stream_details("client-1", MediaType.AUDIO_SOURCE)
    assert details.stream_metadata is not None
    assert details.stream_metadata.title == "Sendspin Source (Den)"


async def test_source_name_uses_the_visible_parent_player(fake_client: _FakeClient) -> None:
    """A protocol player's source is named after the player the user sees."""
    provider = await make_provider([fake_client])
    players = get_players(provider).players
    players["client-1"].protocol_parent_id = "cast-1"
    players["cast-1"] = _FakePlayer("cast-1", display_name="Kitchen")
    assert [s.name for s in await provider.get_audio_sources()] == ["Sendspin Source (Kitchen)"]


async def test_source_name_falls_back_to_the_device_name(fake_client: _FakeClient) -> None:
    """Before its player registers, the source is named after the device's own name."""
    provider = await make_provider([fake_client])
    get_players(provider).players.clear()
    assert [s.name for s in await provider.get_audio_sources()] == ["Sendspin Source (Turntable)"]


async def test_line_in_is_listed_on_its_own_player_while_connected() -> None:
    """A speaker's line-in stays in its own source list between streams until it disconnects."""
    client = _FakeClient("client-1", name="Cast Pro", has_player_role=True)
    provider = await make_provider([client])
    assert [s.item_id for s in provider.get_player_input_sources("client-1")] == ["client-1"]
    await provider.on_source_selected("client-1", "client-1", "client-1", "session-1")
    await provider.on_source_unselected("client-1", "client-1", "session-1")
    assert [s.item_id for s in provider.get_player_input_sources("client-1")] == ["client-1"]
    client.is_connected = False
    client.connection = None
    assert provider.get_player_input_sources("client-1") == []


async def test_line_in_is_listed_before_the_reconnect_handshake_completes() -> None:
    """The reconnect refresh runs before the first client/state marks the client connected."""
    client = _FakeClient("client-1", name="Cast Pro", has_player_role=True)
    provider = await make_provider([client])
    client.is_connected = False
    assert [s.item_id for s in provider.get_player_input_sources("client-1")] == ["client-1"]


async def test_capture_only_client_lists_no_input(fake_client: _FakeClient) -> None:
    """A client without a player role has no source list of its own to appear in."""
    provider = await make_provider([fake_client])
    assert provider.get_player_input_sources("client-1") == []


async def test_line_in_stays_available_to_every_player(fake_client: _FakeClient) -> None:
    """The plugin never binds its sources to players, so every speaker can play the line-in."""
    provider = await make_provider([fake_client])
    assert provider.get_player_audio_sources("client-1") is None


async def test_reconnect_refreshes_the_player_listing_the_line_in() -> None:
    """The visible player rebuilds its source list once the source role is back."""
    client = _FakeClient("client-1", name="Cast Pro", has_player_role=True)
    provider = await make_provider([client])
    players = get_players(provider)
    players.players["client-1"].protocol_parent_id = "visible-1"
    players.updated.clear()
    role = client.detach_roles()
    get_server_api(provider).emit(ClientConnectedEvent("client-1"))
    client.attach_roles(role)
    await _settle()
    assert players.updated == ["visible-1"]


async def test_disconnect_refreshes_the_player_listing_the_line_in() -> None:
    """The visible player drops the line-in once the client's connection is gone."""
    client = _FakeClient("client-1", has_player_role=True)
    provider = await make_provider([client])
    players = get_players(provider)
    players.players["client-1"].protocol_parent_id = "visible-1"
    players.updated.clear()
    client.is_connected = False
    client.connection = None
    get_server_api(provider).emit(ClientDisconnectedEvent("client-1", goodbye_reason=None))
    assert players.updated == ["visible-1"]


async def test_load_refreshes_the_players_listing_a_line_in() -> None:
    """Players already connected when the plugin loads get their line-in listed."""
    speaker = _FakeClient("speaker", has_player_role=True)
    capture_only = _FakeClient("capture-only")
    provider = await make_provider([speaker, capture_only])
    assert get_players(provider).updated == ["speaker"]


async def test_unload_drops_the_line_in_from_its_player() -> None:
    """Unloading the plugin rebuilds the player's source list without the line-in."""
    provider = await make_provider([_FakeClient("speaker", has_player_role=True)])
    players = get_players(provider)
    players.updated.clear()
    await provider.unload()
    assert players.updated == ["speaker"]
    assert provider.get_player_input_sources("speaker") == []


async def test_stream_details_rejects_unknown_source() -> None:
    """get_stream_details raises for a client that is not a connected source."""
    provider = await make_provider([_FakeClient("no-role", has_source_role=False)])
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("no-role", MediaType.AUDIO_SOURCE)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("unknown", MediaType.AUDIO_SOURCE)


async def test_select_requests_start_and_subscribes(fake_client: _FakeClient) -> None:
    """Selecting a source sends server/command start and attaches a client listener."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    assert fake_client.source_role is not None
    assert fake_client.source_role.start_requests == 1
    assert len(fake_client.listeners) == 1


async def test_unselect_stops_and_releases(fake_client: _FakeClient) -> None:
    """Unselecting with the live session id sends stop and drops the session."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    await provider.on_source_unselected("client-1", "queue-1", "session-1")
    assert fake_client.source_role is not None
    assert fake_client.source_role.stop_requests == 1
    with pytest.raises(AudioError):
        await _take(provider.get_audio_stream(_stream_details()), 1)
    assert get_players(provider).stopped == []
    # The signal watcher outlives the session, so autostart still works afterwards.
    assert len(fake_client.listeners) == 1


async def test_unselect_ignores_stale_session_id(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale unselect callback must not tear down the live session."""
    provider = await make_provider([fake_client])
    await _start_streaming(provider, fake_client, monkeypatch)
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-2")
    await provider.on_source_unselected("client-1", "queue-1", "session-1")
    assert fake_client.source_role is not None
    assert fake_client.source_role.stop_requests == 0
    assert await _take(provider.get_audio_stream(_stream_details()), 1)
    assert len(fake_client.listeners) == 1


async def _start_streaming(
    provider: Any,
    client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[tuple[bytes, int]] | None = None,
) -> _StubBridge:
    """Select the source and let one chunk through, so the stream is past its cold start."""
    bridge = _StubBridge()
    monkeypatch.setattr(provider, "_create_bridge", lambda *_args: bridge)
    await provider.on_source_selected(client.client_id, "player-1", "queue-1", "session-1")
    handle = _fake_handle(chunks if chunks is not None else [(b"\x01\x02\x03\x04", 1_000_000)])
    client.emit(SourceStreamStartedEvent(audio_format=NATIVE_FORMAT, handle=handle))
    await _settle()
    return bridge


async def _latency_passed_to_bridge(
    provider: Any, client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> list[int]:
    """Select the source and return the target latencies the bridge was built with."""
    latencies: list[int] = []

    def _create(_audio_format: Any, target_latency_ms: int) -> _StubBridge:
        latencies.append(target_latency_ms)
        return _StubBridge()

    monkeypatch.setattr(provider, "_create_bridge", _create)
    await provider.on_source_selected(client.client_id, "player-1", "queue-1", "session-1")
    client.emit(SourceStreamStartedEvent(audio_format=NATIVE_FORMAT, handle=_fake_handle([])))
    await _settle()
    return latencies


async def test_bridge_falls_back_to_the_default_latency(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider starts up before its own options are resolved, so the value is unset."""
    provider = await make_provider([fake_client])
    assert await _latency_passed_to_bridge(provider, fake_client, monkeypatch) == [
        DEFAULT_TARGET_LATENCY_MS
    ]


async def test_a_configured_latency_reaches_the_bridge(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the user sets a latency it must win over the fallback."""
    provider = await make_provider([fake_client], {CONF_TARGET_LATENCY: 1200})
    assert await _latency_passed_to_bridge(provider, fake_client, monkeypatch) == [1200]


async def test_stream_fails_when_the_source_never_starts(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that never answers the start command is a failed acquisition, not silence."""
    monkeypatch.setattr(provider_module, "COLD_START_TIMEOUT_S", 0.01)
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    with pytest.raises(AudioError):
        await _take(provider.get_audio_stream(_stream_details()), 1)


async def test_stream_switches_to_bridge_audio_after_stream_start(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client_stream/start routes decoded chunks through the bridge into the stream."""
    provider = await make_provider([fake_client])
    bridge = await _start_streaming(provider, fake_client, monkeypatch)

    chunks = await _take(provider.get_audio_stream(_stream_details()), 5)
    assert all(chunk == MARKER_BYTE * (48000 * 25 // 1000 * 4) for chunk in chunks)
    assert bridge.fed == [(b"\x01\x02\x03\x04", 1_000_000)]
    await provider.on_source_unselected("client-1", "queue-1", "session-1")


async def test_source_stream_end_flushes_the_live_bridge(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normally ended source stream emits the resampler tail."""
    provider = await make_provider([fake_client])
    bridge = await _start_streaming(provider, fake_client, monkeypatch)
    assert bridge.flush_count == 1


async def test_stream_ends_after_source_timeout(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generator ends on its own once no source audio arrives for the timeout."""
    provider = await make_provider([fake_client])
    await _start_streaming(provider, fake_client, monkeypatch)
    monkeypatch.setattr(provider_module, "SOURCE_TIMEOUT_S", 0.05)
    chunks = [chunk async for chunk in provider.get_audio_stream(_stream_details())]
    assert 1 <= len(chunks) <= 10


async def test_turning_the_input_off_releases_the_source_at_once(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connected client that loses its source role leaves nothing to wait for."""
    provider = await make_provider([fake_client])
    await _start_streaming(provider, fake_client, monkeypatch)

    fake_client.detach_roles()
    fake_client.emit(SourceStreamEndedEvent())
    await _settle()

    assert get_players(provider).deselected == ["queue-1"]


async def test_a_disconnect_keeps_the_source_waiting(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped client may come back, so its source holds until the timeout."""
    provider = await make_provider([fake_client])
    await _start_streaming(provider, fake_client, monkeypatch)

    fake_client.is_connected = False
    fake_client.detach_roles()
    fake_client.emit(SourceStreamEndedEvent())
    await _settle()

    assert get_players(provider).deselected == []


async def test_reconnect_re_requests_start(fake_client: _FakeClient) -> None:
    """A reconnect clears the client's start request, so the provider sends it again."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    assert fake_client.source_role is not None
    assert fake_client.source_role.start_requests == 1
    get_server_api(provider).emit(ClientConnectedEvent("client-1"))
    await asyncio.sleep(0)
    assert fake_client.source_role.start_requests == 2
    await provider.on_source_unselected("client-1", "queue-1", "session-1")


async def test_cold_reconnect_re_requests_start_once_roles_are_back(
    fake_client: _FakeClient,
) -> None:
    """Roles attach after the connected signal, so the re-request must not run inside it."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    role = fake_client.detach_roles()
    get_server_api(provider).emit(ClientConnectedEvent("client-1"))
    fake_client.attach_roles(role)
    await _settle()
    assert role is not None
    assert role.start_requests == 2


async def test_held_inputs_are_offered_only_when_approved_here() -> None:
    """A redialable held input is listed once paired or approved here, without dialing it."""
    provider = await make_provider(
        [
            _FakeClient("paired", held=True),
            _FakeClient("approved", held=True),
            _FakeClient("unapproved", held=True),
            _FakeClient("dialed-in", held=True),
            _FakeClient("offline", connected=False),
        ]
    )
    server_api = get_server_api(provider)
    server_api.pairing_store.paired.update({"paired", "dialed-in"})
    del server_api.client_urls["dialed-in"]
    server_api.pairing_store.trusted["approved"] = frozenset({"source"})
    server_api.pairing_store.trusted["unapproved"] = frozenset()
    assert [s.item_id for s in await provider.get_audio_sources()] == ["paired", "approved"]
    with pytest.raises(MediaNotFoundError):
        await provider.on_source_selected("unapproved", "player-1", "queue-1", "session-1")
    assert server_api.reclaim_requests == []


async def test_selecting_a_held_input_takes_the_device_over() -> None:
    """Selecting a held input reclaims the device and asks it to stream once it is back."""
    client = _FakeClient("client-1", held=True)
    provider = await make_provider([client])
    server_api = get_server_api(provider)
    server_api.pairing_store.trusted["client-1"] = frozenset({"source"})
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    assert server_api.reclaim_requests == ["client-1"]

    client.is_connected = True
    client.cleanup_on_mdns_removal = False
    client.attach_roles(role := _FakeSourceRole())
    server_api.emit(ClientConnectedEvent("client-1"))
    await _settle()
    assert role.start_requests == 1


async def test_a_held_input_that_stays_away_fails_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device the other server keeps fails with the did-not-start error."""
    monkeypatch.setattr(provider_module, "COLD_START_TIMEOUT_S", 0.01)
    provider = await make_provider([_FakeClient("client-1", held=True)])
    get_server_api(provider).pairing_store.paired.add("client-1")
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    with pytest.raises(AudioError) as err:
        await _take(provider.get_audio_stream(_stream_details()), 1)
    assert err.value.translation_key == "no_audio"


async def test_reselecting_an_input_another_server_took_reclaims_it(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renderer reopening the stream leaves the device alone, a new selection reclaims it."""
    provider = await make_provider([fake_client])
    server_api = get_server_api(provider)
    server_api.pairing_store.trusted["client-1"] = frozenset({"source"})
    await _start_streaming(provider, fake_client, monkeypatch)
    fake_client.is_connected = False
    fake_client.cleanup_on_mdns_removal = True
    fake_client.detach_roles()

    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-2")
    assert server_api.reclaim_requests == []
    get_players(provider).source_sessions["queue-1"] = SimpleNamespace(
        playback_session_id="playback-2"
    )
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-3")
    assert server_api.reclaim_requests == ["client-1"]


async def test_a_renderer_retry_after_release_does_not_reclaim(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried stream of a released playback session is no new selection to reclaim for."""
    provider = await make_provider([fake_client])
    server_api = get_server_api(provider)
    server_api.pairing_store.trusted["client-1"] = frozenset({"source"})
    get_players(provider).source_sessions["queue-1"] = SimpleNamespace(
        playback_session_id="playback-1"
    )
    await _start_streaming(provider, fake_client, monkeypatch)
    fake_client.is_connected = False
    fake_client.cleanup_on_mdns_removal = True
    fake_client.detach_roles()
    await provider.on_source_unselected("client-1", "queue-1", "session-1")

    with pytest.raises(MediaNotFoundError):
        await provider.on_source_selected("client-1", "player-1", "queue-1", "session-2")
    assert server_api.reclaim_requests == []


async def test_signal_watcher_arms_before_roles_attach() -> None:
    """Watching keys off negotiated roles, which are known before the instances attach."""
    client = _FakeClient("client-1", name="Turntable", connected=False)
    provider = await make_provider([client])
    assert client.listeners == []
    client.is_connected = True
    client.detach_roles()
    get_server_api(provider).emit(ClientConnectedEvent("client-1"))
    await _settle()
    assert len(client.listeners) == 1


async def test_reconnect_leaves_an_open_stream_alone(fake_client: _FakeClient) -> None:
    """A client that kept its input stream across the event is not asked to start again."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    assert fake_client.source_role is not None
    fake_client.source_role.stream_active = True
    get_server_api(provider).emit(ClientConnectedEvent("client-1"))
    await asyncio.sleep(0)
    assert fake_client.source_role.start_requests == 1
    await provider.on_source_unselected("client-1", "queue-1", "session-1")


async def test_handoff_stops_the_player_it_was_taken_from(fake_client: _FakeClient) -> None:
    """Moving a source to another player stops the first, which would drain its buffer."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    await provider.on_source_selected("client-1", "player-2", "queue-2", "session-2")
    assert get_players(provider).stopped == ["player-1"]
    # The client must be stopped too: only a fresh client_stream/start gives the
    # replacement session a bridge, and the client sends one after a stop/start.
    assert fake_client.source_role is not None
    assert fake_client.source_role.stop_requests == 1


async def test_reclaim_by_the_same_queue_keeps_the_client_streaming(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renderer opening the stream url twice must not cost a stop/start of the source."""
    provider = await make_provider([fake_client])
    await _start_streaming(provider, fake_client, monkeypatch)
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-2")
    assert fake_client.source_role is not None
    assert fake_client.source_role.stop_requests == 0
    assert fake_client.source_role.start_requests == 1
    assert get_players(provider).stopped == []
    assert await _take(provider.get_audio_stream(_stream_details()), 1)


async def test_reclaim_by_the_same_queue_retires_the_previous_generator(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only one generator may read the bridge, or the two requests split the audio."""
    provider = await make_provider([fake_client])
    await _start_streaming(provider, fake_client, monkeypatch)
    stream = provider.get_audio_stream(_stream_details())
    assert await anext(stream) is not None
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-2")
    assert [chunk async for chunk in stream] == []


async def test_reclaim_while_waiting_for_audio_retires_the_previous_generator(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-queue reclaim retires a generator that is still waiting for first audio."""
    provider = await make_provider([fake_client])
    bridge = _StubBridge()
    monkeypatch.setattr(provider, "_create_bridge", lambda *_args: bridge)
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    previous = provider.get_audio_stream(_stream_details())
    previous_chunk = asyncio.create_task(anext(previous))
    await asyncio.sleep(0)

    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-2")
    replacement = provider.get_audio_stream(_stream_details())
    replacement_chunk = asyncio.create_task(anext(replacement))
    fake_client.emit(
        SourceStreamStartedEvent(
            audio_format=NATIVE_FORMAT,
            handle=_fake_handle([(b"\x01\x02\x03\x04", 1_000_000)]),
        )
    )
    await _settle()

    with pytest.raises(StopAsyncIteration):
        await previous_chunk
    assert await replacement_chunk == MARKER_BYTE * (48000 * 25 // 1000 * 4)
    await replacement.aclose()


async def test_reclaim_by_the_same_player_on_another_queue_still_hands_off(
    fake_client: _FakeClient,
) -> None:
    """A different queue is a real re-target, so the previous claim is torn down."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    await provider.on_source_selected("client-1", "player-1", "queue-2", "session-2")
    assert fake_client.source_role is not None
    assert fake_client.source_role.stop_requests == 1
    assert get_players(provider).stopped == []


async def test_concurrent_handoffs_leave_the_newest_selection_active(
    fake_client: _FakeClient,
) -> None:
    """Concurrent handoffs serialize so an older stop cannot overwrite the latest claim."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    players = get_players(provider)
    players.stop_started = asyncio.Event()
    players.release_stop = asyncio.Event()

    first = asyncio.create_task(
        provider.on_source_selected("client-1", "player-2", "queue-2", "session-2")
    )
    await players.stop_started.wait()
    second = asyncio.create_task(
        provider.on_source_selected("client-1", "player-3", "queue-3", "session-3")
    )
    await asyncio.sleep(0)
    players.release_stop.set()
    await asyncio.gather(first, second)

    await provider.on_source_unselected("client-1", "queue-3", "session-3")
    assert players.stopped == ["player-1", "player-2"]
    assert fake_client.source_role is not None
    assert fake_client.source_role.stop_requests == 3


async def test_two_sources_stream_concurrently(fake_client: _FakeClient) -> None:
    """Selecting a second source must not stop the first: exclusivity is per source."""
    other = _FakeClient("client-2", name="Aux")
    provider = await make_provider([fake_client, other])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    await provider.on_source_selected("client-2", "player-2", "queue-2", "session-2")
    assert fake_client.source_role is not None
    assert other.source_role is not None
    assert fake_client.source_role.stop_requests == 0
    assert other.source_role.start_requests == 1
    assert get_players(provider).stopped == []


async def test_new_selection_supersedes_running_stream(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-selecting the same source elsewhere makes the previous generator terminate."""
    provider = await make_provider([fake_client])
    await _start_streaming(provider, fake_client, monkeypatch)
    stream = provider.get_audio_stream(_stream_details())
    assert await anext(stream) is not None
    await provider.on_source_selected("client-1", "player-2", "queue-2", "session-2")
    chunks = [chunk async for chunk in stream]
    assert len(chunks) <= 1


def _signal(state: SignalState) -> SourceSignalChangedEvent:
    return SourceSignalChangedEvent(signal=state)


@pytest.fixture
def fast_autostart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the debounce and hold so autostart timing is not a test dependency."""
    monkeypatch.setattr(provider_module, "AUTOSTART_SIGNAL_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(provider_module, "AUTOSTART_SIGNAL_ABSENT_HOLD_S", 0.0)


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.usefixtures("fast_autostart")
async def test_signal_returning_autostarts_configured_target(fake_client: _FakeClient) -> None:
    """A signal appearing after being absent starts the source on the configured player."""
    provider = await make_provider([fake_client])
    get_config(provider).values[("client-1", CONF_SOURCE_AUTOSTART_TARGET)] = "client-1"
    fake_client.emit(_signal(SignalState.ABSENT))
    fake_client.emit(_signal(SignalState.PRESENT))
    await _settle()
    assert get_queues(provider).played == [
        ("client-1", "sendspin_source://audio_source/client-1", QueueOption.PLAY)
    ]


@pytest.mark.usefixtures("fast_autostart")
async def test_autostart_uses_the_entry_default_when_nothing_was_saved(
    fake_client: _FakeClient,
) -> None:
    """A device that plays its own line-in must work before the user ever saves the page."""
    provider = await make_provider([fake_client])
    get_config(provider).defaults[("client-1", CONF_SOURCE_AUTOSTART_TARGET)] = "client-1"
    fake_client.emit(_signal(SignalState.ABSENT))
    fake_client.emit(_signal(SignalState.PRESENT))
    await _settle()
    assert get_queues(provider).played == [
        ("client-1", "sendspin_source://audio_source/client-1", QueueOption.PLAY)
    ]


@pytest.mark.usefixtures("fast_autostart")
async def test_first_signal_report_never_autostarts(fake_client: _FakeClient) -> None:
    """A needle already down at startup must not start playing by itself."""
    provider = await make_provider([fake_client])
    get_config(provider).values[("client-1", CONF_SOURCE_AUTOSTART_TARGET)] = "client-1"
    fake_client.emit(_signal(SignalState.PRESENT))
    await _settle()
    assert get_queues(provider).played == []


async def test_transient_signal_does_not_autostart(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal that disappears during the debounce never starts playback."""
    monkeypatch.setattr(provider_module, "AUTOSTART_SIGNAL_DEBOUNCE_S", 60.0)
    provider = await make_provider([fake_client])
    get_config(provider).values[("client-1", CONF_SOURCE_AUTOSTART_TARGET)] = "client-1"
    fake_client.emit(_signal(SignalState.ABSENT))
    fake_client.emit(_signal(SignalState.PRESENT))
    fake_client.emit(_signal(SignalState.ABSENT))
    await _settle()
    assert get_queues(provider).played == []
    await provider.unload()


async def test_manual_selection_cancels_a_fired_autostart(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual selection wins while a fired autostart is starting playback."""
    provider = await make_provider([fake_client])
    get_config(provider).values[("client-1", CONF_SOURCE_AUTOSTART_TARGET)] = "client-1"
    queues = get_queues(provider)
    queues.play_started = asyncio.Event()
    queues.release_play = asyncio.Event()
    monkeypatch.setattr(provider_module, "AUTOSTART_SIGNAL_DEBOUNCE_S", 0.0)
    fake_client.emit(_signal(SignalState.ABSENT))
    fake_client.emit(_signal(SignalState.PRESENT))
    await queues.play_started.wait()

    await provider.on_source_selected("client-1", "player-2", "queue-2", "session-1")
    queues.release_play.set()
    await _settle()
    assert queues.played == []


@pytest.mark.usefixtures("fast_autostart")
async def test_delayed_autostart_stream_cannot_override_manual_selection(
    fake_client: _FakeClient,
) -> None:
    """A delayed stream request from a completed autostart cannot reclaim the source."""
    provider = await make_provider([fake_client])
    get_config(provider).values[("client-1", CONF_SOURCE_AUTOSTART_TARGET)] = "client-1"
    fake_client.emit(_signal(SignalState.ABSENT))
    fake_client.emit(_signal(SignalState.PRESENT))
    await _settle()

    queues = get_queues(provider)
    # the handoff suspends while it releases the autostart player, which is where the
    # delayed request has to arrive for this to be the race it is testing
    players = get_players(provider)
    players.stop_started = asyncio.Event()
    players.release_stop = asyncio.Event()
    manual = asyncio.create_task(
        provider.on_source_selected("client-1", "player-2", "queue-2", "session-1")
    )
    await players.stop_started.wait()
    delayed = asyncio.create_task(
        provider.on_source_selected("client-1", "client-1", "client-1", "session-2")
    )
    await asyncio.sleep(0)
    players.release_stop.set()
    await manual
    with pytest.raises(RuntimeError, match="Superseded autostart"):
        await delayed

    await queues.play_media("client-1", "sendspin_source://audio_source/client-1")
    await provider.on_source_selected("client-1", "client-1", "client-1", "session-3")
    assert fake_client.source_role is not None
    assert fake_client.source_role.start_requests == 2


@pytest.mark.usefixtures("fast_autostart")
async def test_autostart_stays_off_without_a_configured_target(fake_client: _FakeClient) -> None:
    """With no target configured the signal is observed but never acted on."""
    provider = await make_provider([fake_client])
    fake_client.emit(_signal(SignalState.ABSENT))
    fake_client.emit(_signal(SignalState.PRESENT))
    await _settle()
    assert get_queues(provider).played == []


@pytest.mark.usefixtures("fast_autostart")
async def test_autostart_interrupts_a_busy_target(fake_client: _FakeClient) -> None:
    """A detected signal takes over a target that is already playing."""
    provider = await make_provider([fake_client])
    get_config(provider).values[("client-1", CONF_SOURCE_AUTOSTART_TARGET)] = "client-1"
    get_players(provider).players["client-1"].queue.state = PlaybackState.PLAYING
    fake_client.emit(_signal(SignalState.ABSENT))
    fake_client.emit(_signal(SignalState.PRESENT))
    await _settle()
    assert get_queues(provider).played == [
        ("client-1", "sendspin_source://audio_source/client-1", QueueOption.PLAY)
    ]


@pytest.mark.usefixtures("fast_autostart")
async def test_autostart_skipped_while_the_source_already_streams(fake_client: _FakeClient) -> None:
    """A source the user already routed somewhere must not be stolen back by a signal."""
    provider = await make_provider([fake_client])
    get_config(provider).values[("client-1", CONF_SOURCE_AUTOSTART_TARGET)] = "client-1"
    await provider.on_source_selected("client-1", "player-2", "queue-2", "session-1")
    fake_client.emit(_signal(SignalState.ABSENT))
    fake_client.emit(_signal(SignalState.PRESENT))
    await _settle()
    assert get_queues(provider).played == []


@pytest.mark.usefixtures("fast_autostart")
async def test_signal_loss_stops_a_running_source(fake_client: _FakeClient) -> None:
    """A record reaching its end stops the stream instead of playing silence forever."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    fake_client.emit(_signal(SignalState.PRESENT))
    fake_client.emit(_signal(SignalState.ABSENT))
    await _settle()
    # the source is given up on the player that owns it, so the player stops saying
    # it is playing something that has gone quiet
    assert get_players(provider).deselected == ["queue-1"]


async def test_signal_return_cancels_pending_autostop(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal returning during the hold keeps the running source playing."""
    monkeypatch.setattr(provider_module, "AUTOSTART_SIGNAL_ABSENT_HOLD_S", 60.0)
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    fake_client.emit(_signal(SignalState.PRESENT))
    fake_client.emit(_signal(SignalState.ABSENT))
    fake_client.emit(_signal(SignalState.PRESENT))
    await _settle()
    assert get_queues(provider).stopped == []
    await provider.unload()


@pytest.mark.usefixtures("fast_autostart")
async def test_a_reconnect_does_not_defuse_a_pending_autostop(fake_client: _FakeClient) -> None:
    """A same-queue reconnect re-claims the source and must not cancel the pending stop."""
    provider = await make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    fake_client.emit(_signal(SignalState.PRESENT))
    fake_client.emit(_signal(SignalState.ABSENT))
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-2")
    await _settle()
    assert get_players(provider).deselected == ["queue-1"]


async def test_deferred_reconnect_does_not_rewatch_after_unload(fake_client: _FakeClient) -> None:
    """A connected callback queued before unload cannot restore its client listener."""
    provider = await make_provider([fake_client])
    get_server_api(provider).emit(ClientConnectedEvent("client-1"))
    await provider.unload()
    await _settle()
    assert fake_client.listeners == []
