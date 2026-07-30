"""Tests for provider/notifier.py — StateNotifier batching and lifecycle."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

# Use mock enums from conftest
from music_assistant_models.enums import EventType, PlaybackState

from music_assistant.providers.yandex_smarthome.notifier import (
    StateNotifier,
    _CallbackErrorAlreadyLogged,
)

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent


@dataclass
class MockPlayer:
    """Mock player for notifier tests."""

    player_id: str = "p1"
    name: str = "Speaker"
    available: bool = True
    enabled: bool = True
    powered: bool | None = True
    playback_state: Any = PlaybackState.PLAYING
    volume_level: int | None = 50
    volume_muted: bool | None = False
    synced_to: str | None = None
    device_info: Any = None
    supported_features: set[str] = field(default_factory=set)
    source_list: list[str] = field(default_factory=list)
    active_source: str | None = None
    group_members: list[str] = field(default_factory=list)

    @property
    def state(self) -> MockPlayer:
        """Return self as state (mirrors real Player.state)."""
        return self


@dataclass
class MockEvent:
    """Mock event for notifier tests."""

    event: str
    data: Any = None


def _event(event: str, data: Any = None) -> MassEvent:
    """Build a MockEvent viewed through the real MassEvent type."""
    return cast("MassEvent", MockEvent(event=event, data=data))


def _make_mass(players: list[MockPlayer] | None = None) -> MagicMock:
    """Create a mock MusicAssistant."""
    mass = MagicMock()
    mass.loop = MagicMock()

    if players is None:
        players = [MockPlayer()]
    mass.players.__iter__ = MagicMock(return_value=iter(players))
    mass.players.all_players = MagicMock(return_value=players)

    # subscribe returns an unsubscribe callable
    mass.subscribe = MagicMock(return_value=MagicMock())

    # create_task returns a mock Task that can be awaited
    class _MockTask:
        """Minimal awaitable mock task for testing."""

        def __init__(self) -> None:
            self._cancelled = False
            self._done = False

        def done(self) -> bool:
            return self._done or self._cancelled

        def cancel(self) -> bool:
            self._cancelled = True
            return True

        def __await__(self):  # type: ignore[no-untyped-def]
            if self._cancelled:
                raise asyncio.CancelledError
            yield

    mass.create_task = MagicMock(return_value=_MockTask())

    return mass


def _make_notifier(
    mass: MagicMock | None = None,
    session: MagicMock | None = None,
) -> StateNotifier:
    """Create a StateNotifier with mocks."""
    if mass is None:
        mass = _make_mass()
    if session is None:
        session = MagicMock(spec=aiohttp.ClientSession)

    return StateNotifier(
        mass=mass,
        session=session,
        user_id="test_user",
        callback_url="https://example.com/callback/state",
        auth_header={"Authorization": "Bearer test-token"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStateNotifierLifecycle:
    """Tests for StateNotifier lifecycle methods."""

    @pytest.mark.asyncio
    async def test_start_subscribes(self) -> None:
        """Test start subscribes."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)

        await notifier.start()

        mass.subscribe.assert_called_once()
        assert notifier._unsub is not None
        assert notifier._heartbeat_task is not None

        await notifier.stop()

    @pytest.mark.asyncio
    async def test_stop_unsubscribes(self) -> None:
        """Test stop unsubscribes."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)

        await notifier.start()
        unsub = cast("MagicMock", notifier._unsub)
        await notifier.stop()

        unsub.assert_called_once()
        assert notifier._unsub is None
        assert notifier._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_stop_without_start(self) -> None:
        """Test stop without start."""
        notifier = _make_notifier()
        # Should not raise
        await notifier.stop()


class TestStateNotifierEvents:
    """Tests for StateNotifier event handling."""

    def test_on_player_updated_queues_state(self) -> None:
        """Test on player updated marks player as dirty."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)

        player = MockPlayer(player_id="p1", playback_state=PlaybackState.PLAYING)
        event = _event(event=EventType.PLAYER_UPDATED, data=player)

        notifier._on_player_event(event)

        assert "p1" in notifier._dirty_player_ids

    def test_on_player_updated_unavailable_ignored(self) -> None:
        """Test on player updated unavailable ignored."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)

        player = MockPlayer(player_id="p1", available=False)
        event = _event(event=EventType.PLAYER_UPDATED, data=player)

        notifier._on_player_event(event)

        assert "p1" not in notifier._dirty_player_ids

    def test_on_player_added_triggers_discovery(self) -> None:
        """Test on player added triggers discovery."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)

        event = _event(event=EventType.PLAYER_ADDED, data=MockPlayer())
        notifier._on_player_event(event)

        # Discovery triggers create_task
        mass.create_task.assert_called()

    def test_on_player_removed_triggers_discovery(self) -> None:
        """Test on player removed triggers discovery."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)

        event = _event(event=EventType.PLAYER_REMOVED, data="p1")
        notifier._on_player_event(event)

        mass.create_task.assert_called()

    def test_on_none_data_ignored(self) -> None:
        """Test on none data ignored."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)

        event = _event(event=EventType.PLAYER_UPDATED, data=None)
        notifier._on_player_event(event)

        assert len(notifier._dirty_player_ids) == 0

    def test_on_player_filtered_by_exposed_ids(self) -> None:
        """Player not in exposed_ids should be ignored."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)
        notifier._exposed_ids = {"p2", "p3"}

        player = MockPlayer(player_id="p1", playback_state=PlaybackState.PLAYING)
        event = _event(event=EventType.PLAYER_UPDATED, data=player)
        notifier._on_player_event(event)

        assert "p1" not in notifier._dirty_player_ids

    def test_on_player_included_by_exposed_ids(self) -> None:
        """Player in exposed_ids should be marked dirty."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)
        notifier._exposed_ids = {"p1", "p2"}

        player = MockPlayer(player_id="p1", playback_state=PlaybackState.PLAYING)
        event = _event(event=EventType.PLAYER_UPDATED, data=player)
        notifier._on_player_event(event)

        assert "p1" in notifier._dirty_player_ids

    def test_child_event_propagates_to_group(self) -> None:
        """When a synced child fires PLAYER_UPDATED, the parent group is marked dirty."""
        mass = _make_mass()
        notifier = _make_notifier(mass=mass)

        child = MockPlayer(player_id="child1", synced_to="grp1")
        event = _event(event=EventType.PLAYER_UPDATED, data=child)
        notifier._on_player_event(event)

        assert "grp1" in notifier._dirty_player_ids
        assert "child1" not in notifier._dirty_player_ids


class TestStateNotifierFlush:
    """Tests for StateNotifier flush mechanism."""

    @pytest.mark.asyncio
    async def test_flush_sends_callback(self) -> None:
        """Test flush reads fresh state and sends callback."""
        mock_resp = AsyncMock()
        mock_resp.status = 200

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        player = MockPlayer(player_id="p1", volume_level=75)
        mass = _make_mass([player])
        mass.players.get_player = MagicMock(return_value=player)
        notifier = _make_notifier(mass=mass, session=session)

        notifier._dirty_player_ids.add("p1")

        await notifier._flush_pending()

        session.post.assert_called_once()
        assert len(notifier._dirty_player_ids) == 0

    @pytest.mark.asyncio
    async def test_flush_empty_noop(self) -> None:
        """Test flush empty noop."""
        session = MagicMock(spec=aiohttp.ClientSession)
        mass = _make_mass()
        notifier = _make_notifier(mass=mass, session=session)

        await notifier._flush_pending()

        session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_reads_fresh_volume(self) -> None:
        """Volume should be read at flush time, not at event time."""
        mock_resp = AsyncMock()
        mock_resp.status = 200

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        # Event arrives with volume 0 (transient state)
        player_event = MockPlayer(player_id="p1", volume_level=0)
        # But by flush time, player has correct volume
        player_live = MockPlayer(player_id="p1", volume_level=75)

        mass = _make_mass([player_live])
        mass.players.get_player = MagicMock(return_value=player_live)
        notifier = _make_notifier(mass=mass, session=session)

        # Simulate event with transient volume=0
        event = _event(event=EventType.PLAYER_UPDATED, data=player_event)
        notifier._on_player_event(event)

        # Flush should use live player state (volume=75)
        await notifier._flush_pending()

        call_kwargs = session.post.call_args
        json_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        devices = json_body["payload"]["devices"]
        volume_cap = next(
            c for c in devices[0]["capabilities"] if c["state"]["instance"] == "volume"
        )
        assert volume_cap["state"]["value"] == 75


class TestStateNotifierReportAll:
    """Tests for StateNotifier report-all-states."""

    @pytest.mark.asyncio
    async def test_report_all_states(self) -> None:
        """Test report all states."""
        mock_resp = AsyncMock()
        mock_resp.status = 200

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        players = [MockPlayer(player_id="p1"), MockPlayer(player_id="p2")]
        mass = _make_mass(players)
        notifier = _make_notifier(mass=mass, session=session)

        await notifier._report_all_states()

        session.post.assert_called_once()
        # Verify the payload contains both devices
        call_kwargs = session.post.call_args
        json_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert len(json_body["payload"]["devices"]) == 2

    @pytest.mark.asyncio
    async def test_report_all_no_players(self) -> None:
        """Test report all no players."""
        session = MagicMock(spec=aiohttp.ClientSession)
        mass = _make_mass([])
        notifier = _make_notifier(mass=mass, session=session)

        await notifier._report_all_states()

        session.post.assert_not_called()


class TestStateNotifierCloudPlus:
    """Tests specific to Cloud Plus (Yandex Dialogs API) behaviour."""

    @pytest.mark.asyncio
    async def test_accepts_http_202(self) -> None:
        """Yandex Dialogs returns 202 on successful callback — should not warn."""
        mock_resp = AsyncMock()
        mock_resp.status = 202

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        mass = _make_mass()
        notifier = StateNotifier(
            mass=mass,
            session=session,
            user_id="cloud-instance-id",
            callback_url="https://dialogs.yandex.net/api/v1/skills/test-uuid/callback/state",
            auth_header={"Authorization": "OAuth test-oauth-token"},
        )

        player = MockPlayer(player_id="p1")
        mass.players.get_player = MagicMock(return_value=player)
        notifier._dirty_player_ids.add("p1")

        await notifier._flush_pending()

        session.post.assert_called_once()
        call_kwargs = session.post.call_args
        assert "dialogs.yandex.net" in call_kwargs[0][0]
        assert call_kwargs.kwargs["headers"]["Authorization"] == "OAuth test-oauth-token"

    @pytest.mark.asyncio
    async def test_rejects_http_500(self) -> None:
        """HTTP 500: re-queue dirty IDs, swallow exception (already logged)."""
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal Server Error")

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        player = MockPlayer(player_id="p1")
        mass = _make_mass([player])
        mass.players.get_player = MagicMock(return_value=player)
        notifier = _make_notifier(mass=mass, session=session)

        notifier._dirty_player_ids.add("p1")

        # No raise — _send_state_callback's _CallbackErrorAlreadyLogged is
        # deduped + swallowed here so MA's task scheduler does not re-log
        # it as "Task exception was never retrieved" on every retry.
        await notifier._flush_pending()

        session.post.assert_called_once()
        assert "p1" in notifier._dirty_player_ids

    @pytest.mark.asyncio
    async def test_http_5xx_dedupe_warns_once(self, caplog: pytest.LogCaptureFixture) -> None:
        """Repeated HTTP 5xx logs WARNING once, then DEBUG on retries."""
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal Server Error")

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        player = MockPlayer(player_id="p1")
        mass = _make_mass([player])
        mass.players.get_player = MagicMock(return_value=player)
        notifier = _make_notifier(mass=mass, session=session)

        caplog.set_level(logging.DEBUG, logger=notifier._logger.name)

        # First failure → WARNING
        with pytest.raises(_CallbackErrorAlreadyLogged):
            await notifier._send_state_callback(
                [MagicMock()]  # devices payload — content irrelevant for this test
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "HTTP 500" in warnings[0].message

        # Second failure with same fingerprint → DEBUG, no new WARNING
        caplog.clear()
        with pytest.raises(_CallbackErrorAlreadyLogged):
            await notifier._send_state_callback([MagicMock()])
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert warnings == []
        assert any("still failing" in r.message for r in debugs)

    @pytest.mark.asyncio
    async def test_recovery_logs_info_and_clears_fingerprint(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A successful callback after a failure logs INFO and resets state."""
        # Pre-set the fingerprint to simulate prior failure
        mass = _make_mass()
        session = MagicMock(spec=aiohttp.ClientSession)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        notifier = _make_notifier(mass=mass, session=session)
        notifier._last_error_fingerprint = "http_500"

        caplog.set_level(logging.INFO, logger=notifier._logger.name)
        await notifier._send_state_callback([MagicMock()])

        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert any("recovered" in r.message for r in infos)
        assert notifier._last_error_fingerprint is None

    @pytest.mark.asyncio
    async def test_discovery_url_cloud_plus(self) -> None:
        """Discovery URL should use replace('/state', '/discovery') for Dialogs API."""
        mock_resp = AsyncMock()
        mock_resp.status = 202

        session = MagicMock(spec=aiohttp.ClientSession)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = ctx

        mass = _make_mass()
        notifier = StateNotifier(
            mass=mass,
            session=session,
            user_id="cloud-instance-id",
            callback_url="https://dialogs.yandex.net/api/v1/skills/test-uuid/callback/state",
            auth_header={"Authorization": "OAuth test-token"},
        )

        await notifier._send_discovery()

        session.post.assert_called_once()
        url = session.post.call_args[0][0]
        assert url == "https://dialogs.yandex.net/api/v1/skills/test-uuid/callback/discovery"
