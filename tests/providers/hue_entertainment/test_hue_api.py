"""Tests for HueEntertainmentAPI."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from music_assistant.providers.hue_entertainment.hue_sendspin_bridge.api import HueEntertainmentAPI


@pytest.fixture
def api() -> HueEntertainmentAPI:
    """Return an API client pointed at a fake bridge."""
    return HueEntertainmentAPI("192.168.1.100", "test-app-key")


class TestHueEntertainmentAPI:
    """Tests for the REST API wrapper."""

    def test_base_url(self, api: HueEntertainmentAPI) -> None:
        """Test base URL construction."""
        assert api.base_url == "https://192.168.1.100"

    def test_host_setter(self, api: HueEntertainmentAPI) -> None:
        """Test host can be updated (e.g. after mDNS IP change)."""
        api.host = "192.168.1.200"
        assert api.host == "192.168.1.200"
        assert api.base_url == "https://192.168.1.200"

    @pytest.mark.asyncio
    async def test_get_entertainment_areas_parses_response(self, api: HueEntertainmentAPI) -> None:
        """Test parsing of entertainment configuration response."""
        mock_response = {
            "data": [
                {
                    "id": "area-uuid-1",
                    "metadata": {"name": "Living Room"},
                    "channels": [
                        {
                            "channel_id": 0,
                            "position": {"x": -0.5, "y": 0.8, "z": 0.0},
                            "members": [{"service": {"rid": "light-1", "rtype": "light"}}],
                        },
                        {
                            "channel_id": 1,
                            "position": {"x": 0.5, "y": 0.8, "z": 0.0},
                            "members": [{"service": {"rid": "light-2", "rtype": "light"}}],
                        },
                    ],
                },
                {
                    "id": "area-uuid-2",
                    "metadata": {"name": "Bedroom"},
                    "channels": [],
                },
            ]
        }

        with patch.object(api, "_request", new_callable=AsyncMock, return_value=mock_response):
            areas = await api.get_entertainment_areas()

        assert len(areas) == 2
        assert areas[0].id == "area-uuid-1"
        assert areas[0].name == "Living Room"
        assert len(areas[0].channels) == 2
        assert areas[0].channels[0].channel_id == 0
        assert areas[0].channels[0].service_id == "light-1"
        assert areas[0].channels[0].position == (-0.5, 0.8, 0.0)
        assert areas[0].channels[1].channel_id == 1

        assert areas[1].id == "area-uuid-2"
        assert areas[1].name == "Bedroom"
        assert len(areas[1].channels) == 0

    @pytest.mark.asyncio
    async def test_get_entertainment_areas_empty(self, api: HueEntertainmentAPI) -> None:
        """Test handling of empty entertainment config response."""
        with patch.object(api, "_request", new_callable=AsyncMock, return_value={"data": []}):
            areas = await api.get_entertainment_areas()

        assert areas == []

    @pytest.mark.asyncio
    async def test_start_entertainment(self, api: HueEntertainmentAPI) -> None:
        """Test start entertainment mode makes correct API call."""
        with patch.object(api, "_request", new_callable=AsyncMock) as mock_req:
            await api.start_entertainment("test-area-id")

        mock_req.assert_called_once_with(
            "PUT",
            "/clip/v2/resource/entertainment_configuration/test-area-id",
            json_data={"action": "start"},
        )

    @pytest.mark.asyncio
    async def test_stop_entertainment(self, api: HueEntertainmentAPI) -> None:
        """Test stop entertainment mode makes correct API call."""
        with patch.object(api, "_request", new_callable=AsyncMock) as mock_req:
            await api.stop_entertainment("test-area-id")

        mock_req.assert_called_once_with(
            "PUT",
            "/clip/v2/resource/entertainment_configuration/test-area-id",
            json_data={"action": "stop"},
        )

    @pytest.mark.asyncio
    async def test_stop_entertainment_ignores_errors(self, api: HueEntertainmentAPI) -> None:
        """Test that stop_entertainment doesn't raise on failure."""
        with patch.object(
            api, "_request", new_callable=AsyncMock, side_effect=aiohttp.ClientError("gone")
        ):
            # Should not raise
            await api.stop_entertainment("test-area-id")

    @pytest.mark.asyncio
    async def test_pair_success(self, api: HueEntertainmentAPI) -> None:
        """Test successful pairing returns credentials."""
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(
            return_value=[
                {
                    "success": {
                        "username": "test-username",
                        "clientkey": "AABBCCDD",
                    }
                }
            ]
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False

        with patch.object(api, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = await api.pair()

        assert result["username"] == "test-username"
        assert result["clientkey"] == "AABBCCDD"

    @pytest.mark.asyncio
    async def test_get_bridge_id(self, api: HueEntertainmentAPI) -> None:
        """Test fetching bridge ID."""
        mock_response = {"data": [{"id": "bridge-123"}]}
        with patch.object(api, "_request", new_callable=AsyncMock, return_value=mock_response):
            bridge_id = await api.get_bridge_id()

        assert bridge_id == "bridge-123"

    @pytest.mark.asyncio
    async def test_get_bridge_id_returns_none_on_error(self, api: HueEntertainmentAPI) -> None:
        """Test bridge ID returns None on failure."""
        with patch.object(
            api, "_request", new_callable=AsyncMock, side_effect=aiohttp.ClientError("err")
        ):
            result = await api.get_bridge_id()

        assert result is None

    @pytest.mark.asyncio
    async def test_close_session(self) -> None:
        """Test that close properly cleans up the session."""
        api = HueEntertainmentAPI("192.168.1.100")
        mock_session = AsyncMock()
        mock_session.closed = False
        api._session = mock_session
        await api.close()
        mock_session.close.assert_called_once()
