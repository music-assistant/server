"""Test Twitch Provider recommendations."""

from __future__ import annotations

from music_assistant_models.media_items import Radio, RecommendationFolder

from music_assistant.providers.twitch import TwitchProvider
from tests.providers.twitch.conftest import (
    MockResponse,
    load_fixture,
    make_mock_session_method,
)


def _users_response() -> MockResponse:
    """Return a mock users API response."""
    return MockResponse(status=200, json_data=load_fixture("user_lookup.json"))


def _setup_authenticated_provider(provider: TwitchProvider) -> None:
    """Configure provider with test credentials and cached data."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"
    provider._user_id = "99"


# --- Recommendations ---


async def test_recommendations_returns_live_channels_folder(provider: TwitchProvider) -> None:
    """recommendations() returns a single RecommendationFolder with live Radio items."""
    _setup_authenticated_provider(provider)

    fixture_channels = load_fixture("followed_channels.json")
    fixture_streams = load_fixture("live_streams.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data=fixture_streams),
            _users_response(),
        ]
    )

    result = await provider.recommendations()
    assert len(result) == 1
    folder = result[0]
    assert isinstance(folder, RecommendationFolder)
    assert folder.name == "Live Channels"
    assert len(folder.items) > 0
    assert all(isinstance(item, Radio) for item in folder.items)


async def test_recommendations_folder_contains_only_live(provider: TwitchProvider) -> None:
    """Offline followed channels are not in the recommendations folder."""
    _setup_authenticated_provider(provider)

    fixture_channels = load_fixture("followed_channels.json")
    fixture_streams = load_fixture("live_streams.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            # Return all 3 channels (page1 + page2)
            MockResponse(status=200, json_data=fixture_channels["page1"]),
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            # Only streamer_a and streamer_c are live
            MockResponse(status=200, json_data=fixture_streams),
            _users_response(),
        ]
    )

    result = await provider.recommendations()
    assert len(result) == 1
    logins = [item.item_id for item in result[0].items]
    assert "streamer_a" in logins
    assert "streamer_c" in logins
    assert "streamer_b" not in logins


async def test_recommendations_empty_when_none_live(provider: TwitchProvider) -> None:
    """Returns empty list (no folder) when no followed channels are live."""
    _setup_authenticated_provider(provider)

    fixture_channels = load_fixture("followed_channels.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data={"data": []}),  # nobody live
            _users_response(),
        ]
    )

    result = await provider.recommendations()
    assert result == []


async def test_recommendations_requires_auth(provider: TwitchProvider) -> None:
    """Returns empty list when not authenticated."""
    provider._access_token = None
    provider._user_id = None

    result = await provider.recommendations()
    assert result == []


async def test_recommendations_folder_metadata(provider: TwitchProvider) -> None:
    """RecommendationFolder has correct name, icon, and provider."""
    _setup_authenticated_provider(provider)

    fixture_channels = load_fixture("followed_channels.json")
    fixture_streams = load_fixture("live_streams.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data=fixture_streams),
            _users_response(),
        ]
    )

    result = await provider.recommendations()
    assert len(result) == 1
    folder = result[0]
    assert folder.name == "Live Channels"
    assert folder.icon == "mdi-broadcast"
    assert folder.provider == provider.instance_id
    assert folder.item_id == f"{provider.instance_id}_live_channels"
