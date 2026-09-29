"""
Sanctioned-API helpers for wizard-side token management.

The wizard mints (and therefore wants to revoke / list) MA auth tokens, but
the public ``auth.revoke_token`` / ``auth.get_user_tokens`` methods are
``@api_command``-decorated — they read the current authenticated user from
the ``current_user`` ContextVar, which is normally populated by MA's HTTP /
WS request middleware. The wizard's ASGI endpoints run inside MA's process
but outside that middleware, so the contextvar is empty by default.

This module mirrors the pattern MA's own test suite uses
(``tests/test_webserver_auth.py:336-354``): briefly impersonate a known
``User`` via the public ``set_current_user`` helper, then call the API
method, then restore the prior context.

The internal import path
``music_assistant.controllers.webserver.helpers.auth_middleware`` is the
same one MA's tests use; it is not under ``music_assistant_models`` but is
the de-facto contract for in-process callers.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user as _ma_get_current_user,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    set_current_user as _ma_set_current_user,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models.auth import AuthToken, User

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


@contextmanager
def _as_user(user: User) -> Iterator[None]:
    """
    Briefly impersonate ``user`` for an ``@api_command`` call.

    ``current_user`` is a ContextVar — save/restore is task-local, so
    concurrent requests on other users are unaffected.

    :param user: The user to set as the current authenticated user for the
        duration of the ``with`` block.
    """
    prev = _ma_get_current_user()
    _ma_set_current_user(user)
    try:
        yield
    finally:
        _ma_set_current_user(prev)


async def revoke_token_by_id(mass: MusicAssistant, user: User, token_id: str) -> bool:
    """
    Revoke a token via the sanctioned ``auth.revoke_token`` API.

    MA's ``revoke_token`` enforces ownership internally — the impersonated
    ``user`` must own the token (or be admin), or the call raises
    ``InsufficientPermissions``. ``InvalidDataError`` is raised for an
    unknown ``token_id``. Both are swallowed; this is a best-effort
    operation.

    :param mass: MusicAssistant instance.
    :param user: Owner of the token being revoked (sets the auth context).
    :param token_id: ``jti`` of the token to revoke.
    :return: ``True`` if ``revoke_token`` returned without raising,
        ``False`` otherwise.
    """
    with _as_user(user):
        try:
            await mass.webserver.auth.revoke_token(token_id)
        except Exception:
            LOGGER.exception(
                "Connect Wizard: revoke_token failed (token_id=%s, user=%s)",
                token_id,
                user.user_id,
            )
            return False
    return True


async def list_user_tokens(mass: MusicAssistant, user: User) -> list[AuthToken] | None:
    """
    List ``user``'s auth tokens via the sanctioned ``auth.get_user_tokens`` API.

    Returns typed ``AuthToken`` dataclasses — no raw ``sqlite3.Row``
    objects leak across the boundary.

    Note: MA core caps the query at 100 rows. A user with > 100 active
    tokens will see some priors miss our dedup pass — acceptable for the
    typical case (handful of tokens).

    :param mass: MusicAssistant instance.
    :param user: User whose tokens to list (sets the auth context).
    :return: The user's tokens, or ``None`` when the lookup failed.
    """
    with _as_user(user):
        try:
            return list(await mass.webserver.auth.get_user_tokens())
        except Exception:
            LOGGER.exception("Connect Wizard: get_user_tokens failed (user=%s)", user.user_id)
            return None
