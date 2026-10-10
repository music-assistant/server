"""File-info compatibility through the real Yandex Music client."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from ya_passport_auth import SecretStr
from yandex_music import ClientAsync

from music_assistant.helpers.throttle_retry import RequestPriority, request_priority
from music_assistant.providers.yandex_music.api_client import YandexMusicClient


async def test_file_info_uses_desktop_signature_and_normalized_codecs() -> None:
    """Encrypted file URLs use the matching desktop signature and client header."""
    underlying = ClientAsync("fake_token")
    request = AsyncMock(
        return_value={
            "downloadInfo": {
                "trackId": "12345",
                "quality": "lossless",
                "codec": "flac",
                "bitrate": 0,
                "transport": "encraw",
                "url": "https://cdn.example/audio",
                "urls": ["https://cdn.example/audio"],
                "key": "0123456789abcdef" * 2,
            }
        }
    )
    client = YandexMusicClient(token=SecretStr("fake_token"))
    client._client = underlying
    with (
        patch.object(underlying._request, "get", request),
        patch("yandex_music.utils.sign_request.datetime.datetime") as clock,
    ):
        clock.now.return_value = datetime.fromtimestamp(1234567890, UTC)
        result = await client.get_track_file_info(
            "12345", codecs=" flac-mp4, flac, ", transport="encraw"
        )
    assert result is not None
    assert result["url"] == "https://cdn.example/audio"
    assert result["needs_decryption"] is True
    assert result["key"] == "0123456789abcdef" * 2
    request.assert_awaited_once_with(
        "https://api.music.yandex.net/get-file-info",
        params={
            "ts": 1234567890,
            "trackId": "12345",
            "quality": "lossless",
            "codecs": "flac-mp4,flac",
            "transports": "encraw",
            "sign": "kb1/yWkWPisNC3GSMHOTvGOQhI/73fDGx9hA37mfkZY",
        },
        headers={"X-Yandex-Music-Client": "YandexMusicDesktopAppWindows/5.95.0"},
    )


@pytest.mark.parametrize("key", [None, ""])
async def test_file_info_empty_key_keeps_raw_stream(key: str | None) -> None:
    """Nullable model keys do not turn unencrypted URLs into encrypted streams."""
    underlying = ClientAsync("fake_token")
    client = YandexMusicClient(token=SecretStr("fake_token"))
    client._client = underlying
    response = {
        "downloadInfo": {
            "trackId": "42",
            "quality": "nq",
            "codec": "aac",
            "bitrate": 192,
            "transport": "raw",
            "url": "https://cdn.example/audio",
            "urls": [],
            "key": key,
        }
    }
    with patch.object(underlying._request, "get", AsyncMock(return_value=response)):
        result = await client.get_track_file_info("42", quality="nq", codecs="aac")
    assert result is not None
    assert result["needs_decryption"] is False
    assert "key" not in result
    assert result["bitrate"] == 192


@pytest.mark.parametrize(
    "response", [None, {}, {"downloadInfo": None}, {"downloadInfo": {"url": None}}]
)
async def test_file_info_missing_url_returns_none(response: dict[str, Any] | None) -> None:
    """Partial API models without a usable URL preserve the fallback contract."""
    underlying = ClientAsync("fake_token")
    client = YandexMusicClient(token=SecretStr("fake_token"))
    client._client = underlying
    with patch.object(underlying._request, "get", AsyncMock(return_value=response)):
        result = await client.get_track_file_info("42")
    assert result is None


async def test_concurrent_file_info_requests_reuse_one_fresh_url() -> None:
    """Concurrent playback lookups for the same variant spend one API request."""
    underlying = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = underlying
    response = {
        "downloadInfo": {
            "trackId": "42",
            "quality": "lossless",
            "codec": "flac",
            "bitrate": 0,
            "transport": "raw",
            "url": "https://cdn.example/fresh",
            "urls": [],
        }
    }

    async def respond(*_args: object, **_kwargs: object) -> dict[str, Any]:
        await asyncio.sleep(0)
        return response

    request = AsyncMock(side_effect=respond)
    with patch.object(underlying.request, "get", request):
        results = await asyncio.gather(*(client.get_track_file_info("42") for _ in range(3)))
    assert all(result and result["url"] == "https://cdn.example/fresh" for result in results)
    assert request.await_count == 1


async def test_cancelled_file_info_waiter_does_not_cancel_shared_lookup() -> None:
    """Cancelling a queued caller leaves the active URL lookup usable."""
    raw = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = raw
    started = asyncio.Event()
    release = asyncio.Event()
    response = {
        "downloadInfo": {
            "trackId": "42",
            "quality": "lossless",
            "codec": "flac",
            "bitrate": 0,
            "transport": "raw",
            "url": "https://cdn.example/fresh",
            "urls": [],
        }
    }

    async def respond(*_args: object, **_kwargs: object) -> dict[str, Any]:
        started.set()
        await release.wait()
        return response

    request = AsyncMock(side_effect=respond)
    with patch.object(raw.request, "get", request):
        active = asyncio.create_task(client.get_track_file_info("42"))
        await started.wait()
        waiter = asyncio.create_task(client.get_track_file_info("42"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        result = await active
        assert result is not None
        assert result["url"] == "https://cdn.example/fresh"
        assert await client.get_track_file_info("42") == result
    assert request.await_count == 1


async def test_cancelled_file_info_lookup_allows_next_waiter_to_retry() -> None:
    """Cancelling the in-flight request releases its variant lock for another caller."""
    raw = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = raw
    started = asyncio.Event()
    response = {
        "downloadInfo": {
            "trackId": "42",
            "quality": "lossless",
            "codec": "flac",
            "bitrate": 0,
            "transport": "raw",
            "url": "https://cdn.example/fresh",
            "urls": [],
        }
    }
    calls = 0

    async def respond(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        return response

    with patch.object(raw.request, "get", AsyncMock(side_effect=respond)):
        active = asyncio.create_task(client.get_track_file_info("42"))
        await started.wait()
        waiter = asyncio.create_task(client.get_track_file_info("42"))
        await asyncio.sleep(0)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        result = await asyncio.wait_for(waiter, timeout=2)
        assert result is not None
        assert result["url"] == "https://cdn.example/fresh"


async def test_concurrent_playback_refreshes_share_new_url_but_later_refresh_again() -> None:
    """Joined playback callers share a fresh URL; later playback rejects that cache."""
    raw = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = raw
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def respond(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
            await release.wait()
        return {
            "downloadInfo": {
                "trackId": "42",
                "quality": "lossless",
                "codec": "flac",
                "bitrate": 0,
                "transport": "raw",
                "url": f"https://cdn.example/url-{calls}",
                "urls": [],
            }
        }

    request = AsyncMock(side_effect=respond)
    with patch.object(raw.request, "get", request):
        old = await client.get_track_file_info("42")
        assert old is not None
        assert old["url"] == "https://cdn.example/url-1"
        with request_priority(RequestPriority.HIGH):
            active = asyncio.create_task(client.get_track_file_info("42"))
            await started.wait()
            waiters = [asyncio.create_task(client.get_track_file_info("42")) for _ in range(2)]
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(active, *waiters)
            assert all(r and r["url"] == "https://cdn.example/url-2" for r in results)
            assert request.await_count == 2
            later = await client.get_track_file_info("42")
        assert later is not None
        assert later["url"] == "https://cdn.example/url-3"
    assert request.await_count == 3


@pytest.mark.parametrize("block_during_refresh", [False, True])
async def test_joined_playback_refresh_bypasses_captcha_cooldown(
    block_during_refresh: bool,
) -> None:
    """Playback shares a refreshed URL during quarantine; ordinary callers stay blocked."""
    raw = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = raw
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def respond(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
            await release.wait()
        return {
            "downloadInfo": {
                "trackId": "42",
                "quality": "lossless",
                "codec": "flac",
                "bitrate": 0,
                "transport": "raw",
                "url": f"https://cdn.example/url-{calls}",
                "urls": [],
            }
        }

    request = AsyncMock(side_effect=respond)
    with patch.object(raw.request, "get", request):
        old = await client.get_track_file_info("42")
        assert old is not None
        assert old["url"] == "https://cdn.example/url-1"
        if not block_during_refresh:
            client._block_until["file_info"] = time.monotonic() + 300
        with request_priority(RequestPriority.HIGH):
            active = asyncio.create_task(client.get_track_file_info("42"))
            await started.wait()
            waiters = [asyncio.create_task(client.get_track_file_info("42")) for _ in range(2)]
        if block_during_refresh:
            client._block_until["file_info"] = time.monotonic() + 300
        ordinary = asyncio.create_task(client.get_track_file_info("42"))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(active, *waiters)
        assert all(r and r["url"] == "https://cdn.example/url-2" for r in results)
        assert await ordinary is None
        assert request.await_count == 2
        with request_priority(RequestPriority.HIGH):
            later = await client.get_track_file_info("42")
        assert later is not None
        assert later["url"] == "https://cdn.example/url-3"
        assert await client.get_track_file_info("42") is None
    assert request.await_count == 3
    assert not client._file_info_locks


@pytest.mark.parametrize("cancel_active", [False, True])
async def test_joined_playback_refresh_retries_after_missing_result(cancel_active: bool) -> None:
    """Failed or cancelled refreshes must not let playback reuse the stale URL."""
    raw = ClientAsync()
    client = YandexMusicClient(SecretStr("fake_token"))
    client._client = raw
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def respond(*_args: object, **_kwargs: object) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
            await release.wait()
            return None
        return {
            "downloadInfo": {
                "trackId": "42",
                "quality": "lossless",
                "codec": "flac",
                "bitrate": 0,
                "transport": "raw",
                "url": f"https://cdn.example/url-{calls}",
                "urls": [],
            }
        }

    request = AsyncMock(side_effect=respond)
    with patch.object(raw.request, "get", request):
        old = await client.get_track_file_info("42")
        assert old is not None
        assert old["url"] == "https://cdn.example/url-1"
        with request_priority(RequestPriority.HIGH):
            active = asyncio.create_task(client.get_track_file_info("42"))
            await started.wait()
            waiter = asyncio.create_task(client.get_track_file_info("42"))
            await asyncio.sleep(0)
            if cancel_active:
                active.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await active
            else:
                release.set()
                assert await active is None
            result = await asyncio.wait_for(waiter, timeout=2)
        assert result is not None
        assert result["url"] == "https://cdn.example/url-3"
    assert request.await_count == 3
