"""Authentication service for nicovideo."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from niconico.exceptions import LoginFailureError

from music_assistant.providers.nicovideo.helpers import log_verbose
from music_assistant.providers.nicovideo.services.base import NicovideoBaseService

if TYPE_CHECKING:
    from asyncio import TimerHandle

    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoAuthService(NicovideoBaseService):
    """Handles authentication and session management for nicovideo."""

    def __init__(self, service_manager: NicovideoServiceManager) -> None:
        """Initialize the NicovideoAuthService with a reference to the parent service manager."""
        super().__init__(service_manager)
        self._periodic_relogin_task: TimerHandle | None = None

    @property
    def is_logged_in(self) -> bool:
        """Check if the user is logged in to niconico."""
        return bool(self.niconico_py_client.logined)

    async def try_login(self) -> bool:
        """Attempt to login to niconico with the configured credentials."""
        if self.is_logged_in:
            return True

        config = self.nicovideo_config
        username = config.auth.mail
        password = config.auth.password
        mfa = config.auth.mfa
        user_session = config.auth.user_session
        max_retries = 3
        retry_delay_seconds = 1
        async with self.service_manager.niconico_api_throttler.bypass():
            for attempt in range(max_retries):
                try:
                    self.logger.debug(
                        "Trying to log in... (Number of attempts: %d/%d)",
                        attempt + 1,
                        max_retries,
                    )
                    if user_session:
                        self.logger.debug("Using user_session for login.")
                        await asyncio.to_thread(
                            self.niconico_py_client.login_with_session,
                            str(user_session),
                        )
                    else:
                        self.logger.debug("Using mail and password for login.")
                        if not username or not password:
                            self.logger.debug(
                                "Username and password are not set in the configuration.",
                            )
                            return False
                        await asyncio.to_thread(
                            self.niconico_py_client.login_with_mail,
                            str(username),
                            str(password),
                            str(mfa) if mfa else None,
                        )
                    self.logger.info("Successfully authenticated with Nicovideo!")
                    # Clear MFA code after successful use (one-time password should not be reused)
                    if mfa:
                        config.auth.clear_mfa_code()
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
                    if user_session:
                        user_session = None  # Clear session on failure
                        self.logger.warning("Login with user_session failed: %s", err)
                    else:
                        self.logger.error("Login with mail and password failed: %s", err)
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

    async def try_logout(self) -> None:
        """Log out from the niconico service."""
        if self.niconico_py_client:
            if self.is_logged_in:
                await asyncio.to_thread(self.niconico_py_client.logout)
            self.service_manager.reset_niconico_py_client()

    def start_periodic_relogin_task(self) -> None:
        """Start the periodic re-login task."""
        # Cancel existing task if any
        self.stop_periodic_relogin_task()
        self._periodic_relogin_task = self.service_manager.mass.call_later(
            30 * 24 * 60 * 60, self._schedule_periodic_relogin
        )

    def stop_periodic_relogin_task(self) -> None:
        """Stop the periodic re-login task."""
        if self._periodic_relogin_task and not self._periodic_relogin_task.cancelled():
            self._periodic_relogin_task.cancel()
        self._periodic_relogin_task = None

    async def _schedule_periodic_relogin(self) -> None:
        """Periodic re-login every 30 days."""
        try:
            self.logger.debug("Performing periodic re-login to refresh the session.")

            config = self.nicovideo_config
            if not (config.auth.mail or config.auth.password):
                self.logger.debug("No login credentials provided, skipping periodic re-login.")
                self.start_periodic_relogin_task()
                return

            await self.try_logout()
            await asyncio.sleep(3)  # Short delay to ensure logout completes
            await self.try_login()
            self.start_periodic_relogin_task()
        except asyncio.CancelledError:
            self.logger.debug("Periodic relogin task was cancelled.")
            raise
