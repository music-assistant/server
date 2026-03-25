"""E2E tests for provider HTTP API integration using WireMock replay."""

from __future__ import annotations

import json

import aiohttp
import pytest

from tests.support.wiremock import WireMockContainer


@pytest.mark.asyncio
async def test_wiremock_stubs_and_replays_provider_api_response(
    wiremock: WireMockContainer,
) -> None:
    """Given a WireMock stub, when the endpoint is called, the stubbed response is returned."""
    base_url = wiremock.get_base_url()

    # Given a stub registered for a mock provider's track-list endpoint
    stub_body = {
        "request": {"method": "GET", "url": "/api/tracks"},
        "response": {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "tracks": [
                        {"id": "t1", "title": "WireMock Track One"},
                        {"id": "t2", "title": "WireMock Track Two"},
                    ]
                }
            ),
        },
    }
    async with aiohttp.ClientSession() as session:
        stub_url = f"{base_url}/__admin/mappings"
        async with session.post(stub_url, json=stub_body) as resp:
            assert resp.status == 201, f"Failed to register stub: {await resp.text()}"

        # When the stubbed endpoint is called
        async with session.get(f"{base_url}/api/tracks") as resp:
            assert resp.status == 200
            data = await resp.json()

    # Then the response contains the stubbed track data
    tracks = data["tracks"]
    assert len(tracks) == 2
    assert tracks[0]["title"] == "WireMock Track One"
    assert tracks[1]["title"] == "WireMock Track Two"


@pytest.mark.asyncio
async def test_wiremock_verifies_request_was_received(wiremock: WireMockContainer) -> None:
    """Given a WireMock stub, when the endpoint is called, WireMock records the received request."""
    base_url = wiremock.get_base_url()

    # Given a stub for an artist search endpoint
    stub_body = {
        "request": {"method": "GET", "urlPattern": "/api/search\\?.*"},
        "response": {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"results": [{"id": "ar1", "name": "The Mock Band"}]}),
        },
    }
    async with aiohttp.ClientSession() as session:
        await session.post(f"{base_url}/__admin/mappings", json=stub_body)

        # When the search endpoint is called with a query parameter
        async with session.get(f"{base_url}/api/search?q=mock+band") as resp:
            assert resp.status == 200
            data = await resp.json()

        # And we check the recorded requests
        async with session.get(f"{base_url}/__admin/requests") as resp:
            requests_data = await resp.json()

    # Then WireMock recorded at least one request to the search endpoint
    received_requests = requests_data.get("requests", [])
    search_requests = [
        r for r in received_requests if "/api/search" in r.get("request", {}).get("url", "")
    ]
    assert len(search_requests) >= 1
    assert data["results"][0]["name"] == "The Mock Band"
