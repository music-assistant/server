"""Tests for the operator language hint the Sendspin server announces to clients."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiosendspin.noise.trust_store import FileServerPairingStore

import music_assistant.providers.sendspin.provider as provider_module
from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant


@pytest.mark.parametrize(
    ("locale", "expected"),
    [("nl_NL", ("nl-NL", "nl")), ("en", ("en",))],
)
async def test_server_announces_the_metadata_locale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, locale: str, expected: tuple[str, ...]
) -> None:
    """The server is created with the operator's languages, most preferred first."""
    server_cls = MagicMock()
    monkeypatch.setattr(provider_module, "SendspinServer", server_cls)
    monkeypatch.setattr(
        provider_module, "get_or_create_server_identity", lambda _storage_dir: object()
    )
    monkeypatch.setattr(FileServerPairingStore, "open", AsyncMock())
    monkeypatch.setattr(provider_module, "_evict_stale_pairings", AsyncMock(return_value=(0, 0)))
    provider = SendspinProvider.__new__(SendspinProvider)
    mass: Any = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.metadata = SimpleNamespace(locale=locale)
    mass.get_provider.return_value = None
    provider.mass = cast("MusicAssistant", mass)
    provider.config = MagicMock()
    provider.config.get_value.return_value = True
    provider.logger = logging.getLogger("test.sendspin.languages")

    await provider.handle_async_init()

    assert server_cls.call_args.kwargs["languages"] == expected
