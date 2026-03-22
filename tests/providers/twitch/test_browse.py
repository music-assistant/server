"""Test Twitch Provider browse, library radios, and search."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import BrowseFolder, Radio

from music_assistant.providers.twitch import TwitchProvider
from tests.providers.twitch.conftest import MockResponse, make_mock_session_method

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file."""
    with (FIXTURES / name).open() as f:
        return json.load(f)  # type: ignore[no-any-return]


def _users_response() -> MockResponse:
    """Return a mock users API response."""
    return MockResponse(status=200, json_data=load_fixture("user_lookup.json"))


def _setup_authenticated_provider(provider: TwitchProvider) -> None:
    """Configure provider with test credentials and cached data."""
    provider._access_token = "test_token"
    provider._client_id = "test_client"
    provider._user_id = "99"


# --- Library Radios (Live Only) ---


async def test_library_radios_yields_radio_items(provider: TwitchProvider) -> None:
    """get_library_radios() yields Radio objects."""
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

    radios = [item async for item in provider.get_library_radios()]
    assert len(radios) > 0
    assert all(isinstance(r, Radio) for r in radios)


async def test_library_radios_only_live(provider: TwitchProvider) -> None:
    """Offline followed channels are not yielded."""
    _setup_authenticated_provider(provider)

    # Channel B (456) is followed but not in live_streams fixture
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

    radios = [item async for item in provider.get_library_radios()]
    logins = [r.item_id for r in radios]
    assert "streamer_a" in logins
    assert "streamer_c" in logins
    assert "streamer_b" not in logins  # offline — not in library


async def test_library_radios_item_fields(provider: TwitchProvider) -> None:
    """Each Radio has correct item_id (login), name (display_name), provider."""
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

    radios = [item async for item in provider.get_library_radios()]
    assert len(radios) > 0
    radio = radios[0]
    assert radio.item_id  # has an ID
    assert radio.name  # has a name
    assert radio.provider == provider.domain


async def test_library_radios_empty_when_none_live(provider: TwitchProvider) -> None:
    """Returns empty when no followed channels are live."""
    _setup_authenticated_provider(provider)

    fixture_channels = load_fixture("followed_channels.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data={"data": []}),  # nobody live
            _users_response(),
        ]
    )

    radios = [item async for item in provider.get_library_radios()]
    assert radios == []


async def test_library_radios_requires_auth(provider: TwitchProvider) -> None:
    """Returns empty when not authenticated."""
    provider._access_token = None
    provider._user_id = None

    radios = [item async for item in provider.get_library_radios()]
    assert radios == []


# --- Browse Structure ---


async def test_browse_root_returns_two_folders(provider: TwitchProvider) -> None:
    """browse("") returns "Live" and "Following" BrowseFolder items."""
    _setup_authenticated_provider(provider)
    items = await provider.browse("")
    folder_names = [f.name for f in items if isinstance(f, BrowseFolder)]
    assert "Live" in folder_names
    assert "Following" in folder_names


async def test_browse_live_returns_only_live_channels(provider: TwitchProvider) -> None:
    """browse("Live") returns only currently live channels."""
    _setup_authenticated_provider(provider)

    fixture_channels = load_fixture("followed_channels.json")
    fixture_streams = load_fixture("live_streams.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data=fixture_channels["page1"]),
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data=fixture_streams),
            _users_response(),
        ]
    )

    items = await provider.browse(f"{provider.instance_id}://live")
    assert len(items) > 0
    # Should only contain live channels
    item_ids = [getattr(r, "item_id", None) for r in items]
    assert "streamer_a" in item_ids
    assert "streamer_b" not in item_ids  # offline


async def test_browse_following_returns_all_channels(
    provider: TwitchProvider,
) -> None:
    """browse("Following") returns all followed channels."""
    _setup_authenticated_provider(provider)

    fixture_channels = load_fixture("followed_channels.json")
    fixture_streams = load_fixture("live_streams.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data=fixture_channels["page1"]),
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data=fixture_streams),
            _users_response(),
        ]
    )

    items = await provider.browse(f"{provider.instance_id}://following")
    item_ids = [getattr(r, "item_id", None) for r in items]
    assert "streamer_a" in item_ids
    assert "streamer_b" in item_ids  # offline but still in Following
    assert "streamer_c" in item_ids


async def test_browse_following_marks_offline(provider: TwitchProvider) -> None:
    """Offline channels in Following browse have '(offline)' in name."""
    _setup_authenticated_provider(provider)

    fixture_channels = load_fixture("followed_channels.json")
    fixture_streams = load_fixture("live_streams.json")

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=200, json_data=fixture_channels["page1"]),
            MockResponse(status=200, json_data=fixture_channels["page2"]),
            MockResponse(status=200, json_data=fixture_streams),
            _users_response(),
        ]
    )

    items = await provider.browse(f"{provider.instance_id}://following")
    # Find streamer_b (offline)
    offline_items = [r for r in items if getattr(r, "item_id", None) == "streamer_b"]
    assert len(offline_items) == 1
    assert "(offline)" in offline_items[0].name.lower()


async def test_browse_invalid_path_returns_empty(provider: TwitchProvider) -> None:
    """Unknown browse path returns empty list."""
    _setup_authenticated_provider(provider)
    items = await provider.browse(f"{provider.instance_id}://nonexistent")
    assert items == []


# --- Search ---


async def test_search_returns_matching_channels(provider: TwitchProvider) -> None:
    """search() returns channels matching query from Twitch search API."""
    _setup_authenticated_provider(provider)

    fixture = load_fixture("search_results.json")
    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=200, json_data=fixture)
    )

    results = await provider.search("streamer", [MediaType.RADIO])
    assert len(results.radio) > 0


async def test_search_results_are_radio_type(provider: TwitchProvider) -> None:
    """Search results contain Radio items."""
    _setup_authenticated_provider(provider)

    fixture = load_fixture("search_results.json")
    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=200, json_data=fixture)
    )

    results = await provider.search("streamer", [MediaType.RADIO])
    assert all(isinstance(r, Radio) for r in results.radio)


async def test_search_empty_query_returns_empty(provider: TwitchProvider) -> None:
    """Empty search query returns empty results."""
    _setup_authenticated_provider(provider)

    results = await provider.search("", [MediaType.RADIO])
    assert len(results.radio) == 0


async def test_search_unauthenticated_returns_empty(provider: TwitchProvider) -> None:
    """Search without auth returns empty result, not crash."""
    provider._access_token = None

    results = await provider.search("test", [MediaType.RADIO])
    assert len(results.radio) == 0
