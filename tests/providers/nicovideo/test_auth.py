"""Tests for the nicovideo login."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
import requests
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError
from niconico.exceptions import LoginFailureError

from music_assistant.providers.nicovideo.services.auth import NicovideoAuthService


def _create_auth_service(login_with_session: Mock) -> tuple[NicovideoAuthService, Mock]:
    """Create an auth service whose niconico client logs in with the given mock."""
    service_manager = Mock()
    service_manager.niconico_py_client.logined = False
    service_manager.niconico_py_client.login_with_session = login_with_session
    service_manager.niconico_py_client.get_user_session.return_value = "new_session"
    service_manager.nicovideo_config.auth.user_session = "session"
    service_manager.provider.translation_owner = "provider.nicovideo"
    return NicovideoAuthService(service_manager), service_manager


async def test_login_saves_the_session() -> None:
    """A successful login saves the session niconico handed back."""
    auth, service_manager = _create_auth_service(Mock())

    await auth.login()

    service_manager.nicovideo_config.auth.save_user_session.assert_called_once_with("new_session")


async def test_login_rejected_session_raises_login_failed() -> None:
    """A user_session niconico rejects asks the user for a new one."""
    auth, _ = _create_auth_service(Mock(side_effect=LoginFailureError(message="Login failed")))

    with pytest.raises(LoginFailed) as exc_info:
        await auth.login()

    assert exc_info.value.translation_key == "user_session_expired"


async def test_login_network_error_raises_provider_unavailable() -> None:
    """An unreachable niconico is retried, then reported without blaming the user_session."""
    login_with_session = Mock(side_effect=requests.ConnectionError("Name or service not known"))
    auth, _ = _create_auth_service(login_with_session)

    with (
        patch("music_assistant.providers.nicovideo.services.auth.asyncio.sleep"),
        pytest.raises(ProviderUnavailableError),
    ):
        await auth.login()

    assert login_with_session.call_count == 3
