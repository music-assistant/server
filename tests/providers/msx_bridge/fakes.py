"""Narrow controller fakes for MSX Bridge tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock


async def empty_async_generator() -> AsyncIterator[object]:
    """Provide an empty async generator for playlist track responses."""
    if asyncio.current_task() is None:
        yield object()


class FakeMass:
    """Minimal explicit MusicAssistant surface used by provider unit tests."""

    def __init__(self, player_config: Mock) -> None:
        """Initialize known controller seams with behavioral mocks."""
        self.closing = False
        self.http_session = AsyncMock()
        self.webserver = SimpleNamespace(
            base_url="http://ma.local:8095",
            auth=SimpleNamespace(list_users=AsyncMock(return_value=[])),
        )
        self.streams = SimpleNamespace(
            base_url="http://ma.local:8097",
            audio=SimpleNamespace(get_player_output_plan=Mock()),
        )
        self.cache = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
        self.config = SimpleNamespace(
            create_default_player_config=Mock(),
            get_base_player_config=Mock(return_value=player_config),
            get_raw_player_config_value=Mock(return_value="stereo"),
            get_raw_provider_config_value=Mock(return_value=None),
            remove_provider_config_value=AsyncMock(),
            get_player_dsp_config=Mock(),
            get=Mock(return_value={}),
        )
        self.metadata = SimpleNamespace(get_image_url=Mock(return_value=None))
        self.music = SimpleNamespace(
            albums=SimpleNamespace(
                library_items=AsyncMock(return_value=[]), tracks=AsyncMock(return_value=[])
            ),
            artists=SimpleNamespace(
                library_items=AsyncMock(return_value=[]), albums=AsyncMock(return_value=[])
            ),
            playlists=SimpleNamespace(
                library_items=AsyncMock(return_value=[]),
                tracks=Mock(return_value=empty_async_generator()),
            ),
            tracks=SimpleNamespace(library_items=AsyncMock(return_value=[])),
            search=AsyncMock(
                return_value=SimpleNamespace(artists=[], albums=[], tracks=[], playlists=[])
            ),
            get_item_by_uri=AsyncMock(return_value=None),
        )
        self.player_queues = SimpleNamespace(
            play_media=AsyncMock(),
            resume=AsyncMock(),
            items=Mock(return_value=[]),
            get=Mock(return_value=None),
            get_item=Mock(return_value=None),
            get_active_queue=Mock(return_value=None),
            play_index=AsyncMock(),
        )
        self.players = SimpleNamespace(
            cmd_pause=AsyncMock(),
            cmd_play=AsyncMock(),
            cmd_stop=AsyncMock(),
            cmd_next_track=AsyncMock(),
            cmd_previous_track=AsyncMock(),
            _handle_cmd_pause=AsyncMock(),
            _handle_cmd_play=AsyncMock(),
            _handle_cmd_stop=AsyncMock(),
            _handle_play_media=AsyncMock(),
            get=Mock(return_value=None),
            get_player=Mock(return_value=None),
            get_player_lock=Mock(side_effect=self._player_lock),
            register=AsyncMock(),
            unregister=AsyncMock(),
            all=Mock(return_value=[]),
            all_players=Mock(return_value=[]),
            iter_players=Mock(return_value=[]),
        )
        self.verify_event_loop_thread = Mock()
        self.create_task = Mock()
        self.get_provider = Mock(return_value=None)

    @staticmethod
    @asynccontextmanager
    async def _player_lock(*_args: Any, **_kwargs: Any) -> Any:
        """Provide the async player lock context expected by group propagation."""
        yield
