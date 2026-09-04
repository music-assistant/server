"""Test that two Deezer instances do not share authentication state."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from aiohttp import CookieJar
from yarl import URL

from music_assistant.providers.deezer.gw_client import GW_LIGHT_URL, GWClient
from music_assistant.providers.deezer.provider import SUPPORTED_FEATURES, DeezerProvider

DEFAULT_FORMATS = [{"cipher": "BF_CBC_STRIPE", "format": "MP3_128"}]


def _response(**cookies: str) -> Mock:
    """Build a response carrying the given Set-Cookie values."""
    return Mock(cookies={name: Mock(value=value) for name, value in cookies.items()})


def _session(**jar_cookies: str) -> Mock:
    """Build a stand-in for the shared session, its jar holding the given cookies."""
    jar = CookieJar()
    if jar_cookies:
        jar.update_cookies(jar_cookies, URL("https://www.deezer.com"))
    return Mock(cookie_jar=jar)


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


async def test_clients_use_the_shared_session() -> None:
    """Both clients run on the server-wide session, no provider owned one."""
    mass = Mock()
    mass.config.get.return_value = {}
    provider = _provider("deezer--first", "arl-one", mass)

    with (
        patch("music_assistant.providers.deezer.provider.DeezerGQLClient") as gql_client,
        patch("music_assistant.providers.deezer.provider.GWClient") as gw_client,
    ):
        gql_client.return_value.get_me = AsyncMock(return_value=Mock(id="user123"))
        gw_client.return_value.setup = AsyncMock()
        await provider.handle_async_init()

    assert gql_client.call_args.kwargs["session"] is mass.http_session
    assert gw_client.call_args.args[0] is mass.http_session


async def test_arl_is_sent_per_request() -> None:
    """The arl travels with the request, it is never left in the shared jar."""
    client = GWClient(_session(), "arl-one")

    assert client._request_cookies(GW_LIGHT_URL)["arl"] == "arl-one"


async def test_every_foreign_jar_cookie_is_blanked() -> None:
    """Deezer ties its bot detection tokens to the session, so the sid alone is not enough."""
    session = _session(sid="their-session", dzr_uniq_id="their-visitor", _abck="their-token")
    client = GWClient(session, "arl-one")

    cookies = client._request_cookies(GW_LIGHT_URL)

    assert cookies["sid"] == ""
    assert cookies["dzr_uniq_id"] == ""
    assert cookies["_abck"] == ""
    assert cookies["arl"] == "arl-one"


async def test_an_empty_jar_stays_empty() -> None:
    """Nothing to override means nothing to send, exactly as a session of our own would."""
    client = GWClient(_session(), "arl-one")

    assert client._request_cookies(GW_LIGHT_URL) == {"arl": "arl-one"}


async def test_own_cookies_win_over_the_blanked_jar() -> None:
    """Once deezer answered us, our own session id must survive the blanking."""
    client = GWClient(_session(sid="their-session"), "arl-one")

    client._store_cookies(_response(sid="our-own-session"))
    assert client._request_cookies(GW_LIGHT_URL)["sid"] == "our-own-session"


async def test_session_cookies_are_kept_per_instance() -> None:
    """Two clients on one session must not see each other's sid."""
    session = _session()
    one = GWClient(session, "arl-one")
    two = GWClient(session, "arl-two")

    one._store_cookies(_response(sid="session-one"))

    assert one._request_cookies(GW_LIGHT_URL)["sid"] == "session-one"
    assert "sid" not in two._request_cookies(GW_LIGHT_URL)


def test_gw_client_formats_are_per_instance() -> None:
    """Quality rights must not leak between instances through class-level state."""
    one = GWClient(Mock(), "arl-one")
    two = GWClient(Mock(), "arl-two")

    one.formats.insert(0, {"cipher": "BF_CBC_STRIPE", "format": "FLAC"})
    assert two.formats == DEFAULT_FORMATS
    assert GWClient(Mock(), "arl-three").formats == DEFAULT_FORMATS
