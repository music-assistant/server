"""Tests for the Zvuk Music setup flow and setup-backed token."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ConfigEntryType

from music_assistant.controllers.config.migrations import migrate_provider_setup_data
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.zvuk_music.constants import CONF_QUALITY, CONF_TOKEN
from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider
from music_assistant.providers.zvuk_music.setup_flow import run_setup


def _make_session(*, setup_data: dict[str, Any] | None = None) -> Any:
    """Create a minimal setup session mock."""
    session = Mock()
    session.context = SimpleNamespace(setup_data=setup_data or {})
    session.form = AsyncMock()
    session.finish = AsyncMock()
    return session


class _ImageResponse:
    """Provide the async response protocol used by image resolution."""

    status = 200

    async def read(self) -> bytes:
        """Return deterministic image content."""
        return b"image"

    async def __aenter__(self) -> Self:
        """Enter the response context."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit the response context."""


@pytest.mark.asyncio
async def test_run_setup_finishes_with_submitted_token() -> None:
    """The setup flow persists the submitted secure token."""
    session = _make_session()
    session.form.return_value = {CONF_TOKEN: "fresh-token"}

    await run_setup(session)

    entries = session.form.await_args.args[0]
    assert [entry.key for entry in entries][-1] == CONF_TOKEN
    assert entries[-1].type == ConfigEntryType.SECURE_STRING
    session.finish.assert_awaited_once_with({CONF_TOKEN: "fresh-token"})


@pytest.mark.asyncio
async def test_run_setup_retries_with_preserved_token_after_validation_error() -> None:
    """A rejected token stays visible while the setup form reports its error."""
    session = _make_session()
    session.form.side_effect = [
        {CONF_TOKEN: "rejected-token"},
        {CONF_TOKEN: "replacement-token"},
    ]
    session.finish.side_effect = [
        SetupFlowError("Rejected", translation_key="login_failed"),
        None,
    ]

    await run_setup(session)

    second_entries = session.form.await_args_list[1].args[0]
    assert second_entries[-1].value == "rejected-token"
    assert session.form.await_args_list[1].kwargs["errors"] == {"base": "login_failed"}
    assert session.finish.await_args_list[-1].args[0] == {CONF_TOKEN: "replacement-token"}


@pytest.mark.asyncio
async def test_provider_options_expose_quality_but_not_token() -> None:
    """Authentication stays out of ordinary provider options."""
    provider = Mock(spec=ZvukMusicProvider)
    provider.get_config_entries = ZvukMusicProvider.get_config_entries.__get__(
        provider, ZvukMusicProvider
    )

    entries = await provider.get_config_entries()

    keys = {entry.key for entry in entries}
    assert CONF_QUALITY in keys
    assert CONF_TOKEN not in keys


@pytest.mark.asyncio
async def test_provider_initialization_uses_setup_token() -> None:
    """Provider initialization connects with the setup-backed token."""
    provider = Mock(spec=ZvukMusicProvider)
    provider.get_setup_value = Mock(return_value="setup-token")
    provider.logger = Mock()
    provider.handle_async_init = ZvukMusicProvider.handle_async_init.__get__(
        provider, ZvukMusicProvider
    )
    client = Mock()
    client.connect = AsyncMock()

    with patch(
        "music_assistant.providers.zvuk_music.provider.ZvukMusicClient", return_value=client
    ) as client_cls:
        await provider.handle_async_init()

    provider.get_setup_value.assert_called_once_with(CONF_TOKEN)
    client_cls.assert_called_once_with("setup-token")
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_initialization_uses_migrated_legacy_token() -> None:
    """Existing installations migrate their config token into setup data."""
    instance_id = "zvuk_music--legacy"
    settings = {
        "providers": {
            instance_id: {
                "domain": "zvuk_music",
                "values": {CONF_TOKEN: "legacy-token"},
            }
        }
    }
    assert migrate_provider_setup_data(settings, lambda value: f"encrypted:{value}")
    migrated_config = settings["providers"][instance_id]
    assert migrated_config["values"] == {}
    assert migrated_config["setup_data"] == {CONF_TOKEN: "encrypted:legacy-token"}

    provider = Mock(spec=ZvukMusicProvider)
    provider.instance_id = instance_id
    provider.mass = Mock()
    provider.mass.config.get.return_value = migrated_config["setup_data"]
    provider.mass.config.decrypt_string.return_value = "legacy-token"
    provider.get_setup_value = MusicProvider.get_setup_value.__get__(provider, ZvukMusicProvider)
    provider.logger = Mock()
    provider.handle_async_init = ZvukMusicProvider.handle_async_init.__get__(
        provider, ZvukMusicProvider
    )
    client = Mock()
    client.connect = AsyncMock()

    with patch(
        "music_assistant.providers.zvuk_music.provider.ZvukMusicClient", return_value=client
    ) as client_cls:
        await provider.handle_async_init()

    provider.mass.config.get.assert_called_once()
    provider.mass.config.decrypt_string.assert_called_once_with("encrypted:legacy-token")
    client_cls.assert_called_once_with("legacy-token")
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_image_resolution_uses_setup_token() -> None:
    """Authenticated image requests use the setup-backed token."""
    provider = Mock(spec=ZvukMusicProvider)
    provider.get_setup_value = Mock(return_value="setup-token")
    provider.mass = Mock()
    provider.mass.http_session.get = Mock(return_value=_ImageResponse())
    provider.logger = Mock()
    provider.resolve_image = ZvukMusicProvider.resolve_image.__get__(provider, ZvukMusicProvider)

    result = await provider.resolve_image("https://zvuk.com/cover.jpg")

    assert result == b"image"
    provider.get_setup_value.assert_called_once_with(CONF_TOKEN)
    headers = provider.mass.http_session.get.call_args.kwargs["headers"]
    assert headers["X-Auth-Token"] == "setup-token"


def test_strings_define_setup_flow_and_token_specific_login_error() -> None:
    """Provider strings describe setup and name the rejected token."""
    strings_path = Path(run_setup.__code__.co_filename).with_name("strings.json")
    strings = json.loads(strings_path.read_text())

    assert strings["setup_flow"]["user"]["title"] == "Connect to Zvuk"
    assert "X-Auth-Token" in strings["errors"]["login_failed"]
    assert "clear_auth" not in strings["config_entries"]
