"""Tests for the core players setting that keeps web browser players out."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import UserRole

from music_assistant.controllers.dashboard.controller import DASHBOARD_VIEWER_USERNAME
from music_assistant.providers.sendspin.provider import SendspinProvider

from .test_pin_session import _FakeMass

if TYPE_CHECKING:
    from aiosendspin.server import SendspinServer
    from aiosendspin.server.client import SendspinClient

    from music_assistant.mass import MusicAssistant


def _client(client_id: str, product_name: str = "Web Player") -> SendspinClient:
    return cast(
        "SendspinClient",
        SimpleNamespace(
            client_id=client_id,
            info_or_none=SimpleNamespace(device_info=SimpleNamespace(product_name=product_name)),
        ),
    )


def _make_provider(
    *,
    allowed: bool,
    clients: list[SendspinClient] | None = None,
    registered: set[str] | None = None,
    session_users: dict[str, list[tuple[UserRole, str]]] | None = None,
) -> SendspinProvider:
    registered = registered or set()
    session_users = session_users or {}
    mass = _FakeMass(asyncio.get_running_loop())
    mass.players = SimpleNamespace(  # type: ignore[attr-defined]
        get_config_value=lambda *_args, **_kwargs: allowed,
        get_player=lambda player_id: object() if player_id in registered else None,
    )
    mass.webserver = SimpleNamespace(  # type: ignore[attr-defined]
        get_sendspin_player_users=lambda player_id: [
            SimpleNamespace(role=role, username=username)
            for role, username in session_users.get(player_id, [])
        ]
    )
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast("MusicAssistant", mass)
    provider.server_api = cast("SendspinServer", SimpleNamespace(connected_clients=clients or []))
    provider.logger = logging.getLogger("test.sendspin.browser_players")
    provider._client_event_versions = {}
    provider._client_event_task_counts = {}
    return provider


def _record_handlers(provider: SendspinProvider) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def _recorder(kind: str) -> Any:
        async def _handler(client_id: str, _event_version: int) -> None:
            calls.append((kind, client_id))

        return _handler

    provider._handle_client_added = _recorder("added")  # type: ignore[method-assign]
    provider._handle_client_removed = _recorder("removed")  # type: ignore[method-assign]
    return calls


async def test_a_browser_player_is_kept_out_when_not_allowed() -> None:
    """A signed-in user's browser must not show up as a player once the setting is off."""
    provider = _make_provider(allowed=False, session_users={"c1": [(UserRole.USER, "gav")]})

    assert provider._is_blocked_browser_client(_client("c1"))


async def test_a_browser_player_is_let_in_when_allowed() -> None:
    """The default keeps today's behaviour for every browser."""
    provider = _make_provider(allowed=True, session_users={"c1": [(UserRole.USER, "gav")]})

    assert not provider._is_blocked_browser_client(_client("c1"))


async def test_a_guest_browser_player_is_let_in_when_not_allowed() -> None:
    """Party and music quiz guests hear the music through their browser, so they stay."""
    provider = _make_provider(
        allowed=False, session_users={"c1": [(UserRole.GUEST, "party_guest")]}
    )

    assert not provider._is_blocked_browser_client(_client("c1"))


async def test_a_dashboard_screen_is_kept_out_when_not_allowed() -> None:
    """A dashboard screen shares the guest role but is a household device, so it is blocked."""
    provider = _make_provider(
        allowed=False, session_users={"c1": [(UserRole.GUEST, DASHBOARD_VIEWER_USERNAME)]}
    )

    assert provider._is_blocked_browser_client(_client("c1"))


async def test_the_phone_app_is_not_affected() -> None:
    """The Music Assistant app was connected on purpose, it is not a browser player."""
    provider = _make_provider(allowed=False)

    assert not provider._is_blocked_browser_client(_client("c1", "Mobile Application"))


async def test_turning_the_setting_off_removes_connected_browser_players() -> None:
    """A registered browser player leaves straight away, other players stay."""
    clients = [_client("browser"), _client("guest"), _client("app", "Mobile Application")]
    provider = _make_provider(
        allowed=False,
        clients=clients,
        registered={"browser", "guest", "app"},
        session_users={"guest": [(UserRole.GUEST, "music_quiz_guest")]},
    )
    calls = _record_handlers(provider)

    provider.apply_browser_players_setting()
    await asyncio.sleep(0)

    assert calls == [("removed", "browser")]


async def test_turning_the_setting_on_adds_connected_browser_players() -> None:
    """A browser kept out while it was off is added back without reconnecting."""
    clients = [_client("browser"), _client("present"), _client("app", "Mobile Application")]
    provider = _make_provider(allowed=True, clients=clients, registered={"present"})
    calls = _record_handlers(provider)

    provider.apply_browser_players_setting()
    await asyncio.sleep(0)

    assert calls == [("added", "browser")]
