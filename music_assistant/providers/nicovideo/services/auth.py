"""Authentication service for nicovideo."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import requests
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError
from niconico.exceptions import LoginFailureError

from music_assistant.providers.nicovideo.helpers import log_verbose
from music_assistant.providers.nicovideo.services.base import NicovideoBaseService

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoAuthService(NicovideoBaseService):
    """Handles authentication and session management for nicovideo."""

    def __init__(self, service_manager: NicovideoServiceManager) -> None:
        """Initialize the NicovideoAuthService with a reference to the parent service manager."""
        super().__init__(service_manager)

    @property
    def is_logged_in(self) -> bool:
        """Check if the user is logged in to niconico."""
        return bool(self.niconico_py_client.logined)

    async def login(self) -> None:
        """
        Log in to niconico with the configured user_session cookie.

        :raises LoginFailed: If niconico rejects the user_session cookie.
        :raises ProviderUnavailableError: If niconico cannot be reached.
        """
        if self.is_logged_in:
            return

        config = self.nicovideo_config
        user_session = str(config.auth.user_session)
        max_retries = 3
        retry_delay_seconds = 1
        for attempt in range(max_retries):
            self.logger.debug(
                "Trying to log in... (Number of attempts: %d/%d)",
                attempt + 1,
                max_retries,
            )
            try:
                await asyncio.to_thread(self.niconico_py_client.login_with_session, user_session)
            except LoginFailureError as err:
                raise LoginFailed(
                    "Your niconico user_session cookie is invalid or has expired. "
                    "Enter a new one in the provider settings.",
                    translation_key="user_session_expired",
                    translation_owner=self.service_manager.provider.translation_owner,
                ) from err
            except (requests.ConnectionError, requests.Timeout) as err:
                if attempt + 1 == max_retries:
                    raise ProviderUnavailableError(
                        f"Could not reach niconico to log in: {err}"
                    ) from err
                self.logger.warning(
                    "Network error while logging in: %s. Retrying in %d seconds...",
                    err,
                    retry_delay_seconds,
                )
                await asyncio.sleep(retry_delay_seconds)
            else:
                break

        self.logger.info("Successfully authenticated with Nicovideo!")
        session = self.niconico_py_client.get_user_session()
        if session:
            config.auth.save_user_session(session)
            log_verbose(
                self.logger,
                "Saved user session for future logins (length: %d chars)",
                len(session),
            )
