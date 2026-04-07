"""Tests for the GENA eventing module."""

from __future__ import annotations

import pytest

from provider.eventing import EventingManager


@pytest.fixture
def manager() -> EventingManager:
    """Create a fresh eventing manager."""
    return EventingManager()


def test_subscribe_returns_sid_and_timeout(manager: EventingManager) -> None:
    sid, timeout = manager.subscribe("<http://192.168.1.5:8080/callback>")
    assert sid.startswith("uuid:")
    assert timeout == 1800


def test_subscribe_custom_timeout(manager: EventingManager) -> None:
    sid, timeout = manager.subscribe(
        "<http://192.168.1.5:8080/callback>",
        "Second-300",
    )
    assert timeout == 300


def test_subscribe_multiple_callbacks(manager: EventingManager) -> None:
    sid, _ = manager.subscribe(
        "<http://host1:8080/cb><http://host2:8080/cb>",
    )
    sub = manager._subscriptions[sid]
    assert len(sub.callback_urls) == 2


def test_subscribe_no_callback_raises(manager: EventingManager) -> None:
    with pytest.raises(ValueError):
        manager.subscribe("")


def test_unsubscribe(manager: EventingManager) -> None:
    sid, _ = manager.subscribe("<http://host:8080/cb>")
    assert sid in manager._subscriptions
    manager.unsubscribe(sid)
    assert sid not in manager._subscriptions


def test_unsubscribe_unknown_is_noop(manager: EventingManager) -> None:
    manager.unsubscribe("uuid:nonexistent")  # should not raise


def test_renew(manager: EventingManager) -> None:
    sid, _ = manager.subscribe("<http://host:8080/cb>", "Second-100")
    new_timeout = manager.renew(sid, "Second-600")
    assert new_timeout == 600


def test_renew_unknown_raises(manager: EventingManager) -> None:
    with pytest.raises(KeyError):
        manager.renew("uuid:nonexistent")


def test_parse_callback_header() -> None:
    urls = EventingManager._parse_callback_header(
        "<http://192.168.1.5:8080/event><http://10.0.0.1:9000/ev>",
    )
    assert urls == ["http://192.168.1.5:8080/event", "http://10.0.0.1:9000/ev"]


def test_parse_callback_header_single() -> None:
    urls = EventingManager._parse_callback_header("<http://host:1234/cb>")
    assert urls == ["http://host:1234/cb"]


def test_parse_timeout_default() -> None:
    assert EventingManager._parse_timeout(None) == 1800
    assert EventingManager._parse_timeout("") == 1800


def test_parse_timeout_infinite() -> None:
    assert EventingManager._parse_timeout("infinite") == 1800


def test_parse_timeout_seconds() -> None:
    assert EventingManager._parse_timeout("Second-300") == 300
    assert EventingManager._parse_timeout("Second-7200") == 7200


def test_build_propertyset() -> None:
    xml = EventingManager._build_propertyset({"Volume": "75", "Mute": "0"})
    assert "e:propertyset" in xml
    assert "<Volume>75</Volume>" in xml
    assert "<Mute>0</Mute>" in xml


def test_build_propertyset_escapes_values() -> None:
    xml = EventingManager._build_propertyset({"Title": "Tom & Jerry"})
    assert "Tom &amp; Jerry" in xml


async def test_notify_no_subscribers(manager: EventingManager) -> None:
    """Notify with no subscribers should be a no-op."""
    await manager.notify({"TransportState": "PLAYING"})
