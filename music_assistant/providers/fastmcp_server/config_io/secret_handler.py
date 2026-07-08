"""
Gate SECURE_STRING config writes behind the orthogonal secret tag.

The provider performs NO encryption: plaintext is handed to MA's
``save_*_config``, which encrypts via ``Config.to_raw()`` before
persisting. This module only decides whether a write touching a
SECURE_STRING entry is *allowed* given the caller's enabled tags.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError
from music_assistant_models.enums import ConfigEntryType

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.config_entries import ConfigEntry


def is_secret_key(entries: Mapping[str, ConfigEntry], key: str) -> bool:
    """
    Return True iff ``key`` maps to a SECURE_STRING ConfigEntry.

    :param entries: ConfigEntry definitions keyed by config key.
    :param key: The config key to test.
    """
    entry = entries.get(key)
    return entry is not None and entry.type == ConfigEntryType.SECURE_STRING


def gate_secret_writes(
    entries: Mapping[str, ConfigEntry],
    values: Mapping[str, object],
    *,
    secret_tag_enabled: bool,
) -> None:
    """
    Raise ``ToolError`` if ``values`` writes a SECURE_STRING key without the tag.

    The check is atomic — the whole payload is rejected (the provider does
    not split it), naming the first offending secret key.

    :param entries: ConfigEntry definitions for the target.
    :param values: The proposed key→value writes.
    :param secret_tag_enabled: Whether ``config:write:secret`` is enabled.
    """
    if secret_tag_enabled:
        return
    for key in values:
        if is_secret_key(entries, key):
            raise ToolError(f"SECURE_STRING write requires config:write:secret tag (key={key!r})")
