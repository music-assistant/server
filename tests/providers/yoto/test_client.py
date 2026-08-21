"""Tests for the Yoto API client adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError
from yoto_api import YotoAPIError

import music_assistant.providers.yoto.client as client_module
from music_assistant.providers.yoto.catalogue import Catalogue, encode_track_id
from music_assistant.providers.yoto.client import YotoAdapter


@dataclass
class _Token:
    """Minimal yoto-api token model."""

    refresh_token: str | None


@dataclass
class _Track:
    """Minimal yoto-api track model."""

    key: str = "track"
    title: str = "Track"
    trackUrl: str | None = None  # noqa: N815 - mirrors yoto-api's public model
    duration: int = 42
    format: str | None = "aac"
    channels: str | None = "stereo"
    icon: str | None = None
    type: str | None = "audio"


@dataclass
class _Chapter:
    """Minimal yoto-api chapter model."""

    key: str = "chapter"
    title: str | None = "Chapter"
    icon: str | None = None
    tracks: dict[str, _Track] = field(default_factory=lambda: {"track": _Track()})


@dataclass
class _Card:
    """Minimal yoto-api card model."""

    title: str = "Card"
    description: str | None = None
    author: str | None = None
    category: str | None = "stories"
    cover_image_large: str | None = None
    series_title: str | None = None
    series_order: int | None = None
    chapters: dict[str, _Chapter] = field(default_factory=lambda: {"chapter": _Chapter()})


class _API:
    """Controllable implementation of the yoto-api 4.3.4 boundary."""

    def __init__(self) -> None:
        self.token: _Token | None = None
        self.library: dict[str, Any] = {"card": _Card()}
        self.groups: dict[str, Any] = {}
        self.calls: list[str] = []
        self.active_details = 0
        self.max_active_details = 0

    def set_refresh_token(self, refresh_token: str) -> None:
        self.token = _Token(refresh_token)

    async def check_and_refresh_token(self) -> _Token:
        self.calls.append("auth")
        self.token = _Token("rotated-refresh-token")
        return self.token

    async def update_library(self) -> None:
        self.calls.append("library")
        self.library["card"] = _Card()

    async def update_card_detail(self, card_id: str) -> None:
        self.calls.append(f"detail:{card_id}")
        self.active_details += 1
        self.max_active_details = max(self.max_active_details, self.active_details)
        await asyncio.sleep(0)
        self.library[card_id].chapters["chapter"].tracks[
            "track"
        ].trackUrl = "https://media.example/audio.aac?signature=fresh"
        self.active_details -= 1

    async def update_groups(self) -> None:
        self.calls.append("groups")


async def test_authentication_persists_rotating_refresh_token() -> None:
    """A replacement refresh token is persisted immediately after refresh."""
    api = _API()
    persisted: list[str] = []
    adapter = YotoAdapter(
        "client-id",
        "old-refresh-token",
        api=api,
        token_callback=persisted.append,
    )

    await adapter.ensure_authenticated()

    assert persisted == ["rotated-refresh-token"]
    assert "old-refresh-token" not in repr(adapter)
    assert "rotated-refresh-token" not in repr(adapter)


async def test_failed_token_persistence_is_retried_before_accepting_rotation() -> None:
    """A transient persistence failure must not make a rotated token look durable."""
    api = _API()
    attempts = 0

    def persist(_refresh_token: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("fixture storage failure")

    adapter = YotoAdapter(
        "client-id",
        "old-refresh-token",
        api=api,
        token_callback=persist,
    )

    with pytest.raises(RuntimeError, match="storage failure"):
        await adapter.ensure_authenticated()
    await adapter.ensure_authenticated()

    assert attempts == 2


async def test_unexpected_authentication_and_persistence_errors_are_not_masked() -> None:
    """Only expected yoto-api failures are translated at the adapter boundary."""
    api = _API()

    async def unexpected_auth_failure() -> _Token:
        raise RuntimeError("programming defect")

    api.check_and_refresh_token = unexpected_auth_failure  # type: ignore[method-assign]
    adapter = YotoAdapter("client-id", "old-refresh-token", api=api)
    with pytest.raises(RuntimeError, match="programming defect"):
        await adapter.ensure_authenticated()

    async def expected_auth_failure() -> _Token:
        raise YotoAPIError("network unavailable")

    api.check_and_refresh_token = expected_auth_failure  # type: ignore[method-assign]
    with pytest.raises(Exception, match="authentication failed"):
        await adapter.ensure_authenticated()


async def test_raw_transport_timeouts_retain_temporary_failure_semantics() -> None:
    """Bare transport timeouts are translated at every yoto-api boundary."""
    auth_api = _API()

    async def auth_timeout() -> _Token:
        raise TimeoutError("auth timed out")

    auth_api.check_and_refresh_token = auth_timeout  # type: ignore[method-assign]
    with pytest.raises(LoginFailed, match="authentication failed"):
        await YotoAdapter("client-id", "old-refresh-token", api=auth_api).ensure_authenticated()

    stream_api = _API()

    async def detail_timeout(_card_id: str) -> None:
        raise TimeoutError("detail timed out")

    cast("Any", stream_api).update_card_detail = detail_timeout
    with pytest.raises(ProviderUnavailableError, match="stream is unavailable"):
        await YotoAdapter("client-id", "old-refresh-token", api=stream_api).resolve_stream(
            encode_track_id("card", "chapter", "track")
        )

    catalogue_api = _API()

    async def library_timeout() -> None:
        raise TimeoutError("library timed out")

    catalogue_api.update_library = library_timeout  # type: ignore[method-assign]
    with pytest.raises(ProviderUnavailableError, match="refresh the Yoto library"):
        await YotoAdapter("client-id", "old-refresh-token", api=catalogue_api).refresh_catalogue()


async def test_stream_resolution_clears_stale_url_and_refetches_just_in_time() -> None:
    """Stream lookup cannot reuse the signed URL already held by yoto-api."""
    api = _API()
    track = api.library["card"].chapters["chapter"].tracks["track"]
    track.trackUrl = "https://media.example/stale?signature=secret"

    async def refresh_without_url(_card_id: str) -> None:
        api.calls.append("detail:card")
        assert track.trackUrl is None

    api.update_card_detail = refresh_without_url  # type: ignore[assignment]
    adapter = YotoAdapter("client-id", "old-refresh-token", api=api)
    item_id = encode_track_id("card", "chapter", "track")

    with pytest.raises(ProviderUnavailableError, match="stream is unavailable") as err:
        await adapter.resolve_stream(item_id)

    assert "signature=secret" not in str(err.value)
    assert api.calls == ["auth", "detail:card"]


async def test_stream_resolution_accepts_only_https_and_hides_url_from_repr() -> None:
    """Only a fresh HTTPS URL is returned and it never appears in representations."""
    api = _API()
    adapter = YotoAdapter("client-id", "old-refresh-token", api=api)
    item_id = encode_track_id("card", "chapter", "track")

    stream = await adapter.resolve_stream(item_id)

    assert stream.path.startswith("https://")
    assert stream.duration == 42
    assert stream.format == "aac"
    assert "media.example" not in repr(stream)

    async def insecure_refresh(card_id: str) -> None:
        api.library[card_id].chapters["chapter"].tracks["track"].trackUrl = "http://media.example"

    api.update_card_detail = insecure_refresh  # type: ignore[method-assign]
    with pytest.raises(ProviderUnavailableError, match="stream is unavailable"):
        await adapter.resolve_stream(item_id)


async def test_stream_resolution_does_not_mask_unexpected_model_errors() -> None:
    """Programming defects must not be downgraded to temporary stream failures."""
    api = _API()

    async def unexpected_failure(_card_id: str) -> None:
        raise RuntimeError("unexpected model shape")

    cast("Any", api).update_card_detail = unexpected_failure
    adapter = YotoAdapter("client-id", "old-refresh-token", api=api)

    with pytest.raises(RuntimeError, match="unexpected model shape"):
        await adapter.resolve_stream(encode_track_id("card", "chapter", "track"))


async def test_catalogue_refresh_returns_url_free_snapshot_and_serializes_client_mutation() -> None:
    """Catalogue creation strips URLs and mutable yoto-api calls never overlap."""
    api = _API()
    adapter = YotoAdapter("client-id", "old-refresh-token", api=api)

    catalogue, stream = await asyncio.gather(
        adapter.refresh_catalogue(),
        adapter.resolve_stream(encode_track_id("card", "chapter", "track")),
    )

    assert isinstance(catalogue, Catalogue)
    assert "media.example" not in repr(catalogue)
    assert stream.path.startswith("https://")
    assert api.max_active_details == 1


async def test_catalogue_refresh_drops_cards_removed_from_the_remote_library() -> None:
    """Each refresh is a snapshot rather than an accumulation of stale yoto-api models."""
    api = _API()
    api.library["removed"] = _Card(title="Removed card")

    async def update_library() -> None:
        api.calls.append("library")
        api.library["card"] = _Card()

    api.update_library = update_library  # type: ignore[method-assign]
    adapter = YotoAdapter("client-id", "old-refresh-token", api=api)

    catalogue = await adapter.refresh_catalogue()

    assert list(catalogue.cards) == ["card"]


async def test_catalogue_detail_requests_are_explicitly_throttled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unavoidable per-card detail endpoint is paced between library cards."""
    api = _API()

    async def update_library() -> None:
        api.calls.append("library")
        api.library["card"] = _Card()
        api.library["second"] = _Card(title="Second")

    api.update_library = update_library  # type: ignore[method-assign]
    sleep = AsyncMock()
    monkeypatch.setattr("music_assistant.providers.yoto.client.asyncio.sleep", sleep)
    adapter = YotoAdapter("client-id", "old-refresh-token", api=api)

    await adapter.refresh_catalogue()
    await adapter.refresh_catalogue()

    assert [call for call in api.calls if call.startswith("detail:")] == [
        "detail:card",
        "detail:second",
    ]
    sleep.assert_any_await(client_module.CARD_DETAIL_REQUEST_DELAY)
