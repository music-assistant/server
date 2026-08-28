"""Immutable capability-policy model tests."""

from __future__ import annotations

from typing import cast

import pytest

from music_assistant.providers.fastmcp_server.capabilities import Capability
from music_assistant.providers.fastmcp_server.policy import (
    PolicyMode,
    PolicyProfile,
    PolicyResolver,
    PolicySelection,
    combine_policy_modes,
    policy_snapshot,
)

_CAPABILITIES = (
    "query:library",
    "query:queue",
    "query:players",
    "query:metadata",
    "control:playback",
    "control:volume",
    "control:players",
    "control:media",
    "edit:library",
    "edit:queue",
    "edit:playlists",
    "edit:favorites",
    "delete:library",
    "delete:queue",
    "delete:playlists",
    "delete:favorites",
    "debug:inspect",
    "debug:logs",
    "debug:events",
    "debug:providers",
    "config:read",
    "config:write:provider",
    "config:write:core",
    "config:write:player",
    "config:write:secret",
    "system:admin",
)


def test_capability_vocabulary_is_the_stable_26_value_snapshot() -> None:
    """A missing, renamed, or reordered public capability breaks the v2 schema."""
    assert tuple(map(str, Capability)) == _CAPABILITIES


@pytest.mark.parametrize(
    ("profile", "allowed_prefixes", "confirmed_prefixes"),
    [
        (PolicyProfile.SAFE_QUERIES, ("query:",), ()),
        (
            PolicyProfile.HOME_CONTROL,
            ("query:", "control:", "edit:"),
            ("delete:",),
        ),
        (
            PolicyProfile.INTERACTIVE_ADMIN,
            ("query:", "control:"),
            ("edit:", "delete:", "debug:", "config:", "system:"),
        ),
        (
            PolicyProfile.TRUSTED,
            tuple({value.split(":", 1)[0] + ":" for value in _CAPABILITIES}),
            (),
        ),
    ],
)
def test_named_profile_snapshot_covers_every_capability(
    profile: PolicyProfile,
    allowed_prefixes: tuple[str, ...],
    confirmed_prefixes: tuple[str, ...],
) -> None:
    """Each named profile assigns one literal mode to every capability."""
    snapshot = policy_snapshot(profile)
    assert tuple(snapshot.modes) == _CAPABILITIES
    assert {capability: mode.value for capability, mode in snapshot.modes.items()} == {
        capability: (
            "allow"
            if capability.startswith(allowed_prefixes)
            else "confirm"
            if capability.startswith(confirmed_prefixes)
            else "deny"
        )
        for capability in _CAPABILITIES
    }


def test_resolver_any_allows_checks_default_and_overrides() -> None:
    """Event retention asks the compiled resolver, not a second policy walk."""
    denied = PolicyResolver(default=PolicySelection.profile(PolicyProfile.SAFE_QUERIES))
    allowed = PolicyResolver(
        default=PolicySelection.profile(PolicyProfile.SAFE_QUERIES),
        overrides={"tok": PolicySelection.profile(PolicyProfile.TRUSTED)},
    )

    assert denied.any_allows(Capability.DEBUG_EVENTS) is False
    assert allowed.any_allows(Capability.DEBUG_EVENTS) is True


def test_custom_snapshot_defaults_unset_capabilities_to_deny() -> None:
    """A partial Custom policy cannot accidentally inherit broader access."""
    snapshot = policy_snapshot(
        PolicyProfile.CUSTOM,
        {
            "query:library": PolicyMode.ALLOW,
            "delete:queue": PolicyMode.CONFIRM,
        },
    )
    assert snapshot.mode("query:library") is PolicyMode.ALLOW
    assert snapshot.mode("delete:queue") is PolicyMode.CONFIRM
    assert snapshot.mode("system:admin") is PolicyMode.DENY
    assert sum(mode is PolicyMode.DENY for mode in snapshot.modes.values()) == 24


def test_policy_snapshots_are_deeply_immutable() -> None:
    """Runtime policy cannot drift after an atomic snapshot swap."""
    custom = {"query:library": PolicyMode.ALLOW}
    snapshot = policy_snapshot(PolicyProfile.CUSTOM, custom)
    custom["query:library"] = PolicyMode.DENY
    assert snapshot.mode("query:library") is PolicyMode.ALLOW
    with pytest.raises(TypeError):
        cast("dict[str, PolicyMode]", snapshot.modes)["query:library"] = PolicyMode.DENY


@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        ((PolicyMode.ALLOW,), PolicyMode.ALLOW),
        ((PolicyMode.ALLOW, PolicyMode.CONFIRM), PolicyMode.CONFIRM),
        ((PolicyMode.CONFIRM, PolicyMode.DENY), PolicyMode.DENY),
        ((PolicyMode.DENY, PolicyMode.ALLOW, PolicyMode.CONFIRM), PolicyMode.DENY),
    ],
)
def test_required_capabilities_use_deny_confirm_allow_precedence(
    modes: tuple[PolicyMode, ...], expected: PolicyMode
) -> None:
    """The most restrictive required capability determines the call mode."""
    assert combine_policy_modes(modes) is expected


def test_resolver_uses_token_id_overrides_and_inherit() -> None:
    """Only an exact token-ID override can replace the global default."""
    resolver = PolicyResolver(
        default=PolicySelection.profile(PolicyProfile.HOME_CONTROL),
        overrides={
            "token-read": PolicySelection.profile(PolicyProfile.SAFE_QUERIES),
            "token-custom": PolicySelection.custom({"debug:logs": PolicyMode.CONFIRM}),
            "token-inherit": PolicySelection.inherit(),
        },
    )
    assert resolver.resolve("token-read").profile is PolicyProfile.SAFE_QUERIES
    assert resolver.resolve("token-custom").mode("debug:logs") is PolicyMode.CONFIRM
    assert resolver.resolve("token-custom").mode("query:library") is PolicyMode.DENY
    assert resolver.resolve("token-inherit").profile is PolicyProfile.HOME_CONTROL
    assert resolver.resolve("unknown-token").profile is PolicyProfile.HOME_CONTROL
    assert resolver.resolve(None).profile is PolicyProfile.HOME_CONTROL


def test_resolver_copies_selection_and_override_inputs() -> None:
    """Mutating configuration inputs after construction cannot change resolution."""
    custom = {"query:queue": PolicyMode.ALLOW}
    overrides = {"token": PolicySelection.custom(custom)}
    resolver = PolicyResolver(
        default=PolicySelection.profile(PolicyProfile.SAFE_QUERIES),
        overrides=overrides,
    )
    custom["query:queue"] = PolicyMode.DENY
    overrides.clear()
    assert resolver.resolve("token").mode("query:queue") is PolicyMode.ALLOW
