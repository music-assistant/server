"""Tests for the error responses of the HTTP JSON-RPC API route."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import StreamReader, web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.errors import (
    AuthenticationFailed,
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
    InvalidToken,
    MediaNotFoundError,
    PlayerUnavailableError,
    ProviderPermissionDenied,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
    UserNotFoundError,
)

from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.helpers.api import APICommandHandler
from music_assistant.helpers.json import json_dumps, json_loads

COMMAND = "test/command"


@pytest.fixture
def webserver() -> WebserverController:
    """Create a webserver controller whose server knows no commands yet."""
    mass = MagicMock()
    mass.command_handlers = {}
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.translations.ensure_locale_loaded = AsyncMock()
    mass.translations.get_translation.return_value = None
    controller = WebserverController(mass)
    controller.auth = MagicMock(has_users=True)
    return controller


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (InsufficientPermissions("Admins only"), 403),
        (MediaNotFoundError("No such track"), 404),
        (PlayerUnavailableError("No such player"), 404),
        (InvalidDataError("Bad value"), 400),
        (UserNotFoundError("No such user"), 400),
        (ProviderUnavailableError("Provider is not loaded"), 400),
        (UnsupportedFeaturedException("Not supported"), 400),
        # credentials of a provider account, not those of the caller
        (InvalidToken("Invalid provider token"), 400),
        (AuthenticationFailed("Provider login failed"), 400),
        (ProviderPermissionDenied("Provider refused"), 400),
    ],
)
async def test_a_refusal_answers_a_client_error_with_its_error_code(
    webserver: WebserverController, error: Exception, status: int
) -> None:
    """
    Test that an error the server raises on purpose answers a 4xx with its code and message.

    :param webserver: The webserver controller.
    :param error: The error the command raises.
    :param status: The HTTP status the route should answer.
    """
    _register(webserver, _raising(error))

    response = await webserver._handle_jsonrpc_api_command(_request())

    assert response.status == status
    body = _body(response)
    assert body["message_id"] == "1"
    assert body["error_code"] == error.error_code  # type: ignore[attr-defined]
    assert body["details"] == str(error)


async def test_a_refusal_reads_in_the_language_of_the_client(
    webserver: WebserverController,
) -> None:
    """
    Test that the message of a refusal is localized for the locale of the request.

    :param webserver: The webserver controller.
    """

    def _translate(key: str, locale: str | None = None, **_: Any) -> str | None:
        return f"{locale}: niet gevonden" if key == "errors.media_not_found" else None

    _register(webserver, _raising(MediaNotFoundError("No such track")))

    with patch.object(webserver.mass.translations, "get_translation", side_effect=_translate):
        response = await webserver._handle_jsonrpc_api_command(
            _request(headers={"Accept-Language": "nl-NL"})
        )

    assert response.status == 404
    assert _body(response)["details"] == "nl-NL: niet gevonden"


async def test_an_unexpected_failure_answers_500_and_is_logged(
    webserver: WebserverController, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Test that a failure the server did not raise on purpose answers 500 without its details.

    :param webserver: The webserver controller.
    :param caplog: The log capture fixture.
    """
    _register(webserver, _raising(RuntimeError("secret internals")))

    with caplog.at_level(logging.ERROR):
        response = await webserver._handle_jsonrpc_api_command(_request())

    assert response.status == 500
    body = _body(response)
    assert body["error_code"] == 999
    assert body["details"] == "Internal server error"
    assert "secret internals" in caplog.text


async def test_invalid_arguments_answer_400(webserver: WebserverController) -> None:
    """
    Test that a command called with an argument of the wrong type answers 400.

    :param webserver: The webserver controller.
    """

    async def _command(count: int) -> int:
        return count

    _register(webserver, _command)

    response = await webserver._handle_jsonrpc_api_command(_request({"count": "many"}))

    assert response.status == 400
    assert _body(response)["error_code"] == InvalidDataError.error_code


async def test_an_unknown_command_answers_400_with_its_error_code(
    webserver: WebserverController,
) -> None:
    """
    Test that an unknown command answers 400 with the invalid command error code.

    :param webserver: The webserver controller.
    """
    response = await webserver._handle_jsonrpc_api_command(_request())

    assert response.status == 400
    assert _body(response)["error_code"] == InvalidCommand.error_code


async def test_a_missing_scope_answers_403_with_its_error_code(
    webserver: WebserverController,
) -> None:
    """
    Test that a user without the scope of a command gets a 403 with the permissions code.

    :param webserver: The webserver controller.
    """
    _register(webserver, _returning("ok"), required_scope=Scope.ALL)
    user = User(user_id="u1", username="someone", role=UserRole.USER)

    with patch(
        "music_assistant.controllers.webserver.controller.get_authenticated_user",
        AsyncMock(return_value=user),
    ):
        response = await webserver._handle_jsonrpc_api_command(_request())

    assert response.status == 403
    assert _body(response)["error_code"] == InsufficientPermissions.error_code


async def test_a_missing_login_answers_401_with_its_error_code(
    webserver: WebserverController,
) -> None:
    """
    Test that a call without a valid token gets a 401 with the authentication code.

    :param webserver: The webserver controller.
    """
    _register(webserver, _returning("ok"), authenticated=True)

    with patch(
        "music_assistant.controllers.webserver.controller.get_authenticated_user",
        AsyncMock(return_value=None),
    ):
        response = await webserver._handle_jsonrpc_api_command(_request())

    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="Music Assistant"'
    assert _body(response)["error_code"] == 20


async def test_a_successful_command_answers_its_result(webserver: WebserverController) -> None:
    """
    Test that a command that succeeds answers 200 with its result.

    :param webserver: The webserver controller.
    """
    _register(webserver, _returning({"answer": 42}))

    response = await webserver._handle_jsonrpc_api_command(_request())

    assert response.status == 200
    assert _body(response) == {"answer": 42}


def _register(
    webserver: WebserverController,
    func: Callable[..., Any],
    authenticated: bool = False,
    required_scope: Scope | None = None,
) -> None:
    """
    Register a command on the server of the webserver.

    :param webserver: The webserver controller.
    :param func: The function that handles the command.
    :param authenticated: Whether the command requires a signed in user.
    :param required_scope: The scope the command requires.
    """
    webserver.mass.command_handlers[COMMAND] = APICommandHandler.parse(
        COMMAND, func, authenticated=authenticated, required_scope=required_scope
    )


def _raising(error: Exception) -> Callable[[], Any]:
    """
    Return a command function that raises the given error.

    :param error: The error to raise.
    """

    async def _command() -> None:
        raise error

    return _command


def _returning(result: Any) -> Callable[[], Any]:
    """
    Return a command function that returns the given result.

    :param result: The result to return.
    """

    async def _command() -> Any:
        return result

    return _command


def _request(
    args: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> web.Request:
    """
    Build a POST request to the API route that calls the test command.

    :param args: The arguments of the command.
    :param headers: Extra headers of the request.
    """
    body = json_dumps({"message_id": "1", "command": COMMAND, "args": args or {}}).encode()
    payload = StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    payload.feed_data(body)
    payload.feed_eof()
    return make_mocked_request(
        "POST",
        "/api",
        headers={"Content-Type": "application/json", **(headers or {})},
        app=web.Application(),
        payload=payload,
    )


def _body(response: web.StreamResponse) -> Any:
    """
    Return the decoded JSON body of a response.

    :param response: The response of the route.
    """
    assert isinstance(response, web.Response)
    assert response.content_type == "application/json"
    assert response.body is not None
    return json_loads(bytes(response.body))  # type: ignore[arg-type]
