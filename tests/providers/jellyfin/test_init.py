"""Tests for the Jellyfin provider."""

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import pytest
from aiohttp import ClientSession
from aiojellyfin.session import SessionConfiguration
from aiojellyfin.testing import FixtureBuilder

from music_assistant.mass import MusicAssistant
from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.jellyfin import (
    CONF_ACCESS_TOKEN,
    CONF_PASSWORD,
    CONF_USER_ID,
    CONF_USERNAME,
    authenticate_with_quick_connect,
    initiate_quick_connect,
)
from music_assistant.providers.jellyfin import setup_flow
from tests.common import get_fixtures_dir, wait_for_sync_completion

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


@pytest.fixture
async def jellyfin_provider(mass: MusicAssistant) -> AsyncGenerator[ProviderConfig]:
    """Configure an aiojellyfin test fixture, and add a provider to mass that uses it."""
    f = FixtureBuilder()
    async for _, artist in get_fixtures_dir("artists", "jellyfin"):
        f.add_json_bytes(artist)

    async for _, album in get_fixtures_dir("albums", "jellyfin"):
        f.add_json_bytes(album)

    async for _, track in get_fixtures_dir("tracks", "jellyfin"):
        f.add_json_bytes(track)

    authenticate_by_name = f.to_authenticate_by_name()

    with mock.patch(
        "music_assistant.providers.jellyfin.authenticate_by_name", authenticate_by_name
    ):
        async with wait_for_sync_completion(mass):
            config = await mass.config._create_provider_instance(
                "jellyfin",
                {},
                # connection details are collected by the setup flow and live in setup_data
                setup_data=mass.config._encrypt_values(
                    {
                        "url": "http://localhost",
                        "username": "username",
                        "password": "password",
                    }
                ),
            )
            await mass.music.start_sync()

        yield config


@pytest.mark.usefixtures("jellyfin_provider")
async def test_get_artist_albums(mass: MusicAssistant) -> None:
    """Test that get_artist_albums returns albums for a real artist ID."""
    artists = await mass.music.artists.library_items(search="Ash", summary=False)
    ash = artists[0]
    prov_mapping = next(m for m in ash.provider_mappings if m.provider_domain == "jellyfin")
    albums = await mass.music.artists.get_provider_artist_albums(
        prov_mapping.item_id, prov_mapping.provider_instance
    )
    assert any(album.name == "Nu-Clear Sounds" for album in albums)


@pytest.mark.usefixtures("jellyfin_provider")
async def test_initial_sync(mass: MusicAssistant) -> None:
    """Test that initial sync worked."""
    artists = await mass.music.artists.library_items(search="Ash")
    assert artists[0].name == "Ash"

    albums = await mass.music.albums.library_items(search="christmas")
    assert albums[0].name == "This Is Christmas"

    tracks = await mass.music.tracks.library_items(search="where the bands are")
    assert tracks[0].name == "Where the Bands Are"
    assert tracks[0].version == "2018 Version"


async def test_quick_connect_authentication() -> None:
    """Test Jellyfin Quick Connect exchanges an approved secret for a token."""
    http_session = mock.MagicMock(spec=ClientSession)

    def response_context(payload: dict[str, object]) -> mock.MagicMock:
        response = mock.MagicMock()
        response.json = mock.AsyncMock(return_value=payload)
        context = mock.MagicMock()
        context.__aenter__ = mock.AsyncMock(return_value=response)
        context.__aexit__ = mock.AsyncMock(return_value=None)
        return context

    http_session.post.side_effect = [
        response_context({"Secret": "secret", "Code": "123456"}),
        response_context({"User": {"Id": "user-id"}, "AccessToken": "access-token"}),
    ]
    http_session.get.side_effect = [
        response_context({"Authenticated": False}),
        response_context({"Authenticated": True}),
    ]
    session_config = SessionConfiguration(
        session=cast("ClientSession", http_session),
        url="https://jellyfin.example",
        app_name="Music Assistant",
        app_version="1.0",
        device_name="test device",
        device_id="test-device-id",
    )

    secret, code = await initiate_quick_connect(session_config)
    with mock.patch("music_assistant.providers.jellyfin.asyncio.sleep", mock.AsyncMock()):
        client = await authenticate_with_quick_connect(session_config, secret)

    assert code == "123456"
    assert client._user_id == "user-id"
    assert client._access_token == "access-token"
    assert http_session.get.call_count == 2
    assert http_session.post.call_args_list[1].kwargs["json"] == {"Secret": "secret"}


def _setup_flow_session(
    setup_data: dict[str, Any], form_values: list[dict[str, Any]], collected: dict[str, Any]
) -> SetupSession:
    """Create a setup-flow session with deterministic form responses."""
    mass = mock.MagicMock()
    mass.server_id = "server-id"
    mass.version = "1.0"
    mass.http_session = mock.MagicMock()
    mass.http_session_no_ssl = mock.MagicMock()
    form = mock.AsyncMock(side_effect=form_values)

    async def finish(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "jellyfin--1"}

    session = SetupSession(
        mass,
        "flow-test",
        SetupFlowContext(kind="setup", reason="user", domain="jellyfin", setup_data=setup_data),
        finish,
    )
    session.form = form  # type: ignore[method-assign]
    return session


async def test_password_setup_clears_quick_connect_credentials() -> None:
    """Switching to password authentication removes stale token credentials."""
    collected: dict[str, Any] = {}
    session = _setup_flow_session(
        {
            CONF_ACCESS_TOKEN: "old-token",
            CONF_USER_ID: "old-user",
        },
        [
            {"auth_method": "password"},
            {
                "url": "https://jellyfin.example",
                CONF_USERNAME: "user",
                CONF_PASSWORD: "password",
                "verify_ssl": True,
            },
        ],
        collected,
    )

    await setup_flow.run_setup(session)

    assert collected == {
        "url": "https://jellyfin.example",
        CONF_USERNAME: "user",
        CONF_PASSWORD: "password",
        "verify_ssl": True,
    }


async def test_quick_connect_setup_clears_password_credentials() -> None:
    """Quick Connect setup stores its token without stale password credentials."""
    collected: dict[str, Any] = {}
    session = _setup_flow_session(
        {
            CONF_USERNAME: "old-user",
            CONF_PASSWORD: "old-password",
        },
        [
            {"auth_method": "quick_connect"},
            {"url": "https://jellyfin.example", "verify_ssl": True},
        ],
        collected,
    )
    client = SimpleNamespace(_access_token="new-token", _user_id="new-user")
    captured: dict[str, Any] = {}

    async def external_until(awaitable: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return await awaitable

    session.external_until = external_until  # type: ignore[method-assign]
    with (
        mock.patch.object(
            setup_flow,
            "initiate_quick_connect",
            new=mock.AsyncMock(return_value=("secret", "123456")),
        ),
        mock.patch.object(
            setup_flow,
            "authenticate_with_quick_connect",
            new=mock.AsyncMock(return_value=client),
        ),
    ):
        await setup_flow.run_setup(session)

    assert captured["copy_text"] == "123456"
    assert collected == {
        "url": "https://jellyfin.example",
        "verify_ssl": True,
        CONF_ACCESS_TOKEN: "new-token",
        CONF_USER_ID: "new-user",
        "device_id": "server-id",
    }
