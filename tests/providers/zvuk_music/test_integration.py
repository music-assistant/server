"""Integration tests for the Zvuk Music provider via HTTP JSON-RPC API.

These tests require a real Zvuk API token set via ZVUK_TEST_TOKEN env var
and a free port 8095. They perform read-only operations only.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from tests.providers.zvuk_music.conftest import MATestClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def search_results(
    api_client: MATestClient,
    zvuk_provider_instance_id: str,  # noqa: ARG001
) -> dict[str, Any]:
    """Run a search and cache results for use across tests.

    :param api_client: Authenticated API client.
    :param zvuk_provider_instance_id: Ensures provider is loaded.
    """
    result: dict[str, Any] = await api_client.command(
        "music/search",
        search_query="\u041a\u0438\u043d\u043e",
        media_types=["track", "artist", "album"],
        limit=5,
    )
    return result


async def test_provider_loaded(
    api_client: MATestClient,
    zvuk_provider_instance_id: str,
) -> None:
    """Test that the Zvuk provider is loaded and listed in provider configs."""
    providers = await api_client.command(
        "config/providers",
        provider_domain="zvuk_music",
    )
    assert len(providers) > 0, "No Zvuk provider configs found"

    zvuk_conf = None
    for prov in providers:
        if prov["instance_id"] == zvuk_provider_instance_id:
            zvuk_conf = prov
            break

    assert zvuk_conf is not None, f"Provider {zvuk_provider_instance_id} not in config list"
    assert zvuk_conf["domain"] == "zvuk_music"
    assert zvuk_conf["enabled"] is True


async def test_search_tracks(
    search_results: dict[str, Any],
) -> None:
    """Test searching for tracks returns valid results."""
    tracks = search_results.get("tracks", [])
    assert len(tracks) > 0, "Search for '\u041a\u0438\u043d\u043e' returned no tracks"

    for track in tracks:
        assert track.get("name"), "Track missing name"
        assert track.get("item_id"), "Track missing item_id"


async def test_search_artists(
    search_results: dict[str, Any],
) -> None:
    """Test searching for artists returns valid results."""
    artists = search_results.get("artists", [])
    assert len(artists) > 0, "Search for '\u041a\u0438\u043d\u043e' returned no artists"

    for artist in artists:
        assert artist.get("name"), "Artist missing name"
        assert artist.get("item_id"), "Artist missing item_id"


async def test_search_albums(
    search_results: dict[str, Any],
) -> None:
    """Test searching for albums returns valid results."""
    albums = search_results.get("albums", [])
    assert len(albums) > 0, "Search for '\u041a\u0438\u043d\u043e' returned no albums"

    for album in albums:
        assert album.get("name"), "Album missing name"
        assert album.get("item_id"), "Album missing item_id"


async def test_get_track(
    api_client: MATestClient,
    search_results: dict[str, Any],
) -> None:
    """Test fetching a single track by ID from search results."""
    tracks = search_results.get("tracks", [])
    assert tracks, "No tracks from search to test get_track"

    track_id = tracks[0]["item_id"]
    provider = tracks[0]["provider"]

    result = await api_client.command(
        "music/item",
        media_type="track",
        item_id=track_id,
        provider_instance_id_or_domain=provider,
    )
    assert result["name"], "Track has no name"
    assert result["item_id"] == track_id
    assert result.get("provider_mappings"), "Track has no provider_mappings"


async def test_get_album(
    api_client: MATestClient,
    search_results: dict[str, Any],
) -> None:
    """Test fetching a single album by ID from search results."""
    albums = search_results.get("albums", [])
    assert albums, "No albums from search to test get_album"

    album_id = albums[0]["item_id"]
    provider = albums[0]["provider"]

    result = await api_client.command(
        "music/item",
        media_type="album",
        item_id=album_id,
        provider_instance_id_or_domain=provider,
    )
    assert result["name"], "Album has no name"
    assert result["item_id"] == album_id


async def test_get_artist(
    api_client: MATestClient,
    search_results: dict[str, Any],
) -> None:
    """Test fetching a single artist by ID from search results."""
    artists = search_results.get("artists", [])
    assert artists, "No artists from search to test get_artist"

    artist_id = artists[0]["item_id"]
    provider = artists[0]["provider"]

    result = await api_client.command(
        "music/item",
        media_type="artist",
        item_id=artist_id,
        provider_instance_id_or_domain=provider,
    )
    assert result["name"], "Artist has no name"
    assert result["item_id"] == artist_id
    assert result.get("provider_mappings"), "Artist has no provider_mappings"


async def test_sync_triggers(
    api_client: MATestClient,
    zvuk_provider_instance_id: str,
) -> None:
    """Test that sync can be triggered without errors for the Zvuk provider."""
    await api_client.command(
        "music/sync",
        providers=[zvuk_provider_instance_id],
    )

    synctasks = await api_client.command("music/synctasks")
    assert isinstance(synctasks, list)


async def test_browse_provider(
    api_client: MATestClient,
    zvuk_provider_instance_id: str,
) -> None:
    """Test browsing the Zvuk provider root."""
    result = await api_client.command(
        "music/browse",
        path=f"{zvuk_provider_instance_id}://",
    )
    assert isinstance(result, list)
