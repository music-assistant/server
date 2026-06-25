"""Tests for music_assistant.helpers.util networking helpers."""

from unittest.mock import patch

import pytest

from music_assistant.helpers.util import get_source_ip_for_target


class TestGetSourceIpForTarget:
    """get_source_ip_for_target resolves a bindable, device-reachable source IP."""

    @pytest.mark.asyncio
    async def test_prefers_explicit_bind_ip(self) -> None:
        """A concrete (non-wildcard) bind_ip is honoured as-is."""
        result = await get_source_ip_for_target(
            "10.10.20.31", bind_ip="10.0.0.5", publish_ip="10.45.0.20"
        )
        assert result == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_uses_routing_lookup_when_bind_ip_wildcard(self) -> None:
        """With a wildcard bind_ip, the per-device routing lookup result wins."""
        with patch("music_assistant.helpers.util.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.getsockname.return_value = ("10.10.20.106", 0)
            result = await get_source_ip_for_target(
                "10.10.20.31", bind_ip="0.0.0.0", publish_ip="10.45.0.20"
            )
        assert result == "10.10.20.106"

    @pytest.mark.asyncio
    async def test_falls_back_to_publish_ip_on_lookup_failure(self) -> None:
        """If the routing lookup cannot resolve, fall back to publish_ip."""
        with patch("music_assistant.helpers.util.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = OSError("no route to host")
            result = await get_source_ip_for_target(
                "10.10.20.31", bind_ip="0.0.0.0", publish_ip="10.45.0.20"
            )
        assert result == "10.45.0.20"

    @pytest.mark.asyncio
    async def test_falls_back_to_bind_ip_when_no_publish_ip(self) -> None:
        """With no publish_ip and an inconclusive lookup, return the bind_ip as last resort."""
        with patch("music_assistant.helpers.util.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = OSError("no route to host")
            result = await get_source_ip_for_target("10.10.20.31", bind_ip="0.0.0.0", publish_ip="")
        assert result == "0.0.0.0"
