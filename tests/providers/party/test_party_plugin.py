"""Tests for the party plugin."""

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.enums import EventType, MediaType, PlaybackState
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.shared_playback import SharedPlaybackMode
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.party import (
    _UNSET,
    CONF_ENABLE_ADD_QUEUE,
    CONF_ENABLE_BOOST,
    CONF_ENABLE_GUEST_ACCESS,
    CONF_PARTY_MODE,
    CONF_PREVENT_DUPLICATE_TRACKS,
    PartyPlugin,
)


def _create_party_plugin() -> PartyPlugin:
    """Create a minimally configured party plugin for unit tests."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.mass.music = MagicMock()
    plugin.mass.player_queues = MagicMock()
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin.manifest = MagicMock()
    plugin.manifest.instance_id = "party_instance_1"
    plugin.config.instance_id = "party_instance_1"
    plugin._queue_lock = asyncio.Lock()
    plugin._session = None
    plugin._session_lock = asyncio.Lock()
    plugin._unregister_handles = []
    plugin._expiry_task = None
    plugin._last_pushed_url = cast("Any", _UNSET)
    plugin.get_party_player = AsyncMock(return_value="party_queue")  # type: ignore[method-assign]
    config_values = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_ENABLE_BOOST: True,
        CONF_ENABLE_ADD_QUEUE: True,
        CONF_PREVENT_DUPLICATE_TRACKS: True,
    }
    plugin.config.get_value.side_effect = config_values.get
    return plugin


@pytest.mark.asyncio
async def test_add_to_queue_rechecks_duplicates_during_priority_insert() -> None:
    """Reject a duplicate that appears after the initial queue lookup."""
    plugin = _create_party_plugin()
    player_queues = cast("MagicMock", plugin.mass.player_queues)
    music = cast("MagicMock", plugin.mass.music)
    uri = "spotify://track/123"

    queue = MagicMock()
    queue.state = PlaybackState.PLAYING
    queue.current_index = 0
    queue.index_in_buffer = 0
    player_queues.get.return_value = queue
    player_queues.items.return_value = []
    player_queues.load = AsyncMock()

    async def mutate_queue_during_resolve(_uri: str) -> MagicMock:
        media_item = MagicMock()
        media_item.media_type = MediaType.TRACK
        player_queues.items.return_value = [MagicMock(uri=uri, extra_attributes={})]
        return media_item

    music.get_item_by_uri = AsyncMock(side_effect=mutate_queue_during_resolve)
    queue_item = MagicMock()
    queue_item.extra_attributes = {}

    with (
        patch("music_assistant.providers.party.build_queue_item", return_value=queue_item),
        pytest.raises(InvalidDataError, match="already in the queue"),
    ):
        await plugin.add_to_queue(uri)

    player_queues.load.assert_not_awaited()


def _create_session_test_plugin(mode: str) -> PartyPlugin:
    """Create a party plugin with a real get_party_player for session tests."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin._session = None
    plugin._session_lock = asyncio.Lock()
    config_values = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_PARTY_MODE: mode,
    }
    plugin.config.get_value.side_effect = config_values.__getitem__
    return plugin


@pytest.mark.asyncio
async def test_get_party_player_remote_mode_returns_session_queue() -> None:
    """In remote mode the party player is the session's virtual player queue."""
    plugin = _create_session_test_plugin(SharedPlaybackMode.REMOTE.value)
    session = MagicMock()
    session.queue_id = "sendspin_virtual_party"
    plugin._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

    assert await plugin.get_party_player() == "sendspin_virtual_party"


@pytest.mark.asyncio
async def test_get_party_player_remote_mode_no_session() -> None:
    """In remote mode without a session (sendspin missing) no queue is returned."""
    plugin = _create_session_test_plugin(SharedPlaybackMode.REMOTE.value)
    plugin._get_session = AsyncMock(return_value=None)  # type: ignore[method-assign]

    assert await plugin.get_party_player() is None


@pytest.mark.asyncio
async def test_listen_in_without_session_raises() -> None:
    """Listen-in is rejected when no session is available (venue auto mode)."""
    plugin = _create_session_test_plugin(SharedPlaybackMode.VENUE.value)
    plugin._get_or_create_session_locked = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with (
        pytest.raises(InvalidDataError, match="not available"),
    ):
        await plugin.listen_in("web_player_1")


@pytest.mark.asyncio
async def test_listen_in_attaches_guest_player() -> None:
    """Listen-in attaches the guest's web player to the session."""
    plugin = _create_session_test_plugin(SharedPlaybackMode.REMOTE.value)
    session = MagicMock()
    session.queue_id = "sendspin_virtual_party"

    async def _assert_locked(_web_player_id: str) -> None:
        assert plugin._session_lock.locked()

    session.add_guest_listener = AsyncMock(side_effect=_assert_locked)
    plugin._get_or_create_session_locked = AsyncMock(return_value=session)  # type: ignore[method-assign]

    result = await plugin.listen_in("web_player_1")

    assert result == {"success": True, "queue_id": "sendspin_virtual_party"}
    session.add_guest_listener.assert_awaited_once_with("web_player_1")


@pytest.mark.parametrize("mode", [SharedPlaybackMode.VENUE.value, SharedPlaybackMode.REMOTE.value])
@pytest.mark.asyncio
async def test_get_party_config_exposes_mode(mode: str) -> None:
    """get_party_config surfaces the configured playback mode to the guest frontend."""
    plugin = _create_party_plugin()
    cast("MagicMock", plugin.config.get_value).side_effect = lambda key, *_args, **_kwargs: (
        mode if key == CONF_PARTY_MODE else True
    )

    config = await plugin.get_party_config()

    assert config.mode == mode


@pytest.mark.asyncio
async def test_guest_readable_commands_use_guest_scope() -> None:
    """party/url and party/config stay on a guest-readable scope, and loaded_in_mass subscribes to CORE_STATE_UPDATED."""
    plugin = _create_party_plugin()

    await plugin.loaded_in_mass()

    scopes = {
        call.args[0]: call.kwargs["required_scope"]
        for call in cast("MagicMock", plugin.mass.register_api_command).call_args_list
    }
    assert scopes["party/url"] == Scope.PROVIDERS_READ
    assert scopes["party/config"] == Scope.PROVIDERS_READ
    assert scopes["party/listen_in"] == Scope.PLAYERS_CONTROL
    assert scopes["party/stop_listen_in"] == Scope.PLAYERS_CONTROL
    assert scopes["party/can_listen_in"] == Scope.PLAYERS_CONTROL

    cast("MagicMock", plugin.mass.subscribe).assert_called_once_with(
        plugin._on_core_state_updated, EventType.CORE_STATE_UPDATED
    )


@pytest.mark.asyncio
async def test_push_url_update_dispatches_url_and_qr_code() -> None:
    """_push_url_update dispatches provider_event with URL and SVG QR code."""
    plugin = _create_party_plugin()
    plugin._last_pushed_url = cast("Any", _UNSET)
    plugin._expiry_task = None
    plugin.signal_provider_event = MagicMock()  # type: ignore[misc,method-assign]

    url = "https://app.music-assistant.io/?remote_id=MA-1234&join=CODE1"
    expiry = datetime(2026, 7, 22, 18, 0, 0, tzinfo=UTC)
    plugin._get_party_url_and_expiry = AsyncMock(return_value=(url, expiry))  # type: ignore[method-assign]
    plugin._schedule_join_code_expiry_timer = AsyncMock()  # type: ignore[method-assign]

    await plugin._push_url_update()

    plugin.signal_provider_event.assert_called_once()
    call_kwargs = plugin.signal_provider_event.call_args.kwargs
    assert call_kwargs["sub_scope"] == "url"
    assert call_kwargs["data"]["url"] == url
    assert "<svg" in call_kwargs["data"]["qr_code"]
    assert plugin._last_pushed_url == url


@pytest.mark.asyncio
async def test_push_url_update_deduplicates_unchanged_url() -> None:
    """_push_url_update skips signaling if the URL has not changed."""
    plugin = _create_party_plugin()
    url = "https://app.music-assistant.io/?remote_id=MA-1234&join=CODE1"
    plugin._last_pushed_url = cast("Any", url)
    plugin._expiry_task = None
    plugin.signal_provider_event = MagicMock()  # type: ignore[misc,method-assign]
    plugin._schedule_join_code_expiry_timer = AsyncMock()  # type: ignore[method-assign]

    expiry = datetime(2026, 7, 22, 18, 0, 0, tzinfo=UTC)
    plugin._get_party_url_and_expiry = AsyncMock(return_value=(url, expiry))  # type: ignore[method-assign]

    await plugin._push_url_update()

    plugin.signal_provider_event.assert_not_called()
    plugin._schedule_join_code_expiry_timer.assert_awaited_once_with(expiry)


@pytest.mark.asyncio
async def test_push_url_update_disabled_guest_access_pushes_none() -> None:
    """_push_url_update pushes None for URL and QR code when guest access is disabled."""
    plugin = _create_party_plugin()
    plugin.signal_provider_event = MagicMock()  # type: ignore[misc,method-assign]
    plugin._schedule_join_code_expiry_timer = AsyncMock()  # type: ignore[method-assign]

    plugin._get_party_url_and_expiry = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]

    await plugin._push_url_update()

    plugin.signal_provider_event.assert_called_once_with(
        data={"url": None, "qr_code": None},
        sub_scope="url",
    )
    plugin._schedule_join_code_expiry_timer.assert_awaited_once_with(None)
    assert plugin._last_pushed_url is None


@pytest.mark.asyncio
async def test_push_url_update_transitions_from_active_url_to_none() -> None:
    """_push_url_update dispatches None when guest access is disabled mid-session."""
    plugin = _create_party_plugin()
    plugin._last_pushed_url = cast(
        "Any", "https://app.music-assistant.io/?remote_id=MA-1234&join=OLDCODE"
    )
    plugin.signal_provider_event = MagicMock()  # type: ignore[misc,method-assign]
    plugin._schedule_join_code_expiry_timer = AsyncMock()  # type: ignore[method-assign]

    plugin._get_party_url_and_expiry = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]

    await plugin._push_url_update()

    plugin.signal_provider_event.assert_called_once_with(
        data={"url": None, "qr_code": None},
        sub_scope="url",
    )
    plugin._schedule_join_code_expiry_timer.assert_awaited_once_with(None)
    assert plugin._last_pushed_url is None


@pytest.mark.asyncio
async def test_on_core_state_updated_triggers_url_push() -> None:
    """_on_core_state_updated delegates to _push_url_update."""
    plugin = _create_party_plugin()
    plugin._push_url_update = AsyncMock()  # type: ignore[method-assign]

    event = MagicMock()
    await plugin._on_core_state_updated(event)

    plugin._push_url_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_code_expiry_timer_refreshes_url_and_qr_code() -> None:
    """When the active join code expires, the timer fires and pushes a refreshed URL/QR code."""
    plugin = _create_party_plugin()
    plugin.signal_provider_event = MagicMock()  # type: ignore[misc,method-assign]

    def _create_task(coro: object) -> asyncio.Task[None]:
        return asyncio.create_task(cast("Coroutine[Any, Any, None]", coro))

    plugin.mass.create_task = MagicMock(side_effect=_create_task)  # type: ignore[method-assign]

    code1_url = "https://app.music-assistant.io/?remote_id=MA-1234&join=CODE1"
    code2_url = "https://app.music-assistant.io/?remote_id=MA-1234&join=CODE2"
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
    expiry1 = now + timedelta(hours=8)
    expiry2 = now + timedelta(hours=16)

    # Initial call returns CODE1, second call (after timer expiry) returns CODE2
    plugin._get_party_url_and_expiry = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            (code1_url, expiry1),
            (code2_url, expiry2),
        ]
    )

    with (
        patch("music_assistant.providers.party.utc", return_value=now),
        patch(
            "music_assistant.providers.party.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep,
    ):
        await plugin._push_url_update()
        assert plugin._expiry_task is not None
        with suppress(asyncio.CancelledError):
            await plugin._expiry_task

    # Verify asyncio.sleep was called with 8 hours + 1s buffer (28801 seconds)
    expected_delay = (expiry1 - now).total_seconds() + 1
    mock_sleep.assert_any_await(expected_delay)

    # Verify signal_provider_event was called twice (first with CODE1, second with CODE2 after timer expiry)
    assert plugin.signal_provider_event.call_count == 2

    first_call_data = plugin.signal_provider_event.call_args_list[0].kwargs["data"]
    assert first_call_data["url"] == code1_url

    second_call_data = plugin.signal_provider_event.call_args_list[1].kwargs["data"]
    assert second_call_data["url"] == code2_url
    assert plugin._last_pushed_url == code2_url


@pytest.mark.asyncio
async def test_push_url_update_when_get_party_url_and_expiry_raises() -> None:
    """_push_url_update logs error and aborts without pushing if _get_party_url_and_expiry raises."""
    plugin = _create_party_plugin()
    plugin.signal_provider_event = MagicMock()  # type: ignore[misc,method-assign]
    plugin._get_party_url_and_expiry = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("DB query failed")
    )

    await plugin._push_url_update()

    plugin.signal_provider_event.assert_not_called()
    cast("MagicMock", plugin.logger.error).assert_called_once()
    assert plugin._last_pushed_url == _UNSET


@pytest.mark.asyncio
async def test_push_url_update_when_segno_make_raises() -> None:
    """_push_url_update pushes URL with qr_code=None if QR generation raises an exception."""
    plugin = _create_party_plugin()
    plugin.signal_provider_event = MagicMock()  # type: ignore[misc,method-assign]
    plugin._schedule_join_code_expiry_timer = AsyncMock()  # type: ignore[method-assign]

    url = "https://app.music-assistant.io/?remote_id=MA-1234&join=CODE1"
    expiry = datetime(2026, 7, 22, 18, 0, 0, tzinfo=UTC)
    plugin._get_party_url_and_expiry = AsyncMock(return_value=(url, expiry))  # type: ignore[method-assign]

    with patch(
        "music_assistant.providers.party.segno.make",
        side_effect=RuntimeError("Segno generation error"),
    ):
        await plugin._push_url_update()

    plugin.signal_provider_event.assert_called_once_with(
        data={"url": url, "qr_code": None},
        sub_scope="url",
    )
    cast("MagicMock", plugin.logger.error).assert_called_once()
    plugin._schedule_join_code_expiry_timer.assert_awaited_once_with(expiry)
    assert plugin._last_pushed_url == url


@pytest.mark.asyncio
async def test_schedule_join_code_expiry_timer_cancels_existing_task() -> None:
    """_schedule_join_code_expiry_timer cancels any existing expiry task before scheduling."""
    plugin = _create_party_plugin()
    mock_task = MagicMock(spec=asyncio.Task)
    plugin._expiry_task = mock_task

    await plugin._schedule_join_code_expiry_timer(None)

    mock_task.cancel.assert_called_once()
    assert plugin._expiry_task is None


@pytest.mark.asyncio
async def test_unload_cancels_expiry_task_and_resets_state() -> None:
    """unload() cancels active expiry task and resets _last_pushed_url to _UNSET."""
    plugin = _create_party_plugin()
    mock_task = MagicMock(spec=asyncio.Task)
    plugin._expiry_task = mock_task
    plugin._last_pushed_url = "https://app.music-assistant.io/?remote_id=MA-1234&join=CODE1"

    with patch.object(PluginProvider, "unload", new_callable=AsyncMock):
        await plugin.unload()

    mock_task.cancel.assert_called_once()
    assert plugin._expiry_task is None
    assert plugin._last_pushed_url == _UNSET  # type: ignore[unreachable]
