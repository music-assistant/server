"""Tests for syncing a Plex library as soon as Plex reports that it changed."""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Coroutine
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import requests
from music_assistant_models.background_task import BackgroundTask
from music_assistant_models.enums import EventType

from music_assistant.providers.plex import PlexProvider
from music_assistant.providers.plex.constants import CONF_SYNC_ON_LIBRARY_CHANGE
from music_assistant.providers.plex.helpers import SUPPORTED_FEATURES

# plexapi casts the section key to an int, while the xml attribute is a string
SECTION_KEY = 3
SCAN_FINISHED = {
    "NotificationContainer": {
        "type": "activity",
        "ActivityNotification": [
            {"event": "ended", "Activity": {"type": "library.update.section"}}
        ],
    }
}


class _FakeSocket:
    """Notification socket stub that delivers the given messages and is then cancelled."""

    def __init__(self, messages: list[aiohttp.WSMessage]) -> None:
        self.messages = messages

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def __aiter__(self) -> AsyncIterator[aiohttp.WSMessage]:
        for message in self.messages:
            yield message
        raise asyncio.CancelledError


def _text_message(payload: dict[str, Any]) -> aiohttp.WSMessage:
    """Build a websocket text message carrying the given payload."""
    return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(payload), None)


def _sections(content_changed_at: str) -> ET.Element:
    """Build a /library/sections response holding this library and one other."""
    root = ET.Element("MediaContainer")
    ET.SubElement(root, "Directory", {"key": "1", "contentChangedAt": "111"})
    ET.SubElement(
        root, "Directory", {"key": str(SECTION_KEY), "contentChangedAt": content_changed_at}
    )
    return root


def _make_provider(sync_on_library_change: bool = True, content_changed_at: str = "100") -> Any:
    """
    Create a PlexProvider connected to a stubbed server.

    :param sync_on_library_change: Value of the sync on library change option.
    :param content_changed_at: Content version the stubbed server reports for the library.
    """
    mock_mass = MagicMock()
    mock_mass.music.start_sync = AsyncMock()
    mock_mass.music.active_sync_tasks = []
    mock_config = MagicMock()
    mock_config.instance_id = "plex_instance_1"
    config_values = {
        "library_type": "music",
        "log_level": "INFO",
        CONF_SYNC_ON_LIBRARY_CHANGE: sync_on_library_change,
    }
    mock_config.get_value = lambda key: config_values.get(key)
    setup_data = {"library_type": "music", "token": "local_auth", "local_server_verify_cert": True}
    mock_mass.config.get = lambda key, default=None: (
        setup_data if str(key).endswith("/setup_data") else default
    )
    mock_mass.config.get_raw_provider_config_value = lambda _instance_id, _key: None
    mock_mass.config.decrypt_string = lambda value: value
    mock_manifest = MagicMock()
    mock_manifest.type = "music"
    mock_manifest.domain = "plex"

    provider = PlexProvider(mock_mass, mock_manifest, mock_config, SUPPORTED_FEATURES)
    provider._baseurl = "https://192.168.1.10:32400"
    provider._plex_library = MagicMock()
    provider._plex_library.key = SECTION_KEY
    provider._plex_server = MagicMock()
    provider._plex_server.query = MagicMock(return_value=_sections(content_changed_at))
    provider._plex_server._headers = MagicMock(return_value={"X-Plex-Token": "secret"})
    return provider


def _close(coro: Coroutine[Any, Any, Any]) -> MagicMock:
    """Stand in for mass.create_task without running the coroutine."""
    coro.close()
    return MagicMock()


def _sync_task(provider_instance: str) -> BackgroundTask:
    """Build an active library sync task of the given provider instance."""
    return BackgroundTask(
        name="Sync tracks",
        metadata={"task_domain": "music_sync", "provider_instance": provider_instance},
    )


async def test_watcher_is_not_started_by_default() -> None:
    """Holding a socket open to the Plex server is opt-in."""
    provider = _make_provider(sync_on_library_change=False)
    provider.mass.create_task = MagicMock(side_effect=_close)

    await provider.loaded_in_mass()

    provider.mass.create_task.assert_not_called()
    provider.mass.subscribe.assert_not_called()


async def test_watcher_is_started_and_stopped_with_the_provider() -> None:
    """The watcher runs while the provider is loaded and is cancelled on unload."""
    provider = _make_provider()
    provider.mass.create_task = MagicMock(side_effect=_close)
    unsubscribe = MagicMock()
    provider.mass.subscribe = MagicMock(return_value=unsubscribe)

    await provider.loaded_in_mass()
    task = provider._notification_task
    await provider.unload()

    provider.mass.create_task.assert_called_once()
    provider.mass.subscribe.assert_called_once_with(
        provider._on_music_sync_completed, EventType.MUSIC_SYNC_COMPLETED
    )
    assert provider._content_changed_at == "100"
    task.cancel.assert_called_once()
    unsubscribe.assert_called_once()


async def test_finished_scan_starts_a_sync() -> None:
    """A finished scan that changed this library starts a sync of this provider."""
    provider = _make_provider(content_changed_at="101")
    provider._content_changed_at = "100"
    socket = _FakeSocket(
        [
            aiohttp.WSMessage(aiohttp.WSMsgType.PING, b"", None),
            _text_message({"NotificationContainer": {"type": "playing"}}),
            _text_message(SCAN_FINISHED),
        ]
    )
    provider.mass.http_session.ws_connect = MagicMock(return_value=socket)

    with pytest.raises(asyncio.CancelledError):
        await provider._watch_library_changes()

    provider.mass.music.start_sync.assert_awaited_once_with(providers=["plex_instance_1"])
    assert provider._content_changed_at == "101"


async def test_token_is_sent_as_a_header_and_not_in_the_url() -> None:
    """Connection errors include the url, so a token in it would end up in the log."""
    provider = _make_provider()
    provider.mass.http_session.ws_connect = MagicMock(return_value=_FakeSocket([]))

    with pytest.raises(asyncio.CancelledError):
        await provider._watch_library_changes()

    call = provider.mass.http_session.ws_connect.call_args
    assert call.args[0] == "wss://192.168.1.10:32400/:/websockets/notifications"
    assert call.kwargs["headers"] == {"X-Plex-Token": "secret"}
    assert call.kwargs["ssl"] is True


async def test_scan_that_left_this_library_unchanged_does_not_sync() -> None:
    """Plex scans every library and reports the scan without saying which one it was."""
    provider = _make_provider(content_changed_at="100")
    provider._content_changed_at = "100"

    await provider._sync_if_library_changed()

    provider.mass.music.start_sync.assert_not_awaited()


async def test_sync_starts_when_the_content_version_cannot_be_read() -> None:
    """Without a version to compare against, a finished scan still starts a sync."""
    provider = _make_provider()
    provider._content_changed_at = "100"
    provider._plex_server.query = MagicMock(side_effect=requests.ConnectionError("unreachable"))

    await provider._sync_if_library_changed()

    provider.mass.music.start_sync.assert_awaited_once()


async def test_change_during_a_sync_of_this_provider_is_synced_once_it_ends() -> None:
    """A sync that is already queued or running ignores a new request, so the change waits."""
    provider = _make_provider(content_changed_at="101")
    provider._content_changed_at = "100"
    provider.mass.music.active_sync_tasks = [_sync_task("plex_instance_1")]

    await provider._sync_if_library_changed()

    provider.mass.music.start_sync.assert_not_awaited()
    assert provider._content_changed_at == "100"

    provider.mass.music.active_sync_tasks = []
    await provider._on_music_sync_completed(MagicMock())

    provider.mass.music.start_sync.assert_awaited_once_with(providers=["plex_instance_1"])
    assert provider._content_changed_at == "101"


async def test_sync_of_another_provider_does_not_hold_back_the_sync() -> None:
    """Only a sync of this provider would ignore the request."""
    provider = _make_provider(content_changed_at="101")
    provider._content_changed_at = "100"
    provider.mass.music.active_sync_tasks = [_sync_task("other_instance")]

    await provider._sync_if_library_changed()

    provider.mass.music.start_sync.assert_awaited_once_with(providers=["plex_instance_1"])


async def test_completed_sync_without_a_reported_change_does_not_check_the_library() -> None:
    """Every sync ends with this event, and an unreachable server would otherwise sync forever."""
    provider = _make_provider()
    provider._plex_server.query = MagicMock(side_effect=requests.ConnectionError("unreachable"))

    await provider._on_music_sync_completed(MagicMock())

    provider._plex_server.query.assert_not_called()
    provider.mass.music.start_sync.assert_not_awaited()
