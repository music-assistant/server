"""Tests for how the open device-management section is rendered from the config snapshot."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from music_assistant.providers.sendspin.constants import (
    CONF_ACTION_MANAGEMENT_ENTER,
    CONF_ACTION_MANAGEMENT_EXIT,
)
from music_assistant.providers.sendspin.helpers import SecurityActionError
from music_assistant.providers.sendspin.player import SendspinBasePlayer

if TYPE_CHECKING:
    from aiosendspin.models.management import ManagementResultData

    from music_assistant.providers.sendspin.provider import SendspinProvider


def _empty_config() -> ManagementResultData:
    return cast(
        "ManagementResultData",
        SimpleNamespace(unpaired_access=None, static_pin=None, dynamic_pin=None),
    )


class _FakeProvider:
    """Provider stand-in that records management-config fetches."""

    def __init__(self, *, session: object | None, config: ManagementResultData | None) -> None:
        self._session = session
        self._config = config
        self.fetch_calls = 0
        self.exit_calls = 0

    def get_management_session(self, client_id: str) -> object | None:
        return self._session

    async def management_get_pairing_config(self, client_id: str) -> ManagementResultData:
        self.fetch_calls += 1
        if self._config is None:
            raise SecurityActionError("management_error_timeout")
        return self._config

    def exit_management(self, client_id: str) -> None:
        self.exit_calls += 1


def _player() -> SendspinBasePlayer:
    player = SendspinBasePlayer.__new__(SendspinBasePlayer)
    player._player_id = "client-1"
    return player


async def _paired(provider: _FakeProvider, snapshot: ManagementResultData | None) -> set[str]:
    entries = await _player()._paired_entries(cast("SendspinProvider", provider), snapshot)
    return {entry.key for entry in entries}


async def test_renders_from_snapshot_without_fetch() -> None:
    """An open session with a fresh snapshot renders from it, issuing no device round trip."""
    provider = _FakeProvider(session=object(), config=None)
    keys = await _paired(provider, _empty_config())
    assert provider.fetch_calls == 0
    assert CONF_ACTION_MANAGEMENT_EXIT in keys


async def test_falls_back_to_fetch_when_snapshot_empty() -> None:
    """A session open without a snapshot fetches once as a fallback."""
    provider = _FakeProvider(session=object(), config=_empty_config())
    keys = await _paired(provider, None)
    assert provider.fetch_calls == 1
    assert CONF_ACTION_MANAGEMENT_EXIT in keys


async def test_fetch_failure_exits_and_offers_enter() -> None:
    """A failed fallback fetch drops the session and falls through to the enter action."""
    provider = _FakeProvider(session=object(), config=None)
    keys = await _paired(provider, None)
    assert provider.fetch_calls == 1
    assert provider.exit_calls == 1
    assert CONF_ACTION_MANAGEMENT_ENTER in keys
    assert CONF_ACTION_MANAGEMENT_EXIT not in keys
