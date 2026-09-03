"""
Loader for the bundled Yandex Dialogs skill logo.

The PNG (``skill_logo.png`` next to this module) is the official Music
Assistant 512x512 PWA icon, used as the catalog / dev-console logo for
the auto-created Yandex Dialogs skill so the skill matches the running
Music Assistant install at a glance.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

__all__ = ["load_skill_logo_bytes"]

_LOGO_PATH = Path(__file__).parent / "skill_logo.png"


@lru_cache(maxsize=1)
def load_skill_logo_bytes() -> bytes:
    """
    Return the bundled Music Assistant logo PNG bytes.

    Falls back to the ya-dialogs-api default logo if the bundled file
    is missing for any reason — keeps the create-skill pipeline alive
    on a broken install rather than failing it.
    """
    try:
        return _LOGO_PATH.read_bytes()
    except OSError:
        from ya_dialogs_api import load_default_logo_bytes  # noqa: PLC0415

        return load_default_logo_bytes()
