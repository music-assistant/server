"""Tests for the WiiM provider's UPnP event-callback host resolution."""

from unittest.mock import MagicMock, patch

import pytest

from music_assistant.providers.wiim.provider import _resolve_callback_host


def _mass(bind_ip: str = "0.0.0.0", publish_ip: str = "10.45.0.20") -> MagicMock:
    """Build a mock MusicAssistant exposing only streams.bind_ip / publish_ip."""
    mass = MagicMock()
    mass.streams.bind_ip = bind_ip
    mass.streams.publish_ip = publish_ip
    return mass


class TestResolveCallbackHost:
    """_resolve_callback_host returns a bindable, device-reachable source IP."""

    @pytest.mark.asyncio
    async def test_prefers_explicit_bind_ip(self) -> None:
        """A concrete (non-wildcard) stream bind_ip is honoured as-is."""
        mass = _mass(bind_ip="10.0.0.5", publish_ip="10.45.0.20")
        result = await _resolve_callback_host(mass, "10.10.20.31")
        assert result == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_uses_routing_lookup_when_bind_ip_wildcard(self) -> None:
        """With bind_ip=0.0.0.0, the per-device routing lookup result wins."""
        mass = _mass(bind_ip="0.0.0.0", publish_ip="10.45.0.20")
        with patch("music_assistant.providers.wiim.provider.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.getsockname.return_value = ("10.10.20.106", 0)
            result = await _resolve_callback_host(mass, "10.10.20.31")
        assert result == "10.10.20.106"

    @pytest.mark.asyncio
    async def test_falls_back_to_publish_ip_on_lookup_failure(self) -> None:
        """If the routing lookup cannot resolve, fall back to publish_ip (prior behaviour)."""
        mass = _mass(bind_ip="0.0.0.0", publish_ip="10.45.0.20")
        with patch("music_assistant.providers.wiim.provider.socket.socket") as mock_socket:
            sock = mock_socket.return_value.__enter__.return_value
            sock.connect.side_effect = OSError("no route to host")
            result = await _resolve_callback_host(mass, "10.10.20.31")
        assert result == "10.45.0.20"
