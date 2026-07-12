"""Mixin Base."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from logging import Logger

    from aioaudiobookshelf.client import SocketClient, UserClient
    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.audiobookshelf.helpers import (
        LibrariesHelper,
        ProgressGuard,
        SessionHelper,
    )


class MixinBase:
    """MixinBase."""

    if TYPE_CHECKING:
        # inherited from MusicProvider class
        domain: str
        instance_id: str

        mass: MusicAssistant
        config: ProviderConfig
        logger: Logger

        # inherited from AudiobookshelfProvider class
        def _log_no_libraries(self) -> None: ...
        def _log_no_helper_item_ids(self) -> None: ...
        async def _cache_set_helper_libraries(self) -> None: ...
        def _get_all_known_item_ids(self) -> set[str]: ...
        async def reauthenticate(self) -> None: ...  # noqa: D102

        libraries: LibrariesHelper
        sessions: dict[str, SessionHelper]
        create_session_lock: asyncio.Lock
        abs_username: str
        is_token_user: bool
        progress_guard: ProgressGuard
        _client: UserClient
        _client_socket: SocketClient

        playlist_lock: asyncio.Lock
        playlist_last: float
