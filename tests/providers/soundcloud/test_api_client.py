"""Tests for the rate limiting of the Soundcloud api client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.errors import RateLimited

from music_assistant.providers.soundcloud import _SoundcloudAPI

_SLEEP = "music_assistant.helpers.throttle_retry.asyncio.sleep"


def _response(status: int, payload: Any = None, headers: dict[str, str] | None = None) -> MagicMock:
    """Build the async context manager returned by http_session.get."""
    response = MagicMock(status=status, headers=headers or {})
    response.json = AsyncMock(return_value=payload)
    request = MagicMock()
    request.__aenter__.return_value = response
    return request


def _api(*responses: MagicMock) -> _SoundcloudAPI:
    """Build an api client whose requests get the given responses in order."""
    http_session = Mock()
    http_session.get.side_effect = list(responses)
    return _SoundcloudAPI("OAuth token", "client-id", http_session, AsyncMock(), Mock())


async def test_rate_limit_response_raises_rate_limited() -> None:
    """A 429 response is raised as RateLimited carrying the Retry-After wait."""
    api = _api(_response(429, headers={"Retry-After": "7"}))
    get: Any = _SoundcloudAPI.get.__wrapped__  # type: ignore[attr-defined]

    with pytest.raises(RateLimited) as err:
        await get(api, "https://api-v2.soundcloud.com/me")

    assert err.value.backoff_time == 7


async def test_rate_limited_request_is_retried_after_the_wait() -> None:
    """A rate limited request waits as long as Soundcloud asks and is then retried."""
    api = _api(_response(429, headers={"Retry-After": "7"}), _response(200, {"id": 1}))

    with patch(_SLEEP, AsyncMock()) as sleep:
        result = await api.get("https://api-v2.soundcloud.com/me")

    assert result == {"id": 1}
    assert api.http_session.get.call_count == 2
    assert sleep.await_args is not None
    assert sleep.await_args.args[0] >= 7
    # the cooldown holds back every other request of this account as well
    assert api.throttler.cooldown_remaining > 0
