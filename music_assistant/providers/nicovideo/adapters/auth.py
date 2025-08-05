"""Authentication adapter for nicovideo."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from niconico import NicoNico
from niconico.exceptions import LoginFailureError

from music_assistant.providers.nicovideo.adapters.base import NicovideoBaseAdapter
from music_assistant.providers.nicovideo.helpers import log_verbose

if TYPE_CHECKING:
    from asyncio import TimerHandle

    from music_assistant.providers.nicovideo.adapter import NicovideoMusicAssistantAdapter


class NicovideoAuthAdapter(NicovideoBaseAdapter):
    """Handles authentication and session management for nicovideo."""

    def __init__(self, adapter: NicovideoMusicAssistantAdapter) -> None:
        """Initialize the NicovideoAuthAdapter with a reference to the parent adapter."""
        super().__init__(adapter)
        self._periodic_relogin_task: TimerHandle | None = None

    def is_logged_in(self) -> bool:
        """Check if the user is logged in to niconico."""
        return self.adapter.niconico_py_client.logined

    async def try_login(self) -> bool:
        """Attempt to login to niconico with the configured credentials."""
        if self.is_logged_in():
            return True

        config = self.nicovideo_config
        credentials = config.get_auth_credentials()
        username = credentials.username
        password = credentials.password
        mfa = credentials.mfa
        user_session = credentials.user_session
        max_retries = 3
        retry_delay_seconds = 1
        async with self.adapter.niconico_api_throttler.bypass():
            for attempt in range(max_retries):
                try:
                    log_verbose(
                        self.adapter.logger,
                        "Trying to log in... (Number of attempts: %d/%d)",
                        attempt + 1,
                        max_retries,
                    )
                    if user_session:
                        log_verbose(self.adapter.logger, "Using user_session for login.")
                        await asyncio.to_thread(
                            self.adapter.niconico_py_client.login_with_session, str(user_session)
                        )
                    else:
                        log_verbose(self.adapter.logger, "Using mail and password for login.")
                        if not username or not password:
                            log_verbose(
                                self.adapter.logger,
                                "Username and password are not set in the configuration.",
                            )
                            return False
                        await asyncio.to_thread(
                            self.adapter.niconico_py_client.login_with_mail,
                            str(username),
                            str(password),
                            str(mfa) if mfa else None,
                        )
                    log_verbose(self.adapter.logger, "Successful login!")
                    # Clear MFA code after successful use (one-time password should not be reused)
                    if mfa:
                        config.clear_mfa_code()
                    session = self.adapter.niconico_py_client.get_user_session()
                    if session:
                        config.save_user_session(session)
                    return True
                except LoginFailureError as err:
                    if user_session:
                        user_session = None  # Clear session on failure
                        self.adapter.logger.warning("Login with user_session failed: %s", err)
                    else:
                        self.adapter.logger.error("Login with mail and password failed: %s", err)
                        return False
                except Exception as e:
                    if (
                        "Name or service not known" in str(e)
                        or "Max retries exceeded" in str(e)
                        or "ConnectionError" in str(e)
                    ):
                        self.adapter.logger.warning(
                            f"Network or DNS error occurred: {e}. "
                            f"Retrying in {retry_delay_seconds} seconds..."
                        )
                        await asyncio.sleep(retry_delay_seconds)
                    else:
                        self.adapter.logger.error("An unexpected error has occurred.: %s", e)
                        return False
        self.adapter.logger.error(
            f"Could not login after exceeding the maximum number of retries ({max_retries})."
        )
        return False

    async def try_logout(self) -> None:
        """Log out from the niconico service."""
        if self.adapter.niconico_py_client:
            if self.is_logged_in():
                await asyncio.to_thread(self.adapter.niconico_py_client.logout)
            self.adapter.niconico_py_client = NicoNico()

    def start_periodic_relogin_task(self) -> None:
        """Start the periodic re-login task."""
        # Cancel existing task if any
        self.stop_periodic_relogin_task()
        self._periodic_relogin_task = self.adapter.mass.call_later(
            30 * 24 * 60 * 60, self._schedule_periodic_relogin()
        )

    def stop_periodic_relogin_task(self) -> None:
        """Stop the periodic re-login task."""
        if self._periodic_relogin_task and not self._periodic_relogin_task.cancelled():
            self._periodic_relogin_task.cancel()
        self._periodic_relogin_task = None

    async def _schedule_periodic_relogin(self) -> None:
        """Periodic re-login every 30 days."""
        try:
            log_verbose(self.adapter.logger, "Performing periodic re-login to refresh the session.")
            await self.try_logout()
            await self.try_login()
            self.start_periodic_relogin_task()
        except asyncio.CancelledError:
            log_verbose(self.adapter.logger, "Periodic relogin task was cancelled.")
            raise
