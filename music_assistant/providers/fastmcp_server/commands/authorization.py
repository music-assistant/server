"""Authorization shared by provider-owned native MA API commands."""
# ruff: noqa: TID252 -- provider source is transplanted under the MA package.

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope
from music_assistant_models.errors import AuthenticationRequired, InsufficientPermissions

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_token,
    get_current_user,
    has_scope,
)

from ..confirmation_context import capability_was_confirmed

# removed global capability fallback

if TYPE_CHECKING:
    from music_assistant_models.auth import User
    from music_assistant_models.config_entries import ProviderConfig

    from ..policy import PolicySnapshot


def normalize_scope(required_scope: object) -> Scope | None:
    """Return one known MA scope or fail closed for unknown runtime values."""
    if isinstance(required_scope, Scope):
        scope = required_scope
    elif isinstance(required_scope, str):
        try:
            scope = Scope(required_scope)
        except ValueError:
            return None
    else:
        return None
    return None if scope is Scope.UNKNOWN else scope


def scope_allowed(user: User, required_scope: object) -> bool:
    """Delegate enabled-user scope checks to Music Assistant's current helper."""
    if not getattr(user, "enabled", False):
        return False
    scope = normalize_scope(required_scope)
    if scope is None:
        return False
    return bool(has_scope(user, scope))


def current_bearer_token() -> str | None:
    """Return the current MA request bearer for exact policy resolution."""
    return get_current_token()


def current_user() -> User | None:
    """Return the current Music Assistant request user."""
    return get_current_user()


def authorize_extension(
    _config: ProviderConfig,
    *,
    required_scope: str,
    required_capability: str,
    policy_provider: Callable[[str | None], PolicySnapshot] | None = None,
    require_auth: bool = True,
    confirmation_command: str | None = None,
) -> User | None:
    """Require request identity when enabled and always enforce provider policy."""
    user = get_current_user()
    if require_auth:
        if user is None or not getattr(user, "enabled", False):
            raise AuthenticationRequired("An enabled Music Assistant user is required")
        if not scope_allowed(user, required_scope):
            raise InsufficientPermissions(f"Scope {required_scope!r} is required")
    if policy_provider is not None:
        from ..policy import PolicyMode  # noqa: PLC0415

        bearer = get_current_token()
        mode = (
            policy_provider(bearer).mode(required_capability)
            if bearer is not None or not require_auth
            else PolicyMode.DENY
        )
        if mode is PolicyMode.DENY:
            raise InsufficientPermissions(
                f"Provider permission {required_capability!r} is disabled"
            )
        if mode is PolicyMode.CONFIRM and (
            confirmation_command is None
            or not capability_was_confirmed(confirmation_command, required_capability)
        ):
            raise InsufficientPermissions(
                f"Capability {required_capability!r} requires confirmation; set it to Allow or use an "
                "elicitation-capable client"
            )
    else:
        raise InsufficientPermissions("A request policy provider is required")
    return user  # type: ignore[no-any-return, unused-ignore]
