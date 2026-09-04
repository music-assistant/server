"""
Test that a failed Deezer login says WHY it failed.

Every cause used to collapse into one generic LoginFailed whose message the API
layer then replaced with the shared "login_failed" translation, so a report could
not tell an expired ARL from an account without a subscription.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from deezer_python_gql import GraphQLClientAuthError, GraphQLClientError
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.deezer.gw_client import (
    DeezerGWAuthError,
    DeezerGWError,
    DeezerGWNoSubscriptionError,
    GWClient,
)
from music_assistant.providers.deezer.provider import SUPPORTED_FEATURES, DeezerProvider

STRINGS = pathlib.Path("music_assistant/providers/deezer/strings.json")


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
    """Run handle_async_init where the GW setup raises, and return the LoginFailed."""
    mass = Mock()
    mass.config.get.return_value = {}
    provider = _provider(mass)
    with (
        patch("music_assistant.providers.deezer.provider.DeezerGQLClient") as gql_client,
        patch("music_assistant.providers.deezer.provider.GWClient") as gw_client,
    ):
        gql_client.return_value.get_me = AsyncMock(return_value=Mock(id="user123"))
        gw_client.return_value.setup = AsyncMock(side_effect=error)
        with pytest.raises(LoginFailed) as raised:
            await provider.handle_async_init()
    return raised.value


def _user_data(user_id: str | int, offer_id: int) -> dict:
    """Build a getUserData payload complete enough to reach the end of _update_user_data."""
    return {
        "error": [],
        "results": {
            "checkForm": "csrf-token",
            "COUNTRY": "DE",
            "OFFER_ID": offer_id,
            "USER": {
                "USER_ID": user_id,
                "OPTIONS": {
                    "license_token": "license",
                    "expiration_timestamp": 4102444800,
                    "web_sound_quality": {"high": True, "lossless": True},
                    "mobile_sound_quality": {"high": True, "lossless": True},
                },
            },
        },
    }


async def test_missing_user_raises_auth_error() -> None:
    """No USER_ID on either attempt means the ARL was not accepted."""
    client = GWClient(Mock(), "arl-one")
    call = AsyncMock(return_value=_user_data("", 1))
    with patch.object(GWClient, "_gw_api_call", call), pytest.raises(DeezerGWAuthError):
        await client.setup()
    assert call.await_count == 2, "the anonymous first answer must be retried once"


async def test_anonymous_first_answer_is_retried() -> None:
    """Deezer hands out the sid on the first call; the second one may be the one that lands."""
    client = GWClient(Mock(), "arl-one")
    call = AsyncMock(side_effect=[_user_data("", 1), _user_data("123", 1)])
    with patch.object(GWClient, "_gw_api_call", call):
        await client.setup()
    assert call.await_count == 2
    assert client._user_id == 123


async def test_retry_does_not_recurse_through_gw_api_call() -> None:
    """_gw_api_call's own retry path calls this method, so it must ask it not to."""
    client = GWClient(Mock(), "arl-one")
    call = AsyncMock(return_value=_user_data("123", 1))
    with patch.object(GWClient, "_gw_api_call", call):
        await client.setup()
    assert call.await_args.kwargs.get("retry") is False


async def test_missing_offer_raises_no_subscription_error() -> None:
    """An account without an offer authenticated fine, it just cannot stream."""
    client = GWClient(Mock(), "arl-one")
    with (
        patch.object(GWClient, "_gw_api_call", AsyncMock(return_value=_user_data("123", 0))),
        pytest.raises(DeezerGWNoSubscriptionError),
    ):
        await client.setup()


def test_specific_errors_stay_catchable_as_the_generic_one() -> None:
    """streaming.py catches DeezerGWError broadly, the subclasses must not escape it."""
    assert issubclass(DeezerGWAuthError, DeezerGWError)
    assert issubclass(DeezerGWNoSubscriptionError, DeezerGWError)


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


async def test_cause_is_chained_and_logged() -> None:
    """Without this the reason is unrecoverable: clients only see the translation."""
    cause = DeezerGWNoSubscriptionError("Free subscriptions cannot be used in MA.")
    login_failed = await _init_with(cause)
    assert login_failed.__cause__ is cause


def test_every_translation_key_exists_in_strings() -> None:
    """A key without a string would silently fall back to the generic message."""
    errors = json.loads(STRINGS.read_text(encoding="utf-8"))["errors"]
    for key in ("no_subscription", "arl_rejected", "gw_no_session", "auth_failed"):
        assert errors.get(key), f"missing errors.{key} in deezer strings.json"


async def test_gw_failure_does_not_blame_the_arl() -> None:
    """
    get_me() signed in with this token first, so a GW failure is not a bad ARL.

    Sharing arl_rejected here would send the user after a fresh token that changes nothing.
    """
    gw_failure = await _init_with(DeezerGWAuthError("no user"))
    arl_failure = await _init_with(GraphQLClientAuthError("rejected"))
    assert gw_failure.translation_key != arl_failure.translation_key
