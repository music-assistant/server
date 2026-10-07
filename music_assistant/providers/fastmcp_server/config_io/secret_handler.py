"""
Gate SECURE_STRING config writes behind the orthogonal secret capability.

The provider performs NO encryption: plaintext is handed to MA's
``save_*_config``, which encrypts via ``Config.to_raw()`` before
persisting. This module only decides whether a write touching a
SECURE_STRING entry is *allowed* given the caller's enabled capabilities.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError
from music_assistant_models.enums import ConfigEntryType

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


def is_secret_key(entries: Mapping[str, ConfigEntry] | Iterable[ConfigEntry], key: str) -> bool:
    """
    Return True iff ``key`` maps to a SECURE_STRING ConfigEntry.

    :param entries: ConfigEntry definitions keyed by config key.
    :param key: The config key to test.
    """
    entry = _entries_by_key(entries).get(key)
    return entry is not None and entry.type == ConfigEntryType.SECURE_STRING


def gate_secret_writes(
    entries: Mapping[str, ConfigEntry] | Iterable[ConfigEntry],
    values: Mapping[str, object],
    *,
    secret_capability_enabled: bool,
) -> None:
    """
    Raise ``ToolError`` if ``values`` writes a SECURE_STRING key without the capability.

    The check is atomic — the whole payload is rejected (the provider does
    not split it), naming the first offending secret key.

    :param entries: ConfigEntry definitions for the target.
    :param values: The proposed key→value writes.
    :param secret_capability_enabled: Whether ``config:write:secret`` is enabled.
    """
    if secret_capability_enabled:
        return
    indexed = _entries_by_key(entries)
    for key in values:
        if is_secret_key(indexed, key):
            raise ToolError(
                f"SECURE_STRING write requires config:write:secret capability (key={key!r})"
            )


def _entries_by_key(
    entries: Mapping[str, ConfigEntry] | Iterable[ConfigEntry],
) -> Mapping[str, ConfigEntry]:
    """Return config entries indexed by key."""
    if isinstance(entries, Mapping):
        return entries
    return {entry.key: entry for entry in entries}
