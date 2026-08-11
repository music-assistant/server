"""Tests for the Sendspin Source provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from aiosendspin.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server import ClientConnectedEvent, SourceStreamStartedEvent
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.streamdetails import StreamDetails

import music_assistant.providers.sendspin_source.provider as provider_module
from music_assistant.providers.sendspin_source.provider import OUTPUT_FORMAT

from .conftest import _FakeClient, get_players, get_server_api, make_provider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

MARKER_BYTE = b"\x7f"
NATIVE_FORMAT = SendspinAudioFormat(sample_rate=44100, bit_depth=16, channels=2)


class _StubBridge:
    """Bridge stand-in returning marker bytes and recording feeds."""

    occupancy_us = 0

    def __init__(self) -> None:
        self.fed: list[tuple[bytes, int]] = []

    def feed(self, pcm: bytes, capture_timestamp_us: int) -> None:
        self.fed.append((pcm, capture_timestamp_us))

    def read(self, frames: int) -> bytes:
        return MARKER_BYTE * (frames * 4)


def _stream_details(item_id: str = "client-1") -> StreamDetails:
    return StreamDetails(
        provider="sendspin_source",
        item_id=item_id,
        audio_format=OUTPUT_FORMAT,
        media_type=MediaType.AUDIO_SOURCE,
        stream_type=StreamType.CUSTOM,
    )


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
    provider = make_provider(
        [
            _FakeClient("with-role", name="Turntable"),
            _FakeClient("no-role", name="Speaker", has_source_role=False),
            _FakeClient("offline", name="Gone", connected=False),
        ]
    )
    sources = await provider.get_audio_sources()
    assert [s.item_id for s in sources] == ["with-role"]
    assert sources[0].name == "Turntable"
    assert sources[0].exclusive is True
    assert sources[0].can_initiate is True


async def test_stream_details_rejects_unknown_source() -> None:
    """get_stream_details raises for a client that is not a connected source."""
    provider = make_provider([_FakeClient("no-role", has_source_role=False)])
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("no-role", MediaType.AUDIO_SOURCE)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("unknown", MediaType.AUDIO_SOURCE)


async def test_select_requests_start_and_subscribes(fake_client: _FakeClient) -> None:
    """Selecting a source sends server/command start and attaches a client listener."""
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    assert fake_client.source_role is not None
    assert fake_client.source_role.start_requests == 1
    assert len(fake_client.listeners) == 1


async def test_unselect_stops_and_releases(fake_client: _FakeClient) -> None:
    """Unselecting with the live session id sends stop and detaches the listener."""
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    await provider.on_source_unselected("client-1", "queue-1", "session-1")
    assert fake_client.source_role is not None
    assert fake_client.source_role.stop_requests == 1
    assert fake_client.listeners == []
    assert get_server_api(provider).listeners == []
    assert get_players(provider).stopped == []


async def test_unselect_ignores_stale_session_id(fake_client: _FakeClient) -> None:
    """A stale unselect callback must not tear down the live session."""
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-2")
    await provider.on_source_unselected("client-1", "queue-1", "session-1")
    assert len(fake_client.listeners) == 1


async def test_stream_yields_silence_until_source_starts(fake_client: _FakeClient) -> None:
    """Before the client streams, the generator produces correctly sized silence."""
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    chunks = await _take(provider.get_audio_stream(_stream_details()), 2)
    expected_size = 48000 * 25 // 1000 * 4
    assert all(chunk == bytes(expected_size) for chunk in chunks)
    await provider.on_source_unselected("client-1", "queue-1", "session-1")


async def test_stream_switches_to_bridge_audio_after_stream_start(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client_stream/start routes decoded chunks through the bridge into the stream."""
    provider = make_provider([fake_client])
    bridge = _StubBridge()
    monkeypatch.setattr(provider, "_create_bridge", lambda *_args: bridge)
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    handle = _fake_handle([(b"\x01\x02\x03\x04", 1_000_000)])
    fake_client.emit(SourceStreamStartedEvent(audio_format=NATIVE_FORMAT, handle=handle))

    chunks = await _take(provider.get_audio_stream(_stream_details()), 5)
    assert any(chunk.startswith(MARKER_BYTE) for chunk in chunks)
    assert bridge.fed == [(b"\x01\x02\x03\x04", 1_000_000)]
    await provider.on_source_unselected("client-1", "queue-1", "session-1")


async def test_stream_ends_after_source_timeout(
    fake_client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generator ends on its own once no source audio arrives for the timeout."""
    monkeypatch.setattr(provider_module, "SOURCE_TIMEOUT_S", 0.05)
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    chunks = [chunk async for chunk in provider.get_audio_stream(_stream_details())]
    assert 1 <= len(chunks) <= 10


async def test_reconnect_re_requests_start(fake_client: _FakeClient) -> None:
    """A reconnect clears the client's start request, so the provider sends it again."""
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    assert fake_client.source_role is not None
    assert fake_client.source_role.start_requests == 1
    get_server_api(provider).emit(ClientConnectedEvent("client-1"))
    await asyncio.sleep(0)
    assert fake_client.source_role.start_requests == 2
    await provider.on_source_unselected("client-1", "queue-1", "session-1")


async def test_reconnect_leaves_an_open_stream_alone(fake_client: _FakeClient) -> None:
    """A client that kept its input stream across the event is not asked to start again."""
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    assert fake_client.source_role is not None
    fake_client.source_role.stream_active = True
    get_server_api(provider).emit(ClientConnectedEvent("client-1"))
    await asyncio.sleep(0)
    assert fake_client.source_role.start_requests == 1
    await provider.on_source_unselected("client-1", "queue-1", "session-1")


async def test_handoff_stops_the_player_it_was_taken_from(fake_client: _FakeClient) -> None:
    """Moving a source to another player stops the first, which would drain its buffer."""
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    await provider.on_source_selected("client-1", "player-2", "queue-2", "session-2")
    assert get_players(provider).stopped == ["player-1"]


async def test_reclaim_by_the_same_player_keeps_it_playing(fake_client: _FakeClient) -> None:
    """A reconnect re-claims the same queue with a fresh session and must not stop it."""
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-2")
    assert get_players(provider).stopped == []


async def test_two_sources_stream_concurrently(fake_client: _FakeClient) -> None:
    """Selecting a second source must not stop the first: exclusivity is per source."""
    other = _FakeClient("client-2", name="Aux")
    provider = make_provider([fake_client, other])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    await provider.on_source_selected("client-2", "player-2", "queue-2", "session-2")
    assert fake_client.source_role is not None
    assert other.source_role is not None
    assert fake_client.source_role.stop_requests == 0
    assert other.source_role.start_requests == 1
    assert get_players(provider).stopped == []
    chunks = await _take(provider.get_audio_stream(_stream_details("client-1")), 1)
    assert len(chunks) == 1


async def test_new_selection_supersedes_running_stream(fake_client: _FakeClient) -> None:
    """Re-selecting the same source elsewhere makes the previous generator terminate."""
    provider = make_provider([fake_client])
    await provider.on_source_selected("client-1", "player-1", "queue-1", "session-1")
    stream = provider.get_audio_stream(_stream_details())
    assert await anext(stream) is not None
    await provider.on_source_selected("client-1", "player-2", "queue-2", "session-2")
    chunks = [chunk async for chunk in stream]
    assert len(chunks) <= 1
