"""Tests for the external Snapcast mass_bridge control script."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from music_assistant.providers.snapcast.snapserver import mass_bridge
from music_assistant.providers.snapcast.snapserver.mass_bridge import MusicAssistantControl


def _create_control() -> Any:
    """Create a lightweight controller instance without starting background threads."""
    ctrl: Any = MusicAssistantControl.__new__(MusicAssistantControl)
    ctrl.stream = "Kitchen Group"
    ctrl.snapcast_host = "localhost"
    ctrl.snapcast_port = 1780
    ctrl.ma_websocket_ip = "localhost"
    ctrl.ma_websocket_port = 8095
    ctrl.ma_access_token = "token"
    ctrl._metadata = {}
    ctrl._properties = ctrl._default_properties()
    ctrl._request_callbacks = {}
    ctrl._ws = None
    ctrl._authenticated = True
    ctrl._stopped = False
    ctrl._shutdown_event = threading.Event()
    ctrl._send_lock = threading.Lock()
    ctrl._callback_lock = threading.Lock()
    ctrl._resolve_retry_lock = threading.Lock()
    ctrl._current_queue_id = None
    ctrl._current_player_id = None
    ctrl._resolve_retry_timer = None
    ctrl._ws_thread = None
    return ctrl


def test_has_existing_inactive_snapcast_stream_matches_uri_name() -> None:
    """An idle Snapcast stream should be found by its configured URI name."""
    ctrl = _create_control()
    ctrl._fetch_snapcast_server_status = MagicMock(
        return_value={
            "streams": [
                {
                    "id": "snap-stream-123",
                    "status": "idle",
                    "uri": {
                        "raw": ("tcp://0.0.0.0:4953?name=Kitchen+Group&sampleformat=48000:16:2"),
                    },
                }
            ]
        }
    )

    assert ctrl._has_existing_inactive_snapcast_stream() is True


def test_resolve_runtime_params_uses_user_config_defaults_and_env_token() -> None:
    """User-config defaults should be honored, with env token fallback when left empty."""
    original_params = dict(mass_bridge.params)
    try:
        mass_bridge.params.update(
            {
                "progname": "mass_bridge.py",
                "snapcast-host": "192.168.10.3",
                "snapcast-port": 1780,
                "ma-websocket-ip": "192.168.10.4",
                "ma-websocket-port": 8095,
                "ma-access-token": None,
                "stream": "broadcast",
            }
        )

        runtime_params = mass_bridge._resolve_runtime_params(
            ["mass_bridge.py"], {"MASS_ACCESS_TOKEN": "env-token"}
        )

        assert runtime_params["snapcast-host"] == "192.168.10.3"
        assert runtime_params["snapcast-port"] == 1780
        assert runtime_params["ma-websocket-ip"] == "192.168.10.4"
        assert runtime_params["ma-websocket-port"] == 8095
        assert runtime_params["ma-access-token"] == "env-token"
        assert runtime_params["stream"] == "broadcast"
    finally:
        mass_bridge.params.clear()
        mass_bridge.params.update(original_params)


def test_resolve_runtime_params_cli_overrides_user_config() -> None:
    """CLI args should override the editable user-config block at the top of the script."""
    original_params = dict(mass_bridge.params)
    try:
        mass_bridge.params.update(
            {
                "progname": "mass_bridge.py",
                "snapcast-host": "192.168.10.3",
                "snapcast-port": 1780,
                "ma-websocket-ip": "192.168.10.4",
                "ma-websocket-port": 8095,
                "ma-access-token": "config-token",
                "stream": "broadcast",
            }
        )

        runtime_params = mass_bridge._resolve_runtime_params(
            [
                "mass_bridge.py",
                "--snapcast-host=127.0.0.1",
                "--ma-websocket-port=9000",
                "--stream=living_room",
            ],
            {},
        )

        assert runtime_params["snapcast-host"] == "127.0.0.1"
        assert runtime_params["ma-websocket-port"] == 9000
        assert runtime_params["ma-access-token"] == "config-token"
        assert runtime_params["stream"] == "living_room"
    finally:
        mass_bridge.params.clear()
        mass_bridge.params.update(original_params)


def test_resolve_runtime_params_reads_token_from_configured_file(tmp_path: Path) -> None:
    """A configured token file should be read when no direct token value is set."""
    token_file = tmp_path / "mass_token.txt"
    token_file.write_text("file-token\n", encoding="utf-8")
    original_params = dict(mass_bridge.params)
    try:
        mass_bridge.params.update(
            {
                "progname": "mass_bridge.py",
                "ma-access-token": "",
                "ma-access-token-file": str(token_file),
            }
        )

        runtime_params = mass_bridge._resolve_runtime_params(["mass_bridge.py"], {})

        assert runtime_params["ma-access-token-file"] == str(token_file)
        assert runtime_params["ma-access-token"] == "file-token"
    finally:
        mass_bridge.params.clear()
        mass_bridge.params.update(original_params)


def test_resolve_runtime_params_prefers_direct_token_over_token_file(tmp_path: Path) -> None:
    """A direct token value should win over a configured token file."""
    token_file = tmp_path / "mass_token.txt"
    token_file.write_text("file-token\n", encoding="utf-8")
    original_params = dict(mass_bridge.params)
    try:
        mass_bridge.params.update(
            {
                "progname": "mass_bridge.py",
                "ma-access-token": "direct-token",
                "ma-access-token-file": str(token_file),
            }
        )

        runtime_params = mass_bridge._resolve_runtime_params(["mass_bridge.py"], {})

        assert runtime_params["ma-access-token"] == "direct-token"
    finally:
        mass_bridge.params.clear()
        mass_bridge.params.update(original_params)


def test_resolve_stream_state_schedules_retry_for_inactive_snapcast_stream() -> None:
    """A reconnect should retry resolution when only a stale idle stream is still visible."""
    ctrl = _create_control()
    ctrl._has_existing_inactive_snapcast_stream = MagicMock(return_value=True)
    ctrl._schedule_stream_state_retry = MagicMock()
    ctrl._cancel_stream_state_retry = MagicMock()
    ctrl.send_snapcast_properties_notification = MagicMock()

    def fake_send_request(command: str, callback: Any = None, **args: Any) -> bool:
        assert command == "snapcast/resolve_control_stream"
        assert args["stream"] == "Kitchen Group"
        assert callback is not None
        callback(None)
        return True

    ctrl.send_request = fake_send_request

    ctrl._resolve_stream_state(notify=True)

    ctrl._schedule_stream_state_retry.assert_called_once_with()
    ctrl._cancel_stream_state_retry.assert_not_called()
    ctrl.send_snapcast_properties_notification.assert_called_once_with(ctrl._default_properties())


def test_resolve_stream_state_without_queue_stays_read_only() -> None:
    """A matched stream without queue data must remain unresolved and read-only."""
    ctrl = _create_control()
    ctrl._has_existing_inactive_snapcast_stream = MagicMock(return_value=False)
    ctrl._schedule_stream_state_retry = MagicMock()
    ctrl._cancel_stream_state_retry = MagicMock()
    ctrl.send_snapcast_properties_notification = MagicMock()

    def fake_send_request(command: str, callback: Any = None, **args: Any) -> bool:
        assert command == "snapcast/resolve_control_stream"
        assert args["stream"] == "Kitchen Group"
        assert callback is not None
        callback({"queue_id": "plugin-source", "player_id": "plugin-source"})
        return True

    ctrl.send_request = fake_send_request

    ctrl._resolve_stream_state(notify=True)

    assert ctrl._current_queue_id is None
    assert ctrl._current_player_id is None
    ctrl._schedule_stream_state_retry.assert_not_called()
    ctrl._cancel_stream_state_retry.assert_called_once_with()
    ctrl.send_snapcast_properties_notification.assert_called_once_with(ctrl._default_properties())


def test_queue_time_updated_refreshes_position() -> None:
    """Queue time updates should only refresh the published position."""
    ctrl = _create_control()
    ctrl._current_queue_id = "queue-1"
    ctrl._properties = {
        **ctrl._default_properties(),
        "canControl": True,
        "playbackStatus": "playing",
        "position": 10.0,
    }
    ctrl.send_snapcast_properties_notification = MagicMock()

    ctrl._handle_ws_message(
        json.dumps({"event": "queue_time_updated", "object_id": "queue-1", "data": 42.5})
    )

    ctrl.send_snapcast_properties_notification.assert_called_once_with(
        {
            **ctrl._properties,
            "position": 42.5,
        }
    )


def test_create_properties_keeps_can_seek_boolean_without_current_item() -> None:
    """Published Snapcast properties must keep canSeek as a strict boolean."""
    ctrl = _create_control()

    properties = ctrl._create_properties(
        {
            "state": "stopped",
            "current_item": None,
            "next_item": None,
            "current_index": 0,
            "elapsed_time": 0.0,
            "shuffle_enabled": False,
            "repeat_mode": "off",
        }
    )

    assert properties["canSeek"] is False


def test_unresolved_control_request_returns_error(monkeypatch: Any) -> None:
    """Transport commands must be rejected while no queue is resolved."""
    ctrl = _create_control()
    ctrl._resolve_stream_state = MagicMock()
    ctrl.send_request = MagicMock()
    send_error_mock = MagicMock()
    monkeypatch.setattr(mass_bridge, "send_error", send_error_mock)

    ctrl.handle_snapcast_request(
        {
            "id": "req-1",
            "jsonrpc": "2.0",
            "method": "Plugin.Stream.Player.Control",
            "params": {"command": "next", "params": {}},
        }
    )

    ctrl._resolve_stream_state.assert_called_once_with(notify=False)
    ctrl.send_request.assert_not_called()
    send_error_mock.assert_called_once_with(
        "req-1",
        -32000,
        "No active Music Assistant queue resolved for stream 'Kitchen Group'",
    )


def test_send_request_requires_auth_for_non_auth_commands() -> None:
    """Normal MA commands must wait until the websocket has authenticated."""
    ctrl = _create_control()
    ctrl._authenticated = False
    ctrl._ws = MagicMock()

    sent = ctrl.send_request("players/all")

    assert sent is False
    ctrl._ws.send.assert_not_called()


def test_handle_auth_result_marks_bridge_authenticated_and_resolves() -> None:
    """Successful auth should mark the bridge authenticated and resolve stream state."""
    ctrl = _create_control()
    ctrl._ws = MagicMock()
    ctrl._resolve_stream_state = MagicMock()

    ctrl._handle_auth_result({"authenticated": True})

    assert ctrl._authenticated is True
    ctrl._ws.settimeout.assert_called_once_with(None)
    ctrl._resolve_stream_state.assert_called_once_with(notify=True)


def test_handle_auth_result_failed_keeps_bridge_read_only() -> None:
    """Failed auth should keep the bridge unresolved and close the websocket."""
    ctrl = _create_control()
    ctrl._current_queue_id = "queue-1"
    ctrl._current_player_id = "queue-1"
    ctrl._properties = {**ctrl._default_properties(), "canControl": True}
    ctrl._ws = MagicMock()
    ctrl.send_snapcast_properties_notification = MagicMock()

    ctrl._handle_auth_result({"authenticated": False})

    assert ctrl._authenticated is False
    assert ctrl._current_queue_id is None
    assert ctrl._current_player_id is None
    ctrl.send_snapcast_properties_notification.assert_called_once_with(ctrl._default_properties())
    ctrl._ws.close.assert_called_once_with()
