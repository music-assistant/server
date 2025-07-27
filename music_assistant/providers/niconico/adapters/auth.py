"""Authentication adapter for NicoNico."""

import asyncio
from typing import TYPE_CHECKING

from niconico import NicoNico
from niconico.exceptions import LoginFailureError

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.constants import (
    CONF_MFA,
    CONF_USER_SESSION,
)

if TYPE_CHECKING:
    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoAuthAdapter(NiconicoBaseAdapter):
    """Handles authentication and session management for NicoNico."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
        """Initialize the NiconicoAuthAdapter with a reference to the parent adapter."""
        super().__init__(adapter)

    def is_logged_in(self) -> bool:
        """Check if the user is logged in to NicoNico."""
        return self.adapter.niconico_py_client.logined

    async def try_login(self) -> bool:
        """Attempt to login to NicoNico with the configured credentials."""
        if self.is_logged_in():
            return True
        provider = self.adapter.provider
        username = provider.config.get_value(CONF_USERNAME)
        password = provider.config.get_value(CONF_PASSWORD)
        mfa = provider.config.get_value(CONF_MFA)
        user_session = provider.config.get_value(CONF_USER_SESSION)
        max_retries = 3
        retry_delay_seconds = 1
        async with self.adapter.niconico_api_throttler.bypass():
            for attempt in range(max_retries):
                try:
                    self.adapter.logger.info(
                        f"Trying to log in... (Number of attempts: {attempt + 1}/{max_retries})"
                    )
                    if user_session:
                        self.adapter.logger.info("Using user_session for login.")
                        copied_user_session = str(user_session)
                        user_session = None
                        await asyncio.to_thread(
                            self.adapter.niconico_py_client.login_with_session, copied_user_session
                        )
                    else:
                        self.adapter.logger.info("Using mail and password for login.")
                        if not username or not password:
                            self.adapter.logger.info(
                                "Username and password are not set in the configuration."
                            )
                            return False
                        await asyncio.to_thread(
                            self.adapter.niconico_py_client.login_with_mail,
                            str(username),
                            str(password),
                            str(mfa) if mfa else None,
                        )
                    self.adapter.logger.info("Successful login!")
                    self.adapter.mass.config.set_raw_provider_config_value(
                        provider.instance_id,
                        CONF_USER_SESSION,
                        self.adapter.niconico_py_client.get_user_session(),
                        True,
                    )
                    return True
                except LoginFailureError as err:
                    self.adapter.logger.error("Login Failure: %s", err)
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
        """Log out from the NicoNico service."""
        if self.adapter.niconico_py_client:
            await self.adapter.call_with_throttler(
                self.adapter.niconico_py_client.get, "https://account.nicovideo.jp/logout"
            )
            self.adapter.niconico_py_client = NicoNico()

    def start_periodic_relogin_task(self) -> None:
        """Start the periodic re-login task."""
        self.adapter.mass.create_task(self._schedule_periodic_relogin())

    async def _schedule_periodic_relogin(self) -> None:
        """Periodic re-login every 30 days."""
        while True:
            await asyncio.sleep(30 * 24 * 60 * 60)
            self.adapter.logger.info("Performing periodic re-login to refresh the session.")
            await self.try_logout()
            await self.try_login()
