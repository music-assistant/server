"""Tests for the Home Assistant provider setup flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from music_assistant.controllers.webserver.helpers.login_flow import (
    PKCE_VERIFIER_RE,
    verify_pkce,
)
from music_assistant.providers.hass import setup_flow

HASS_URL = "http://homeassistant.local:8123"


def _make_session() -> MagicMock:
    """Build a setup session stub that returns a valid authorization callback."""
    session = MagicMock()
    session.flow_id = "flow-test"
    session.callback_url = "http://ma.local:8095/setup/callback/flow-test"
    session.external = AsyncMock(return_value={"state": "flow-test", "code": "auth-code"})
    return session


async def _authenticate(session: MagicMock) -> tuple[str, str]:
    """Run the OAuth step and return the opened auth URL and the code verifier sent."""
    get_token = AsyncMock(return_value={"access_token": "access"})
    with (
        patch.object(setup_flow, "get_token", get_token),
        patch.object(setup_flow, "get_long_lived_token", AsyncMock(return_value="long-lived")),
    ):
        assert await setup_flow._authenticate(session, HASS_URL) == "long-lived"
    assert get_token.await_args is not None
    return session.external.await_args.args[0], get_token.await_args.kwargs["code_verifier"]


async def test_authenticate_sends_matching_pkce_pair() -> None:
    """The token request carries the verifier of the challenge sent in the auth URL."""
    auth_url, code_verifier = await _authenticate(_make_session())

    query = parse_qs(urlparse(auth_url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert PKCE_VERIFIER_RE.fullmatch(code_verifier)
    assert verify_pkce(code_verifier, query["code_challenge"][0])


async def test_authenticate_uses_new_verifier_per_attempt() -> None:
    """Each setup attempt generates its own PKCE verifier."""
    _, first_verifier = await _authenticate(_make_session())
    _, second_verifier = await _authenticate(_make_session())

    assert first_verifier != second_verifier
