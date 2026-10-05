"""
Compute a before/after diff for a dry-run config write.

SECURE_STRING values are masked on both sides so a preview never leaks
a stored or proposed credential.
"""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE

from ..models import DiffResult, ValueChange
from .secret_handler import is_secret_key

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.config_entries import ConfigEntry


def compute_diff(
    *,
    target_type: str,
    target_id: str,
    entries: Mapping[str, ConfigEntry],
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
) -> DiffResult:
    """
    Build a :class:`DiffResult` for the proposed write.

    :param target_type: "provider" | "core" | "player".
    :param target_id: The target identifier.
    :param entries: ConfigEntry definitions (used to detect SECURE_STRING).
    :param current: Currently stored values.
    :param proposed: Proposed new values (only these keys are diffed).
    """
    changes: list[ValueChange] = []
    for key, after in proposed.items():
        secret = is_secret_key(entries, key)
        before = current.get(key)
        if secret:
            before_display: Any = SECURE_STRING_SUBSTITUTE if before is not None else None
            after_display: Any = SECURE_STRING_SUBSTITUTE
        else:
            before_display = before
            after_display = after
        changes.append(
            ValueChange(key=key, before=before_display, after=after_display, secret=secret)
        )
    return DiffResult(target_type=target_type, target_id=target_id, changes=changes)
