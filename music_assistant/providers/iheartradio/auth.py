"""Session handling for the iHeartRadio provider."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import LoginFailed, MediaNotFoundError

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME

from .constants import (
    CONF_PROFILE_ID,
    CONF_SESSION_ID,
    CONF_SESSION_USERNAME,
    DEVICE_NAME,
    HEADER_PROFILE_ID,
    HEADER_SESSION_ID,
    HEADER_SESSION_ID_V2,
    HEADER_USER_ID,
    PATH_GUEST_LOGIN,
    PATH_LOGIN,
    PATH_SESSION,
)

if TYPE_CHECKING:
    from .provider import IHeartRadioProvider


@dataclass(frozen=True)
class IHeartRadioSession:
    """The session the API knows us by."""

    profile_id: str
    session_id: str
    # the account the session belongs to; empty for a guest session
    username: str

    @property
    def is_account(self) -> bool:
        """Return whether the session belongs to a signed-in account."""
        return bool(self.username)

    @property
    def headers(self) -> dict[str, str]:
        """Return the headers that carry this session."""
        return {
            HEADER_PROFILE_ID: self.profile_id,
            HEADER_SESSION_ID: self.session_id,
            HEADER_USER_ID: self.profile_id,
            HEADER_SESSION_ID_V2: self.session_id,
        }


class IHeartRadioAuthManager:
    """
    Hold the provider's session with iHeartRadio.

    A signed-in session is opened when credentials are configured, a guest session
    otherwise, the same way the website serves visitors. Either is persisted so a restart
    reuses it instead of opening another one.
    """

    session: IHeartRadioSession | None = None

    def __init__(self, provider: IHeartRadioProvider) -> None:
        """Initialize the auth manager."""
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger

    @property
    def is_account(self) -> bool:
        """Return whether the provider is signed in to an account."""
        return self.session is not None and self.session.is_account

    @property
    def profile_id(self) -> str:
        """Return the profile id of the current session."""
        if self.session is None:
            raise LoginFailed("Not signed in to iHeartRadio")
        return self.session.profile_id

    @property
    def headers(self) -> dict[str, str]:
        """Return the session headers to send, empty without a session."""
        return self.session.headers if self.session is not None else {}

    async def login(self) -> None:
        """Reuse the persisted session when it is still valid, otherwise open a new one."""
        username = self._username()
        stored = self._stored_session()
        if stored is not None and stored.username == username and await self._is_valid(stored):
            self.session = stored
            return
        await self.relogin()

    async def relogin(self) -> None:
        """Open a new session, signed in when credentials are configured."""
        username = self._username()
        password = str(self.provider.get_setup_value(CONF_PASSWORD) or "")
        self.session = None
        if username:
            payload = await self._account_login(username, password)
        else:
            payload = await self._guest_login()
        profile_id, session_id = payload.get("profileId"), payload.get("sessionId")
        if not profile_id or not session_id:
            raise LoginFailed("iHeartRadio did not return a session")
        self.session = IHeartRadioSession(str(profile_id), str(session_id), username)
        self._persist(self.session)
        self.logger.info(
            "Signed in to iHeartRadio as %s", username or "a guest (no account configured)"
        )

    def _username(self) -> str:
        """Return the configured account name, empty when none is set."""
        return str(self.provider.get_setup_value(CONF_USERNAME) or "").strip()

    async def _account_login(self, username: str, password: str) -> dict[str, Any]:
        """
        Sign in to an account.

        :param username: The account's email address.
        :param password: The account's password.
        """
        if not password:
            raise LoginFailed("A password is required to sign in to iHeartRadio")
        payload = await self.provider.request(
            "POST",
            PATH_LOGIN,
            form={
                "userName": username,
                "password": password,
                "deviceId": self._device_id(username),
                "deviceName": DEVICE_NAME,
                "host": self.provider.host_name,
            },
            authenticated=False,
        )
        return payload if isinstance(payload, dict) else {}

    async def _guest_login(self) -> dict[str, Any]:
        """Open a guest session."""
        payload = await self.provider.request(
            "POST",
            PATH_GUEST_LOGIN,
            form={
                "accessTokenType": "anon",
                "deviceId": self._device_id("guest"),
                "deviceName": DEVICE_NAME,
                "host": self.provider.host_name,
                # stable per instance, so a lost session reattaches to the same guest profile
                "oauthUuid": str(uuid.uuid5(uuid.NAMESPACE_URL, self.provider.instance_id)),
            },
            authenticated=False,
        )
        return payload if isinstance(payload, dict) else {}

    async def _is_valid(self, session: IHeartRadioSession) -> bool:
        """
        Return whether the API still accepts a session.

        :param session: The session to check.
        """
        try:
            await self.provider.request(
                "HEAD", PATH_SESSION, headers=session.headers, retry_auth=False
            )
        except LoginFailed, MediaNotFoundError:
            # a dead session answers 401, an expired one 410
            return False
        return True

    def _device_id(self, owner: str) -> str:
        """
        Return the device id this instance signs in with.

        :param owner: The account name, or a marker for the guest session.
        """
        return hashlib.sha256(f"{self.provider.instance_id}:{owner}".encode()).hexdigest()

    def _stored_session(self) -> IHeartRadioSession | None:
        """Return the persisted session, or None when there is none."""
        config = self.mass.config
        instance_id = self.provider.instance_id
        profile_id = config.get_raw_provider_config_value(instance_id, CONF_PROFILE_ID)
        session_id = config.get_raw_provider_config_value(instance_id, CONF_SESSION_ID)
        if not profile_id or not isinstance(session_id, str):
            return None
        username = config.get_raw_provider_config_value(instance_id, CONF_SESSION_USERNAME)
        return IHeartRadioSession(
            str(profile_id), config.decrypt_string(session_id), str(username or "")
        )

    def _persist(self, session: IHeartRadioSession) -> None:
        """
        Persist a session for the next start.

        :param session: The session to keep.
        """
        config = self.mass.config
        instance_id = self.provider.instance_id
        config.set_raw_provider_config_value(instance_id, CONF_PROFILE_ID, session.profile_id)
        config.set_raw_provider_config_value(
            instance_id, CONF_SESSION_ID, session.session_id, encrypted=True, immediate=True
        )
        config.set_raw_provider_config_value(instance_id, CONF_SESSION_USERNAME, session.username)
