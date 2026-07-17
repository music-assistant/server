"""Tests for the MA cast dashboard controller."""

from __future__ import annotations

from typing import Any

from music_assistant.providers.chromecast.constants import MASS_APP_ID
from music_assistant.providers.chromecast.dashboard_controller import MusicAssistantCastController


def test_show_dashboard_message_payload() -> None:
    """The dashboard message payload matches what the receiver expects."""
    controller = MusicAssistantCastController()
    sent: list[dict[str, Any]] = []
    controller.send_message = lambda data, **_kwargs: sent.append(data)  # type: ignore[method-assign]
    controller._send_show_dashboard("remote123", "code456", "/party", "2.17.150")
    assert sent == [
        {
            "type": "show_dashboard",
            "remoteId": "remote123",
            "castCode": "code456",
            "path": "/party",
            "serverVersion": "2.17.150",
        }
    ]


def test_supporting_app_id() -> None:
    """The controller launches the given app_id, defaulting to the MA cast app."""
    assert MusicAssistantCastController("920426A9").supporting_app_id == "920426A9"
    assert MusicAssistantCastController().supporting_app_id == MASS_APP_ID
