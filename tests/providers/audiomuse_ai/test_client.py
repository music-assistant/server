"""Tests for the AudioMuse-AI REST client (request shaping and error handling)."""

from __future__ import annotations

from typing import Any, Self, cast
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientError

from music_assistant.providers.audiomuse_ai.client import AudioMuseClient, AudioMuseError


class _FakeResponse:
    """Minimal stand-in for an aiohttp response context manager."""

    def __init__(self, status: int = 200, json_data: Any = None, text_data: str = "") -> None:
        self.status = status
        self._json = json_data
        self._text = text_data

    async def json(self) -> Any:
        return self._json

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Records requests and replays a canned response (or raises)."""

    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None) -> None:
        self._response = response or _FakeResponse()
        self._exc = exc
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, url, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._response


def _client(
    response: _FakeResponse | None = None,
    exc: Exception | None = None,
    token: str | None = None,
    base_url: str = "http://audiomuse:8000/",
) -> tuple[AudioMuseClient, _FakeSession]:
    session = _FakeSession(response=response, exc=exc)
    client = AudioMuseClient(cast("Any", session), base_url, token, MagicMock())
    return client, session


class TestRequestShaping:
    """URL, auth header and payload construction."""

    async def test_strips_trailing_slash_and_builds_url(self) -> None:
        """The base URL's trailing slash must not double up in request URLs."""
        client, session = _client(_FakeResponse(json_data=[]))
        await client.similar_tracks("ms-1", 5)
        _, url, kwargs = session.calls[0]
        assert url == "http://audiomuse:8000/api/similar_tracks"
        assert kwargs["params"] == {"item_id": "ms-1", "n": 5}

    async def test_token_becomes_bearer_header(self) -> None:
        """A configured API token is sent as a Bearer Authorization header."""
        client, session = _client(_FakeResponse(json_data=[]), token="s3cr3t")
        await client.similar_tracks("ms-1", 5)
        assert session.calls[0][2]["headers"] == {"Authorization": "Bearer s3cr3t"}

    async def test_no_token_no_auth_header(self) -> None:
        """Without a token no Authorization header is sent."""
        client, session = _client(_FakeResponse(json_data=[]))
        await client.similar_tracks("ms-1", 5)
        assert session.calls[0][2]["headers"] == {}

    async def test_clap_search_posts_json_body(self) -> None:
        """CLAP search POSTs the query as a JSON body and unwraps 'results'."""
        client, session = _client(_FakeResponse(json_data={"results": [{"item_id": "a"}]}))
        result = await client.clap_search("dreamy synths", 7)
        method, url, kwargs = session.calls[0]
        assert (method, url) == ("POST", "http://audiomuse:8000/api/clap/search")
        assert kwargs["json"] == {"query": "dreamy synths", "limit": 7}
        assert result == [{"item_id": "a"}]


class TestErrorHandling:
    """Non-200 statuses and transport errors become AudioMuseError."""

    async def test_non_200_raises(self) -> None:
        """A non-200 response raises AudioMuseError with the status in it."""
        client, _ = _client(_FakeResponse(status=503, text_data="overloaded"))
        with pytest.raises(AudioMuseError, match="503"):
            await client.similar_tracks("ms-1", 5)

    async def test_client_error_raises(self) -> None:
        """Aiohttp transport errors are wrapped in AudioMuseError."""
        client, _ = _client(exc=ClientError("connection refused"))
        with pytest.raises(AudioMuseError, match="failed"):
            await client.similar_tracks("ms-1", 5)

    async def test_health_swallows_errors(self) -> None:
        """health() reports False instead of raising on any failure."""
        client, _ = _client(exc=ClientError("connection refused"))
        assert await client.health() is False

    async def test_health_requires_ok_status(self) -> None:
        """health() is only True for an explicit status=ok payload."""
        client, _ = _client(_FakeResponse(json_data={"status": "degraded"}))
        assert await client.health() is False
        client, _ = _client(_FakeResponse(json_data={"status": "ok"}))
        assert await client.health() is True

    async def test_unexpected_payload_shape_degrades(self) -> None:
        """A non-list similar_tracks payload degrades to an empty list."""
        client, _ = _client(_FakeResponse(json_data={"weird": True}))
        assert await client.similar_tracks("ms-1", 5) == []

    async def test_clap_stats_swallows_errors(self) -> None:
        """clap_stats() returns {} instead of raising when the probe fails."""
        client, _ = _client(_FakeResponse(status=500, text_data="err"))
        assert await client.clap_stats() == {}
