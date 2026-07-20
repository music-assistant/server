"""Tests for the dashboard-casting helper in the Chromecast provider."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.chromecast.constants import DASHBOARD_NAMESPACE, MASS_APP_ID
from music_assistant.providers.chromecast.helpers import send_hide_dashboard, send_show_dashboard


def test_send_show_dashboard_happy_path() -> None:
    """Launches the receiver app and sends show_dashboard once the namespace is available."""
    chromecast = MagicMock()
    chromecast.name = "Living Room TV"
    chromecast.socket_client.app_namespaces = {DASHBOARD_NAMESPACE}

    def _launch_app(
        app_id: str, *, callback_function: Callable[[bool, Any], None], **_kwargs: Any
    ) -> None:
        assert app_id == MASS_APP_ID
        callback_function(True, None)

    chromecast.socket_client.receiver_controller.launch_app.side_effect = _launch_app

    send_show_dashboard(chromecast, "remote123", "code456", "/party", "2.17.150")

    chromecast.socket_client.send_app_message.assert_called_once_with(
        DASHBOARD_NAMESPACE,
        {
            "type": "show_dashboard",
            "remoteId": "remote123",
            "dashboardCode": "code456",
            "path": "/party",
            "serverVersion": "2.17.150",
        },
    )


def test_send_show_dashboard_launch_failure_raises() -> None:
    """A failed launch callback raises TimeoutError without sending a message."""
    chromecast = MagicMock()
    chromecast.name = "Living Room TV"

    def _launch_app(
        _app_id: str, *, callback_function: Callable[[bool, Any], None], **_kwargs: Any
    ) -> None:
        callback_function(False, None)

    chromecast.socket_client.receiver_controller.launch_app.side_effect = _launch_app

    with pytest.raises(TimeoutError):
        send_show_dashboard(chromecast, "remote123", "code456", "/party", "2.17.150")

    chromecast.socket_client.send_app_message.assert_not_called()


def test_send_show_dashboard_namespace_never_appears_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raises TimeoutError if the dashboard namespace never shows up in app_namespaces."""
    chromecast = MagicMock()
    chromecast.name = "Living Room TV"
    chromecast.socket_client.app_namespaces = set()

    def _launch_app(
        _app_id: str, *, callback_function: Callable[[bool, Any], None], **_kwargs: Any
    ) -> None:
        callback_function(True, None)

    chromecast.socket_client.receiver_controller.launch_app.side_effect = _launch_app

    # fake clock that advances on every call, so the poll loop's deadline check
    # trips deterministically without any real sleeping
    fake_clock = [0.0]

    def _fake_monotonic() -> float:
        fake_clock[0] += 1.0
        return fake_clock[0]

    monkeypatch.setattr(
        "music_assistant.providers.chromecast.helpers.time.monotonic", _fake_monotonic
    )
    monkeypatch.setattr("music_assistant.providers.chromecast.helpers.time.sleep", lambda _s: None)

    with pytest.raises(TimeoutError):
        send_show_dashboard(chromecast, "remote123", "code456", "/party", "2.17.150", timeout=5.0)

    chromecast.socket_client.send_app_message.assert_not_called()


def test_send_hide_dashboard_sends_message_when_app_and_namespace_match() -> None:
    """Sends the hide_dashboard message when our app is running with the dashboard namespace."""
    chromecast = MagicMock()
    chromecast.app_id = MASS_APP_ID
    chromecast.socket_client.app_namespaces = {DASHBOARD_NAMESPACE}

    result = send_hide_dashboard(chromecast)

    assert result is True
    chromecast.socket_client.send_app_message.assert_called_once_with(
        DASHBOARD_NAMESPACE, {"type": "hide_dashboard"}
    )
    chromecast.socket_client.receiver_controller.launch_app.assert_not_called()


def test_send_hide_dashboard_returns_false_when_app_differs() -> None:
    """Nothing is sent when the receiver is not running our app."""
    chromecast = MagicMock()
    chromecast.app_id = "some-other-app"
    chromecast.socket_client.app_namespaces = {DASHBOARD_NAMESPACE}

    result = send_hide_dashboard(chromecast)

    assert result is False
    chromecast.socket_client.send_app_message.assert_not_called()


def test_send_hide_dashboard_returns_false_when_namespace_missing() -> None:
    """Nothing is sent when our app is running but the dashboard namespace isn't up yet."""
    chromecast = MagicMock()
    chromecast.app_id = MASS_APP_ID
    chromecast.socket_client.app_namespaces = set()

    result = send_hide_dashboard(chromecast)

    assert result is False
    chromecast.socket_client.send_app_message.assert_not_called()
