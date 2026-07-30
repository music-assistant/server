# mypy: disable-error-code="attr-defined,unreachable"
"""Tests for the Ynison WebSocket client."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.errors import LoginFailed
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
            raise RuntimeError(msg)

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

        with pytest.raises(RuntimeError, match="closed"):
            await client.connect()


# ------------------------------------------------------------------
# connect() transient error → reconnect
# ------------------------------------------------------------------


class TestConnectTransientError:
    """Tests for connect() scheduling reconnect on transient errors."""

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


# ------------------------------------------------------------------
# _get_redirect_ticket
# ------------------------------------------------------------------


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
        """Missing host/ticket in response raises ConnectionError."""
        mock_msg = MagicMock()
        mock_msg.type = aiohttp.WSMsgType.TEXT
        mock_msg.data = json.dumps({"host": "", "redirect_ticket": ""})

        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(return_value=mock_msg)
        mock_ws.close = AsyncMock()

        mock_session = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)
        client._session = mock_session

        with pytest.raises(ConnectionError, match="missing host or ticket"):
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

    async def test_text_message_invalid_json(self, client: YnisonClient) -> None:
        """TEXT message with invalid JSON logs warning, continues."""
        bad_msg = _make_ws_msg(aiohttp.WSMsgType.TEXT, "not valid json{{{")
        valid_msg = _make_ws_msg(
            aiohttp.WSMsgType.TEXT,
            json.dumps({"player_state": {"status": {"paused": True}}}),
        )
        await self._run_loop_with_messages(client, [bad_msg, valid_msg])

        client._logger.warning.assert_called()

    async def test_callback_exception_continues(
        self,
        client: YnisonClient,
        mock_state_callback: AsyncMock,
    ) -> None:
        """Exception in state callback is caught, loop continues."""
        on_state_update = mock_state_callback
        on_state_update.side_effect = [ValueError("boom"), None]

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
            raise RuntimeError(msg)

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
        """on_auth_failure raises → logs warning, keeps retrying until stopped."""
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
        stop_after = 6

        async def failing_redirect() -> tuple[str, str, int]:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count >= stop_after:
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

        # Callback was called on every attempt — no cap
        assert on_auth_failure.await_count == attempt_count
        # Token unchanged since callback always fails
        assert client._token == SecretStr("old-token")


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
