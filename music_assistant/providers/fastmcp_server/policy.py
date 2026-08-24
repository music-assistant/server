"""Immutable capability-policy primitives and token-ID resolution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from .capabilities import Capability

POLICY_SCHEMA_VERSION = 2


class PolicyMode(StrEnum):
    """One effective capability behavior."""

    DENY = "deny"
    ALLOW = "allow"
    CONFIRM = "confirm"


class PolicyProfile(StrEnum):
    """Named policy profiles exposed by provider configuration."""

    SAFE_QUERIES = "Safe queries"
    HOME_CONTROL = "Home control"
    INTERACTIVE_ADMIN = "Interactive admin"
    TRUSTED = "Trusted"
    CUSTOM = "Custom"


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """Fully expanded immutable policy used by one request."""

    profile: PolicyProfile
    modes: Mapping[str, PolicyMode]

    def __post_init__(self) -> None:
        """Copy and freeze the complete capability map."""
        normalized = {str(capability): PolicyMode(mode) for capability, mode in self.modes.items()}
        expected = {str(capability) for capability in Capability}
        if set(normalized) != expected:
            raise ValueError("Policy snapshot must assign every supported capability")
        object.__setattr__(
            self,
            "modes",
            MappingProxyType(
                {str(capability): normalized[str(capability)] for capability in Capability}
            ),
        )

    def mode(self, capability: str | Capability) -> PolicyMode:
        """Return the effective mode for one supported capability."""
        try:
            return self.modes[str(capability)]
        except KeyError as exc:
            raise ValueError(f"Unsupported capability: {capability}") from exc


@dataclass(frozen=True, slots=True)
class PolicySelection:
    """An Inherit, named-profile, or Custom configuration choice."""

    choice: PolicyProfile | None
    custom_modes: Mapping[str, PolicyMode] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate and freeze custom capability choices."""
        normalized = {
            str(capability): PolicyMode(mode) for capability, mode in self.custom_modes.items()
        }
        unsupported = set(normalized) - {str(capability) for capability in Capability}
        if unsupported:
            raise ValueError(f"Unsupported capabilities: {', '.join(sorted(unsupported))}")
        if self.choice is not PolicyProfile.CUSTOM and normalized:
            raise ValueError("Only Custom selections accept explicit capability modes")
        object.__setattr__(self, "custom_modes", MappingProxyType(normalized))

    @classmethod
    def inherit(cls) -> PolicySelection:
        """Build an Inherit override choice."""
        return cls(None)

    @classmethod
    def profile(cls, profile: PolicyProfile) -> PolicySelection:
        """Build one named-profile choice."""
        if profile is PolicyProfile.CUSTOM:
            raise ValueError("Use PolicySelection.custom for a Custom policy")
        return cls(profile)

    @classmethod
    def custom(cls, modes: Mapping[str | Capability, PolicyMode]) -> PolicySelection:
        """Build one explicit Custom choice."""
        return cls(
            PolicyProfile.CUSTOM, {str(capability): mode for capability, mode in modes.items()}
        )


@dataclass(frozen=True, slots=True)
class PolicyResolver:
    """Immutable default and token-ID-specific policy resolver."""

    default: PolicySelection
    overrides: Mapping[str, PolicySelection] = field(default_factory=dict)
    _default_snapshot: PolicySnapshot = field(init=False, repr=False)
    _override_snapshots: Mapping[str, PolicySnapshot | None] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Compile selections so request resolution cannot observe later mutation."""
        if self.default.choice is None:
            raise ValueError("The default policy cannot Inherit")
        default = _selection_snapshot(self.default)
        overrides = {
            str(token_id): None if selection.choice is None else _selection_snapshot(selection)
            for token_id, selection in self.overrides.items()
        }
        if any(not token_id for token_id in overrides):
            raise ValueError("Token override IDs must not be empty")
        object.__setattr__(self, "default", _copy_selection(self.default))
        object.__setattr__(self, "overrides", MappingProxyType(dict(self.overrides)))
        object.__setattr__(self, "_default_snapshot", default)
        object.__setattr__(self, "_override_snapshots", MappingProxyType(overrides))

    def resolve(self, token_id: str | None) -> PolicySnapshot:
        """Resolve an immutable policy by Music Assistant token ID."""
        if token_id is None:
            return self._default_snapshot
        return self._override_snapshots.get(token_id) or self._default_snapshot


def policy_snapshot(
    profile: PolicyProfile,
    custom_modes: Mapping[str | Capability, PolicyMode] | None = None,
) -> PolicySnapshot:
    """Expand one profile into a complete immutable capability map."""
    if profile is PolicyProfile.CUSTOM:
        explicit = {
            str(capability): PolicyMode(mode) for capability, mode in (custom_modes or {}).items()
        }
        unsupported = set(explicit) - {str(capability) for capability in Capability}
        if unsupported:
            raise ValueError(f"Unsupported capabilities: {', '.join(sorted(unsupported))}")
        expanded = {
            str(capability): explicit.get(str(capability), PolicyMode.DENY)
            for capability in Capability
        }
        return PolicySnapshot(profile, expanded)
    if custom_modes:
        raise ValueError("Explicit capability modes require the Custom profile")

    modes: dict[str, PolicyMode] = {}
    for capability in Capability:
        value = str(capability)
        if profile is PolicyProfile.TRUSTED:
            mode = PolicyMode.ALLOW
        elif profile is PolicyProfile.SAFE_QUERIES:
            mode = PolicyMode.ALLOW if value.startswith("query:") else PolicyMode.DENY
        elif profile is PolicyProfile.HOME_CONTROL:
            mode = (
                PolicyMode.ALLOW
                if value.startswith(("query:", "control:", "edit:"))
                else PolicyMode.CONFIRM
                if value.startswith("delete:")
                else PolicyMode.DENY
            )
        else:
            mode = (
                PolicyMode.ALLOW if value.startswith(("query:", "control:")) else PolicyMode.CONFIRM
            )
        modes[value] = mode
    return PolicySnapshot(profile, modes)


def combine_policy_modes(modes: Iterable[PolicyMode]) -> PolicyMode:
    """Combine required capability modes with deny, confirm, allow precedence."""
    resolved = tuple(PolicyMode(mode) for mode in modes)
    if not resolved or PolicyMode.DENY in resolved:
        return PolicyMode.DENY
    if PolicyMode.CONFIRM in resolved:
        return PolicyMode.CONFIRM
    return PolicyMode.ALLOW


def _selection_snapshot(selection: PolicySelection) -> PolicySnapshot:
    """Compile one non-Inherit configuration selection."""
    if selection.choice is None:
        raise ValueError("Cannot snapshot an Inherit selection")
    return policy_snapshot(selection.choice, selection.custom_modes)


def _copy_selection(selection: PolicySelection) -> PolicySelection:
    """Copy one selection independently from caller-owned inputs."""
    return PolicySelection(selection.choice, dict(selection.custom_modes))
