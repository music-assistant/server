"""
Shared helpers for user-defined wave presets.

Both the settings UI (in ``__init__.py``) and the Browse handler (in
``provider.py``) need to read the same JSON-encoded preset store.
Keeping the decoding + validation in one place avoids schema drift.
"""

from __future__ import annotations

import json


def parse_stored_presets(raw: object) -> list[dict[str, str]]:
    """
    Decode the hidden JSON wave-presets store into a sanitised list.

    Only entries with a non-empty ``name`` string are kept. The optional
    ``diversity`` / ``moodEnergy`` / ``language`` fields are carried through
    as long as they are non-empty *after stripping whitespace* — Yandex
    would reject rotor seeds like ``settingDiversity: `` (space) with a 4xx,
    so we never pass such values through. Stripped form is stored so the
    downstream seed builder gets the canonical value. Any other keys are
    dropped. Malformed JSON, non-list roots or non-dict items yield an
    empty list — the UI treats that as "no presets yet".

    :param raw: Value read from the ``wave_presets_data`` config entry.
    :return: List of sanitised preset dicts in source order.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    result: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        clean: dict[str, str] = {"name": name.strip()}
        for key in ("diversity", "moodEnergy", "language"):
            val = item.get(key)
            if isinstance(val, str):
                val = val.strip()
                if val:
                    clean[key] = val
        result.append(clean)
    return result
