"""Test Twitch API client: pagination, batching, caching, error handling."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.twitch import TwitchProvider
from tests.providers.twitch.conftest import MockResponse, make_mock_session_method

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file."""
    with (FIXTURES / name).open() as f:
        return json.load(f)  # type: ignore[no-any-return]


# --- Request Pattern ---


async def test_get_includes_auth_headers(provider: TwitchProvider) -> None:
    """GET requests include Authorization and Client-Id headers."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=200, json_data={"data": []})
    )

    await provider._api_get("/helix/streams")

    call_kwargs = provider.mass.http_session.get.call_args
    headers = call_kwargs.kwargs.get("headers", {})
    assert headers["Authorization"] == "Bearer test_token"
    assert headers["Client-Id"] == "test_client"


async def test_unauthenticated_request_raises(provider: TwitchProvider) -> None:
    """API call without tokens raises LoginFailed on 401."""
    provider._access_token = None
    provider._refresh_token = None
    provider._client_id = "test_client"

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=401)
    )
    provider.mass.http_session.post = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=401, text_data="bad refresh")
    )

    with pytest.raises(LoginFailed):
        await provider._api_get("/helix/users")


async def test_non_200_raises(provider: TwitchProvider) -> None:
    """Non-success status codes raise an exception."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=500)
    )

    with pytest.raises(Exception, match=r"500"):
        await provider._api_get("/helix/streams")


# --- Pagination ---


async def test_followed_channels_paginates(provider: TwitchProvider) -> None:
    """Multiple pages fetched via cursor until no cursor returned."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"
    provider._user_id = "99"

    fixture = load_fixture("followed_channels.json")
    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data=fixture["page1"]),
            MockResponse(status=200, json_data=fixture["page2"]),
        ]
    )

    channels = await provider._get_followed_channels()
    assert len(channels) == 3
    assert provider.mass.http_session.get.call_count == 2


async def test_single_page_no_extra_requests(provider: TwitchProvider) -> None:
    """When no cursor in response, only one request made."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"
    provider._user_id = "99"

    fixture = load_fixture("followed_channels.json")
    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=200, json_data=fixture["page2"])
    )

    channels = await provider._get_followed_channels()
    assert len(channels) == 1
    assert provider.mass.http_session.get.call_count == 1


# --- Batching ---


async def test_live_streams_batches_over_100(provider: TwitchProvider) -> None:
    """150 user IDs split into batch of 100 + batch of 50."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"

    user_ids = [str(i) for i in range(150)]

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data={"data": [{"user_id": "1"}]}),
            MockResponse(status=200, json_data={"data": [{"user_id": "101"}]}),
        ]
    )

    streams = await provider._get_live_streams(user_ids)
    assert len(streams) == 2
    assert provider.mass.http_session.get.call_count == 2


async def test_live_streams_empty_input(provider: TwitchProvider) -> None:
    """Empty user ID list returns empty without API call."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"

    streams = await provider._get_live_streams([])
    assert streams == []


# --- Caching ---


async def test_live_status_cached_within_ttl(provider: TwitchProvider) -> None:
    """Second call within 5 minutes returns cached result, no API call."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"
    provider._user_id = "99"

    fixture_channels = load_fixture("followed_channels.json")
    fixture_streams = load_fixture("live_streams.json")

    fixture_users = load_fixture("user_lookup.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data=fixture_streams),
            MockResponse(status=200, json_data=fixture_users),
        ]
    )

    # First call — fetches from API
    await provider._get_followed_live_status()
    call_count = provider.mass.http_session.get.call_count

    # Second call — should use cache
    await provider._get_followed_live_status()
    assert provider.mass.http_session.get.call_count == call_count  # no new calls


async def test_live_status_refreshed_after_ttl(provider: TwitchProvider) -> None:
    """Call after 5 minutes makes fresh API request."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"
    provider._user_id = "99"

    fixture_channels = load_fixture("followed_channels.json")
    fixture_streams = load_fixture("live_streams.json")
    fixture_users = load_fixture("user_lookup.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            # First fetch
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data=fixture_streams),
            MockResponse(status=200, json_data=fixture_users),
            # Second fetch after TTL
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data=fixture_streams),
            MockResponse(status=200, json_data=fixture_users),
        ]
    )

    await provider._get_followed_live_status()
    call_count_after_first = provider.mass.http_session.get.call_count

    # Expire the cache
    provider._cache_time = time.monotonic() - 301  # past 5 min TTL

    await provider._get_followed_live_status()
    assert provider.mass.http_session.get.call_count > call_count_after_first


async def test_cache_cleared_on_logout(provider: TwitchProvider) -> None:
    """Logout invalidates the cache."""
    provider._cached_channels = [{"broadcaster_id": "123"}]
    provider._cached_live = {"streamer_a": {"viewer_count": 100}}
    provider._cache_time = time.monotonic()

    provider._clear_cache()

    assert provider._cached_channels is None
    assert provider._cached_live is None  # type: ignore[unreachable]
    assert provider._cache_time == 0.0


# --- User Lookup ---


async def test_get_users_resolves_login_to_id(provider: TwitchProvider) -> None:
    """GET /users?login=X returns user dict with numeric ID."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"

    fixture = load_fixture("user_lookup.json")
    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=200, json_data=fixture)
    )

    users = await provider._get_users(logins=["streamer_a"])
    assert len(users) == 1
    assert users[0]["id"] == "123"
    assert users[0]["login"] == "streamer_a"
