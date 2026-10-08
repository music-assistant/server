"""Test Deezer authentication failures and handshake retries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import CookieJar
from deezer_python_gql import GraphQLClientAuthError, GraphQLClientError
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.deezer.gw_client import (
    DeezerGWAuthError,
    DeezerGWError,
    DeezerGWNoSubscriptionError,
    GWClient,
)
from music_assistant.providers.deezer.provider import SUPPORTED_FEATURES, DeezerProvider

STRINGS = Path(__file__).parents[3] / "music_assistant/providers/deezer/strings.json"


def _provider(mass: Mock) -> DeezerProvider:
    manifest = Mock()
    manifest.domain = "deezer"
    config = Mock()
    config.instance_id = "deezer--test"
    config.name = "Deezer test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
        "arl_token": "arl-one",
    }.get(key, default)
    return DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def _init_with(error: Exception) -> LoginFailed:
    """Initialize the provider with a failure in the corresponding client."""
    mass = Mock()
    mass.config.get.return_value = {}
    provider = _provider(mass)
    with (
        patch("music_assistant.providers.deezer.provider.DeezerGQLClient") as gql_client,
        patch("music_assistant.providers.deezer.provider.GWClient") as gw_client,
    ):
        gql_client.return_value.get_me = AsyncMock(return_value=Mock(id="user123"))
        gw_client.return_value.setup = AsyncMock()
        if isinstance(error, GraphQLClientError):
            gql_client.return_value.get_me.side_effect = error
        else:
            gw_client.return_value.setup.side_effect = error
        with pytest.raises(LoginFailed) as raised:
            await provider.handle_async_init()
    return raised.value


@pytest.mark.parametrize("user_id", [0, "0", ""])
async def test_missing_user_raises_auth_error(
    user_id: str | int, gw_user_data: dict[str, Any]
) -> None:
    """Anonymous responses must fail after one retry."""
    client = GWClient(Mock(), "arl-one")
    gw_user_data["results"]["USER"]["USER_ID"] = user_id
    call = AsyncMock(return_value=gw_user_data)
    with patch.object(GWClient, "_gw_api_call", call), pytest.raises(DeezerGWAuthError):
        await client.setup()
    assert call.await_count == 2


async def test_anonymous_first_answer_is_retried(gw_user_data: dict[str, Any]) -> None:
    """An authenticated second response completes setup."""
    client = GWClient(Mock(), "arl-one")
    anonymous = {"error": [], "results": {"USER": {"USER_ID": 0}}}
    call = AsyncMock(side_effect=[anonymous, gw_user_data])
    with patch.object(GWClient, "_gw_api_call", call):
        await client.setup()
    assert call.await_count == 2
    assert client._user_id == 123


async def test_gw_error_during_setup_does_not_recurse() -> None:
    """A failed handshake must not enter the API call's authentication retry."""
    session = Mock(cookie_jar=CookieJar())
    response = Mock(cookies={})
    response.json = AsyncMock(return_value={"error": {"VALID_TOKEN_REQUIRED": "No session"}})
    session.request = AsyncMock(return_value=response)
    client = GWClient(session, "arl-one")

    with pytest.raises(DeezerGWError, match="Failed to call GW-API"):
        await client.setup()

    session.request.assert_awaited_once()


async def test_missing_offer_raises_no_subscription_error(gw_user_data: dict[str, Any]) -> None:
    """An account without an offer authenticated fine, it just cannot stream."""
    client = GWClient(Mock(), "arl-one")
    gw_user_data["results"]["OFFER_ID"] = 0
    with (
        patch.object(GWClient, "_gw_api_call", AsyncMock(return_value=gw_user_data)),
        pytest.raises(DeezerGWNoSubscriptionError),
    ):
        await client.setup()


@pytest.mark.parametrize(
    ("error", "expected_key"),
    [
        (DeezerGWNoSubscriptionError("no offer"), "no_subscription"),
        (DeezerGWAuthError("no user"), "gw_no_session"),
        (GraphQLClientAuthError("rejected"), "arl_rejected"),
        (GraphQLClientError("boom"), "auth_failed"),
        (DeezerGWError("boom"), "auth_failed"),
    ],
)
async def test_each_cause_gets_its_own_translation_key(error: Exception, expected_key: str) -> None:
    """The cause must survive as a distinct, provider owned key."""
    login_failed = await _init_with(error)
    assert login_failed.translation_key == expected_key
    assert login_failed.translation_owner == "provider.deezer"


async def test_cause_is_chained_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Preserve the exception cause and include it in the server log."""
    cause = DeezerGWNoSubscriptionError("Free subscriptions cannot be used in MA.")
    login_failed = await _init_with(cause)
    assert login_failed.__cause__ is cause
    assert str(cause) in caplog.text


def test_every_translation_key_exists_in_strings() -> None:
    """A key without a string would silently fall back to the generic message."""
    errors = json.loads(STRINGS.read_text(encoding="utf-8"))["errors"]
    for key in ("no_subscription", "arl_rejected", "gw_no_session", "auth_failed"):
        assert errors.get(key), f"missing errors.{key} in deezer strings.json"
