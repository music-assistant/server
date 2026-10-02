"""
Helpers for guest access to Music Assistant.

Provides the shared building blocks for plugins that offer a guest experience
(e.g. the party plugin): a dedicated guest user account, join codes and the
join URL that guests open on their own device.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from music_assistant_models.auth import UserRole
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.models.plugin import GuestSession, PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.auth import User

    from music_assistant.mass import MusicAssistant

DEFAULT_JOIN_CODE_EXPIRY_HOURS = 8
GUEST_SESSION_TIMEOUT = 5.0
LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.guest_access")

# Owner id prefixes, naming the kind of authorization a credential is bound to
GUEST_OWNER_PREFIX = "guest-"
USER_OWNER_PREFIX = "user-"


async def get_active_guest_sessions(mass: MusicAssistant) -> list[GuestSession]:
    """
    Return active guest sessions from loaded plugins in a stable order.

    A failing plugin is skipped so one optional guest experience cannot break consumers
    of another one.

    :param mass: MusicAssistant instance.
    """
    sessions: list[GuestSession] = []
    providers = sorted(
        (
            provider
            for provider in mass.providers
            if isinstance(provider, PluginProvider) and provider.available
        ),
        key=lambda provider: provider.instance_id,
    )

    async def fetch_session(provider: PluginProvider) -> GuestSession | None:
        """Return one plugin's session without allowing it to break discovery."""
        try:
            async with asyncio.timeout(GUEST_SESSION_TIMEOUT):
                return await provider.get_active_guest_session()
        except Exception as err:
            LOGGER.warning(
                "Could not retrieve guest session from %s: %s", provider.instance_id, err
            )
            return None

    for session in await asyncio.gather(*(fetch_session(provider) for provider in providers)):
        if session is not None:
            sessions.append(session)
    return sessions


async def get_or_create_guest_user(mass: MusicAssistant, username: str, display_name: str) -> User:
    """
    Get the guest user with the given username, creating it if needed.

    :param mass: MusicAssistant instance.
    :param username: Unique username for the guest account.
    :param display_name: Human readable display name for the guest account.
    :return: The (existing or newly created) guest User.
    :raises InvalidDataError: If the username belongs to a non-guest account.
    """
    auth = mass.webserver.auth
    if user := await auth.get_user_by_username(username):
        # never hand out guest access on a higher privileged account
        if user.role != UserRole.GUEST:
            raise InvalidDataError(f"User {username} exists but is not a guest account")
        return user
    return await auth.create_user(
        username=username,
        role=UserRole.GUEST,
        display_name=display_name,
    )


async def get_or_create_join_code(
    mass: MusicAssistant,
    user: User,
    *,
    expires_in_hours: int = DEFAULT_JOIN_CODE_EXPIRY_HOURS,
    max_uses: int = 0,
    device_name: str = "Guest",
) -> str:
    """
    Get an active join code for the given guest user, creating one if needed.

    :param mass: MusicAssistant instance.
    :param user: The guest user the join code belongs to.
    :param expires_in_hours: Hours until a newly created code expires.
    :param max_uses: Maximum number of uses for a newly created code (0 = unlimited).
    :param device_name: Device name for tokens created with the code.
    :return: The active join code string.
    """
    auth = mass.webserver.auth
    if existing_code := await auth.get_active_join_code(user):
        return existing_code
    code, _expires_at = await auth.generate_join_code(
        user=user,
        expires_in_hours=expires_in_hours,
        max_uses=max_uses,
        device_name=device_name,
    )
    return code


def build_join_url(mass: MusicAssistant, code: str) -> str:
    """
    Build the URL guests open to join with the given join code.

    When remote access is enabled, returns a URL that works from anywhere via
    WebRTC. Otherwise, returns a local URL that only works on the same network.

    :param mass: MusicAssistant instance.
    :param code: The join code to embed in the URL.
    :return: The guest join URL.
    """
    remote_access = mass.webserver.remote_access
    if remote_access.is_enabled and remote_access.remote_id:
        return f"https://app.music-assistant.io/?remote_id={remote_access.remote_id}&join={code}"
    base_url = mass.webserver.base_url
    assert base_url  # for type-checker only
    return f"{base_url}/?join={code}"


def credential_owner(user: User) -> str:
    """
    Return the owner id that credentials minted by this user are bound to.

    The prefix encodes the credential's lifetime: a guest's credentials end with
    their session or access, a full user's with their account.

    :param user: The authenticated user minting the credential.
    """
    guest_owner, user_owner = credential_owners_for_user_id(user.user_id)
    return guest_owner if user.role == UserRole.GUEST else user_owner


def credential_owners_for_user_id(user_id: str) -> tuple[str, str]:
    """
    Return every owner id credentials of the given user account may be bound to.

    :param user_id: The user id whose credentials are looked up.
    """
    return (f"{GUEST_OWNER_PREFIX}{user_id}", f"{USER_OWNER_PREFIX}{user_id}")


def credential_owner_user_id(owner: str) -> str | None:
    """
    Return the user account an owner id is bound to, or ``None`` for another owner kind.

    :param owner: An owner id produced by credential_owner.
    """
    for prefix in (GUEST_OWNER_PREFIX, USER_OWNER_PREFIX):
        if owner.startswith(prefix):
            return owner.removeprefix(prefix)
    return None


def is_session_scoped_owner(owner: str) -> bool:
    """
    Return whether credentials bound to this owner end with the owner's session.

    :param owner: An owner id produced by credential_owner.
    """
    return owner.startswith(GUEST_OWNER_PREFIX)


async def revoke_guest_access(mass: MusicAssistant, username: str) -> tuple[int, int]:
    """
    Revoke all join codes and auth tokens for the guest user with the given username.

    Active WebSocket connections of the guest user are disconnected so guests
    are immediately logged out and can not reconnect.

    :param mass: MusicAssistant instance.
    :param username: Username of the guest account to revoke access for.
    :return: Tuple of (number of join codes revoked, number of tokens revoked).
    """
    auth = mass.webserver.auth
    if not (user := await auth.get_user_by_username(username)):
        return (0, 0)
    codes_revoked = await auth.revoke_join_codes(user)
    tokens_revoked = await auth.revoke_tokens_for_user(user)
    return (codes_revoked, tokens_revoked)
