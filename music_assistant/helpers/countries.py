"""Helper for the shared bundled ISO country code list."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import cast


@cache
def get_country_codes() -> dict[str, str]:
    """
    Return the bundled ISO 3166-1 alpha-2 country code map.

    Keys are uppercase two-letter codes (e.g. ``"NL"``), values are the English country name.
    """
    countries_file = resources.files(__package__).joinpath("resources", "countries.json")
    raw = countries_file.read_text(encoding="utf-8")
    return cast("dict[str, str]", json.loads(raw))
