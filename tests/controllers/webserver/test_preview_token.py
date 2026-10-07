"""Tests for the short-lived tokens that address the preview endpoint."""

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from music_assistant.controllers.webserver.controller import (
    MAX_PREVIEW_TOKENS,
    PREVIEW_TOKEN_TTL,
    WebserverController,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Yield a single-element list holding the monotonic time the controller reads."""
    now = [1000.0]
    monkeypatch.setattr(
        "music_assistant.controllers.webserver.controller.time.monotonic",
        lambda: now[0],
    )
    return now


@pytest.fixture
def webserver(mock_mass: MagicMock) -> WebserverController:
    """Create a WebserverController with a resolved base url."""
    webserver = WebserverController(mock_mass)
    config = MagicMock()
    config.get_value.return_value = False
    webserver.config = cast("CoreConfig", config)
    webserver.publish_port = 8095
    webserver._auto_base_url = "http://192.168.1.5:8095"
    return webserver


def test_preview_url_carries_a_token_and_not_the_item(webserver: WebserverController) -> None:
    """The url identifies the clip by token, so it reveals no provider or item."""
    url = webserver.create_preview_url("spotify--abc", "track123")
    assert "/preview?token=" in url
    assert "spotify--abc" not in url
    assert "track123" not in url


def test_preview_url_is_relative(webserver: WebserverController) -> None:
    """Clients reach this server on their own address, which we cannot guess for them."""
    url = webserver.create_preview_url("spotify--abc", "track123")
    assert url.startswith("/preview?")
    assert "://" not in url


def test_each_mint_is_a_fresh_token(webserver: WebserverController) -> None:
    """Handing out a new url must not invalidate one already in use."""
    first = webserver.create_preview_url("spotify--abc", "track123").split("token=")[1]
    second = webserver.create_preview_url("spotify--abc", "track456").split("token=")[1]
    assert first != second
    assert webserver._resolve_preview_token(first) == ("spotify--abc", "track123")
    assert webserver._resolve_preview_token(second) == ("spotify--abc", "track456")


def test_store_is_capped(webserver: WebserverController) -> None:
    """A chatty client cannot grow the store without bound."""
    for index in range(MAX_PREVIEW_TOKENS + 50):
        webserver.create_preview_url("spotify--abc", f"track{index}")
    assert len(webserver._preview_tokens) <= MAX_PREVIEW_TOKENS


def test_sweep_keeps_tokens_that_are_still_valid(
    webserver: WebserverController, clock: list[float]
) -> None:
    """Clearing out expired tokens must not take live ones with it."""
    stale = webserver.create_preview_url("spotify--abc", "old").split("token=")[1]
    clock[0] += PREVIEW_TOKEN_TTL - 1
    live = webserver.create_preview_url("spotify--abc", "new").split("token=")[1]
    clock[0] += 2
    webserver.create_preview_url("spotify--abc", "newest")
    assert stale not in webserver._preview_tokens
    assert webserver._resolve_preview_token(live) == ("spotify--abc", "new")


def test_token_resolves_to_the_item_it_was_minted_for(webserver: WebserverController) -> None:
    """A freshly minted token grants exactly the provider and item it was created with."""
    url = webserver.create_preview_url("spotify--abc", "track123")
    token = url.split("token=")[1]
    assert webserver._resolve_preview_token(token) == ("spotify--abc", "track123")


def test_token_stays_valid_for_repeated_requests(webserver: WebserverController) -> None:
    """Players re-request a media url they already opened, so a token is not single-use."""
    token = webserver.create_preview_url("spotify--abc", "track123").split("token=")[1]
    assert webserver._resolve_preview_token(token) is not None
    assert webserver._resolve_preview_token(token) is not None


@pytest.mark.parametrize("token", ["", "not-a-real-token"])
def test_unknown_token_is_refused(webserver: WebserverController, token: str) -> None:
    """A token that was never minted grants nothing."""
    assert webserver._resolve_preview_token(token) is None


def test_expired_token_is_refused_and_dropped(
    webserver: WebserverController, clock: list[float]
) -> None:
    """A token past its lifetime no longer resolves and leaves no entry behind."""
    token = webserver.create_preview_url("spotify--abc", "track123").split("token=")[1]
    clock[0] += PREVIEW_TOKEN_TTL + 1
    assert webserver._resolve_preview_token(token) is None
    assert token not in webserver._preview_tokens


def test_minting_sweeps_expired_tokens(webserver: WebserverController, clock: list[float]) -> None:
    """Expired tokens are cleared out as new ones are handed out."""
    stale = webserver.create_preview_url("spotify--abc", "track123").split("token=")[1]
    clock[0] += PREVIEW_TOKEN_TTL + 1
    webserver.create_preview_url("spotify--abc", "track456")
    assert stale not in webserver._preview_tokens
    assert len(webserver._preview_tokens) == 1


async def test_serve_preview_stream_refuses_an_unknown_token(
    webserver: WebserverController,
) -> None:
    """The endpoint no longer streams for a caller that just names a provider and item."""
    request = MagicMock()
    request.query = {"provider": "spotify--abc", "item_id": "track123"}
    with pytest.raises(web.HTTPNotFound):
        await webserver.serve_preview_stream(request)


async def test_serve_preview_stream_serves_the_item_the_token_was_minted_for(
    webserver: WebserverController, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid token streams that item, and nothing from the query string is consulted."""

    async def _clip() -> AsyncGenerator[bytes]:
        yield b"audio"

    get_preview_stream = MagicMock(return_value=_clip())
    monkeypatch.setattr(webserver.mass.streams, "get_preview_stream", get_preview_stream)
    token = webserver.create_preview_url("spotify--abc", "track123").split("token=")[1]
    request = MagicMock()
    # a mismatching provider/item in the query must be ignored entirely
    request.query = {"token": token, "provider": "tidal--xyz", "item_id": "other"}
    response = MagicMock()
    response.prepare = AsyncMock()
    response.write = AsyncMock()
    with patch(
        "music_assistant.controllers.webserver.controller.web.StreamResponse",
        MagicMock(return_value=response),
    ):
        await webserver.serve_preview_stream(request)
    get_preview_stream.assert_called_once_with("spotify--abc", "track123")
    response.write.assert_awaited_once_with(b"audio")
