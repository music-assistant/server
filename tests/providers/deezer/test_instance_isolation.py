"""Test that two Deezer instances do not share authentication state."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.deezer.gw_client import GWClient
from music_assistant.providers.deezer.provider import SUPPORTED_FEATURES, DeezerProvider

DEFAULT_FORMATS = [{"cipher": "BF_CBC_STRIPE", "format": "MP3_128"}]


def _provider(instance_id: str, arl: str, mass: Mock) -> DeezerProvider:
    manifest = Mock()
    manifest.domain = "deezer"
    config = Mock()
    config.instance_id = instance_id
    config.name = f"Deezer {instance_id}"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        "arl_token": arl,
    }.get(key, default)
    return DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def _init(provider: DeezerProvider) -> SimpleNamespace:
    """Init the provider with stubbed clients and report what they received."""
    sessions: list[Mock] = []

    def _new_session(*_args: object, **_kwargs: object) -> Mock:
        session = Mock(closed=False, close=AsyncMock())
        sessions.append(session)
        return session

    with (
        patch(
            "music_assistant.providers.deezer.provider.create_clientsession",
            side_effect=_new_session,
        ),
        patch("music_assistant.providers.deezer.provider.DeezerGQLClient") as gql_client,
        patch("music_assistant.providers.deezer.provider.GWClient") as gw_client,
    ):
        gql_client.return_value.get_me = AsyncMock(return_value=Mock(id="user123"))
        gw_client.return_value.setup = AsyncMock()
        await provider.handle_async_init()
        return SimpleNamespace(
            session=sessions[-1],
            gql_session=gql_client.call_args.kwargs["session"],
            gw_session=gw_client.call_args.args[0],
        )


async def test_each_instance_gets_its_own_session() -> None:
    """Both clients share the instance session, never the server-wide one."""
    mass = Mock()
    mass.config.get.return_value = {}

    first = await _init(_provider("deezer--first", "arl-one", mass))
    second = await _init(_provider("deezer--second", "arl-two", mass))

    assert first.gql_session is first.session
    assert first.gw_session is first.session
    assert first.session is not mass.http_session
    assert first.session is not second.session
    assert second.gql_session is second.session


async def test_failed_setup_closes_the_session() -> None:
    """A provider that never loads is not unloaded either, so it must clean up itself."""
    mass = Mock()
    mass.config.get.return_value = {}
    provider = _provider("deezer--first", "arl-one", mass)
    sessions: list[Mock] = []

    def _new_session(*_args: object, **_kwargs: object) -> Mock:
        session = Mock(closed=False, close=AsyncMock())
        sessions.append(session)
        return session

    with (
        patch(
            "music_assistant.providers.deezer.provider.create_clientsession",
            side_effect=_new_session,
        ),
        patch("music_assistant.providers.deezer.provider.DeezerGQLClient") as gql_client,
    ):
        gql_client.return_value.get_me = AsyncMock(return_value=None)
        with pytest.raises(LoginFailed):
            await provider.handle_async_init()
    sessions[-1].close.assert_awaited_once()


async def test_unload_closes_the_session() -> None:
    """An unloaded instance must not leave its session behind."""
    mass = Mock()
    mass.config.get.return_value = {}
    provider = _provider("deezer--first", "arl-one", mass)
    result = await _init(provider)

    with patch("music_assistant.models.music_provider.MusicProvider.unload", new=AsyncMock()):
        await provider.unload()
    result.session.close.assert_awaited_once()


def test_gw_client_formats_are_per_instance() -> None:
    """Quality rights must not leak between instances through class-level state."""
    one = GWClient(Mock(), "arl-one")
    two = GWClient(Mock(), "arl-two")

    one.formats.insert(0, {"cipher": "BF_CBC_STRIPE", "format": "FLAC"})
    assert two.formats == DEFAULT_FORMATS
    assert GWClient(Mock(), "arl-three").formats == DEFAULT_FORMATS
