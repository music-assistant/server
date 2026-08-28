"""Immutable compiled catalog descriptors and request-generation views."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .command_policy import CommandDecision
from .command_profiles import CommandProfile
from .dynamic_signatures import CompiledSignature
from .policy import PolicyMode

type CatalogFingerprint = str | tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class DynamicEntry:
    """One compiled canonical Music Assistant command descriptor."""

    name: str
    command: str
    description: str
    input_schema: dict[str, Any]
    required_scope: str | None
    allow_impersonation: bool
    handler: Any
    search_aliases: tuple[str, ...] = ()
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, bool] = dataclasses.field(default_factory=dict)
    profile: CommandProfile | None = None
    compiled_signature: CompiledSignature | None = None
    decision: CommandDecision | None = None
    policy_mode: PolicyMode = PolicyMode.CONFIRM


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Compiled descriptors for one live command-registry generation."""

    fingerprint: CatalogFingerprint
    entries: tuple[DynamicEntry, ...]
    by_name: Mapping[str, DynamicEntry] = dataclasses.field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Build an immutable O(1) descriptor lookup."""
        object.__setattr__(
            self,
            "by_name",
            MappingProxyType({entry.name: entry for entry in self.entries}),
        )

    def with_entries(self, entries: tuple[DynamicEntry, ...]) -> CatalogSnapshot:
        """Return the same generation with a different entry set."""
        return CatalogSnapshot(self.fingerprint, entries)


CatalogView = CatalogSnapshot


@dataclass(frozen=True, slots=True)
class RequestCatalogContext:
    """One base snapshot and request view from the same registry generation."""

    snapshot: CatalogSnapshot
    view: CatalogView
