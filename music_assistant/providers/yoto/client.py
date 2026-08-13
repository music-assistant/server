"""Yoto API boundary for the Yoto provider."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from music_assistant_models.errors import LoginFailed, ProviderUnavailableError
from yoto_api import YotoClient

from .catalogue import Catalogue, decode_track_id

TokenCallback = Callable[[str], None | Awaitable[None]]


class YotoClientProtocol(Protocol):
    """Subset of yoto-api 4.3.4 used by the provider."""

    token: Any
    library: dict[str, Any]
    groups: dict[str, Any]

    def set_refresh_token(self, refresh_token: str) -> None:
        """Set the refresh token."""
        ...

    async def check_and_refresh_token(self) -> Any:
        """Return a usable token, refreshing when needed."""
        ...

    async def update_library(self) -> None:
        """Refresh library card metadata."""
        ...

    async def update_card_detail(self, card_id: str) -> None:
        """Refresh one card's details."""
        ...

    async def update_groups(self) -> None:
        """Refresh library groups."""
        ...


@dataclass(frozen=True, slots=True)
class ResolvedStream:
    """Fresh stream metadata with its signed path hidden from representations."""

    path: str = field(repr=False)
    duration: int = 0
    format: str | None = None


class YotoAdapter:
    """Secret-safe, serialized adapter around yoto-api 4.3.4."""

    def __init__(
        self,
        client_id: str,
        refresh_token: str | None = None,
        *,
        api: YotoClientProtocol | None = None,
        token_callback: TokenCallback | None = None,
        session: Any = None,
    ) -> None:
        """
        Initialize the adapter.

        :param client_id: Yoto OAuth client ID.
        :param refresh_token: Persisted rotating refresh token.
        :param api: Optional yoto-api-compatible client.
        :param token_callback: Callback used to persist token rotation.
        :param session: Optional shared HTTP session.
        """
        if not client_id.strip():
            msg = "A Yoto client ID is required"
            raise LoginFailed(msg)
        self._api = api or YotoClient(client_id=client_id, session=session)
        self._token_callback = token_callback
        self._refresh_token = refresh_token
        self._lock = asyncio.Lock()
        if refresh_token:
            self._api.set_refresh_token(refresh_token)

    def __repr__(self) -> str:
        """Return a representation without credentials."""
        return f"{type(self).__name__}(authenticated={bool(self._refresh_token)})"

    async def ensure_authenticated(self) -> None:
        """Refresh access and persist a rotated refresh token."""
        async with self._lock:
            await self._ensure_authenticated()

    async def resolve_stream(self, item_id: str) -> ResolvedStream:
        """Refetch one card and return its current HTTPS stream."""
        try:
            card_id, chapter_key, track_key = decode_track_id(item_id)
        except ValueError as err:
            msg = "Invalid Yoto track identifier"
            raise ProviderUnavailableError(msg) from err
        async with self._lock:
            await self._ensure_authenticated()
            try:
                track = self._api.library[card_id].chapters[chapter_key].tracks[track_key]
                track.trackUrl = None
                await self._api.update_card_detail(card_id)
                track = self._api.library[card_id].chapters[chapter_key].tracks[track_key]
                path = track.trackUrl
                if not isinstance(path, str) or not path.startswith("https://"):
                    msg = "Yoto stream is unavailable"
                    raise ProviderUnavailableError(msg)
                return ResolvedStream(
                    path=path,
                    duration=track.duration or 0,
                    format=track.format,
                )
            except ProviderUnavailableError:
                raise
            except Exception as err:
                msg = "Yoto stream is unavailable"
                raise ProviderUnavailableError(msg) from err

    async def refresh_catalogue(self) -> Catalogue:
        """Fetch cards, details, and groups into a URL-free snapshot."""
        async with self._lock:
            await self._ensure_authenticated()
            try:
                self._api.library.clear()
                await self._api.update_library()
                for card_id in tuple(self._api.library):
                    await self._api.update_card_detail(card_id)
                await self._api.update_groups()
                return Catalogue.from_yoto_models(self._api.library, self._api.groups)
            except Exception as err:
                msg = "Unable to refresh the Yoto library"
                raise ProviderUnavailableError(msg) from err

    async def _ensure_authenticated(self) -> None:
        if not self._refresh_token:
            msg = "Yoto is not authenticated"
            raise LoginFailed(msg)
        try:
            token = await self._api.check_and_refresh_token()
            refresh_token = getattr(token, "refresh_token", None)
            if isinstance(refresh_token, str) and refresh_token != self._refresh_token:
                await self._persist_token(refresh_token)
        except LoginFailed:
            raise
        except Exception as err:
            msg = "Yoto authentication failed"
            raise LoginFailed(msg) from err

    async def _persist_token(self, refresh_token: str) -> None:
        if self._token_callback is not None:
            result = self._token_callback(refresh_token)
            if inspect.isawaitable(result):
                await result
        self._refresh_token = refresh_token
