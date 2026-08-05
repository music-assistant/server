"""Tests for the party plugin."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.enums import ConfigEntryType, MediaType, PlaybackState
from music_assistant_models.errors import ActionUnavailable, InvalidDataError

from music_assistant.helpers.shared_playback import SharedPlaybackMode
from music_assistant.providers.party import (
    CONF_ENABLE_ADD_QUEUE,
    CONF_ENABLE_BOOST,
    CONF_ENABLE_GUEST_ACCESS,
    CONF_PARTY_DURATION,
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
    plugin._queue_lock = asyncio.Lock()
    plugin._session = None
    plugin._session_lock = asyncio.Lock()
    plugin.get_party_player = AsyncMock(return_value="party_queue")  # type: ignore[method-assign]
    config_values = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_ENABLE_BOOST: True,
        CONF_ENABLE_ADD_QUEUE: True,
        CONF_PREVENT_DUPLICATE_TRACKS: True,
        CONF_PARTY_DURATION: 8,
    }
    plugin.config.get_value.side_effect = config_values.__getitem__
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
    cast("MagicMock", plugin.config.get_value).side_effect = {CONF_PARTY_MODE: mode}.get

    config = await plugin.get_party_config()

    assert config.mode == mode


@pytest.mark.asyncio
async def test_guest_readable_commands_use_guest_scope() -> None:
    """party/url and party/config stay on a guest-readable scope, never a host-only one."""
    plugin = _create_party_plugin()
    plugin._unregister_handles = []

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


@pytest.mark.parametrize("expiry", [8, 48])
@pytest.mark.asyncio
async def test_get_party_url_passes_configured_expiry(expiry: int) -> None:
    """get_party_url passes the configured expiry through to the join code helper."""
    plugin = _create_party_plugin()
    cast("MagicMock", plugin.config.get_value).side_effect = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_PARTY_DURATION: expiry,
    }.get

    guest_user = MagicMock()
    with (
        patch(
            "music_assistant.providers.party.guest_access.get_or_create_guest_user",
            AsyncMock(return_value=guest_user),
        ),
        patch(
            "music_assistant.providers.party.guest_access.get_or_create_join_code",
            AsyncMock(return_value="abc123"),
        ) as mock_get_code,
        patch(
            "music_assistant.providers.party.guest_access.build_join_url",
            return_value="http://example/?join=abc123",
        ),
    ):
        url = await plugin.get_party_url()

    assert url == "http://example/?join=abc123"
    mock_get_code.assert_awaited_once()
    assert mock_get_code.call_args.kwargs["expires_in_hours"] == expiry


def _create_config_entries_plugin(*, guest_access_enabled: bool) -> PartyPlugin:
    """Create a party plugin for exercising get_config_entries/handle_config_action."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.mass.players.all_players.return_value = []
    plugin.config = MagicMock()
    # an empty (real) dict, so get_config_value falls through to config.get_value below
    # instead of taking the "typed entry present" branch a MagicMock would fake
    plugin.config.values = {}
    plugin.config.get_value.side_effect = {CONF_ENABLE_GUEST_ACCESS: guest_access_enabled}.get
    return plugin


@pytest.mark.parametrize("guest_access_enabled", [True, False])
@pytest.mark.asyncio
async def test_get_config_entries_guest_access_is_a_visible_toggle(
    guest_access_enabled: bool,
) -> None:
    """Guest access is a plain visible boolean; the two removed action buttons are gone."""
    plugin = _create_config_entries_plugin(guest_access_enabled=guest_access_enabled)

    entries = await plugin.get_config_entries()
    by_key = {entry.key: entry for entry in entries}

    toggle = by_key[CONF_ENABLE_GUEST_ACCESS]
    assert toggle.type == ConfigEntryType.BOOLEAN
    assert toggle.hidden is False
    assert toggle.immediate_apply is True
    assert "action_enable_guest_access" not in by_key
    assert "action_disable_guest_access" not in by_key
    # the two notes still toggle their visibility from the live guest_access_enabled value
    assert by_key["guest_disabled_note"].hidden is guest_access_enabled
    assert by_key["guest_enabled_note"].hidden is (not guest_access_enabled)


@pytest.mark.asyncio
async def test_handle_config_action_rejects_former_guest_access_actions() -> None:
    """With the buttons removed, their old action ids fall through to the base rejection."""
    plugin = _create_config_entries_plugin(guest_access_enabled=False)

    with pytest.raises(ActionUnavailable):
        await plugin.handle_config_action("action_enable_guest_access")


def _create_unload_plugin(*, guest_access_enabled: bool) -> PartyPlugin:
    """Create a party plugin wired for a bare unload() call, with no session and no handles."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.mass.config.get_raw_provider_config_value.return_value = guest_access_enabled
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin.config.instance_id = "party--test"
    plugin._unregister_handles = []
    plugin._session = None
    plugin._session_lock = asyncio.Lock()
    plugin._revoke_guest_tokens = AsyncMock()  # type: ignore[method-assign]
    return plugin


@pytest.mark.asyncio
async def test_unload_revokes_guest_tokens_when_guest_access_is_off() -> None:
    """Switching guest access off makes the ensuing unload revoke the guest tokens."""
    plugin = _create_unload_plugin(guest_access_enabled=False)

    await plugin.unload()

    cast("AsyncMock", plugin._revoke_guest_tokens).assert_awaited_once()
    # the live stored value is what decides, not the config snapshot taken at init
    cast("MagicMock", plugin.mass.config.get_raw_provider_config_value).assert_called_once_with(
        "party--test", CONF_ENABLE_GUEST_ACCESS, default=True
    )


@pytest.mark.asyncio
async def test_unload_keeps_guest_tokens_while_guest_access_is_on() -> None:
    """A plain reload with guest access still on leaves the guest tokens intact."""
    plugin = _create_unload_plugin(guest_access_enabled=True)

    await plugin.unload()

    cast("AsyncMock", plugin._revoke_guest_tokens).assert_not_awaited()


@pytest.mark.asyncio
async def test_unload_revokes_guest_tokens_on_removal() -> None:
    """Removing the plugin revokes the guest tokens even with guest access still on."""
    plugin = _create_unload_plugin(guest_access_enabled=True)

    await plugin.unload(is_removed=True)

    cast("AsyncMock", plugin._revoke_guest_tokens).assert_awaited_once()
