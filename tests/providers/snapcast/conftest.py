"""Fixtures for Snapcast provider tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.snapcast.constants import DEFAULT_SNAPCAST_FORMAT


class FakeSnapstream:
    """Minimal Snapstream stand-in for testing ma_stream._register_tcp_server_source."""

    def __init__(self, identifier: str, name: str, path: str = "") -> None:
        """Initialize with stream identifier, name, and optional path."""
        self.identifier = identifier
        self.name = name
        self.path = path
        self._stream: dict[str, Any] = {"uri": {"host": "127.0.0.1"}}
        self._callback = None

    def set_callback(self, cb: Any) -> None:
        """Register a callback for stream events."""
        self._callback = cb


class FakeSnapserver:
    """Configurable mock implementing the subset of SnapserverProto used by ma_stream."""

    def __init__(self) -> None:
        """Initialize with empty stream registry and response queues."""
        # streams added via stream_add_stream are kept here keyed by id
        self._streams_by_id: dict[str, FakeSnapstream] = {}
        # responses to return for each call to stream_add_stream, in order
        # Each entry is a dict that looks like the snapserver result OR error payload.
        self._add_stream_responses: list[dict[str, Any]] = []
        # Calls captured for assertions
        self.add_stream_calls: list[str] = []

        self.stream_add_stream = AsyncMock(side_effect=self._add_stream_impl)
        self.stream_remove_stream = AsyncMock()
        self.status = AsyncMock(side_effect=self._status_impl)
        self._status_payload: dict[str, Any] | None = None

    @property
    def streams(self) -> list[FakeSnapstream]:
        """Return all registered streams."""
        return list(self._streams_by_id.values())

    def stream(self, identifier: str) -> FakeSnapstream:
        """Return the stream with the given identifier."""
        return self._streams_by_id[identifier]

    def synchronize(self, status: dict[str, Any]) -> None:
        """Populate _streams_by_id from a status payload, simulating real Snapserver."""
        streams = status.get("server", {}).get("streams", [])
        for s in streams:
            sid = s.get("id")
            if not sid:
                continue
            if sid not in self._streams_by_id:
                self._streams_by_id[sid] = FakeSnapstream(sid, name=s.get("name", sid))

    async def _status_impl(self) -> tuple[Any, Any]:
        # status() in the real Snapserver is async and returns (result, error)
        return (self._status_payload, None)

    async def _add_stream_impl(self, stream_uri: str) -> dict[str, Any]:
        self.add_stream_calls.append(stream_uri)
        if self._add_stream_responses:
            response = self._add_stream_responses.pop(0)
        else:
            response = {
                "code": -32603,
                "data": "Generic snapserver error",
                "message": "Internal error",
            }
        # On a successful add, also register the stream in our internal store
        if isinstance(response, dict) and "id" in response:
            stream_id = response["id"]
            # parse name= from uri for realism
            name = stream_uri.split("&name=", 1)[1] if "&name=" in stream_uri else stream_id
            self._streams_by_id[stream_id] = FakeSnapstream(stream_id, name, path=stream_uri)
        return response

    def queue_response(self, response: dict[str, Any]) -> None:
        """Queue a response that the next stream_add_stream call will return."""
        self._add_stream_responses.append(response)

    def queue_success(self, stream_id: str = "stream-1") -> None:
        """Queue a successful stream_add_stream response with the given stream id."""
        self.queue_response({"id": stream_id})

    def queue_name_collision(self) -> None:
        """Queue a name-already-exists error response."""
        self.queue_response(
            {
                "code": -32603,
                "data": 'Stream with name "x" already exists',
                "message": "Internal error",
            }
        )

    def queue_other_error(self, data: str = "bind: Address already in use") -> None:
        """Queue a generic (non-name-collision) error response."""
        self.queue_response({"code": -32603, "data": data, "message": "Internal error"})

    def cache_stream_directly(self, stream_id: str, name: str) -> FakeSnapstream:
        """
        Pre-register a stream in the local cache (skipping the status round-trip).

        Use this when the test wants the orphan to be visible WITHOUT requiring
        adopt() to call status() + synchronize() first.
        """
        s = FakeSnapstream(stream_id, name)
        self._streams_by_id[stream_id] = s
        return s

    def stage_orphan_stream(self, stream_id: str, name: str) -> None:
        """
        Stage an orphan stream that only appears after .synchronize(.status()).

        Use this when the test wants to verify that adopt() falls back to a
        status-driven resync (the actual Bug B post-MA-restart scenario).
        The stream is NOT in _streams_by_id until synchronize() is called.
        """
        self._status_payload = {
            "server": {
                "streams": [{"id": stream_id, "name": name}],
                "groups": [],
            }
        }


@pytest.fixture
def fake_snapserver() -> FakeSnapserver:
    """Provide a fresh FakeSnapserver for each test."""
    return FakeSnapserver()


@pytest.fixture
def fake_provider(fake_snapserver: FakeSnapserver) -> MagicMock:
    """Provide a minimal SnapCastProvider mock wired to fake_snapserver."""
    provider = MagicMock()
    provider._snapserver = fake_snapserver
    provider._snapcast_stream_idle_threshold = 60000
    provider._use_builtin_server = False
    provider.stream_audio_format = DEFAULT_SNAPCAST_FORMAT
    provider.logger = MagicMock()
    return provider
