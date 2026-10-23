"""Authentication service for nicovideo."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

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

    async def try_login(self) -> bool:
        """Attempt to login to niconico with the configured user_session cookie."""
        if self.is_logged_in:
            return True

        config = self.nicovideo_config
        user_session = config.auth.user_session
        if not user_session:
            self.logger.debug("user_session is not set in the configuration.")
            return False
        max_retries = 3
        retry_delay_seconds = 1
        for attempt in range(max_retries):
            try:
                self.logger.debug(
                    "Trying to log in... (Number of attempts: %d/%d)",
                    attempt + 1,
                    max_retries,
                )
                await asyncio.to_thread(
                    self.niconico_py_client.login_with_session,
                    str(user_session),
                )
                self.logger.info("Successfully authenticated with Nicovideo!")
                session = self.niconico_py_client.get_user_session()
                if session:
                    config.auth.save_user_session(session)
                    log_verbose(
                        self.logger,
                        "Saved user session for future logins (length: %d chars)",
                        len(session),
                    )
                return True
            except LoginFailureError as err:
                self.logger.error("Login with user_session failed, it may have expired: %s", err)
                return False
            except Exception as e:
                if (
                    "Name or service not known" in str(e)
                    or "Max retries exceeded" in str(e)
                    or "ConnectionError" in str(e)
                ):
                    self.logger.warning(
                        "Network or DNS error occurred: %s. Retrying in %d seconds...",
                        e,
                        retry_delay_seconds,
                    )
                    await asyncio.sleep(retry_delay_seconds)
                else:
                    self.logger.error("An unexpected error has occurred.: %s", e)
                    return False
        self.logger.error(
            "Could not login after exceeding the maximum number of retries (%d).",
            max_retries,
        )
        return False
