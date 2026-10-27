# mypy: disable-error-code="attr-defined,unreachable"
"""Tests for the Ynison WebSocket client."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.enums import RepeatMode
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import SecretStr

from music_assistant.providers.yandex_ynison.constants import (
    DEFAULT_APP_NAME,
    DEVICE_TYPE_WEB,
    YNISON_ORIGIN,
)
from music_assistant.providers.yandex_ynison.ynison_client import (
    YnisonClient,
    YnisonDeviceInfo,
    YnisonSendError,
    YnisonState,
    generate_device_id,
    make_version_block,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ynison"


def _fixture(name: str) -> dict[str, Any]:
    """Load a sanitized Ynison protocol fixture."""
    return cast("dict[str, Any]", json.loads((FIXTURES / name).read_text()))


@pytest.fixture(autouse=True)
def _short_strict_send_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep strict sends on a dead channel from waiting the production 5 s."""
    monkeypatch.setattr(
        "music_assistant.providers.yandex_ynison.ynison_client.STRICT_SEND_WAIT", 0.2
    )


@pytest.fixture
def device_info() -> YnisonDeviceInfo:
    """Create test device info."""
    return YnisonDeviceInfo(
        device_id="test-device-id",
        title="Test Device",
    )


@pytest.fixture
def mock_state_callback() -> AsyncMock:
    """Create a mock callback for state updates."""
    return AsyncMock()


@pytest.fixture
def client(
    device_info: YnisonDeviceInfo,
    mock_state_callback: AsyncMock,
) -> YnisonClient:
    """Create a YnisonClient instance for testing."""
    return YnisonClient(
        token=SecretStr("test-token"),
        device_info=device_info,
        on_state_update=mock_state_callback,
        logger=MagicMock(),
    )


# ------------------------------------------------------------------
# YnisonDeviceInfo
# ------------------------------------------------------------------


class TestYnisonDeviceInfo:
    """Tests for YnisonDeviceInfo dataclass."""

    def test_defaults(self) -> None:
        """Default type is WEB and app_name is set."""
        info = YnisonDeviceInfo(device_id="abc", title="My Speaker")
        assert info.type == DEVICE_TYPE_WEB
        assert info.app_name == DEFAULT_APP_NAME

    def test_custom_values(self) -> None:
        """Custom values override defaults."""
        info = YnisonDeviceInfo(
            device_id="xyz",
            title="Custom",
            type="TV",
            app_name="CustomApp",
            app_version="2.0",
        )
        assert info.type == "TV"
        assert info.app_version == "2.0"


# ------------------------------------------------------------------
# YnisonState
# ------------------------------------------------------------------


class TestYnisonState:
    """Tests for YnisonState dataclass."""

    def test_empty_state(self) -> None:
        """Empty state returns safe defaults."""
        state = YnisonState()
        assert state.current_track_id is None
        assert state.is_paused is True
        assert state.progress_ms == 0
        assert state.duration_ms == 0

    def test_current_track_id(self) -> None:
        """Extracts track ID from playable list by index."""
        state = YnisonState(
            player_state={
                "player_queue": {
                    "current_playable_index": 1,
                    "playable_list": [
                        {"playable_id": "track1"},
                        {"playable_id": "track2"},
                        {"playable_id": "track3"},
                    ],
                }
            }
        )
        assert state.current_track_id == "track2"

    def test_current_track_id_out_of_bounds(self) -> None:
        """Returns None when index exceeds playable list."""
        state = YnisonState(
            player_state={
                "player_queue": {
                    "current_playable_index": 10,
                    "playable_list": [{"playable_id": "track1"}],
                }
            }
        )
        assert state.current_track_id is None

    def test_is_paused(self) -> None:
        """Reads paused status from player state."""
        state = YnisonState(player_state={"status": {"paused": False}})
        assert state.is_paused is False

    def test_progress_and_duration(self) -> None:
        """Reads progress and duration from player state."""
        state = YnisonState(
            player_state={
                "status": {
                    "progress_ms": 30000,
                    "duration_ms": 180000,
                }
            }
        )
        assert state.progress_ms == 30000
        assert state.duration_ms == 180000


# ------------------------------------------------------------------
# YnisonClient internals
# ------------------------------------------------------------------


class TestYnisonClientBuildMethods:
    """Tests for YnisonClient helper/build methods."""

    def test_build_headers(self, client: YnisonClient) -> None:
        """Headers include auth, origin, and protocol."""
        headers = client._build_headers()
        assert headers["Authorization"] == "OAuth test-token"
        assert headers["Origin"] == YNISON_ORIGIN
        assert "Sec-WebSocket-Protocol" in headers

    def test_build_headers_with_ticket(self, client: YnisonClient) -> None:
        """Headers include redirect ticket and session ID when provided."""
        headers = client._build_headers(redirect_ticket="ticket123", session_id=42)
        proto = headers["Sec-WebSocket-Protocol"]
        assert "Ynison-Redirect-Ticket" in proto
        assert "ticket123" in proto
        assert "42" in proto

    def test_build_ws_protocol_header(self, client: YnisonClient) -> None:
        """Protocol header contains device ID and info."""
        proto = client._build_ws_protocol_header()
        assert proto.startswith("Bearer, v2, ")
        data = json.loads(proto[len("Bearer, v2, ") :])
        assert data["Ynison-Device-Id"] == "test-device-id"
        device_info = json.loads(data["Ynison-Device-Info"])
        assert device_info["app_name"] == DEFAULT_APP_NAME

    def test_build_device_dict(self, client: YnisonClient) -> None:
        """Device dict includes capabilities and info."""
        device = client._build_device_dict()
        assert device["info"]["device_id"] == "test-device-id"
        assert device["capabilities"]["can_be_player"] is True
        assert device["capabilities"]["can_be_remote_controller"] is False

    def test_device_header_carries_app_version(self, client: YnisonClient) -> None:
        """The device-info header names the app version, as the official header proto does."""
        data = json.loads(client._build_ws_protocol_header()[len("Bearer, v2, ") :])

        assert json.loads(data["Ynison-Device-Info"])["app_version"] == "1.0.0"

    def test_device_registration_has_only_update_device_fields(self, client: YnisonClient) -> None:
        """Registration sends only fields the UpdateDevice message defines."""
        assert set(client._build_device_dict()) == {"info", "capabilities"}

    async def test_losing_activity_mutes_passive_events_again(self, client: YnisonClient) -> None:
        """The server clears mute_events_if_passive on activation, so it is re-sent on loss."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        client._ws = mock_ws
        client._connected = True
        frames = [
            {"active_device_id_optional": "test-device-id"},
            {"active_device_id_optional": "phone"},
        ]

        async def _aiter(_self: Any) -> Any:
            for frame in frames:
                yield _make_ws_msg(aiohttp.WSMsgType.TEXT, json.dumps(frame))

        mock_ws.__aiter__ = _aiter
        with patch.object(client, "_reconnect", new_callable=AsyncMock):
            await client._message_loop()

        sent = [json.loads(call.args[0]) for call in mock_ws.send_str.call_args_list]
        assert [msg["update_session_params"] for msg in sent if "update_session_params" in msg] == [
            {"mute_events_if_passive": True}
        ]

    def test_build_initial_state(self, client: YnisonClient) -> None:
        """Initial state is paused with empty queue."""
        state = client._build_initial_state()
        assert state["status"]["paused"] is True
        assert state["player_queue"]["playable_list"] == []

    def test_build_initial_state_string_fields(self, client: YnisonClient) -> None:
        """Ynison rejects integer timestamps; all time/version fields must be str."""
        state = client._build_initial_state()
        for block in (state["status"], state["player_queue"]):
            version = block["version"]
            assert version["device_id"] == "test-device-id"
            assert isinstance(version["version"], str)
            assert version["version"].isdigit()
            assert version["timestamp_ms"] == "0"
        assert state["status"]["progress_ms"] == "0"
        assert state["status"]["duration_ms"] == "0"

    def test_device_id_property(self, client: YnisonClient) -> None:
        """device_id property exposes the registered device id."""
        assert client.device_id == "test-device-id"


class TestMakeVersionBlock:
    """Tests for the module-level make_version_block helper."""

    def test_fields_are_strings(self) -> None:
        """Version and timestamp_ms must be strings (Ynison 500s on ints)."""
        block = make_version_block("dev-42")
        assert block["device_id"] == "dev-42"
        assert isinstance(block["version"], str)
        assert block["version"].isdigit()
        assert block["timestamp_ms"] == "0"


# ------------------------------------------------------------------
# YnisonClient parse state
# ------------------------------------------------------------------


class TestYnisonClientParseState:
    """Tests for state parsing."""

    def test_parse_state(self, client: YnisonClient) -> None:
        """Parses full state response into YnisonState."""
        data: dict[str, Any] = {
            "player_state": {
                "status": {"paused": False, "progress_ms": 5000, "duration_ms": 200000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "track42"}],
                },
            },
            "active_device_id_optional": "test-device-id",
            "devices": [{"info": {"device_id": "test-device-id"}}],
        }
        client._parse_state(data)
        assert client.state.current_track_id == "track42"
        assert client.state.active_device_id == "test-device-id"
        assert client.state.is_paused is False

    def test_parse_state_partial(self, client: YnisonClient) -> None:
        """Partial updates should preserve existing state."""
        client.state.active_device_id = "old-device"
        client._parse_state({"player_state": {"status": {"paused": True}}})
        assert client.state.active_device_id == "old-device"

    def test_disconnect_sentinel_clears_implicit_active_device(self, client: YnisonClient) -> None:
        """A disconnect sentinel without an explicit owner clears stale ownership."""
        client.state.active_device_id = "test-device-id"

        client._parse_state(_fixture("disconnect_sentinel.json"))

        assert client.state.active_device_id is None

    async def test_recent_empty_version_status_is_heartbeat_echo(
        self, client: YnisonClient
    ) -> None:
        """A matching server-normalized status is recognized as our heartbeat."""
        client.state.player_state = {
            "player_queue": {
                "current_playable_index": 0,
                "playable_list": [{"playable_id": "TRACK_A"}],
            }
        }
        ws = MagicMock(closed=False)
        ws.send_str = AsyncMock()
        client._ws = ws

        await client.update_playing_status(120000, 240000, paused=False)
        client._parse_state(_fixture("empty_version_heartbeat.json"))

        assert client.state.last_update_is_echo is True

    async def test_foreign_status_wins_over_matching_heartbeat(self, client: YnisonClient) -> None:
        """A peer-authored status is authoritative even when its values match."""
        client.state.player_state = {
            "player_queue": {
                "current_playable_index": 0,
                "playable_list": [{"playable_id": "TRACK_A"}],
            }
        }
        ws = MagicMock(closed=False)
        ws.send_str = AsyncMock()
        client._ws = ws
        await client.update_playing_status(180000, 240000, paused=False)

        client._parse_state(_fixture("phone_seek.json"))

        assert client.state.last_update_is_echo is False

    async def test_delayed_heartbeat_does_not_overwrite_newer_phone_seek(
        self, client: YnisonClient
    ) -> None:
        """A pre-seek heartbeat arriving late cannot roll state back."""
        client.state.player_state = {
            "player_queue": {
                "current_playable_index": 0,
                "playable_list": [{"playable_id": "TRACK_A"}],
            },
            "status": {"paused": False, "progress_ms": "120000", "duration_ms": "240000"},
        }
        ws = MagicMock(closed=False)
        ws.send_str = AsyncMock()
        client._ws = ws
        await client.update_playing_status(120000, 240000, paused=False)
        client._parse_state(_fixture("phone_seek.json"))

        client._parse_state(_fixture("empty_version_heartbeat.json"))

        assert client.state.progress_ms == 180000
        assert client.state.last_update_is_echo is True

        client._parse_state(_fixture("empty_version_heartbeat.json"))

        assert client.state.progress_ms == 180000
        assert client.state.last_update_is_echo is True

    async def test_queue_update_is_not_hidden_by_matching_status(
        self, client: YnisonClient
    ) -> None:
        """An incoming queue block remains observable beside a matching heartbeat."""
        client.state.player_state = {
            "player_queue": {
                "current_playable_index": 0,
                "playable_list": [{"playable_id": "TRACK_A"}],
            }
        }
        ws = MagicMock(closed=False)
        ws.send_str = AsyncMock()
        client._ws = ws
        await client.update_playing_status(120000, 240000, paused=False)
        update = _fixture("empty_version_heartbeat.json")
        update["player_state"]["player_queue"] = {
            "current_playable_index": 1,
            "playable_list": [
                {"playable_id": "TRACK_A"},
                {"playable_id": "TRACK_B"},
            ],
        }

        client._parse_state(update)

        assert client.state.last_update_is_echo is False

    async def test_matching_heartbeat_with_unchanged_queue_is_echo(
        self, client: YnisonClient
    ) -> None:
        """Ynison may attach the complete unchanged queue to a status echo."""
        queue = {
            "current_playable_index": 0,
            "playable_list": [{"playable_id": "TRACK_A"}],
            "version": {"device_id": "peer", "version": "7", "timestamp_ms": "0"},
        }
        client.state.player_state = {"player_queue": queue}
        ws = MagicMock(closed=False)
        ws.send_str = AsyncMock()
        client._ws = ws
        await client.update_playing_status(120000, 240000, paused=False)
        update = _fixture("empty_version_heartbeat.json")
        update["player_state"]["player_queue"] = deepcopy(queue)

        client._parse_state(update)

        assert client.state.last_update_is_echo is True

    def test_fully_authored_queue_advance_applies_reset_status(self, client: YnisonClient) -> None:
        """A fully authored echo advances canonical queue and resets its status."""
        client.state.player_state = {
            "status": {"paused": False, "progress_ms": "90000", "duration_ms": "200000"},
            "player_queue": {
                "current_playable_index": 0,
                "playable_list": [{"playable_id": "A"}, {"playable_id": "B"}],
            },
        }
        authored_version = {
            "device_id": "test-device-id",
            "version": "10",
            "timestamp_ms": "0",
        }

        client._parse_state(
            {
                "player_state": {
                    "status": {
                        "paused": False,
                        "progress_ms": "0",
                        "duration_ms": "0",
                        "version": authored_version,
                    },
                    "player_queue": {
                        "current_playable_index": 1,
                        "playable_list": [{"playable_id": "A"}, {"playable_id": "B"}],
                        "version": authored_version,
                    },
                }
            }
        )

        assert client.state.last_update_is_echo is True
        assert client.state.current_track_id == "B"
        assert client.state.progress_ms == 0
        assert client.state.duration_ms == 0

    async def test_attached_queue_heartbeat_does_not_overwrite_newer_seek(
        self, client: YnisonClient
    ) -> None:
        """An unchanged attached queue does not make a delayed status authoritative."""
        queue = {
            "current_playable_index": 0,
            "playable_list": [{"playable_id": "TRACK_A"}],
            "version": {"device_id": "peer", "version": "7", "timestamp_ms": "0"},
        }
        client.state.player_state = {"player_queue": queue}
        ws = MagicMock(closed=False)
        ws.send_str = AsyncMock()
        client._ws = ws
        await client.update_playing_status(120000, 240000, paused=False)
        client._parse_state(_fixture("phone_seek.json"))
        delayed = _fixture("empty_version_heartbeat.json")
        delayed["player_state"]["player_queue"] = deepcopy(queue)

        client._parse_state(delayed)

        assert client.state.progress_ms == 180000
        assert client.state.last_update_is_echo is True

    def test_echo_flag_true_only_when_both_authors_ours(self, client: YnisonClient) -> None:
        """AND-logic (1.9.1): both queue.version AND status.version must be ours."""
        client._parse_state(
            {
                "player_state": {
                    "player_queue": {
                        "playable_list": [{"playable_id": "t1"}],
                        "current_playable_index": 0,
                        "version": {
                            "device_id": "test-device-id",
                            "version": "42",
                            "timestamp_ms": "0",
                        },
                    },
                    "status": {
                        "paused": False,
                        "progress_ms": "1000",
                        "duration_ms": "5000",
                        "version": {
                            "device_id": "test-device-id",
                            "version": "43",
                            "timestamp_ms": "0",
                        },
                    },
                },
            }
        )
        assert client.state.last_update_is_echo is True

    def test_echo_flag_false_when_only_queue_is_ours(self, client: YnisonClient) -> None:
        """
        Status authored by peer → NOT echo, even if queue.version is ours.

        Regression for the OR-logic bug: a peer toggling pause produced
        status.version=peer + our stale queue.version=ours, which the old
        OR-rule wrongly classified as echo and silenced the user action.
        """
        client._parse_state(
            {
                "player_state": {
                    "player_queue": {
                        "playable_list": [{"playable_id": "t1"}],
                        "current_playable_index": 0,
                        "version": {
                            "device_id": "test-device-id",
                            "version": "42",
                            "timestamp_ms": "0",
                        },
                    },
                    "status": {
                        "paused": True,
                        "progress_ms": "1000",
                        "duration_ms": "5000",
                        "version": {
                            "device_id": "peer-device",
                            "version": "44",
                            "timestamp_ms": "0",
                        },
                    },
                },
            }
        )
        assert client.state.last_update_is_echo is False

    def test_echo_flag_false_when_only_status_is_ours(self, client: YnisonClient) -> None:
        """
        Queue authored by peer → NOT echo, even if status.version is ours.

        The mirror case: our heartbeat just stamped status.version=ours, but
        the peer changed the queue. Under AND-logic the peer change is not
        silenced.
        """
        client._parse_state(
            {
                "player_state": {
                    "player_queue": {
                        "playable_list": [{"playable_id": "new-track"}],
                        "current_playable_index": 0,
                        "version": {
                            "device_id": "peer-device",
                            "version": "100",
                            "timestamp_ms": "0",
                        },
                    },
                    "status": {
                        "paused": False,
                        "progress_ms": "0",
                        "duration_ms": "5000",
                        "version": {
                            "device_id": "test-device-id",
                            "version": "99",
                            "timestamp_ms": "0",
                        },
                    },
                },
            }
        )
        assert client.state.last_update_is_echo is False

    def test_echo_flag_false_on_foreign_author(self, client: YnisonClient) -> None:
        """Both authors are peer → not an echo."""
        client._parse_state(
            {
                "player_state": {
                    "player_queue": {
                        "playable_list": [{"playable_id": "t1"}],
                        "current_playable_index": 0,
                        "version": {
                            "device_id": "some-other-device",
                            "version": "42",
                            "timestamp_ms": "0",
                        },
                    },
                },
            }
        )
        assert client.state.last_update_is_echo is False

    def test_echo_flag_false_when_version_missing(self, client: YnisonClient) -> None:
        """
        No version block at all → not an echo (safe default).

        AND-logic treats missing version-block as "not ours" — matches the
        previous safe default. Without a version-block we can't claim
        ownership, so we let the update reach handlers.
        """
        client._parse_state(
            {
                "player_state": {
                    "player_queue": {"playable_list": [], "current_playable_index": -1},
                },
            }
        )
        assert client.state.last_update_is_echo is False

    def test_echo_flag_false_when_player_state_missing(self, client: YnisonClient) -> None:
        """status-only or non-player_state updates cannot be echoes."""
        client.state.last_update_is_echo = True  # sticky from a prior update
        client._parse_state({"active_device_id_optional": "some-device"})
        assert client.state.last_update_is_echo is False

    def test_parse_state_coerces_int_timestamps_to_strings(self, client: YnisonClient) -> None:
        """
        Inbound int timestamps are stringified so outbound echoes stay safe.

        Guards the reconnect path (send_full_state echoes self.state.player_state)
        and queue-mutating update_player_state calls that shallow-copy status.
        """
        client._parse_state(
            {
                "player_state": {
                    "status": {
                        "paused": False,
                        "progress_ms": 1234,
                        "duration_ms": 56789,
                        "player_action_timestamp_ms": 111,
                        "version": {
                            "device_id": "peer",
                            "version": 42,
                            "timestamp_ms": 0,
                        },
                    },
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [],
                        "version": {
                            "device_id": "peer",
                            "version": 99,
                            "timestamp_ms": 0,
                        },
                    },
                }
            }
        )
        status = client.state.player_state["status"]
        assert status["progress_ms"] == "1234"
        assert status["duration_ms"] == "56789"
        assert status["player_action_timestamp_ms"] == "111"
        assert status["version"]["version"] == "42"
        assert status["version"]["timestamp_ms"] == "0"
        queue_version = client.state.player_state["player_queue"]["version"]
        assert queue_version["version"] == "99"
        assert queue_version["timestamp_ms"] == "0"


# ------------------------------------------------------------------
# YnisonClient send methods
# ------------------------------------------------------------------


class TestYnisonClientSend:
    """Tests for send methods."""

    @pytest.fixture(autouse=True)
    def _setup_ws(self, client: YnisonClient) -> None:
        """Set up a mock WebSocket."""
        self.mock_ws = AsyncMock()
        self.mock_ws.closed = False
        client._ws = self.mock_ws
        client._connected = True

    async def test_update_playing_status(self, client: YnisonClient) -> None:
        """Sends correct playing status message with string-typed timestamps."""
        await client.update_playing_status(1000, 5000, paused=False)
        call_args = self.mock_ws.send_str.call_args[0][0]
        msg = json.loads(call_args)
        status = msg["update_playing_status"]["playing_status"]
        # Ynison expects strings for timestamp fields (integers trigger 500)
        assert status["progress_ms"] == "1000"
        assert status["duration_ms"] == "5000"
        assert status["paused"] is False

    async def test_update_active_device(self, client: YnisonClient) -> None:
        """Sends active device update message."""
        await client.update_active_device("device-123")
        msg = json.loads(self.mock_ws.send_str.call_args[0][0])
        assert msg["update_active_device"]["device_id_optional"] == "device-123"

    async def test_send_not_connected(self, client: YnisonClient) -> None:
        """Should silently skip when not connected."""
        client._ws = None
        await client.update_active_device("test")
        # No exception raised

    @pytest.mark.parametrize(
        "send",
        [
            lambda c: c.update_playing_status(1000, 5000, paused=True),
            lambda c: c.update_active_device("device-123"),
            lambda c: c.update_session_params(mute_events_if_passive=True),
        ],
    )
    async def test_every_message_carries_request_envelope(
        self, client: YnisonClient, send: Callable[[YnisonClient], Awaitable[Any]]
    ) -> None:
        """Status, active-device and session-params messages carry the request envelope."""
        await send(client)
        msg = json.loads(self.mock_ws.send_str.call_args[0][0])

        assert msg["rid"]
        assert msg["player_action_timestamp_ms"].isdigit()
        assert msg["activity_interception_type"] == "DO_NOT_INTERCEPT_BY_DEFAULT"

    async def test_own_unpaused_status_intercepts_if_no_one_active(
        self, client: YnisonClient
    ) -> None:
        """An unpaused status claims activity only when no device is active."""
        await client.update_playing_status(1000, 5000, paused=False)
        msg = json.loads(self.mock_ws.send_str.call_args[0][0])

        assert msg["activity_interception_type"] == "INTERCEPT_IF_NO_ONE_ACTIVE"


class TestReportStatus:
    """Tests for the status reporter policy in front of update_playing_status."""

    @pytest.fixture(autouse=True)
    def _setup(self, client: YnisonClient) -> None:
        """Connect a mock socket and make this device active on a known track."""
        self.mock_ws = AsyncMock()
        self.mock_ws.closed = False
        client._ws = self.mock_ws
        client._connected = True
        client._parse_state(
            {
                "active_device_id_optional": "test-device-id",
                "player_state": {
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [{"playable_id": "t1"}],
                    },
                    "status": {
                        "progress_ms": "10000",
                        "duration_ms": "200000",
                        "paused": False,
                        "version": {"device_id": "peer", "version": "1", "timestamp_ms": "0"},
                    },
                },
            }
        )

    def _sent(self) -> list[dict[str, Any]]:
        """Return the playing-status payloads sent so far."""
        return [
            json.loads(call.args[0])["update_playing_status"]["playing_status"]
            for call in self.mock_ws.send_str.call_args_list
        ]

    async def test_clamps_progress_to_duration(self, client: YnisonClient) -> None:
        """Progress never exceeds duration on the wire."""
        assert await client.report_status(250000, 200000, paused=False, strict=True)

        assert self._sent()[-1]["progress_ms"] == "200000"

    async def test_falls_back_to_server_duration(self, client: YnisonClient) -> None:
        """An unknown local duration uses the server's duration."""
        assert await client.report_status(50000, 0, paused=True, strict=True)

        assert self._sent()[-1]["duration_ms"] == "200000"

    async def test_unknown_duration_strict_raises(self, client: YnisonClient) -> None:
        """A strict report without any known duration fails instead of succeeding silently."""
        client.state.player_state["status"]["duration_ms"] = "0"

        with pytest.raises(YnisonSendError):
            await client.report_status(1000, 0, paused=True, strict=True)
        assert self._sent() == []

    async def test_unknown_duration_best_effort_skips(self, client: YnisonClient) -> None:
        """A best-effort report without a duration is skipped."""
        client.state.player_state["status"]["duration_ms"] = "0"

        assert await client.report_status(1000, 0, paused=False) is False
        assert self._sent() == []

    async def test_best_effort_matching_server_status_is_skipped(
        self, client: YnisonClient
    ) -> None:
        """A heartbeat within 2 s of the extrapolated server status is not sent."""
        with patch(
            "music_assistant.providers.yandex_ynison.ynison_client.time.monotonic",
            return_value=time.monotonic() + 5,
        ):
            sent = await client.report_status(15500, 200000, paused=False)

        assert sent is False
        assert self._sent() == []

    @pytest.mark.parametrize(
        ("progress", "duration", "paused"),
        [(40000, 200000, False), (15000, 200000, True), (15000, 180000, False)],
    )
    async def test_best_effort_change_is_sent(
        self, client: YnisonClient, progress: int, duration: int, paused: bool
    ) -> None:
        """Drift above 2 s, a pause change, or a duration change is published."""
        with patch(
            "music_assistant.providers.yandex_ynison.ynison_client.time.monotonic",
            return_value=time.monotonic() + 5,
        ):
            assert await client.report_status(progress, duration, paused=paused)

        assert self._sent()[-1]["progress_ms"] == str(progress)

    async def test_own_send_becomes_dedup_baseline(self, client: YnisonClient) -> None:
        """After publishing a seek, a matching heartbeat is not sent again."""
        assert await client.report_status(90000, 200000, paused=False, strict=True)
        assert await client.report_status(90500, 200000, paused=False) is False

        assert len(self._sent()) == 1

    def test_relayed_known_status_is_marked(self, client: YnisonClient) -> None:
        """A frame repeating the newest known status is flagged as a relay."""
        client._parse_state(
            {
                "player_state": {
                    "status": {"progress_ms": "10000", "duration_ms": "200000", "paused": False}
                }
            }
        )
        assert client.state.status_is_relay is True

        client._parse_state(
            {
                "player_state": {
                    "status": {"progress_ms": "90000", "duration_ms": "200000", "paused": False}
                }
            }
        )
        assert client.state.status_is_relay is False

    async def test_relay_of_own_sent_status_is_marked(self, client: YnisonClient) -> None:
        """After our seek, the server relaying that position is not a new status."""
        assert await client.report_status(90000, 200000, paused=False, strict=True)

        client._parse_state(
            {
                "player_state": {
                    "status": {"progress_ms": "90000", "duration_ms": "200000", "paused": False}
                }
            }
        )
        assert client.state.status_is_relay is True

    def test_peer_seek_to_known_position_is_not_a_relay(self, client: YnisonClient) -> None:
        """A newly versioned peer status is fresh even at the last known position."""
        client._parse_state(
            {
                "player_state": {
                    "status": {
                        "progress_ms": "10000",
                        "duration_ms": "200000",
                        "paused": False,
                        "version": {"device_id": "peer", "version": "2", "timestamp_ms": "0"},
                    }
                }
            }
        )
        assert client.state.status_is_relay is False

    def test_relay_keeps_the_known_version(self, client: YnisonClient) -> None:
        """A status repeated with its original version is a relay."""
        client._parse_state(
            {
                "player_state": {
                    "status": {
                        "progress_ms": "10000",
                        "duration_ms": "200000",
                        "paused": False,
                        "version": {"device_id": "peer", "version": "1", "timestamp_ms": "0"},
                    }
                }
            }
        )
        assert client.state.status_is_relay is True

    async def test_peer_seek_to_own_sent_position_is_not_a_relay(
        self, client: YnisonClient
    ) -> None:
        """A peer command at the position we published last is not our relay."""
        assert await client.report_status(0, 200000, paused=False, strict=True)

        client._parse_state(
            {
                "player_state": {
                    "status": {
                        "progress_ms": "0",
                        "duration_ms": "200000",
                        "paused": False,
                        "version": {"device_id": "peer", "version": "3", "timestamp_ms": "0"},
                    }
                }
            }
        )
        assert client.state.status_is_relay is False

    def test_frame_without_status_keeps_relay_marker(self, client: YnisonClient) -> None:
        """A frame that does not carry a status leaves the retained status stale."""
        client._parse_state({"devices": [{"info": {"device_id": "peer"}}]})
        assert client.state.status_is_relay is True

        client._parse_state(
            {
                "player_state": {
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [{"playable_id": "t1"}, {"playable_id": "t2"}],
                    }
                }
            }
        )
        assert client.state.status_is_relay is True

    async def test_best_effort_skipped_while_other_device_active(
        self, client: YnisonClient
    ) -> None:
        """Best-effort status is not published for a session another device owns."""
        client.state.active_device_id = "phone"

        assert await client.report_status(90000, 200000, paused=False) is False
        assert self._sent() == []

    async def test_strict_report_is_not_gated(self, client: YnisonClient) -> None:
        """User commands are delivered even while another device is active."""
        client.state.active_device_id = "phone"

        assert await client.report_status(90000, 200000, paused=False, strict=True)

    async def test_strict_waits_for_reconnect(self, client: YnisonClient) -> None:
        """A strict report waits for the channel to come back."""
        client._connected = False
        client._ws = None

        async def reconnect() -> None:
            await asyncio.sleep(0.05)
            client._ws = self.mock_ws
            client._connected = True

        task = asyncio.create_task(reconnect())
        assert await client.report_status(90000, 200000, paused=True, strict=True)
        await task

    async def test_strict_raises_after_wait(self, client: YnisonClient) -> None:
        """A strict report raises when the channel does not return in time."""
        client._connected = False
        client._ws = None

        with (
            patch("music_assistant.providers.yandex_ynison.ynison_client.STRICT_SEND_WAIT", 0.05),
            pytest.raises(YnisonSendError),
        ):
            await client.report_status(90000, 200000, paused=True, strict=True)


# ------------------------------------------------------------------
# generate_device_id
# ------------------------------------------------------------------


class TestGenerateDeviceId:
    """Tests for generate_device_id."""

    def test_format(self) -> None:
        """Device ID is 16-char lowercase alphanumeric."""
        device_id = generate_device_id()
        assert len(device_id) == 16
        assert device_id.isalnum()
        assert device_id.islower() or device_id.isdigit()

    def test_uniqueness(self) -> None:
        """Generated IDs are unique."""
        ids = {generate_device_id() for _ in range(10)}
        assert len(ids) == 10


# ------------------------------------------------------------------
# YnisonClient disconnect
# ------------------------------------------------------------------


class TestYnisonClientDisconnect:
    """Tests for disconnect handling."""

    async def test_disconnect_closes_ws(self, client: YnisonClient) -> None:
        """Disconnect closes WebSocket and clears state."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        client._ws = mock_ws
        client._connected = True

        await client.disconnect()

        mock_ws.close.assert_called_once()
        assert client._connected is False
        assert client._ws is None

    async def test_disconnect_cancels_tasks(self, client: YnisonClient) -> None:
        """Disconnect cancels running message task."""

        # Create a real task that we can cancel
        async def _forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.ensure_future(_forever())
        client._message_task = task

        await client.disconnect()

        assert task.cancelled()

    async def test_disconnect_when_not_connected(self, client: YnisonClient) -> None:
        """Should not raise when already disconnected."""
        await client.disconnect()


# ------------------------------------------------------------------
# Reconnect session ownership
# ------------------------------------------------------------------


class TestReconnectSessionOwnership:
    """Tests for _reconnect respecting external session ownership."""

    async def test_reconnect_reuses_external_session(self) -> None:
        """Reconnect reuses a still-open external session instead of creating a new one."""
        on_state = AsyncMock()
        ext_session = MagicMock(spec=aiohttp.ClientSession)
        ext_session.closed = False

        client = YnisonClient(
            token=SecretStr("test-token"),
            device_info=YnisonDeviceInfo(device_id="dev1", title="Test"),
            on_state_update=on_state,
            logger=MagicMock(),
            http_session=ext_session,
        )
        client._session = None  # simulate session lost
        client._stop_event.clear()

        def stop_after_session_select() -> None:
            client._stop_event.set()
            msg = "stop after session selection"
            raise OSError(msg)

        sleep_path = "music_assistant.providers.yandex_ynison.ynison_client.asyncio.sleep"
        with (
            patch(sleep_path, new_callable=AsyncMock),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
            ) as mock_redir,
        ):
            mock_redir.side_effect = stop_after_session_select
            await client._reconnect()

        assert mock_redir.await_count == 1
        assert client._session is ext_session

    async def test_reconnect_retries_on_closed_external_session_until_stopped(
        self,
    ) -> None:
        """Reconnect with closed external session retries until stop_event is set."""
        on_state = AsyncMock()
        ext_session = MagicMock(spec=aiohttp.ClientSession)
        ext_session.closed = True

        client = YnisonClient(
            token=SecretStr("test-token"),
            device_info=YnisonDeviceInfo(device_id="dev1", title="Test"),
            on_state_update=on_state,
            logger=MagicMock(),
            http_session=ext_session,
        )
        client._session = None  # simulate session lost

        # Simulate an operator calling disconnect() after a few failures —
        # without this the reconnect loop would retry forever.
        sleep_calls = 0

        async def stop_after_n_sleeps(*_args: object, **_kw: object) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:
                client._stop_event.set()

        sleep_path = "music_assistant.providers.yandex_ynison.ynison_client.asyncio.sleep"
        with (
            patch(sleep_path, side_effect=stop_after_n_sleeps),
            patch.object(client, "_get_redirect_ticket", new_callable=AsyncMock) as mock_redir,
        ):
            mock_redir.side_effect = AssertionError("should not reach here")
            await client._reconnect()

        # Never reached the redirect step because session is closed.
        mock_redir.assert_not_awaited()
        assert not client._connected

    async def test_connect_raises_on_closed_external_session(self) -> None:
        """connect() raises RuntimeError if external session is already closed."""
        on_state = AsyncMock()
        ext_session = MagicMock(spec=aiohttp.ClientSession)
        ext_session.closed = True

        client = YnisonClient(
            token=SecretStr("test-token"),
            device_info=YnisonDeviceInfo(device_id="dev1", title="Test"),
            on_state_update=on_state,
            logger=MagicMock(),
            http_session=ext_session,
        )

        with pytest.raises(ResourceTemporarilyUnavailable, match="closed"):
            await client.connect()


# ------------------------------------------------------------------
# connect() transient error → reconnect
# ------------------------------------------------------------------


class TestConnectTransientError:
    """Tests for connect() scheduling reconnect on transient errors."""

    async def test_auth_without_refresh_callback_remains_terminal(
        self, client: YnisonClient
    ) -> None:
        """Authentication without a refresh source fails without an endless reconnect."""
        session = AsyncMock(closed=False)
        client._external_session = session
        with (
            patch.object(client, "_get_redirect_ticket", side_effect=LoginFailed("expired")),
            pytest.raises(LoginFailed, match="expired"),
        ):
            await client.connect()
        assert client._reconnect_task is None
        assert client._stop_event.is_set()
        session.close.assert_not_awaited()

    async def test_invalid_redirect_payload_propagates_without_retry(
        self, client: YnisonClient
    ) -> None:
        """Invalid protocol data is not silently retried as a network outage."""
        session = AsyncMock(closed=False)
        ws = AsyncMock(closed=False)
        ws.receive.return_value = _make_ws_msg(aiohttp.WSMsgType.TEXT, "invalid-json")
        session.ws_connect.return_value = ws
        client._external_session = session
        with pytest.raises(json.JSONDecodeError):
            await client.connect()
        assert client._reconnect_task is None
        ws.close.assert_awaited_once()

    async def test_connect_transient_schedules_reconnect(self) -> None:
        """Non-auth error during connect schedules _reconnect task."""
        on_state = AsyncMock()
        client = YnisonClient(
            token=SecretStr("test-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=on_state,
            logger=MagicMock(),
        )
        with (
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=ConnectionError("network down"),
            ),
            patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_reconnect,
        ):
            await client.connect()
            await asyncio.sleep(0)  # let ensure_future task run

        assert client._connected is False
        assert client._ws is None
        assert client._reconnect_task is not None
        mock_reconnect.assert_awaited_once()

    async def test_connect_transient_closes_ws_and_session(self) -> None:
        """Transient connect error closes stale ws and owned session."""
        on_state = AsyncMock()
        client = YnisonClient(
            token=SecretStr("test-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=on_state,
            logger=MagicMock(),
        )
        mock_ws = AsyncMock()
        mock_ws.closed = False

        async def fake_redirect() -> None:
            # Simulate ws being set before the error
            client._ws = mock_ws
            raise OSError("timeout")

        with (
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=fake_redirect,
            ),
            patch.object(client, "_reconnect", new_callable=AsyncMock),
        ):
            await client.connect()

        mock_ws.close.assert_awaited_once()
        assert client._session is None


# ------------------------------------------------------------------
# disconnect() — reconnect task cancellation
# ------------------------------------------------------------------


class TestDisconnectReconnectCancellation:
    """Tests for disconnect() cancelling a running reconnect task."""

    async def test_disconnect_cancels_reconnect_task(self) -> None:
        """disconnect() cancels and awaits pending reconnect task."""
        on_state = AsyncMock()
        client = YnisonClient(
            token=SecretStr("test-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=on_state,
            logger=MagicMock(),
        )

        async def _forever() -> None:
            await asyncio.Event().wait()

        task = asyncio.ensure_future(_forever())
        client._reconnect_task = task

        await client.disconnect()

        assert task.cancelled()


# ------------------------------------------------------------------
# Message building methods
# ------------------------------------------------------------------


class TestMessageBuildingMethods:
    """Tests for sync_state_from_eov, update_player_state, send_full_state."""

    @pytest.fixture(autouse=True)
    def _setup_ws(self, client: YnisonClient) -> None:
        """Set up a mock WebSocket."""
        self.mock_ws = AsyncMock()
        self.mock_ws.closed = False
        client._ws = self.mock_ws
        client._connected = True

    async def test_sync_state_from_eov(self, client: YnisonClient) -> None:
        """sync_state_from_eov builds correct message structure."""
        await client.sync_state_from_eov(actual_queue_id="q123")
        call_data = json.loads(self.mock_ws.send_str.call_args[0][0])
        assert call_data["sync_state_from_eov"]["actual_queue_id"] == "q123"
        assert "rid" in call_data
        assert call_data["activity_interception_type"] == "DO_NOT_INTERCEPT_BY_DEFAULT"
        # Ynison expects string-typed timestamps (integers cause 500s)
        assert isinstance(call_data["player_action_timestamp_ms"], str)
        assert call_data["player_action_timestamp_ms"].isdigit()

    async def test_update_player_state(self, client: YnisonClient) -> None:
        """update_player_state builds correct message and logs queue info."""
        ps = {
            "player_queue": {
                "current_playable_index": 2,
                "playable_list": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                "entity_type": "ALBUM",
            }
        }
        await client.update_player_state(ps)
        call_data = json.loads(self.mock_ws.send_str.call_args[0][0])
        assert call_data["update_player_state"]["player_state"] == ps
        assert "rid" in call_data
        assert call_data["activity_interception_type"] == "DO_NOT_INTERCEPT_BY_DEFAULT"

    async def test_concurrent_queue_controls_are_serialized(self, client: YnisonClient) -> None:
        """A second command includes the first command even while its send is pending."""
        client.state.player_state = {"player_queue": {"playable_list": []}}
        entered = asyncio.Event()
        release = asyncio.Event()

        async def send(_data: str) -> None:
            if not entered.is_set():
                entered.set()
                await release.wait()

        self.mock_ws.send_str.side_effect = send
        first = asyncio.create_task(
            client.mutate_player_state(
                lambda ps: ps["player_queue"].update(options={"repeat_mode": "ONE"})
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(
            client.mutate_player_state(
                lambda ps: ps["player_queue"].update(shuffle_optional={"playable_indices": []})
            )
        )
        release.set()
        await asyncio.gather(first, second)
        sent = json.loads(self.mock_ws.send_str.call_args.args[0])["update_player_state"][
            "player_state"
        ]
        assert sent["player_queue"]["options"]["repeat_mode"] == "ONE"
        assert "shuffle_optional" in sent["player_queue"]

    async def test_failed_queue_send_is_not_replayed_and_disconnect_clears_pending(
        self,
        client: YnisonClient,
    ) -> None:
        """Only successfully sent edits survive a failed write, until disconnect."""
        client.state.player_state = {"player_queue": {"playable_list": []}}
        await client.mutate_player_state(
            lambda ps: ps["player_queue"].update(options={"repeat_mode": "ALL"})
        )
        self.mock_ws.send_str.side_effect = OSError("write failed")
        try:
            with pytest.raises(YnisonSendError):
                await client.mutate_player_state(
                    lambda ps: ps["player_queue"].update(shuffle_optional={"playable_indices": []})
                )
            self.mock_ws.send_str.side_effect = None
            client._connected = True
            await client.mutate_player_state(lambda _ps: None)
            sent = json.loads(self.mock_ws.send_str.call_args.args[0])["update_player_state"][
                "player_state"
            ]
            assert sent["player_queue"]["options"]["repeat_mode"] == "ALL"
            assert "shuffle_optional" not in sent["player_queue"]
            assert "options" not in client.state.player_state["player_queue"]
        finally:
            await client.disconnect()
        client._ws = self.mock_ws
        client._connected = True
        await client.mutate_player_state(lambda _ps: None)
        sent = json.loads(self.mock_ws.send_str.call_args.args[0])["update_player_state"][
            "player_state"
        ]
        assert "options" not in sent["player_queue"]

    async def test_unacknowledged_queue_expires(self, client: YnisonClient) -> None:
        """Missing acknowledgements cannot keep an optimistic queue indefinitely."""
        client.state.player_state = {"player_queue": {"playable_list": []}}
        with patch(
            "music_assistant.providers.yandex_ynison.ynison_client.time.monotonic", return_value=100
        ):
            await client.mutate_player_state(
                lambda ps: ps["player_queue"].update(options={"repeat_mode": "ALL"})
            )
        with patch(
            "music_assistant.providers.yandex_ynison.ynison_client.time.monotonic", return_value=131
        ):
            await client.mutate_player_state(lambda _ps: None)
        sent = json.loads(self.mock_ws.send_str.call_args.args[0])["update_player_state"][
            "player_state"
        ]
        assert "options" not in sent["player_queue"]

    async def test_send_full_state_default(self, client: YnisonClient) -> None:
        """send_full_state with no args sends initial state and device dict."""
        await client.send_full_state()
        call_data = json.loads(self.mock_ws.send_str.call_args[0][0])
        ufs = call_data["update_full_state"]
        assert ufs["device"]["info"]["device_id"] == "test-device-id"
        assert ufs["player_state"]["status"]["paused"] is True
        assert ufs["is_currently_active"] is False
        assert "rid" in call_data

    async def test_send_full_state_custom(self, client: YnisonClient) -> None:
        """send_full_state with custom player_state uses it."""
        custom_state = {"status": {"paused": False, "progress_ms": 42}}
        await client.send_full_state(player_state=custom_state)
        call_data = json.loads(self.mock_ws.send_str.call_args[0][0])
        assert call_data["update_full_state"]["player_state"] == custom_state


class TestGetRedirectTicket:
    """Tests for _get_redirect_ticket."""

    async def test_success(self, client: YnisonClient) -> None:
        """Returns (host, ticket, session_id) on success."""
        mock_msg = MagicMock()
        mock_msg.type = aiohttp.WSMsgType.TEXT
        mock_msg.data = json.dumps(
            {
                "host": "ynison-node.yandex.net",
                "redirect_ticket": "ticket-abc",
                "session_id": 42,
            }
        )

        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(return_value=mock_msg)
        mock_ws.close = AsyncMock()

        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        client._session = mock_session

        host, ticket, sid = await client._get_redirect_ticket()

        assert host == "ynison-node.yandex.net"
        assert ticket == "ticket-abc"
        assert sid == 42
        mock_ws.close.assert_awaited_once()

    @pytest.mark.parametrize(
        ("redirect_extra", "heartbeat"),
        [
            ({"keep_alive_params": {"keep_alive_time_seconds": 20}}, 20.0),
            ({"keep_alive_params": {"keep_alive_time_seconds": "45"}}, 45.0),
            ({"keep_alive_params": {"keep_alive_time_seconds": 0}}, 30.0),
            ({"keep_alive_params": {"keep_alive_time_seconds": 0.01}}, 5.0),
            ({}, 30.0),
        ],
    )
    async def test_server_keepalive_drives_state_heartbeat(
        self,
        client: YnisonClient,
        redirect_extra: dict[str, Any],
        heartbeat: float,
    ) -> None:
        """The redirector's keep-alive interval becomes the state socket heartbeat."""
        redirect_msg = MagicMock()
        redirect_msg.type = aiohttp.WSMsgType.TEXT
        redirect_msg.data = json.dumps(
            {"host": "node", "redirect_ticket": "ticket", "session_id": 1, **redirect_extra}
        )
        redirect_ws = AsyncMock()
        redirect_ws.receive = AsyncMock(return_value=redirect_msg)
        state_ws = AsyncMock()
        session = AsyncMock()
        session.ws_connect = AsyncMock(side_effect=[redirect_ws, state_ws])
        client._session = session

        with patch.object(client, "send_full_state", new_callable=AsyncMock):
            await client._connect_state(*await client._get_redirect_ticket())
        message_task = client._message_task
        assert message_task is not None
        message_task.cancel()
        with suppress(asyncio.CancelledError):
            await message_task

        assert session.ws_connect.await_args_list[1].kwargs["heartbeat"] == heartbeat

    async def test_auth_failure_401(self, client: YnisonClient) -> None:
        """401 WSServerHandshakeError raises LoginFailed."""
        err = aiohttp.WSServerHandshakeError(
            request_info=MagicMock(),
            history=(),
            status=401,
            message="Unauthorized",
            headers=MagicMock(),
        )
        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(side_effect=err)
        client._session = mock_session

        with pytest.raises(LoginFailed):
            await client._get_redirect_ticket()

    async def test_auth_failure_403(self, client: YnisonClient) -> None:
        """403 WSServerHandshakeError raises LoginFailed."""
        err = aiohttp.WSServerHandshakeError(
            request_info=MagicMock(),
            history=(),
            status=403,
            message="Forbidden",
            headers=MagicMock(),
        )
        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(side_effect=err)
        client._session = mock_session

        with pytest.raises(LoginFailed):
            await client._get_redirect_ticket()

    async def test_network_error_500(self, client: YnisonClient) -> None:
        """500 WSServerHandshakeError re-raises (not LoginFailed)."""
        err = aiohttp.WSServerHandshakeError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Server Error",
            headers=MagicMock(),
        )
        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(side_effect=err)
        client._session = mock_session

        with pytest.raises(aiohttp.WSServerHandshakeError):
            await client._get_redirect_ticket()

    async def test_missing_host_ticket(self, client: YnisonClient) -> None:
        """Missing host/ticket is identified as an authentication-suspect redirect."""
        mock_msg = MagicMock()
        mock_msg.type = aiohttp.WSMsgType.TEXT
        mock_msg.data = json.dumps({"host": "", "redirect_ticket": ""})

        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(return_value=mock_msg)
        mock_ws.close = AsyncMock()

        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        client._session = mock_session

        with pytest.raises(LoginFailed, match="missing host or ticket"):
            await client._get_redirect_ticket()

    async def test_unexpected_msg_type(self, client: YnisonClient) -> None:
        """Non-TEXT/BINARY message type raises ConnectionError."""
        mock_msg = MagicMock()
        mock_msg.type = aiohttp.WSMsgType.CLOSE
        mock_msg.data = None

        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(return_value=mock_msg)
        mock_ws.close = AsyncMock()

        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        client._session = mock_session

        with pytest.raises(ConnectionError, match="Unexpected message type"):
            await client._get_redirect_ticket()

    async def test_no_session_raises_runtime_error(self, client: YnisonClient) -> None:
        """Raises RuntimeError when session is None."""
        client._session = None
        with pytest.raises(RuntimeError, match="session not initialized"):
            await client._get_redirect_ticket()


# ------------------------------------------------------------------
# _connect_state
# ------------------------------------------------------------------


class TestConnectState:
    """Tests for _connect_state."""

    async def test_success(self, client: YnisonClient) -> None:
        """Successful connect sets _connected, calls send_full_state, starts loop."""
        mock_ws = AsyncMock()

        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        client._session = mock_session

        with patch.object(client, "send_full_state", new_callable=AsyncMock) as mock_sfs:
            await client._connect_state("host.yandex.net", "ticket", 42)

        assert client._connected is True
        # Cold start: send_full_state called with no args (blank state)
        mock_sfs.assert_awaited_once_with()
        assert client._has_connected_once is True
        assert client._message_task is not None
        # Clean up the task
        client._message_task.cancel()
        with suppress(asyncio.CancelledError):
            await client._message_task

    async def test_reconnect_sends_fresh_state_no_stale_replay(self, client: YnisonClient) -> None:
        """
        v2.0: reconnect sends a fresh initial state — no stale replay.

        Replaying the last known state (which after a heartbeat could carry
        `paused=True`) caused the server to broadcast it back and trigger
        an unintended pause on the still-running player.
        """
        mock_ws = AsyncMock()
        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        client._session = mock_session

        # Simulate prior connection with stale paused state cached.
        client._has_connected_once = True
        client.state.player_state = {
            "status": {"paused": True, "progress_ms": 120000, "duration_ms": 300000},
            "player_queue": {
                "current_playable_index": 3,
                "playable_list": [{"playable_id": "t1"}],
            },
        }

        with patch.object(client, "send_full_state", new_callable=AsyncMock) as mock_sfs:
            await client._connect_state("host.yandex.net", "ticket", 42)

        # send_full_state must be called WITHOUT player_state — it falls
        # back to a fresh _build_initial_state() internally.
        mock_sfs.assert_awaited_once_with()
        # Settle window armed for ~2 s.
        assert client.in_post_reconnect_settle is True
        # Clean up
        assert client._message_task is not None
        client._message_task.cancel()
        with suppress(asyncio.CancelledError):
            await client._message_task

    async def test_cold_start_does_not_arm_settle_window(self, client: YnisonClient) -> None:
        """First-ever connect skips the settle window — nothing stale to swallow."""
        mock_ws = AsyncMock()
        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        client._session = mock_session

        with patch.object(client, "send_full_state", new_callable=AsyncMock):
            await client._connect_state("host.yandex.net", "ticket", 42)

        assert client.in_post_reconnect_settle is False
        assert client._message_task is not None
        client._message_task.cancel()
        with suppress(asyncio.CancelledError):
            await client._message_task

    async def test_auth_failure_401(self, client: YnisonClient) -> None:
        """401 during state connect raises LoginFailed."""
        err = aiohttp.WSServerHandshakeError(
            request_info=MagicMock(),
            history=(),
            status=401,
            message="Unauthorized",
            headers=MagicMock(),
        )
        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(side_effect=err)
        client._session = mock_session

        with pytest.raises(LoginFailed):
            await client._connect_state("host", "ticket", 1)

    async def test_no_session_raises_runtime_error(self, client: YnisonClient) -> None:
        """Raises RuntimeError when session is None."""
        client._session = None
        with pytest.raises(RuntimeError, match="session not initialized"):
            await client._connect_state("host", "ticket", 1)


# ------------------------------------------------------------------
# _message_loop
# ------------------------------------------------------------------


def _make_ws_msg(
    msg_type: aiohttp.WSMsgType,
    data: str | bytes | None = None,
    extra: Any = None,
) -> MagicMock:
    """Create a mock WS message."""
    msg = MagicMock()
    msg.type = msg_type
    msg.data = data
    msg.extra = extra
    return msg


class TestMessageLoop:
    """Tests for _message_loop."""

    @pytest.mark.parametrize("during_send", [False, True])
    async def test_peer_queue_supersedes_command_waiting_for_transport(
        self,
        client: YnisonClient,
        mock_state_callback: AsyncMock,
        during_send: bool,
    ) -> None:
        """A peer replacement rejects queued and in-flight mutations as obsolete."""
        client.state.player_state = {
            "player_queue": {
                "playable_list": [{"playable_id": "old"}],
                "current_playable_index": 0,
            }
        }
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_send(_data: str) -> None:
            entered.set()
            await release.wait()

        ws = AsyncMock(closed=False)
        ws.send_str.side_effect = slow_send
        client._ws = ws
        client._connected = True
        heartbeat = None
        if not during_send:
            heartbeat = asyncio.create_task(client.update_playing_status(0, 0, False))
            await asyncio.wait_for(entered.wait(), timeout=1)
        command = asyncio.create_task(
            client.mutate_player_state(
                lambda ps: ps["player_queue"].update(options={"repeat_mode": "ALL"})
            )
        )
        if during_send:
            await asyncio.wait_for(entered.wait(), timeout=1)
        else:
            await asyncio.sleep(0)
        mock_state_callback.side_effect = lambda _state: client._stop_event.set()
        try:
            await self._run_loop_with_messages(
                client,
                [
                    _make_ws_msg(
                        aiohttp.WSMsgType.TEXT,
                        json.dumps(
                            {
                                "player_state": {
                                    "player_queue": {
                                        "playable_list": [{"playable_id": "peer-new"}],
                                        "current_playable_index": 0,
                                        "version": {
                                            "device_id": "peer",
                                            "version": "1",
                                            "timestamp_ms": "0",
                                        },
                                    }
                                }
                            }
                        ),
                    )
                ],
            )
            release.set()
            if heartbeat is not None:
                await heartbeat
            with pytest.raises(ResourceTemporarilyUnavailable, match="queue changed"):
                await command
            client._ws.send_str.assert_not_awaited()
            assert client.state.current_track_id == "peer-new"
            assert client.queue_snapshot()["playable_list"] == [{"playable_id": "peer-new"}]
        finally:
            release.set()
            tasks: list[asyncio.Task[Any]] = (
                [command] if heartbeat is None else [heartbeat, command]
            )
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_callback_programming_error_propagates(
        self,
        client: YnisonClient,
        mock_state_callback: AsyncMock,
    ) -> None:
        """A broken state callback propagates while scheduling transport recovery."""
        mock_state_callback.side_effect = ValueError("broken callback")
        msg = _make_ws_msg(aiohttp.WSMsgType.TEXT, json.dumps({"player_state": {}}))
        with pytest.raises(ValueError, match="broken callback"):
            await self._run_loop_with_messages(client, [msg])
        assert not client.connected
        assert client._reconnect_task is not None
        await client._reconnect_task

    @pytest.mark.parametrize("fault", ["callback", "parser"])
    async def test_programming_error_closes_socket_and_schedules_recovery(
        self, client: YnisonClient, mock_state_callback: AsyncMock, fault: str
    ) -> None:
        """Unexpected failures retain their traceback and cannot leak a live socket."""
        msg = _make_ws_msg(aiohttp.WSMsgType.TEXT, json.dumps({"player_state": {}}))

        async def messages(_socket: object) -> AsyncGenerator[MagicMock]:
            yield msg

        socket = MagicMock(closed=False, close=AsyncMock())
        socket.__aiter__ = messages
        client._ws = socket
        client._connected = True
        if fault == "callback":
            mock_state_callback.side_effect = ValueError("broken callback")
        with (
            patch.object(client, "_parse_state", wraps=client._parse_state) as parse,
            patch.object(client, "_schedule_reconnect") as schedule,
        ):
            if fault == "parser":
                parse.side_effect = ValueError("broken parser")
            with pytest.raises(ValueError, match="broken"):
                await client._message_loop()

        socket.close.assert_awaited_once()
        schedule.assert_called_once()
        assert not client.connected

    @pytest.mark.parametrize("field", ["progress_ms", "duration_ms"])
    @pytest.mark.parametrize("value", [None, "invalid", {}, True])
    async def test_incomplete_heartbeat_cannot_match_sent_status(
        self, client: YnisonClient, field: str, value: object
    ) -> None:
        """Missing or malformed wire values cannot break echo classification."""
        client._ws = AsyncMock(closed=False)
        client.state.player_state = {
            "player_queue": {"current_playable_index": 0, "playable_list": [{"playable_id": "t1"}]}
        }
        await client.update_playing_status(1000, 2000, False)
        status: dict[str, Any] = {
            "progress_ms": "1000",
            "duration_ms": "2000",
            "paused": False,
            "version": {"device_id": "", "version": "0", "timestamp_ms": "0"},
        }
        if value is None:
            status.pop(field)
        else:
            status[field] = value
        assert client._classify_state_as_echo({"status": status}) == (False, False)

    async def test_stale_message_loop_cannot_close_replacement_socket(
        self, client: YnisonClient, mock_state_callback: AsyncMock
    ) -> None:
        """A reconnect completed during a callback owns its own transport cleanup."""
        replacement = AsyncMock(closed=False)

        async def replace_socket(_state: YnisonState) -> None:
            client._ws = replacement
            client._connected = True
            raise ValueError("old callback failed")

        mock_state_callback.side_effect = replace_socket
        msg = _make_ws_msg(aiohttp.WSMsgType.TEXT, json.dumps({"player_state": {}}))
        with pytest.raises(ValueError, match="old callback failed"):
            await self._run_loop_with_messages(client, [msg])
        replacement.close.assert_not_awaited()
        assert client.connected
        assert client._reconnect_task is None

    async def _run_loop_with_messages(
        self,
        client: YnisonClient,
        messages: list[MagicMock],
    ) -> None:
        """Set up mock ws and run _message_loop."""

        async def _aiter(_self: Any) -> Any:
            for m in messages:
                yield m

        mock_ws = MagicMock()
        mock_ws.__aiter__ = _aiter
        mock_ws.send_str = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.closed = False
        mock_ws.exception = MagicMock(return_value=None)
        mock_ws.close_code = None
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock):
            await client._message_loop()

    async def test_text_message_parses_and_calls_callback(
        self,
        client: YnisonClient,
        mock_state_callback: AsyncMock,
    ) -> None:
        """TEXT message: parses JSON, updates state, invokes callback."""
        on_state_update = mock_state_callback
        payload = {
            "player_state": {
                "status": {"paused": False, "progress_ms": 1000, "duration_ms": 5000},
                "player_queue": {
                    "current_playable_index": 0,
                    "playable_list": [{"playable_id": "t1"}],
                },
            },
            "active_device_id_optional": "dev1",
        }
        msg = _make_ws_msg(aiohttp.WSMsgType.TEXT, json.dumps(payload))
        await self._run_loop_with_messages(client, [msg])

        on_state_update.assert_awaited_once()
        assert client.state.current_track_id == "t1"
        assert client.state.is_paused is False

    @pytest.mark.parametrize("echo", ["older", "latest_then_older", "peer", "unchanged_peer"])
    async def test_queue_commands_after_incoming_echo(
        self,
        client: YnisonClient,
        mock_state_callback: AsyncMock,
        echo: str,
    ) -> None:
        """Older acknowledgements cannot roll back controls; peer queues take over."""
        client.state.player_state = {
            "player_queue": {
                "playable_list": [{"playable_id": "original"}],
                "current_playable_index": 0,
            }
        }
        client._ws = AsyncMock(closed=False)
        client._connected = True

        def repeat(state: dict[str, Any]) -> None:
            state["player_queue"]["options"] = {"repeat_mode": "ALL"}

        def shuffle(state: dict[str, Any]) -> None:
            state["player_queue"]["shuffle_optional"] = {"playable_indices": [0]}

        await client.mutate_player_state(repeat)
        first = json.loads(client._ws.send_str.call_args.args[0])["update_player_state"][
            "player_state"
        ]
        await client.mutate_player_state(shuffle)
        second = json.loads(client._ws.send_str.call_args.args[0])["update_player_state"][
            "player_state"
        ]
        frames = [first]
        if echo == "latest_then_older":
            frames.insert(0, second)
        elif echo == "unchanged_peer":
            frames = [deepcopy(client.state.player_state)]
        elif echo == "peer":
            frames = [
                {
                    "player_queue": {
                        "playable_list": [{"playable_id": "peer-track"}],
                        "current_playable_index": 0,
                        "options": {"repeat_mode": "NONE"},
                        "version": {"device_id": "peer", "version": "1", "timestamp_ms": "0"},
                    }
                }
            ]
        received = 0

        async def on_state(_state: YnisonState) -> None:
            nonlocal received
            received += 1
            if received == len(frames):
                await client.mutate_player_state(
                    lambda ps: ps["player_queue"].update(entity_id="next")
                )
                client._stop_event.set()

        mock_state_callback.side_effect = on_state
        await self._run_loop_with_messages(
            client,
            [
                _make_ws_msg(aiohttp.WSMsgType.TEXT, json.dumps({"player_state": frame}))
                for frame in frames
            ],
        )
        sent = json.loads(client._ws.send_str.call_args.args[0])["update_player_state"][
            "player_state"
        ]
        queue = sent["player_queue"]
        if echo == "peer":
            assert queue["playable_list"] == [{"playable_id": "peer-track"}]
            assert queue["options"]["repeat_mode"] == "NONE"
            assert "shuffle_optional" not in queue
        else:
            assert queue["options"]["repeat_mode"] == "ALL"
            assert queue["shuffle_optional"]["playable_indices"] == [0]

    async def test_text_message_with_error_field(self, client: YnisonClient) -> None:
        """TEXT message with non-reconnect error logs warning, continues."""
        error_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"error": {"code": 500, "message": "server error"}}),
        )
        # Second valid message to confirm the loop continues
        valid_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": True}}}),
        )
        await self._run_loop_with_messages(client, [error_msg, valid_msg])

        client._logger.warning.assert_called()

    async def test_rebalance_error_breaks_loop(
        self,
        client: YnisonClient,
        mock_state_callback: AsyncMock,
    ) -> None:
        """Ynison re-balance error (300100001) breaks the loop for immediate reconnect."""
        on_state_update = mock_state_callback
        rebalance_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps(
                {
                    "error": {
                        "details": {
                            "ynison-error-code": "300100001",
                            "ynison-backoff-millis": "0:100:500:1000:1000:5000",
                        },
                        "grpc_code": 10,
                        "http_code": 409,
                        "message": "User re-balanced to another host",
                    }
                }
            ),
        )
        # This message should NOT be reached because the loop breaks
        valid_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": True}}}),
        )
        await self._run_loop_with_messages(client, [rebalance_msg, valid_msg])

        # The valid message was never processed (loop broke on re-balance error)
        on_state_update.assert_not_awaited()

    async def test_not_served_error_breaks_loop(
        self,
        client: YnisonClient,
        mock_state_callback: AsyncMock,
    ) -> None:
        """Ynison 'not served' error (300100002) also breaks the loop."""
        on_state_update = mock_state_callback
        not_served_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps(
                {
                    "error": {
                        "details": {"ynison-error-code": "300100002"},
                        "grpc_code": 10,
                        "http_code": 409,
                        "message": "Current user's not served by this host",
                    }
                }
            ),
        )
        valid_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": True}}}),
        )
        await self._run_loop_with_messages(client, [not_served_msg, valid_msg])

        on_state_update.assert_not_awaited()

    async def _record_reconnect_delays(self, client: YnisonClient, count: int) -> list[float]:
        """Run ``count`` failing reconnect attempts and return their scheduled delays."""
        client._session = MagicMock()
        client._session.closed = False
        sleeps: list[float] = []

        async def record_sleep(delay: float) -> None:
            sleeps.append(delay)
            if len(sleeps) >= count:
                client._stop_event.set()

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock, side_effect=record_sleep),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=ConnectionError("down"),
            ),
        ):
            await client._reconnect()
        client._stop_event.clear()
        return sleeps

    async def test_go_away_error_breaks_loop_and_waits_exact_window(
        self,
        client: YnisonClient,
        mock_state_callback: AsyncMock,
    ) -> None:
        """A server go-away stops the session and holds the next dial for exactly its window."""
        go_away_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT, json.dumps(_fixture("go_away_throttle.json"))
        )
        valid_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": True}}}),
        )

        await self._run_loop_with_messages(client, [go_away_msg, valid_msg])
        mock_state_callback.assert_not_awaited()

        assert await self._record_reconnect_delays(client, 1) == [3600.0]

    async def test_repeated_go_away_waits_server_window_without_escalation(
        self, client: YnisonClient
    ) -> None:
        """Consecutive go-aways each wait the server window; the client adds no escalation."""
        go_away = _make_ws_msg(
            aiohttp.WSMsgType.TEXT, json.dumps(_fixture("go_away_throttle.json"))
        )
        for _ in range(3):
            await self._run_loop_with_messages(client, [go_away])
            assert await self._record_reconnect_delays(client, 1) == [3600.0]

    async def test_ladder_restarts_after_go_away_window(self, client: YnisonClient) -> None:
        """Failures after a go-away wait restart the default ladder from its first step."""
        await self._record_reconnect_delays(client, 3)
        await self._run_loop_with_messages(
            client,
            [_make_ws_msg(aiohttp.WSMsgType.TEXT, json.dumps(_fixture("go_away_throttle.json")))],
        )

        assert await self._record_reconnect_delays(client, 3) == [3600.0, 1.0, 5.0]

    async def test_go_away_defers_a_reconnect_already_waiting(self, client: YnisonClient) -> None:
        """A go-away that arrives while a reconnect waits holds that dial for its window."""
        client._session = MagicMock()
        client._session.closed = False
        client._policy.on_transport_failure()
        sleeps: list[float] = []
        dials: list[list[float]] = []

        async def record_sleep(delay: float) -> None:
            sleeps.append(delay)
            if len(sleeps) == 1:
                client._apply_error_frame(_fixture("go_away_throttle.json")["error"])

        async def dial() -> None:
            dials.append(list(sleeps))
            client._stop_event.set()
            raise ConnectionError("down")

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock, side_effect=record_sleep),
            patch.object(client, "_get_redirect_ticket", new_callable=AsyncMock, side_effect=dial),
        ):
            await client._reconnect()
        client._stop_event.clear()

        assert dials == [[1.0, 3600.0]]

    async def test_drop_after_healthy_state_is_a_transport_failure(
        self, client: YnisonClient
    ) -> None:
        """A non-fatal error followed by state and a drop retries on the transport ladder."""
        error = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"error": {"details": {"ynison-error-code": "400030001"}, "grpc_code": 3}}),
        )
        state = _make_ws_msg(
            aiohttp.WSMsgType.TEXT, json.dumps({"player_state": {"status": {"paused": True}}})
        )
        await self._run_loop_with_messages(client, [error, state])

        assert await self._record_reconnect_delays(client, 1) == [1.0]

    async def test_server_backoff_ladder_replaces_default(self, client: YnisonClient) -> None:
        """``ynison-backoff-millis`` supplies the ladder, sorted and de-duplicated."""
        rebalance = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps(
                {
                    "error": {
                        "details": {
                            "ynison-error-code": "300100001",
                            "ynison-backoff-millis": "0:100:500:1000:1000:5000",
                        },
                        "grpc_code": 10,
                        "http_code": 409,
                    }
                }
            ),
        )
        await self._run_loop_with_messages(client, [rebalance])

        # The first dial follows the server ladder; failed dials are transport
        # failures, a different error kind, so the default ladder takes over.
        assert await self._record_reconnect_delays(client, 5) == [0.0, 1.0, 5.0, 30.0, 300.0]

    async def test_state_frames_wait_for_the_previous_callback(
        self, client: YnisonClient, mock_state_callback: AsyncMock
    ) -> None:
        """A frame is not applied while the provider still handles the previous one."""
        seen_during_callback: list[bool] = []

        async def slow_callback(state: YnisonState) -> None:
            if not seen_during_callback:
                await asyncio.sleep(0.01)
                seen_during_callback.append(state.is_paused)

        mock_state_callback.side_effect = slow_callback
        frames = [
            _make_ws_msg(
                aiohttp.WSMsgType.TEXT,
                json.dumps({"player_state": {"status": {"paused": paused}}}),
            )
            for paused in (False, True)
        ]

        await self._run_loop_with_messages(client, frames)

        assert seen_during_callback == [False]

    async def test_text_message_invalid_json(self, client: YnisonClient) -> None:
        """TEXT message with invalid JSON logs warning, continues."""
        bad_msg = _make_ws_msg(aiohttp.WSMsgType.TEXT, "not valid json{{{")
        valid_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": True}}}),
        )
        await self._run_loop_with_messages(client, [bad_msg, valid_msg])

        client._logger.warning.assert_called()

    async def test_text_message_non_object_json_is_skipped(
        self, client: YnisonClient, mock_state_callback: AsyncMock
    ) -> None:
        """A JSON value that is not an object is skipped like invalid JSON."""
        bad_msg = _make_ws_msg(aiohttp.WSMsgType.TEXT, "[1, 2]")
        valid_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": True}}}),
        )
        await self._run_loop_with_messages(client, [bad_msg, valid_msg])

        client._logger.warning.assert_called()
        mock_state_callback.assert_awaited_once()

    async def test_callback_exception_continues(
        self,
        client: YnisonClient,
        mock_state_callback: AsyncMock,
    ) -> None:
        """Exception in state callback is caught, loop continues."""
        on_state_update = mock_state_callback
        on_state_update.side_effect = [ResourceTemporarilyUnavailable("busy"), None]

        msg1 = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": True}}}),
        )
        msg2 = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": False}}}),
        )
        await self._run_loop_with_messages(client, [msg1, msg2])

        assert on_state_update.await_count == 2

    async def test_binary_message_logged(self, client: YnisonClient) -> None:
        """BINARY message is logged, loop continues."""
        bin_msg = _make_ws_msg(aiohttp.WSMsgType.BINARY, b"\x00\x01\x02")
        valid_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": True}}}),
        )
        await self._run_loop_with_messages(client, [bin_msg, valid_msg])

        client._logger.debug.assert_called()

    async def test_error_message_breaks_and_reconnects(self, client: YnisonClient) -> None:
        """ERROR message breaks loop and schedules reconnect."""

        async def _aiter(_self: Any) -> Any:
            yield _make_ws_msg(aiohttp.WSMsgType.ERROR)

        mock_ws = MagicMock()
        mock_ws.__aiter__ = _aiter
        mock_ws.exception = MagicMock(return_value=Exception("ws error"))
        mock_ws.close_code = None
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            await client._message_loop()
            await asyncio.sleep(0)  # let ensure_future task run

        assert client._connected is False
        mock_rc.assert_awaited_once()

    async def test_close_message_breaks_and_reconnects(self, client: YnisonClient) -> None:
        """CLOSE message breaks loop and schedules reconnect."""

        async def _aiter(_self: Any) -> Any:
            yield _make_ws_msg(aiohttp.WSMsgType.CLOSE, extra="normal close")

        mock_ws = MagicMock()
        mock_ws.__aiter__ = _aiter
        mock_ws.exception = MagicMock(return_value=None)
        mock_ws.close_code = 1000
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            await client._message_loop()
            await asyncio.sleep(0)  # let ensure_future task run

        assert client._connected is False
        mock_rc.assert_awaited_once()

    async def test_closing_message_breaks_loop(self, client: YnisonClient) -> None:
        """CLOSING message breaks loop."""

        async def _aiter(_self: Any) -> Any:
            yield _make_ws_msg(aiohttp.WSMsgType.CLOSING)

        mock_ws = MagicMock()
        mock_ws.__aiter__ = _aiter
        mock_ws.exception = MagicMock(return_value=None)
        mock_ws.close_code = None
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock):
            await client._message_loop()

        assert client._connected is False

    async def test_stop_event_breaks_loop(self, client: YnisonClient) -> None:
        """stop_event set → breaks loop without reconnect."""
        client._stop_event.set()

        async def _aiter(_self: Any) -> Any:
            yield _make_ws_msg(
                aiohttp.WSMsgType.TEXT,
                json.dumps({"player_state": {}}),
            )

        mock_ws = MagicMock()
        mock_ws.__aiter__ = _aiter
        mock_ws.exception = MagicMock(return_value=None)
        mock_ws.close_code = None
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            await client._message_loop()

        mock_rc.assert_not_awaited()

    async def test_cancelled_error_exits_cleanly(self, client: YnisonClient) -> None:
        """CancelledError exits without reconnect."""

        async def _aiter(_self: Any) -> Any:
            raise asyncio.CancelledError
            yield

        mock_ws = MagicMock()
        mock_ws.__aiter__ = _aiter
        mock_ws.exception = MagicMock(return_value=None)
        mock_ws.close_code = None
        client._ws = mock_ws
        client._connected = True

        # CancelledError should be handled cleanly (no reconnect)
        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            await client._message_loop()

        mock_rc.assert_not_awaited()

    async def test_empty_data_message(self, client: YnisonClient) -> None:
        """Message with empty data gets '<empty>' preview."""
        msg = _make_ws_msg(aiohttp.WSMsgType.TEXT, "")
        # Empty string → json.loads will fail → warning logged
        await self._run_loop_with_messages(client, [msg])
        client._logger.warning.assert_called()

    async def test_no_ws_raises_runtime_error(self, client: YnisonClient) -> None:
        """_message_loop raises RuntimeError when ws is None."""
        client._ws = None
        with pytest.raises(RuntimeError, match="not connected"):
            await client._message_loop()


# ------------------------------------------------------------------
# _reconnect
# ------------------------------------------------------------------

SLEEP_PATH = "music_assistant.providers.yandex_ynison.ynison_client.asyncio.sleep"


class TestReconnect:
    """Tests for _reconnect."""

    async def test_success_on_first_attempt(self, client: YnisonClient) -> None:
        """Reconnect succeeds on first attempt."""
        client._session = MagicMock()
        client._session.closed = False

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                return_value=("host", "ticket", 1),
            ),
            patch.object(client, "_connect_state", new_callable=AsyncMock),
        ):
            await client._reconnect()

        client._logger.info.assert_any_call("Ynison reconnected successfully")

    async def test_retries_indefinitely_until_stopped(self, client: YnisonClient) -> None:
        """Reconnect keeps retrying past the old 5-attempt cap until stop_event."""
        client._session = MagicMock()
        client._session.closed = False

        attempt_count = 0
        stop_after = 8  # well past the old MAX_RECONNECT_ATTEMPTS of 5

        async def failing_redirect() -> tuple[str, str, int]:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count >= stop_after:
                client._stop_event.set()
            msg = "fail"
            raise ConnectionError(msg)

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=failing_redirect,
            ),
        ):
            await client._reconnect()

        assert attempt_count >= stop_after

    async def test_backoff_escalates_across_sessions_lost_before_healthy_state(
        self, client: YnisonClient
    ) -> None:
        """A session that connects but dies before any state does not reset the backoff."""
        client._session = MagicMock()
        client._session.closed = False
        sleeps: list[float] = []

        async def record_sleep(delay: float) -> None:
            sleeps.append(delay)

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock, side_effect=record_sleep),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                return_value=("host", "ticket", 1),
            ),
            patch.object(client, "_connect_state", new_callable=AsyncMock),
        ):
            for _ in range(3):
                await client._reconnect()

        assert sleeps == [1.0, 5.0, 30.0]

    async def test_healthy_state_resets_backoff(self, client: YnisonClient) -> None:
        """The first state message of a session restores the shortest reconnect delay."""
        client._session = MagicMock()
        client._session.closed = False
        sleeps: list[float] = []

        async def record_sleep(delay: float) -> None:
            sleeps.append(delay)

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock, side_effect=record_sleep),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                return_value=("host", "ticket", 1),
            ),
            patch.object(client, "_connect_state", new_callable=AsyncMock),
        ):
            await client._reconnect()
            await client._reconnect()
            await TestMessageLoop()._run_loop_with_messages(
                client,
                [
                    _make_ws_msg(
                        aiohttp.WSMsgType.TEXT,
                        json.dumps({"player_state": {"status": {"paused": True}}}),
                    )
                ],
            )
            await client._reconnect()

        assert sleeps == [1.0, 5.0, 1.0]

    async def test_stop_event_before_attempt(self, client: YnisonClient) -> None:
        """stop_event set before reconnect → exits immediately."""
        client._stop_event.set()
        await client._reconnect()

    async def test_stop_event_after_sleep(self, client: YnisonClient) -> None:
        """stop_event set during sleep → exits on next check."""

        async def set_stop(*_args: Any, **_kwargs: Any) -> None:
            client._stop_event.set()

        client._session = MagicMock()
        client._session.closed = False

        with patch(SLEEP_PATH, new_callable=AsyncMock, side_effect=set_stop):
            await client._reconnect()

        # Should exit without calling _get_redirect_ticket
        assert client._stop_event.is_set()

    async def test_cancelled_error_during_reconnect(self, client: YnisonClient) -> None:
        """CancelledError during reconnect exits cleanly."""
        client._session = MagicMock()
        client._session.closed = False

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ),
        ):
            await client._reconnect()

    async def test_creates_new_session_when_none(self, client: YnisonClient) -> None:
        """Creates new ClientSession when _session is None and no external."""
        client._session = None
        client._external_session = None

        mock_new_session = MagicMock(spec=aiohttp.ClientSession)
        mock_new_session.closed = False
        mock_new_session.close = AsyncMock()

        def stop_after_session(*_args: Any, **_kwargs: Any) -> None:
            client._stop_event.set()
            msg = "stop"
            raise OSError(msg)

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch(
                "music_assistant.providers.yandex_ynison.ynison_client.aiohttp.ClientSession",
                return_value=mock_new_session,
            ),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=stop_after_session,
            ),
        ):
            await client._reconnect()

        assert client._session is mock_new_session

    async def test_closes_stale_ws_on_reconnect(self, client: YnisonClient) -> None:
        """Stale ws is closed before reconnect attempt."""
        stale_ws = AsyncMock()
        stale_ws.closed = False
        client._ws = stale_ws
        client._session = MagicMock()
        client._session.closed = False

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                return_value=("host", "ticket", 1),
            ),
            patch.object(client, "_connect_state", new_callable=AsyncMock),
        ):
            await client._reconnect()

        stale_ws.close.assert_awaited_once()


# ------------------------------------------------------------------
# _send() error handling
# ------------------------------------------------------------------


class TestSendErrorHandling:
    """Tests for _send() error handling and reconnect scheduling."""

    async def test_connection_error_triggers_reconnect(self, client: YnisonClient) -> None:
        """ConnectionError during send sets _connected=False, schedules reconnect."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_str = AsyncMock(side_effect=ConnectionError("broken pipe"))
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            await client._send({"test": True})
            await asyncio.sleep(0)

        assert client._connected is False
        mock_rc.assert_awaited_once()

    async def test_client_error_triggers_reconnect(self, client: YnisonClient) -> None:
        """aiohttp.ClientError during send triggers reconnect."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_str = AsyncMock(side_effect=aiohttp.ClientError("connection lost"))
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            await client._send({"test": True})
            await asyncio.sleep(0)

        assert client._connected is False
        mock_rc.assert_awaited_once()

    async def test_runtime_error_triggers_reconnect(self, client: YnisonClient) -> None:
        """RuntimeError during send triggers reconnect."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_str = AsyncMock(side_effect=RuntimeError("ws closed"))
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            await client._send({"test": True})
            await asyncio.sleep(0)

        assert client._connected is False
        mock_rc.assert_awaited_once()

    async def test_os_error_triggers_reconnect(self, client: YnisonClient) -> None:
        """OSError during send triggers reconnect."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_str = AsyncMock(side_effect=OSError("network"))
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            await client._send({"test": True})
            await asyncio.sleep(0)

        assert client._connected is False
        mock_rc.assert_awaited_once()

    async def test_send_skips_when_ws_closed(self, client: YnisonClient) -> None:
        """_send skips when ws is present but closed."""
        mock_ws = AsyncMock()
        mock_ws.closed = True
        client._ws = mock_ws
        client._connected = True

        await client._send({"test": True})
        mock_ws.send_str.assert_not_called()


# ------------------------------------------------------------------
# connect() creates session when none provided
# ------------------------------------------------------------------


class TestConnectSessionCreation:
    """Tests for connect() creating an aiohttp session."""

    async def test_connect_creates_session(self) -> None:
        """connect() creates a new session when no external session given."""
        on_state = AsyncMock()
        client = YnisonClient(
            token=SecretStr("test-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=on_state,
            logger=MagicMock(),
        )
        with (
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                return_value=("host", "ticket", 1),
            ),
            patch.object(client, "_connect_state", new_callable=AsyncMock),
        ):
            await client.connect()

        assert client._session is not None
        # Clean up
        await client.disconnect()

    async def test_disconnect_does_not_close_external_session(self) -> None:
        """disconnect() does not close an externally-provided session."""
        on_state = AsyncMock()
        ext_session = MagicMock(spec=aiohttp.ClientSession)
        ext_session.closed = False
        ext_session.close = AsyncMock()

        client = YnisonClient(
            token=SecretStr("test-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=on_state,
            logger=MagicMock(),
            http_session=ext_session,
        )
        client._session = ext_session

        await client.disconnect()

        ext_session.close.assert_not_called()


# ------------------------------------------------------------------
# Token refresh on auth failure during reconnect
# ------------------------------------------------------------------


class TestTokenRefreshOnReconnect:
    """Tests for on_auth_failure callback in _reconnect."""

    @pytest.mark.parametrize("failure_stage", ["redirect", "state"])
    async def test_initial_auth_failure_recovers_with_one_refresh(self, failure_stage: str) -> None:
        """An expired token at startup recovers through the bounded reconnect budget."""
        refresh = AsyncMock(return_value=SecretStr("new-token"))
        session = MagicMock(closed=False, close=AsyncMock())
        client = YnisonClient(
            token=SecretStr("old-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=AsyncMock(),
            logger=MagicMock(),
            http_session=session,
            on_auth_failure=refresh,
        )
        redirect = AsyncMock(return_value=("host", "ticket", 1))
        state = AsyncMock()
        failing_call = redirect if failure_stage == "redirect" else state
        success = ("host", "ticket", 1) if failure_stage == "redirect" else None
        failing_call.side_effect = [LoginFailed("expired"), LoginFailed("expired"), success]
        try:
            with (
                patch(SLEEP_PATH, new_callable=AsyncMock),
                patch.object(client, "_get_redirect_ticket", redirect),
                patch.object(client, "_connect_state", state),
            ):
                await client.connect()
                assert client._reconnect_task is not None
                await asyncio.wait_for(client._reconnect_task, timeout=1)
            refresh.assert_awaited_once()
            assert client._token == SecretStr("new-token")
            assert failing_call.await_count == 3
            session.close.assert_not_awaited()
        finally:
            await client.disconnect()

    async def test_auth_failure_triggers_token_refresh(self) -> None:
        """LoginFailed during reconnect invokes on_auth_failure callback."""
        on_state = AsyncMock()
        on_auth_failure = AsyncMock(return_value=SecretStr("new-token"))

        client = YnisonClient(
            token=SecretStr("old-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=on_state,
            logger=MagicMock(),
            on_auth_failure=on_auth_failure,
        )
        client._session = MagicMock()
        client._session.closed = False

        # First attempt: LoginFailed → refresh → second attempt: success
        attempt_count = 0

        async def redirect_side_effect() -> tuple[str, str, int]:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                raise LoginFailed("expired")
            return ("host", "ticket", 1)

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=redirect_side_effect,
            ),
            patch.object(client, "_connect_state", new_callable=AsyncMock),
        ):
            await client._reconnect()

        on_auth_failure.assert_awaited_once()
        assert client._token == SecretStr("new-token")
        client._logger.info.assert_any_call("Token refreshed, will retry with new token")

    async def test_auth_failure_no_callback(self) -> None:
        """LoginFailed without on_auth_failure keeps retrying on the same token."""
        on_state = AsyncMock()

        client = YnisonClient(
            token=SecretStr("old-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=on_state,
            logger=MagicMock(),
        )
        client._session = MagicMock()
        client._session.closed = False

        attempt_count = 0

        async def failing_redirect() -> tuple[str, str, int]:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count >= 4:
                client._stop_event.set()
            raise LoginFailed("expired")

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=failing_redirect,
            ),
        ):
            await client._reconnect()

        assert attempt_count >= 4
        assert client._token == SecretStr("old-token")

    async def test_auth_failure_callback_raises(self) -> None:
        """An unexpected callback error escapes instead of hiding a programming defect."""
        on_state = AsyncMock()
        on_auth_failure = AsyncMock(side_effect=RuntimeError("refresh failed"))

        client = YnisonClient(
            token=SecretStr("old-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=on_state,
            logger=MagicMock(),
            on_auth_failure=on_auth_failure,
        )
        client._session = MagicMock()
        client._session.closed = False

        attempt_count = 0

        async def failing_redirect() -> tuple[str, str, int]:
            nonlocal attempt_count
            attempt_count += 1
            raise LoginFailed("expired")

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(
                client,
                "_get_redirect_ticket",
                new_callable=AsyncMock,
                side_effect=failing_redirect,
            ),
            pytest.raises(RuntimeError, match="refresh failed"),
        ):
            await client._reconnect()

        assert on_auth_failure.await_count == 1
        # Token unchanged since callback always fails
        assert client._token == SecretStr("old-token")

    async def test_transient_refresh_failure_is_retried_after_backoff(self) -> None:
        """A temporary refresh outage preserves the refresh budget for a later retry."""
        on_auth_failure = AsyncMock(
            side_effect=[
                ResourceTemporarilyUnavailable("linked provider not loaded"),
                SecretStr("new-token"),
            ]
        )
        client = YnisonClient(
            token=SecretStr("old-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=AsyncMock(),
            logger=MagicMock(),
            on_auth_failure=on_auth_failure,
        )
        client._session = MagicMock(closed=False)
        redirect = AsyncMock(
            side_effect=[
                LoginFailed("expired"),
                LoginFailed("expired"),
                ("host", "ticket", 1),
            ]
        )

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock) as sleep,
            patch.object(client, "_get_redirect_ticket", redirect),
            patch.object(client, "_connect_state", new_callable=AsyncMock),
        ):
            await client._reconnect()

        assert on_auth_failure.await_count == 2
        assert sleep.await_count == 3
        assert client._token == SecretStr("new-token")

    async def test_permanent_refresh_failure_consumes_budget(self) -> None:
        """A permanent refresh rejection is attempted only once per reconnect episode."""
        on_auth_failure = AsyncMock(side_effect=LoginFailed("refresh rejected"))
        client = YnisonClient(
            token=SecretStr("old-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=AsyncMock(),
            logger=MagicMock(),
            on_auth_failure=on_auth_failure,
        )
        client._session = MagicMock(closed=False)
        attempts = 0

        async def failing_redirect() -> tuple[str, str, int]:
            nonlocal attempts
            attempts += 1
            if attempts == 4:
                client._stop_event.set()
            raise LoginFailed("expired")

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(client, "_get_redirect_ticket", side_effect=failing_redirect),
        ):
            await client._reconnect()

        assert attempts == 4
        on_auth_failure.assert_awaited_once()
        assert client._token == SecretStr("old-token")

    async def test_empty_redirect_triggers_one_token_refresh(self) -> None:
        """The live invalid-token response shares the bounded refresh budget."""
        on_auth_failure = AsyncMock(return_value=SecretStr("new-token"))
        client = YnisonClient(
            token=SecretStr("old-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=AsyncMock(),
            logger=MagicMock(),
            on_auth_failure=on_auth_failure,
        )
        client._session = MagicMock(closed=False)
        attempts = 0

        async def redirect() -> tuple[str, str, int]:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise LoginFailed("missing host or ticket")
            return "host", "ticket", 1

        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(client, "_get_redirect_ticket", side_effect=redirect),
            patch.object(client, "_connect_state", new_callable=AsyncMock),
        ):
            await client._reconnect()

        assert on_auth_failure.await_count == 1
        assert client._token == SecretStr("new-token")


class TestUpdateToken:
    """Tests for update_token method."""

    def test_update_token_replaces_stored_token(self) -> None:
        """update_token swaps the internal _token."""
        on_state = AsyncMock()
        client = YnisonClient(
            token=SecretStr("old-token"),
            device_info=YnisonDeviceInfo(device_id="d1", title="T"),
            on_state_update=on_state,
            logger=MagicMock(),
        )
        assert client._token == SecretStr("old-token")
        client.update_token(SecretStr("new-token"))
        assert client._token == SecretStr("new-token")


# ------------------------------------------------------------------
# Strict-mode delivery signalling (spec 0003)
# ------------------------------------------------------------------


class TestSendStrictMode:
    """Tests for `_send`/`update_*` strict-mode raising on transport failure."""

    async def test_send_strict_raises_ynison_send_error_when_disconnected(
        self, client: YnisonClient
    ) -> None:
        """`strict=True` on a disconnected client raises `YnisonSendError`."""
        client._ws = None
        with pytest.raises(YnisonSendError):
            await client._send({"test": True}, strict=True)

    async def test_send_strict_raises_on_client_error_and_schedules_reconnect(
        self, client: YnisonClient
    ) -> None:
        """`strict=True` with a failing send_str raises AND schedules reconnect."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_str = AsyncMock(side_effect=aiohttp.ClientError("connection lost"))
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            with pytest.raises(YnisonSendError):
                await client._send({"test": True}, strict=True)
            await asyncio.sleep(0)

        assert client._connected is False
        mock_rc.assert_awaited_once()

    async def test_send_non_strict_swallows_and_schedules_reconnect(
        self, client: YnisonClient
    ) -> None:
        """Default (`strict=False`) keeps the existing swallow-and-reconnect behaviour."""
        mock_ws = AsyncMock()
        mock_ws.closed = False
        mock_ws.send_str = AsyncMock(side_effect=aiohttp.ClientError("connection lost"))
        client._ws = mock_ws
        client._connected = True

        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            # Must NOT raise
            await client._send({"test": True})
            await asyncio.sleep(0)

        assert client._connected is False
        mock_rc.assert_awaited_once()

    async def test_update_playing_status_forwards_strict_kwarg(self, client: YnisonClient) -> None:
        """`update_playing_status(strict=True)` forwards to `_send`."""
        with patch.object(client, "_send", new_callable=AsyncMock) as mock_send:
            await client.update_playing_status(
                progress_ms=10, duration_ms=100, paused=False, strict=True
            )
        mock_send.assert_awaited_once()
        _args, kwargs = mock_send.call_args
        assert kwargs.get("strict") is True

    async def test_update_playing_status_default_strict_false(self, client: YnisonClient) -> None:
        """Default call passes `strict=False` (or omits, equivalent)."""
        with patch.object(client, "_send", new_callable=AsyncMock) as mock_send:
            await client.update_playing_status(progress_ms=10, duration_ms=100, paused=False)
        _args, kwargs = mock_send.call_args
        assert kwargs.get("strict", False) is False

    async def test_update_player_state_forwards_strict_kwarg(self, client: YnisonClient) -> None:
        """`update_player_state(strict=True)` forwards to `_send`."""
        with patch.object(client, "_send", new_callable=AsyncMock) as mock_send:
            await client.update_player_state(
                player_state={"player_queue": {}, "status": {}}, strict=True
            )
        mock_send.assert_awaited_once()
        _args, kwargs = mock_send.call_args
        assert kwargs.get("strict") is True


class TestScheduleReconnect:
    """Tests for the extracted `_schedule_reconnect` helper."""

    @pytest.mark.parametrize("cancelled", [False, True])
    async def test_success_and_cancellation_do_not_log_background_errors(
        self, client: YnisonClient, cancelled: bool
    ) -> None:
        """Normal completion and cancellation cannot be reported as reconnect defects."""
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop_errors: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            with patch.object(
                client, "_reconnect", side_effect=asyncio.CancelledError() if cancelled else None
            ):
                client._schedule_reconnect()
                task = client._reconnect_task
                assert task is not None
                await asyncio.gather(task, return_exceptions=True)
        finally:
            loop.set_exception_handler(previous_handler)
        client._logger.error.assert_not_called()
        assert not loop_errors

    async def test_unexpected_refresh_failure_is_logged_before_task_is_awaited(
        self, client: YnisonClient
    ) -> None:
        """A background refresh defect is reported immediately and remains awaitable."""
        logged = asyncio.Event()
        failure = RuntimeError("unexpected refresh defect")
        client._on_auth_failure = AsyncMock(side_effect=failure)
        client._session = MagicMock(closed=False)
        client._logger.error.side_effect = lambda *_args, **_kwargs: logged.set()
        with (
            patch(SLEEP_PATH, new_callable=AsyncMock),
            patch.object(client, "_get_redirect_ticket", side_effect=LoginFailed("expired")),
        ):
            client._schedule_reconnect()
            task = client._reconnect_task
            assert task is not None
            try:
                await asyncio.wait_for(logged.wait(), timeout=1)
                assert client._logger.error.call_args.kwargs["exc_info"][1] is failure
                with pytest.raises(RuntimeError, match="unexpected refresh defect"):
                    await task
            finally:
                await asyncio.gather(task, return_exceptions=True)

    async def test_schedule_reconnect_creates_task_when_none_alive(
        self, client: YnisonClient
    ) -> None:
        """First call creates a reconnect task."""
        assert client._reconnect_task is None
        with patch.object(client, "_reconnect", new_callable=AsyncMock):
            client._schedule_reconnect()
            task = client._reconnect_task
            assert task is not None
            await task  # let it finish so we don't leak it

    async def test_schedule_reconnect_idempotent_when_task_alive(
        self, client: YnisonClient
    ) -> None:
        """Second call while a task is alive does not create another."""
        # Use a real task that we can hold open
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_reconnect() -> None:
            started.set()
            await finish.wait()

        with patch.object(client, "_reconnect", side_effect=slow_reconnect):
            client._schedule_reconnect()
            first = client._reconnect_task
            assert first is not None
            await started.wait()

            # Try again while first is running
            client._schedule_reconnect()
            assert client._reconnect_task is first  # same task, no replacement

            finish.set()
            await first

    async def test_schedule_reconnect_noop_when_stop_event_set(self, client: YnisonClient) -> None:
        """Once the client is being torn down, no new reconnect tasks are scheduled."""
        client._stop_event.set()
        with patch.object(client, "_reconnect", new_callable=AsyncMock) as mock_rc:
            client._schedule_reconnect()
        assert client._reconnect_task is None
        mock_rc.assert_not_called()


# ------------------------------------------------------------------
# Go-away log
# ------------------------------------------------------------------


class TestGoAwayLog:
    """A go-away is explained in the log."""

    def _client(self, **kwargs: Any) -> YnisonClient:
        return YnisonClient(
            token=SecretStr("test-token"),
            device_info=YnisonDeviceInfo(device_id="test-device-id", title="T"),
            on_state_update=AsyncMock(),
            logger=MagicMock(),
            **kwargs,
        )

    async def test_go_away_log_explains_impact_and_next_attempt(self) -> None:
        """The warning names the error, the wait, the user impact, and the next attempt time."""
        client = self._client()

        await TestMessageLoop()._run_loop_with_messages(
            client,
            [_make_ws_msg(aiohttp.WSMsgType.TEXT, json.dumps(_fixture("go_away_throttle.json")))],
        )

        warnings = [call.args for call in client._logger.warning.call_args_list]
        message, *args = next(w for w in warnings if "asked to wait" in w[0])
        rendered = message % tuple(args)
        assert "500140000" in rendered
        assert "3600 s" in rendered
        assert "will not appear in the Yandex Music app" in rendered
        assert time.strftime("%Y-%m-%d", time.localtime(time.time() + 3600)) in rendered


# ------------------------------------------------------------------
# Queue commands
# ------------------------------------------------------------------


class TestQueueCommands:
    """Queue commands publish complete edits through the serialized mutation path."""

    @pytest.fixture(autouse=True)
    def _setup(self, client: YnisonClient) -> None:
        """Make this device active on a three-item queue."""
        self.mock_ws = AsyncMock()
        self.mock_ws.closed = False
        client._ws = self.mock_ws
        client._connected = True
        client._parse_state(
            {
                "active_device_id_optional": "test-device-id",
                "player_state": {
                    "player_queue": {
                        "current_playable_index": 0,
                        "playable_list": [{"playable_id": f"t{i}"} for i in range(3)],
                        "options": {"repeat_mode": "NONE"},
                        "entity_type": "VARIOUS",
                        "version": {"device_id": "peer", "version": "1", "timestamp_ms": "0"},
                    },
                    "status": {"progress_ms": "0", "duration_ms": "9000", "paused": False},
                },
            }
        )

    def _sent_queue(self) -> dict[str, Any]:
        """Return the last published player queue."""
        msg = json.loads(self.mock_ws.send_str.call_args.args[0])
        return cast("dict[str, Any]", msg["update_player_state"]["player_state"]["player_queue"])

    async def test_advance_publishes_next_item(self, client: YnisonClient) -> None:
        """A natural advance moves to the next item."""
        assert await client.advance_queue(natural=True, duration_ms=9000) == "change"
        assert self._sent_queue()["current_playable_index"] == 1

    async def test_advance_at_radio_end_does_not_publish(self, client: YnisonClient) -> None:
        """A RADIO queue end returns ``radio`` and publishes nothing."""
        client.state.player_state["player_queue"].update(
            entity_type="RADIO", current_playable_index=2
        )

        assert await client.advance_queue(natural=True, duration_ms=9000) == "radio"
        self.mock_ws.send_str.assert_not_awaited()

    async def test_advance_aborts_when_playback_changed_meanwhile(
        self, client: YnisonClient
    ) -> None:
        """A peer pause between decision and publication supersedes navigation."""
        original = client.mutate_player_state

        async def pause_then_mutate(mutation: Any, **kwargs: Any) -> None:
            client.state.player_state["status"]["paused"] = True
            await original(mutation, **kwargs)

        with (
            patch.object(client, "mutate_player_state", side_effect=pause_then_mutate),
            pytest.raises(ResourceTemporarilyUnavailable, match="changed during navigation"),
        ):
            await client.advance_queue(natural=True, duration_ms=9000)

    async def test_go_to_index_with_items_keeps_window(self, client: YnisonClient) -> None:
        """Appending items while moving keeps the 200-item window around the target."""
        queue = client.state.player_state["player_queue"]
        queue["playable_list"] = [{"playable_id": f"t{i}"} for i in range(200)]
        queue["current_playable_index"] = 199
        items = [*queue["playable_list"], {"playable_id": "n0"}]

        await client.go_to_index(200, items=items)

        sent = self._sent_queue()
        assert len(sent["playable_list"]) == 200
        assert sent["playable_list"][sent["current_playable_index"]] == {"playable_id": "n0"}

    async def test_publish_items_is_gated_on_activity(self, client: YnisonClient) -> None:
        """A best-effort list publication is skipped while another device is active."""
        client.state.active_device_id = "phone"
        items = [*client.state.player_state["player_queue"]["playable_list"], {"playable_id": "n"}]

        assert await client.publish_items(items) is None
        self.mock_ws.send_str.assert_not_awaited()

    async def test_publish_items_appends(self, client: YnisonClient) -> None:
        """An active device publishes the appended list without moving."""
        items = [*client.state.player_state["player_queue"]["playable_list"], {"playable_id": "n"}]

        assert await client.publish_items(items) == 0
        assert self._sent_queue()["playable_list"][-1] == {"playable_id": "n"}
        assert self._sent_queue()["current_playable_index"] == 0

    async def test_set_repeat_and_shuffle(self, client: YnisonClient) -> None:
        """Repeat and shuffle edits are published and preserve each other before the echo."""
        await client.set_repeat_mode(RepeatMode.ALL)
        await client.set_shuffle(enabled=True)

        sent = self._sent_queue()
        assert sent["options"]["repeat_mode"] == "ALL"
        assert sent["shuffle_optional"]["playable_indices"][0] == 0
