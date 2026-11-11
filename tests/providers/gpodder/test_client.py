"""Tests for the gPodder API client."""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from typing import TYPE_CHECKING, Any, cast

import pytest

from music_assistant.providers.gpodder.client import GPodderClient

if TYPE_CHECKING:
    import aiohttp


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.content_type = "application/json"
        self._body = json.dumps(body).encode()
        self.released = False

    async def read(self) -> bytes:
        return self._body

    def release(self) -> None:
        self.released = True


class _FakeRequest:
    """Stand-in for aiohttp's request context, usable with await and with async with."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def __await__(self) -> Generator[Any, None, _FakeResponse]:
        return self._get().__await__()

    async def _get(self) -> _FakeResponse:
        return self._response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info: object) -> None:
        self._response.release()


def _client(response: _FakeResponse) -> GPodderClient:
    session = type("Session", (), {"get": lambda *_args, **_kwargs: _FakeRequest(response)})()
    client = GPodderClient(cast("aiohttp.ClientSession", session), logging.getLogger(__name__))
    client.init_nc(base_url="https://cloud.example.com", nc_token="token")
    return client


async def test_failed_call_releases_its_connection() -> None:
    """A failing call hands its connection back to the shared session."""
    response = _FakeResponse(500, {})

    with pytest.raises(RuntimeError):
        await _client(response).get_episode_actions()

    assert response.released
