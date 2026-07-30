"""Unit tests for the config dry-run differ."""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.fastmcp_server.config_io.differ import compute_diff


def _entries() -> dict[str, ConfigEntry]:
    return {
        "log_level": ConfigEntry(key="log_level", type=ConfigEntryType.STRING, label="L"),
        "token": ConfigEntry(key="token", type=ConfigEntryType.SECURE_STRING, label="T"),
    }


def test_diff_computes_before_after_per_key() -> None:
    """Test that diff records before/after per key for non-secret values."""
    diff = compute_diff(
        target_type="provider",
        target_id="p1",
        entries=_entries(),
        current={"log_level": "INFO"},
        proposed={"log_level": "DEBUG"},
    )
    assert diff.target_type == "provider"
    change = next(c for c in diff.changes if c.key == "log_level")
    assert change.before == "INFO"
    assert change.after == "DEBUG"
    assert change.secret is False


def test_diff_masks_secret_values_both_sides() -> None:
    """Test that secret values are masked on both before and after sides."""
    diff = compute_diff(
        target_type="provider",
        target_id="p1",
        entries=_entries(),
        current={"token": "old-secret"},
        proposed={"token": "new-secret"},
    )
    change = next(c for c in diff.changes if c.key == "token")
    assert change.secret is True
    assert change.before == SECURE_STRING_SUBSTITUTE
    assert change.after == SECURE_STRING_SUBSTITUTE
    assert "old-secret" not in str(diff)
    assert "new-secret" not in str(diff)
