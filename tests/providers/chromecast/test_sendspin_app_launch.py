"""
Tests for launching the Sendspin Cast App from the Chromecast Sendspin bridge.

Regression tests for https://github.com/music-assistant/support/issues/4653, where a
Raumfeld One M with old Cast firmware refused the launch with CAST_INIT_TIMEOUT and the
bridge carried on regardless: it retried sending its config to an app that was not
running, logged the app as launched, and left the play command waiting 30 seconds for a
ready report that could never come.

A refused launch must fail the waiting command straight away with the receiver's reason.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import PlayerCommandFailed
from pychromecast.error import UnsupportedNamespace

from music_assistant.providers.chromecast.constants import SENDSPIN_CAST_APP_ID
from music_assistant.providers.chromecast.sendspin_bridge import SendspinChromecastBridge


def _make_bridge(
    *, accept_launch: bool, refusal_reason: str | None = None
) -> tuple[SendspinChromecastBridge, list[dict[str, Any]]]:
    """
    Build a bridge for a Cast device whose receiver accepts or refuses the launch.

    :param accept_launch: Whether the receiver starts the Sendspin Cast App.
    :param refusal_reason: LAUNCH_ERROR reason reported by the receiver.
    :return: The bridge, and the list collecting the messages sent to the Cast app.
    """
    mass = MagicMock()
    # the launch runs in an executor and is acknowledged from that thread, so the real
    # running loop is needed to bridge back to the waiting coroutine
    mass.loop = asyncio.get_running_loop()
    mass.streams.publish_ip = "192.168.1.10"
    mass.config.get_raw_player_config_value = MagicMock(return_value=None)

    cast_player = MagicMock()
    cast_player.player_id = "cc_player"
    cast_player.display_name = "Esszimmer"
    cast_player.cc.app_id = None
    receiver = cast_player.cc.socket_client.receiver_controller
    receiver.launch_failure.reason = refusal_reason
    sent_messages: list[dict[str, Any]] = []

    def launch_app(app_id: str, *, force_launch: bool, callback_function: Any) -> None:  # noqa: ARG001
        if accept_launch:
            cast_player.cc.app_id = app_id
            callback_function(True, {"type": "RECEIVER_STATUS"})
        else:
            callback_function(False, {"type": "LAUNCH_ERROR", "reason": refusal_reason})

    def send_app_message(namespace: str, message: dict[str, Any]) -> None:
        if cast_player.cc.app_id != SENDSPIN_CAST_APP_ID:
            raise UnsupportedNamespace(f"Namespace {namespace} is not supported by current app")
        sent_messages.append(message)

    receiver.launch_app = launch_app
    cast_player.cc.socket_client.send_app_message = send_app_message

    provider = MagicMock()
    provider.mass = mass
    provider.logger = logging.getLogger("test.cast_bridge")
    bridge = SendspinChromecastBridge(provider, cast_player, MagicMock(), "spb_aabbccddeeff")
    return bridge, sent_messages


async def test_refused_launch_fails_the_waiting_command() -> None:
    """The receiver's refusal reaches the waiting command with its reason, without delay."""
    bridge, _ = _make_bridge(accept_launch=False, refusal_reason="CAST_INIT_TIMEOUT")
    ready = bridge.reset_cast_app_ready()

    await bridge._launch_sendspin_app()

    assert ready.done()
    with pytest.raises(PlayerCommandFailed, match="CAST_INIT_TIMEOUT"):
        ready.result()


async def test_refused_launch_is_not_treated_as_a_running_app() -> None:
    """No config is sent to an app that did not start, and it is not marked active."""
    bridge, sent_messages = _make_bridge(accept_launch=False, refusal_reason="CAST_INIT_TIMEOUT")
    bridge.reset_cast_app_ready()

    await bridge._launch_sendspin_app()

    assert sent_messages == []
    assert bridge._cast_app_was_active is False


async def test_refusal_without_a_reason_still_fails() -> None:
    """A receiver that gives no reason still fails the waiting command."""
    bridge, _ = _make_bridge(accept_launch=False)
    ready = bridge.reset_cast_app_ready()

    await bridge._launch_sendspin_app()

    with pytest.raises(PlayerCommandFailed, match="no reason given"):
        ready.result()


async def test_accepted_launch_sends_the_config() -> None:
    """A started app gets its config and is left to report ready on its own."""
    bridge, sent_messages = _make_bridge(accept_launch=True)
    ready = bridge.reset_cast_app_ready()

    await bridge._launch_sendspin_app()

    assert [message["type"] for message in sent_messages] == ["config"]
    assert bridge._cast_app_was_active is True
    # readiness comes from the app's own "connected" status, not from the launch
    assert not ready.done()
