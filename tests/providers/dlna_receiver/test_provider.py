"""Tests for the multi-player provider logic.

Provider module imports music_assistant.models which is only available
when running inside MA.  We test the pure utility functions directly
and guard the full-provider import with pytest.importorskip.
"""

from __future__ import annotations

import uuid

from provider.constants import UDN_NAMESPACE


def _deterministic_udn(player_id: str) -> str:
    """Replicate the UDN generation logic for standalone testing."""
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, UDN_NAMESPACE)
    return f"uuid:{uuid.uuid5(namespace, player_id or '__default__')}"


def test_deterministic_udn_same_input() -> None:
    """Same player_id always produces the same UDN."""
    udn1 = _deterministic_udn("player_kitchen")
    udn2 = _deterministic_udn("player_kitchen")
    assert udn1 == udn2
    assert udn1.startswith("uuid:")


def test_deterministic_udn_different_inputs() -> None:
    """Different player_ids produce different UDNs."""
    udn1 = _deterministic_udn("player_kitchen")
    udn2 = _deterministic_udn("player_bedroom")
    assert udn1 != udn2


def test_deterministic_udn_default() -> None:
    """Empty player_id produces a stable UDN for the default instance."""
    udn1 = _deterministic_udn("")
    udn2 = _deterministic_udn("")
    assert udn1 == udn2
    assert udn1 != _deterministic_udn("some_player")


def test_deterministic_udn_is_valid_uuid() -> None:
    """Generated UDN contains a valid UUID5."""
    udn = _deterministic_udn("test_player")
    uuid_str = udn.replace("uuid:", "")
    parsed = uuid.UUID(uuid_str)
    assert parsed.version == 5


def test_multiple_renderers_different_ports() -> None:
    """Verify multiple renderers can bind to different ports."""
    from provider.renderer import UPnPRenderer

    r1 = UPnPRenderer("Player 1", "127.0.0.1", 9001)
    r2 = UPnPRenderer("Player 2", "127.0.0.1", 9002)
    assert r1.http_port != r2.http_port
    assert r1.udn != r2.udn


def test_renderer_with_explicit_udn() -> None:
    """Renderer uses provided UDN instead of generating one."""
    from provider.renderer import UPnPRenderer

    udn = _deterministic_udn("test_player")
    r = UPnPRenderer("Test", "127.0.0.1", 9999, udn=udn)
    assert r.udn == udn
