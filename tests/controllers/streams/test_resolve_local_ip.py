"""Tests for StreamsController.resolve_local_ip callback/source-IP resolution."""

from unittest.mock import MagicMock, patch

from music_assistant.controllers.streams.controller import StreamsController


def _ctrl(bind_ip: str = "0.0.0.0", publish_ip: str | None = "10.45.0.20") -> MagicMock:
    """Build a stand-in exposing only the bind_ip / publish_ip the method reads."""
    ctrl = MagicMock()
    ctrl.bind_ip = bind_ip
    ctrl.publish_ip = publish_ip
    return ctrl


async def test_prefers_explicit_bind_ip() -> None:
    """A concrete (non-wildcard) bind_ip is honoured as-is, skipping the lookup."""
    ctrl = _ctrl(bind_ip="10.0.0.5")
    with patch(
        "music_assistant.controllers.streams.controller.get_source_ip_to_target"
    ) as mock_lookup:
        result = await StreamsController.resolve_local_ip(ctrl, "10.10.20.31")
    assert result == "10.0.0.5"
    mock_lookup.assert_not_called()


async def test_uses_routing_lookup_when_bind_ip_wildcard() -> None:
    """With bind_ip=0.0.0.0, the per-device routing lookup result wins."""
    ctrl = _ctrl(bind_ip="0.0.0.0", publish_ip="10.45.0.20")
    with patch(
        "music_assistant.controllers.streams.controller.get_source_ip_to_target",
        return_value="10.10.20.106",
    ):
        result = await StreamsController.resolve_local_ip(ctrl, "10.10.20.31")
    assert result == "10.10.20.106"


async def test_falls_back_to_publish_ip_on_lookup_failure() -> None:
    """If the routing lookup is inconclusive, fall back to publish_ip (prior behaviour)."""
    ctrl = _ctrl(bind_ip="0.0.0.0", publish_ip="10.45.0.20")
    with patch(
        "music_assistant.controllers.streams.controller.get_source_ip_to_target",
        return_value=None,
    ):
        result = await StreamsController.resolve_local_ip(ctrl, "10.10.20.31")
    assert result == "10.45.0.20"


async def test_falls_back_to_bind_ip_without_publish_ip() -> None:
    """With no publish_ip and an inconclusive lookup, return the (wildcard) bind_ip."""
    ctrl = _ctrl(bind_ip="0.0.0.0", publish_ip=None)
    with patch(
        "music_assistant.controllers.streams.controller.get_source_ip_to_target",
        return_value=None,
    ):
        result = await StreamsController.resolve_local_ip(ctrl, "10.10.20.31")
    assert result == "0.0.0.0"
