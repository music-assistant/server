"""Authorization shared by provider-owned native MA API commands."""
# ruff: noqa: TID252 -- provider source is transplanted under the MA package.

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.errors import AuthenticationRequired, InsufficientPermissions

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_token,
    get_current_user,
    has_scope,
)

from ..command_policy import resolve_command_policy
from ..command_profiles import COMMAND_PROFILES
from ..confirmation_context import capability_was_confirmed
from ..target_filters import enforce_target_filters

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
    command: str | None = None,
    arguments: Mapping[str, Any] | None = None,
    mass: Any = None,
) -> User | None:
    """Require request identity when enabled and always enforce provider policy."""
    from ..policy import PolicyMode  # noqa: PLC0415

    user = get_current_user()
    if require_auth:
        if user is None or not getattr(user, "enabled", False):
            raise AuthenticationRequired("An enabled Music Assistant user is required")
        if not scope_allowed(user, required_scope):
            raise InsufficientPermissions(f"Scope {required_scope!r} is required")
    if policy_provider is None:
        raise InsufficientPermissions("A request policy provider is required")
    bearer = get_current_token()
    if bearer is None and require_auth:
        raise InsufficientPermissions(f"Provider permission {required_capability!r} is disabled")
    policy = policy_provider(bearer)
    if command is not None:
        decision = resolve_command_policy(command, required_scope, COMMAND_PROFILES.get(command))
        if decision.hard_denied:
            raise InsufficientPermissions(
                f"Provider permission {required_capability!r} is disabled"
            )
        mode = decision.effective_mode(policy)
    else:
        mode = policy.mode(required_capability)
    if mode is PolicyMode.DENY:
        raise InsufficientPermissions(f"Provider permission {required_capability!r} is disabled")
    confirm_command = confirmation_command or command
    if mode is PolicyMode.CONFIRM and (
        confirm_command is None
        or not capability_was_confirmed(confirm_command, required_capability)
    ):
        raise InsufficientPermissions(
            f"Capability {required_capability!r} requires confirmation; set it to Allow or use an "
            "elicitation-capable client"
        )
    if command is not None and user is not None and arguments is not None and mass is not None:
        enforce_target_filters(mass, user, command, arguments)
    return user  # type: ignore[no-any-return, unused-ignore]
