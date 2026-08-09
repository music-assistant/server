"""
Tests for SendspinPlayer auto-resume after reconnect logic.

Covers:
- _was_playing flag lifecycle (set in play_media, NOT reset in stop)
- _on_group_stopped guard (does not cancel session when _was_playing=True)
- _refresh_client_info triggers auto-resume when _was_playing=True
- _auto_resume_after_reconnect calls resume() then resets _was_playing
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from music_assistant.providers.sendspin.player import (
    SendspinBasePlayer,
    SendspinPlayer,
)


def _make_player(provider: MagicMock, was_playing: bool = False) -> SendspinPlayer:
    """Create a SendspinPlayer with minimal mocking, optionally setting _was_playing."""
    with (
        patch.object(SendspinBasePlayer, "_refresh_client_info"),
        patch.object(SendspinBasePlayer, "_subscribe_client_callbacks"),
        patch(
            "music_assistant.models.player.Player.synced_to",
            new_callable=PropertyMock,
            return_value=None,
        ),
    ):
        player = SendspinPlayer(provider, "test_iphone")
    player._was_playing = was_playing
    return player


@pytest.fixture
def mock_provider() -> MagicMock:
    """Create a minimal mock provider for testing."""
    provider = MagicMock()
    provider.instance_id = "sendspin"
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.players = MagicMock()
    provider.mass.player_queues = MagicMock()
    provider.mass.streams = MagicMock()
    provider.server_api = MagicMock()
    return provider


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a minimal mock Sendspin client."""
    client = MagicMock()
    client.client_id = "test_iphone"
    client.info = MagicMock()
    client.info.device_info = MagicMock()
    client.info.device_info.product_name = "Mobile Application"
    client.info.player_support = None
    return client


# ─────────────────────────────────────────────
# _was_playing lifecycle
# ─────────────────────────────────────────────


class TestWasPlayingFlag:
    """Verify _was_playing lifecycle."""

    def test_init_is_false(self, mock_provider: MagicMock) -> None:
        """_was_playing starts False."""
        player = _make_player(mock_provider)
        assert player._was_playing is False

    async def test_play_media_sets_true(self, mock_provider: MagicMock) -> None:
        """play_media() sets _was_playing = True."""
        player = _make_player(mock_provider)
        with (
            patch.object(player.playback_session, "start", new_callable=AsyncMock),
            patch.object(player.playback_session, "cancel", new_callable=AsyncMock),
        ):
            media = MagicMock()
            media.uri = "spotify://track/test"
            await player.play_media(media)
        assert player._was_playing is True

    async def test_stop_does_not_reset_flag(self, mock_provider: MagicMock) -> None:
        """
        stop() must NOT reset _was_playing (reconnect-induced ControllerStopEvent).

        This is the critical fix: _was_playing survives stop() so that
        _on_group_stopped and _refresh_client_info still see it as True
        during the reconnect window.
        """
        player = _make_player(mock_provider, was_playing=True)
        with patch.object(player.playback_session, "cancel", new_callable=AsyncMock):
            await player.stop()
        assert player._was_playing is True, (
            "stop() must NOT reset _was_playing — the flag must survive "
            "ControllerStopEvent so the reconnect guard still works"
        )


# ─────────────────────────────────────────────
# _on_group_stopped guard
# ─────────────────────────────────────────────


class TestOnGroupStoppedGuard:
    """Verify _on_group_stopped does not cancel session during reconnect."""

    def test_guard_protects_when_was_playing(self, mock_provider: MagicMock) -> None:
        """When _was_playing=True and synced_to=None, session is NOT cancelled."""
        player = _make_player(mock_provider, was_playing=True)
        with patch.object(player.playback_session, "cancel", new_callable=AsyncMock) as mock_cancel:
            player._on_group_stopped()
            mock_cancel.assert_not_called()

    def test_guard_cancels_when_not_playing(self, mock_provider: MagicMock) -> None:
        """When _was_playing=False and synced_to=None, session IS cancelled."""
        player = _make_player(mock_provider, was_playing=False)
        with patch.object(player.playback_session, "cancel", new_callable=AsyncMock) as mock_cancel:
            player._on_group_stopped()
            mock_cancel.assert_called_once()

    def test_guard_protects_when_synced_to_leader(self, mock_provider: MagicMock) -> None:
        """
        When _was_playing=True and synced_to is a leader, guard still protects.

        The condition is: not self._was_playing → False when _was_playing=True,
        regardless of synced_to state.
        """
        player = _make_player(mock_provider, was_playing=False)
        player._was_playing = True  # override
        # synced_to is mocked to return None by _make_player, override it
        with (
            patch(
                "music_assistant.models.player.Player.synced_to",
                new_callable=PropertyMock,
                return_value="leader_id",
            ),
            patch.object(player.playback_session, "cancel", new_callable=AsyncMock) as mock_cancel,
        ):
            player._on_group_stopped()
            mock_cancel.assert_not_called()


# ─────────────────────────────────────────────
# _refresh_client_info → auto-resume trigger
# ─────────────────────────────────────────────


class TestRefreshClientInfoTriggersAutoResume:
    """Verify _refresh_client_info creates the auto-resume task when appropriate."""

    def test_triggers_when_was_playing(
        self, mock_provider: MagicMock, mock_client: MagicMock
    ) -> None:
        """_refresh_client_info schedules auto-resume when _was_playing=True, synced_to=None."""
        player = _make_player(mock_provider, was_playing=True)
        player.is_web_player = True
        with patch.object(player.mass, "create_task") as mock_create_task:
            player._refresh_client_info(mock_client)
            mock_create_task.assert_called_once()
            task_cb = mock_create_task.call_args[0][0]
            assert task_cb.__name__ == "_auto_resume_after_reconnect"

    def test_skips_when_not_playing(self, mock_provider: MagicMock, mock_client: MagicMock) -> None:
        """_refresh_client_info does NOT schedule auto-resume when _was_playing=False."""
        player = _make_player(mock_provider, was_playing=False)
        with patch.object(player.mass, "create_task") as mock_create_task:
            player._refresh_client_info(mock_client)
            mock_create_task.assert_not_called()

    def test_skips_when_synced(self, mock_provider: MagicMock, mock_client: MagicMock) -> None:
        """_refresh_client_info does NOT schedule auto-resume when player is synced to a group."""
        player = _make_player(mock_provider, was_playing=True)
        with (
            patch(
                "music_assistant.models.player.Player.synced_to",
                new_callable=PropertyMock,
                return_value="group_leader_id",
            ),
            patch.object(player.mass, "create_task") as mock_create_task,
        ):
            player._refresh_client_info(mock_client)
            mock_create_task.assert_not_called()


# ─────────────────────────────────────────────
# _auto_resume_after_reconnect
# ─────────────────────────────────────────────


class TestAutoResumeAfterReconnect:
    """Verify _auto_resume_after_reconnect calls resume then resets flag."""

    async def test_resume_called_then_flag_reset(self, mock_provider: MagicMock) -> None:
        """resume() is called on the queue, then _was_playing is reset to False."""
        player = _make_player(mock_provider, was_playing=True)

        mock_queue = MagicMock()
        mock_queue.queue_id = "test_iphone"
        mock_provider.mass.player_queues.get_active_queue.return_value = mock_queue
        mock_provider.mass.player_queues.resume = AsyncMock()

        await player._auto_resume_after_reconnect()
        mock_provider.mass.player_queues.resume.assert_called_once_with("test_iphone")
        assert player._was_playing is False

    async def test_no_queue_no_crash_flag_reset(self, mock_provider: MagicMock) -> None:
        """If there's no active queue, _auto_resume_after_reconnect just resets the flag."""
        player = _make_player(mock_provider, was_playing=True)
        mock_provider.mass.player_queues.get_active_queue.return_value = None

        await player._auto_resume_after_reconnect()
        assert player._was_playing is False

    async def test_resume_failure_handled(self, mock_provider: MagicMock) -> None:
        """If resume() raises, the exception is logged and flag is still reset."""
        player = _make_player(mock_provider, was_playing=True)

        mock_queue = MagicMock()
        mock_queue.queue_id = "test_iphone"
        mock_provider.mass.player_queues.get_active_queue.return_value = mock_queue
        mock_provider.mass.player_queues.resume = AsyncMock(side_effect=Exception("boom"))

        await player._auto_resume_after_reconnect()
        assert player._was_playing is False


# ─────────────────────────────────────────────
# Integration flow: full reconnect simulation
# ─────────────────────────────────────────────


class TestReconnectFlowIntegration:
    """
    Full reconnect flow simulation.

    Traces the exact event sequence that happens during a real reconnect
    (3 min drop: disconnect → group stop → controller stop → reconnect).
    """

    async def _setup_player(self, provider: MagicMock) -> SendspinPlayer:
        return _make_player(provider)

    async def test_long_drop_reconnect_flow(
        self, mock_provider: MagicMock, mock_client: MagicMock
    ) -> None:
        """
        Simulate: play → WS drop (~3 min) → reconnect.

        Expected: auto-resume fires, resume() called, session NOT cancelled.
        """
        player = await self._setup_player(mock_provider)

        # Step 1: User plays music
        with (
            patch.object(player.playback_session, "start", new_callable=AsyncMock),
            patch.object(player.playback_session, "cancel", new_callable=AsyncMock),
        ):
            media = MagicMock()
            media.uri = "spotify://track/test"
            await player.play_media(media)
        assert player._was_playing is True

        # Step 2: WS disconnects → GroupStateChangedEvent(STOPPED)
        # Guard must NOT cancel session
        with patch.object(player.playback_session, "cancel", new_callable=AsyncMock) as mock_cancel:
            player._on_group_stopped()
            mock_cancel.assert_not_called()

        # Step 3: ControllerStopEvent (iPhone sends stop on reconnect)
        # MUST NOT reset _was_playing
        with patch.object(player.playback_session, "cancel", new_callable=AsyncMock):
            await player.stop()
        assert player._was_playing is True

        # Step 4: _refresh_client_info (triggered by ClientConnectedEvent)
        mock_queue = MagicMock()
        mock_queue.queue_id = "test_iphone"
        mock_provider.mass.player_queues.get_active_queue.return_value = mock_queue
        mock_provider.mass.player_queues.resume = AsyncMock()

        with patch.object(player.mass, "create_task") as mock_create_task:
            player._refresh_client_info(mock_client)
            assert mock_create_task.called

        # Step 5: Execute auto-resume
        await player._auto_resume_after_reconnect()
        mock_provider.mass.player_queues.resume.assert_called_once_with("test_iphone")
        assert player._was_playing is False

    async def test_short_drop_reconnect_flow(
        self, mock_provider: MagicMock, mock_client: MagicMock
    ) -> None:
        """
        Simulate: play → WS drop (<30s) → reconnect BEFORE group stop.

        No group stop fires, no ControllerStopEvent. Auto-resume still works.
        """
        player = await self._setup_player(mock_provider)

        # Play music
        with (
            patch.object(player.playback_session, "start", new_callable=AsyncMock),
            patch.object(player.playback_session, "cancel", new_callable=AsyncMock),
        ):
            media = MagicMock()
            media.uri = "spotify://track/test"
            await player.play_media(media)
        assert player._was_playing is True

        # Quick reconnect (no events)
        mock_queue = MagicMock()
        mock_queue.queue_id = "test_iphone"
        mock_provider.mass.player_queues.get_active_queue.return_value = mock_queue
        mock_provider.mass.player_queues.resume = AsyncMock()

        player._refresh_client_info(mock_client)
        await player._auto_resume_after_reconnect()
        mock_provider.mass.player_queues.resume.assert_called_once_with("test_iphone")
        assert player._was_playing is False
