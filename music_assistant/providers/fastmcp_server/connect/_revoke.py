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

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant_models.auth import AuthToken, User

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)

# Sanctioned contextvar helpers live in an MA-internal module. In a real MA
# install the import succeeds (it's the same module MA's own tests use —
# tests/test_webserver_auth.py:19-22). In this repo's minimal dev venv the
# transitive ``music_assistant.controllers.webserver`` package can't be
# loaded (frontend / chardet / torch are not installed), so fall back to
# no-op shims for collect-time imports. Tests mock the API methods that
# would actually read ``current_user``, so a no-op context manager is safe
# there. Production always hits the real branch.
try:
    from music_assistant.controllers.webserver.helpers.auth_middleware import (
        get_current_user as _ma_get_current_user,
    )
    from music_assistant.controllers.webserver.helpers.auth_middleware import (
        set_current_user as _ma_set_current_user,
    )
except ImportError:
    # Narrow on purpose: only swallow ``ImportError`` (which covers
    # ``ModuleNotFoundError``) — the case is the minimal dev venv missing
    # a transitive MA dep. Anything else (e.g. ``AttributeError`` from a
    # renamed symbol) must propagate so MA-side breakage surfaces loudly
    # instead of silently disabling token revocation.
    # Signatures must match the real MA helpers exactly — mypy on CI sees
    # both branches with the full MA install and rejects any drift.

    def _ma_get_current_user() -> User | None:
        return None

    def _ma_set_current_user(user: User | None) -> None:  # noqa: ARG001
        return None


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


async def list_user_tokens(mass: MusicAssistant, user: User) -> list[AuthToken]:
    """
    List ``user``'s auth tokens via the sanctioned ``auth.get_user_tokens`` API.

    Returns typed ``AuthToken`` dataclasses — no raw ``sqlite3.Row``
    objects leak across the boundary. Best-effort: an error returns ``[]``.

    Note: MA core caps the query at 100 rows. A user with > 100 active
    tokens will see some priors miss our dedup pass — acceptable for the
    typical case (handful of tokens).

    :param mass: MusicAssistant instance.
    :param user: User whose tokens to list (sets the auth context).
    """
    tokens: list[AuthToken] = []
    with _as_user(user):
        try:
            tokens = await mass.webserver.auth.get_user_tokens()
        except Exception:
            LOGGER.exception("Connect Wizard: get_user_tokens failed (user=%s)", user.user_id)
    return tokens
